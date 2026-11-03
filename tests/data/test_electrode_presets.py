"""Electrode setup presets: validation, the built-ins, and the store.

No Qt, no instrument. The wiring rules themselves are
:class:`src.data.models.TechniqueConfig`'s; these tests check that a preset
cannot get past them, and that every mode (A, B and C) saves and loads.
"""

from __future__ import annotations

import json
import os

import pytest

from src.data.electrode_presets import (
    ELECTRODE_PRESET_FILE_FORMAT,
    ElectrodePreset,
    ElectrodePresetManager,
    preset_key,
    read_electrode_preset_file,
    write_electrode_preset_file,
)
from src.data.models import (
    EXTERNAL_RE_CE_CHANNEL,
    ON_BOARD_RE_CE_CHANNEL,
    TechniqueConfig,
)
from src.data.presets import PresetManager, read_preset_file
from src.techniques.scripts import generate

MODES = ("external", "on_board", "manual")


def _store(tmp_path) -> str:
    return str(tmp_path / "electrode_presets.mux16el")


# ---- the built-ins ----------------------------------------------------------


def test_the_built_ins_cover_every_mode(tmp_path) -> None:
    mgr = ElectrodePresetManager(_store(tmp_path))
    modes = {p.electrode_config_mode for p in mgr.get_all().values()}
    assert modes == set(MODES)
    assert {"separate_re_ce", "on_chip_re_ce", "per_electrode_re_ce"} <= set(
        mgr.list_presets()
    )


@pytest.mark.parametrize(
    "key",
    [
        "separate_re_ce",
        "on_chip_re_ce",
        "per_electrode_re_ce",
        "die_16_external",
        "die_16_on_chip",
    ],
)
def test_every_built_in_builds_a_config_and_a_script(tmp_path, key) -> None:
    preset = ElectrodePresetManager(_store(tmp_path)).get_preset(key)
    mode, channels, re_ce = preset.wiring()
    config = TechniqueConfig(
        technique="ca",
        params={},
        channels=channels,
        re_ce_channels=re_ce,
        electrode_config_mode=mode,
    )
    assert config.channels == preset.channels
    assert len(config.re_ce_channels) == len(channels)
    script = generate("ca", {"e_dc": 0.1, "t_run": 1.0}, channels, re_ce)
    assert script and script[-1].strip() != ""


def test_mode_a_and_b_put_the_re_ce_where_the_mode_says(tmp_path) -> None:
    mgr = ElectrodePresetManager(_store(tmp_path))
    _, channels, re_ce = mgr.get_preset("separate_re_ce").wiring()
    assert re_ce == [EXTERNAL_RE_CE_CHANNEL] * len(channels)
    _, channels, re_ce = mgr.get_preset("on_chip_re_ce").wiring()
    assert re_ce == [ON_BOARD_RE_CE_CHANNEL] * len(channels)


def test_mode_c_built_in_pairs_each_electrode_with_its_own_position(tmp_path) -> None:
    mode, channels, re_ce = ElectrodePresetManager(_store(tmp_path)).get_preset(
        "per_electrode_re_ce"
    ).wiring()
    assert mode == "manual"
    assert channels == list(range(1, 15)) == re_ce


@pytest.mark.parametrize("key", ["die_16_external", "die_16_on_chip"])
def test_each_die_preset_ignores_the_two_middle_pads(tmp_path, key) -> None:
    preset = ElectrodePresetManager(_store(tmp_path)).get_preset(key)
    assert preset.channels == list(range(1, 17))  # 16 of the 18 pads
    labels = preset.site_labels
    pads = {int(v.split()[1]) for v in labels.values()}
    assert pads == set(range(1, 9)) | set(range(11, 19))  # pads 9 and 10 absent
    assert {labels[str(c)] for c in range(1, 9)} == {f"pad {n}" for n in range(1, 9)}
    assert labels["9"] == "pad 11" and labels["16"] == "pad 18"
    assert [labels[str(c)] for c in preset.channels] == [  # in pad order
        f"pad {n}" for n in (*range(1, 9), *range(11, 19))
    ]


def test_the_two_die_presets_differ_only_in_the_re_ce(tmp_path) -> None:
    mgr = ElectrodePresetManager(_store(tmp_path))
    external = mgr.get_preset("die_16_external")
    on_chip = mgr.get_preset("die_16_on_chip")

    # Same channels, same pad map; only the wiring mode differs.
    assert external.channels == on_chip.channels == list(range(1, 17))
    assert external.site_labels == on_chip.site_labels
    mode, channels, re_ce = external.wiring()
    assert mode == "external" and re_ce == [EXTERNAL_RE_CE_CHANNEL] * 16
    mode, channels, re_ce = on_chip.wiring()
    assert mode == "on_board" and re_ce == [ON_BOARD_RE_CE_CHANNEL] * 16

    # A person can tell them apart by name and by description.
    assert external.name != on_chip.name
    assert "external" in external.name and "on-chip" in on_chip.name
    assert "pads 9, 10" in on_chip.name
    assert "pad 9 is the reference" in on_chip.description
    assert "pad 10 the counter" in on_chip.description
    assert "position 15" in external.describe()
    assert "position 16" in on_chip.describe()
    # The key a user gets by typing a name never collides with a built-in.
    assert preset_key(external.name) not in mgr.list_presets()
    assert preset_key(on_chip.name) not in mgr.list_presets()


def test_no_built_in_asks_for_a_caveat_about_the_pad_numbering(tmp_path) -> None:
    for preset in ElectrodePresetManager(_store(tmp_path)).get_all().values():
        text = (preset.name + " " + preset.description).lower()
        assert "check it against" not in text and "before the first run" not in text


def test_describe_says_what_the_setup_does(tmp_path) -> None:
    mgr = ElectrodePresetManager(_store(tmp_path))
    assert "CH1-CH16" in mgr.get_preset("separate_re_ce").describe()
    assert "position 16" in mgr.get_preset("on_chip_re_ce").describe()
    assert "CH3->CH3" in mgr.get_preset("per_electrode_re_ce").describe()


# ---- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        (dict(name=" "), "needs a name"),
        (dict(name="x", electrode_config_mode="bogus", channels=[1]), "one of"),
        (dict(name="x", channels=[]), "non-empty"),
        (dict(name="x", channels=[True]), "integers"),
        (dict(name="x", channels=[1, 1]), "repeat"),
        (dict(name="x", channels=[0]), "WE channel"),
        (dict(name="x", channels=[17]), "WE channel"),
        (dict(name="x", electrode_config_mode="manual", channels=[1]), "one RE/CE"),
        (
            dict(name="x", electrode_config_mode="manual", channels=[1, 2], re_ce_channels=[1]),
            "one RE/CE",
        ),
        (
            dict(name="x", electrode_config_mode="manual", channels=[15], re_ce_channels=[1]),
            "Mode C",
        ),
        (
            dict(name="x", electrode_config_mode="manual", channels=[1], re_ce_channels=[16]),
            "Mode C",
        ),
        (dict(name="x", channels=[1], re_ce_channels=[3]), "must be empty"),
        (dict(name="x", electrode_config_mode="on_board", channels=[1], re_ce_channels=[3]), "must be empty"),
        (dict(name="x", channels=[1], site_labels={"2": "pad"}), "not in channels"),
        (dict(name="x", channels=[1], site_labels={"a": "pad"}), "not a channel"),
        (dict(name="x", channels=[1], site_labels={"1": 5}), "must be text"),
    ],
)
def test_a_wiring_that_cannot_run_is_refused(kwargs, fragment) -> None:
    with pytest.raises(ValueError, match=fragment):
        ElectrodePreset(**kwargs)


def test_the_mode_is_normalised_and_the_name_trimmed() -> None:
    preset = ElectrodePreset(name="  Mine ", electrode_config_mode="EXTERNAL", channels=[2, 1])
    assert preset.name == "Mine"
    assert preset.electrode_config_mode == "external"
    assert preset.channels == [2, 1]  # run order is kept


def test_preset_key() -> None:
    assert preset_key("16-site die (18 pads)") == "16_site_die_18_pads"
    assert preset_key("  Mode C Test ") == "mode_c_test"
    with pytest.raises(ValueError):
        preset_key("???")


# ---- the store: every mode saves and loads ------------------------------------

SAVED = {
    "my_external": ElectrodePreset("My external", "external", [1, 3, 5], description="a"),
    "my_on_board": ElectrodePreset("My on-board", "on_board", [2, 16]),
    "my_manual": ElectrodePreset("My manual", "manual", [2, 3, 14], [5, 6, 1]),
    "my_labelled": ElectrodePreset("My labelled", "external", [4, 9], site_labels={"4": "A", "9": "B"}),
}


@pytest.mark.parametrize("key", sorted(SAVED))
def test_every_mode_round_trips_through_the_file(tmp_path, key) -> None:
    path = _store(tmp_path)
    ElectrodePresetManager(path).add_preset(key, SAVED[key])
    again = ElectrodePresetManager(path).get_preset(key)
    assert again == SAVED[key]
    assert again.wiring() == SAVED[key].wiring()


def test_mode_c_keeps_its_pairing_in_order(tmp_path) -> None:
    path = _store(tmp_path)
    ElectrodePresetManager(path).add_preset("my_manual", SAVED["my_manual"])
    mode, channels, re_ce = ElectrodePresetManager(path).get_preset("my_manual").wiring()
    assert (mode, channels, re_ce) == ("manual", [2, 3, 14], [5, 6, 1])


def test_nothing_is_written_until_a_preset_is_saved(tmp_path) -> None:
    path = _store(tmp_path)
    mgr = ElectrodePresetManager(path)
    assert mgr.get_preset("die_16_external") is not None
    assert not os.path.exists(path)
    mgr.add_preset("my_external", SAVED["my_external"])
    assert os.path.isfile(path)
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_the_file_holds_only_what_the_person_added(tmp_path) -> None:
    path = _store(tmp_path)
    ElectrodePresetManager(path).add_preset("my_external", SAVED["my_external"])
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["format"] == ELECTRODE_PRESET_FILE_FORMAT
    assert list(data["presets"]) == ["my_external"]


def test_a_built_in_cannot_be_deleted_but_a_replacement_can(tmp_path) -> None:
    path = _store(tmp_path)
    mgr = ElectrodePresetManager(path)
    assert mgr.is_builtin("die_16_external") and not mgr.is_deletable("die_16_external")
    assert mgr.delete_preset("die_16_external") is False
    assert mgr.delete_preset("nope") is False

    mine = ElectrodePreset("Mine", "external", [1, 2])
    mgr.add_preset("die_16_external", mine)  # replaces the built-in
    assert ElectrodePresetManager(path).get_preset("die_16_external").channels == [1, 2]
    assert mgr.is_deletable("die_16_external")
    assert mgr.delete_preset("die_16_external") is True  # brings the built-in back
    assert ElectrodePresetManager(path).get_preset("die_16_external").channels == list(range(1, 17))


def test_a_user_preset_can_be_deleted(tmp_path) -> None:
    path = _store(tmp_path)
    mgr = ElectrodePresetManager(path)
    mgr.add_preset("my_manual", SAVED["my_manual"])
    assert mgr.delete_preset("my_manual") is True
    assert ElectrodePresetManager(path).get_preset("my_manual") is None


def test_the_manager_hands_out_copies(tmp_path) -> None:
    mgr = ElectrodePresetManager(_store(tmp_path))
    mgr.get_preset("separate_re_ce").channels.append(99)
    assert 99 not in mgr.get_preset("separate_re_ce").channels
    mgr.get_all()["separate_re_ce"].channels.clear()
    assert mgr.get_preset("separate_re_ce").channels


def test_a_bad_preset_is_not_added(tmp_path) -> None:
    mgr = ElectrodePresetManager(_store(tmp_path))
    preset = ElectrodePreset("x", "external", [1])
    preset.channels = [99]  # broken after the fact
    with pytest.raises(ValueError):
        mgr.add_preset("x", preset)
    with pytest.raises(ValueError):
        mgr.add_preset("", SAVED["my_external"])
    assert mgr.get_preset("x") is None


# ---- a damaged file never costs the person their presets ---------------------


def test_a_file_that_is_not_json_is_kept_as_a_backup(tmp_path) -> None:
    path = _store(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    mgr = ElectrodePresetManager(path)
    assert mgr.get_preset("die_16_external") is not None  # built-ins still there
    mgr.add_preset("my_external", SAVED["my_external"])
    with open(path + ".bak", encoding="utf-8") as f:
        assert f.read() == "{ not json"
    assert ElectrodePresetManager(path).get_preset("my_external") is not None


def test_an_invalid_entry_is_skipped_and_the_file_is_kept(tmp_path) -> None:
    path = _store(tmp_path)
    good = {"name": "Good", "electrode_config_mode": "external", "channels": [1]}
    bad = {"name": "Bad", "electrode_config_mode": "manual", "channels": [15], "re_ce_channels": [1]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"format": ELECTRODE_PRESET_FILE_FORMAT, "version": 1, "presets": {"good": good, "bad": bad}}, f
        )
    original = open(path, encoding="utf-8").read()
    mgr = ElectrodePresetManager(path)
    assert mgr.get_preset("good") is not None and mgr.get_preset("bad") is None
    mgr.add_preset("my_external", SAVED["my_external"])  # the next save
    with open(path + ".bak", encoding="utf-8") as f:
        assert f.read() == original  # the bad entry is still recoverable


def test_the_strict_reader_names_the_bad_entry(tmp_path) -> None:
    path = _store(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "format": ELECTRODE_PRESET_FILE_FORMAT,
                "presets": {"bad": {"name": "Bad", "channels": [17]}},
            },
            f,
        )
    with pytest.raises(ValueError, match="'bad'"):
        read_electrode_preset_file(path)


def test_the_two_preset_kinds_cannot_be_mistaken_for_each_other(tmp_path) -> None:
    electrode_file = _store(tmp_path)
    write_electrode_preset_file(electrode_file, {"a": SAVED["my_external"]})
    with pytest.raises(ValueError, match="Not a preset file"):
        read_preset_file(electrode_file)  # the measurement-preset reader says no

    measurement_file = str(tmp_path / "presets.mux16")
    PresetManager(path=measurement_file)  # writes a measurement preset store
    with pytest.raises(ValueError, match="Not an electrode preset file"):
        read_electrode_preset_file(measurement_file)
    assert not os.path.exists(electrode_file + ".bak")


def test_saving_a_measurement_preset_leaves_the_electrode_file_alone(tmp_path) -> None:
    electrode_file = _store(tmp_path)
    ElectrodePresetManager(electrode_file).add_preset("my_manual", SAVED["my_manual"])
    before = open(electrode_file, "rb").read()
    PresetManager(path=str(tmp_path / "presets.mux16"))
    assert open(electrode_file, "rb").read() == before

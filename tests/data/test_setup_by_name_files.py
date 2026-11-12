"""Measurement presets and sequence steps that name an electrode setup.

A preset (``*.mux16``) or a sequence step (``*.mux16seq``) may refer to an
electrode setup by name instead of carrying its own wiring. These tests
check that:

* files written before this existed load with exactly the wiring they gave
  before, never consult the setups, and are written back without the new key;
* a named setup supplies the RE/CE of each working electrode, and the
  preset's or step's channels pick which of its electrodes are measured;
* a name that does not resolve fails the load and names the setup (strict
  loads raise; the startup store leaves the preset out, reports it, and
  keeps it in the file);
* a file that names a setup still carries the wiring, so the previous
  version (which drops unknown keys) reads the same wiring;
* the sequence runner's config for a named step equals the setup's wiring.
"""

from __future__ import annotations

import json

import pytest

from src.data.electrode_presets import ElectrodePreset, builtin_electrode_presets
from src.data.models import TechniqueConfig
from src.data.presets import Preset, PresetManager, read_preset_file, write_preset_file
from src.data.sequence import Sequence, SequenceStep, build_config
from src.techniques.scripts import generate, technique_params

# Fields the previous version's dataclasses had (recorded from revision
# d7e1dfb); its loaders keep only these keys of each entry.
OLD_PRESET_FIELDS = {
    "name", "technique", "params", "channels", "auto_save", "description",
    "electrode_config_mode", "re_ce_channels",
}
OLD_STEP_FIELDS = {
    "preset_name", "repeat", "delay_s", "technique", "params", "channels",
    "electrode_config_mode", "re_ce_channels", "channels_override",
    "mode_override",
}


def _setups() -> dict[str, ElectrodePreset]:
    setups = builtin_electrode_presets()
    setups["bench_pairs"] = ElectrodePreset(
        name="Bench pairs", channels=[2, 3, 4], re_ce_channels=[5, 6, 7]
    )
    return setups


def _never():
    raise AssertionError("the setups must not be read for a file that names none")


def _config(item) -> TechniqueConfig:
    re_ce = list(item.re_ce_channels)
    if len(re_ce) != len(item.channels):
        re_ce = []
    return TechniqueConfig(
        technique=item.technique or "ca",
        params={},
        channels=list(item.channels),
        re_ce_channels=re_ce,
        electrode_config_mode=item.electrode_config_mode,
    )


def _wiring(config: TechniqueConfig) -> tuple:
    return (config.electrode_config_mode, config.channels, config.re_ce_channels)


# ---- older files: identical wiring ------------------------------------------

OLD_PRESETS = {
    "ext": {"name": "ext", "technique": "ca", "params": {}, "channels": [1, 2],
            "auto_save": False, "description": "",
            "electrode_config_mode": "external", "re_ce_channels": []},
    "onb": {"name": "onb", "technique": "cv", "params": {}, "channels": [3],
            "auto_save": False, "description": "",
            "electrode_config_mode": "on_board", "re_ce_channels": []},
    "man": {"name": "man", "technique": "eis", "params": {}, "channels": [2, 3],
            "auto_save": False, "description": "",
            "electrode_config_mode": "manual", "re_ce_channels": [5, 6]},
    "ext_list": {"name": "ext_list", "technique": "ca", "params": {},
                 "channels": [4], "auto_save": False, "description": "",
                 "electrode_config_mode": "external", "re_ce_channels": [4]},
    # Written before the electrode mode existed at all.
    "bare": {"name": "bare", "technique": "ca", "params": {}, "channels": [7]},
}


def _expected_old_wiring(raw: dict) -> tuple:
    """What the previous version built from the entry: the raw fields."""
    mode = raw.get("electrode_config_mode", "external")
    config = TechniqueConfig(
        technique=raw["technique"], params={}, channels=list(raw["channels"]),
        re_ce_channels=list(raw.get("re_ce_channels", [])),
        electrode_config_mode=mode,
    )
    return _wiring(config)


def test_an_old_preset_file_loads_with_the_same_wiring_and_saves_unchanged(tmp_path) -> None:
    path = tmp_path / "old.mux16"
    path.write_text(json.dumps(
        {"format": "mux16-presets", "version": 1, "presets": OLD_PRESETS}
    ), encoding="utf-8")
    presets = read_preset_file(str(path), _never)
    for key, raw in OLD_PRESETS.items():
        assert _wiring(_config(presets[key])) == _expected_old_wiring(raw), key
        assert presets[key].electrode_setup == ""

    manager = PresetManager(path=str(tmp_path / "store.mux16"), setups=_never)
    manager.load_from_path(str(path))
    for key, raw in OLD_PRESETS.items():
        assert _wiring(_config(manager.get_preset(key))) == _expected_old_wiring(raw)

    out = tmp_path / "again.mux16"
    write_preset_file(str(out), presets)
    for entry in json.loads(out.read_text(encoding="utf-8"))["presets"].values():
        assert "electrode_setup" not in entry


OLD_STEPS = [
    {"preset_name": "a", "repeat": 1, "delay_s": 0.0, "technique": "ca",
     "params": {}, "channels": [1, 2], "electrode_config_mode": "external",
     "re_ce_channels": [], "channels_override": None, "mode_override": None},
    {"preset_name": "b", "repeat": 2, "delay_s": 1.0, "technique": "cv",
     "params": {}, "channels": [2, 3], "electrode_config_mode": "manual",
     "re_ce_channels": [5, 6], "channels_override": None, "mode_override": None},
    {"preset_name": "c", "repeat": 1, "delay_s": 0.0, "technique": "eis",
     "params": {}, "channels": [9], "electrode_config_mode": "on_board",
     "re_ce_channels": [], "channels_override": None, "mode_override": None},
]


def test_an_old_sequence_file_loads_with_the_same_wiring_and_saves_unchanged(tmp_path) -> None:
    path = tmp_path / "old.mux16seq"
    payload = {"format": "mux16-sequence", "version": 1, "name": "s", "steps": OLD_STEPS}
    path.write_text(json.dumps(payload), encoding="utf-8")
    sequence = Sequence.load_from_path(str(path), _never)
    for raw, step in zip(OLD_STEPS, sequence.steps):
        assert _wiring(build_config(step)) == _expected_old_wiring(raw)
    assert sequence.to_dict() == payload


# ---- a named setup supplies the wiring --------------------------------------


@pytest.mark.parametrize("key", sorted(builtin_electrode_presets()))
def test_a_preset_naming_a_setup_runs_the_setups_wiring(tmp_path, key) -> None:
    """Same config and same script as giving the setup's wiring by hand."""
    setup = builtin_electrode_presets()[key]
    label, channels, re_ce = setup.wiring()
    path = str(tmp_path / "p.mux16")
    write_preset_file(path, {"p": Preset(
        name="p", technique="ca", params=technique_params("ca"),
        channels=list(channels), electrode_setup=key,
    )})
    loaded = read_preset_file(path, builtin_electrode_presets())["p"]
    config = TechniqueConfig(
        technique="ca", params=loaded.params, channels=loaded.channels,
        re_ce_channels=loaded.re_ce_channels,
        electrode_config_mode=loaded.electrode_config_mode,
    )
    assert _wiring(config) == (label, channels, re_ce)
    assert generate("ca", config.params, config.channels, re_ce_channels=config.re_ce_channels) == generate(
        "ca", technique_params("ca"), channels, re_ce_channels=re_ce
    )


def test_channels_pick_some_of_the_setups_electrodes(tmp_path) -> None:
    path = str(tmp_path / "p.mux16")
    write_preset_file(path, {"p": Preset(
        name="p", technique="ca", channels=[4, 2], electrode_setup="Bench pairs",
    )})
    loaded = read_preset_file(path, _setups())["p"]
    assert loaded.electrode_setup == "bench_pairs"  # stored by key from now on
    assert (loaded.channels, loaded.re_ce_channels) == ([4, 2], [7, 5])
    assert loaded.electrode_config_mode == "manual"


def test_the_setup_is_read_again_on_each_load(tmp_path) -> None:
    setups = _setups()
    path = str(tmp_path / "p.mux16")
    write_preset_file(path, {"p": Preset(
        name="p", technique="ca", channels=[2], electrode_setup="bench_pairs",
    )})
    assert read_preset_file(path, setups)["p"].re_ce_channels == [5]
    setups["bench_pairs"] = ElectrodePreset(
        name="Bench pairs", channels=[2], re_ce_channels=[9]
    )
    assert read_preset_file(path, setups)["p"].re_ce_channels == [9]


# ---- a missing setup fails at load ------------------------------------------


def _write_named(path, setup="gone_setup", channels=(1,)) -> None:
    write_preset_file(str(path), {
        "ok": Preset(name="ok", technique="ca", channels=[1]),
        "named": Preset(name="named", technique="ca", channels=list(channels),
                        electrode_setup=setup),
    })


def test_read_preset_file_refuses_a_missing_setup(tmp_path) -> None:
    path = tmp_path / "p.mux16"
    _write_named(path)
    with pytest.raises(ValueError, match="Preset 'named': Unknown electrode setup 'gone_setup'"):
        read_preset_file(str(path), _setups())


def test_import_refuses_a_missing_setup_and_changes_nothing(tmp_path) -> None:
    path = tmp_path / "p.mux16"
    _write_named(path)
    manager = PresetManager(path=str(tmp_path / "store.mux16"), setups=_setups)
    before = (manager.path, manager.list_presets())
    with pytest.raises(ValueError, match="gone_setup"):
        manager.load_from_path(str(path))
    assert (manager.path, manager.list_presets()) == before


def test_a_channel_the_setup_lacks_fails_at_load(tmp_path) -> None:
    path = tmp_path / "p.mux16"
    _write_named(path, setup="bench_pairs", channels=(9,))
    with pytest.raises(ValueError, match=r"Channel\(s\) \[9\] are not working electrodes"):
        read_preset_file(str(path), _setups())


def test_the_store_leaves_a_missing_setup_out_says_so_and_keeps_it(tmp_path) -> None:
    path = tmp_path / "store.mux16"
    _write_named(path)
    manager = PresetManager(path=str(path), setups=_setups)
    assert "named" not in manager.list_presets()
    assert "ok" in manager.list_presets()
    assert len(manager.load_problems) == 1
    assert "'named'" in manager.load_problems[0]
    assert "Unknown electrode setup 'gone_setup'" in manager.load_problems[0]
    # A later save keeps the entry it could not use.
    manager.add_preset("other", Preset(name="other", technique="cv", channels=[2]))
    on_disk = json.loads(path.read_text(encoding="utf-8"))["presets"]
    assert on_disk["named"]["electrode_setup"] == "gone_setup"


def test_a_sequence_step_with_a_missing_setup_fails_at_load(tmp_path) -> None:
    path = tmp_path / "s.mux16seq"
    Sequence(name="s", steps=[
        SequenceStep(technique="ca", channels=[1]),
        SequenceStep(technique="ca", channels=[1], electrode_setup="gone_setup"),
    ]).save_to_path(str(path))
    with pytest.raises(ValueError, match="Step 2: Unknown electrode setup 'gone_setup'"):
        Sequence.load_from_path(str(path), _setups())


# ---- the previous version reads the same wiring -----------------------------


def test_the_previous_version_reads_the_same_wiring_from_a_named_preset(tmp_path) -> None:
    path = tmp_path / "p.mux16"
    write_preset_file(str(path), {
        key: Preset(name=key, technique="ca", channels=[], electrode_setup=key)
        for key in _setups()
    })
    # Saved through the loader once, as the app and the agent do, so the
    # snapshot of the wiring is in the file.
    write_preset_file(str(path), read_preset_file(str(path), _setups()))
    entries = json.loads(path.read_text(encoding="utf-8"))["presets"]
    now = read_preset_file(str(path), _setups())
    for key, raw in entries.items():
        old_view = {k: v for k, v in raw.items() if k in OLD_PRESET_FIELDS}
        assert _expected_old_wiring(old_view) == _wiring(_config(now[key])), key


def test_the_previous_version_reads_the_same_wiring_from_a_named_step(tmp_path) -> None:
    path = tmp_path / "s.mux16seq"
    Sequence(name="s", steps=[
        SequenceStep(technique="ca", channels=[3, 2], electrode_setup="bench_pairs"),
        SequenceStep(technique="ca", channels=[], electrode_setup="on_chip_re_ce"),
    ]).save_to_path(str(path))
    bound = Sequence.load_from_path(str(path), _setups())
    bound.save_to_path(str(path))
    raw_steps = json.loads(path.read_text(encoding="utf-8"))["steps"]
    now = Sequence.load_from_path(str(path), _setups())
    for raw, step in zip(raw_steps, now.steps):
        old = SequenceStep(**{k: v for k, v in raw.items() if k in OLD_STEP_FIELDS})
        assert _wiring(build_config(old)) == _wiring(build_config(step))
    assert _wiring(build_config(now.steps[0])) == ("manual", [3, 2], [6, 5])
    assert _wiring(build_config(now.steps[1])) == ("on_board", list(range(1, 17)), [16] * 16)


def test_a_step_seeded_from_a_named_preset_keeps_the_name() -> None:
    preset = Preset(name="p", technique="ca", channels=[2], electrode_setup="bench_pairs")
    assert SequenceStep.from_preset("p", preset).electrode_setup == "bench_pairs"

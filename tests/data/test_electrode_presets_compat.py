"""A setups file saved now can be read by the previous version.

The previous reader (the one before the single wiring model) accepted an
entry only if:

* ``electrode_config_mode`` was ``external``, ``on_board`` or ``manual``;
* ``channels`` was a non-empty list of distinct integers;
* for ``manual``: ``re_ce_channels`` had one entry per channel, and every
  WE and RE/CE was in 1-14;
* for ``external`` and ``on_board``: ``re_ce_channels`` was EMPTY (the mode
  set the position) and every WE was in 1-16.

``_old_reader_accepts`` is a copy of those rules, taken from
``src/data/electrode_presets.py`` and ``src/data/models.py`` at that
revision, so this test fails if a newly written file stops being readable
there.
"""

from __future__ import annotations

import json

import pytest

from src.data.electrode_presets import (
    ElectrodePreset,
    ElectrodePresetManager,
    read_electrode_preset_file,
    write_electrode_preset_file,
)

_OLD_MODES = ("external", "on_board", "manual")


def _old_reader_accepts(entry: dict) -> None:
    """Raise ValueError where the previous reader raised it."""
    mode = str(entry.get("electrode_config_mode", "external")).lower()
    if mode not in _OLD_MODES:
        raise ValueError(f"unknown mode {mode!r}")
    channels = entry.get("channels")
    if (
        not isinstance(channels, list)
        or not channels
        or not all(isinstance(c, int) and not isinstance(c, bool) for c in channels)
    ):
        raise ValueError("channels must be a non-empty list of integers")
    if len(set(channels)) != len(channels):
        raise ValueError("channels repeat")
    re_ce = list(entry.get("re_ce_channels") or [])
    if mode == "manual":
        if len(re_ce) != len(channels):
            raise ValueError("manual needs one RE/CE per channel")
        for ch in [*channels, *re_ce]:
            if not isinstance(ch, int) or ch < 1 or ch > 14:
                raise ValueError(f"manual channel {ch} out of 1-14")
    else:
        if re_ce:
            raise ValueError(f"the {mode!r} mode sets the RE/CE itself")
        for ch in channels:
            if ch < 1 or ch > 16:
                raise ValueError(f"WE channel {ch} out of 1-16")


_USER_SETUPS = {
    "two_at_15": ElectrodePreset(name="Two at 15", channels=[3, 9], re_ce_channels=[15, 15]),
    "all_at_16": ElectrodePreset(
        name="All at 16", channels=list(range(1, 17)), re_ce_channels=[16] * 16
    ),
    "pairs": ElectrodePreset(name="Pairs", channels=[2, 1, 7], re_ce_channels=[5, 6, 7]),
    "one_shared_low": ElectrodePreset(
        name="Shared 3", channels=[1, 2], re_ce_channels=[3, 3]
    ),
    "labelled": ElectrodePreset(
        name="Labelled",
        channels=[1, 16],
        re_ce_channels=[15, 15],
        site_labels={"16": "pad 18"},
    ),
}


def _saved_file(tmp_path) -> tuple[str, ElectrodePresetManager]:
    path = str(tmp_path / "e.mux16el")
    mgr = ElectrodePresetManager(path)
    for key, preset in _USER_SETUPS.items():
        mgr.add_preset(key, preset)
    # A replaced built-in is written too.
    replaced = mgr.get_preset("separate_re_ce")
    replaced.channels = [1, 2, 3]
    replaced.re_ce_channels = [15, 15, 15]
    mgr.add_preset("separate_re_ce", replaced)
    return path, mgr


def test_every_entry_of_a_new_file_is_accepted_by_the_previous_reader(tmp_path) -> None:
    path, _ = _saved_file(tmp_path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["format"] == "mux16-electrode-presets"
    assert data["version"] == 1
    assert set(data["presets"]) == {*_USER_SETUPS, "separate_re_ce"}
    for key, entry in data["presets"].items():
        _old_reader_accepts(entry)


def test_shared_15_and_16_are_written_in_the_old_form(tmp_path) -> None:
    path, _ = _saved_file(tmp_path)
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)["presets"]
    assert entries["two_at_15"]["electrode_config_mode"] == "external"
    assert entries["two_at_15"]["re_ce_channels"] == []
    assert entries["all_at_16"]["electrode_config_mode"] == "on_board"
    assert entries["all_at_16"]["re_ce_channels"] == []
    assert entries["pairs"]["re_ce_channels"] == [5, 6, 7]


def test_load_save_load_loses_nothing(tmp_path) -> None:
    path, mgr = _saved_file(tmp_path)
    first = read_electrode_preset_file(path)
    again = str(tmp_path / "again.mux16el")
    write_electrode_preset_file(again, first)
    second = read_electrode_preset_file(again)
    assert first == second
    for key, preset in _USER_SETUPS.items():
        assert first[key].wiring() == preset.wiring()
        assert first[key].site_labels == preset.site_labels
        assert ElectrodePresetManager(path).get_preset(key) == mgr.get_preset(key)


def test_built_ins_written_to_a_file_are_old_readable(tmp_path) -> None:
    mgr = ElectrodePresetManager(str(tmp_path / "none.mux16el"))
    path = str(tmp_path / "all.mux16el")
    write_electrode_preset_file(path, mgr.get_all())
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)["presets"]
    for entry in entries.values():
        _old_reader_accepts(entry)
    assert read_electrode_preset_file(path) == mgr.get_all()


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "x", "electrode_config_mode": "external", "channels": [1], "re_ce_channels": [15]},
        {"name": "x", "electrode_config_mode": "manual", "channels": [15], "re_ce_channels": [1]},
    ],
)
def test_the_copied_old_rules_still_refuse_what_they_refused(entry) -> None:
    with pytest.raises(ValueError):
        _old_reader_accepts(entry)

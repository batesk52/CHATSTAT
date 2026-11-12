"""One wiring model: every built-in setup sends the same script as before.

Electrode setups used to be stored as a wiring mode (``external``,
``on_board`` or ``manual``) plus channels; now every setup is a table with
one RE/CE position per working electrode. The instrument only ever sees
the MethodSCRIPT that :func:`src.techniques.scripts.generate` builds from
the channels and the RE/CE list, so these tests freeze the five built-ins
as they were defined in the mode form and check, for every built-in and
every verified technique, that the script the old path produced and the
script the new path produces are identical line for line (the
``set_gpio`` lines carry the multiplexer addresses).
"""

from __future__ import annotations

import json

import pytest

from src.data.electrode_presets import ElectrodePresetManager
from src.data.models import TechniqueConfig
from src.techniques.scripts import generate, supported_techniques, technique_params

#: The built-ins exactly as they were stored before the single wiring model:
#: (mode, channels, re_ce_channels). Do not edit; this is the reference.
OLD_BUILTINS = {
    "separate_re_ce": ("external", list(range(1, 17)), []),
    "on_chip_re_ce": ("on_board", list(range(1, 17)), []),
    "per_electrode_re_ce": ("manual", list(range(1, 15)), list(range(1, 15))),
    "die_16_external": ("external", list(range(1, 17)), []),
    "die_16_on_chip": ("on_board", list(range(1, 17)), []),
}


def _engine_script(config: TechniqueConfig) -> list[str]:
    """The script the measurement engine sends for *config*.

    Mirrors ``MeasurementEngine.start_measurement``: it passes
    ``config.re_ce_channels or None`` to ``generate`` with a copy of the
    parameters.
    """
    return generate(
        config.technique,
        dict(config.params),
        list(config.channels),
        re_ce_channels=getattr(config, "re_ce_channels", None) or None,
    )


def _old_config(key: str, technique: str) -> TechniqueConfig:
    mode, channels, re_ce = OLD_BUILTINS[key]
    return TechniqueConfig(
        technique=technique,
        params=technique_params(technique),
        channels=list(channels),
        re_ce_channels=list(re_ce),
        electrode_config_mode=mode,
    )


def _new_config(key: str, technique: str, tmp_path) -> TechniqueConfig:
    preset = ElectrodePresetManager(str(tmp_path / "none.mux16el")).get_preset(key)
    mode, channels, re_ce = preset.wiring()
    return TechniqueConfig(
        technique=technique,
        params=technique_params(technique),
        channels=channels,
        re_ce_channels=re_ce,
        electrode_config_mode=mode,
    )


def test_the_reference_covers_every_built_in(tmp_path) -> None:
    mgr = ElectrodePresetManager(str(tmp_path / "none.mux16el"))
    assert set(OLD_BUILTINS) == set(mgr.list_presets())


@pytest.mark.parametrize("technique", supported_techniques())
@pytest.mark.parametrize("key", sorted(OLD_BUILTINS))
def test_every_built_in_sends_the_same_script(tmp_path, key, technique) -> None:
    old = _old_config(key, technique)
    new = _new_config(key, technique, tmp_path)
    assert new.channels == old.channels
    assert new.re_ce_channels == old.re_ce_channels
    # The label written into exported files does not change either.
    assert new.electrode_config_mode == old.electrode_config_mode
    old_script = _engine_script(old)
    assert _engine_script(new) == old_script
    assert any("set_gpio" in line for line in old_script)


# ---- files written before the single wiring model --------------------------

#: A setups file as the earlier version wrote it: a label per entry, and an
#: RE/CE list only for per-electrode pairing.
LEGACY_FILE = {
    "format": "mux16-electrode-presets",
    "version": 1,
    "presets": {
        "bench_external": {
            "name": "Bench, external RE/CE",
            "electrode_config_mode": "external",
            "channels": [1, 2, 3, 4],
            "re_ce_channels": [],
            "description": "",
            "site_labels": {},
        },
        "bench_on_board": {
            "name": "Bench, on-board RE/CE",
            "electrode_config_mode": "on_board",
            "channels": [16, 5],
        },
        "bench_pairs": {
            "name": "Two pairs",
            "electrode_config_mode": "manual",
            "channels": [2, 3],
            "re_ce_channels": [5, 6],
        },
        # A user entry that replaced a built-in, in the old form.
        "die_16_on_chip": {
            "name": "16-site die, on-chip RE + CE (pads 9, 10)",
            "electrode_config_mode": "on_board",
            "channels": [1, 2],
        },
    },
}


def _write_legacy(tmp_path) -> str:
    path = str(tmp_path / "electrode_presets.mux16el")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(LEGACY_FILE, f)
    return path


@pytest.mark.parametrize("technique", ["ca", "eis"])
@pytest.mark.parametrize("key", sorted(LEGACY_FILE["presets"]))
def test_an_old_file_loads_with_the_same_wiring(tmp_path, key, technique) -> None:
    mgr = ElectrodePresetManager(_write_legacy(tmp_path))
    assert mgr.load_problems == []
    entry = LEGACY_FILE["presets"][key]
    old = TechniqueConfig(
        technique=technique,
        params=technique_params(technique),
        channels=list(entry["channels"]),
        re_ce_channels=list(entry.get("re_ce_channels", [])),
        electrode_config_mode=entry["electrode_config_mode"],
    )
    mode, channels, re_ce = mgr.get_preset(key).wiring()
    new = TechniqueConfig(
        technique=technique,
        params=technique_params(technique),
        channels=channels,
        re_ce_channels=re_ce,
        electrode_config_mode=mode,
    )
    assert (new.electrode_config_mode, new.channels, new.re_ce_channels) == (
        old.electrode_config_mode,
        old.channels,
        old.re_ce_channels,
    )
    assert _engine_script(new) == _engine_script(old)


def test_an_old_file_is_migrated_to_tables_on_the_next_save(tmp_path) -> None:
    path = _write_legacy(tmp_path)
    mgr = ElectrodePresetManager(path)
    before = {k: mgr.get_preset(k).wiring() for k in LEGACY_FILE["presets"]}
    mgr.add_preset("another", mgr.get_preset("bench_pairs"))
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)["presets"]
    # Shared tables are written in the form the previous version reads
    # (label plus an empty RE/CE list) and expand to the full table on load.
    assert saved["bench_external"]["electrode_config_mode"] == "external"
    assert saved["bench_external"]["re_ce_channels"] == []
    assert saved["bench_on_board"]["electrode_config_mode"] == "on_board"
    assert saved["bench_on_board"]["re_ce_channels"] == []
    assert saved["bench_pairs"]["re_ce_channels"] == list(
        LEGACY_FILE["presets"]["bench_pairs"]["re_ce_channels"]
    )
    again_tables = ElectrodePresetManager(path)
    assert again_tables.get_preset("bench_external").re_ce_channels == [15] * 4
    assert again_tables.get_preset("bench_on_board").re_ce_channels == [16] * 2
    again = ElectrodePresetManager(path)
    assert {k: again.get_preset(k).wiring() for k in LEGACY_FILE["presets"]} == before
    # The replaced built-in is still the person's version, not the built-in.
    assert again.get_preset("die_16_on_chip").channels == [1, 2]

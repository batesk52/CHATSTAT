"""Tests for electrode-config fields on the Preset dataclass.

Batch 2 of WS-electrode-config-modes adds ``electrode_config_mode`` and
``re_ce_channels`` to :class:`src.data.presets.Preset`. These tests
verify dataclass defaults, JSON roundtrip with the new keys, and
graceful fallback when an older JSON file omits them.
"""

from __future__ import annotations

import json
import os

import pytest

from src.data.presets import Preset, PresetManager


def test_preset_defaults_to_external_mode_with_empty_re_ce() -> None:
    """Bare Preset() picks safe defaults matching legacy behaviour."""
    p = Preset(name="x", technique="cv")
    assert p.electrode_config_mode == "external"
    assert p.re_ce_channels == []


def test_preset_manager_loads_legacy_json_without_new_fields(
    tmp_path,
) -> None:
    """A JSON file lacking the new keys still loads with safe defaults."""
    presets_path = tmp_path / "presets.json"
    # Write a legacy-shaped file (no electrode_config_mode key).
    legacy = {
        "legacy_one": {
            "name": "Legacy One",
            "technique": "cv",
            "params": {"e_step": 0.01},
            "channels": [1, 2, 3],
            "auto_save": False,
            "description": "Pre-batch-2 preset",
        }
    }
    presets_path.write_text(json.dumps(legacy), encoding="utf-8")

    mgr = PresetManager(path=str(presets_path))
    preset = mgr.get_preset("legacy_one")
    assert preset is not None
    assert preset.electrode_config_mode == "external"
    assert preset.re_ce_channels == []


def test_preset_manager_loads_json_with_new_fields(tmp_path) -> None:
    """A JSON file carrying the new keys keeps them intact."""
    presets_path = tmp_path / "presets.json"
    data = {
        "manual_pair": {
            "name": "Manual Pair",
            "technique": "cv",
            "params": {},
            "channels": [1, 2, 3],
            "auto_save": False,
            "description": "Mode C example",
            "electrode_config_mode": "manual",
            "re_ce_channels": [1, 1, 13],
        }
    }
    presets_path.write_text(json.dumps(data), encoding="utf-8")

    mgr = PresetManager(path=str(presets_path))
    preset = mgr.get_preset("manual_pair")
    assert preset is not None
    assert preset.electrode_config_mode == "manual"
    assert preset.re_ce_channels == [1, 1, 13]


def test_preset_manager_tolerates_unknown_extra_keys(tmp_path) -> None:
    """Extra keys in JSON do NOT crash the loader."""
    presets_path = tmp_path / "presets.json"
    data = {
        "extra": {
            "name": "Extra",
            "technique": "cv",
            "params": {},
            "channels": [1],
            "auto_save": False,
            "description": "",
            "electrode_config_mode": "external",
            "re_ce_channels": [],
            "futuristic_unknown_field": 42,
        }
    }
    presets_path.write_text(json.dumps(data), encoding="utf-8")

    mgr = PresetManager(path=str(presets_path))
    assert mgr.get_preset("extra") is not None

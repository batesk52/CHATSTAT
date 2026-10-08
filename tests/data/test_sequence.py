"""Tests for the preset sequence model (sequencer feature — Phase 2).

Covers:
  * a 3-step sequence round-trips through ``*.mux16seq`` save/load with
    equality preserved (including per-step overrides), and
  * ``build_config`` lets ``TechniqueConfig.__post_init__`` reject a
    Mode-C step that carries no ``re_ce_channels``.
"""

from __future__ import annotations

import pytest

from src.data.presets import Preset
from src.data.sequence import (
    Sequence,
    SequenceStep,
    build_config,
)


def test_three_step_sequence_round_trip(tmp_path) -> None:
    """A 3-step sequence survives save -> load unchanged."""
    seq = Sequence(
        name="my_sequence",
        steps=[
            SequenceStep(preset_name="cv_one"),
            SequenceStep(
                preset_name="ca_two",
                repeat=3,
                delay_s=2.5,
            ),
            SequenceStep(
                preset_name="manual_three",
                channels_override=[1, 2],
                mode_override="manual",
            ),
        ],
    )

    path = tmp_path / "seq.mux16seq"
    seq.save_to_path(str(path))
    loaded = Sequence.load_from_path(str(path))

    assert loaded == seq
    assert loaded.name == "my_sequence"
    assert len(loaded.steps) == 3
    assert loaded.steps[1].repeat == 3
    assert loaded.steps[1].delay_s == 2.5
    assert loaded.steps[2].channels_override == [1, 2]
    assert loaded.steps[2].mode_override == "manual"


def test_build_config_resolves_preset(tmp_path) -> None:
    """build_config carries preset fields into the TechniqueConfig."""
    preset = Preset(
        name="CV One",
        technique="cv",
        params={"e_step": 0.01},
        channels=[1, 2, 3],
        electrode_config_mode="external",
    )
    step = SequenceStep(preset_name="cv_one")

    cfg = build_config(step, preset)

    assert cfg.technique == "cv"
    assert cfg.channels == [1, 2, 3]
    assert cfg.electrode_config_mode == "external"
    # __post_init__ fills external RE/CE from the mode default.
    assert cfg.re_ce_channels == [15, 15, 15]


def test_build_config_applies_overrides() -> None:
    """Step overrides take precedence over preset values."""
    preset = Preset(
        name="CV One",
        technique="cv",
        params={},
        channels=[1, 2, 3, 4],
        re_ce_channels=[1, 2, 3, 4],
        electrode_config_mode="manual",
    )
    step = SequenceStep(
        preset_name="cv_one",
        channels_override=[5, 6],
        mode_override="external",
    )

    cfg = build_config(step, preset)

    assert cfg.channels == [5, 6]
    assert cfg.electrode_config_mode == "external"


def test_build_config_raises_on_mode_c_empty_re_ce() -> None:
    """A Mode-C step with empty re_ce_channels is rejected."""
    preset = Preset(
        name="Manual Bad",
        technique="cv",
        params={},
        channels=[1, 2],
        electrode_config_mode="manual",
        re_ce_channels=[],
    )
    step = SequenceStep(preset_name="manual_three")

    with pytest.raises(ValueError):
        build_config(step, preset)


def test_from_preset_embeds_a_self_contained_copy() -> None:
    """from_preset snapshots the preset into the step (a copy, not a ref)."""
    preset = Preset(
        name="CV Std",
        technique="cv",
        params={"scan_rate": 0.1, "e_step": 0.01},
        channels=[1, 4],
        electrode_config_mode="external",
    )
    step = SequenceStep.from_preset("cv_std", preset, repeat=2, delay_s=1.0)

    assert step.is_embedded
    assert step.preset_name == "cv_std"  # origin label retained
    assert step.technique == "cv"
    assert step.params == {"scan_rate": 0.1, "e_step": 0.01}
    assert step.channels == [1, 4]
    assert step.repeat == 2 and step.delay_s == 1.0

    # Editing the step must not mutate the source preset.
    step.params["scan_rate"] = 0.5
    step.channels.append(7)
    assert preset.params["scan_rate"] == 0.1
    assert preset.channels == [1, 4]


def test_build_config_embedded_uses_step_values_not_preset() -> None:
    """An embedded step's edited values win; the preset is ignored."""
    preset = Preset(
        name="CV", technique="cv", params={"scan_rate": 0.1}, channels=[1, 4]
    )
    step = SequenceStep.from_preset("cv", preset)
    step.params["scan_rate"] = 0.25  # edit the step
    step.channels = [2, 3, 5]

    # No preset passed -> embedded values still resolve the run.
    cfg = build_config(step, preset=None)
    assert cfg.technique == "cv"
    assert cfg.params["scan_rate"] == 0.25
    assert cfg.channels == [2, 3, 5]


def test_build_config_legacy_without_preset_raises() -> None:
    """A legacy reference step with no resolving preset raises KeyError."""
    step = SequenceStep(preset_name="missing")  # no embedded technique
    with pytest.raises(KeyError):
        build_config(step, preset=None)


def test_embedded_step_round_trips(tmp_path) -> None:
    """An embedded step survives save -> load with its values intact."""
    preset = Preset(
        name="EIS",
        technique="eis",
        params={"freq_start": 50000.0, "freq_end": 5.0},
        channels=[1, 2],
        electrode_config_mode="external",
    )
    step = SequenceStep.from_preset("eis", preset)
    step.params["freq_end"] = 1.0
    seq = Sequence(name="s", steps=[step])

    path = tmp_path / "s.mux16seq"
    seq.save_to_path(str(path))
    loaded = Sequence.load_from_path(str(path))

    s0 = loaded.steps[0]
    assert s0.is_embedded
    assert s0.technique == "eis"
    assert s0.params["freq_end"] == 1.0
    assert s0.channels == [1, 2]
    assert loaded == seq

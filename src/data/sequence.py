"""Preset sequence model and persistence (sequencer feature — Phase 2).

A sequence stacks saved presets as ordered steps and runs them
back-to-back on the MUX-16 (a PSTrace-"Scripts" equivalent).  Steps
reference presets by name and may carry per-step overrides; the
sequence file is portable on its own and is stored SEPARATELY from the
preset store in a ``*.mux16seq`` file.

On-disk format is JSON under a versioned wrapper::

    {"format": "mux16-sequence", "version": 1,
     "name": <str>, "steps": [<asdict(SequenceStep)>, ...]}

``build_config`` resolves a step against its preset (applying overrides)
and constructs a :class:`TechniqueConfig`, letting
``TechniqueConfig.__post_init__`` validate it (Mode-C bounds, RE/CE
length, etc.).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from src.data.models import TechniqueConfig
from src.data.presets import Preset

# On-disk wrapper identity.
SEQUENCE_FILE_FORMAT = "mux16-sequence"
SEQUENCE_FILE_VERSION = 1


@dataclass
class SequenceStep:
    """One self-contained step in a preset sequence.

    A step **carries its own resolved configuration** (technique, params,
    channels, electrode mode). It is seeded from a preset when added, but
    from then on it owns the values: editing a step changes only the step,
    and the sequence trumps the preset store, so a ``*.mux16seq`` is
    portable on its own. ``preset_name`` is retained only as an origin
    label (which preset seeded it).

    Older ``*.mux16seq`` files saved before embedding stored just a
    preset-name reference plus optional overrides; such *legacy* steps
    (no ``technique``) are still honoured by :func:`build_config`, which
    resolves them against the preset store.

    Attributes:
        preset_name: Origin label / legacy resolution key.
        repeat: Number of times the step is run back-to-back (>= 1).
        delay_s: Idle delay (s) inserted AFTER the step (and its repeats).
        technique: Embedded technique id; empty for a legacy reference
            step.
        params: Embedded technique parameters.
        channels: Embedded 1-indexed WE channel list.
        electrode_config_mode: Embedded wiring mode.
        re_ce_channels: Embedded per-WE RE/CE pairing (Mode C).
        channels_override: Legacy-only WE channel override (ignored once
            ``technique`` is set).
        mode_override: Legacy-only electrode-mode override (ignored once
            ``technique`` is set).
    """

    preset_name: str = ""
    repeat: int = 1
    delay_s: float = 0.0
    technique: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    channels: list[int] = field(default_factory=list)
    electrode_config_mode: str = "external"
    re_ce_channels: list[int] = field(default_factory=list)
    channels_override: Optional[list[int]] = None
    mode_override: Optional[str] = None

    @property
    def is_embedded(self) -> bool:
        """True when the step carries its own config (not a legacy ref)."""
        return bool(self.technique)

    @classmethod
    def from_preset(
        cls,
        key: str,
        preset: Preset,
        *,
        repeat: int = 1,
        delay_s: float = 0.0,
    ) -> "SequenceStep":
        """Seed a self-contained step by copying a preset's values.

        Args:
            key: Preset key (kept as the origin label).
            preset: The preset to snapshot into the step.
            repeat: Initial repeat count.
            delay_s: Initial trailing delay.

        Returns:
            A new embedded :class:`SequenceStep`.
        """
        return cls(
            preset_name=key,
            repeat=repeat,
            delay_s=delay_s,
            technique=preset.technique,
            params=dict(preset.params),
            channels=list(preset.channels),
            electrode_config_mode=preset.electrode_config_mode,
            re_ce_channels=list(preset.re_ce_channels),
        )


@dataclass
class Sequence:
    """An ordered, named list of preset steps.

    Attributes:
        name: Display name for the sequence.
        steps: Ordered list of :class:`SequenceStep`.
    """

    name: str
    steps: list[SequenceStep] = field(default_factory=list)

    # -- (de)serialization -------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned wrapper dict for this sequence.

        Returns:
            A JSON-serializable mapping in the on-disk wrapper format.
        """
        return {
            "format": SEQUENCE_FILE_FORMAT,
            "version": SEQUENCE_FILE_VERSION,
            "name": self.name,
            "steps": [asdict(step) for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Sequence":
        """Build a :class:`Sequence` from a parsed wrapper dict.

        Tolerates unknown extra keys on each step so the format can
        evolve.  Missing step fields fall back to their dataclass
        defaults.

        Args:
            data: Parsed JSON object (wrapper format).

        Returns:
            The reconstructed ``Sequence``.
        """
        allowed = set(SequenceStep.__dataclass_fields__.keys())
        steps = [
            SequenceStep(
                **{k: v for k, v in raw.items() if k in allowed}
            )
            for raw in data.get("steps", [])
        ]
        return cls(name=data.get("name", ""), steps=steps)

    def save_to_path(self, path: str) -> None:
        """Write the sequence to a ``*.mux16seq`` file.

        Args:
            path: Destination file path.
        """
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_path(cls, path: str) -> "Sequence":
        """Read a sequence from a ``*.mux16seq`` file.

        Args:
            path: Source file path.

        Returns:
            The loaded ``Sequence``.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


def build_config(
    step: SequenceStep, preset: Optional[Preset] = None
) -> TechniqueConfig:
    """Resolve a step into a validated :class:`TechniqueConfig`.

    For an **embedded** step (the normal case) the step's own technique,
    params, channels, mode and RE/CE pairing are used and ``preset`` is
    ignored — the sequence carries the values. For a **legacy** reference
    step (no embedded technique) the named ``preset`` supplies the config,
    with the step's ``channels_override`` / ``mode_override`` applied.

    Either way the result is validated by ``TechniqueConfig.__post_init__``
    (Mode-C bounds, RE/CE length, channel ranges).

    Args:
        step: The sequence step to resolve.
        preset: The named preset — required only for a legacy step.

    Returns:
        A constructed, validated ``TechniqueConfig``.

    Raises:
        KeyError: If a legacy step is given no resolving ``preset``.
        ValueError: If the resolved configuration is invalid.
    """
    if step.is_embedded:
        channels = list(step.channels)
        re_ce_channels = list(step.re_ce_channels)
        if len(re_ce_channels) != len(channels):
            re_ce_channels = []
        return TechniqueConfig(
            technique=step.technique,
            params=dict(step.params),
            channels=channels,
            re_ce_channels=re_ce_channels,
            electrode_config_mode=step.electrode_config_mode,
        )

    if preset is None:
        raise KeyError(
            f"Sequence step references unknown preset: "
            f"{step.preset_name!r}"
        )
    channels = (
        list(step.channels_override)
        if step.channels_override is not None
        else list(preset.channels)
    )
    mode = (
        step.mode_override
        if step.mode_override is not None
        else preset.electrode_config_mode
    )
    # Carry the preset's explicit RE/CE pairing only when it still matches
    # the resolved channel count; otherwise external/on_board repopulate
    # from the mode default in ``__post_init__`` (and a manual step with no
    # usable pairing raises there — the intended safety behaviour).
    re_ce_channels = list(preset.re_ce_channels)
    if len(re_ce_channels) != len(channels):
        re_ce_channels = []
    return TechniqueConfig(
        technique=preset.technique,
        params=dict(preset.params),
        channels=channels,
        re_ce_channels=re_ce_channels,
        electrode_config_mode=mode,
    )

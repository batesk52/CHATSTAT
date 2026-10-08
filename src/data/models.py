"""Data models for measurements and configuration.

Provides dataclasses for technique configuration, individual data points,
full measurement results with metadata, and per-channel filtered views.
These models form the data layer shared between the engine, GUI, and
export modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

#: A MethodSCRIPT current-range token: integer mantissa plus an optional
#: SI prefix (``100n``, ``2u``, ``1m``). The value is pasted verbatim into
#: ``set_cr`` / ``set_range`` / ``set_autoranging`` lines, so anything else
#: (whitespace, a line break, a second command) must be refused here, the
#: single construction point for every run - GUI, presets, sequences and
#: the agent's tools alike (audit 2026-10-08).
_CR_TOKEN_RE = re.compile(r"^[0-9]+[pnum]?$")


# ---------------------------------------------------------------------------
# Electrode configuration constants
# ---------------------------------------------------------------------------

# Default RE/CE position for the "external" wiring mode: the external
# reference and counter electrodes are wired into MUX RE/CE position 15
# so the user-facing CH1–CH14 stay free for working electrodes.
EXTERNAL_RE_CE_CHANNEL = 15

# Default RE/CE position for the "on-board" wiring mode: the on-board
# RE/CE shorting is routed via MUX RE/CE position 16.
ON_BOARD_RE_CE_CHANNEL = 16

# In "manual" (Mode C) wiring, both WE and RE/CE must stay within
# CH1–CH14 because CH15 and CH16 are reserved as infrastructure
# positions for the external/on-board modes.
MODE_C_MAX_CHANNEL = 14

# Allowed electrode-config mode identifiers (lower-case canonical form).
ELECTRODE_CONFIG_MODES = ("external", "on_board", "manual")


def default_re_ce_channel(electrode_config_mode: str) -> int:
    """Return the RE/CE channel implied by an electrode-config mode.

    Used as a fallback by exporters when an explicit per-channel
    ``re_ce_channels`` list is unavailable (e.g. legacy/directly
    constructed results). Mirrors ``TechniqueConfig.__post_init__`` so
    exported provenance stays consistent with the wiring the mode
    implies, instead of the historical hardcoded ``1``.

    Note: ``manual`` mode has no single mode-implied RE/CE position (it
    is per-channel), so it falls through to the external default (15).
    A manual result should always carry an explicit ``re_ce_channels``
    list, so this fallback is only a last resort for malformed/legacy
    inputs and is not expected on the normal engine path.

    Args:
        electrode_config_mode: One of ``ELECTRODE_CONFIG_MODES``.

    Returns:
        The RE/CE MUX channel for that mode (on_board -> 16, otherwise
        -> 15).
    """
    if electrode_config_mode == "on_board":
        return ON_BOARD_RE_CE_CHANNEL
    return EXTERNAL_RE_CE_CHANNEL


@dataclass
class AutoSaveConfig:
    """Configuration for incremental auto-save during measurement.

    When enabled, CSV data is flushed to disk at each MUX loop
    boundary so that data is preserved even if the application
    crashes or the user aborts mid-experiment.

    Attributes:
        enabled: Whether auto-save is active.
        output_dir: Base directory for auto-saved files. A timestamped
            subdirectory is created automatically unless ``exact_dir``
            is set.
        exact_dir: When True, ``output_dir`` IS the run directory —
            no timestamped subdirectory is created. Used by the
            sequencer to give every step run (repeats included) its own
            collision-free ``stepNN_<technique>`` folder.
    """

    enabled: bool = False
    output_dir: str = ""
    exact_dir: bool = False


@dataclass
class TechniqueConfig:
    """Configuration for an electrochemical technique.

    Bundles the technique name, its parameters, the list of MUX
    channels to measure on, and the electrode-config wiring policy.

    Attributes:
        technique: Lowercase technique identifier (e.g., 'cv', 'dpv',
            'eis'). Must match a key in the technique script registry.
        params: Technique-specific parameter dictionary. Keys and
            expected types vary by technique (e.g., ``e_begin``,
            ``scan_rate``, ``freq_start``).
        channels: 1-indexed MUX channel numbers to include in the
            measurement (e.g., ``[1, 2, 5]``).
        auto_save: Optional auto-save configuration. When provided and
            enabled, measurement data is written incrementally to CSV.
        re_ce_channels: Optional parallel list of 1-indexed RE/CE
            positions, one per entry in ``channels``. When empty, the
            list is populated from ``electrode_config_mode`` in
            ``__post_init__``.
        electrode_config_mode: One of ``"external"`` (RE/CE on
            position 15), ``"on_board"`` (RE/CE on position 16), or
            ``"manual"`` (operator-supplied per-step RE/CE within
            CH1–CH14).
    """

    technique: str
    params: dict[str, Any]
    channels: list[int]
    auto_save: Optional[AutoSaveConfig] = None
    continuous: bool = False
    re_ce_channels: list[int] = field(default_factory=list)
    electrode_config_mode: str = "external"

    def __post_init__(self) -> None:
        """Normalise technique + mode and populate / validate RE/CE.

        Mode-driven defaults:
            * ``external`` -> ``[EXTERNAL_RE_CE_CHANNEL] * N``
            * ``on_board`` -> ``[ON_BOARD_RE_CE_CHANNEL] * N``
            * ``manual``   -> caller must supply ``re_ce_channels``

        Raises:
            ValueError: If the mode is unknown, the RE/CE list length
                does not match channels, or channel ranges violate
                the mode's wiring rules.
        """
        self.technique = self.technique.lower()
        self.electrode_config_mode = self.electrode_config_mode.lower()

        cr = self.params.get("cr") if isinstance(self.params, dict) else None
        if cr is not None and (
            not isinstance(cr, str) or not _CR_TOKEN_RE.fullmatch(cr)
        ):
            raise ValueError(
                "cr must be a MethodSCRIPT current-range token such as "
                f"'100n', '2u' or '1m'; got {cr!r}"
            )

        if self.electrode_config_mode not in ELECTRODE_CONFIG_MODES:
            raise ValueError(
                "electrode_config_mode must be one of "
                f"'external'/'on_board'/'manual', "
                f"got {self.electrode_config_mode!r}"
            )

        if not self.re_ce_channels:
            if self.electrode_config_mode == "external":
                self.re_ce_channels = [
                    EXTERNAL_RE_CE_CHANNEL
                ] * len(self.channels)
            elif self.electrode_config_mode == "on_board":
                self.re_ce_channels = [
                    ON_BOARD_RE_CE_CHANNEL
                ] * len(self.channels)
            else:  # manual
                raise ValueError(
                    "Mode C (manual) requires explicit re_ce_channels; "
                    "cannot be empty"
                )

        if len(self.re_ce_channels) != len(self.channels):
            raise ValueError(
                "re_ce_channels length must match channels length "
                f"(got {len(self.re_ce_channels)} vs "
                f"{len(self.channels)})"
            )

        if self.electrode_config_mode in ("external", "on_board"):
            for ch in self.channels:
                if not isinstance(ch, int) or ch < 1 or ch > 16:
                    raise ValueError(
                        f"WE channel {ch!r} not allowed in "
                        f"{self.electrode_config_mode!r} mode; "
                        "must be an integer in range 1-16"
                    )
        else:  # manual
            for ch in self.channels:
                if (
                    not isinstance(ch, int)
                    or ch < 1
                    or ch > MODE_C_MAX_CHANNEL
                ):
                    raise ValueError(
                        f"Mode C WE channel {ch} not allowed; must be "
                        f"in range 1-{MODE_C_MAX_CHANNEL} "
                        "(CH15+CH16 are infrastructure-reserved)"
                    )
            for ch in self.re_ce_channels:
                if (
                    not isinstance(ch, int)
                    or ch < 1
                    or ch > MODE_C_MAX_CHANNEL
                ):
                    raise ValueError(
                        f"Mode C RE/CE channel {ch} not allowed; must "
                        f"be in range 1-{MODE_C_MAX_CHANNEL} "
                        "(CH15+CH16 are infrastructure-reserved)"
                    )


@dataclass
class DataPoint:
    """A single decoded measurement sample.

    Represents one data packet from the device, containing one or more
    named measurement values (potential, current, impedance, etc.).

    Attributes:
        timestamp: Time of acquisition in seconds relative to
            measurement start. ``None`` if not yet assigned.
        channel: 1-indexed MUX channel that produced this sample.
        variables: Mapping of variable names to float values
            (e.g., ``{'set_potential': 0.5, 'current': 1.2e-6}``).
        overload: True when the device flagged a hard current overload
            for this sample (the reading railed the pinned current
            range). Carried from the packet's status metadata so the
            agent layer can detect an under-ranged run and re-range.
            Defaults to False (no overload / status not reported).
    """

    timestamp: Optional[float]
    channel: int
    variables: dict[str, float] = field(default_factory=dict)
    overload: bool = False

    def get(self, name: str, default: float = 0.0) -> float:
        """Return the value of a named variable.

        Args:
            name: Variable name (e.g., 'current', 'set_potential').
            default: Value to return if the variable is not present.

        Returns:
            The float value, or *default* if not found.
        """
        return self.variables.get(name, default)


@dataclass
class MeasurementResult:
    """Complete result of a measurement run.

    Collects all data points from a single measurement execution along
    with metadata describing the run.

    Attributes:
        data_points: Ordered list of all decoded data points.
        technique: Lowercase technique identifier used for this run.
        start_time: Wall-clock time when the measurement started.
        device_info: Optional dict with device metadata (e.g.,
            ``{'firmware': '...', 'serial': '...'}``).
        params: Copy of the technique parameters used.
        channels: List of channels that were measured.
        re_ce_channels: Parallel list of 1-indexed RE/CE positions, one
            per entry in ``channels``. Empty when the run was performed
            without explicit per-channel RE/CE addressing.
        electrode_config_mode: Wiring mode in effect for the run
            (``"external"`` / ``"on_board"`` / ``"manual"``). Defaults
            to ``"external"`` to match ``TechniqueConfig``.
    """

    data_points: list[DataPoint] = field(default_factory=list)
    technique: str = ""
    start_time: Optional[datetime] = None
    device_info: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    channels: list[int] = field(default_factory=list)
    re_ce_channels: list[int] = field(default_factory=list)
    electrode_config_mode: str = "external"

    def add_point(self, point: DataPoint) -> None:
        """Append a data point to the result.

        Args:
            point: The decoded data point to add.
        """
        self.data_points.append(point)

    def channel_data(self, channel: int) -> ChannelData:
        """Return a filtered view for a single channel.

        Args:
            channel: 1-indexed channel number.

        Returns:
            A ``ChannelData`` instance containing only the data points
            from the specified channel.
        """
        filtered = [
            dp for dp in self.data_points if dp.channel == channel
        ]
        return ChannelData(
            channel=channel,
            data_points=filtered,
            technique=self.technique,
            params=self.params,
        )

    @property
    def num_points(self) -> int:
        """Return the total number of data points."""
        return len(self.data_points)

    @property
    def measured_channels(self) -> list[int]:
        """Return sorted list of channels that have data points."""
        return sorted({dp.channel for dp in self.data_points})


@dataclass
class ChannelData:
    """Filtered view of measurement data for a single channel.

    Provides convenient access to the data points belonging to one
    MUX channel within a ``MeasurementResult``.

    Attributes:
        channel: 1-indexed channel number.
        data_points: Data points for this channel only.
        technique: Technique identifier.
        params: Technique parameters.
    """

    channel: int
    data_points: list[DataPoint] = field(default_factory=list)
    technique: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def num_points(self) -> int:
        """Return the number of data points for this channel."""
        return len(self.data_points)

    def values(self, name: str) -> list[float]:
        """Extract a list of values for a named variable.

        Args:
            name: Variable name (e.g., 'current').

        Returns:
            List of float values in acquisition order. Points that
            lack the variable are skipped.
        """
        return [
            dp.variables[name]
            for dp in self.data_points
            if name in dp.variables
        ]

    def timestamps(self) -> list[float]:
        """Return timestamps for all data points in this channel.

        Returns:
            List of timestamp floats. Points with ``None`` timestamps
            are excluded.
        """
        return [
            dp.timestamp
            for dp in self.data_points
            if dp.timestamp is not None
        ]

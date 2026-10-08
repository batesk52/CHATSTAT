"""PsSessionExporter: writes PSTrace-compatible .pssession files.

Produces UTF-16 LE encoded, minified JSON with BOM and trailing BOM
that matches the PalmSens PSTrace serialisation exactly. Supports
CV, LSV, DPV, SWV, CA, and EIS techniques.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

from src.data.models import MeasurementResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UNIT_VOLT: dict[str, str] = {
    "Type": "PalmSens.Units.Volt",
    "S": "V",
    "Q": "Potential",
    "A": "E",
}

UNIT_MICRO_AMPERE: dict[str, str] = {
    "Type": "PalmSens.Units.MicroAmpere",
    "S": "A",
    "Q": "Current",
    "A": "i",
}

UNIT_TIME: dict[str, str] = {
    "Type": "PalmSens.Units.Time",
    "S": "s",
    "Q": "Time",
    "A": "t",
}

UNIT_MICRO_COULOMB: dict[str, str] = {
    "Type": "PalmSens.Units.MicroCoulomb",
    "S": "C",
    "Q": "Charge",
    "A": "Q",
}

# Technique name → integer code used in PSTrace method strings.
# These are the MethodSCRIPT measurement-technique IDs (manual v1.6
# Table 5); PSTrace's METHOD "TECHNIQUE=" field uses the same enum.
# Verified against native PSTrace .pssession files (CV=5, SWV=2, CA=7,
# EIS=14). The old values for lsv/dpv/swv were wrong (2/3/4 = LSV's
# neighbours), so PSTrace mislabeled the technique.
_TECHNIQUE_NUMBER: dict[str, int] = {
    "lsv": 0,
    "dpv": 1,
    "swv": 2,
    "npv": 3,
    "acv": 4,
    "cv": 5,
    "fcv": 5,
    "ca": 7,
    "ca_alt_mux": 7,
    "pad": 8,
    "eis": 14,  # PSTrace-specific (EIS is not in Table 5)
}

# Technique name → friendly title
_TECHNIQUE_TITLES: dict[str, str] = {
    "cv": "Cyclic Voltammetry",
    "lsv": "Linear Sweep Voltammetry",
    "dpv": "Differential Pulse Voltammetry",
    "swv": "Square Wave Voltammetry",
    "ca": "Chronoamperometry",
    "ca_alt_mux": "Chronoamperometry",
    "eis": "Impedance Spectroscopy",
}

# PSTrace method-string key overrides per technique. Our generic
# uppercased param dump (E_DC, E_VERTEX1, FREQ_START, ...) is NOT what
# PSTrace's method parser reads, so PSTrace fell back to template
# defaults for those fields. These map our param names to the keys
# PSTrace actually uses, verified against native PSTrace .pssession
# files (CA/CV/SWV/EIS) — see docs/references/pstrace_method_keys.md.
# ``t_eq -> T_EQUIL`` applies to every technique.
_PSTRACE_COMMON_KEYS: dict[str, str] = {"t_eq": "T_EQUIL"}
_PSTRACE_METHOD_KEYS: dict[str, dict[str, str]] = {
    "ca": {"e_dc": "E"},
    "ca_alt_mux": {"e_dc": "E"},
    "cv": {"e_vertex1": "E_VTX1", "e_vertex2": "E_VTX2"},
    "fcv": {"e_vertex1": "E_VTX1", "e_vertex2": "E_VTX2"},
    "swv": {"amplitude": "E_AMP", "frequency": "FREQ"},
    "acv": {"amplitude": "E_AMP", "frequency": "FREQ"},
    "eis": {
        "freq_start": "MAX_FREQ",
        "freq_end": "MIN_FREQ",
        "e_dc": "E",
        "e_ac": "AMPLITUDE",
    },
    # geis: frequency keys follow EIS; i_dc/i_ac unverified (no reference).
    "geis": {"freq_start": "MAX_FREQ", "freq_end": "MIN_FREQ"},
}


def _pstrace_method_keys(technique: str) -> dict[str, str]:
    """Return the param→PSTrace-key overrides for a technique."""
    return {
        **_PSTRACE_COMMON_KEYS,
        **_PSTRACE_METHOD_KEYS.get(technique, {}),
    }


# EIS techniques that use ImpedimetricMeasurement type
_EIS_TECHNIQUES = {"eis", "geis"}

# Techniques that use curves (non-EIS)
_CURVE_TECHNIQUES = {
    "cv", "lsv", "dpv", "swv", "ca", "ca_alt_mux",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def datetime_to_dotnet_ticks(dt: datetime) -> int:
    """Convert a datetime to .NET local DateTime ticks.

    .NET ticks are 100-nanosecond intervals since 0001-01-01 00:00:00.
    This returns ticks for the local time representation.
    """
    unix_ts = dt.timestamp()
    return int(unix_ts * 10_000_000) + 621_355_968_000_000_000


def datetime_to_dotnet_utc_ticks(dt: datetime) -> int:
    """Convert a datetime to .NET UTC DateTime ticks.

    Returns ticks for the UTC representation.
    """
    if dt.tzinfo is not None:
        utc_ts = dt.astimezone(timezone.utc).timestamp()
    else:
        utc_ts = dt.timestamp()
    # For UTC ticks we add the UTC offset
    utc_offset_seconds = time.timezone
    if time.daylight and time.localtime(dt.timestamp()).tm_isdst:
        utc_offset_seconds = time.altzone
    utc_ticks = (
        int(utc_ts * 10_000_000)
        + 621_355_968_000_000_000
        + utc_offset_seconds * 10_000_000
    )
    return utc_ticks


def default_appearance(color: str = "-16776961") -> dict[str, Any]:
    """Return a default PalmSens VisualSettings dict."""
    return {
        "Type": "PalmSens.Plottables.VisualSettings",
        "AutoAssignColor": True,
        "Color": color,
        "LineWidth": 2,
        "SymbolSize": 5,
        "SymbolType": 0,
        "SymbolFill": True,
        "NoLine": False,
    }


def default_appearance_subscan(
    color: str = "-16776961",
) -> dict[str, Any]:
    """Return a VisualSettings for EIS frequency sub-scan curves."""
    return {
        "Type": "PalmSens.Plottables.VisualSettings",
        "AutoAssignColor": True,
        "Color": color,
        "LineWidth": 1,
        "SymbolSize": 5,
        "SymbolType": 0,
        "SymbolFill": True,
        "NoLine": False,
    }


def random_hash() -> list[int]:
    """Return a 48-byte random hash (like SHA-384)."""
    return [random.randint(0, 255) for _ in range(48)]


def build_method_string(
    method_id: str, result: MeasurementResult
) -> str:
    """Build a PSTrace-compatible MethodForMeasurement string.

    Includes the 3-line PSTrace header and TECHNIQUE as integer code.
    """
    technique = result.technique.lower()
    tech_num = _TECHNIQUE_NUMBER.get(technique, 0)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "#PSTrace, Version=5.12.1031.30690",
        f"#{now_str}",
        "#",
        "#Method file version",
        "METHOD_VERSION=1",
        "#Technique and application",
        f"METHOD_ID={method_id}",
        f"TECHNIQUE={tech_num}",
        "NOTES=",
    ]

    # Map our param names to the keys PSTrace actually reads (per
    # technique). Without this, PSTrace ignores the param and shows its
    # template default. See _PSTRACE_METHOD_KEYS / the reference docs.
    key_overrides = _pstrace_method_keys(technique)

    # Add technique parameters in scientific notation
    for k, v in result.params.items():
        key_upper = key_overrides.get(k, k.upper())
        if isinstance(v, bool):
            lines.append(f"{key_upper}={v}")
        elif isinstance(v, float):
            lines.append(f"{key_upper}={v:.7E}")
        elif isinstance(v, int):
            lines.append(f"{key_upper}={v}")
        else:
            lines.append(f"{key_upper}={v}")

    # MUX channels
    if result.channels:
        ch_bits = sum(1 << (ch - 1) for ch in result.channels)
        lines.append(f"USE_MUX_CH={ch_bits}")

    # Electrode-config provenance — emits the wiring mode + a
    # per-channel WE->RE/CE mapping so downstream re-imports can
    # tell which RE/CE position was active for each curve.  Back-compat:
    # an absent or empty mode falls back to "external" with RE/CE=1.
    mode = (
        getattr(result, "electrode_config_mode", "") or "external"
    )
    lines.append(f"ELECTRODE_CONFIG_MODE={mode}")
    re_ce_list = getattr(result, "re_ce_channels", None) or []
    if result.channels and re_ce_list:
        # Pair WE -> RE/CE positions in declaration order; truncate at
        # the shorter list so we never index past either edge.
        pairs = [
            f"{we}:{re_ce}"
            for we, re_ce in zip(result.channels, re_ce_list)
        ]
        lines.append(
            "RE_CE_CHANNELS=" + ",".join(pairs)
        )

    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# PsSessionExporter
# ---------------------------------------------------------------------------


class PsSessionExporter:
    """Exports measurement results to PalmSens .pssession format.

    The .pssession format is a UTF-16 LE encoded JSON file with BOM
    used by PalmSens PSTrace software and compatible with CMU.49.011
    analysis pipelines.

    Example usage::

        exporter = PsSessionExporter()
        path = exporter.export_pssession(result, "run.pssession")
    """

    def export_pssession(
        self,
        result: MeasurementResult,
        output_path: str,
    ) -> str:
        """Write a .pssession file for a measurement result.

        Creates parent directories if they do not exist. The file is
        written with UTF-16 BOM, minified JSON, and trailing BOM to
        match PSTrace output exactly.

        Args:
            result: The measurement result to export.
            output_path: Destination file path.

        Returns:
            Absolute path to the written file.
        """
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        session = self._build_session(result)

        # Write raw bytes: BOM + minified JSON as UTF-16-LE + trailing BOM
        json_str = json.dumps(
            session, separators=(",", ":"), ensure_ascii=False
        )
        payload = (
            b"\xff\xfe"  # UTF-16 LE BOM
            + json_str.encode("utf-16-le")
            + "\ufeff".encode("utf-16-le")  # trailing BOM
        )
        # Write to a temp file, then os.replace onto the destination. The
        # replace is atomic on the same filesystem, so a reader sees either
        # the old file or the fully-written new one — never a half-written
        # file, and a crash mid-write can't destroy a prior good copy (the
        # old in-place "wb" open truncated output_path immediately). Note:
        # full crash durability of the new directory entry would also need
        # an fsync of the parent directory, which is not done here.
        tmp_path = f"{output_path}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, output_path)
        except OSError:
            # Clean up the temp file so a failed export leaves no debris.
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        abs_path = os.path.abspath(output_path)
        logger.info("Wrote .pssession file: %s", abs_path)
        return abs_path

    # -- Internal -----------------------------------------------------------

    @staticmethod
    def _build_session(
        result: MeasurementResult,
    ) -> dict[str, Any]:
        """Build the PalmSens-compatible session JSON structure."""
        # Lazy imports to avoid circular dependency
        from src.data.pssession_curves import (
            build_curves_measurement,
        )
        from src.data.pssession_eis import (
            build_eis_measurement,
        )

        technique = result.technique.lower()
        method_id = technique
        if technique in ("ca", "ca_alt_mux", "fca"):
            method_id = "ad"
        method_str = build_method_string(method_id, result)

        title = _TECHNIQUE_TITLES.get(technique, technique.upper())

        if technique in _EIS_TECHNIQUES:
            measurement = build_eis_measurement(
                result, title, method_str
            )
        else:
            measurement = build_curves_measurement(
                result, title, method_str
            )

        # Electrode-config metadata at session level — backward-compat
        # defaults mirror the METHOD string fields above.  Per-channel
        # RE/CE assignment is encoded in the ``MUXChannel`` /
        # ``ReCeChannel`` fields on each Curve / EISData entry.
        electrode_mode = (
            getattr(result, "electrode_config_mode", "") or "external"
        )
        re_ce_list = (
            getattr(result, "re_ce_channels", None) or []
        )

        session: dict[str, Any] = {
            "Type": "PalmSens.DataFiles.SessionFile",
            "CoreVersion": "5.12.1031.0",
            "MethodForMeasurement": method_str,
            "Measurements": [measurement],
            "ElectrodeConfigMode": electrode_mode,
            "ReCeChannels": list(re_ce_list),
        }
        return session

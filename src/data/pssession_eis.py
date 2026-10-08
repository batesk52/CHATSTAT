"""EIS .pssession builder.

Builds the Measurement dict with EISDataList and DataSetEIS for
impedance spectroscopy techniques. Matches PSTrace serialisation
with all 22 data arrays in the correct order.
"""

from __future__ import annotations

import math
from typing import Any

from src.data.models import MeasurementResult, default_re_ce_channel
from src.data.pssession_exporter import (
    UNIT_MICRO_AMPERE,
    UNIT_TIME,
    UNIT_VOLT,
    datetime_to_dotnet_ticks,
    datetime_to_dotnet_utc_ticks,
    default_appearance,
    default_appearance_subscan,
    random_hash,
)

# ---------------------------------------------------------------------------
# EIS-specific unit definitions
# ---------------------------------------------------------------------------

UNIT_HERTZ: dict[str, str] = {
    "Type": "PalmSens.Units.Hertz",
    "S": "Hz",
    "Q": "Frequency",
    "A": "f",
}

UNIT_ZRE: dict[str, str] = {
    "Type": "PalmSens.Units.ZRe",
    "S": "\u03a9",
    "Q": "Z'",
    "A": "Z",
}

UNIT_ZIM: dict[str, str] = {
    "Type": "PalmSens.Units.ZIm",
    "S": "\u03a9",
    "Q": "-Z''",
    "A": "Z",
}

UNIT_Z: dict[str, str] = {
    "Type": "PalmSens.Units.Z",
    "S": "\u03a9",
    "Q": "Z",
    "A": "Z",
}

UNIT_PHASE: dict[str, str] = {
    "Type": "PalmSens.Units.Phase",
    "S": "\u00b0",
    "Q": "-Phase",
    "A": "Phase",
}

UNIT_Y: dict[str, str] = {
    "Type": "PalmSens.Units.Y",
    "S": "S",
    "Q": "Y",
    "A": "Y",
}

UNIT_YRE: dict[str, str] = {
    "Type": "PalmSens.Units.YRe",
    "S": "S",
    "Q": "Y'",
    "A": "Y",
}

UNIT_YIM: dict[str, str] = {
    "Type": "PalmSens.Units.YIm",
    "S": "S",
    "Q": "Y''",
    "A": "Y",
}

UNIT_FARAD: dict[str, str] = {
    "Type": "PalmSens.Units.Farad",
    "S": "F",
    "Q": "C",
    "A": "Cs",
}

UNIT_FAHRAD_REAL: dict[str, str] = {
    "Type": "PalmSens.Units.FahradReal",
    "S": "F",
    "Q": "C'",
    "A": "C'",
}

UNIT_FAHRAD_IMAGINARY: dict[str, str] = {
    "Type": "PalmSens.Units.FahradImaginary",
    "S": "F",
    "Q": "-C''",
    "A": "C''",
}


def _fixed_unit(s: str) -> dict[str, Any]:
    """Return a FixedUnit dict with the given S value."""
    return {
        "Type": "PalmSens.Units.FixedUnit",
        "S": s,
        "Q": None,
        "A": None,
    }


# ---------------------------------------------------------------------------
# EIS measurement builder
# ---------------------------------------------------------------------------


def build_eis_measurement(
    result: MeasurementResult,
    title: str,
    method_str: str,
) -> dict[str, Any]:
    """Build a complete Measurement dict for EIS techniques.

    Creates one EISDataList entry per channel, each with a full
    22-array DataSetEIS. Curves is empty for EIS.

    Args:
        result: The measurement result.
        title: Measurement title (e.g. "Impedance Spectroscopy").
        method_str: The Method string for PSTrace.

    Returns:
        Measurement dict matching PSTrace EIS structure.
    """
    measured = result.measured_channels

    # Per-channel RE/CE lookup for electrode-config metadata on each
    # EISData entry.  Backward compat: legacy results without
    # re_ce_channels fall back to RE/CE = 1 (the historical "shared
    # reference" assumption).
    electrode_mode = (
        getattr(result, "electrode_config_mode", "") or "external"
    )
    re_ce_list = getattr(result, "re_ce_channels", []) or []
    re_ce_by_channel: dict[int, int] = {}
    for cfg_idx, cfg_ch in enumerate(result.channels):
        if cfg_idx < len(re_ce_list):
            re_ce_by_channel[cfg_ch] = re_ce_list[cfg_idx]

    eis_data_list: list[dict[str, Any]] = []
    last_dataset: dict[str, Any] | None = None

    for ch in measured:
        ch_data = result.channel_data(ch)

        # Extract raw EIS data
        freqs = ch_data.values("set_frequency")
        zreals = ch_data.values("zreal")
        zimags = ch_data.values("zimag")
        n_freq = len(freqs)

        if n_freq == 0:
            continue

        # Compute derived quantities
        z_mag = []
        phase = []
        y_re = []
        y_im = []
        y_mag = []
        cap = []
        cap_re = []
        cap_im = []

        for i in range(n_freq):
            zr = zreals[i] if i < len(zreals) else 0.0
            zi = zimags[i] if i < len(zimags) else 0.0
            f = freqs[i] if i < len(freqs) else 1.0

            # Z magnitude and phase
            z = math.sqrt(zr * zr + zi * zi)
            ph = math.degrees(math.atan2(zi, zr))
            z_mag.append(z)
            phase.append(ph)

            # Admittance: Y = 1/Z
            denom = zr * zr + zi * zi
            if denom > 0:
                yr = zr / denom
                yi_val = -zi / denom
            else:
                yr = 0.0
                yi_val = 0.0
            y_re.append(yr)
            y_im.append(yi_val)
            y_mag.append(math.sqrt(yr * yr + yi_val * yi_val))

            # Capacitance: C = Y / (2*pi*f)
            omega = 2.0 * math.pi * f if f > 0 else 1.0
            cap.append(
                math.sqrt(yr * yr + yi_val * yi_val) / omega
            )
            cap_re.append(yr / omega)
            cap_im.append(yi_val / omega)

        # Placeholder zero-rows for unused DataSetEIS arrays. These are
        # templates: every consumer below copies them per-row via
        # ``[dict(r) for r in ...]`` so no dict instance is shared either
        # within an array or across the (many) arrays that reuse a
        # template — avoids the ``[{...}] * n`` / ``list(...)`` aliasing
        # trap where a future per-row mutation would change every row.
        n = n_freq
        zeros_generic = [{"V": 0.0} for _ in range(n)]
        zeros_current = [{"V": 0.0, "C": 7, "S": 0} for _ in range(n)]
        zeros_voltage = [{"V": 0.0, "S": 0, "R": 7} for _ in range(n)]

        dataset_eis: dict[str, Any] = {
            "Type": "PalmSens.Data.DataSetEIS",
            "Values": [
                # [0] Idc
                {
                    "Type": "PalmSens.Data.DataArrayCurrents",
                    "ArrayType": 2,
                    "Description": "Idc",
                    "DataValueType": (
                        "PalmSens.Data.CurrentReading"
                    ),
                    "Unit": UNIT_MICRO_AMPERE,
                    "DataValues": [dict(r) for r in zeros_current],
                },
                # [1] potential
                {
                    "Type": "PalmSens.Data.DataArrayPotentials",
                    "ArrayType": 1,
                    "Description": "potential",
                    "DataValueType": (
                        "PalmSens.Data.VoltageReading"
                    ),
                    "Unit": UNIT_VOLT,
                    "DataValues": [dict(r) for r in zeros_voltage],
                },
                # [2] time
                {
                    "Type": "PalmSens.Data.DataArrayTime",
                    "ArrayType": 0,
                    "Description": "time",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_TIME,
                    "DataValues": [dict(r) for r in zeros_generic],
                },
                # [3] Frequency
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 5,
                    "Description": "Frequency",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_HERTZ,
                    "DataValues": [{"V": v} for v in freqs],
                },
                # [4] ZRe
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 7,
                    "Description": "ZRe",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_ZRE,
                    "DataValues": [{"V": v} for v in zreals],
                },
                # [5] ZIm
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 8,
                    "Description": "ZIm",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_ZIM,
                    "DataValues": [{"V": -v} for v in zimags],
                },
                # [6] Z
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 10,
                    "Description": "Z",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_Z,
                    "DataValues": [{"V": v} for v in z_mag],
                },
                # [7] Phase
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 6,
                    "Description": "Phase",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_PHASE,
                    "DataValues": [{"V": -v} for v in phase],
                },
                # [8] Iac
                {
                    "Type": "PalmSens.Data.DataArrayCurrents",
                    "ArrayType": 9,
                    "Description": "Iac",
                    "DataValueType": (
                        "PalmSens.Data.CurrentReading"
                    ),
                    "Unit": UNIT_MICRO_AMPERE,
                    "DataValues": [dict(r) for r in zeros_current],
                },
                # [9] miDC
                {
                    "Type": "PalmSens.Data.DataArrayCurrents",
                    "ArrayType": 36,
                    "Description": "miDC",
                    "DataValueType": (
                        "PalmSens.Data.CurrentReading"
                    ),
                    "Unit": UNIT_MICRO_AMPERE,
                    "DataValues": [dict(r) for r in zeros_current],
                },
                # [10] mEdc
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 33,
                    "Description": "mEdc",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_VOLT,
                    "DataValues": [dict(r) for r in zeros_generic],
                },
                # [11] Eac
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 34,
                    "Description": "Eac",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_VOLT,
                    "DataValues": [dict(r) for r in zeros_generic],
                },
                # [12] nPointsAC
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 16383,
                    "Description": "nPointsAC",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": _fixed_unit("npoints"),
                    "DataValues": [dict(r) for r in zeros_generic],
                },
                # [13] realtintac
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 16384,
                    "Description": "realtintac",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": _fixed_unit("tint"),
                    "DataValues": [dict(r) for r in zeros_generic],
                },
                # [14] ymean
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 16385,
                    "Description": "ymean",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": _fixed_unit("ymean"),
                    "DataValues": [dict(r) for r in zeros_generic],
                },
                # [15] debugtext
                {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 16386,
                    "Description": "debugtext",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": _fixed_unit(""),
                    "DataValues": [dict(r) for r in zeros_generic],
                },
                # [16] Y (admittance magnitude)
                {
                    "Type": (
                        "PalmSens.Data.DataArrayAdmittance"
                    ),
                    "ArrayType": 11,
                    "Description": "Y",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_Y,
                    "DataValues": [{"V": v} for v in y_mag],
                },
                # [17] YRe
                {
                    "Type": (
                        "PalmSens.Data.DataArrayAdmittance"
                    ),
                    "ArrayType": 12,
                    "Description": "YRe",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_YRE,
                    "DataValues": [{"V": v} for v in y_re],
                },
                # [18] YIm
                {
                    "Type": (
                        "PalmSens.Data.DataArrayAdmittance"
                    ),
                    "ArrayType": 13,
                    "Description": "YIm",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_YIM,
                    "DataValues": [{"V": v} for v in y_im],
                },
                # [19] Capacitance
                {
                    "Type": (
                        "PalmSens.Data.DataArrayCustomFunc"
                    ),
                    "ArrayType": 14,
                    "Description": "Capacitance",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_FARAD,
                    "DataValues": [{"V": v} for v in cap],
                },
                # [20] Capacitance'
                {
                    "Type": (
                        "PalmSens.Data.DataArrayCustomFunc"
                    ),
                    "ArrayType": 15,
                    "Description": "Capacitance'",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_FAHRAD_REAL,
                    "DataValues": [{"V": v} for v in cap_re],
                },
                # [21] Capacitance''
                {
                    "Type": (
                        "PalmSens.Data.DataArrayCustomFunc"
                    ),
                    "ArrayType": 16,
                    "Description": "Capacitance''",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "Unit": UNIT_FAHRAD_IMAGINARY,
                    "DataValues": [{"V": v} for v in cap_im],
                },
            ],
        }

        # EISDataList entry.  ``MUXChannel`` / ``ReCeChannel`` /
        # ``ElectrodeConfigMode`` record the wiring that produced this
        # impedance spectrum (electrode-config metadata follow-up).
        eis_entry: dict[str, Any] = {
            "Appearance": default_appearance(),
            "Title": f"CH {ch}: {n_freq} freqs",
            "Hash": random_hash(),
            "Type": "PalmSens.Plottables.EISData",
            "MUXChannel": ch,
            "ReCeChannel": re_ce_by_channel.get(
                ch, default_re_ce_channel(electrode_mode)
            ),
            "ElectrodeConfigMode": electrode_mode,
            "ScanType": 2,
            "FreqType": 1,
            "CDC": None,
            "FitValues": [],
            "AppearanceFrequencySubScanCurves": [
                [
                    default_appearance_subscan(),
                    default_appearance_subscan(),
                ]
                for _ in range(n_freq)
            ],
            "TitleFrequencySubScanCurves": [
                ["", ""] for _ in range(n_freq)
            ],
            "DataSet": dataset_eis,
        }
        eis_data_list.append(eis_entry)

        last_dataset = dataset_eis  # always assign — PSTrace uses last channel

    # Build timestamps
    ts = 0
    utc_ts = 0
    if result.start_time is not None:
        ts = datetime_to_dotnet_ticks(result.start_time)
        utc_ts = datetime_to_dotnet_utc_ticks(result.start_time)

    # Measurement-level DataSet matches the last channel's DataSetEIS
    meas_dataset = last_dataset if last_dataset else {
        "Type": "PalmSens.Data.DataSetEIS",
        "Values": [],
    }

    measurement: dict[str, Any] = {
        "Title": title,
        "TimeStamp": ts,
        "UTCTimeStamp": utc_ts,
        "DeviceUsed": 9,
        "DeviceSerial": result.device_info.get("serial", ""),
        "DeviceFW": result.device_info.get("firmware", ""),
        "Type": (
            "PalmSens.Techniques.ImpedimetricMeasurement"
        ),
        "DataSet": meas_dataset,
        "Method": method_str,
        "Curves": [],
        "EISDataList": eis_data_list,
    }
    return measurement

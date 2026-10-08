"""Curve-based .pssession builders for CV, LSV, DPV, SWV, CA.

Builds the Measurement dict including Curves and DataSet for
non-EIS techniques. Matches PSTrace serialisation structure.
"""

from __future__ import annotations

from typing import Any

from src.data.models import MeasurementResult, default_re_ce_channel
from src.data.pssession_exporter import (
    UNIT_MICRO_AMPERE,
    UNIT_MICRO_COULOMB,
    UNIT_TIME,
    UNIT_VOLT,
    datetime_to_dotnet_ticks,
    datetime_to_dotnet_utc_ticks,
    default_appearance,
    random_hash,
)

# Voltammetry techniques (potential on X axis)
_VOLTAMMETRY = {"cv", "lsv", "dpv", "swv"}

# Amperometry techniques (time on X axis)
_AMPEROMETRY = {"ca", "ca_alt_mux", "fca"}

# Colors that PSTrace cycles through
_COLORS = [
    "-16776961",  # blue
    "-65536",  # red
    "-16744448",  # green
    "-23296",  # orange
    "-8388480",  # purple
    "-16777077",  # dark blue
]


def build_curves_measurement(
    result: MeasurementResult,
    title: str,
    method_str: str,
) -> dict[str, Any]:
    """Build a complete Measurement dict for curve-based techniques.

    For CV: one curve per scan per channel.
    For CA: one curve per channel.
    For LSV/DPV/SWV: one curve per channel.

    Args:
        result: The measurement result.
        title: Measurement title (e.g. "Cyclic Voltammetry").
        method_str: The Method string for PSTrace.

    Returns:
        Measurement dict matching PSTrace structure.
    """
    technique = result.technique.lower()
    measured = result.measured_channels

    # Per-channel RE/CE lookup so each curve records the wiring that
    # produced it.  When re_ce_channels is absent or shorter than
    # channels, fall back to the RE/CE position implied by the electrode
    # mode (external/manual -> 15, on_board -> 16) via
    # default_re_ce_channel().
    electrode_mode = (
        getattr(result, "electrode_config_mode", "") or "external"
    )
    re_ce_list = getattr(result, "re_ce_channels", []) or []
    re_ce_by_channel: dict[int, int] = {}
    for cfg_idx, ch in enumerate(result.channels):
        if cfg_idx < len(re_ce_list):
            re_ce_by_channel[ch] = re_ce_list[cfg_idx]

    def _re_ce_for(ch: int) -> int:
        return re_ce_by_channel.get(
            ch, default_re_ce_channel(electrode_mode)
        )

    curves: list[dict[str, Any]] = []
    dataset_values: list[dict[str, Any]] = []
    color_idx = 0

    if technique in _VOLTAMMETRY:
        abbrev = technique.upper()
        n_scans = result.params.get("n_scans", 1)
        if not isinstance(n_scans, int) or n_scans < 1:
            n_scans = 1

        # Build time array from first channel data
        first_ch = measured[0] if measured else 1
        first_ch_data = result.channel_data(first_ch)
        n_total = len(first_ch_data.data_points)
        pts_per_scan = n_total // n_scans if n_scans > 0 else n_total
        # NOTE: when n_total is not evenly divisible by n_scans the trailing
        # (n_total % n_scans) points are not written to the .pssession (the
        # CSV keeps every point). Recovering them naively makes the last
        # scan longer than the others, which leaves the shared DataSet time
        # array and the per-scan arrays mismatched in length — PSTrace may
        # reject that. Proper fix is deferred until it can be validated by
        # opening an uneven-split CV in PSTrace (bench item).

        # Time DataArray for DataSet (zero-based)
        time_values = first_ch_data.timestamps()
        if time_values:
            t0 = time_values[0]
            time_zeroed = [t - t0 for t in time_values]
            interval = (
                time_zeroed[-1] / (len(time_zeroed) - 1)
                if len(time_zeroed) > 1
                else 0.05
            )
        else:
            time_zeroed = []
            interval = 0.05

        time_arr: dict[str, Any] = {
            "Type": "PalmSens.Data.DataArrayTime",
            "ArrayType": 0,
            "Description": "time",
            "DataValueType": "PalmSens.Data.GenericValue",
            "IntervalTime": interval,
            "Unit": UNIT_TIME,
            "DataValues": [{"V": t} for t in time_zeroed[:pts_per_scan]],
        }
        dataset_values.append(time_arr)

        for ch in measured:
            ch_data = result.channel_data(ch)
            all_potentials = ch_data.values("set_potential")
            all_currents = ch_data.values("current")
            all_timestamps = ch_data.timestamps()
            ch_desc = f"channel{ch}"

            for scan in range(n_scans):
                start = scan * pts_per_scan
                end = start + pts_per_scan
                pot_slice = all_potentials[start:end]
                cur_slice = all_currents[start:end]
                time_slice = all_timestamps[start:end]

                # Convert current to µA
                cur_ua = [c * 1e6 for c in cur_slice]

                # Compute charge via trapezoidal integration (µC)
                charge_vals = _trapezoidal_charge(
                    time_slice, cur_slice
                )

                color = _COLORS[color_idx % len(_COLORS)]
                color_idx += 1

                # Curve for plots
                curve: dict[str, Any] = {
                    "Appearance": default_appearance(color),
                    "Title": (
                        f"{abbrev} i vs E Scan {scan + 1} "
                        f"Channel {ch}"
                    ),
                    "Hash": random_hash(),
                    "Type": "PalmSens.Plottables.Curve",
                    "XAxis": 0,
                    "YAxis": 0,
                    "MeasType": 1 if len(curves) == 0 else 0,
                    "MUXChannel": ch,
                    "ReCeChannel": _re_ce_for(ch),
                    "ElectrodeConfigMode": electrode_mode,
                    "CorrosionButlerVolmer": [0, 0],
                    "CorrosionTafel": [0, 0, 0, 0],
                    "XAxisDataArray": {
                        "Type": "PalmSens.Data.DataArrayPotentials",
                        "ArrayType": 1,
                        "Description": ch_desc,
                        "DataValueType": (
                            "PalmSens.Data.VoltageReading"
                        ),
                        "Unit": UNIT_VOLT,
                        "DataValues": [
                            {"V": v, "S": 0, "R": 7}
                            for v in pot_slice
                        ],
                    },
                    "YAxisDataArray": {
                        "Type": "PalmSens.Data.DataArray",
                        "ArrayType": 2,
                        "Description": ch_desc,
                        "DataValueType": (
                            "PalmSens.Data.CurrentReading"
                        ),
                        "Unit": UNIT_MICRO_AMPERE,
                        "DataValues": [
                            {"V": v, "C": 3, "S": 0}
                            for v in cur_ua
                        ],
                    },
                }
                curves.append(curve)

                # DataSet arrays: pot, cur, charge per scan per ch
                pot_arr: dict[str, Any] = {
                    "Type": "PalmSens.Data.DataArrayPotentials",
                    "ArrayType": 1,
                    "Description": ch_desc,
                    "DataValueType": (
                        "PalmSens.Data.VoltageReading"
                    ),
                    "Unit": UNIT_VOLT,
                    "DataValues": [
                        {"V": v, "S": 0, "R": 7}
                        for v in pot_slice
                    ],
                }
                cur_arr: dict[str, Any] = {
                    "Type": "PalmSens.Data.DataArrayCurrents",
                    "ArrayType": 2,
                    "Description": ch_desc,
                    "DataValueType": (
                        "PalmSens.Data.CurrentReading"
                    ),
                    "Unit": UNIT_MICRO_AMPERE,
                    "DataValues": [
                        {"V": v, "C": 3, "S": 0}
                        for v in cur_ua
                    ],
                }
                chg_arr: dict[str, Any] = {
                    "Type": "PalmSens.Data.DataArrayCharge",
                    "ArrayType": 3,
                    "Description": ch_desc,
                    "DataValueType": "PalmSens.Data.GenericValue",
                    "Unit": UNIT_MICRO_COULOMB,
                    "DataValues": [{"V": v} for v in charge_vals],
                }
                dataset_values.extend([pot_arr, cur_arr, chg_arr])

    elif technique in _AMPEROMETRY:
        # Build shared time axis (zero-based) from first channel
        first_ch = measured[0] if measured else 1
        first_ch_data = result.channel_data(first_ch)
        time_values = first_ch_data.timestamps()

        if time_values:
            t0 = time_values[0]
            time_zeroed = [t - t0 for t in time_values]
            interval = (
                time_zeroed[-1] / (len(time_zeroed) - 1)
                if len(time_zeroed) > 1
                else 0.5
            )
        else:
            time_zeroed = []
            interval = 0.5

        time_arr = {
            "Type": "PalmSens.Data.DataArrayTime",
            "ArrayType": 0,
            "Description": "time",
            "DataValueType": "PalmSens.Data.GenericValue",
            "IntervalTime": interval,
            "Unit": UNIT_TIME,
            "DataValues": [{"V": t} for t in time_zeroed],
        }
        dataset_values.append(time_arr)

        for ch in measured:
            ch_data = result.channel_data(ch)
            raw_times = ch_data.timestamps()
            currents = ch_data.values("current")
            potentials = ch_data.values("set_potential")
            ch_desc = f"channel{ch}"

            # Zero-base time for this channel
            if raw_times:
                ch_t0 = raw_times[0]
                ch_time_zeroed = [t - ch_t0 for t in raw_times]
            else:
                ch_time_zeroed = list(time_zeroed)

            cur_ua = [c * 1e6 for c in currents]
            charge_vals = _trapezoidal_charge(raw_times, currents)

            color = _COLORS[color_idx % len(_COLORS)]
            color_idx += 1

            # Compute per-channel interval from zero-based times
            ch_interval = (
                ch_time_zeroed[1] - ch_time_zeroed[0]
                if len(ch_time_zeroed) >= 2
                else 0.5
            )

            curve = {
                "Appearance": default_appearance(color),
                "Title": f"CA i vs t Channel {ch}",
                "Hash": random_hash(),
                "Type": "PalmSens.Plottables.Curve",
                "XAxis": 0,
                "YAxis": 0,
                "MeasType": 1 if len(curves) == 0 else 0,
                "MUXChannel": ch,
                "ReCeChannel": _re_ce_for(ch),
                "ElectrodeConfigMode": electrode_mode,
                "CorrosionButlerVolmer": [0, 0],
                "CorrosionTafel": [0, 0, 0, 0],
                "XAxisDataArray": {
                    "Type": "PalmSens.Data.DataArrayTime",
                    "ArrayType": 0,
                    "Description": "time",
                    "DataValueType": (
                        "PalmSens.Data.GenericValue"
                    ),
                    "IntervalTime": ch_interval,
                    "Unit": UNIT_TIME,
                    "DataValues": [
                        {"V": t} for t in ch_time_zeroed
                    ],
                },
                "YAxisDataArray": {
                    "Type": "PalmSens.Data.DataArray",
                    "ArrayType": 2,
                    "Description": ch_desc,
                    "DataValueType": (
                        "PalmSens.Data.CurrentReading"
                    ),
                    "Unit": UNIT_MICRO_AMPERE,
                    "DataValues": [
                        {"V": v, "C": 3, "S": 0}
                        for v in cur_ua
                    ],
                },
            }
            curves.append(curve)

            # DataSet arrays: pot, cur, charge per channel
            if not potentials:
                potentials = [0.0] * len(currents)
            pot_arr = {
                "Type": "PalmSens.Data.DataArrayPotentials",
                "ArrayType": 1,
                "Description": ch_desc,
                "DataValueType": "PalmSens.Data.VoltageReading",
                "Unit": UNIT_VOLT,
                "DataValues": [
                    {"V": v, "S": 0, "R": 7} for v in potentials
                ],
            }
            cur_arr = {
                "Type": "PalmSens.Data.DataArrayCurrents",
                "ArrayType": 2,
                "Description": ch_desc,
                "DataValueType": "PalmSens.Data.CurrentReading",
                "Unit": UNIT_MICRO_AMPERE,
                "DataValues": [
                    {"V": v, "C": 3, "S": 0} for v in cur_ua
                ],
            }
            chg_arr = {
                "Type": "PalmSens.Data.DataArrayCharge",
                "ArrayType": 3,
                "Description": ch_desc,
                "DataValueType": "PalmSens.Data.GenericValue",
                "Unit": UNIT_MICRO_COULOMB,
                "DataValues": [{"V": v} for v in charge_vals],
            }
            dataset_values.extend([pot_arr, cur_arr, chg_arr])

    else:
        # Generic fallback — same as amperometry layout
        pass

    dataset = {
        "Type": "PalmSens.Data.DataSetCommon",
        "Values": dataset_values,
    }

    # Build timestamps
    ts = 0
    utc_ts = 0
    if result.start_time is not None:
        ts = datetime_to_dotnet_ticks(result.start_time)
        utc_ts = datetime_to_dotnet_utc_ticks(result.start_time)

    measurement: dict[str, Any] = {
        "Title": title,
        "TimeStamp": ts,
        "UTCTimeStamp": utc_ts,
        "DeviceUsed": 9,
        "DeviceSerial": result.device_info.get("serial", ""),
        "DeviceFW": result.device_info.get("firmware", ""),
        "Type": "PalmSens.Comm.GenericCommMeasurement",
        "DataSet": dataset,
        "Method": method_str,
        "Curves": curves,
        "EISDataList": [],
    }
    return measurement


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trapezoidal_charge(
    timestamps: list[float],
    currents_a: list[float],
) -> list[float]:
    """Compute cumulative charge (µC) by trapezoidal integration.

    Args:
        timestamps: Time values in seconds.
        currents_a: Current values in amperes.

    Returns:
        List of cumulative charge values in microcoulombs.
    """
    n = len(timestamps)
    if n == 0 or len(currents_a) == 0:
        return []

    charge = [0.0]
    for i in range(1, min(n, len(currents_a))):
        dt = timestamps[i] - timestamps[i - 1]
        avg_current = (currents_a[i] + currents_a[i - 1]) / 2.0
        # charge in coulombs, then convert to µC
        q = charge[-1] + avg_current * dt * 1e6
        charge.append(q)
    return charge

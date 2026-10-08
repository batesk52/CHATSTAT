#!/usr/bin/env python3
"""
Lightweight PalmSens .pssession File Parser

Parses PalmSens .pssession files (UTF-16 encoded JSON) and extracts
individual experiments for EIS and CV analysis.

Author: Claude Code
Date: 2025-10-29
"""

import json
import logging
import re
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Union
from ..utils.path_utils import intelligent_path_handler

logger = logging.getLogger(__name__)


def _extract_method_id(measurement: Dict[str, Any]) -> str:
    """
    Extract METHOD_ID from measurement method string.

    Returns: 'ad' (CA), 'cv', 'eis', or '' (unknown)
    """
    # Try various possible key names for the method string
    method_str = (measurement.get('method', '') or
                  measurement.get('Method', '') or
                  measurement.get('methodformeasurement', '') or
                  measurement.get('MethodForMeasurement', ''))

    # Parse METHOD_ID=xxx from method string
    for line in method_str.split('\n'):
        if line.startswith('METHOD_ID='):
            return line.split('=')[1].strip().lower()

    return ''


def _detect_channel_groups(
    curves: List[Dict[str, Any]],
) -> Dict[int, List[int]]:
    """Scan curve data arrays for channelN descriptions.

    Inspects the ``Description`` field of each curve's
    ``XAxisDataArray`` (or ``YAxisDataArray``) for the
    ``channel(\\d+)`` pattern (case-insensitive).

    Args:
        curves: List of curve dictionaries from a PalmSens
            measurement.

    Returns:
        Dict mapping channel number (int) to a list of curve
        indices that belong to that channel. Returns an empty
        dict when no channel labels are detected.
    """
    channel_pattern = re.compile(r"channel(\d+)", re.IGNORECASE)
    groups: Dict[int, List[int]] = {}

    for idx, curve in enumerate(curves):
        if not isinstance(curve, dict):
            continue

        # Check Description on XAxisDataArray then YAxisDataArray
        for key in (
            "XAxisDataArray",
            "xaxisdataarray",
            "YAxisDataArray",
            "yaxisdataarray",
        ):
            data_array = curve.get(key)
            if not isinstance(data_array, dict):
                continue
            desc = (
                data_array.get("Description")
                or data_array.get("description")
                or ""
            )
            match = channel_pattern.search(desc)
            if match:
                ch_num = int(match.group(1))
                groups.setdefault(ch_num, []).append(idx)
                break  # Found channel for this curve

    if groups:
        logger.debug(
            "Detected %d channel groups across %d curves",
            len(groups),
            sum(len(v) for v in groups.values()),
        )
    return groups


def _parse_channel_list(title: str) -> Dict[int, int]:
    """Parse a comma-separated channel list from a measurement title.

    Titles like ``"Channels 1, 3, 5, 7, 9, 11, 13, 15"`` or
    ``"Channels 4, 5, 13, 15"`` encode the mapping from MUX port
    (1-based positional index) to true electrode (BLADE channel)
    number.

    Args:
        title: Measurement title string.

    Returns:
        Dict mapping MUX port (1-based) to BLADE electrode number,
        or empty dict if no comma-list pattern found.

    Examples:
        "Channels 1, 3, 5, 7, 9, 11, 13, 15"
            -> {1: 1, 2: 3, 3: 5, 4: 7, 5: 9, 6: 11, 7: 13, 8: 15}
        "Channels 4, 5, 13, 15" -> {1: 4, 2: 5, 3: 13, 4: 15}
    """
    # Match "channels 1, 3, 5" - require at least one comma so a
    # single channel like "Channel 5" is not interpreted as a list.
    match = re.search(
        r"channels?\s+(\d+(?:\s*,\s*\d+)+)",
        title,
        re.IGNORECASE,
    )
    if not match:
        logger.debug(
            "No channel-list pattern found in title: '%s'", title
        )
        return {}

    list_str = match.group(1)
    electrodes = [int(x.strip()) for x in list_str.split(',')]
    mapping = {
        port: electrode
        for port, electrode in enumerate(electrodes, start=1)
    }

    logger.debug(
        "Parsed channel list from '%s': %s", title, mapping
    )
    return mapping


def _parse_channel_range(title: str) -> Dict[int, int]:
    """Parse a channel range from a measurement title.

    Titles like ``"chip1 - Board 3 - channels 18-11"`` encode
    the mapping from MUX port number to true electrode number.
    A descending range (18-11) means Ch1→18, Ch2→17, ..., Ch8→11.
    An ascending range (1-8) means Ch1→1, Ch2→2, ..., Ch8→8.

    Args:
        title: Measurement title string.

    Returns:
        Dict mapping MUX port (1-based) to true electrode number,
        or empty dict if no range pattern found.
    """
    # Match: "channel(s) 18-11", "Channel 18 - 11", etc.
    match = re.search(
        r'channels?\s+(\d+)\s*-\s*(\d+)', title, re.IGNORECASE
    )
    if not match:
        logger.debug(
            "No channel range pattern found in title: '%s'",
            title,
        )
        return {}

    start = int(match.group(1))
    end = int(match.group(2))

    if start <= end:
        # Ascending: 1-8 → Ch1→1, Ch2→2, ...
        mapping = {
            port: electrode
            for port, electrode in enumerate(
                range(start, end + 1), start=1
            )
        }
    else:
        # Descending: 18-11 → Ch1→18, Ch2→17, ...
        mapping = {
            port: electrode
            for port, electrode in enumerate(
                range(start, end - 1, -1), start=1
            )
        }

    logger.debug(
        "Parsed channel range from '%s': %s", title, mapping
    )
    return mapping


def _extract_mux_curves(
    curves: List[Dict[str, Any]],
    method_id: str,
    curve_index: int,
    channel_groups: Dict[int, List[int]],
) -> Dict[str, pd.DataFrame]:
    """Extract per-channel DataFrames from multiplexed curves.

    Delegates to ``_extract_curves_dataframe`` for each
    channel's subset using pre-computed channel groups.

    Args:
        curves: Full list of curve dicts from the measurement.
        method_id: Method identifier ('cv', 'ad', etc.).
        curve_index: Which curve to select per channel when
            multiple exist (-1 = last).
        channel_groups: Pre-computed dict mapping channel number
            (int) to list of curve indices, from
            ``_detect_channel_groups()``.

    Returns:
        Dict mapping ``"Ch{N}"`` labels to DataFrames.
    """
    results: Dict[str, pd.DataFrame] = {}

    for ch_num in sorted(channel_groups):
        indices = channel_groups[ch_num]
        ch_curves = [curves[i] for i in indices]
        df = _extract_curves_dataframe(
            ch_curves, method_id, curve_index
        )
        if df is not None and not df.empty:
            results[f"Ch{ch_num}"] = df

    return results


def _extract_mux_info(method_string: str) -> Dict[str, Any]:
    """Parse MUX settings from a measurement method string.

    Extracts informational fields such as ``MUX_METHOD``,
    ``USE_MUX_CH``, ``MUX_SETTINGS``, and
    ``MUX_NO_TIME_RESET`` from the multi-line method string
    stored in PalmSens session data. This is purely
    informational; channel detection is data-driven via
    ``_detect_channel_groups``.

    Args:
        method_string: The raw method string from a
            measurement dict (may contain ``\\r\\n`` line
            endings).

    Returns:
        Dict with parsed MUX fields. Empty dict if no MUX
        settings found.
    """
    info: Dict[str, Any] = {}

    # Normalise line endings
    text = method_string.replace("\r\n", "\n").replace("\r", "\n")

    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("MUX_METHOD="):
            try:
                info["mux_method"] = int(
                    line.split("=", 1)[1]
                )
            except ValueError:
                info["mux_method"] = line.split("=", 1)[1]

        elif line.startswith("USE_MUX_CH="):
            raw = line.split("=", 1)[1]
            # Value may be a bitmask int or comma-separated
            if "," in raw:
                parsed_channels = []
                for x in raw.split(","):
                    try:
                        parsed_channels.append(int(x.strip()))
                    except ValueError:
                        logger.warning(
                            "Non-integer value '%s' in "
                            "USE_MUX_CH list, skipping",
                            x.strip(),
                        )
                info["use_mux_ch"] = parsed_channels
            else:
                try:
                    info["use_mux_ch"] = int(raw)
                except ValueError:
                    info["use_mux_ch"] = raw

        elif line.startswith("MUX_SETTINGS="):
            info["mux_settings"] = line.split("=", 1)[1]

        elif line.startswith("MUX_NO_TIME_RESET="):
            val = line.split("=", 1)[1].strip().lower()
            info["mux_no_time_reset"] = val == "true"

    if info:
        logger.debug("Parsed MUX info: %s", info)

    return info


@intelligent_path_handler
def parse_pssession_file(file_path: str) -> List[Dict[str, Any]]:
    """
    Parse a PalmSens .pssession file.

    Args:
        file_path: Path to the .pssession file

    Returns:
        List of dictionaries containing the parsed session data (multiple JSON objects)
    """
    # Try different encodings
    encodings_to_try = ['utf-16-le', 'utf-16', 'utf-8-sig', 'utf-8']
    content = None

    for encoding in encodings_to_try:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, FileNotFoundError, OSError, PermissionError):
            if encoding == encodings_to_try[-1]:
                raise  # Re-raise on last attempt for proper error handling
            continue

    if content is None:
        raise ValueError(f"Could not decode file {file_path} with any encoding")

    # Remove BOM if present
    if content.startswith('\ufeff'):
        content = content[1:]

    # The file contains multiple JSON objects concatenated
    # Use brace counting to split them
    json_objects = []
    brace_count = 0
    current_json = ""

    for char in content:
        current_json += char
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1

            # Complete JSON object found
            if brace_count == 0 and current_json.strip():
                try:
                    json_obj = json.loads(current_json.strip())
                    json_objects.append(json_obj)
                    current_json = ""
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"Failed to parse JSON object at position "
                        f"{len(json_objects) + 1}: {e}"
                    )

    if not json_objects:
        raise ValueError(f"No valid JSON objects found in {file_path}")

    return json_objects


def extract_experiments_from_session(session_data_list: List[Dict[str, Any]],
                                     curve_index: int = -1) -> Dict[str, pd.DataFrame]:
    """
    Extract individual experiments from session data.

    Args:
        session_data_list: List of parsed session data dictionaries
        curve_index: Which curve to select when multiple exist (-1 = last).

    Returns:
        Dictionary mapping experiment names to DataFrames
    """
    experiments = {}

    for obj_idx, session_data in enumerate(session_data_list):
        if not isinstance(session_data, dict):
            continue

        # Look for measurements (handle both lowercase and capitalized)
        measurements_key = 'measurements' if 'measurements' in session_data else 'Measurements'

        if measurements_key not in session_data:
            continue

        measurements = session_data[measurements_key]
        if not isinstance(measurements, list):
            continue

        for i, measurement in enumerate(measurements):
            if not isinstance(measurement, dict):
                continue

            # Get experiment name
            exp_name = measurement.get('title') or measurement.get('Title') or f'Measurement_{i+1}'

            # Make unique if multiple session objects
            if len(session_data_list) > 1:
                exp_name = f"{exp_name}_obj{obj_idx}"

            # Try EIS data first
            eis_key = (
                'eisdatalist'
                if 'eisdatalist' in measurement
                else 'EISDataList'
            )
            if (
                eis_key in measurement
                and isinstance(measurement[eis_key], list)
            ):
                eis_result = _extract_eis_dataframe(
                    measurement[eis_key]
                )
                if eis_result is not None:
                    if isinstance(eis_result, dict):
                        # Multi-entry EIS: map channels
                        # using title range like CV path
                        ch_map = _parse_channel_range(
                            exp_name
                        )
                        for ch_label, df in (
                            eis_result.items()
                        ):
                            ch_match = re.match(
                                r'Ch(\d+)', ch_label
                            )
                            if ch_match:
                                port = int(
                                    ch_match.group(1)
                                )
                            else:
                                port = ch_label
                            true_ch = ch_map.get(
                                port, port
                            )
                            experiments[
                                f"{exp_name} - Ch{true_ch}"
                            ] = df
                        continue
                    elif not eis_result.empty:
                        # Single-entry EIS: backward
                        # compatible path
                        experiments[exp_name] = eis_result
                        continue

            # Try CV/CA curves data
            curves_key = (
                'curves' if 'curves' in measurement
                else 'Curves'
            )
            if (
                curves_key in measurement
                and isinstance(measurement[curves_key], list)
            ):
                method_id = _extract_method_id(measurement)
                curves_list = measurement[curves_key]

                # Check for multiplexed channels
                ch_groups = _detect_channel_groups(curves_list)
                if ch_groups:
                    # Extract MUX metadata for debug logging
                    method_str = (
                        measurement.get('method', '')
                        or measurement.get('Method', '')
                        or measurement.get(
                            'methodformeasurement', ''
                        )
                        or measurement.get(
                            'MethodForMeasurement', ''
                        )
                    )
                    if method_str:
                        mux_info = _extract_mux_info(method_str)
                        if mux_info:
                            logger.debug(
                                "MUX info for '%s': %s",
                                exp_name, mux_info,
                            )

                    mux_dfs = _extract_mux_curves(
                        curves_list, method_id, curve_index,
                        ch_groups,
                    )
                    if mux_dfs:
                        # Map MUX port -> true electrode number
                        # from title (e.g. "channels 18-11")
                        ch_map = _parse_channel_range(exp_name)
                        for ch_label, df in mux_dfs.items():
                            # Parse port from "Ch{N}" with regex
                            ch_match = re.match(
                                r'Ch(\d+)', ch_label
                            )
                            if ch_match:
                                port = int(ch_match.group(1))
                            else:
                                logger.warning(
                                    "Unexpected channel label "
                                    "format: '%s', using as-is",
                                    ch_label,
                                )
                                port = ch_label
                            true_ch = ch_map.get(port, port)
                            experiments[
                                f"{exp_name} - Ch{true_ch}"
                            ] = df
                    else:
                        # MUX channels detected but extraction
                        # returned empty; fall through to
                        # single-scan extraction
                        logger.warning(
                            "MUX channels detected for '%s' "
                            "but extraction returned empty; "
                            "falling back to single-scan "
                            "extraction",
                            exp_name,
                        )
                        df = _extract_curves_dataframe(
                            curves_list, method_id, curve_index
                        )
                        if df is not None and not df.empty:
                            experiments[exp_name] = df
                else:
                    df = _extract_curves_dataframe(
                        curves_list, method_id, curve_index
                    )
                    if df is not None and not df.empty:
                        experiments[exp_name] = df

    return experiments


def _extract_single_eis_entry(
    entry: Dict[str, Any],
) -> Optional[pd.DataFrame]:
    """Extract a DataFrame from a single EIS data entry.

    Parses one element of the ``EISDataList`` array, extracting
    frequency, impedance, and phase data into a standardized
    DataFrame.

    Args:
        entry: A single dict from the ``EISDataList`` array,
            expected to contain a ``DataSet`` (or ``dataset``)
            key with ``Values`` arrays.

    Returns:
        DataFrame with EIS columns (Frequency_Hz, Z_real_Ohm,
        Z_imag_Ohm, Impedance_Ohm, Phase_deg), or None if
        extraction fails.
    """
    if not isinstance(entry, dict):
        return None

    dataset_key = (
        'dataset' if 'dataset' in entry else 'DataSet'
    )
    if dataset_key not in entry:
        return None

    dataset = entry[dataset_key]
    values_key = (
        'values' if 'values' in dataset else 'Values'
    )
    if values_key not in dataset:
        return None

    data_arrays: Dict[str, List] = {}

    for array_data in dataset[values_key]:
        if not isinstance(array_data, dict):
            continue

        # Get description and data values
        description = (
            array_data.get('description')
            or array_data.get('Description', '')
        ).lower()
        datavalues = (
            array_data.get('datavalues')
            or array_data.get('DataValues', [])
        )

        if not datavalues:
            continue

        # Extract numeric values
        values = []
        for val in datavalues:
            if isinstance(val, dict):
                values.append(val.get('v') or val.get('V'))
            else:
                values.append(val)

        # Map to standard column names
        if 'frequency' in description:
            data_arrays['Frequency_Hz'] = values
        elif 'zre' in description or "z'" in description:
            data_arrays['Z_real_Ohm'] = values
        elif (
            'zim' in description or "z''" in description
        ):
            data_arrays['Z_imag_Ohm'] = values
        elif description == 'z':
            data_arrays['Impedance_Ohm'] = values
        elif 'phase' in description:
            data_arrays['Phase_deg'] = values

    if not data_arrays:
        return None

    df = pd.DataFrame(data_arrays)

    # Calculate missing fields
    if (
        'Z_real_Ohm' in df.columns
        and 'Z_imag_Ohm' in df.columns
    ):
        if 'Impedance_Ohm' not in df.columns:
            df['Impedance_Ohm'] = np.sqrt(
                df['Z_real_Ohm'] ** 2
                + df['Z_imag_Ohm'] ** 2
            )
        if 'Phase_deg' not in df.columns:
            df['Phase_deg'] = np.degrees(
                np.arctan2(
                    df['Z_imag_Ohm'], df['Z_real_Ohm']
                )
            )

    return df


def _detect_eis_channel(
    entry: Dict[str, Any],
) -> Optional[int]:
    """Detect channel number from an EIS data entry.

    Checks the entry-level ``Title`` field for a ``CH\\s*(\\d+)``
    pattern (e.g. ``"CH 1: 61 freqs"``).  Falls back to scanning
    DataSet Value descriptions for ``channel(\\d+)`` if no title
    match is found.

    Args:
        entry: A single dict from the ``EISDataList`` array.

    Returns:
        Channel number (int) if found, None otherwise.
    """
    if not isinstance(entry, dict):
        return None

    # Primary: check entry Title (e.g. "CH 1: 61 freqs")
    title = (
        entry.get('Title')
        or entry.get('title')
        or ''
    )
    title_match = re.search(
        r'CH\s*(\d+)', title, re.IGNORECASE
    )
    if title_match:
        return int(title_match.group(1))

    # Fallback: check DataSet Value descriptions
    dataset_key = (
        'dataset' if 'dataset' in entry else 'DataSet'
    )
    if dataset_key not in entry:
        return None

    dataset = entry[dataset_key]
    values_key = (
        'values' if 'values' in dataset else 'Values'
    )
    if values_key not in dataset:
        return None

    channel_pattern = re.compile(
        r"channel(\d+)", re.IGNORECASE
    )

    for array_data in dataset[values_key]:
        if not isinstance(array_data, dict):
            continue
        desc = (
            array_data.get('description')
            or array_data.get('Description', '')
        )
        match = channel_pattern.search(desc)
        if match:
            return int(match.group(1))

    return None


def _extract_eis_dataframe(
    eis_data: List[Dict],
) -> Optional[Union[pd.DataFrame, Dict[str, pd.DataFrame]]]:
    """Extract DataFrame(s) from EIS data.

    Handles both single-entry and multi-entry EIS data lists.

    For single-entry lists (``len == 1``), returns a plain
    DataFrame for backward compatibility.

    For multi-entry lists (``len > 1``), returns a dict mapping
    channel labels to DataFrames. Channel labels are derived
    from ``channel(\\d+)`` patterns in DataSet descriptions
    (``"Ch{N}"``), falling back to ``"EIS_{idx+1}"`` when no
    channel label is found.

    Args:
        eis_data: The ``EISDataList`` array from a PalmSens
            measurement.

    Returns:
        - ``pd.DataFrame`` for single-entry lists (backward
          compatible)
        - ``Dict[str, pd.DataFrame]`` for multi-entry lists
          (keyed by channel label)
        - ``None`` if extraction fails
    """
    if not eis_data or not isinstance(eis_data, list):
        return None

    # Single-entry case: return plain DataFrame (backward
    # compatible)
    if len(eis_data) == 1:
        return _extract_single_eis_entry(eis_data[0])

    # Multi-entry case: extract each entry separately
    results: Dict[str, pd.DataFrame] = {}

    for idx, entry in enumerate(eis_data):
        df = _extract_single_eis_entry(entry)
        if df is None or df.empty:
            logger.debug(
                "EIS entry %d produced no data, skipping",
                idx,
            )
            continue

        # Detect channel label from DataSet descriptions
        ch_num = _detect_eis_channel(entry)
        if ch_num is not None:
            key = f"Ch{ch_num}"
        else:
            key = f"EIS_{idx + 1}"

        results[key] = df

    if not results:
        return None

    logger.debug(
        "Extracted %d EIS entries: %s",
        len(results),
        list(results.keys()),
    )
    return results


def _extract_curves_dataframe(curves_data: List[Dict], method_id: str = '',
                               curve_index: int = -1) -> pd.DataFrame:
    """Extract DataFrame from PalmSens curves data (CV, CA, etc.).

    Args:
        curves_data: List of curve dictionaries from PalmSens session
        method_id: Method identifier ('cv', 'ad', etc.)
        curve_index: Which curve to select when multiple exist.
            Supports negative indexing (-1 = last, -2 = second-to-last).
            Default is -1 (last curve, most stable repetition).
    """
    if not curves_data:
        return None

    all_data = {}
    max_length = 0

    for i, curve in enumerate(curves_data):
        if not isinstance(curve, dict):
            continue

        # Get X and Y axis data
        x_data_dict = curve.get('xaxisdataarray') or curve.get('XAxisDataArray') or {}
        y_data_dict = curve.get('yaxisdataarray') or curve.get('YAxisDataArray') or {}

        # Extract data values
        x_data = _extract_data_values(x_data_dict)
        y_data = _extract_data_values(y_data_dict)

        if len(x_data) != len(y_data) or len(x_data) == 0:
            continue

        # Track maximum length for padding
        max_length = max(max_length, len(x_data))

        # Get axis info for column naming
        x_axis_info = curve.get('xaxis', {}) or {}
        y_axis_info = curve.get('yaxis', {}) or {}

        x_label = x_axis_info.get('name', 'X') if isinstance(x_axis_info, dict) else 'X'
        y_label = y_axis_info.get('name', 'Y') if isinstance(y_axis_info, dict) else 'Y'

        # Get units
        x_unit = _extract_unit_symbol(x_data_dict)
        y_unit = _extract_unit_symbol(y_data_dict)

        # Convert current from µA to A if necessary.
        # PalmSens stores current in µA but the unit symbol may just say "A".
        # Skip for CP (METHOD_ID=pot, y-axis is potential not current) because
        # real galvanostatic potentials regularly exceed 1 V and would trip the
        # magnitude heuristic, silently corrupting them by a factor of 1e6.
        # Also skip when the explicit unit symbol identifies the axis as a
        # voltage (V / mV / uV / µV / kV) — defensive against future techniques
        # that use the generic PalmSens 'Y' axis label with a voltage payload.
        is_voltage_unit = y_unit in {'V', 'mV', 'uV', 'µV', 'kV'}
        if (
            method_id != 'pot'
            and not is_voltage_unit
            and ('current' in y_label.lower() or y_label.upper() == 'Y')
        ):
            if y_data:
                # Check unit Type field for explicit MicroAmpere declaration
                y_unit_info = y_data_dict.get('unit', y_data_dict.get('Unit', {})) if isinstance(y_data_dict, dict) else {}
                unit_type = y_unit_info.get('Type', '') if isinstance(y_unit_info, dict) else ''
                is_micro_ampere = 'MicroAmpere' in unit_type

                max_abs_current = max(abs(v) for v in y_data if v is not None)
                # Convert if: unit explicitly says MicroAmpere, OR magnitude
                # heuristic (values > 1 are unrealistic in Amps for bench
                # electrochemistry — raw values > 1 indicate µA)
                if is_micro_ampere or max_abs_current > 1.0:
                    y_data = [val * 1e-6 if val is not None else None for val in y_data]
                    y_unit = 'A'  # Ensure unit is A after conversion

        # Create column names
        x_col = f"{x_label} ({x_unit})" if x_unit else x_label
        y_col = f"{y_label} ({y_unit})" if y_unit else y_label

        # Make unique if multiple curves
        if len(curves_data) > 1:
            x_col = f"{x_col}_{i+1}"
            y_col = f"{y_col}_{i+1}"

        all_data[x_col] = x_data
        all_data[y_col] = y_data

    if not all_data:
        return None

    # Pad shorter arrays with NaN to match the longest array
    for col, data in all_data.items():
        if len(data) < max_length:
            # Extend with NaN values to match max_length
            all_data[col] = data + [float('nan')] * (max_length - len(data))

    df = pd.DataFrame(all_data)

    # If multiple curves exist, select one based on curve_index
    # (Default: last curve = most stable repetition)
    if len(df.columns) > 2:
        # Find all numbered suffixes to determine curve count
        suffix_numbers = set()
        for col in df.columns:
            if '_' in col and col.rsplit('_', 1)[-1].isdigit():
                suffix_numbers.add(int(col.rsplit('_', 1)[-1]))

        if suffix_numbers:
            n_curves = max(suffix_numbers)
            # Resolve negative index
            target = curve_index if curve_index >= 0 else n_curves + 1 + curve_index
            target = max(1, min(target, n_curves))  # Clamp to valid range

            target_cols = []
            for col in df.columns:
                if col.endswith(f'_{target}'):
                    target_cols.append(col)
                elif '_' not in col or not col.rsplit('_', 1)[-1].isdigit():
                    target_cols.append(col)

            if target_cols:
                df = df[target_cols]
        else:
            # No numbered suffixes - columns without suffix (single curve)
            pass

    # Standardize electrochemical column names
    new_names = {}
    for col in df.columns:
        # Remove any numeric suffix for matching
        col_clean = col.rsplit('_', 1)[0] if ('_' in col and col.rsplit('_', 1)[-1].isdigit()) else col
        col_lower = col_clean.lower()

        # Match column type
        if any(term in col_lower for term in ['potential', 'voltage', 'we(1).potential']):
            new_names[col] = 'Potential (V)'
        elif any(term in col_lower for term in ['current', 'we(1).current', 'i ']):
            new_names[col] = 'Current (A)'
        # Handle generic X/Y naming (common in PalmSens)
        elif col_clean.upper() == 'X':
            new_names[col] = 'Potential (V)'
        elif col_clean.upper() == 'Y':
            new_names[col] = 'Current (A)'

    if new_names:
        df = df.rename(columns=new_names)

    # Determine standard column names based on METHOD_ID
    if method_id == 'ad':  # Chronoamperometry
        x_col_standard = 'Time (s)'
        y_col_standard = 'Current (A)'
    elif method_id == 'pot':  # Chronopotentiometry (galvanostatic)
        x_col_standard = 'Time (s)'
        y_col_standard = 'Potential (V)'
    else:  # CV or unknown - default behavior
        x_col_standard = 'Potential (V)'
        y_col_standard = 'Current (A)'

    # Final validation: ensure we have exactly the right columns
    if len(df.columns) == 2:
        expected = {x_col_standard, y_col_standard}
        if set(df.columns) != expected:
            # Apply standard column names based on technique
            df.columns = [x_col_standard, y_col_standard]

    return df


def _extract_data_values(data_dict: Any) -> List[float]:
    """Extract numeric values from PalmSens data structure."""
    if isinstance(data_dict, dict):
        raw_data = data_dict.get('datavalues') or data_dict.get('DataValues') or []
        values = []
        for val in raw_data:
            if isinstance(val, dict):
                values.append(val.get('v') or val.get('V'))
            else:
                values.append(val)
        return values
    elif isinstance(data_dict, list):
        return data_dict
    return []


def _extract_unit_symbol(data_dict: Any) -> str:
    """Extract unit symbol from data dictionary."""
    if not isinstance(data_dict, dict):
        return ''
    unit_info = data_dict.get('unit') or data_dict.get('Unit')
    if isinstance(unit_info, dict):
        return (
            unit_info.get('symbol')
            or unit_info.get('Symbol')
            or unit_info.get('S')
            or unit_info.get('name')
            or ''
        )
    return ''


@intelligent_path_handler
def load_all_scans_from_psession(file_path: str, curve_index: int = -1) -> Dict[str, pd.DataFrame]:
    """
    Load all scans from a .pssession file.

    Args:
        file_path: Path to the .pssession file (Windows or WSL format)
        curve_index: Which curve to select when multiple exist (-1 = last).

    Returns:
        Dictionary mapping scan names to DataFrames

    Example:
        >>> scans = load_all_scans_from_psession('data.pssession')
        >>> for name, df in scans.items():
        ...     print(f"Scan: {name}, Shape: {df.shape}")
    """
    session_data = parse_pssession_file(file_path)
    experiments = extract_experiments_from_session(session_data, curve_index)

    if not experiments:
        raise ValueError(f"No experiments found in {file_path}")

    return experiments


def save_scans_as_csv(scans: Dict[str, pd.DataFrame], output_dir: str, base_name: str = None) -> List[Path]:
    """
    Save each scan as a separate CSV file.

    Args:
        scans: Dictionary mapping scan names to DataFrames
        output_dir: Directory to save CSV files
        base_name: Optional base name for files (defaults to 'scan')

    Returns:
        List of paths to saved CSV files

    Example:
        >>> scans = load_all_scans_from_psession('data.pssession')
        >>> paths = save_scans_as_csv(scans, 'exports/scans', 'my_data')
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    base_name = base_name or 'scan'
    saved_files = []

    for scan_name, df in scans.items():
        # Clean scan name for filename
        clean_name = scan_name.replace(' ', '_').replace('/', '_')
        csv_filename = f"{base_name}_{clean_name}.csv"
        csv_path = output_path / csv_filename

        df.to_csv(csv_path, index=False)
        saved_files.append(csv_path)
        print(f"Saved: {csv_path} ({len(df)} rows)")

    return saved_files

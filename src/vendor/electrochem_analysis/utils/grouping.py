"""
Grouping utilities for batch analysis of electrochemical data.

This module provides functions for grouping scans, validating groupings,
and calculating group statistics for CV and EIS analysis.
"""

import numpy as np
from scipy.interpolate import interp1d
from typing import Dict, List, Any, Tuple, Optional
import pandas as pd


def validate_grouping(grouping: Dict[str, List[str]],
                      scans_dict: Dict[str, pd.DataFrame]) -> Tuple[bool, List[str]]:
    """
    Validate that all scans in grouping dictionary exist in scans_dict.

    Args:
        grouping: Dictionary mapping group names to lists of scan names
        scans_dict: Dictionary of available scans (scan_name -> DataFrame)

    Returns:
        Tuple of (is_valid, missing_scans)
        - is_valid: True if all scans exist
        - missing_scans: List of scan names that don't exist
    """
    missing_scans = []

    for group_name, scan_names in grouping.items():
        for scan_name in scan_names:
            if scan_name not in scans_dict:
                missing_scans.append(f"{scan_name} (from group '{group_name}')")

    is_valid = len(missing_scans) == 0
    return is_valid, missing_scans


def calculate_mean_std(data_arrays: List[np.ndarray]) -> Tuple[np.ndarray, np.ndarray]:
    """
    Calculate mean and standard deviation for aligned data arrays.

    Args:
        data_arrays: List of 1D numpy arrays with same length

    Returns:
        Tuple of (mean_array, std_array)
    """
    # Stack arrays into 2D array (n_arrays × n_points)
    stacked = np.vstack(data_arrays)

    # Calculate mean along axis 0 (across arrays)
    mean_array = np.mean(stacked, axis=0)

    # Calculate SD along axis 0
    std_array = np.std(stacked, axis=0, ddof=1)

    return mean_array, std_array


def get_group_data(group_name: str,
                   scan_names: List[str],
                   scans_dict: Dict[str, pd.DataFrame],
                   value_column: str) -> List[np.ndarray]:
    """
    Extract data arrays for a group of scans.

    Args:
        group_name: Name of the group (for error messages)
        scan_names: List of scan names in the group
        scans_dict: Dictionary of scan DataFrames
        value_column: Name of column to extract (e.g., 'Current (A)')

    Returns:
        List of numpy arrays containing the requested data

    Raises:
        ValueError: If scans have different lengths or missing columns
    """
    data_arrays = []
    expected_length = None

    for scan_name in scan_names:
        if scan_name not in scans_dict:
            raise ValueError(f"Scan '{scan_name}' not found in scans_dict")

        df = scans_dict[scan_name]

        if value_column not in df.columns:
            raise ValueError(f"Column '{value_column}' not found in scan '{scan_name}'")

        data = df[value_column].values

        # Check that all arrays have same length
        if expected_length is None:
            expected_length = len(data)
        elif len(data) != expected_length:
            raise ValueError(
                f"Scan '{scan_name}' has {len(data)} points but expected {expected_length}. "
                f"All scans in group '{group_name}' must have the same number of data points."
            )

        data_arrays.append(data)

    return data_arrays


def calculate_group_statistics(group_name: str,
                               scan_names: List[str],
                               scans_dict: Dict[str, pd.DataFrame],
                               value_columns: List[str]) -> Dict[str, Dict[str, float]]:
    """
    Calculate mean and standard deviation for multiple value columns across a group.

    Args:
        group_name: Name of the group
        scan_names: List of scan names in the group
        scans_dict: Dictionary of scan DataFrames
        value_columns: List of column names to calculate statistics for

    Returns:
        Dictionary mapping column names to {'mean': array, 'std': array}
    """
    statistics = {}

    for column in value_columns:
        try:
            data_arrays = get_group_data(group_name, scan_names, scans_dict, column)
            mean_array, std_array = calculate_mean_std(data_arrays)
            statistics[column] = {
                'mean': mean_array,
                'std': std_array,
                'n_scans': len(data_arrays)
            }
        except Exception as e:
            print(f"Warning: Could not calculate statistics for column '{column}' "
                  f"in group '{group_name}': {e}")
            statistics[column] = None

    return statistics


def create_group_summary_df(grouping: Dict[str, List[str]],
                           group_results: Dict[str, Any]) -> pd.DataFrame:
    """
    Create a summary DataFrame from group analysis results.

    Args:
        grouping: Original grouping dictionary
        group_results: Dictionary of analysis results per group

    Returns:
        DataFrame with group-level summary statistics
    """
    summary_data = []

    for group_name, scan_names in grouping.items():
        if group_name in group_results and group_results[group_name] is not None:
            result = group_results[group_name]
            row = {
                'Group': group_name,
                'N_Scans': len(scan_names),
                'Scan_Names': ', '.join(scan_names),
                **result  # Merge in the analysis-specific results
            }
            summary_data.append(row)

    return pd.DataFrame(summary_data)


def format_error_display(mean_value: float, std_value: float, precision: int = 2) -> str:
    """
    Format mean ± SD for display.

    Args:
        mean_value: Mean value
        std_value: Standard deviation
        precision: Number of decimal places

    Returns:
        Formatted string like "12.34 ± 0.56"
    """
    return f"{mean_value:.{precision}f} ± {std_value:.{precision}f}"


def interpolate_to_common_frequency(scan_names: List[str],
                                   scans_dict: Dict[str, pd.DataFrame],
                                   n_points: int = 100,
                                   use_intersection: bool = True) -> Tuple[np.ndarray, Dict[str, Dict[str, np.ndarray]]]:
    """
    Interpolate EIS data from multiple scans to a common frequency grid.

    This is necessary when scans have different numbers of frequency points
    (e.g., 50 vs 61 points) due to different measurement settings.

    Args:
        scan_names: List of scan names to interpolate
        scans_dict: Dictionary of scan DataFrames with columns including
                   'Frequency_Hz', 'Z_real_Ohm', 'Z_imag_Ohm'
        n_points: Number of points in the common frequency grid
        use_intersection: If True, use the intersection of frequency ranges
                         (conservative, no extrapolation). If False, use union
                         (may extrapolate).

    Returns:
        Tuple of:
        - common_freq: Common frequency grid (numpy array)
        - interpolated_data: Dict mapping scan names to dicts with 'Z_real' and 'Z_imag' arrays

    Example:
        >>> common_freq, interp_data = interpolate_to_common_frequency(
        ...     ['scan1', 'scan2'], scans_dict, n_points=100
        ... )
        >>> z_real_scan1 = interp_data['scan1']['Z_real']
    """
    if not scan_names:
        raise ValueError("No scan names provided for interpolation")

    # Check if all scans already have the same frequency points
    freq_arrays = []
    for scan_name in scan_names:
        if scan_name not in scans_dict:
            raise ValueError(f"Scan '{scan_name}' not found in scans_dict")

        df = scans_dict[scan_name]
        if 'Frequency_Hz' not in df.columns:
            raise ValueError(f"'Frequency_Hz' column not found in scan '{scan_name}'")

        freq_arrays.append(df['Frequency_Hz'].values)

    # Check if all frequencies are already aligned
    all_same = True
    first_freq = freq_arrays[0]
    for freq in freq_arrays[1:]:
        if len(freq) != len(first_freq) or not np.allclose(freq, first_freq):
            all_same = False
            break

    # If all frequencies are the same, no interpolation needed
    if all_same:
        common_freq = first_freq
        interpolated_data = {}
        for scan_name in scan_names:
            df = scans_dict[scan_name]
            interpolated_data[scan_name] = {
                'Z_real': df['Z_real_Ohm'].values,
                'Z_imag': df['Z_imag_Ohm'].values
            }
        return common_freq, interpolated_data

    # Find the common frequency range
    freq_mins = [np.min(freq) for freq in freq_arrays]
    freq_maxs = [np.max(freq) for freq in freq_arrays]

    if use_intersection:
        # Conservative approach: use intersection of ranges
        common_f_min = max(freq_mins)
        common_f_max = min(freq_maxs)

        # Check if there's an overlap
        if common_f_min >= common_f_max:
            raise ValueError(f"No frequency overlap between scans. "
                           f"Ranges: {list(zip(freq_mins, freq_maxs))}")
    else:
        # Aggressive approach: use union of ranges
        common_f_min = min(freq_mins)
        common_f_max = max(freq_maxs)

    # Create common log-spaced frequency grid
    common_freq = np.logspace(np.log10(common_f_min),
                             np.log10(common_f_max),
                             n_points)

    # Interpolate each scan to the common grid
    interpolated_data = {}

    for scan_name in scan_names:
        df = scans_dict[scan_name]

        # Get frequency and impedance data
        freq = df['Frequency_Hz'].values
        z_real = df['Z_real_Ohm'].values
        z_imag = df['Z_imag_Ohm'].values

        # Sort by frequency (in case it's not already sorted)
        sort_idx = np.argsort(freq)
        freq = freq[sort_idx]
        z_real = z_real[sort_idx]
        z_imag = z_imag[sort_idx]

        # Create interpolation functions
        # Use linear interpolation on log scale for frequency
        # This is more appropriate for EIS data
        interp_real = interp1d(freq, z_real,
                               kind='linear',
                               bounds_error=False,
                               fill_value='extrapolate')
        interp_imag = interp1d(freq, z_imag,
                               kind='linear',
                               bounds_error=False,
                               fill_value='extrapolate')

        # Interpolate to common grid
        z_real_interp = interp_real(common_freq)
        z_imag_interp = interp_imag(common_freq)

        # Warn if extrapolation occurred
        if use_intersection == False:
            scan_f_min = np.min(freq)
            scan_f_max = np.max(freq)
            if common_f_min < scan_f_min or common_f_max > scan_f_max:
                print(f"Warning: Extrapolation for scan '{scan_name}' "
                      f"(scan range: {scan_f_min:.1f}-{scan_f_max:.1f} Hz, "
                      f"common range: {common_f_min:.1f}-{common_f_max:.1f} Hz)")

        interpolated_data[scan_name] = {
            'Z_real': z_real_interp,
            'Z_imag': z_imag_interp
        }

    return common_freq, interpolated_data


def check_cv_voltage_alignment(scan_names: List[str],
                               scans_dict: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """
    Check if CV scans have aligned voltage grids.

    Args:
        scan_names: List of scan names to check
        scans_dict: Dictionary of DataFrames with CV data

    Returns:
        Dictionary with alignment information:
        - aligned: Boolean indicating if all voltage grids match
        - n_points: List of number of points per scan
        - needs_interpolation: Boolean indicating if interpolation is needed
    """
    n_points = []
    voltage_ranges = []

    for scan_name in scan_names:
        df = scans_dict[scan_name]
        n_points.append(len(df))
        voltage_ranges.append((df['Potential (V)'].min(), df['Potential (V)'].max()))

    # Check if all scans have the same number of points
    aligned = len(set(n_points)) == 1

    return {
        'aligned': aligned,
        'n_points': n_points,
        'voltage_ranges': voltage_ranges,
        'needs_interpolation': not aligned
    }


def interpolate_cv_to_common_index(scan_names: List[str],
                                   scans_dict: Dict[str, pd.DataFrame],
                                   n_points: int = 500) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    """
    Interpolate CV scans to a common index grid, preserving hysteresis.

    Uses parametric interpolation where t ∈ [0, 1] represents progress
    through the CV cycle (forward + reverse sweep). This preserves the
    full CV loop structure unlike voltage-based interpolation which
    averages forward and reverse sweeps.

    **Assumption:** All scans have similar voltage windows (same start/end
    potentials). The returned common_voltage is taken from the first scan,
    and other scans' currents are aligned by normalized index position.
    Scans with different voltage ranges will produce misaligned results
    when plotted against common_voltage.

    For consistent experimental setups where all CVs use the same potential
    window, this approach works well. For mixed voltage ranges, consider
    filtering scans to consistent windows before grouping.

    Args:
        scan_names: List of scan names to interpolate
        scans_dict: Dictionary of DataFrames with CV data
        n_points: Number of points in common index grid

    Returns:
        Tuple of:
        - common_voltage: Common voltage array (from first scan after interpolation)
        - interpolated_data: Dictionary of scan_name -> interpolated current array
    """
    interpolated_data = {}
    common_voltage = None

    for scan_name in scan_names:
        df = scans_dict[scan_name]
        voltage = df['Potential (V)'].values
        current = df['Current (A)'].values

        # Create normalized parameter t based on index
        n_orig = len(voltage)
        t_orig = np.linspace(0, 1, n_orig)
        t_common = np.linspace(0, 1, n_points)

        # Interpolate voltage and current independently against t
        interp_v = interp1d(t_orig, voltage, kind='linear')
        interp_i = interp1d(t_orig, current, kind='linear')

        interpolated_voltage = interp_v(t_common)
        interpolated_current = interp_i(t_common)

        interpolated_data[scan_name] = interpolated_current

        # Use first scan's voltage as common reference
        # All scans should have similar voltage profiles after parametric interpolation
        if common_voltage is None:
            common_voltage = interpolated_voltage

    return common_voltage, interpolated_data


def check_frequency_alignment(scan_names: List[str],
                              scans_dict: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """
    Check if EIS scans have aligned frequency points.

    Args:
        scan_names: List of scan names to check
        scans_dict: Dictionary of scan DataFrames

    Returns:
        Dictionary with alignment information:
        - 'aligned': bool, True if all scans have same frequency points
        - 'n_points': List of number of points per scan
        - 'freq_ranges': List of (min, max) frequency ranges per scan
        - 'needs_interpolation': bool
    """
    if not scan_names:
        return {'aligned': True, 'n_points': [], 'freq_ranges': [],
                'needs_interpolation': False}

    n_points = []
    freq_ranges = []
    freq_arrays = []

    for scan_name in scan_names:
        if scan_name not in scans_dict:
            continue

        df = scans_dict[scan_name]
        if 'Frequency_Hz' not in df.columns:
            continue

        freq = df['Frequency_Hz'].values
        freq_arrays.append(freq)
        n_points.append(len(freq))
        freq_ranges.append((np.min(freq), np.max(freq)))

    # Check if all aligned
    aligned = True
    if freq_arrays:
        first_freq = freq_arrays[0]
        for freq in freq_arrays[1:]:
            if len(freq) != len(first_freq) or not np.allclose(freq, first_freq):
                aligned = False
                break

    return {
        'aligned': aligned,
        'n_points': n_points,
        'freq_ranges': freq_ranges,
        'needs_interpolation': not aligned
    }
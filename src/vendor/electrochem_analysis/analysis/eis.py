"""
EIS Analyzer - Electrochemical Impedance Spectroscopy

Lightweight analyzer for EIS data with Rs/Rct extraction and basic plotting.
Plots are designed for SVG export with minimal annotations for post-processing.
Includes grouped analysis for averaging multiple scans with error visualization.

Adapted from Device Characterization (CMU.49.005) electrochemical_impedance_spectroscopy.py.
"""

import logging
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from typing import Tuple, Optional, Dict, Any, List
from pathlib import Path
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.vendor.electrochem_analysis.utils.grouping import (
    validate_grouping,
    calculate_mean_std,
    get_group_data,
    create_group_summary_df,
    format_error_display,
    interpolate_to_common_frequency,
    check_frequency_alignment
)

try:
    from impedance.models.circuits import CustomCircuit
    _IMPEDANCE_AVAILABLE = True
except ImportError:  # pragma: no cover - validated by Task 1 install
    CustomCircuit = None
    _IMPEDANCE_AVAILABLE = False

logger = logging.getLogger(__name__)

# Physical sanity guards for Rs / Rct extraction.
# Rs (solution resistance) must be non-negative by physics; a negative
# intercept indicates an unreliable extrapolation on a flat / tilted /
# noisy Nyquist arc.  Rct above ~1 GOhm is only meaningful for completely
# dry / open circuits, never aqueous electrochemistry, and almost always
# signals a runaway peak-finding fit.
RCT_MAX_PHYSICAL_OHM = 1e9


class EISAnalyzer:
    """
    Lightweight EIS analyzer for impedance spectroscopy data.

    Features:
    - Rs (solution resistance) extraction from high-frequency intercept
    - Rct (charge transfer resistance) from semicircle peak
    - Nyquist plot (-Z_imag vs Z_real)
    - Bode plots (magnitude and phase vs frequency)
    - Minimal annotations for SVG post-processing
    """

    def __init__(self, data: pd.DataFrame):
        """
        Initialize EIS analyzer with standardized data.

        Args:
            data: DataFrame with columns Frequency_Hz, Z_real_Ohm, Z_imag_Ohm

        Raises:
            ValueError: If required columns are missing
        """
        if data.empty:
            raise ValueError("Data DataFrame is empty")

        required_cols = ['Frequency_Hz', 'Z_real_Ohm', 'Z_imag_Ohm']
        missing = [col for col in required_cols if col not in data.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        self.data = data.copy()
        self.rs = None
        self.rct = None
        self.peak_frequency = None
        # Optional context label used in physical-sanity warnings.  Callers
        # (e.g. batch_analyze, batch_analyze_grouped) may set this after
        # construction so log messages identify the offending scan.
        self.scan_name: Optional[str] = None

    def calculate_impedance_parameters(self) -> Tuple[float, float]:
        """
        Calculate Rs and Rct from impedance data.

        Rs: Solution resistance (high-frequency intercept on real axis)
        Rct: Charge transfer resistance (semicircle diameter)

        Returns:
            Tuple of (Rs, Rct) in Ohms

        Algorithm:
            1. Sort data by descending frequency
            2. Rs = Z_real at highest frequency
            3. Find peak in |Z_imag| (maximum imaginary impedance)
            4. Rct = Z_real(peak) - Rs
        """
        # Sort by frequency (descending - high to low)
        df_sorted = self.data.sort_values('Frequency_Hz', ascending=False).reset_index(drop=True)

        # Rs is the high-frequency intercept (first point)
        rs_estimate = float(df_sorted.iloc[0]['Z_real_Ohm'])

        # Find peak in imaginary impedance (maximum |Z_imag|)
        peak_idx = df_sorted['Z_imag_Ohm'].abs().idxmax()
        z_real_peak = float(df_sorted.loc[peak_idx, 'Z_real_Ohm'])
        self.peak_frequency = float(df_sorted.loc[peak_idx, 'Frequency_Hz'])

        # Rct is the difference between peak Z_real and Rs (computed from the
        # raw rs_estimate so a NaN-guarded Rs does not poison the Rct estimate
        # before we have a chance to evaluate Rct on its own merits).
        rct_estimate = z_real_peak - rs_estimate

        # Physical-sanity guard: Rs must be non-negative.  A negative
        # high-frequency intercept means the extrapolation landed below the
        # real axis (flat / tilted / noisy Nyquist) and the fit is
        # unreliable - return NaN so downstream nanmean / isfinite count
        # guards can drop the scan without raising.
        scan_label = self.scan_name if self.scan_name else "<unnamed scan>"
        if not np.isfinite(rs_estimate) or rs_estimate < 0:
            logger.warning(
                "Rs guard: rejecting non-physical Rs estimate %.4g Ohm for %s "
                "(high-frequency intercept landed below real axis); returning NaN",
                rs_estimate, scan_label,
            )
            self.rs = float('nan')
            # If Rs is unreliable, the Rs-anchored Rct estimate is too.
            self.rct = float('nan')
            return self.rs, self.rct

        # Physical-sanity guard: Rct above ~1 GOhm is only meaningful for
        # completely dry / open circuits, never aqueous electrochemistry -
        # this catches runaway peak-finding fits (Ch1/Ch2/Ch3 mux_eis_cv
        # produced 1e8-1e9 Ohm Rct on the same scans that produced negative
        # Rs).  Rs survives; only the Rct estimate is invalidated.
        if not np.isfinite(rct_estimate) or rct_estimate > RCT_MAX_PHYSICAL_OHM:
            logger.warning(
                "Rct guard: rejecting non-physical Rct estimate %.4g Ohm for %s "
                "(exceeds %.0g Ohm physical ceiling for aqueous EC); returning NaN",
                rct_estimate, scan_label, RCT_MAX_PHYSICAL_OHM,
            )
            self.rs = rs_estimate
            self.rct = float('nan')
            return self.rs, self.rct

        self.rs = rs_estimate
        self.rct = rct_estimate

        return self.rs, self.rct

    def calculate_impedance_at_frequency(self, target_freq: float = 1000.0) -> float:
        """
        Calculate impedance magnitude at a specific frequency.

        Args:
            target_freq: Target frequency in Hz (default: 1000 Hz = 1 kHz)

        Returns:
            Impedance magnitude |Z| in Ohms at the closest frequency to target

        Note:
            |Z| = sqrt(Z_real^2 + Z_imag^2)
        """
        # Find the closest frequency point to target
        freq_diff = np.abs(self.data['Frequency_Hz'] - target_freq)
        closest_idx = freq_diff.idxmin()

        # Get real and imaginary components at that frequency
        z_real = self.data.loc[closest_idx, 'Z_real_Ohm']
        z_imag = self.data.loc[closest_idx, 'Z_imag_Ohm']

        # Calculate impedance magnitude
        z_magnitude = np.sqrt(z_real**2 + z_imag**2)

        return float(z_magnitude)

    def plot_nyquist(self, figsize: Tuple[int, int] = (6, 6)) -> plt.Figure:
        """
        Generate Nyquist plot (-Z_imag vs Z_real).

        Args:
            figsize: Figure size in inches

        Returns:
            matplotlib Figure object

        Plot features:
        - Scatter points with line connecting them
        - Equal aspect ratio
        - Grid for readability
        - Minimal text annotations (user adds in vector editor)
        """
        fig, ax = plt.subplots(figsize=figsize)

        # Plot data
        ax.plot(
            self.data['Z_real_Ohm'],
            -self.data['Z_imag_Ohm'],
            'o-',
            markersize=4,
            linewidth=1,
            label='EIS Data'
        )

        # Labels and formatting
        ax.set_xlabel("Z' (Ω)", fontsize=11)
        ax.set_ylabel("-Z'' (Ω)", fontsize=11)
        ax.set_title('Nyquist Plot', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_aspect('equal', adjustable='datalim')

        # Add legend
        ax.legend(loc='best', fontsize=9)

        plt.tight_layout()
        return fig

    def plot_bode(self, figsize: Tuple[int, int] = (8, 6)) -> plt.Figure:
        """
        Generate Bode plots (magnitude and phase vs frequency).

        Args:
            figsize: Figure size in inches

        Returns:
            matplotlib Figure object with 2 subplots

        Plot features:
        - Top: |Z| vs frequency (log scale)
        - Bottom: Phase angle vs frequency (log scale)
        - Minimal annotations for SVG editing
        """
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=figsize, sharex=True)

        # Calculate magnitude and phase
        z_magnitude = np.sqrt(self.data['Z_real_Ohm']**2 + self.data['Z_imag_Ohm']**2)
        z_phase = np.degrees(np.arctan2(self.data['Z_imag_Ohm'], self.data['Z_real_Ohm']))

        # Plot magnitude
        ax1.loglog(self.data['Frequency_Hz'], z_magnitude, 'o-', markersize=4, linewidth=1)
        ax1.set_ylabel('|Z| (Ω)', fontsize=11)
        ax1.set_title('Bode Plot', fontsize=12)
        ax1.grid(True, alpha=0.3, which='both', linestyle='--')

        # Plot phase
        ax2.semilogx(self.data['Frequency_Hz'], z_phase, 'o-', markersize=4, linewidth=1, color='C1')
        ax2.set_xlabel('Frequency (Hz)', fontsize=11)
        ax2.set_ylabel('Phase (°)', fontsize=11)
        ax2.grid(True, alpha=0.3, which='both', linestyle='--')

        plt.tight_layout()
        return fig

    def get_results_dataframe(self) -> pd.DataFrame:
        """
        Export analysis results as a DataFrame.

        Returns:
            DataFrame with analysis parameters
        """
        if self.rs is None or self.rct is None:
            # Calculate if not already done
            self.calculate_impedance_parameters()

        # Calculate impedance at 1kHz
        z_1khz = self.calculate_impedance_at_frequency(1000.0)

        results = {
            'Parameter': ['Rs', 'Rct', '|Z| @ 1kHz', 'Peak Frequency', 'Time Constant'],
            'Value': [
                self.rs,
                self.rct,
                z_1khz,
                self.peak_frequency,
                1 / (2 * np.pi * self.peak_frequency) if self.peak_frequency > 0 else np.nan
            ],
            'Unit': ['Ω', 'Ω', 'Ω', 'Hz', 's']
        }

        return pd.DataFrame(results)

    def get_summary(self) -> Dict[str, Any]:
        """
        Get summary dictionary of analysis results.

        Returns:
            Dictionary with analysis parameters
        """
        if self.rs is None or self.rct is None:
            self.calculate_impedance_parameters()

        # Calculate impedance at 1kHz
        impedance_1khz = self.calculate_impedance_at_frequency(1000.0)

        return {
            'technique': 'EIS',
            'rs_ohm': float(self.rs),
            'rct_ohm': float(self.rct),
            'impedance_1khz_ohm': impedance_1khz,
            'peak_frequency_hz': float(self.peak_frequency),
            'time_constant_s': float(1 / (2 * np.pi * self.peak_frequency)) if self.peak_frequency > 0 else None,
            'n_data_points': len(self.data),
            'frequency_range_hz': (float(self.data['Frequency_Hz'].min()), float(self.data['Frequency_Hz'].max()))
        }

    @staticmethod
    def batch_analyze(scans: Dict[str, pd.DataFrame], export_manager=None) -> Tuple[pd.DataFrame, Dict[str, plt.Figure]]:
        """
        Perform batch EIS analysis on multiple scans.

        Args:
            scans: Dictionary mapping scan names to DataFrames
            export_manager: Optional ExportManager for automatic saving

        Returns:
            Tuple of:
            - Summary DataFrame with all scan results
            - Dictionary of scan_name -> {nyquist_fig, bode_fig}

        Example:
            >>> from src.dataloaders.psession_parser import load_all_scans_from_psession
            >>> scans = load_all_scans_from_psession('data.pssession')
            >>> summary_df, figures = EISAnalyzer.batch_analyze(scans)
            >>> print(summary_df)
        """
        results_list = []
        figures = {}

        for scan_name, data in scans.items():
            try:
                # Create analyzer
                analyzer = EISAnalyzer(data)
                analyzer.scan_name = scan_name

                # Calculate parameters
                analyzer.calculate_impedance_parameters()

                # Get results
                summary = analyzer.get_summary()
                summary['scan_name'] = scan_name
                results_list.append(summary)

                # Generate plots
                nyquist_fig = analyzer.plot_nyquist()
                bode_fig = analyzer.plot_bode()

                figures[scan_name] = {
                    'nyquist': nyquist_fig,
                    'bode': bode_fig
                }

                # Save if export manager provided
                if export_manager:
                    export_manager.save_figure(
                        nyquist_fig,
                        f"{scan_name}_nyquist",
                        subdir="plots"
                    )
                    export_manager.save_figure(
                        bode_fig,
                        f"{scan_name}_bode",
                        subdir="plots"
                    )

                print(f"✓ Analyzed {scan_name}: Rs={summary['rs_ohm']:.1f}Ω, Rct={summary['rct_ohm']:.1f}Ω")

            except Exception as e:
                print(f"✗ Failed to analyze {scan_name}: {e}")
                continue

        # Create summary DataFrame
        if results_list:
            summary_df = pd.DataFrame(results_list)

            # Reorder columns for better readability
            cols = ['scan_name', 'rs_ohm', 'rct_ohm', 'impedance_1khz_ohm', 'peak_frequency_hz', 'time_constant_s',
                    'n_data_points', 'frequency_range_hz']
            summary_df = summary_df[[c for c in cols if c in summary_df.columns]]

            if export_manager:
                export_manager.save_dataframe(summary_df, 'batch_eis_results', subdir='data')

            return summary_df, figures
        else:
            return pd.DataFrame(), {}

    @staticmethod
    def plot_nyquist_grouped(scans: Dict[str, pd.DataFrame],
                            grouping: Dict[str, List[str]],
                            error_style: str = 'bands',
                            figsize: Tuple[int, int] = (8, 6),
                            alpha_bands: float = 0.3,
                            export_manager=None) -> plt.Figure:
        """
        Plot grouped Nyquist data with mean and standard deviation visualization.

        Args:
            scans: Dictionary of {scan_name: DataFrame} pairs
            grouping: Dictionary of {group_name: [scan_names]} for grouping
            error_style: 'bands' for shaded error regions or 'bars' for error bars
            figsize: Figure size in inches
            alpha_bands: Alpha transparency for error bands (if error_style='bands')
            export_manager: Optional ExportManager for saving results

        Returns:
            matplotlib Figure object with grouped Nyquist plots
        """
        # Validate grouping
        is_valid, missing = validate_grouping(grouping, scans)
        if not is_valid:
            raise ValueError(f"Invalid grouping - missing scans: {missing}")

        fig, ax = plt.subplots(figsize=figsize)

        # Color palette for different groups
        colors = plt.cm.tab10(np.linspace(0, 0.7, len(grouping)))

        for idx, (group_name, scan_names) in enumerate(grouping.items()):
            try:
                # Check if interpolation is needed
                alignment_info = check_frequency_alignment(scan_names, scans)

                if alignment_info['needs_interpolation']:
                    # Interpolate to common frequency grid
                    print(f"  Interpolating {len(scan_names)} scans to common frequency grid "
                          f"(points: {alignment_info['n_points']})")
                    common_freq, interpolated_data = interpolate_to_common_frequency(
                        scan_names, scans, n_points=100, use_intersection=True
                    )

                    # Extract interpolated arrays
                    z_real_arrays = [interpolated_data[name]['Z_real'] for name in scan_names]
                    z_imag_arrays = [interpolated_data[name]['Z_imag'] for name in scan_names]
                else:
                    # Use original data if already aligned
                    z_real_arrays = get_group_data(
                        group_name, scan_names, scans, 'Z_real_Ohm'
                    )
                    z_imag_arrays = get_group_data(
                        group_name, scan_names, scans, 'Z_imag_Ohm'
                    )

                # Calculate mean and SD
                mean_z_real, std_z_real = calculate_mean_std(z_real_arrays)
                mean_z_imag, std_z_imag = calculate_mean_std(z_imag_arrays)

                # Plot mean line
                ax.plot(mean_z_real, -mean_z_imag,
                       'o-', color=colors[idx], linewidth=2, markersize=4,
                       label=f"{group_name} (n={len(scan_names)})")

                # Add error visualization
                if error_style == 'bands':
                    # Create error polygon for Nyquist plot
                    # Upper and lower bounds for both real and imaginary
                    ax.fill_between(mean_z_real,
                                  -mean_z_imag - std_z_imag,
                                  -mean_z_imag + std_z_imag,
                                  alpha=alpha_bands, color=colors[idx])
                elif error_style == 'bars':
                    # Sample error bars to avoid clutter
                    n_error_bars = 15
                    step = max(1, len(mean_z_real) // n_error_bars)
                    indices = range(0, len(mean_z_real), step)

                    ax.errorbar(mean_z_real[indices], -mean_z_imag[indices],
                              xerr=std_z_real[indices], yerr=std_z_imag[indices],
                              fmt='none', color=colors[idx], alpha=0.5,
                              capsize=3, capthick=1)

                print(f"✓ Plotted Nyquist for group '{group_name}' with {len(scan_names)} scans")

            except Exception as e:
                print(f"✗ Failed to plot Nyquist for group '{group_name}': {e}")

        # Labels and formatting
        ax.set_xlabel("Z' (Ω)", fontsize=11)
        ax.set_ylabel("-Z'' (Ω)", fontsize=11)
        ax.set_title('Grouped Nyquist Plot', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_aspect('equal', adjustable='datalim')
        ax.legend(loc='best', fontsize=9)

        plt.tight_layout()

        # Save if export manager provided
        if export_manager:
            export_manager.save_figure(fig, 'nyquist_grouped', subdir='plots')

        return fig

    @staticmethod
    def plot_bode_grouped(scans: Dict[str, pd.DataFrame],
                         grouping: Dict[str, List[str]],
                         error_style: str = 'bands',
                         figsize: Tuple[int, int] = (8, 8),
                         alpha_bands: float = 0.3,
                         export_manager=None) -> plt.Figure:
        """
        Plot grouped Bode data with mean and standard deviation visualization.

        Args:
            scans: Dictionary of {scan_name: DataFrame} pairs
            grouping: Dictionary of {group_name: [scan_names]} for grouping
            error_style: 'bands' for shaded error regions or 'bars' for error bars
            figsize: Figure size in inches
            alpha_bands: Alpha transparency for error bands (if error_style='bands')
            export_manager: Optional ExportManager for saving results

        Returns:
            matplotlib Figure object with grouped Bode plots (magnitude and phase)
        """
        # Validate grouping
        is_valid, missing = validate_grouping(grouping, scans)
        if not is_valid:
            raise ValueError(f"Invalid grouping - missing scans: {missing}")

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=figsize, sharex=True)

        # Color palette for different groups
        colors = plt.cm.tab10(np.linspace(0, 0.7, len(grouping)))

        for idx, (group_name, scan_names) in enumerate(grouping.items()):
            try:
                # Check if interpolation is needed
                alignment_info = check_frequency_alignment(scan_names, scans)

                if alignment_info['needs_interpolation']:
                    # Interpolate to common frequency grid
                    print(f"  Interpolating {len(scan_names)} scans for Bode plot "
                          f"(points: {alignment_info['n_points']})")
                    common_freq, interpolated_data = interpolate_to_common_frequency(
                        scan_names, scans, n_points=100, use_intersection=True
                    )

                    # Extract interpolated arrays
                    z_real_arrays = [interpolated_data[name]['Z_real'] for name in scan_names]
                    z_imag_arrays = [interpolated_data[name]['Z_imag'] for name in scan_names]
                    frequency = common_freq
                else:
                    # Use original data if already aligned
                    z_real_arrays = get_group_data(
                        group_name, scan_names, scans, 'Z_real_Ohm'
                    )
                    z_imag_arrays = get_group_data(
                        group_name, scan_names, scans, 'Z_imag_Ohm'
                    )
                    # Use frequency from first scan (all should be identical in group)
                    frequency = scans[scan_names[0]]['Frequency_Hz'].values

                # Calculate magnitude and phase for each scan
                magnitude_arrays = []
                phase_arrays = []

                for z_real, z_imag in zip(z_real_arrays, z_imag_arrays):
                    magnitude = np.sqrt(z_real**2 + z_imag**2)
                    phase = np.degrees(np.arctan2(z_imag, z_real))
                    magnitude_arrays.append(magnitude)
                    phase_arrays.append(phase)

                # Magnitude: compute SD in log-space (geometric SD)
                # so that error bands are symmetric on the loglog axis
                log_mag_arrays = [np.log10(m) for m in magnitude_arrays]
                log_mean_mag, log_std_mag = calculate_mean_std(log_mag_arrays)
                mean_magnitude = 10**log_mean_mag  # geometric mean
                mag_upper = 10**(log_mean_mag + log_std_mag)
                mag_lower = 10**(log_mean_mag - log_std_mag)

                # Phase: linear y-axis, use regular SD
                mean_phase, std_phase = calculate_mean_std(phase_arrays)

                # Plot magnitude
                ax1.loglog(frequency, mean_magnitude,
                          'o-', color=colors[idx], linewidth=2, markersize=4,
                          label=f"{group_name} (n={len(scan_names)})")

                if error_style == 'bands':
                    ax1.fill_between(frequency, mag_lower, mag_upper,
                                    alpha=alpha_bands, color=colors[idx])
                elif error_style == 'bars':
                    # Sample error bars (asymmetric in linear space)
                    n_error_bars = 15
                    step = max(1, len(frequency) // n_error_bars)
                    indices = list(range(0, len(frequency), step))
                    yerr_lower = mean_magnitude[indices] - mag_lower[indices]
                    yerr_upper = mag_upper[indices] - mean_magnitude[indices]
                    ax1.errorbar(frequency[indices], mean_magnitude[indices],
                               yerr=[yerr_lower, yerr_upper],
                               fmt='none', color=colors[idx], alpha=0.5,
                               capsize=3, capthick=1)

                # Plot phase
                ax2.semilogx(frequency, mean_phase,
                           'o-', color=colors[idx], linewidth=2, markersize=4)

                if error_style == 'bands':
                    ax2.fill_between(frequency,
                                    mean_phase - std_phase,
                                    mean_phase + std_phase,
                                    alpha=alpha_bands, color=colors[idx])
                elif error_style == 'bars':
                    indices = range(0, len(frequency), step)
                    ax2.errorbar(frequency[indices], mean_phase[indices],
                               yerr=std_phase[indices],
                               fmt='none', color=colors[idx], alpha=0.5,
                               capsize=3, capthick=1)

                print(f"✓ Plotted Bode for group '{group_name}' with {len(scan_names)} scans")

            except Exception as e:
                print(f"✗ Failed to plot Bode for group '{group_name}': {e}")

        # Format magnitude subplot
        ax1.set_ylabel('|Z| (Ω)', fontsize=11)
        ax1.set_title('Grouped Bode Plot', fontsize=12)
        ax1.grid(True, alpha=0.3, which='both', linestyle='--')
        ax1.legend(loc='best', fontsize=9)

        # Format phase subplot
        ax2.set_xlabel('Frequency (Hz)', fontsize=11)
        ax2.set_ylabel('Phase (°)', fontsize=11)
        ax2.grid(True, alpha=0.3, which='both', linestyle='--')

        plt.tight_layout()

        # Save if export manager provided
        if export_manager:
            export_manager.save_figure(fig, 'bode_grouped', subdir='plots')

        return fig

    @staticmethod
    def batch_analyze_grouped(scans: Dict[str, pd.DataFrame],
                            grouping: Dict[str, List[str]],
                            export_manager=None,
                            plot_individual: bool = False,
                            error_style: str = 'bands') -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """
        Batch analyze EIS scans with grouping and calculate group statistics.

        Args:
            scans: Dictionary of {scan_name: DataFrame} pairs
            grouping: Dictionary of {group_name: [scan_names]} for grouping
            export_manager: Optional ExportManager for saving results
            plot_individual: If True, also generate individual plots for each scan
            error_style: 'bands' or 'bars' for error visualization in grouped plots

        Returns:
            Tuple of (summary_dataframe, figures_dict)
            - summary_dataframe: Group-level statistics (mean Rs, Rct, SD, etc.)
            - figures_dict: Dictionary containing grouped plots and optionally individual plots
        """
        # Validate grouping
        is_valid, missing = validate_grouping(grouping, scans)
        if not is_valid:
            raise ValueError(f"Invalid grouping - missing scans: {missing}")

        group_results = []
        figures = {}

        # Process each group
        for group_name, scan_names in grouping.items():
            print(f"\nAnalyzing group: {group_name}")

            rs_values = []
            rct_values = []
            impedance_1khz_values = []
            peak_freq_values = []
            time_const_values = []
            individual_figures = {}

            # Analyze each scan in the group
            for scan_name in scan_names:
                try:
                    # Create analyzer
                    analyzer = EISAnalyzer(scans[scan_name])
                    analyzer.scan_name = scan_name

                    # Calculate parameters
                    rs, rct = analyzer.calculate_impedance_parameters()
                    rs_values.append(rs)
                    rct_values.append(rct)
                    peak_freq_values.append(analyzer.peak_frequency)

                    # Calculate impedance at 1kHz
                    z_1khz = analyzer.calculate_impedance_at_frequency(1000.0)
                    impedance_1khz_values.append(z_1khz)

                    # Calculate time constant
                    time_const = 1 / (2 * np.pi * analyzer.peak_frequency) if analyzer.peak_frequency > 0 else None
                    if time_const:
                        time_const_values.append(time_const)

                    print(f"  ✓ {scan_name}: Rs={rs:.1f}Ω, Rct={rct:.1f}Ω, |Z|@1kHz={z_1khz:.1f}Ω")

                    # Generate individual plots if requested
                    if plot_individual:
                        nyquist_fig = analyzer.plot_nyquist()
                        bode_fig = analyzer.plot_bode()
                        individual_figures[scan_name] = {
                            'nyquist': nyquist_fig,
                            'bode': bode_fig
                        }

                        if export_manager:
                            clean_name = scan_name.replace('/', '_').replace('\\', '_')
                            export_manager.save_figure(
                                nyquist_fig, f"{clean_name}_nyquist",
                                subdir=f'plots/{group_name}'
                            )
                            export_manager.save_figure(
                                bode_fig, f"{clean_name}_bode",
                                subdir=f'plots/{group_name}'
                            )

                except Exception as e:
                    print(f"  ✗ Failed to analyze {scan_name}: {e}")

            # Calculate group statistics
            group_summary = {
                'group_name': group_name,
                'n_scans': len(scan_names),
                'scan_names': ', '.join(scan_names)
            }

            if rs_values:
                n_valid = np.sum(np.isfinite(rs_values))
                group_summary['rs_mean_ohm'] = np.nanmean(rs_values)
                group_summary['rs_std_ohm'] = np.nanstd(rs_values, ddof=1) if n_valid > 1 else 0
                group_summary['rs_formatted'] = format_error_display(
                    group_summary['rs_mean_ohm'],
                    group_summary['rs_std_ohm'], precision=1
                )

            if rct_values:
                n_valid = np.sum(np.isfinite(rct_values))
                group_summary['rct_mean_ohm'] = np.nanmean(rct_values)
                group_summary['rct_std_ohm'] = np.nanstd(rct_values, ddof=1) if n_valid > 1 else 0
                group_summary['rct_formatted'] = format_error_display(
                    group_summary['rct_mean_ohm'],
                    group_summary['rct_std_ohm'], precision=1
                )

            if peak_freq_values:
                n_valid = np.sum(np.isfinite(peak_freq_values))
                group_summary['peak_frequency_mean_hz'] = np.nanmean(peak_freq_values)
                group_summary['peak_frequency_std_hz'] = np.nanstd(peak_freq_values, ddof=1) if n_valid > 1 else 0

            if time_const_values:
                n_valid = np.sum(np.isfinite(time_const_values))
                group_summary['time_constant_mean_s'] = np.nanmean(time_const_values)
                group_summary['time_constant_std_s'] = np.nanstd(time_const_values, ddof=1) if n_valid > 1 else 0

            if impedance_1khz_values:
                n_valid = np.sum(np.isfinite(impedance_1khz_values))
                group_summary['impedance_1khz_mean_ohm'] = np.nanmean(impedance_1khz_values)
                group_summary['impedance_1khz_std_ohm'] = np.nanstd(impedance_1khz_values, ddof=1) if n_valid > 1 else 0
                group_summary['impedance_1khz_formatted'] = format_error_display(
                    group_summary['impedance_1khz_mean_ohm'],
                    group_summary['impedance_1khz_std_ohm'], precision=1
                )

            group_results.append(group_summary)

            if plot_individual:
                figures[f'{group_name}_individual'] = individual_figures

        # Create group summary dataframe
        summary_df = pd.DataFrame(group_results)

        # Generate grouped plots
        nyquist_grouped_fig = EISAnalyzer.plot_nyquist_grouped(
            scans, grouping, error_style=error_style, export_manager=export_manager
        )
        bode_grouped_fig = EISAnalyzer.plot_bode_grouped(
            scans, grouping, error_style=error_style, export_manager=export_manager
        )

        figures['nyquist_grouped'] = nyquist_grouped_fig
        figures['bode_grouped'] = bode_grouped_fig

        # Save summary if export manager provided
        if export_manager:
            export_manager.save_dataframe(summary_df, 'grouped_eis_results', subdir='data')

        print(f"\n{'='*50}")
        print("Group Analysis Summary:")
        for _, row in summary_df.iterrows():
            if 'rs_formatted' in row and 'rct_formatted' in row:
                print(f"  {row['group_name']}: Rs = {row['rs_formatted']}Ω, Rct = {row['rct_formatted']}Ω")

        return summary_df, figures


class EISCircuitFitter:
    """Equivalent-circuit fitter for EIS data using impedance.py.

    Paper-grade R_ct and CPE-alpha extraction with covariance-derived 1-sigma
    uncertainties, alongside the faster peak-finder ``EISAnalyzer``. The fitter
    builds an ``impedance.models.circuits.CustomCircuit`` for one of three
    pre-defined topologies, fits it against the complex impedance spectrum, and
    exposes the fitted parameters with goodness-of-fit reporting.

    Topologies (class attributes / string constants):
        CPE_RANDLES: ``"R0-p(R1,CPE1)"`` - R_s in series with R_ct in parallel
            with a constant-phase element. The default; appropriate for
            blocking / quasi-blocking interfaces with depressed semicircles.
        RANDLES_WARBURG: ``"R0-p(R1,CPE1)-W"`` - Randles + semi-infinite
            Warburg diffusion. Adds one parameter (W) for mass-transport
            limited regimes.
        TLM_POROUS: ``"R0-TLMQ0"`` - Simplified transmission-line porous
            electrode model (Landesfeind et al. 2016, doi:10.1149/2.1141607jes)
            with R_s in series. TLMQ0 has three parameters: pore ionic
            resistance, distributed-capacitance Y0, and CPE-like exponent.
            Selected over the 4-param Paasch ``T`` element because the latter
            requires fitting two solid-phase resistivities that are not
            separately identifiable from a single-port spectrum.

    Attributes:
        data: Validated copy of the input impedance DataFrame.
        topology: Circuit string passed to impedance.py CustomCircuit.
        fit_result: Underlying CustomCircuit instance after ``fit()`` runs,
            otherwise None.
        scan_name: Optional label used in log messages (mirrors EISAnalyzer).
    """

    CPE_RANDLES = "R0-p(R1,CPE1)"
    RANDLES_WARBURG = "R0-p(R1,CPE1)-W"
    TLM_POROUS = "R0-TLMQ0"

    _VALID_TOPOLOGIES = (CPE_RANDLES, RANDLES_WARBURG, TLM_POROUS)

    def __init__(
        self,
        data: pd.DataFrame,
        topology: str = CPE_RANDLES,
    ) -> None:
        """Validate inputs and initialise fitter state.

        Args:
            data: DataFrame with columns ``Frequency_Hz``, ``Z_real_Ohm``,
                ``Z_imag_Ohm`` (same contract as :class:`EISAnalyzer`).
            topology: One of :attr:`CPE_RANDLES`, :attr:`RANDLES_WARBURG`,
                or :attr:`TLM_POROUS`. Defaults to ``CPE_RANDLES``.

        Raises:
            ValueError: If ``data`` is empty, is missing any required column,
                or if ``topology`` is not one of the supported strings.
            ImportError: If the ``impedance`` library is not installed.
        """
        if not _IMPEDANCE_AVAILABLE:
            raise ImportError(
                "impedance.py library required for EISCircuitFitter. "
                "Install via: pip install 'impedance>=1.4'"
            )

        if data.empty:
            raise ValueError("Data DataFrame is empty")

        required_cols = ['Frequency_Hz', 'Z_real_Ohm', 'Z_imag_Ohm']
        missing = [col for col in required_cols if col not in data.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        if topology not in self._VALID_TOPOLOGIES:
            raise ValueError(
                f"Unknown topology {topology!r}; "
                f"must be one of {self._VALID_TOPOLOGIES}"
            )

        self.data = data.copy()
        self.topology = topology
        self.fit_result: Optional[Any] = None
        self.scan_name: Optional[str] = None

    def _auto_seed(self) -> List[float]:
        """Build an auto-seed initial guess from the peak-finder.

        Uses :class:`EISAnalyzer` to estimate R_s (HF intercept) and R_ct
        (semicircle peak). Falls back to physically-reasonable defaults
        (R_s=10, R_ct=1000) when the peak-finder returns NaN values from its
        physical-sanity guards. CPE Y0 seeds at 1e-6 F.s^(alpha-1), alpha at
        0.85; Warburg coefficient W at 100 Ohm.s^(-1/2); TLMQ defaults to the
        same Y0 and exponent as CPE.

        Returns:
            List of seed values whose length matches the topology parameter
            count.
        """
        analyzer = EISAnalyzer(self.data)
        analyzer.scan_name = self.scan_name
        try:
            rs_seed, rct_seed = analyzer.calculate_impedance_parameters()
        except Exception:  # noqa: BLE001 - any failure falls through to defaults
            rs_seed, rct_seed = float('nan'), float('nan')

        if not np.isfinite(rs_seed) or rs_seed <= 0:
            rs_seed = 10.0
        if not np.isfinite(rct_seed) or rct_seed <= 0:
            rct_seed = 1000.0

        if self.topology == self.CPE_RANDLES:
            return [rs_seed, rct_seed, 1e-6, 0.85]
        if self.topology == self.RANDLES_WARBURG:
            return [rs_seed, rct_seed, 1e-6, 0.85, 100.0]
        if self.topology == self.TLM_POROUS:
            # TLMQ params: R_ion (pore), Y0-like (q_seed), gamma.
            # Use 1e-4 for q_seed: typical magnitude for porous-electrode
            # distributed capacitance, more robust than the CPE 1e-6 seed
            # for transmission-line topologies.
            return [rs_seed, rct_seed, 1e-4, 0.85]
        # Unreachable - validated in __init__
        raise ValueError(f"Unknown topology: {self.topology}")

    def fit(
        self,
        initial_guess: Optional[List[float]] = None,
    ) -> Dict[str, float]:
        """Fit the chosen equivalent circuit to the impedance data.

        Builds an ``impedance.models.circuits.CustomCircuit`` for ``topology``
        and fits it against the complex impedance ``Z = Z_real + j*Z_imag``
        sampled at ``Frequency_Hz``. When ``initial_guess`` is None, seeds are
        auto-derived from the peak-finder (see :meth:`_auto_seed`).

        Args:
            initial_guess: Optional explicit seed values. Length must match
                the topology's parameter count. If None, auto-seeded.

        Returns:
            Dict mirroring :meth:`get_parameters`. Values are NaN if the fit
            failed to converge.
        """
        if initial_guess is None:
            initial_guess = self._auto_seed()

        frequencies = self.data['Frequency_Hz'].to_numpy(dtype=float)
        z_complex = (
            self.data['Z_real_Ohm'].to_numpy(dtype=float)
            + 1j * self.data['Z_imag_Ohm'].to_numpy(dtype=float)
        )

        circuit = CustomCircuit(
            circuit=self.topology,
            initial_guess=list(initial_guess),
        )

        scan_label = self.scan_name if self.scan_name else "<unnamed scan>"
        try:
            circuit.fit(frequencies, z_complex)
            self.fit_result = circuit
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EISCircuitFitter: fit did not converge for %s "
                "(topology=%s): %r",
                scan_label, self.topology, exc,
            )
            # Store circuit object even on failure so plotting raises the
            # documented RuntimeError uniformly (rather than crashing here).
            self.fit_result = None

        return self.get_parameters()

    def _compute_chi_sq(self) -> float:
        """Compute reduced chi-square from data minus model residuals."""
        if self.fit_result is None or self.fit_result.parameters_ is None:
            return float('nan')
        frequencies = self.data['Frequency_Hz'].to_numpy(dtype=float)
        z_obs = (
            self.data['Z_real_Ohm'].to_numpy(dtype=float)
            + 1j * self.data['Z_imag_Ohm'].to_numpy(dtype=float)
        )
        try:
            z_pred = self.fit_result.predict(frequencies)
        except Exception:  # noqa: BLE001
            return float('nan')
        # Sum of squared residuals over real + imag (a standard impedance
        # chi-square; not normalised by uncertainty since we have no per-point
        # sigma estimate from the instrument).
        residuals = z_obs - z_pred
        return float(np.sum(np.abs(residuals) ** 2))

    def _param_keys(self) -> List[str]:
        """Return ordered list of parameter keys for the active topology.

        Keys correspond positionally to the impedance.py ``parameters_`` /
        ``conf_`` arrays for ``self.topology``. Topology-specific aliases
        (e.g. TLM_POROUS' ``TLM_Rion`` / ``TLM_Y0`` / ``TLM_gamma`` which
        mirror ``R_ct`` / ``CPE_Y0`` / ``CPE_alpha``) are added in
        :meth:`get_parameters`, not here, so this list stays 1-to-1 with
        the underlying fit-parameter vector.

        Returns:
            List of canonical key names in impedance.py parameter order.

        Raises:
            ValueError: If ``self.topology`` is not one of the supported
                topologies (defensive; ``__init__`` already validates).
        """
        if self.topology == self.CPE_RANDLES:
            # impedance.py parameter order: R0, R1, CPE1_0, CPE1_1
            return ['R_s', 'R_ct', 'CPE_Y0', 'CPE_alpha']
        if self.topology == self.RANDLES_WARBURG:
            # impedance.py parameter order: R0, R1, CPE1_0, CPE1_1, W
            return ['R_s', 'R_ct', 'CPE_Y0', 'CPE_alpha', 'W']
        if self.topology == self.TLM_POROUS:
            # impedance.py parameter order: R0 (R_s), TLMQ0_0 (pore R_ion),
            # TLMQ0_1 (distributed Y0), TLMQ0_2 (gamma exponent). R_ct holds
            # the same slot as TLM_Rion to keep cross-topology AIC bench-
            # marking aligned; TLM_*-aliases are layered on in
            # get_parameters().
            return ['R_s', 'R_ct', 'CPE_Y0', 'CPE_alpha']
        raise ValueError(f"Unknown topology: {self.topology}")

    def get_parameters(self) -> Dict[str, float]:
        """Return fitted parameters and 1-sigma uncertainties.

        Common keys for all topologies: ``R_s``, ``R_ct``, ``CPE_Y0``,
        ``CPE_alpha``, ``chi_sq``, and matching ``*_err`` keys for each fit
        parameter (1-sigma from ``sqrt(diag(covariance))``, exposed by
        impedance.py via ``CustomCircuit.conf_``).

        Topology-specific extras:
            RANDLES_WARBURG: ``W``, ``W_err``.
            TLM_POROUS: ``TLM_Rion``, ``TLM_Y0``, ``TLM_gamma`` plus matching
                ``*_err``. These map onto the TLMQ element's three parameters
                in canonical order.

        Returns:
            Dict[str, float]. All values are NaN if the fit did not converge.

        Raises:
            RuntimeError: If :meth:`fit` has not been called yet (i.e.,
                ``self.fit_result is None`` AND no prior failed fit attempt).
        """
        if self.fit_result is None:
            raise RuntimeError("Call fit() before get_parameters()")

        params = self.fit_result.parameters_
        errs = self.fit_result.conf_

        # impedance.py sets parameters_ to None when fit() was never called;
        # but if we land here, fit() ran. A truly failed fit returns NaNs.
        if params is None:
            return self._nan_parameter_dict()

        params = np.asarray(params, dtype=float)
        errs = (
            np.asarray(errs, dtype=float)
            if errs is not None
            else np.full_like(params, np.nan)
        )

        keys = self._param_keys()
        # Build value + error dict from the ordered key list. This replaces
        # the previous per-topology if/elif branch that hand-assembled each
        # dict; behavior is identical (same keys, same values, same NaN
        # propagation) but readability scales linearly with topology count.
        out: Dict[str, float] = {key: float(params[i]) for i, key in enumerate(keys)}
        out['chi_sq'] = self._compute_chi_sq()
        for i, key in enumerate(keys):
            out[f'{key}_err'] = float(errs[i])

        # Topology-specific aliasing: TLM_POROUS exposes the TLMQ element's
        # three parameters under TLM_*-prefixed keys in addition to the
        # canonical R_ct / CPE_Y0 / CPE_alpha slots, so downstream code can
        # disambiguate "this came from a transmission-line fit" without
        # inspecting topology. Indices 1/2/3 in the impedance.py parameter
        # vector for TLM_POROUS are TLMQ0_0/1/2 (R_ion, Y0, gamma).
        if self.topology == self.TLM_POROUS:
            out['TLM_Rion'] = float(params[1])
            out['TLM_Y0'] = float(params[2])
            out['TLM_gamma'] = float(params[3])
            out['TLM_Rion_err'] = float(errs[1])
            out['TLM_Y0_err'] = float(errs[2])
            out['TLM_gamma_err'] = float(errs[3])

        return out

    def _nan_parameter_dict(self) -> Dict[str, float]:
        """Return an all-NaN parameter dict (used for non-converged fits)."""
        common = {
            'R_s': float('nan'),
            'R_ct': float('nan'),
            'CPE_Y0': float('nan'),
            'CPE_alpha': float('nan'),
            'chi_sq': float('nan'),
            'R_s_err': float('nan'),
            'R_ct_err': float('nan'),
            'CPE_Y0_err': float('nan'),
            'CPE_alpha_err': float('nan'),
        }
        if self.topology == self.RANDLES_WARBURG:
            common['W'] = float('nan')
            common['W_err'] = float('nan')
        elif self.topology == self.TLM_POROUS:
            common['TLM_Rion'] = float('nan')
            common['TLM_Y0'] = float('nan')
            common['TLM_gamma'] = float('nan')
            common['TLM_Rion_err'] = float('nan')
            common['TLM_Y0_err'] = float('nan')
            common['TLM_gamma_err'] = float('nan')
        return common

    def _predict_on_grid(self, n_points: int = 200) -> Tuple[np.ndarray, np.ndarray]:
        """Predict fit impedance on a fine log-frequency grid for plotting.

        Grid is generated descending (high f -> low f) to match the typical
        EIS sweep ordering and the order ``impedance.py`` was fit with. This
        is purely defensive; impedance.py is generally tolerant of grid
        order, but keeping prediction and fit grids aligned removes a class
        of subtle plotting bugs.
        """
        if self.fit_result is None:
            raise RuntimeError("Call fit() before plotting")
        fmin = float(self.data['Frequency_Hz'].min())
        fmax = float(self.data['Frequency_Hz'].max())
        f_fine = np.logspace(np.log10(fmin), np.log10(fmax), n_points)[::-1]
        z_fit = self.fit_result.predict(f_fine)
        return f_fine, z_fit

    def plot_nyquist_with_fit(
        self,
        figsize: Tuple[int, int] = (6, 6),
    ) -> plt.Figure:
        """Generate a Nyquist plot with data markers and fit overlay.

        Args:
            figsize: Figure size in inches.

        Returns:
            matplotlib Figure with measured points as markers (no connecting
            line) and the fit interpolated across a fine log-frequency grid
            as a smooth line. Equal aspect ratio, grid enabled, no R_s / R_ct
            / chi-sq text labels (matches :meth:`EISAnalyzer.plot_nyquist`
            convention - user annotates in vector editor).

        Raises:
            RuntimeError: If :meth:`fit` has not been called.
        """
        if self.fit_result is None:
            raise RuntimeError("Call fit() before plotting")

        _, z_fit = self._predict_on_grid()

        fig, ax = plt.subplots(figsize=figsize)
        ax.plot(
            self.data['Z_real_Ohm'],
            -self.data['Z_imag_Ohm'],
            'o',
            markersize=5,
            label='Data',
        )
        ax.plot(
            z_fit.real,
            -z_fit.imag,
            '-',
            linewidth=1.5,
            color='C1',
            label='Fit',
        )
        ax.set_xlabel("Z' (Ω)", fontsize=11)
        ax.set_ylabel("-Z'' (Ω)", fontsize=11)
        ax.set_title('Nyquist Plot with Fit', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_aspect('equal', adjustable='datalim')
        ax.legend(loc='best', fontsize=9)
        plt.tight_layout()
        return fig

    def plot_bode_with_fit(
        self,
        figsize: Tuple[int, int] = (8, 6),
    ) -> plt.Figure:
        """Generate a Bode plot (magnitude and phase) with fit overlay.

        Args:
            figsize: Figure size in inches.

        Returns:
            matplotlib Figure with two stacked panels (sharex). Top panel
            shows ``|Z|`` vs frequency on log-log axes; bottom panel shows
            phase (degrees) vs frequency on semilog-x axes. Data appears as
            markers, fit as a smooth line on each panel.

        Raises:
            RuntimeError: If :meth:`fit` has not been called.
        """
        if self.fit_result is None:
            raise RuntimeError("Call fit() before plotting")

        f_data = self.data['Frequency_Hz'].to_numpy(dtype=float)
        z_real = self.data['Z_real_Ohm'].to_numpy(dtype=float)
        z_imag = self.data['Z_imag_Ohm'].to_numpy(dtype=float)
        mag_data = np.sqrt(z_real ** 2 + z_imag ** 2)
        phase_data = np.degrees(np.arctan2(z_imag, z_real))

        f_fine, z_fit = self._predict_on_grid()
        mag_fit = np.abs(z_fit)
        phase_fit = np.degrees(np.arctan2(z_fit.imag, z_fit.real))

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=figsize, sharex=True)

        ax1.loglog(f_data, mag_data, 'o', markersize=5, label='Data')
        ax1.loglog(f_fine, mag_fit, '-', linewidth=1.5, color='C1', label='Fit')
        ax1.set_ylabel('|Z| (Ω)', fontsize=11)
        ax1.set_title('Bode Plot with Fit', fontsize=12)
        ax1.grid(True, alpha=0.3, which='both', linestyle='--')
        ax1.legend(loc='best', fontsize=9)

        ax2.semilogx(f_data, phase_data, 'o', markersize=5)
        ax2.semilogx(f_fine, phase_fit, '-', linewidth=1.5, color='C1')
        ax2.set_xlabel('Frequency (Hz)', fontsize=11)
        ax2.set_ylabel('Phase (°)', fontsize=11)
        ax2.grid(True, alpha=0.3, which='both', linestyle='--')

        plt.tight_layout()
        return fig

    def plot_residuals(
        self,
        figsize: Tuple[int, int] = (8, 6),
    ) -> plt.Figure:
        """Generate real / imaginary residual plot (data minus fit) vs frequency.

        Args:
            figsize: Figure size in inches.

        Returns:
            matplotlib Figure with two stacked panels (sharex). Top panel
            shows real-axis residuals (``Z_real_data - Z_real_fit``); bottom
            panel shows imaginary-axis residuals. Zero reference line and
            log-frequency x-axis on both panels.

        Raises:
            RuntimeError: If :meth:`fit` has not been called.
        """
        if self.fit_result is None:
            raise RuntimeError("Call fit() before plotting")

        f_data = self.data['Frequency_Hz'].to_numpy(dtype=float)
        z_real_obs = self.data['Z_real_Ohm'].to_numpy(dtype=float)
        z_imag_obs = self.data['Z_imag_Ohm'].to_numpy(dtype=float)
        z_pred = self.fit_result.predict(f_data)
        real_res = z_real_obs - z_pred.real
        imag_res = z_imag_obs - z_pred.imag

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=figsize, sharex=True)

        ax1.semilogx(f_data, real_res, 'o-', markersize=4, linewidth=1)
        ax1.axhline(0, color='k', linewidth=0.5)
        ax1.set_ylabel("Real residual (Ω)", fontsize=11)
        ax1.set_title('Fit Residuals (data − fit)', fontsize=12)
        ax1.grid(True, alpha=0.3, which='both', linestyle='--')

        ax2.semilogx(f_data, imag_res, 'o-', markersize=4, linewidth=1, color='C1')
        ax2.axhline(0, color='k', linewidth=0.5)
        ax2.set_xlabel('Frequency (Hz)', fontsize=11)
        ax2.set_ylabel("Imag residual (Ω)", fontsize=11)
        ax2.grid(True, alpha=0.3, which='both', linestyle='--')

        plt.tight_layout()
        return fig

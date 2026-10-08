"""
CA Analyzer - Chronoamperometry Biosensor Calibration

Analyzer for enzymatic biosensor calibration data with:
- Step detection from concentration additions
- Baseline subtraction and sentinel correction
- Linear regression for sensitivity, LOD, LOQ (ICH Q2R1)
- Michaelis-Menten kinetic fitting (Km(app), Vmax(app))
- Response time (t10-90) calculation
- Selectivity analysis (vs AA, DA)

Follows patterns from EISAnalyzer, CVAnalyzer, and CoganCICAnalyzer.
"""

import logging
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.stats import linregress
from scipy.signal import find_peaks, savgol_filter
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum physically plausible Km(app) for neurotransmitter-class biosensors,
# in uM. scipy.optimize.curve_fit can return runaway values (10^9-10^10 uM) on
# flat-top / weak-response data; aggregate statistics filter against this bound.
# Per-scan raw fit values are preserved unfiltered for audit. Bump this if the
# codebase is retasked for high-Km enzymes (glucose oxidase, alcohol DH, etc.).
KM_MAX_PHYSICAL_UM = 1e5


def _get_scan_config(config_dict: dict, scan_name: str):
    """
    Get configuration for a specific scan, falling back to Default.

    Args:
        config_dict: Dictionary with "Default" key and optional per-scan overrides
        scan_name: Name of the scan to get config for

    Returns:
        Configuration value for the scan (from scan-specific or Default)
    """
    return config_dict.get(scan_name, config_dict.get("Default"))


class CAAnalyzer:
    """
    Analyzer for Chronoamperometry biosensor calibration data.

    Handles:
    - Raw time-series processing with step detection
    - Baseline subtraction and sentinel correction
    - Linear regression for sensitivity, LOD, LOQ
    - Michaelis-Menten kinetic fitting (Km(app), Vmax(app))
    - Response time (t10-90) calculation
    - Selectivity analysis (vs AA, DA)
    """

    def __init__(self, data: pd.DataFrame):
        """
        Initialize CA analyzer with raw time-series data.

        Args:
            data: DataFrame with columns ['Time (s)', 'Current (A)']

        Raises:
            ValueError: If required columns are missing
        """
        required_cols = ['Time (s)', 'Current (A)']
        missing = [col for col in required_cols if col not in data.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        self.data = data.copy()
        # Drop NaN values from time column
        self.data = self.data.dropna(subset=['Time (s)'])

        # Internal state
        self.steps = None  # List of step indices
        self.step_times = None  # Times when steps occur
        self.steady_state_currents = None  # Current at each step
        self.baseline_mean = None
        self.baseline_std = None
        self.results = {}

    # --- Time-series Processing ---

    def detect_steps_guided(self, addition_times: List[float],
                            search_window_s: float = 15.0,
                            smooth_window_s: float = 2.0) -> List[int]:
        """
        Detect steps using user-provided approximate addition times.

        For each hint time, searches ±search_window_s to find the point
        with the steepest derivative (largest |di/dt|). This avoids false
        positives from noise by only looking where the user expects steps.

        Args:
            addition_times: List of approximate times (seconds from recording start)
                           when concentration additions occurred
            search_window_s: Search window around each hint (seconds). Default ±15s.
            smooth_window_s: Savitzky-Golay smoothing window for derivative (seconds)

        Returns:
            List of indices where steps were detected
        """
        time = self.data['Time (s)'].values
        current = self.data['Current (A)'].values

        # Calculate derivative
        dt = np.diff(time)
        di = np.diff(current)
        di_dt = di / np.maximum(dt, 1e-10)

        # Calculate sample rate
        valid_dt = dt[(~np.isnan(dt)) & (dt > 0)]
        sample_rate = 1 / np.median(valid_dt) if len(valid_dt) > 0 else 10.0

        # Apply Savitzky-Golay smoothing to derivative
        if smooth_window_s > 0 and len(di_dt) > 5:
            window_samples = int(smooth_window_s * sample_rate)
            if window_samples % 2 == 0:
                window_samples += 1
            window_samples = max(5, min(window_samples, len(di_dt) - 1))
            if window_samples % 2 == 0:
                window_samples -= 1
            di_dt = savgol_filter(di_dt, window_samples, polyorder=2)

        abs_di_dt = np.abs(di_dt)

        detected_steps = []
        detected_times = []

        for hint_time in addition_times:
            # Define search window
            start_time = hint_time - search_window_s
            end_time = hint_time + search_window_s

            # Find samples within the search window
            window_mask = (time[:-1] >= start_time) & (time[:-1] <= end_time)
            window_indices = np.where(window_mask)[0]

            if len(window_indices) == 0:
                continue

            # Find the point with maximum |di/dt| in the window
            window_deriv = abs_di_dt[window_indices]
            max_idx_in_window = np.argmax(window_deriv)
            step_idx = window_indices[max_idx_in_window]

            detected_steps.append(step_idx)
            detected_times.append(time[step_idx])

        self.steps = detected_steps
        self.step_times = detected_times

        return self.steps

    def extract_steady_state(self, step_idx: int,
                            window_s: float = 30.0) -> float:
        """
        Extract steady-state current for a specific step.

        Takes average of final `window_s` seconds before next step.

        Args:
            step_idx: Index of the step in self.steps
            window_s: Window duration to average (seconds)

        Returns:
            Steady-state current in Amperes
        """
        if self.steps is None:
            raise ValueError("Call detect_steps_guided() before extract_steady_state()")

        time = self.data['Time (s)'].values
        current = self.data['Current (A)'].values

        step_sample = self.steps[step_idx]

        # Find end of window (next step or end of recording)
        if step_idx + 1 < len(self.steps):
            end_sample = self.steps[step_idx + 1]
        else:
            end_sample = len(time)

        # Find start of averaging window
        end_time = time[end_sample - 1] if end_sample > 0 else time[-1]
        start_time = end_time - window_s

        # Find samples within the window
        window_mask = (time >= start_time) & (time < end_time)
        window_current = current[window_mask]

        if len(window_current) == 0:
            # Fall back to last few samples before next step
            window_current = current[max(0, end_sample - 50):end_sample]

        return np.mean(window_current)

    def extract_all_steady_states(self, window_s: float = 30.0) -> np.ndarray:
        """
        Extract steady-state currents for all detected steps.

        Args:
            window_s: Window duration to average (seconds)

        Returns:
            Array of steady-state currents
        """
        if self.steps is None:
            raise ValueError("Call detect_steps_guided() before extract_all_steady_states()")

        self.steady_state_currents = np.array([
            self.extract_steady_state(i, window_s)
            for i in range(len(self.steps))
        ])

        return self.steady_state_currents

    def get_baseline_stats(self, baseline_window: Tuple[float, float] = None,
                           duration_s: float = None) -> Tuple[float, float]:
        """
        Calculate baseline mean and std from recording period.

        Args:
            baseline_window: Tuple of (start_s, end_s) defining baseline period.
                           If None, falls back to duration_s behavior.
            duration_s: Duration of baseline period from start (seconds).
                       Deprecated - use baseline_window instead.

        Returns:
            (baseline_mean, baseline_std) for LOD calculation
        """
        time = self.data['Time (s)'].values
        current = self.data['Current (A)'].values

        # Use baseline_window if provided, otherwise fall back to duration_s
        if baseline_window is not None:
            start_s, end_s = baseline_window
            baseline_mask = (time >= start_s) & (time <= end_s)
        elif duration_s is not None:
            baseline_mask = time <= duration_s
        else:
            # Default: first 60 seconds
            baseline_mask = time <= 60.0

        baseline_current = current[baseline_mask]

        if len(baseline_current) == 0:
            window_desc = f"{baseline_window}" if baseline_window else f"first {duration_s or 60}s"
            raise ValueError(f"No data within {window_desc}")

        self.baseline_mean = np.mean(baseline_current)
        self.baseline_std = np.std(baseline_current)

        return self.baseline_mean, self.baseline_std

    # --- Corrections ---

    def subtract_baseline(self, baseline_current: float = None) -> pd.DataFrame:
        """
        Subtract baseline from all currents.

        Args:
            baseline_current: Baseline to subtract (uses self.baseline_mean if None)

        Returns:
            DataFrame with baseline-corrected current
        """
        if baseline_current is None:
            if self.baseline_mean is None:
                self.get_baseline_stats()
            baseline_current = self.baseline_mean

        self.data['Current_corrected (A)'] = (
            self.data['Current (A)'] - baseline_current
        )

        return self.data

    def apply_sentinel_correction(self,
                                  sentinel_currents: np.ndarray) -> pd.DataFrame:
        """
        Subtract sentinel channel currents (matched time-series).

        Args:
            sentinel_currents: Array of sentinel currents (same length as data)

        Returns:
            DataFrame with sentinel-corrected current
        """
        if len(sentinel_currents) != len(self.data):
            raise ValueError(
                f"Sentinel data length ({len(sentinel_currents)}) must match "
                f"main data length ({len(self.data)})"
            )

        # Use corrected if available, otherwise raw
        if 'Current_corrected (A)' in self.data.columns:
            self.data['Current_corrected (A)'] -= sentinel_currents
        else:
            self.data['Current_corrected (A)'] = (
                self.data['Current (A)'] - sentinel_currents
            )

        return self.data

    # --- Core Calculations ---

    def fit_linear(self, concentrations: np.ndarray,
                   currents: np.ndarray,
                   conc_range: Tuple[float, float] = None) -> dict:
        """
        Linear fit for sensitivity calculation.

        Args:
            concentrations: Concentration values (µM)
            currents: Corresponding current values (A or nA)
            conc_range: Optional (min, max) concentration range for fit

        Returns:
            Dictionary with slope, intercept, r_squared, std_err
        """
        conc = np.array(concentrations, dtype=float)
        curr = np.array(currents, dtype=float)

        # Filter NaN/inf values before fitting
        valid_mask = np.isfinite(conc) & np.isfinite(curr)
        conc = conc[valid_mask]
        curr = curr[valid_mask]

        # Apply concentration range filter if specified
        if conc_range is not None:
            mask = (conc >= conc_range[0]) & (conc <= conc_range[1])
            conc = conc[mask]
            curr = curr[mask]

        if len(conc) < 2:
            raise ValueError("Need at least 2 valid (non-NaN/inf) points for linear fit")

        # Perform linear regression
        result = linregress(conc, curr)

        return {
            'slope': result.slope,
            'intercept': result.intercept,
            'r_squared': result.rvalue ** 2,
            'std_err': result.stderr,
            'p_value': result.pvalue,
            'n_points': len(conc)
        }

    def calculate_lod_loq(self, baseline_std: float = None,
                          slope: float = None) -> Tuple[float, float]:
        """
        Calculate LOD and LOQ using ICH Q2R1 method.

        LOD = 3.3 * sigma / slope
        LOQ = 10 * sigma / slope

        Args:
            baseline_std: Standard deviation of blank (uses self.baseline_std if None)
            slope: Sensitivity slope (uses results if available)

        Returns:
            (LOD, LOQ) in same units as concentrations
        """
        if baseline_std is None:
            if self.baseline_std is None:
                self.get_baseline_stats()
            baseline_std = self.baseline_std

        if slope is None:
            if 'sensitivity' in self.results:
                slope = self.results['sensitivity']
            else:
                raise ValueError("Slope not provided and not available in results")

        if abs(slope) < 1e-15:
            raise ValueError(
                "Slope is approximately zero (flat calibration curve). "
                "LOD/LOQ cannot be calculated without measurable sensitivity."
            )

        lod = 3.3 * baseline_std / abs(slope)
        loq = 10 * baseline_std / abs(slope)

        return lod, loq

    def fit_michaelis_menten(self, concentrations: np.ndarray,
                             currents: np.ndarray) -> dict:
        """
        Fit Michaelis-Menten kinetics: I = (Imax * [S]) / (Km + [S])

        Args:
            concentrations: Substrate concentrations (µM)
            currents: Corresponding current values

        Returns:
            Dictionary with Imax, Km_app, r_squared, pcov
        """
        def mm_equation(s, imax, km):
            return (imax * s) / (km + s)

        conc = np.array(concentrations)
        curr = np.array(currents)

        # Initial guesses
        imax_guess = np.max(curr) * 1.2
        km_guess = conc[len(conc) // 2]

        try:
            popt, pcov = curve_fit(
                mm_equation, conc, curr,
                p0=[imax_guess, km_guess],
                maxfev=5000,
                bounds=([0, 0], [np.inf, np.inf])
            )

            # Calculate R²
            predicted = mm_equation(conc, *popt)
            ss_res = np.sum((curr - predicted) ** 2)
            ss_tot = np.sum((curr - np.mean(curr)) ** 2)
            r_squared = 1 - (ss_res / ss_tot)

            return {
                'Imax': popt[0],
                'Km_app': popt[1],
                'r_squared': r_squared,
                'pcov': pcov,
                'imax_std': np.sqrt(pcov[0, 0]),
                'km_std': np.sqrt(pcov[1, 1])
            }

        except (RuntimeError, ValueError) as e:
            logger.warning(f"Michaelis-Menten fit failed: {e}")
            return {
                'Imax': np.nan,
                'Km_app': np.nan,
                'r_squared': np.nan,
                'error': str(e)
            }

    def calculate_response_time(self, step_idx: int) -> float:
        """
        Calculate t10-90 response time for specific concentration step.

        Args:
            step_idx: Index of the step in self.steps

        Returns:
            Time (s) from 10% to 90% of steady-state response
        """
        if self.steps is None:
            raise ValueError("Call detect_steps_guided() before calculate_response_time()")

        time = self.data['Time (s)'].values
        current = self.data['Current (A)'].values

        step_sample = self.steps[step_idx]

        # Get current before step (baseline for this step)
        if step_idx > 0:
            prev_step = self.steps[step_idx - 1]
            # Guard against empty slice if steps are too close
            if prev_step >= step_sample:
                pre_step_current = current[:step_sample].mean()
            else:
                pre_step_current = current[prev_step:step_sample].mean()
        else:
            pre_step_current = current[:step_sample].mean()

        # Get steady-state current after step
        ss_current = self.extract_steady_state(step_idx)

        # Calculate 10% and 90% levels
        delta_current = ss_current - pre_step_current
        level_10 = pre_step_current + 0.1 * delta_current
        level_90 = pre_step_current + 0.9 * delta_current

        # Find end of step window
        if step_idx + 1 < len(self.steps):
            end_sample = self.steps[step_idx + 1]
        else:
            end_sample = len(time)

        # Get data for this step
        step_time = time[step_sample:end_sample]
        step_current = current[step_sample:end_sample]

        # Find when current crosses 10% and 90% levels
        if delta_current > 0:  # Increasing current
            t10_idx = np.where(step_current >= level_10)[0]
            t90_idx = np.where(step_current >= level_90)[0]
        else:  # Decreasing current
            t10_idx = np.where(step_current <= level_10)[0]
            t90_idx = np.where(step_current <= level_90)[0]

        if len(t10_idx) == 0 or len(t90_idx) == 0:
            return np.nan

        t10 = step_time[t10_idx[0]] - step_time[0]
        t90 = step_time[t90_idx[0]] - step_time[0]

        # Validate t90 > t10 (can be inverted for noisy/malformed signals)
        if t90 <= t10:
            return np.nan

        return t90 - t10

    # --- Selectivity ---

    def calculate_selectivity(self, analyte_sensitivity: float,
                              interferent_response: float,
                              interferent_conc: float) -> float:
        """
        Calculate selectivity ratio.

        Selectivity = (Sensitivity_analyte) / (Response_interferent / [interferent])

        Args:
            analyte_sensitivity: Sensitivity to primary analyte (nA/µM)
            interferent_response: Current response to interferent (nA)
            interferent_conc: Interferent concentration (µM)

        Returns:
            Selectivity ratio (dimensionless)
        """
        if interferent_response == 0 or interferent_conc == 0:
            return np.inf

        interferent_sensitivity = interferent_response / interferent_conc
        selectivity = abs(analyte_sensitivity) / abs(interferent_sensitivity)

        return selectivity

    # --- Complete Pipeline ---

    def analyze_calibration(self, concentrations: List[float],
                           electrode_area_um2: float = 2000,
                           linear_range: Tuple[float, float] = (0.5, 50),
                           baseline_window: Tuple[float, float] = None,
                           steady_state_window_s: float = 30) -> dict:
        """
        Complete calibration analysis pipeline.

        Requires detect_steps_guided() to be called first.

        1. Extract steady-state currents
        2. Calculate baseline stats
        3. Fit linear model (in linear_range)
        4. Calculate LOD/LOQ
        5. Fit Michaelis-Menten (full range)
        6. Calculate response times

        Args:
            concentrations: Expected concentration sequence (µM)
            electrode_area_um2: Electrode area for normalization
            linear_range: Concentration range for linear fit (µM)
            baseline_window: Tuple of (start_s, end_s) for baseline noise calculation
            steady_state_window_s: Window for steady-state averaging

        Returns:
            Dictionary with all results
        """
        # 1. Verify steps were detected via detect_steps_guided()
        if self.steps is None:
            raise ValueError("Call detect_steps_guided() before analyze_calibration()")

        if len(self.steps) != len(concentrations):
            logger.warning(f"Detected {len(self.steps)} steps, expected {len(concentrations)}")

        # Use minimum of detected steps and provided concentrations
        n_steps = min(len(self.steps), len(concentrations))
        concentrations = np.array(concentrations[:n_steps])

        # 2. Extract steady-state currents
        currents = self.extract_all_steady_states(steady_state_window_s)[:n_steps]

        # Convert to nA for readability
        currents_nA = currents * 1e9

        # 3. Baseline stats
        if baseline_window is not None:
            self.get_baseline_stats(baseline_window=baseline_window)
        else:
            self.get_baseline_stats()  # Use default (first 60s)
        baseline_nA = self.baseline_mean * 1e9
        baseline_std_nA = self.baseline_std * 1e9

        # 4. Subtract baseline from currents
        currents_corrected_nA = currents_nA - baseline_nA

        # 5. Linear fit in specified range
        linear_result = self.fit_linear(
            concentrations, currents_corrected_nA,
            conc_range=linear_range
        )
        sensitivity_nA_uM = linear_result['slope']

        # 6. LOD/LOQ
        lod, loq = self.calculate_lod_loq(baseline_std_nA, sensitivity_nA_uM)

        # 7. Michaelis-Menten fit (full range)
        mm_result = self.fit_michaelis_menten(concentrations, currents_corrected_nA)

        # 8. Response times
        response_times = []
        for i in range(n_steps):
            try:
                t_response = self.calculate_response_time(i)
                response_times.append(t_response)
            except Exception as e:
                logger.debug(f"Response time calculation failed for step {i}: {e}")
                response_times.append(np.nan)

        # Calculate area-normalized sensitivity
        electrode_area_cm2 = electrode_area_um2 * 1e-8
        sensitivity_nA_uM_cm2 = sensitivity_nA_uM / electrode_area_cm2

        # Store results
        self.results = {
            'technique': 'CA',
            'n_steps': n_steps,
            'concentrations_uM': concentrations.tolist(),
            'currents_nA': currents_corrected_nA.tolist(),
            'baseline_mean_nA': baseline_nA,
            'baseline_std_nA': baseline_std_nA,
            'sensitivity_nA_uM': sensitivity_nA_uM,
            'sensitivity_nA_uM_cm2': sensitivity_nA_uM_cm2,
            'linear_intercept_nA': linear_result['intercept'],
            'linear_r_squared': linear_result['r_squared'],
            'linear_range_uM': linear_range,
            'lod_uM': lod,
            'loq_uM': loq,
            'Km_app_uM': mm_result.get('Km_app', np.nan),
            'Imax_nA': mm_result.get('Imax', np.nan),
            'mm_r_squared': mm_result.get('r_squared', np.nan),
            'response_times_s': response_times,
            'mean_response_time_s': np.nanmean(response_times),
            'electrode_area_um2': electrode_area_um2
        }

        return self.results

    # --- Visualization ---

    def plot_raw_timeseries(self, mark_steps: bool = True,
                            figsize: Tuple[int, int] = (10, 4),
                            xlim: Tuple[float, float] = None,
                            ylim: Tuple[float, float] = None,
                            tail_s: float = None) -> plt.Figure:
        """
        Plot full recording with detected step markers.

        Args:
            mark_steps: If True, add vertical lines at step locations
            figsize: Figure size
            xlim: Optional (xmin, xmax) time window in seconds
            ylim: Optional (ymin, ymax) current limits in nA
            tail_s: If set, show only the last N seconds. Overrides xlim.
                Y-axis auto-fits to the visible data window.

        Returns:
            matplotlib Figure object
        """
        fig, ax = plt.subplots(figsize=figsize)

        time = self.data['Time (s)'].values
        current_nA = self.data['Current (A)'].values * 1e9

        # Compute window from tail_s
        if tail_s is not None:
            t_max = time[-1]
            xlim = (t_max - tail_s, t_max)

        ax.plot(time, current_nA, 'b-', linewidth=0.5)

        if mark_steps and self.steps is not None:
            for step_idx in self.steps:
                ax.axvline(time[step_idx], color='r', linestyle='--',
                          alpha=0.5, linewidth=0.8)

        ax.set_xlabel('Time (s)', fontsize=11)
        ax.set_ylabel('Current (nA)', fontsize=11)
        ax.set_title('Chronoamperometry Time Series', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')

        if xlim is not None:
            ax.set_xlim(xlim)
            # Auto-fit y-axis to visible data if ylim not explicitly set
            if ylim is None:
                mask = (time >= xlim[0]) & (time <= xlim[1])
                if mask.any():
                    visible = current_nA[mask]
                    margin = (visible.max() - visible.min()) * 0.05
                    ylim = (visible.min() - margin, visible.max() + margin)
        if ylim is not None:
            ax.set_ylim(ylim)

        plt.tight_layout()
        return fig

    def plot_calibration_curve(self, show_mm_fit: bool = True,
                               show_linear_fit: bool = True,
                               figsize: Tuple[int, int] = (6, 5)) -> plt.Figure:
        """
        Plot concentration vs current with optional fits.

        Args:
            show_mm_fit: Show Michaelis-Menten curve
            show_linear_fit: Show linear fit line
            figsize: Figure size

        Returns:
            matplotlib Figure object
        """
        if not self.results:
            raise ValueError("Run analyze_calibration() first")

        fig, ax = plt.subplots(figsize=figsize)

        conc = np.array(self.results['concentrations_uM'])
        curr = np.array(self.results['currents_nA'])

        # Plot data points
        ax.plot(conc, curr, 'ko', markersize=6, label='Data')

        # Linear fit using stored slope and intercept
        if show_linear_fit and 'sensitivity_nA_uM' in self.results:
            linear_range = self.results.get('linear_range_uM', (conc.min(), conc.max()))
            linear_conc = np.linspace(linear_range[0], linear_range[1], 100)
            linear_curr = (self.results['sensitivity_nA_uM'] * linear_conc +
                          self.results['linear_intercept_nA'])
            ax.plot(linear_conc, linear_curr, 'b--', linewidth=1.5,
                   label=f'Linear (R²={self.results["linear_r_squared"]:.4f})')

        # Michaelis-Menten fit
        if show_mm_fit and 'Km_app_uM' in self.results:
            if not np.isnan(self.results['Km_app_uM']):
                mm_conc = np.linspace(0, conc.max() * 1.1, 100)
                imax = self.results['Imax_nA']
                km = self.results['Km_app_uM']
                mm_curr = (imax * mm_conc) / (km + mm_conc)
                ax.plot(mm_conc, mm_curr, 'r-', linewidth=1.5,
                       label=f'M-M (Km={km:.1f} µM)')

        ax.set_xlabel('Concentration (µM)', fontsize=11)
        ax.set_ylabel('Current (nA)', fontsize=11)
        ax.set_title('Calibration Curve', fontsize=12)
        ax.legend(loc='best', fontsize=9)
        ax.grid(True, alpha=0.3, linestyle='--')

        plt.tight_layout()
        return fig

    # --- Individual Characterization Plots ---

    def plot_sensitivity(self, figsize: Tuple[int, int] = (5, 4)) -> plt.Figure:
        """
        Plot sensitivity with linear fit and R² value.

        Returns:
            matplotlib Figure object
        """
        if not self.results:
            raise ValueError("Run analyze_calibration() first")

        fig, ax = plt.subplots(figsize=figsize)

        conc = np.array(self.results['concentrations_uM'])
        curr = np.array(self.results['currents_nA'])
        sensitivity = self.results['sensitivity_nA_uM']
        intercept = self.results['linear_intercept_nA']
        r_squared = self.results['linear_r_squared']

        # Plot data points
        ax.plot(conc, curr, 'ko', markersize=6, label='Data')

        # Linear fit line using stored slope and intercept
        fit_conc = np.linspace(0, max(conc), 100)
        fit_curr = sensitivity * fit_conc + intercept
        ax.plot(fit_conc, fit_curr, 'b-', linewidth=1.5,
               label=f'Linear fit (R²={r_squared:.4f})')

        ax.set_xlabel('Concentration (µM)', fontsize=11)
        ax.set_ylabel('Current (nA)', fontsize=11)
        ax.set_title(f'Sensitivity: {sensitivity:.4f} nA/µM', fontsize=12)
        ax.legend(loc='best', fontsize=9)
        ax.grid(True, alpha=0.3, linestyle='--')

        plt.tight_layout()
        return fig

    def plot_michaelis_menten(self, figsize: Tuple[int, int] = (6, 5)) -> plt.Figure:
        """
        Plot Michaelis-Menten fit with Km and Imax values.

        Shows normalized I/Imax on left axis and raw current on right axis.

        Returns:
            matplotlib Figure object
        """
        if not self.results:
            raise ValueError("Run analyze_calibration() first")

        km = self.results['Km_app_uM']
        imax = self.results['Imax_nA']

        if np.isnan(km) or np.isnan(imax):
            raise ValueError("Km and Imax must be valid for M-M plot")

        fig, ax_norm = plt.subplots(figsize=figsize)

        conc = np.array(self.results['concentrations_uM'])
        curr = np.array(self.results['currents_nA'])

        # Normalize currents to I/Imax
        curr_normalized = curr / imax

        # Plot normalized data points
        ax_norm.plot(conc, curr_normalized, 'ko', markersize=6, label='Data')

        # Michaelis-Menten fit curve (normalized)
        mm_conc = np.linspace(0, max(conc) * 1.1, 100)
        mm_curr_norm = mm_conc / (km + mm_conc)  # Normalized: [S]/(Km+[S])
        ax_norm.plot(mm_conc, mm_curr_norm, 'r-', linewidth=1.5, label='M-M fit')

        # Mark Km on plot with lines and point
        ax_norm.axvline(km, color='gray', linestyle='--', alpha=0.5)
        ax_norm.axhline(0.5, color='gray', linestyle=':', alpha=0.5)

        # Add Km point marker and annotation
        ax_norm.plot(km, 0.5, 'bo', markersize=8, zorder=5)
        ax_norm.annotate('Km', xy=(km, 0.5), xytext=(km * 1.3, 0.57),
                        fontsize=10, fontweight='bold',
                        arrowprops=dict(arrowstyle='->', color='blue', lw=1.5))

        # Left axis: normalized
        ax_norm.set_xlabel('Concentration (µM)', fontsize=11)
        ax_norm.set_ylabel('I / Imax', fontsize=11)
        ax_norm.set_ylim(-0.05, 1.1)
        ax_norm.legend(loc='lower right', fontsize=9)
        ax_norm.grid(True, alpha=0.3, linestyle='--')

        # Right axis: raw current (nA)
        ax_raw = ax_norm.twinx()
        ax_raw.set_ylabel('Current (nA)', fontsize=11, color='steelblue')
        ax_raw.set_ylim(-0.05 * imax, 1.1 * imax)
        ax_raw.tick_params(axis='y', labelcolor='steelblue')

        ax_norm.set_title(f'Km={km:.1f} µM, Imax={imax:.1f} nA', fontsize=12)

        plt.tight_layout()
        return fig

    def plot_calibration_semilog(self, figsize: Tuple[int, int] = (7, 5)) -> plt.Figure:
        """
        Plot calibration curve with log-scale concentration axis (Wikipedia M-M style).

        Shows normalized I/Imax on left axis and raw current on right axis.
        Marks Km and Imax/2 with annotations.

        Args:
            figsize: Figure size

        Returns:
            matplotlib Figure object
        """
        if not self.results:
            raise ValueError("Run analyze_calibration() first")

        km = self.results.get('Km_app_uM', np.nan)
        imax = self.results.get('Imax_nA', np.nan)

        if np.isnan(km) or np.isnan(imax):
            raise ValueError("Km and Imax must be valid for semilog plot")

        fig, ax_norm = plt.subplots(figsize=figsize)

        conc = np.array(self.results['concentrations_uM'])
        curr = np.array(self.results['currents_nA'])

        # Filter out zero/negative concentrations for log scale
        valid_mask = conc > 0
        conc_valid = conc[valid_mask]
        curr_valid = curr[valid_mask]

        # Normalize currents to I/Imax
        curr_normalized = curr_valid / imax

        # Create smooth M-M curve spanning wider range for sigmoid shape
        conc_min = conc_valid.min() / 10  # Extend below data
        conc_max = conc_valid.max() * 10  # Extend above data
        mm_conc = np.logspace(np.log10(conc_min), np.log10(conc_max), 200)
        mm_curr_norm = mm_conc / (km + mm_conc)  # Normalized M-M: [S]/(Km+[S])

        # Plot normalized M-M curve (sigmoid)
        ax_norm.semilogx(mm_conc, mm_curr_norm, 'k-', linewidth=2, label='M-M fit')

        # Plot normalized data points
        ax_norm.semilogx(conc_valid, curr_normalized, 'ko', markersize=7)

        # Mark Imax/2 (y=0.5) with horizontal line
        ax_norm.axhline(0.5, color='gray', linestyle='--', alpha=0.6, linewidth=1)
        ax_norm.text(conc_min * 1.5, 0.52, 'Imax/2', fontsize=10, color='gray')

        # Mark Km with vertical line
        ax_norm.axvline(km, color='gray', linestyle='--', alpha=0.6, linewidth=1)
        ax_norm.text(km * 1.1, 0.05, f'Km={km:.1f}', fontsize=10, color='gray', rotation=90)

        # Calculate and annotate maximum slope at inflection point
        # For M-M on semilog, max slope = 0.576 * Imax (in normalized units = 0.576)
        # Slope in terms of d(I/Imax)/d(log[S]) at Km
        max_slope_norm = 0.576  # Theoretical max slope for M-M on semilog
        ax_norm.annotate(f'Max slope\n= 0.576',
                        xy=(km, 0.5), xytext=(km * 5, 0.65),
                        fontsize=9, ha='center',
                        arrowprops=dict(arrowstyle='->', color='darkblue', lw=1.2))

        # Left axis: normalized
        ax_norm.set_xlabel('Concentration (µM)', fontsize=11)
        ax_norm.set_ylabel('I / Imax', fontsize=11)
        ax_norm.set_ylim(-0.05, 1.1)
        ax_norm.set_xlim(conc_min, conc_max)

        # Right axis: raw current (nA)
        ax_raw = ax_norm.twinx()
        ax_raw.set_ylabel('Current (nA)', fontsize=11, color='steelblue')
        ax_raw.set_ylim(-0.05 * imax, 1.1 * imax)
        ax_raw.tick_params(axis='y', labelcolor='steelblue')

        ax_norm.set_title('Michaelis-Menten Kinetics (Semilog)', fontsize=12)
        ax_norm.grid(True, alpha=0.2, linestyle='-', which='both')

        plt.tight_layout()
        return fig

    def plot_response_time_summary(self, figsize: Tuple[int, int] = (5, 4)) -> plt.Figure:
        """
        Plot summary of response times across all steps.

        Returns:
            matplotlib Figure object
        """
        if not self.results:
            raise ValueError("Run analyze_calibration() first")

        fig, ax = plt.subplots(figsize=figsize)

        response_times = self.results['response_times_s']
        mean_rt = self.results['mean_response_time_s']
        n_steps = len(response_times)

        # Plot bar chart of response times
        step_labels = [f'Step {i+1}' for i in range(n_steps)]
        bars = ax.bar(step_labels, response_times, color='steelblue', width=0.6)

        # Add mean line
        ax.axhline(mean_rt, color='red', linestyle='--', linewidth=1.5,
                  label=f'Mean: {mean_rt:.2f} s')

        ax.set_ylabel('Response Time (s)', fontsize=11)
        ax.set_title('t₁₀₋₉₀ Response Times', fontsize=12)
        ax.legend(loc='best', fontsize=9)
        ax.grid(True, alpha=0.3, linestyle='--', axis='y')
        ax.tick_params(axis='x', rotation=45)

        plt.tight_layout()
        return fig

    def export_characterization_figures(self, export_manager, scan_name: str):
        """
        Export characterization figures as individual SVGs.

        Handles failures gracefully - if a specific plot fails (e.g., M-M fit
        was invalid), it logs a warning and continues with other plots.

        Args:
            export_manager: ExportManager instance for saving files
            scan_name: Scan name for filename prefix
        """
        clean_name = scan_name.replace('/', '_').replace('\\', '_').replace(' ', '_')

        # Generate and save each figure - handle failures individually
        try:
            fig_sens = self.plot_sensitivity()
            export_manager.save_figure(fig_sens, f"{clean_name}_sensitivity", subdir="plots")
            plt.close(fig_sens)
        except Exception as e:
            logger.warning(f"Failed to generate sensitivity plot for {scan_name}: {e}")

        try:
            fig_mm = self.plot_michaelis_menten()
            export_manager.save_figure(fig_mm, f"{clean_name}_michaelis_menten", subdir="plots")
            plt.close(fig_mm)
        except Exception as e:
            logger.warning(f"Failed to generate M-M plot for {scan_name}: {e}")

        try:
            fig_semilog = self.plot_calibration_semilog()
            export_manager.save_figure(fig_semilog, f"{clean_name}_calibration_semilog", subdir="plots")
            plt.close(fig_semilog)
        except Exception as e:
            logger.warning(f"Failed to generate semilog plot for {scan_name}: {e}")

        try:
            fig_rt = self.plot_response_time_summary()
            export_manager.save_figure(fig_rt, f"{clean_name}_response_time", subdir="plots")
            plt.close(fig_rt)
        except Exception as e:
            logger.warning(f"Failed to generate response time plot for {scan_name}: {e}")

    # --- Export ---

    def get_results_dataframe(self) -> pd.DataFrame:
        """
        Return results as single-row DataFrame.

        Returns:
            DataFrame with analysis results
        """
        if not self.results:
            raise ValueError("Run analyze_calibration() first")

        # Create flat dictionary for DataFrame
        flat_results = {
            'sensitivity_nA_uM': self.results.get('sensitivity_nA_uM'),
            'sensitivity_nA_uM_cm2': self.results.get('sensitivity_nA_uM_cm2'),
            'linear_r_squared': self.results.get('linear_r_squared'),
            'LOD_uM': self.results.get('lod_uM'),
            'LOQ_uM': self.results.get('loq_uM'),
            'Km_app_uM': self.results.get('Km_app_uM'),
            'Imax_nA': self.results.get('Imax_nA'),
            'mm_r_squared': self.results.get('mm_r_squared'),
            'mean_response_time_s': self.results.get('mean_response_time_s'),
            'baseline_std_nA': self.results.get('baseline_std_nA'),
            'n_steps': self.results.get('n_steps'),
            'electrode_area_um2': self.results.get('electrode_area_um2')
        }

        return pd.DataFrame([flat_results])

    def print_results(self):
        """Print formatted results summary to console."""
        if not self.results:
            print("No results available. Run analyze_calibration() first.")
            return

        r = self.results
        print("\n" + "=" * 50)
        print("CHRONOAMPEROMETRY CALIBRATION RESULTS")
        print("=" * 50)

        print(f"\nSensitivity:")
        print(f"  Slope: {r.get('sensitivity_nA_uM', np.nan):.4f} nA/µM")
        print(f"  Area-normalized: {r.get('sensitivity_nA_uM_cm2', np.nan):.4f} nA/µM·cm²")
        print(f"  Linear R²: {r.get('linear_r_squared', np.nan):.4f}")
        print(f"  Linear range: {r.get('linear_range_uM', 'N/A')} µM")

        print(f"\nDetection Limits (ICH Q2R1):")
        print(f"  LOD: {r.get('lod_uM', np.nan):.3f} µM")
        print(f"  LOQ: {r.get('loq_uM', np.nan):.3f} µM")

        print(f"\nMichaelis-Menten Kinetics:")
        print(f"  Km(app): {r.get('Km_app_uM', np.nan):.1f} µM")
        print(f"  Imax: {r.get('Imax_nA', np.nan):.2f} nA")
        print(f"  M-M R²: {r.get('mm_r_squared', np.nan):.4f}")

        print(f"\nResponse Time:")
        print(f"  Mean t₁₀₋₉₀: {r.get('mean_response_time_s', np.nan):.2f} s")

        print(f"\nBaseline:")
        print(f"  Mean: {r.get('baseline_mean_nA', np.nan):.3f} nA")
        print(f"  Std: {r.get('baseline_std_nA', np.nan):.3f} nA")

        print("=" * 50)

    # --- Batch Processing ---

    @staticmethod
    def batch_analyze(scans_dict: Dict[str, pd.DataFrame],
                      concentrations_config: Dict[str, List[float]],
                      addition_times_config: Dict[str, List[float]],
                      baseline_window_config: Dict[str, Tuple[float, float]],
                      addition_search_window_s: float = 15.0,
                      electrode_area_um2: float = 2000,
                      linear_range: Tuple[float, float] = (0.5, 50),
                      export_manager=None,
                      steady_state_window_s: float = 30,
                      timeseries_tail_s: float = None) -> Tuple[pd.DataFrame, Dict, Dict]:
        """
        Batch analyze multiple scans from .pssession file using config dicts.

        Each config dict has format: {"Default": [...], "ScanName": [...]}
        If a scan name is not found, uses "Default".

        Args:
            scans_dict: {scan_name: DataFrame} from load_psession()
            concentrations_config: Per-scan concentration sequences (µM).
                Format: {"Default": [conc1, conc2, ...], "ScanName": [...]}.
            addition_times_config: Per-scan approximate addition times (seconds).
                Format: {"Default": [t1, t2, ...], "ScanName": [...]}.
                Used for guided step detection.
            baseline_window_config: Per-scan baseline windows for noise estimation.
                Format: {"Default": (start_s, end_s), "ScanName": (...)}.
            addition_search_window_s: Search window around each addition time (seconds).
                Default 15s means ±15s around each hint time.
            electrode_area_um2: Electrode area for normalization
            linear_range: Concentration range for linear fit (µM)
            export_manager: Optional ExportManager for saving results
            steady_state_window_s: Window for steady-state averaging (seconds)
            timeseries_tail_s: If set, timeseries plots show only the last N
                seconds with y-axis auto-fitted to the visible window.

        Returns:
            (summary_df, figures_dict, results_dict) - results_dict can be passed
            to plot_mm_semilog_grouped() for grouped comparison plots
        """
        results_list = []
        figures = {}
        analyzers = {}  # Store analyzers for batch comparison figures
        results_dict = {}  # Store full results for grouped plotting

        for scan_name, data in scans_dict.items():
            try:
                analyzer = CAAnalyzer(data)

                # Get per-scan config (falls back to Default)
                concentrations = _get_scan_config(concentrations_config, scan_name)
                addition_times = _get_scan_config(addition_times_config, scan_name)
                baseline_window = _get_scan_config(baseline_window_config, scan_name)

                if concentrations is None or addition_times is None:
                    logger.warning(f"Skipping {scan_name}: Missing config (no Default or scan-specific)")
                    continue

                # Use guided step detection with user-provided addition times
                analyzer.detect_steps_guided(
                    addition_times=addition_times,
                    search_window_s=addition_search_window_s
                )

                result = analyzer.analyze_calibration(
                    concentrations,
                    electrode_area_um2=electrode_area_um2,
                    linear_range=linear_range,
                    baseline_window=baseline_window,
                    steady_state_window_s=steady_state_window_s
                )
                result['scan_name'] = scan_name
                results_list.append(result)

                # Store analyzer for batch figures
                analyzers[scan_name] = analyzer

                # Store full results for grouped plotting (include calibration data)
                results_dict[scan_name] = {
                    **result,
                    'calibration_data': {
                        'concentrations': concentrations,
                        'currents': analyzer.results.get('currents_nA', [])  # nA for normalization
                    }
                }

                # Generate and save per-scan plots
                ts_fig = analyzer.plot_raw_timeseries(tail_s=timeseries_tail_s)
                cal_fig = analyzer.plot_calibration_curve()

                figures[scan_name] = {
                    'timeseries': ts_fig,
                    'calibration': cal_fig
                }

                if export_manager:
                    clean_name = scan_name.replace('/', '_').replace('\\', '_').replace(' ', '_')
                    export_manager.save_figure(ts_fig, f"{clean_name}_timeseries", subdir="plots")
                    export_manager.save_figure(cal_fig, f"{clean_name}_calibration", subdir="plots")
                    plt.close(ts_fig)
                    plt.close(cal_fig)

                    # Export individual characterization figures
                    analyzer.export_characterization_figures(export_manager, scan_name)

                sens = result.get('sensitivity_nA_uM', np.nan)
                lod = result.get('lod_uM', np.nan)
                n_steps = result.get('n_steps', 0)
                logger.info(f"Analyzed {scan_name}: {n_steps} steps, "
                            f"Sensitivity={sens:.4f} nA/µM, LOD={lod:.1f} µM")

            except Exception as e:
                logger.error(f"Failed to analyze {scan_name}: {e}")
                continue

        # Create summary DataFrame
        if results_list:
            summary_df = pd.DataFrame(results_list)

            # Reorder columns
            cols = ['scan_name', 'sensitivity_nA_uM', 'linear_r_squared',
                    'lod_uM', 'loq_uM', 'Km_app_uM', 'Imax_nA',
                    'mean_response_time_s', 'n_steps']
            summary_df = summary_df[[c for c in cols if c in summary_df.columns]]

            if export_manager:
                export_manager.save_dataframe(summary_df, 'batch_ca_results', subdir='data')

                # Generate batch comparison figures if multiple scans
                if len(results_list) > 1:
                    batch_sens_fig = CAAnalyzer._plot_batch_sensitivity_comparison(summary_df)
                    export_manager.save_figure(batch_sens_fig, 'batch_sensitivity_comparison',
                                              subdir='plots')
                    plt.close(batch_sens_fig)

                    batch_lod_fig = CAAnalyzer._plot_batch_lod_comparison(summary_df)
                    export_manager.save_figure(batch_lod_fig, 'batch_lod_comparison',
                                              subdir='plots')
                    plt.close(batch_lod_fig)

            return summary_df, figures, results_dict
        else:
            return pd.DataFrame(), {}, {}

    @staticmethod
    def _plot_batch_sensitivity_comparison(summary_df: pd.DataFrame,
                                           figsize: Tuple[int, int] = (8, 5)) -> plt.Figure:
        """
        Create bar chart comparing sensitivity across all scans.

        Args:
            summary_df: DataFrame from batch_analyze with sensitivity_nA_uM column
            figsize: Figure size

        Returns:
            matplotlib Figure object
        """
        # Validate required columns exist
        if 'scan_name' not in summary_df.columns:
            raise ValueError("summary_df must contain 'scan_name' column")
        if 'sensitivity_nA_uM' not in summary_df.columns:
            raise ValueError("summary_df must contain 'sensitivity_nA_uM' column")

        fig, ax = plt.subplots(figsize=figsize)

        scan_names = summary_df['scan_name'].tolist()
        sensitivities = summary_df['sensitivity_nA_uM'].tolist()

        # Truncate long scan names for display
        display_names = [n[:20] + '...' if len(n) > 20 else n for n in scan_names]

        # Use numeric x positions to avoid duplicate label issues
        x_positions = range(len(scan_names))
        bars = ax.bar(x_positions, sensitivities, color='steelblue', width=0.6)

        # Set tick positions and labels
        ax.set_xticks(x_positions)
        ax.set_xticklabels(display_names)

        # Add value labels on bars
        for bar, val in zip(bars, sensitivities):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                   f'{val:.3f}', ha='center', va='bottom', fontsize=9)

        ax.set_ylabel('Sensitivity (nA/µM)', fontsize=11)
        ax.set_title('Sensitivity Comparison Across Scans', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--', axis='y')
        ax.tick_params(axis='x', rotation=45)

        plt.tight_layout()
        return fig

    @staticmethod
    def _plot_batch_lod_comparison(summary_df: pd.DataFrame,
                                    figsize: Tuple[int, int] = (8, 5)) -> plt.Figure:
        """
        Create bar chart comparing LOD/LOQ across all scans.

        Args:
            summary_df: DataFrame from batch_analyze with lod_uM and loq_uM columns
            figsize: Figure size

        Returns:
            matplotlib Figure object
        """
        # Validate required columns exist
        required_cols = ['scan_name', 'lod_uM', 'loq_uM']
        missing = [col for col in required_cols if col not in summary_df.columns]
        if missing:
            raise ValueError(f"summary_df missing required columns: {missing}")

        fig, ax = plt.subplots(figsize=figsize)

        scan_names = summary_df['scan_name'].tolist()
        lods = summary_df['lod_uM'].tolist()
        loqs = summary_df['loq_uM'].tolist()

        # Truncate long scan names for display
        display_names = [n[:15] + '...' if len(n) > 15 else n for n in scan_names]

        x = np.arange(len(display_names))
        width = 0.35

        bars1 = ax.bar(x - width/2, lods, width, label='LOD', color='steelblue')
        bars2 = ax.bar(x + width/2, loqs, width, label='LOQ', color='darkorange')

        ax.set_ylabel('Concentration (µM)', fontsize=11)
        ax.set_title('LOD/LOQ Comparison Across Scans', fontsize=12)
        ax.set_xticks(x)
        ax.set_xticklabels(display_names, rotation=45, ha='right')
        ax.legend(loc='best', fontsize=9)
        ax.grid(True, alpha=0.3, linestyle='--', axis='y')

        plt.tight_layout()
        return fig

    @staticmethod
    def batch_analyze_grouped(groups_dict: Dict[str, List[str]],
                              scans_dict: Dict[str, pd.DataFrame],
                              concentrations_config: Dict[str, List[float]],
                              addition_times_config: Dict[str, List[float]],
                              baseline_window_config: Dict[str, Tuple[float, float]],
                              addition_search_window_s: float = 15.0,
                              electrode_area_um2: float = 2000,
                              linear_range: Tuple[float, float] = (0.5, 50),
                              steady_state_window_s: float = 30,
                              export_manager=None) -> Tuple[pd.DataFrame, Dict]:
        """
        Grouped analysis with mean ± SEM statistics using config dicts.

        Note on Km aggregation: Per-scan Km(app) values are preserved as raw fit
        output (with the existing NaN guards). Before computing group-level
        km_mean / km_std, Km values outside [0, 1e5] uM are masked to NaN to
        exclude unconstrained M-M fits on non-saturating data (which can return
        Km in the 1e9+ uM range). The bound (100 mM) is ~1000x typical
        biosensor Km and leaves headroom for unusual systems.

        Args:
            groups_dict: {group_name: [scan_name1, scan_name2, ...]}
            scans_dict: {scan_name: DataFrame} from load_psession()
            concentrations_config: Per-scan concentration sequences (µM).
                Format: {"Default": [conc1, conc2, ...], "ScanName": [...]}.
            addition_times_config: Per-scan approximate addition times (seconds).
                Format: {"Default": [t1, t2, ...], "ScanName": [...]}.
                Used for guided step detection.
            baseline_window_config: Per-scan baseline windows for noise estimation.
                Format: {"Default": (start_s, end_s), "ScanName": (...)}.
            addition_search_window_s: Search window around each addition time (seconds).
                Default 15s means ±15s around each hint time.
            electrode_area_um2: Electrode area for normalization
            linear_range: Concentration range for linear fit (µM)
            steady_state_window_s: Window for steady-state averaging (seconds)
            export_manager: Optional ExportManager for saving results

        Returns:
            (grouped_summary_df, grouped_figures_dict)
        """
        group_results = []
        figures = {}

        for group_name, scan_names in groups_dict.items():
            logger.info(f"Analyzing group: {group_name}")

            # Filter scans for this group
            group_scans = {name: scans_dict[name] for name in scan_names
                          if name in scans_dict}

            if not group_scans:
                logger.warning(f"No scans found for group {group_name}")
                continue

            # Analyze each scan in the group
            sensitivities = []
            lods = []
            loqs = []
            km_values = []
            response_times = []

            for scan_name, data in group_scans.items():
                try:
                    analyzer = CAAnalyzer(data)

                    # Get per-scan config (falls back to Default)
                    concentrations = _get_scan_config(concentrations_config, scan_name)
                    addition_times = _get_scan_config(addition_times_config, scan_name)
                    baseline_window = _get_scan_config(baseline_window_config, scan_name)

                    if concentrations is None or addition_times is None:
                        logger.warning(f"Skipping {scan_name}: Missing config (no Default or scan-specific)")
                        continue

                    # Use guided step detection with user-provided addition times
                    analyzer.detect_steps_guided(
                        addition_times=addition_times,
                        search_window_s=addition_search_window_s
                    )

                    result = analyzer.analyze_calibration(
                        concentrations,
                        electrode_area_um2=electrode_area_um2,
                        linear_range=linear_range,
                        baseline_window=baseline_window,
                        steady_state_window_s=steady_state_window_s
                    )

                    sensitivities.append(result.get('sensitivity_nA_uM', np.nan))
                    lods.append(result.get('lod_uM', np.nan))
                    loqs.append(result.get('loq_uM', np.nan))
                    km_values.append(result.get('Km_app_uM', np.nan))
                    response_times.append(result.get('mean_response_time_s', np.nan))

                    logger.info(f"  ✓ {scan_name}: Sensitivity={result.get('sensitivity_nA_uM', np.nan):.4f} nA/µM")

                except Exception as e:
                    logger.error(f"  ✗ Failed to analyze {scan_name}: {e}")

            # Calculate group statistics
            if sensitivities:
                clean_sens = [s for s in sensitivities if not np.isnan(s)]
                clean_lods = [l for l in lods if not np.isnan(l)]
                # Filter unconstrained M-M fits before Km aggregation:
                # treat Km > 1e5 uM (= 100 mM) or Km < 0 as NaN. Biosensor Km
                # is typically uM to low-mM; values >>100 mM indicate a
                # runaway curve_fit on non-saturating data (no saturation
                # plateau -> Km, Imax wander to infinity). The raw per-scan
                # value is still reported in batch_analyze's summary_df; only
                # the group aggregation here is filtered.
                km_arr = np.asarray(km_values, dtype=float)
                n_filtered = int((km_arr > KM_MAX_PHYSICAL_UM).sum() + (km_arr < 0).sum())
                if n_filtered:
                    logger.warning(
                        f"Filtered {n_filtered} of {len(km_arr)} M-M Km(app) values "
                        f"outside [0, {KM_MAX_PHYSICAL_UM:g}] uM before aggregation (likely unconstrained fits)."
                    )
                km_filtered = np.where(
                    np.isnan(km_arr) | (km_arr > KM_MAX_PHYSICAL_UM) | (km_arr < 0),
                    np.nan,
                    km_arr,
                )
                clean_kms = km_filtered[~np.isnan(km_filtered)]
                clean_rts = [r for r in response_times if not np.isnan(r)]
                group_summary = {
                    'group_name': group_name,
                    'n_scans': len(sensitivities),
                    'sensitivity_mean': np.nanmean(sensitivities),
                    'sensitivity_std': np.std(clean_sens, ddof=1) if len(clean_sens) > 1 else 0,
                    'lod_mean': np.nanmean(lods),
                    'lod_std': np.std(clean_lods, ddof=1) if len(clean_lods) > 1 else 0,
                    'loq_mean': np.nanmean(loqs),
                    'km_mean': np.nanmean(km_filtered),
                    'km_std': np.std(clean_kms, ddof=1) if len(clean_kms) > 1 else 0,
                    'response_time_mean': np.nanmean(response_times),
                    'response_time_std': np.std(clean_rts, ddof=1) if len(clean_rts) > 1 else 0,
                }
                group_results.append(group_summary)

                logger.info(f"  Group stats: Sensitivity = {group_summary['sensitivity_mean']:.4f} ± "
                            f"{group_summary['sensitivity_std']:.4f} nA/µM")

        # Create grouped summary DataFrame
        if group_results:
            summary_df = pd.DataFrame(group_results)

            if export_manager:
                export_manager.save_dataframe(summary_df, 'grouped_ca_results',
                                             subdir='data')

            return summary_df, figures
        else:
            return pd.DataFrame(), {}

    @staticmethod
    def plot_mm_semilog_grouped(results_dict: Dict[str, dict],
                                grouping: Dict[str, List[str]] = None,
                                show_data_points: bool = True,
                                figsize: Tuple[int, int] = (8, 6),
                                export_manager=None) -> plt.Figure:
        """
        Overlay normalized M-M semilog curves for multiple sensors/groups.

        Creates a comparison plot showing normalized Michaelis-Menten response
        (I/Imax) vs log concentration for multiple sensors, grouped by category.

        Args:
            results_dict: {scan_name: results_dict} from batch analysis
                         Each results_dict must contain 'mm_params' with
                         'imax', 'km_app', and calibration data
            grouping: {group_name: [scan_names]} for color grouping
                     If None, all sensors plotted with same color
            show_data_points: Whether to show individual data points
            figsize: Figure size tuple
            export_manager: Optional ExportManager for saving

        Returns:
            matplotlib Figure with overlaid normalized sigmoids
        """
        fig, ax = plt.subplots(figsize=figsize)

        # Color map for groups
        colors = plt.cm.tab10.colors

        # If no grouping provided, treat all as one group
        if grouping is None:
            grouping = {'All Sensors': list(results_dict.keys())}

        # Track Km values for each group (for optional statistics)
        group_km_values = {group: [] for group in grouping}

        for group_idx, (group_name, scan_names) in enumerate(grouping.items()):
            color = colors[group_idx % len(colors)]
            valid_count = 0

            for scan_name in scan_names:
                if scan_name not in results_dict:
                    continue

                results = results_dict[scan_name]

                # Check for required M-M parameters (top-level keys from analyze_calibration)
                imax = results.get('Imax_nA', np.nan)
                km = results.get('Km_app_uM', np.nan)

                if np.isnan(imax) or np.isnan(km) or imax <= 0 or km <= 0:
                    continue

                group_km_values[group_name].append(km)
                valid_count += 1

                # Get calibration data for data points
                if show_data_points and 'calibration_data' in results:
                    cal_data = results['calibration_data']
                    if 'concentrations' in cal_data and 'currents' in cal_data:
                        conc = np.array(cal_data['concentrations'])
                        curr = np.array(cal_data['currents'])
                        # Normalize and plot data points
                        norm_curr = curr / imax
                        ax.scatter(conc, norm_curr, color=color, alpha=0.3,
                                  s=20, zorder=2)

                # Plot normalized M-M curve
                conc_range = np.logspace(-1, 5, 200)  # 0.1 to 100,000 µM
                norm_response = conc_range / (km + conc_range)
                ax.plot(conc_range, norm_response, color=color, alpha=0.5,
                       linewidth=1, zorder=1)

            # Add group to legend with count
            if valid_count > 0:
                ax.plot([], [], color=color, linewidth=2,
                       label=f'{group_name} (n={valid_count})')

        # Add reference lines
        ax.axhline(y=0.5, color='gray', linestyle='--', linewidth=0.8,
                  alpha=0.7, label='I/Imax = 0.5')

        # Formatting
        ax.set_xscale('log')
        ax.set_xlabel('Concentration (µM)', fontsize=11)
        ax.set_ylabel('Normalized Response (I/Imax)', fontsize=11)
        ax.set_ylim(-0.05, 1.1)
        ax.set_title('Michaelis-Menten Comparison (Semilog)', fontsize=12)
        ax.legend(loc='lower right', fontsize=9)
        ax.grid(True, alpha=0.3, which='both')

        # Add Km statistics annotation if multiple groups.
        # Apply the same Km filter as batch_analyze_grouped: drop values
        # outside [0, 1e5] uM (= 100 mM) before averaging, so unconstrained
        # M-M fits don't produce nonsense annotations like "Km = 3.9e9 uM".
        if len(grouping) > 1:
            stats_text = []
            for group_name, km_values in group_km_values.items():
                if len(km_values) > 0:
                    km_arr = np.asarray(km_values, dtype=float)
                    n_filtered = int((km_arr > KM_MAX_PHYSICAL_UM).sum() + (km_arr < 0).sum())
                    if n_filtered:
                        logger.warning(
                            f"plot_mm_semilog_grouped[{group_name}]: "
                            f"Filtered {n_filtered} of {len(km_arr)} M-M Km(app) "
                            f"values outside [0, {KM_MAX_PHYSICAL_UM:g}] uM before aggregation "
                            f"(likely unconstrained fits)."
                        )
                    km_arr = km_arr[(km_arr >= 0) & (km_arr <= KM_MAX_PHYSICAL_UM)]
                    if len(km_arr) == 0:
                        continue
                    mean_km = np.mean(km_arr)
                    if len(km_arr) > 1:
                        std_km = np.std(km_arr, ddof=1)
                        stats_text.append(f'{group_name}: Km = {mean_km:.0f} ± {std_km:.0f} µM')
                    else:
                        stats_text.append(f'{group_name}: Km = {mean_km:.0f} µM')

            if stats_text:
                ax.text(0.02, 0.98, '\n'.join(stats_text),
                       transform=ax.transAxes, fontsize=8,
                       verticalalignment='top',
                       bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

        plt.tight_layout()

        if export_manager:
            export_manager.save_figure(fig, 'mm_semilog_grouped', subdir='plots')

        return fig

    @staticmethod
    def plot_sensitivity_grouped(results_dict: Dict[str, dict],
                                 grouping: Dict[str, List[str]] = None,
                                 show_fit_lines: bool = True,
                                 min_r_squared: float = 0.0,
                                 figsize: Tuple[int, int] = (8, 6),
                                 xlim: Tuple[float, float] = None,
                                 ylim: Tuple[float, float] = None,
                                 export_manager=None) -> plt.Figure:
        """
        Overlay linear sensitivity plots for multiple sensors/groups.

        Creates a comparison plot showing calibration data and linear fits
        for multiple sensors, grouped by category.

        Args:
            results_dict: {scan_name: results_dict} from batch analysis
                         Each results_dict must contain 'sensitivity_nA_uM',
                         'linear_intercept_nA', 'linear_r_squared', and
                         calibration data
            grouping: {group_name: [scan_names]} for color grouping
                     If None, all sensors plotted with same color
            show_fit_lines: Whether to show linear fit lines
            min_r_squared: Minimum R² threshold for including a sensor (default 0.0)
            figsize: Figure size tuple
            xlim: Optional tuple (xmin, xmax) for x-axis limits
            ylim: Optional tuple (ymin, ymax) for y-axis limits
            export_manager: Optional ExportManager for saving

        Returns:
            matplotlib Figure with overlaid sensitivity plots
        """
        fig, ax = plt.subplots(figsize=figsize)

        # Color map for groups
        colors = plt.cm.tab10.colors

        # If no grouping provided, treat all as one group
        if grouping is None:
            grouping = {'All Sensors': list(results_dict.keys())}

        # Track sensitivity values for each group (for statistics)
        group_sensitivity_values = {group: [] for group in grouping}
        max_conc = 0  # Track max concentration for fit line extent

        # Store fit line parameters for second pass (to use final max_conc)
        fit_lines_to_plot = []  # List of (sensitivity, intercept, color)

        for group_idx, (group_name, scan_names) in enumerate(grouping.items()):
            color = colors[group_idx % len(colors)]
            valid_count = 0

            for scan_name in scan_names:
                if scan_name not in results_dict:
                    continue

                results = results_dict[scan_name]

                # Check for required linear fit parameters
                sensitivity = results.get('sensitivity_nA_uM', np.nan)
                intercept = results.get('linear_intercept_nA', np.nan)
                r_squared = results.get('linear_r_squared', np.nan)

                if np.isnan(sensitivity) or np.isnan(intercept):
                    continue

                # Filter by R² threshold if specified
                if not np.isnan(r_squared) and r_squared < min_r_squared:
                    continue

                group_sensitivity_values[group_name].append(sensitivity)
                valid_count += 1

                # Get calibration data for data points
                if 'calibration_data' in results:
                    cal_data = results['calibration_data']
                    if 'concentrations' in cal_data and 'currents' in cal_data:
                        conc = np.array(cal_data['concentrations'])
                        curr = np.array(cal_data['currents'])
                        # Plot data points
                        ax.scatter(conc, curr, color=color, alpha=0.4,
                                  s=30, zorder=2)
                        max_conc = max(max_conc, conc.max())

                # Store fit line parameters for later (after max_conc is finalized)
                if show_fit_lines:
                    fit_lines_to_plot.append((sensitivity, intercept, color))

            # Add group to legend with count
            if valid_count > 0:
                ax.plot([], [], color=color, linewidth=2, marker='o', markersize=5,
                       label=f'{group_name} (n={valid_count})')

        # Second pass: plot all fit lines with consistent x-range
        if show_fit_lines and fit_lines_to_plot:
            fit_conc = np.linspace(0, max_conc * 1.1 if max_conc > 0 else 1000, 100)
            for sensitivity, intercept, color in fit_lines_to_plot:
                fit_curr = sensitivity * fit_conc + intercept
                ax.plot(fit_conc, fit_curr, color=color, alpha=0.5,
                       linewidth=1, zorder=1)

        # Formatting
        ax.set_xlabel('Concentration (µM)', fontsize=11)
        ax.set_ylabel('Current (nA)', fontsize=11)
        ax.set_title('Sensitivity Comparison (Linear Region)', fontsize=12)
        ax.legend(loc='upper left', fontsize=9)
        ax.grid(True, alpha=0.3, linestyle='--')

        # Apply axis limits if specified
        if xlim is not None:
            ax.set_xlim(xlim)
        if ylim is not None:
            ax.set_ylim(ylim)

        # Add sensitivity statistics annotation
        stats_text = []
        for group_name, sens_values in group_sensitivity_values.items():
            if len(sens_values) > 0:
                mean_sens = np.mean(sens_values)
                if len(sens_values) > 1:
                    std_sens = np.std(sens_values, ddof=1)
                    stats_text.append(f'{group_name}: {mean_sens:.2f} ± {std_sens:.2f} nA/µM')
                else:
                    stats_text.append(f'{group_name}: {mean_sens:.2f} nA/µM')

        if stats_text:
            ax.text(0.02, 0.98, '\n'.join(stats_text),
                   transform=ax.transAxes, fontsize=8,
                   verticalalignment='top',
                   bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

        plt.tight_layout()

        if export_manager:
            export_manager.save_figure(fig, 'sensitivity_grouped', subdir='plots')

        return fig

    @staticmethod
    def plot_selectivity_comparison(
        analyte_results: Dict[str, Dict[str, dict]],
        target_analyte: str = None,
        metric: str = 'sensitivity',
        grouping: Dict[str, List[str]] = None,
        figsize: Tuple[int, int] = (10, 6),
        export_manager=None
    ) -> plt.Figure:
        """
        Bar chart comparing specificity constants across analytes.

        Creates a grouped bar chart comparing sensor response (sensitivity or
        specificity constant) across different analytes, with optional electrode
        grouping and selectivity ratio annotations.

        Args:
            analyte_results: {analyte_name: {scan_name: results_dict}}
                Example: {"H2O2": {"S001": results, "S002": results},
                          "AA": {"S001": results, "S002": results}}
            target_analyte: Primary analyte for selectivity ratios (default: first key)
            metric: 'sensitivity' (nA/µM), 'k_spec' (Imax/Km), or 'both'
            grouping: Optional {group_name: [scan_names]} for electrode grouping
            figsize: Figure size
            export_manager: Optional ExportManager

        Returns:
            matplotlib Figure with grouped bar chart

        Example:
            >>> # After running batch_analyze for each analyte
            >>> analyte_results = {
            ...     "H2O2": h2o2_results,
            ...     "AA": aa_results,
            ...     "DA": da_results
            ... }
            >>> fig = CAAnalyzer.plot_selectivity_comparison(
            ...     analyte_results,
            ...     target_analyte="H2O2",
            ...     metric='sensitivity'
            ... )
        """
        if not analyte_results:
            raise ValueError("analyte_results cannot be empty")

        analyte_names = list(analyte_results.keys())
        if target_analyte is None:
            target_analyte = analyte_names[0]

        if target_analyte not in analyte_names:
            raise ValueError(f"target_analyte '{target_analyte}' not in analyte_results")

        # Collect all scan names across analytes
        all_scans = set()
        for analyte_data in analyte_results.values():
            all_scans.update(analyte_data.keys())

        # If no grouping, treat all scans as one group
        if grouping is None:
            grouping = {'All Sensors': list(all_scans)}

        # Extract metric values for each group and analyte
        group_names = list(grouping.keys())
        n_groups = len(group_names)
        n_analytes = len(analyte_names)

        # Data structure: {group: {analyte: [values]}}
        data = {g: {a: [] for a in analyte_names} for g in group_names}

        for group_name, scan_names in grouping.items():
            for analyte, analyte_data in analyte_results.items():
                for scan_name in scan_names:
                    if scan_name not in analyte_data:
                        continue

                    results = analyte_data[scan_name]

                    if metric == 'sensitivity':
                        value = results.get('sensitivity_nA_uM', np.nan)
                    elif metric == 'k_spec':
                        imax = results.get('Imax_nA', np.nan)
                        km = results.get('Km_app_uM', np.nan)
                        if imax > 0 and km > 0:
                            value = imax / km
                        else:
                            value = np.nan
                    else:
                        value = results.get('sensitivity_nA_uM', np.nan)

                    if not np.isnan(value):
                        data[group_name][analyte].append(abs(value))

        # Calculate means and SDs
        means = {g: {} for g in group_names}
        stds = {g: {} for g in group_names}

        for group_name in group_names:
            for analyte in analyte_names:
                values = data[group_name][analyte]
                if len(values) > 0:
                    means[group_name][analyte] = np.mean(values)
                    if len(values) > 1:
                        stds[group_name][analyte] = np.std(values, ddof=1)
                    else:
                        stds[group_name][analyte] = 0
                else:
                    means[group_name][analyte] = 0
                    stds[group_name][analyte] = 0

        # Create plot
        fig, ax = plt.subplots(figsize=figsize)

        # Bar positions
        x = np.arange(n_analytes)
        width = 0.8 / n_groups if n_groups > 1 else 0.6
        offsets = np.linspace(-(n_groups-1)*width/2, (n_groups-1)*width/2, n_groups) if n_groups > 1 else [0]

        colors = plt.cm.tab10.colors

        for i, group_name in enumerate(group_names):
            group_means = [means[group_name].get(a, 0) for a in analyte_names]
            group_stds = [stds[group_name].get(a, 0) for a in analyte_names]
            n_samples = [len(data[group_name][a]) for a in analyte_names]

            bars = ax.bar(x + offsets[i], group_means, width,
                         yerr=group_stds, capsize=3,
                         label=f'{group_name} (n={max(n_samples)})',
                         color=colors[i % len(colors)], alpha=0.8)

        # Add selectivity ratio annotations
        target_idx = analyte_names.index(target_analyte)
        for group_name in group_names:
            target_mean = means[group_name].get(target_analyte, 0)
            if target_mean > 0:
                for j, analyte in enumerate(analyte_names):
                    if analyte != target_analyte:
                        analyte_mean = means[group_name].get(analyte, 0)
                        if analyte_mean > 0:
                            ratio = target_mean / analyte_mean
                            # Annotate above bars
                            y_pos = means[group_name][analyte] + sems[group_name][analyte]
                            if n_groups == 1:
                                ax.annotate(f'{ratio:.0f}×',
                                           xy=(j, y_pos),
                                           ha='center', va='bottom',
                                           fontsize=8, color='gray')

        # Formatting
        ax.set_xticks(x)
        ax.set_xticklabels(analyte_names, fontsize=10)
        ax.set_xlabel('Analyte', fontsize=11)

        if metric == 'sensitivity':
            ylabel = 'Sensitivity (nA/µM)'
        elif metric == 'k_spec':
            ylabel = 'Specificity Constant k_spec (nA/µM)'
        else:
            ylabel = 'Response'

        ax.set_ylabel(ylabel, fontsize=11)
        ax.set_title(f'Selectivity Comparison (vs {target_analyte})', fontsize=12)
        ax.legend(loc='upper right', fontsize=9)
        ax.grid(True, alpha=0.3, axis='y')

        # Start y-axis at 0
        ax.set_ylim(bottom=0)

        plt.tight_layout()

        if export_manager:
            export_manager.save_figure(fig, 'selectivity_comparison', subdir='plots')

        return fig

    @staticmethod
    def plot_sensitivity_pooled(results_dict: Dict[str, dict],
                                grouping: Dict[str, List[str]],
                                electrode_area_um2: float = 2000,
                                linear_range: Tuple[float, float] = None,
                                figsize: Tuple[int, int] = (8, 6),
                                xlim: Tuple[float, float] = None,
                                ylim: Tuple[float, float] = None,
                                export_manager=None) -> plt.Figure:
        """
        Pooled sensitivity plot with linear fits per group (Chu et al. style).

        Pools calibration data from all scans within each group, normalizes
        by electrode area, and fits a single linear regression per group.
        Distinct markers and colors per group for publication-ready output.

        Args:
            results_dict: {scan_name: results_dict} from batch_analyze().
                         Each entry must contain 'calibration_data' with
                         'concentrations' and 'currents' (nA, baseline-corrected).
            grouping: {group_name: [scan_name1, scan_name2, ...]}
            electrode_area_um2: Electrode geometric area in µm² (global).
                               Converted to mm² for normalization.
            linear_range: Optional (min, max) concentration range (µM) for
                         the linear fit. If None, fits all data.
            figsize: Figure size tuple
            xlim: Optional (xmin, xmax) for x-axis
            ylim: Optional (ymin, ymax) for y-axis
            export_manager: Optional ExportManager for saving

        Returns:
            matplotlib Figure with pooled sensitivity comparison
        """
        fig, ax = plt.subplots(figsize=figsize)

        # Marker/color pairs cycling per group
        styles = [
            ('s', 'black'), ('o', 'red'), ('^', 'blue'),
            ('v', 'magenta'), ('D', 'green'), ('p', 'orange'),
            ('h', 'cyan'), ('*', 'brown'),
        ]

        area_mm2 = electrode_area_um2 / 1e6
        max_conc = 0

        # First pass: collect data and plot scatter
        group_data = {}  # {group_name: (all_conc, all_curr_density)}

        for group_idx, (group_name, scan_names) in enumerate(grouping.items()):
            marker, color = styles[group_idx % len(styles)]

            all_conc = []
            all_curr = []
            all_curr_raw = []
            all_names = []

            for scan_name in scan_names:
                if scan_name not in results_dict:
                    continue

                results = results_dict[scan_name]

                # Prefer top-level keys (guaranteed same length from
                # analyze_calibration). Fall back to calibration_data.
                conc = results.get('concentrations_uM', None)
                curr = results.get('currents_nA', None)

                if conc is None or curr is None:
                    cal_data = results.get('calibration_data', {})
                    conc = cal_data.get('concentrations', [])
                    curr = cal_data.get('currents', [])

                if not len(conc) or not len(curr):
                    continue

                conc = np.array(conc, dtype=float)
                curr = np.array(curr, dtype=float)

                # Truncate to matching length (safety)
                n = min(len(conc), len(curr))
                conc = conc[:n]
                curr = curr[:n]

                # Area-normalize: nA / mm² → nA·mm⁻²
                curr_density = curr / area_mm2

                all_conc.extend(conc)
                all_curr.extend(curr_density)
                all_curr_raw.extend(curr)
                all_names.extend([scan_name] * n)

            if not all_conc:
                continue

            all_conc = np.array(all_conc)
            all_curr = np.array(all_curr)
            all_curr_raw = np.array(all_curr_raw)
            group_data[group_name] = (all_conc, all_curr, all_curr_raw,
                                      all_names, marker, color)
            max_conc = max(max_conc, all_conc.max())

            # Plot data points
            ax.scatter(all_conc, all_curr, marker=marker, color=color,
                      s=40, edgecolors='black', linewidths=0.5,
                      label=group_name, zorder=3)

        # Second pass: fit and plot lines (uses final max_conc)
        fit_x = np.linspace(0, max_conc * 1.05, 200) if max_conc > 0 else np.array([0, 1])
        fit_summaries = []

        for group_name, (all_conc, all_curr, all_curr_raw,
                         all_names, marker, color) in group_data.items():
            # Apply linear range filter for fit
            if linear_range is not None:
                fit_mask = (all_conc >= linear_range[0]) & (all_conc <= linear_range[1])
            else:
                fit_mask = np.ones(len(all_conc), dtype=bool)

            if np.sum(fit_mask) < 2:
                continue

            fit_result = linregress(all_conc[fit_mask], all_curr[fit_mask])
            fit_y = fit_result.slope * fit_x + fit_result.intercept
            ax.plot(fit_x, fit_y, color=color, linewidth=1.5, zorder=2)

            fit_summaries.append({
                'group': group_name,
                'slope_nA_mm2_per_uM': fit_result.slope,
                'intercept_nA_mm2': fit_result.intercept,
                'r_squared': fit_result.rvalue ** 2,
                'std_err': fit_result.stderr,
                'n_points_fit': int(np.sum(fit_mask)),
                'n_points_total': len(all_conc),
            })

        # Formatting (publication style)
        ax.set_xlabel('Concentration / µM', fontsize=11)
        ax.set_ylabel('Normalized Current Increase / nA·mm⁻²', fontsize=11)
        ax.legend(loc='upper left', fontsize=9, framealpha=0.9)
        ax.tick_params(direction='in', which='both')

        if xlim is not None:
            ax.set_xlim(xlim)
        if ylim is not None:
            ax.set_ylim(ylim)

            # Warn if ylim clips all data points (common misconfiguration)
            if group_data:
                all_y = np.concatenate([d[1] for d in group_data.values()])
                if len(all_y) > 0 and np.min(all_y) > ylim[1]:
                    logger.warning(
                        f"ylim upper bound ({ylim[1]:.0f}) is below the minimum "
                        f"data value ({np.min(all_y):.0f} nA·mm⁻²). "
                        f"Plot will appear empty. Check electrode_area_um2."
                    )

        plt.tight_layout()

        if export_manager:
            export_manager.save_figure(fig, 'sensitivity_pooled', subdir='plots')

            # Export pooled data points (per-sensor detail)
            pooled_rows = []
            for group_name, (all_conc, all_curr, all_curr_raw,
                             all_names, _, _) in group_data.items():
                for name, c, raw, j in zip(all_names, all_conc,
                                           all_curr_raw, all_curr):
                    pooled_rows.append({
                        'group': group_name,
                        'scan_name': name,
                        'concentration_uM': c,
                        'current_nA': raw,
                        'current_density_nA_mm2': j,
                    })
            if pooled_rows:
                pooled_df = pd.DataFrame(pooled_rows)
                export_manager.save_dataframe(
                    pooled_df, 'sensitivity_pooled_data', subdir='data',
                    index=False)

            # Export fit summary
            if fit_summaries:
                fit_df = pd.DataFrame(fit_summaries)
                export_manager.save_dataframe(
                    fit_df, 'sensitivity_pooled_fits', subdir='data',
                    index=False)

        return fig

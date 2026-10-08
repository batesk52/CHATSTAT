"""
CV (Cyclic Voltammetry) analysis module.

This module provides tools for analyzing cyclic voltammetry data, including:
- Charge Storage Capacity (CSC) calculation
- Peak current extraction
- CV visualization with cathodic region highlighting
- Grouped analysis with averaging and error bars
"""

from typing import Dict, List, Tuple, Optional, Any
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy.integrate import trapezoid
import re
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.vendor.electrochem_analysis.utils.grouping import (
    validate_grouping,
    calculate_mean_std,
    get_group_data,
    create_group_summary_df,
    format_error_display,
    check_cv_voltage_alignment,
    interpolate_cv_to_common_index
)

# Randles-Sevcik analysis constants (T = 298 K)
# ip = RANDLES_SEVCIK_CONSTANT * n^1.5 * A * sqrt(D) * C * sqrt(v)
#   ip [A], A [cm^2], D [cm^2/s], C [mol/cm^3], v [V/s]
RANDLES_SEVCIK_CONSTANT = 2.69e5
# Diffusion coefficient of ferricyanide [Fe(CN)6]3- in aqueous KCl near 298 K
DEFAULT_DIFFUSION_COEFF_CM2_S = 7.6e-6
# Ideal peak separation for a reversible 1-electron couple at 298 K (mV)
REVERSIBLE_DELTA_EP_MV = 59.0


class CVAnalyzer:
    """
    Analyzer for Cyclic Voltammetry data.

    Handles:
    - CSC calculation from cathodic region
    - Peak current identification
    - CV plotting with various visualizations
    - Batch processing of multiple CV scans
    """

    def __init__(self, data: pd.DataFrame):
        """
        Initialize CV analyzer with data.

        Args:
            data: DataFrame with columns ['Potential (V)', 'Current (A)']

        Raises:
            ValueError: If required columns are missing
        """
        required_cols = ['Potential (V)', 'Current (A)']
        if not all(col in data.columns for col in required_cols):
            missing = [col for col in required_cols if col not in data.columns]
            raise ValueError(f"Missing required columns: {missing}")

        self.data = data.copy()
        self.csc = None
        self.loop_status = None  # 'closed', 'open', or None
        self.gap_voltage = None  # Voltage difference at endpoints
        self.v_common = None  # Common voltage grid for visualization
        self.i_forward_interp = None  # Interpolated forward scan
        self.i_backward_interp = None  # Interpolated backward scan
        self.peaks = None  # Dict of redox peak metrics (set by extract_peaks)

    def calculate_csc(self, scan_rate: float, electrode_area: float) -> float:
        """
        Calculate Charge Storage Capacity (CSC) from CV data using area-between-curves method.

        CSC is the area between the forward and reverse scans in the cathodic region,
        normalized by scan rate and electrode area.

        Args:
            scan_rate: Scan rate in V/s
            electrode_area: Electrode area in cm²

        Returns:
            CSC in mC/cm²

        Algorithm:
            1. Split data into forward (first half) and reverse (second half) scans
            2. Find voltage overlap region where both scans exist
            3. Interpolate both scans to common voltage grid (1000 points)
            4. Calculate area between curves (cathodic clipping only)
            5. Detect gap at endpoints (open vs. closed loop)

        Note:
            Sets instance variables:
            - self.loop_status: 'closed' or 'open'
            - self.gap_voltage: voltage difference at endpoints
            - self.v_common, self.i_forward_interp, self.i_backward_interp: for visualization
        """
        if scan_rate <= 0:
            raise ValueError(f"scan_rate must be positive, got {scan_rate}")
        if electrode_area <= 0:
            raise ValueError(f"electrode_area must be positive, got {electrode_area}")

        n_points = len(self.data)

        if n_points < 4:
            raise ValueError("Not enough data points for CSC calculation (need at least 4)")

        # Step 1-3: For visualization, still create interpolated scans
        # Detect turning points
        voltage = self.data['Potential (V)'].values
        dv = np.diff(voltage)
        sign_dv = np.sign(dv)
        sign_changes = np.where(np.diff(sign_dv) != 0)[0] + 1

        if len(sign_changes) >= 2:
            first_turning = sign_changes[0] + 1
            second_turning = sign_changes[1] + 1
            forward_data = self.data.iloc[first_turning:second_turning].copy()
            reverse_data = self.data.iloc[second_turning:].copy()
        elif len(sign_changes) == 1:
            split_idx = sign_changes[0] + 1
            forward_data = self.data.iloc[:split_idx].copy()
            reverse_data = self.data.iloc[split_idx:].copy()
        else:
            split_idx = n_points // 2
            forward_data = self.data.iloc[:split_idx].copy()
            reverse_data = self.data.iloc[split_idx:].copy()

        # Sort and interpolate for visualization
        forward_data = forward_data.sort_values('Potential (V)', ascending=True)
        reverse_data = reverse_data.sort_values('Potential (V)', ascending=True)

        v_min = max(forward_data['Potential (V)'].min(), reverse_data['Potential (V)'].min())
        v_max = min(forward_data['Potential (V)'].max(), reverse_data['Potential (V)'].max())

        if v_min < v_max:
            self.v_common = np.linspace(v_min, v_max, 1000)
            self.i_forward_interp = np.interp(
                self.v_common,
                forward_data['Potential (V)'].values,
                forward_data['Current (A)'].values
            )
            self.i_backward_interp = np.interp(
                self.v_common,
                reverse_data['Potential (V)'].values,
                reverse_data['Current (A)'].values
            )
        else:
            # No overlap between forward and reverse scans - use full range
            self.v_common = None
            self.i_forward_interp = None
            self.i_backward_interp = None

        # Step 4: Calculate CSC from area between forward and reverse scans
        # This matches the shaded visualization region exactly
        # Check if interpolation was successful
        if self.i_forward_interp is None or self.i_backward_interp is None:
            raise ValueError("Unable to calculate CSC: forward/reverse scan interpolation failed. "
                           "Data may not have proper CV structure.")

        # Clip both curves at zero (keep only cathodic portions)
        i_forward_clipped = np.minimum(self.i_forward_interp, 0)
        i_reverse_clipped = np.minimum(self.i_backward_interp, 0)

        # Calculate the unsigned area between the clipped curves
        # Using abs() ensures correct result regardless of forward/reverse scan ordering
        area_diff = np.abs(i_forward_clipped - i_reverse_clipped)
        charge = abs(trapezoid(area_diff, self.v_common))

        # Step 5: Detect gap at endpoints (open vs. closed loop)
        v_start = self.data['Potential (V)'].iloc[0]
        v_end = self.data['Potential (V)'].iloc[-1]
        self.gap_voltage = abs(v_end - v_start)

        # Threshold for "closed" loop: gap < 10% of voltage range
        v_range = self.data['Potential (V)'].max() - self.data['Potential (V)'].min()
        gap_threshold = 0.1 * v_range

        self.loop_status = 'closed' if self.gap_voltage < gap_threshold else 'open'

        # Normalize by scan rate and electrode area, convert to mC/cm²
        self.csc = (charge / scan_rate / electrode_area) * 1000

        return self.csc

    def extract_peaks(self, endpoint_guard_fraction: float = 0.05) -> Dict[str, Any]:
        """
        Extract anodic/cathodic redox peaks and reversibility metrics.

        The anodic peak is the maximum current on the positive-going (oxidation)
        sweep; the cathodic peak is the minimum current on the negative-going
        (reduction) sweep. Peak separation (delta_ep) and the peak-current ratio
        are the standard diagnostics of electron-transfer reversibility, and are
        independent of absolute electrode area -- useful for judging whether a
        high-impedance interface is clean (fast, reversible kinetics) or
        passivated (suppressed / drawn-out peaks).

        Args:
            endpoint_guard_fraction: A peak whose index falls within this
                fraction of the start/end of its own sweep is flagged as
                sitting at a sweep endpoint (i.e. a rising background, not a
                true faradaic peak). Default 0.05 (5%).

        Returns:
            Dict with keys:
                epa_v, ipa_a            anodic peak potential (V) / current (A)
                epc_v, ipc_a            cathodic peak potential (V) / current (A)
                delta_ep_mv             |Epa - Epc| in mV
                e_half_v                (Epa + Epc) / 2 in V
                ipa_ipc_ratio           |ipa / ipc| (ideal ~1 for reversible)
                peaks_well_defined      False if either peak sits at a sweep
                                        endpoint (suggests no faradaic peak)
                reversible              True if delta_ep_mv is within
                                        ~2x the 59 mV ideal AND peaks well defined

        Raises:
            ValueError: If fewer than 4 data points are present.

        Note:
            Peak currents are raw extrema (no baseline subtraction). For an
            area calculation that needs baseline-corrected ip, subtract a
            local background before passing currents to
            calculate_area_from_scan_rates. delta_ep / ratio / e_half are
            unaffected by a flat baseline offset.
        """
        if len(self.data) < 4:
            raise ValueError("Not enough data points for peak extraction (need at least 4)")

        voltage = self.data['Potential (V)'].values
        current = self.data['Current (A)'].values

        # Sweep direction at each point (sign of dV); gradient keeps array length
        dv = np.gradient(voltage)
        anodic_mask = dv > 0   # potential increasing -> oxidation sweep
        cathodic_mask = dv < 0  # potential decreasing -> reduction sweep

        if not anodic_mask.any() or not cathodic_mask.any():
            raise ValueError(
                "Could not segment forward/reverse sweeps -- data may not be a CV"
            )

        anodic_idx = np.where(anodic_mask)[0]
        cathodic_idx = np.where(cathodic_mask)[0]

        # Anodic peak = max current on the oxidation sweep
        ia_local = int(np.argmax(current[anodic_idx]))
        ipa_idx = int(anodic_idx[ia_local])
        # Cathodic peak = min current on the reduction sweep
        ic_local = int(np.argmin(current[cathodic_idx]))
        ipc_idx = int(cathodic_idx[ic_local])

        epa = float(voltage[ipa_idx])
        ipa = float(current[ipa_idx])
        epc = float(voltage[ipc_idx])
        ipc = float(current[ipc_idx])

        delta_ep_mv = abs(epa - epc) * 1000.0
        e_half = (epa + epc) / 2.0
        ipa_ipc_ratio = abs(ipa / ipc) if ipc != 0 else float('inf')

        # Flag peaks that sit at a sweep endpoint (rising background, not a peak)
        guard_a = max(1, int(len(anodic_idx) * endpoint_guard_fraction))
        guard_c = max(1, int(len(cathodic_idx) * endpoint_guard_fraction))
        anodic_at_end = ia_local < guard_a or ia_local > len(anodic_idx) - guard_a - 1
        cathodic_at_end = ic_local < guard_c or ic_local > len(cathodic_idx) - guard_c - 1
        peaks_well_defined = not (anodic_at_end or cathodic_at_end)

        reversible = (
            peaks_well_defined
            and delta_ep_mv <= 2.0 * REVERSIBLE_DELTA_EP_MV
            and 0.5 <= ipa_ipc_ratio <= 2.0
        )

        self.peaks = {
            'epa_v': epa,
            'ipa_a': ipa,
            'epc_v': epc,
            'ipc_a': ipc,
            'delta_ep_mv': float(delta_ep_mv),
            'e_half_v': float(e_half),
            'ipa_ipc_ratio': float(ipa_ipc_ratio),
            'peaks_well_defined': bool(peaks_well_defined),
            'reversible': bool(reversible),
        }
        return self.peaks

    def calculate_electroactive_area(
        self,
        scan_rate: float,
        concentration_mM: float,
        n: int = 1,
        diffusion_coeff: float = DEFAULT_DIFFUSION_COEFF_CM2_S,
        peak: str = 'anodic',
    ) -> float:
        """
        Estimate electroactive area from a single CV via the Randles-Sevcik
        equation: ip = 2.69e5 * n^1.5 * A * sqrt(D) * C * sqrt(v).

        This is the SINGLE-scan-rate estimate. The multi-rate regression
        (calculate_area_from_scan_rates) is more robust because it rejects a
        constant background; prefer it when scan-rate-series data is available.

        Args:
            scan_rate: Scan rate in V/s (> 0).
            concentration_mM: Redox-probe bulk concentration in mM
                (e.g. 5.0 for 5 mM ferri/ferrocyanide).
            n: Number of electrons transferred (default 1).
            diffusion_coeff: D in cm^2/s (default ferricyanide ~7.6e-6).
            peak: Which peak current to use -- 'anodic', 'cathodic', or 'mean'.

        Returns:
            Electroactive area in cm^2.

        Raises:
            ValueError: On non-positive scan_rate / concentration, unknown
                peak selection, or if peaks have not been extracted and cannot
                be derived from the data.
        """
        if scan_rate <= 0:
            raise ValueError(f"scan_rate must be positive, got {scan_rate}")
        if concentration_mM <= 0:
            raise ValueError(f"concentration_mM must be positive, got {concentration_mM}")
        if peak not in ('anodic', 'cathodic', 'mean'):
            raise ValueError(f"peak must be 'anodic', 'cathodic', or 'mean', got '{peak}'")

        if self.peaks is None:
            self.extract_peaks()

        ipa = abs(self.peaks['ipa_a'])
        ipc = abs(self.peaks['ipc_a'])
        if peak == 'anodic':
            ip = ipa
        elif peak == 'cathodic':
            ip = ipc
        else:
            ip = 0.5 * (ipa + ipc)

        # Concentration mM -> mol/cm^3:  mM = mmol/L = 1e-3 mol/L = 1e-6 mol/cm^3
        c_mol_cm3 = concentration_mM * 1e-6
        denom = (
            RANDLES_SEVCIK_CONSTANT
            * (n ** 1.5)
            * np.sqrt(diffusion_coeff)
            * c_mol_cm3
            * np.sqrt(scan_rate)
        )
        return float(ip / denom)

    @staticmethod
    def calculate_area_from_scan_rates(
        scan_rates: List[float],
        peak_currents: List[float],
        concentration_mM: float,
        n: int = 1,
        diffusion_coeff: float = DEFAULT_DIFFUSION_COEFF_CM2_S,
    ) -> Dict[str, float]:
        """
        Electroactive area from a scan-rate series via Randles-Sevcik.

        Fits peak current ip against sqrt(scan_rate). The slope is
        2.69e5 * n^1.5 * A * sqrt(D) * C, so A = slope / that prefactor.
        Using the slope (rather than a single ip) cancels any scan-rate-
        independent background current.

        Args:
            scan_rates: Scan rates in V/s (>= 2 values, all > 0).
            peak_currents: Peak currents in A, same order as scan_rates.
                Pass absolute (or baseline-corrected) values.
            concentration_mM: Bulk redox-probe concentration in mM.
            n: Electrons transferred (default 1).
            diffusion_coeff: D in cm^2/s (default ferricyanide ~7.6e-6).

        Returns:
            Dict with: area_cm2, area_mm2, slope_a_per_sqrtvs,
            intercept_a, r_squared.

        Raises:
            ValueError: On length mismatch, < 2 points, or non-positive inputs.
        """
        if len(scan_rates) != len(peak_currents):
            raise ValueError("scan_rates and peak_currents must be the same length")
        if len(scan_rates) < 2:
            raise ValueError("Need at least 2 scan rates for a regression")
        if concentration_mM <= 0:
            raise ValueError(f"concentration_mM must be positive, got {concentration_mM}")

        sqrt_v = np.sqrt(np.asarray(scan_rates, dtype=float))
        ip = np.abs(np.asarray(peak_currents, dtype=float))
        if np.any(sqrt_v <= 0):
            raise ValueError("All scan_rates must be positive")

        slope, intercept = np.polyfit(sqrt_v, ip, 1)
        # Coefficient of determination
        fit = slope * sqrt_v + intercept
        ss_res = float(np.sum((ip - fit) ** 2))
        ss_tot = float(np.sum((ip - np.mean(ip)) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float('nan')

        c_mol_cm3 = concentration_mM * 1e-6
        prefactor = RANDLES_SEVCIK_CONSTANT * (n ** 1.5) * np.sqrt(diffusion_coeff) * c_mol_cm3
        area_cm2 = float(slope / prefactor)

        return {
            'area_cm2': area_cm2,
            'area_mm2': area_cm2 * 100.0,
            'slope_a_per_sqrtvs': float(slope),
            'intercept_a': float(intercept),
            'r_squared': float(r_squared),
        }

    @staticmethod
    def plot_randles_sevcik(
        scan_rates: List[float],
        peak_currents: List[float],
        figsize: Tuple[int, int] = (6, 6),
    ) -> plt.Figure:
        """
        Plot peak current vs sqrt(scan rate) with the linear Randles-Sevcik fit.

        Minimal trace + fit line only (no value annotations), per project
        plotting conventions.

        Args:
            scan_rates: Scan rates in V/s.
            peak_currents: Peak currents in A (absolute values).
            figsize: Figure size in inches.

        Returns:
            matplotlib Figure object.
        """
        sqrt_v = np.sqrt(np.asarray(scan_rates, dtype=float))
        ip_ua = np.abs(np.asarray(peak_currents, dtype=float)) * 1e6

        order = np.argsort(sqrt_v)
        sqrt_v = sqrt_v[order]
        ip_ua = ip_ua[order]

        slope, intercept = np.polyfit(sqrt_v, ip_ua, 1)

        fig, ax = plt.subplots(figsize=figsize)
        ax.plot(sqrt_v, ip_ua, 'ko', label='Peak current')
        ax.plot(sqrt_v, slope * sqrt_v + intercept, 'r-', linewidth=1.0, label='Linear fit')

        ax.set_xlabel('sqrt(scan rate) (V/s)^0.5', fontsize=11)
        ax.set_ylabel('Peak current (µA)', fontsize=11)
        ax.set_title('Randles-Sevcik', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.legend(loc='best', fontsize=9)

        plt.tight_layout()
        return fig

    def plot_cv(self, figsize: Tuple[int, int] = (6, 6)) -> plt.Figure:
        """
        Generate CV plot (Current vs Potential).

        Args:
            figsize: Figure size in inches

        Returns:
            matplotlib Figure object
        """
        fig, ax = plt.subplots(figsize=figsize)

        # Convert current to µA for plotting
        current_ua = self.data['Current (A)'] * 1e6
        potential = self.data['Potential (V)']

        # Plot CV curve
        ax.plot(potential, current_ua, 'k-', linewidth=1.5)

        # Add zero current line
        ax.axhline(0, color='gray', linewidth=0.5, linestyle='--', alpha=0.7)

        # Labels and formatting
        ax.set_xlabel('Potential (V)', fontsize=11)
        ax.set_ylabel('Current (µA)', fontsize=11)
        ax.set_title('Cyclic Voltammetry', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')

        plt.tight_layout()
        return fig

    def plot_cv_with_cathodic_area(self, figsize: Tuple[int, int] = (6, 6)) -> plt.Figure:
        """
        Generate CV plot with shaded cathodic region.

        Args:
            figsize: Figure size in inches

        Returns:
            matplotlib Figure object

        Plot features:
        - CV curve with cathodic region (negative current) shaded
        - If CSC calculated: shows area between forward/reverse scans (actual integration region)
        - If CSC not calculated: shows all cathodic current (fallback behavior)
        - Shading uses fill_between for accurate area representation
        - User can adjust shading color/opacity in vector editor
        """
        fig, ax = plt.subplots(figsize=figsize)

        # Convert current to µA for plotting
        current_ua = self.data['Current (A)'] * 1e6
        potential = self.data['Potential (V)']

        # Plot CV curve
        ax.plot(potential, current_ua, 'k-', linewidth=1.5, label='CV Curve')

        # If CSC has been calculated, show forward/reverse scans and shade between them
        if self.v_common is not None and self.i_forward_interp is not None:
            i_forward_ua = self.i_forward_interp * 1e6
            i_backward_ua = self.i_backward_interp * 1e6

            # Plot forward and reverse scans
            ax.plot(self.v_common, i_forward_ua, 'r--', linewidth=0.8, alpha=0.5, label='Forward Scan')
            ax.plot(self.v_common, i_backward_ua, 'b--', linewidth=0.8, alpha=0.5, label='Reverse Scan')

            # Shade between forward and reverse scans in cathodic region
            # Clip both curves at zero to prevent shading above the zero line
            # This captures the cathodic charge area between the two scans
            forward_clipped = np.minimum(i_forward_ua, 0)
            reverse_clipped = np.minimum(i_backward_ua, 0)

            # Only shade where the curves differ (i.e., where there's area to shade)
            if not np.allclose(forward_clipped, reverse_clipped):
                ax.fill_between(
                    self.v_common,
                    forward_clipped,
                    reverse_clipped,
                    where=(reverse_clipped <= forward_clipped),
                    alpha=0.3,
                    color='blue',
                    label='Cathodic Region (CSC)'
                )
        else:
            # Fallback: shade all cathodic regions of the main curve
            cathodic_mask = current_ua < 0
            if cathodic_mask.any():
                ax.fill_between(
                    potential,
                    current_ua,
                    0,
                    where=cathodic_mask,
                    alpha=0.3,
                    color='blue',
                    label='Cathodic Region'
                )

        # Add zero current line
        ax.axhline(0, color='gray', linewidth=0.5, linestyle='--', alpha=0.7)

        # Labels and formatting
        ax.set_xlabel('Potential (V)', fontsize=11)
        ax.set_ylabel('Current (µA)', fontsize=11)
        ax.set_title('Cyclic Voltammetry - Cathodic Area', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.legend(loc='best', fontsize=9)

        plt.tight_layout()
        return fig

    def get_results_dataframe(self, scan_rate: Optional[float] = None,
                             electrode_area: Optional[float] = None) -> pd.DataFrame:
        """
        Get results as a formatted DataFrame.

        Args:
            scan_rate: Scan rate in V/s (optional)
            electrode_area: Electrode area in cm² (optional)

        Returns:
            DataFrame with analysis results
        """
        # Calculate CSC if parameters provided and not already calculated
        if self.csc is None and scan_rate is not None and electrode_area is not None:
            self.calculate_csc(scan_rate, electrode_area)

        results = {
            'Parameter': [
                'Peak Anodic Current',
                'Peak Cathodic Current',
                'Potential Range'
            ],
            'Value': [],
            'Unit': []
        }

        # Add CSC if calculated
        if self.csc is not None:
            results['Parameter'].insert(0, 'CSC')
            results['Value'].insert(0, f"{self.csc:.2f}")
            results['Unit'].insert(0, 'mC/cm²')

        results['Value'].extend([
            self.data['Current (A)'].max() * 1e6,  # Convert to µA
            self.data['Current (A)'].min() * 1e6,  # Convert to µA
            f"{self.data['Potential (V)'].min():.3f} to {self.data['Potential (V)'].max():.3f}"
        ])
        results['Unit'].extend(['µA', 'µA', 'V'])

        return pd.DataFrame(results)

    def get_summary(self, scan_rate: Optional[float] = None,
                   electrode_area: Optional[float] = None) -> Dict[str, Any]:
        """
        Get summary dictionary of analysis results.

        Args:
            scan_rate: Scan rate in V/s (optional)
            electrode_area: Electrode area in cm² (optional)

        Returns:
            Dictionary with analysis parameters
        """
        # Calculate CSC if parameters provided and not already calculated
        if self.csc is None and scan_rate is not None and electrode_area is not None:
            self.calculate_csc(scan_rate, electrode_area)

        summary = {
            'technique': 'CV',
            'n_data_points': len(self.data),
            'potential_range_v': (
                float(self.data['Potential (V)'].min()),
                float(self.data['Potential (V)'].max())
            ),
            'peak_anodic_current_ua': float(self.data['Current (A)'].max() * 1e6),
            'peak_cathodic_current_ua': float(self.data['Current (A)'].min() * 1e6)
        }

        # Add CSC if calculated
        if self.csc is not None:
            summary['csc_mc_per_cm2'] = float(self.csc)
            summary['scan_rate_v_per_s'] = float(scan_rate) if scan_rate else None
            summary['electrode_area_cm2'] = float(electrode_area) if electrode_area else None

            # Add loop status information
            if self.loop_status is not None:
                summary['loop_status'] = self.loop_status
            if self.gap_voltage is not None:
                summary['gap_voltage_v'] = float(self.gap_voltage)

        # Add redox peak / reversibility metrics if they have been extracted
        if self.peaks is not None:
            summary['epa_v'] = self.peaks['epa_v']
            summary['epc_v'] = self.peaks['epc_v']
            summary['delta_ep_mv'] = self.peaks['delta_ep_mv']
            summary['e_half_v'] = self.peaks['e_half_v']
            summary['ipa_ipc_ratio'] = self.peaks['ipa_ipc_ratio']
            summary['peaks_well_defined'] = self.peaks['peaks_well_defined']
            summary['reversible'] = self.peaks['reversible']

        return summary

    @staticmethod
    def detect_scan_rate_from_name(scan_name: str) -> Optional[float]:
        """
        Attempt to extract scan rate from scan name.

        Common patterns:
        - "50mvps" or "50 mvps" -> 0.05 V/s
        - "100mV/s" or "100 mV/s" -> 0.1 V/s
        - "0.1V/s" or "0.1 V/s" -> 0.1 V/s

        Args:
            scan_name: Name of the scan

        Returns:
            Scan rate in V/s, or None if not detected
        """
        # Pattern 1: X mvps or X mV/s (millivolts per second)
        pattern_mv = re.search(r'(\d+(?:\.\d+)?)\s*m[vV]/?[psS]', scan_name)
        if pattern_mv:
            mv_per_s = float(pattern_mv.group(1))
            return mv_per_s / 1000  # Convert mV/s to V/s

        # Pattern 2: X V/s or X Vps (volts per second)
        pattern_v = re.search(r'(\d+(?:\.\d+)?)\s*[vV]/?[psS]', scan_name)
        if pattern_v:
            return float(pattern_v.group(1))

        return None

    @staticmethod
    def batch_analyze(scans: Dict[str, pd.DataFrame],
                     scan_rate: Optional[float] = None,
                     electrode_area: float = 0.01,
                     export_manager=None,
                     auto_detect_scan_rate: bool = True) -> Tuple[pd.DataFrame, Dict[str, plt.Figure]]:
        """
        Batch analyze multiple CV scans.

        Args:
            scans: Dictionary of {scan_name: DataFrame} pairs
            scan_rate: Scan rate in V/s (optional if auto_detect_scan_rate=True)
            electrode_area: Electrode area in cm²
            export_manager: Optional ExportManager for saving results
            auto_detect_scan_rate: If True, attempt to detect scan rate from scan names

        Returns:
            Tuple of (summary_dataframe, figures_dict)
        """
        results = []
        figures = {}

        for scan_name, data in scans.items():
            try:
                # Create analyzer
                analyzer = CVAnalyzer(data)

                # Detect or use provided scan rate
                if auto_detect_scan_rate:
                    detected_rate = CVAnalyzer.detect_scan_rate_from_name(scan_name)
                    current_scan_rate = detected_rate if detected_rate else scan_rate
                    if current_scan_rate is None:
                        print(f"⚠ Warning: Could not detect scan rate for '{scan_name}', skipping CSC calculation")
                else:
                    current_scan_rate = scan_rate

                # Calculate CSC if scan rate available
                if current_scan_rate is not None:
                    csc = analyzer.calculate_csc(current_scan_rate, electrode_area)
                    if auto_detect_scan_rate and detected_rate:
                        print(f"✓ Analyzed {scan_name}: CSC={csc:.2f} mC/cm² (detected scan rate: {current_scan_rate*1000:.0f} mV/s)")
                    else:
                        print(f"✓ Analyzed {scan_name}: CSC={csc:.2f} mC/cm²")
                else:
                    csc = None
                    print(f"✓ Analyzed {scan_name}: No CSC (scan rate unknown)")

                # Generate plots
                fig_cv = analyzer.plot_cv()
                fig_cathodic = analyzer.plot_cv_with_cathodic_area()

                # Save plots if export manager provided
                if export_manager:
                    clean_name = scan_name.replace('/', '_').replace('\\', '_')
                    export_manager.save_figure(fig_cv, f"{clean_name}_cv", subdir='plots')
                    export_manager.save_figure(fig_cathodic, f"{clean_name}_cv_cathodic", subdir='plots')

                # Store figures
                figures[scan_name] = {
                    'cv': fig_cv,
                    'cv_cathodic': fig_cathodic
                }

                # Get summary
                summary = analyzer.get_summary(current_scan_rate, electrode_area)
                summary['scan_name'] = scan_name
                if auto_detect_scan_rate and detected_rate:
                    summary['detected_scan_rate_v_per_s'] = detected_rate
                results.append(summary)

            except Exception as e:
                print(f"✗ Failed to analyze {scan_name}: {e}")

        # Create summary dataframe
        summary_df = pd.DataFrame(results)

        # Reorder columns for better display
        cols_order = ['scan_name', 'csc_mc_per_cm2', 'peak_anodic_current_ua',
                     'peak_cathodic_current_ua', 'scan_rate_v_per_s', 'electrode_area_cm2',
                     'n_data_points', 'potential_range_v']
        if 'detected_scan_rate_v_per_s' in summary_df.columns:
            cols_order.insert(5, 'detected_scan_rate_v_per_s')

        cols_available = [c for c in cols_order if c in summary_df.columns]
        summary_df = summary_df[cols_available]

        # Save summary if export manager provided
        if export_manager:
            export_manager.save_dataframe(summary_df, 'batch_cv_results', subdir='data')

        return summary_df, figures

    @staticmethod
    def plot_cv_grouped(scans: Dict[str, pd.DataFrame],
                       grouping: Dict[str, List[str]],
                       error_style: str = 'bands',
                       figsize: Tuple[int, int] = (8, 6),
                       alpha_bands: float = 0.3,
                       xlim: Tuple[float, float] = None,
                       ylim: Tuple[float, float] = None,
                       export_manager=None) -> plt.Figure:
        """
        Plot grouped CV data with mean and standard error visualization.

        Args:
            scans: Dictionary of {scan_name: DataFrame} pairs
            grouping: Dictionary of {group_name: [scan_names]} for grouping
            error_style: 'bands' for shaded error regions or 'bars' for error bars
            figsize: Figure size in inches
            alpha_bands: Alpha transparency for error bands (if error_style='bands')
            xlim: Optional (min, max) tuple for x-axis limits in V
            ylim: Optional (min, max) tuple for y-axis limits in µA
            export_manager: Optional ExportManager for saving results

        Returns:
            matplotlib Figure object with grouped CV plots

        Example:
            grouping = {
                "Platinum Black": ["chip1 - 50mvps", "chip2 - 50mvps"],
                "Gold": ["chip3 - 50mvps", "chip4 - 50mvps"]
            }
            fig = CVAnalyzer.plot_cv_grouped(scans, grouping, xlim=(-0.2, 0.6), ylim=(-100, 50))
        """
        # Validate grouping
        is_valid, missing = validate_grouping(grouping, scans)
        if not is_valid:
            raise ValueError(f"Invalid grouping - missing scans: {missing}")

        fig, ax = plt.subplots(figsize=figsize)

        # Color palette for different groups
        colors = plt.cm.tab10(np.linspace(0, 0.7, len(grouping)))

        for idx, (group_name, scan_names) in enumerate(grouping.items()):
            # Get data arrays for this group
            try:
                # Check if interpolation is needed
                alignment_info = check_cv_voltage_alignment(scan_names, scans)

                if alignment_info['needs_interpolation']:
                    # Interpolate to common index grid (preserves CV hysteresis)
                    print(f"  Interpolating {len(scan_names)} scans to common index grid "
                          f"(points: {alignment_info['n_points']})")

                    common_voltage, interpolated_data = interpolate_cv_to_common_index(
                        scan_names, scans, n_points=500
                    )

                    # Convert interpolated data to arrays
                    current_arrays = [interpolated_data[name] for name in scan_names]
                    voltage = common_voltage
                else:
                    # No interpolation needed, use original data
                    current_arrays = get_group_data(
                        group_name, scan_names, scans, 'Current (A)'
                    )
                    # Use voltage from first scan (all should be identical in group)
                    voltage = scans[scan_names[0]]['Potential (V)'].values

                # Calculate mean and SD
                mean_current, std_current = calculate_mean_std(current_arrays)

                # Convert to µA for plotting
                mean_current_ua = mean_current * 1e6
                std_current_ua = std_current * 1e6

                # Plot mean line
                ax.plot(voltage, mean_current_ua,
                       color=colors[idx], linewidth=2,
                       label=f"{group_name} (n={len(scan_names)})")

                # Add error visualization
                if error_style == 'bands':
                    ax.fill_between(voltage,
                                  mean_current_ua - std_current_ua,
                                  mean_current_ua + std_current_ua,
                                  alpha=alpha_bands, color=colors[idx])
                elif error_style == 'bars':
                    # Sample error bars every nth point to avoid clutter
                    n_error_bars = 20  # Show ~20 error bars across the curve
                    step = max(1, len(voltage) // n_error_bars)
                    indices = range(0, len(voltage), step)

                    ax.errorbar(voltage[indices], mean_current_ua[indices],
                              yerr=std_current_ua[indices],
                              fmt='none', color=colors[idx], alpha=0.5,
                              capsize=3, capthick=1)

                print(f"✓ Plotted group '{group_name}' with {len(scan_names)} scans")

            except Exception as e:
                print(f"✗ Failed to plot group '{group_name}': {e}")

        # Add zero current line
        ax.axhline(0, color='gray', linewidth=0.5, linestyle='--', alpha=0.7)

        # Labels and formatting
        ax.set_xlabel('Potential (V)', fontsize=11)
        ax.set_ylabel('Current (µA)', fontsize=11)
        ax.set_title('Grouped Cyclic Voltammetry', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.legend(loc='best', fontsize=9)

        # Apply axis limits if specified
        if xlim is not None:
            ax.set_xlim(xlim)
        if ylim is not None:
            ax.set_ylim(ylim)

        plt.tight_layout()

        # Save if export manager provided
        if export_manager:
            export_manager.save_figure(fig, 'cv_grouped', subdir='plots')

        return fig

    @staticmethod
    def batch_analyze_grouped(scans: Dict[str, pd.DataFrame],
                            grouping: Dict[str, List[str]],
                            scan_rate: Optional[float] = None,
                            electrode_area: float = 0.01,
                            export_manager=None,
                            auto_detect_scan_rate: bool = True,
                            plot_individual: bool = False,
                            error_style: str = 'bands',
                            xlim: Tuple[float, float] = None,
                            ylim: Tuple[float, float] = None) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """
        Batch analyze CV scans with grouping and calculate group statistics.

        Args:
            scans: Dictionary of {scan_name: DataFrame} pairs
            grouping: Dictionary of {group_name: [scan_names]} for grouping
            scan_rate: Scan rate in V/s (optional if auto_detect_scan_rate=True)
            electrode_area: Electrode area in cm²
            export_manager: Optional ExportManager for saving results
            auto_detect_scan_rate: If True, attempt to detect scan rate from scan names
            plot_individual: If True, also generate individual plots for each scan
            error_style: 'bands' or 'bars' for error visualization in grouped plot
            xlim: Optional (min, max) tuple for x-axis limits in V
            ylim: Optional (min, max) tuple for y-axis limits in µA

        Returns:
            Tuple of (summary_dataframe, figures_dict)
            - summary_dataframe: Group-level statistics (mean CSC, SEM, etc.)
            - figures_dict: Dictionary containing 'grouped' plot and optionally 'individual' plots
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

            csc_values = []
            peak_anodic_values = []
            peak_cathodic_values = []
            individual_figures = {}

            # Analyze each scan in the group
            for scan_name in scan_names:
                try:
                    # Create analyzer
                    analyzer = CVAnalyzer(scans[scan_name])

                    # Detect or use provided scan rate
                    if auto_detect_scan_rate:
                        detected_rate = CVAnalyzer.detect_scan_rate_from_name(scan_name)
                        current_scan_rate = detected_rate if detected_rate else scan_rate
                    else:
                        current_scan_rate = scan_rate

                    # Calculate CSC if scan rate available
                    if current_scan_rate is not None and current_scan_rate > 0:
                        csc = analyzer.calculate_csc(current_scan_rate, electrode_area)
                        csc_values.append(csc)
                        print(f"  ✓ {scan_name}: CSC={csc:.2f} mC/cm²")
                    else:
                        print(f"  ⚠ {scan_name}: Could not calculate CSC (no scan rate)")

                    # Get peak currents (don't pass scan_rate if None to avoid CSC calculation)
                    if current_scan_rate is not None and current_scan_rate > 0:
                        summary = analyzer.get_summary(current_scan_rate, electrode_area)
                    else:
                        summary = analyzer.get_summary()  # Don't pass None scan_rate
                    peak_anodic_values.append(summary['peak_anodic_current_ua'])
                    peak_cathodic_values.append(summary['peak_cathodic_current_ua'])

                    # Generate individual plots if requested
                    if plot_individual:
                        fig_cv = analyzer.plot_cv()
                        fig_cathodic = analyzer.plot_cv_with_cathodic_area()
                        individual_figures[scan_name] = {
                            'cv': fig_cv,
                            'cv_cathodic': fig_cathodic
                        }

                        if export_manager:
                            clean_name = scan_name.replace('/', '_').replace('\\', '_')
                            export_manager.save_figure(
                                fig_cv, f"{clean_name}_cv",
                                subdir=f'plots/{group_name}'
                            )
                            export_manager.save_figure(
                                fig_cathodic, f"{clean_name}_cv_cathodic",
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

            if csc_values:
                n_valid = np.sum(np.isfinite(csc_values))
                group_summary['csc_mean_mc_per_cm2'] = np.nanmean(csc_values)
                group_summary['csc_std_mc_per_cm2'] = np.nanstd(csc_values, ddof=1) if n_valid > 1 else 0
                group_summary['csc_formatted'] = format_error_display(
                    group_summary['csc_mean_mc_per_cm2'],
                    group_summary['csc_std_mc_per_cm2']
                )

            if peak_anodic_values:
                n_valid = np.sum(np.isfinite(peak_anodic_values))
                group_summary['peak_anodic_mean_ua'] = np.nanmean(peak_anodic_values)
                group_summary['peak_anodic_std_ua'] = np.nanstd(peak_anodic_values, ddof=1) if n_valid > 1 else 0

            if peak_cathodic_values:
                n_valid = np.sum(np.isfinite(peak_cathodic_values))
                group_summary['peak_cathodic_mean_ua'] = np.nanmean(peak_cathodic_values)
                group_summary['peak_cathodic_std_ua'] = np.nanstd(peak_cathodic_values, ddof=1) if n_valid > 1 else 0

            group_summary['scan_rate_v_per_s'] = current_scan_rate
            group_summary['electrode_area_cm2'] = electrode_area

            group_results.append(group_summary)

            if plot_individual:
                figures[f'{group_name}_individual'] = individual_figures

        # Create group summary dataframe
        summary_df = pd.DataFrame(group_results)

        # Generate grouped plot
        grouped_fig = CVAnalyzer.plot_cv_grouped(
            scans, grouping, error_style=error_style, xlim=xlim, ylim=ylim,
            export_manager=export_manager
        )
        figures['grouped'] = grouped_fig

        # Save summary if export manager provided
        if export_manager:
            export_manager.save_dataframe(summary_df, 'grouped_cv_results', subdir='data')

        print(f"\n{'='*50}")
        print("Group Analysis Summary:")
        for _, row in summary_df.iterrows():
            if 'csc_formatted' in row:
                print(f"  {row['group_name']}: CSC = {row['csc_formatted']} mC/cm²")

        return summary_df, figures
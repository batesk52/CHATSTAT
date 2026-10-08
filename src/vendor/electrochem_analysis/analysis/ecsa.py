"""
ECSA (Electrochemical Surface Area) analysis module.

Supports two methods:
- Cdl (double-layer capacitance): Primary method using multiple scan-rate CVs
- Randles-Sevcik: Secondary method using peak current from a single CV

Typical workflow:
    from src.dataloaders import load_psession
    from src.analysis import ECSAAnalyzer

    # Load scan-rate CVs for one specimen
    scans = load_psession('S0151_left.pssession')
    analyzer = ECSAAnalyzer(scans)
    results = analyzer.calculate_cdl()
    fig = analyzer.plot_cdl_fit()

    # Batch analysis across specimens
    specimen_scans = ECSAAnalyzer.load_specimens('/path/to/data/')
    summary_df, figures = ECSAAnalyzer.batch_analyze(specimen_scans)
"""

from typing import Dict, List, Tuple, Optional, Any
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import linregress
from pathlib import Path
import re
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.vendor.electrochem_analysis.utils.grouping import validate_grouping, format_error_display
from src.vendor.electrochem_analysis.analysis.cv import CVAnalyzer


class ECSAAnalyzer:
    """
    Analyzer for Electrochemical Surface Area using scan-rate CV data.

    Unlike CVAnalyzer (single DataFrame), ECSAAnalyzer takes a dictionary
    of scan-rate CVs because ECSA fundamentally requires multiple scan rates.
    """

    def __init__(self, scans: Dict[str, pd.DataFrame],
                 scan_rates: Dict[str, float] = None,
                 auto_detect_scan_rate: bool = True):
        """
        Initialize ECSA analyzer with scan-rate CV data.

        Args:
            scans: Dictionary of {scan_name: DataFrame} with CV data at
                different scan rates. Each DataFrame must have columns
                ['Potential (V)', 'Current (A)'].
            scan_rates: Optional dict of {scan_name: rate_in_V_per_s}.
                If not provided and auto_detect_scan_rate is True, rates
                are detected from scan names.
            auto_detect_scan_rate: If True, detect scan rates from names.

        Raises:
            ValueError: If required columns are missing or no scan rates found.
        """
        required_cols = ['Potential (V)', 'Current (A)']
        for name, df in scans.items():
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                raise ValueError(
                    f"Scan '{name}' missing required columns: {missing}"
                )

        self.scans = {k: v.copy() for k, v in scans.items()}
        self._cdl_results = None
        self._randles_results = None

        # Resolve scan rates
        if scan_rates is not None:
            self.scan_rates = dict(scan_rates)
        elif auto_detect_scan_rate:
            self.scan_rates = {}
            for name in scans:
                rate = CVAnalyzer.detect_scan_rate_from_name(name)
                if rate is not None:
                    self.scan_rates[name] = rate
            if not self.scan_rates:
                raise ValueError(
                    "Could not detect scan rates from scan names. "
                    "Provide scan_rates explicitly."
                )
        else:
            raise ValueError("scan_rates must be provided when auto_detect_scan_rate=False")

        # Sort by scan rate
        self.scan_rates = dict(
            sorted(self.scan_rates.items(), key=lambda x: x[1])
        )

    def _split_sweeps(self, data: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Split CV data into forward and reverse sweeps using turning points."""
        voltage = data['Potential (V)'].values
        dv = np.diff(voltage)
        sign_dv = np.sign(dv)
        sign_changes = np.where(np.diff(sign_dv) != 0)[0] + 1

        if len(sign_changes) >= 2:
            first_turning = sign_changes[0] + 1
            second_turning = sign_changes[1] + 1
            forward = data.iloc[first_turning:second_turning].copy()
            reverse = data.iloc[second_turning:].copy()
        elif len(sign_changes) == 1:
            split_idx = sign_changes[0] + 1
            forward = data.iloc[:split_idx].copy()
            reverse = data.iloc[split_idx:].copy()
        else:
            split_idx = len(data) // 2
            forward = data.iloc[:split_idx].copy()
            reverse = data.iloc[split_idx:].copy()

        return forward, reverse

    def calculate_cdl(self, v_midpoint: float = None,
                      electrode_area_cm2: float = None,
                      specific_capacitance: float = None) -> dict:
        """
        Calculate double-layer capacitance (Cdl) from scan-rate CVs.

        For each scan rate, the current difference (delta_i) between forward
        and reverse sweeps at v_midpoint is measured. A linear fit of
        delta_i vs scan_rate gives slope = 2*Cdl.

        Args:
            v_midpoint: Potential (V) at which to measure current difference.
                If None (default), auto-calculated as the center of the
                overlapping voltage range across all scans.
            electrode_area_cm2: Electrode geometric area in cm². If provided,
                current density is used and Cdl reported in uF/cm².
            specific_capacitance: Specific capacitance of smooth surface
                in uF/cm². If provided, roughness_factor = Cdl / specific_capacitance.
                If None (default), roughness factor is not computed.

        Returns:
            Dictionary with:
                - cdl_F: Double-layer capacitance in Farads
                - cdl_uF_cm2: Capacitance per area (if electrode_area_cm2 given)
                - r_squared: R² of the linear fit
                - scan_rates: List of scan rates (V/s)
                - delta_values: List of delta_i (A) or delta_j (A/cm²) values
                - roughness_factor: Cdl/specific_capacitance (only if both area and specific_capacitance given)
                - slope: Fit slope (= 2*Cdl)
                - intercept: Fit intercept
        """
        # Auto-detect v_midpoint from data if not provided
        if v_midpoint is None:
            v_mins = []
            v_maxs = []
            for name in self.scan_rates:
                if name not in self.scans:
                    continue
                data = self.scans[name]
                v_mins.append(data['Potential (V)'].min())
                v_maxs.append(data['Potential (V)'].max())
            # Use the intersection range across all scans
            global_min = max(v_mins)
            global_max = min(v_maxs)
            v_midpoint = (global_min + global_max) / 2.0
            print(f"Auto-detected v_midpoint = {v_midpoint:.3f} V "
                  f"(range: {global_min:.3f} to {global_max:.3f} V)")

        rates = []
        delta_values = []

        for name, rate in self.scan_rates.items():
            if name not in self.scans:
                continue
            data = self.scans[name]
            forward, reverse = self._split_sweeps(data)

            # Sort by potential for interpolation
            forward = forward.sort_values('Potential (V)')
            reverse = reverse.sort_values('Potential (V)')

            # Check that v_midpoint is within range of both sweeps
            v_min = max(forward['Potential (V)'].min(),
                        reverse['Potential (V)'].min())
            v_max = min(forward['Potential (V)'].max(),
                        reverse['Potential (V)'].max())

            if not (v_min <= v_midpoint <= v_max):
                print(f"Warning: v_midpoint={v_midpoint:.3f} V outside overlap "
                      f"range [{v_min:.3f}, {v_max:.3f}] for '{name}', skipping")
                continue

            # Interpolate current at v_midpoint
            i_forward = np.interp(
                v_midpoint,
                forward['Potential (V)'].values,
                forward['Current (A)'].values
            )
            i_reverse = np.interp(
                v_midpoint,
                reverse['Potential (V)'].values,
                reverse['Current (A)'].values
            )

            delta_i = abs(i_forward - i_reverse)

            if electrode_area_cm2 is not None and electrode_area_cm2 > 0:
                delta_values.append(delta_i / electrode_area_cm2)
            else:
                delta_values.append(delta_i)

            rates.append(rate)

        if len(rates) < 2:
            raise ValueError(
                f"Need at least 2 valid scan rates for Cdl fit, got {len(rates)}"
            )

        # Linear fit: delta = slope * scan_rate + intercept
        # slope = 2 * Cdl
        rates_arr = np.array(rates)
        delta_arr = np.array(delta_values)
        slope, intercept, r_value, _, _ = linregress(rates_arr, delta_arr)
        r_squared = r_value ** 2

        cdl_raw = abs(slope) / 2.0  # slope = 2*Cdl

        results = {
            'scan_rates': rates,
            'delta_values': delta_values,
            'slope': slope,
            'intercept': intercept,
            'r_squared': r_squared,
            'v_midpoint': v_midpoint,
        }

        if electrode_area_cm2 is not None and electrode_area_cm2 > 0:
            # cdl_raw is in A/(V/s)/cm² = F/cm²
            cdl_uF_cm2 = cdl_raw * 1e6
            results['cdl_uF_cm2'] = cdl_uF_cm2
            results['cdl_F'] = cdl_raw * electrode_area_cm2
            results['electrode_area_cm2'] = electrode_area_cm2
            if specific_capacitance is not None:
                results['roughness_factor'] = cdl_uF_cm2 / specific_capacitance
                results['specific_capacitance'] = specific_capacitance
        else:
            # cdl_raw is in A/(V/s) = F
            results['cdl_F'] = cdl_raw
            results['cdl_uF'] = cdl_raw * 1e6

        self._cdl_results = results
        return results

    def calculate_randles_sevcik(self, scan_name: str = None,
                                 n_electrons: int = 1,
                                 diffusion_coeff: float = 7.6e-6,
                                 concentration: float = 5.0,
                                 scan_rate: float = 0.05) -> dict:
        """
        Calculate ECSA via Randles-Sevcik equation from peak anodic current.

        ip = 2.69e5 * n^1.5 * A * D^0.5 * v^0.5 * C

        Args:
            scan_name: Which scan to use (default: first in self.scans)
            n_electrons: Number of electrons in redox reaction
            diffusion_coeff: Diffusion coefficient in cm²/s
            concentration: Bulk concentration in mM (converted to mol/cm³)
            scan_rate: Scan rate in V/s

        Returns:
            Dictionary with ecsa_cm2 and peak_current_A
        """
        if scan_name is None:
            scan_name = next(iter(self.scans))
        if scan_name not in self.scans:
            raise ValueError(f"Scan '{scan_name}' not found")

        data = self.scans[scan_name]
        ip = abs(data['Current (A)'].max())  # Peak anodic current

        # Convert concentration: mM -> mol/cm³
        # 1 mM = 1e-3 mol/L = 1e-6 mol/cm³
        c_mol_cm3 = concentration * 1e-6

        # Randles-Sevcik: ip = 2.69e5 * n^1.5 * A * D^0.5 * v^0.5 * C
        # Solve for A:
        denominator = 2.69e5 * (n_electrons ** 1.5) * (diffusion_coeff ** 0.5) * (scan_rate ** 0.5) * c_mol_cm3
        ecsa = ip / denominator

        results = {
            'ecsa_cm2': ecsa,
            'peak_current_A': ip,
            'scan_name': scan_name,
            'n_electrons': n_electrons,
            'diffusion_coeff_cm2_s': diffusion_coeff,
            'concentration_mM': concentration,
            'scan_rate_V_s': scan_rate,
        }
        self._randles_results = results
        return results

    @staticmethod
    def calculate_ecsa_hupd(scan: pd.DataFrame,
                            v_window: Tuple[float, float] = (0.05, 0.40),
                            scan_rate: float = 0.05,
                            q_pt: float = 210e-6,
                            sweep: str = 'cathodic') -> Dict[str, float]:
        """
        Estimate a *relative* Pt-black ECSA proxy by integrating the cathodic
        (reduction) current over an H-UPD-like potential window.

        Caveat: Pt-black deposition CVs from the Chu et al. 2023 protocol are
        single-scan deposition sweeps in chloroplatinic-acid bath (not H-UPD
        sweeps in 0.5 M H2SO4). This means the absolute charge integrated
        here is biased upward by the Pt(II) Faradaic deposition current and
        is *not* a true Pt-metal H-UPD ECSA. However, when comparing channels
        within the same specimen run (same bath, same deposition conditions),
        the relative per-channel charge tracks the relative quantity of
        deposited Pt-black, which is the appropriate proxy for the
        Spearman rank-correlation analysis.

        Algorithm:
            1. Split the CV into forward (anodic) and reverse (cathodic) sweeps.
            2. Restrict to the v_window range.
            3. Take the cathodic (reduction) sweep current and integrate
               |I| dt over the sweep, where dt = dV / scan_rate. This gives
               a charge in coulombs.
            4. ECSA_proxy_cm2 = Q / q_pt, where q_pt is the Pt H-UPD
               specific charge (default 210 µC/cm²). RF = ECSA / A_geom
               must be computed by the caller using their geometric area.

        Args:
            scan: DataFrame with columns 'Potential (V)' and 'Current (A)'
                (single CV cycle).
            v_window: (v_low, v_high) potential range to integrate over (V).
                Default (0.05, 0.40) is a typical Pt H-UPD region.
            scan_rate: Scan rate in V/s. Required to convert the
                potential-domain integral into a charge.
            q_pt: Pt H-UPD specific charge in C/cm² (default 210e-6 = 210
                µC/cm²).
            sweep: 'cathodic' (default — reduction current, conventional
                H-UPD direction) or 'anodic'.

        Returns:
            Dictionary with:
                - charge_C: Integrated charge over v_window (Coulombs)
                - ecsa_cm2: Apparent Pt ECSA = charge / q_pt
                - n_points_in_window: Number of CV points used
                - sweep_used: Which sweep ('cathodic' or 'anodic')
                - v_window: The (v_low, v_high) window used
                - q_pt: The specific charge used
        """
        if 'Potential (V)' not in scan.columns or 'Current (A)' not in scan.columns:
            raise ValueError(
                "scan must have columns 'Potential (V)' and 'Current (A)'"
            )
        if scan_rate <= 0:
            raise ValueError("scan_rate must be > 0 V/s")

        v_low, v_high = sorted(v_window)

        # Split sweeps using sign of dV
        v = scan['Potential (V)'].values
        i = scan['Current (A)'].values
        dv = np.diff(v)
        if len(dv) == 0:
            raise ValueError("scan has insufficient points")

        sign_dv = np.sign(dv)
        sign_changes = np.where(np.diff(sign_dv) != 0)[0] + 1

        # Use forward (anodic, increasing V) and reverse (cathodic,
        # decreasing V) halves
        if len(sign_changes) >= 1:
            split_idx = sign_changes[0] + 1
            anodic = scan.iloc[:split_idx].copy()
            cathodic = scan.iloc[split_idx:].copy()
        else:
            # Fallback: split in half
            split_idx = len(scan) // 2
            anodic = scan.iloc[:split_idx].copy()
            cathodic = scan.iloc[split_idx:].copy()

        target = cathodic if sweep == 'cathodic' else anodic
        target = target.sort_values('Potential (V)')

        # Restrict to v_window
        mask = (target['Potential (V)'] >= v_low) & \
               (target['Potential (V)'] <= v_high)
        sub = target.loc[mask]
        n_pts = len(sub)
        if n_pts < 2:
            return {
                'charge_C': float('nan'),
                'ecsa_cm2': float('nan'),
                'n_points_in_window': n_pts,
                'sweep_used': sweep,
                'v_window': (v_low, v_high),
                'q_pt': q_pt,
            }

        v_arr = sub['Potential (V)'].values
        i_arr = sub['Current (A)'].values

        # Q = integral |I| dt over the sweep window.
        # dt = dV / scan_rate, so Q = (1/scan_rate) * integral |I| dV.
        charge_C = float(np.trapezoid(np.abs(i_arr), v_arr) / scan_rate)
        ecsa_cm2 = charge_C / q_pt if q_pt > 0 else float('nan')

        return {
            'charge_C': charge_C,
            'ecsa_cm2': ecsa_cm2,
            'n_points_in_window': n_pts,
            'sweep_used': sweep,
            'v_window': (v_low, v_high),
            'q_pt': q_pt,
        }

    def plot_cv_overlay(self, figsize: Tuple[int, int] = (8, 6)) -> plt.Figure:
        """
        Plot all scan-rate CVs overlaid, colored by scan rate.

        Returns:
            matplotlib Figure object
        """
        fig, ax = plt.subplots(figsize=figsize)
        cmap = plt.cm.viridis
        n = len(self.scan_rates)
        colors = [cmap(i / max(n - 1, 1)) for i in range(n)]

        for idx, (name, rate) in enumerate(self.scan_rates.items()):
            if name not in self.scans:
                continue
            data = self.scans[name]
            current_ua = data['Current (A)'] * 1e6
            label = f"{rate * 1000:.0f} mV/s"
            ax.plot(data['Potential (V)'], current_ua,
                    color=colors[idx], linewidth=1.5, label=label)

        ax.axhline(0, color='gray', linewidth=0.5, linestyle='--', alpha=0.7)
        ax.set_xlabel('Potential (V)', fontsize=11)
        ax.set_ylabel('Current (\u00b5A)', fontsize=11)
        ax.set_title('Scan Rate CV Overlay', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        ax.legend(loc='best', fontsize=9)
        plt.tight_layout()
        return fig

    def plot_cdl_fit(self, figsize: Tuple[int, int] = (6, 5)) -> plt.Figure:
        """
        Plot delta_i (or delta_j) vs scan rate with linear fit.

        Must call calculate_cdl() first.

        Returns:
            matplotlib Figure object
        """
        if self._cdl_results is None:
            raise ValueError("Call calculate_cdl() before plotting fit")

        r = self._cdl_results
        rates = np.array(r['scan_rates'])
        deltas = np.array(r['delta_values'])

        fig, ax = plt.subplots(figsize=figsize)

        # Data points
        rates_mv = rates * 1000  # Convert to mV/s for display
        ax.scatter(rates_mv, deltas, color='navy', s=60, zorder=5)

        # Linear fit line
        fit_x = np.linspace(0, rates.max() * 1.1, 100)
        fit_y = r['slope'] * fit_x + r['intercept']
        ax.plot(fit_x * 1000, fit_y, 'r--', linewidth=1.5)

        # Annotate
        if 'cdl_uF_cm2' in r:
            cdl_str = f"C$_{{dl}}$ = {r['cdl_uF_cm2']:.1f} \u00b5F/cm\u00b2"
            y_label = 'Current Density Difference (A/cm\u00b2)'
        elif 'cdl_uF' in r:
            cdl_str = f"C$_{{dl}}$ = {r['cdl_uF']:.2f} \u00b5F"
            y_label = 'Current Difference (A)'
        else:
            cdl_str = f"C$_{{dl}}$ = {r['cdl_F']:.2e} F"
            y_label = 'Current Difference (A)'

        ax.text(0.05, 0.95, f"{cdl_str}\nR\u00b2 = {r['r_squared']:.4f}",
                transform=ax.transAxes, fontsize=10, verticalalignment='top',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

        ax.set_xlabel('Scan Rate (mV/s)', fontsize=11)
        ax.set_ylabel(y_label, fontsize=11)
        ax.set_title('Cdl Linear Fit', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--')
        plt.tight_layout()
        return fig

    def get_results_dataframe(self) -> pd.DataFrame:
        """Get Cdl results as a single-row DataFrame."""
        if self._cdl_results is None:
            raise ValueError("Call calculate_cdl() first")

        r = self._cdl_results
        row = {
            'r_squared': r['r_squared'],
            'v_midpoint_V': r['v_midpoint'],
            'n_scan_rates': len(r['scan_rates']),
        }

        if 'cdl_uF_cm2' in r:
            row['cdl_uF_cm2'] = r['cdl_uF_cm2']
            row['cdl_F'] = r['cdl_F']
            row['electrode_area_cm2'] = r['electrode_area_cm2']
            if 'roughness_factor' in r:
                row['roughness_factor'] = r['roughness_factor']
        else:
            row['cdl_F'] = r['cdl_F']

        return pd.DataFrame([row])

    def get_summary(self) -> dict:
        """Get summary dictionary of all computed results."""
        summary = {
            'technique': 'ECSA',
            'n_scans': len(self.scans),
            'scan_rates_V_s': list(self.scan_rates.values()),
        }
        if self._cdl_results is not None:
            summary['cdl'] = {k: v for k, v in self._cdl_results.items()
                              if k not in ('scan_rates', 'delta_values')}
        if self._randles_results is not None:
            summary['randles_sevcik'] = self._randles_results
        return summary

    def print_results(self):
        """Print formatted results to console."""
        if self._cdl_results is not None:
            r = self._cdl_results
            print("=== ECSA - Cdl Method ===")
            if 'cdl_uF_cm2' in r:
                print(f"  Cdl:              {r['cdl_uF_cm2']:.2f} uF/cm2")
                if 'roughness_factor' in r:
                    print(f"  Roughness Factor: {r['roughness_factor']:.2f}")
            else:
                print(f"  Cdl:              {r['cdl_F']:.4e} F")
            print(f"  R^2:              {r['r_squared']:.4f}")
            print(f"  v_midpoint:       {r['v_midpoint']:.3f} V")
            print(f"  Scan rates used:  {len(r['scan_rates'])}")

        if self._randles_results is not None:
            r = self._randles_results
            print("\n=== ECSA - Randles-Sevcik ===")
            print(f"  ECSA:             {r['ecsa_cm2']:.4f} cm2")
            print(f"  Peak current:     {r['peak_current_A']:.4e} A")

    @staticmethod
    def load_specimens(data_dir: str,
                       curve_index: int = -1) -> Dict[str, Dict[str, pd.DataFrame]]:
        """
        Load all .pssession files from a directory as separate specimens.

        Args:
            data_dir: Path to directory containing .pssession files
            curve_index: Which curve repetition to use (-1 = last)

        Returns:
            {specimen_name: {scan_name: DataFrame}} ready for batch methods.
            specimen_name = file stem (e.g., "chip1 - left")
        """
        from ..dataloaders.psession_parser import load_all_scans_from_psession
        from ..utils.path_utils import smart_path

        data_path = smart_path(data_dir)
        if not data_path.is_dir():
            raise FileNotFoundError(f"Directory not found: {data_dir}")

        required_cols = ['Potential (V)', 'Current (A)']
        specimen_scans = {}
        for f in sorted(data_path.glob('*.pssession')):
            specimen_name = f.stem
            try:
                scans = load_all_scans_from_psession(str(f), curve_index=curve_index)
                # Skip files whose scans lack required CV columns (e.g. CA/amperometry)
                if not all(
                    all(c in df.columns for c in required_cols)
                    for df in scans.values()
                ):
                    print(f"Skipping '{f.name}' (not CV data)")
                    continue
                specimen_scans[specimen_name] = scans
            except Exception as e:
                print(f"Warning: Failed to load '{f.name}': {e}")

        if not specimen_scans:
            raise ValueError(f"No .pssession files found in {data_dir}")

        return specimen_scans

    @staticmethod
    def batch_analyze(specimen_scans: Dict[str, Dict[str, pd.DataFrame]],
                      method: str = 'cdl',
                      v_midpoint: float = None,
                      electrode_area_cm2: float = None,
                      specific_capacitance: float = None,
                      export_manager=None) -> Tuple[pd.DataFrame, Dict]:
        """
        Batch analyze ECSA across multiple specimens.

        Args:
            specimen_scans: {specimen_name: {scan_name: DataFrame}}
            method: 'cdl' or 'randles_sevcik'
            v_midpoint: Potential for Cdl measurement
            electrode_area_cm2: Geometric electrode area in cm²
            specific_capacitance: For roughness factor calculation (uF/cm²).
                If None (default), roughness factor is not computed.
            export_manager: Optional ExportManager for saving results

        Returns:
            (summary_df, figures_dict)
        """
        results = []
        figures = {}

        for specimen, scans in specimen_scans.items():
            try:
                analyzer = ECSAAnalyzer(scans)

                if method == 'cdl':
                    cdl = analyzer.calculate_cdl(
                        v_midpoint=v_midpoint,
                        electrode_area_cm2=electrode_area_cm2,
                        specific_capacitance=specific_capacitance,
                    )
                    row = {
                        'specimen': specimen,
                        'r_squared': cdl['r_squared'],
                        'n_scan_rates': len(cdl['scan_rates']),
                    }
                    if 'cdl_uF_cm2' in cdl:
                        row['cdl_uF_cm2'] = cdl['cdl_uF_cm2']
                        if 'roughness_factor' in cdl:
                            row['roughness_factor'] = cdl['roughness_factor']
                    else:
                        row['cdl_F'] = cdl['cdl_F']

                    # Generate figures
                    fig_overlay = analyzer.plot_cv_overlay()
                    fig_fit = analyzer.plot_cdl_fit()
                    figures[specimen] = {
                        'cv_overlay': fig_overlay,
                        'cdl_fit': fig_fit,
                    }

                    if export_manager:
                        clean = specimen.replace('/', '_').replace('\\', '_')
                        export_manager.save_figure(fig_overlay,
                                                   f"{clean}_cv_overlay", subdir='plots')
                        export_manager.save_figure(fig_fit,
                                                   f"{clean}_cdl_fit", subdir='plots')

                    if 'cdl_uF_cm2' in cdl:
                        print(f"  {specimen}: Cdl={cdl['cdl_uF_cm2']:.2f} uF/cm2, "
                              f"R2={cdl['r_squared']:.4f}")
                    else:
                        print(f"  {specimen}: Cdl={cdl['cdl_F']:.4e} F, "
                              f"R2={cdl['r_squared']:.4f}")

                elif method == 'randles_sevcik':
                    rs = analyzer.calculate_randles_sevcik()
                    row = {
                        'specimen': specimen,
                        'ecsa_cm2': rs['ecsa_cm2'],
                        'peak_current_A': rs['peak_current_A'],
                    }
                    print(f"  {specimen}: ECSA={rs['ecsa_cm2']:.4f} cm2")

                results.append(row)

            except Exception as e:
                print(f"  Failed: {specimen}: {e}")

        summary_df = pd.DataFrame(results)

        if export_manager and not summary_df.empty:
            export_manager.save_dataframe(summary_df,
                                          'batch_ecsa_results', subdir='data')

        return summary_df, figures

    @staticmethod
    def batch_analyze_grouped(specimen_scans: Dict[str, Dict[str, pd.DataFrame]],
                              grouping: Dict[str, List[str]],
                              method: str = 'cdl',
                              v_midpoint: float = None,
                              electrode_area_cm2: float = None,
                              specific_capacitance: float = None,
                              baseline_cdl: Dict[str, float] = None,
                              min_r_squared: float = 0.0,
                              export_manager=None) -> Tuple[pd.DataFrame, Dict]:
        """
        Batch analyze ECSA with specimen grouping and statistics.

        Args:
            specimen_scans: {specimen_name: {scan_name: DataFrame}}
            grouping: {"bare_Au": ["chip1 - left", "chip3 - right", ...], ...}
            method: 'cdl' or 'randles_sevcik'
            v_midpoint: Potential for Cdl measurement
            electrode_area_cm2: Geometric electrode area in cm²
            specific_capacitance: For roughness factor (uF/cm²).
                If None (default), roughness factor is not computed.
            baseline_cdl: Maps group names to baseline Cdl in µF/cm²
                (e.g., {"25 nC/um2 Pt on Au": 7.77}). When provided,
                enhancement = group_mean_cdl / baseline_cdl for each group.
            min_r_squared: Minimum R² to include specimen in group stats
                (default 0.0 = include all). Specimens below threshold are
                flagged but still plotted.
            export_manager: Optional ExportManager for saving results

        Returns:
            (group_summary_df, figures_dict)
            figures_dict includes per-specimen fits and a 'cdl_grouped' key
            with the grouped comparison plot.
        """
        # Validate grouping against specimen names
        is_valid, missing = validate_grouping(grouping, specimen_scans)
        if not is_valid:
            raise ValueError(f"Invalid grouping - missing specimens: {missing}")

        group_results = []
        figures = {}
        # Store per-specimen Cdl data for the grouped plot
        # {specimen: {'scan_rates': [...], 'delta_values': [...], 'cdl_results': {...}}}
        specimen_cdl_data = {}

        for group_name, specimen_names in grouping.items():
            print(f"\nGroup: {group_name}")
            cdl_values = []
            rf_values = []

            for specimen in specimen_names:
                if specimen not in specimen_scans:
                    print(f"  Skipping missing specimen: {specimen}")
                    continue

                try:
                    scans = specimen_scans[specimen]
                    analyzer = ECSAAnalyzer(scans)

                    if method == 'cdl':
                        cdl = analyzer.calculate_cdl(
                            v_midpoint=v_midpoint,
                            electrode_area_cm2=electrode_area_cm2,
                            specific_capacitance=specific_capacitance,
                        )

                        # Store raw Cdl data for grouped plot
                        specimen_cdl_data[specimen] = {
                            'scan_rates': cdl['scan_rates'],
                            'delta_values': cdl['delta_values'],
                            'cdl_results': cdl,
                        }

                        r2 = cdl['r_squared']
                        r2_flag = " ***" if r2 < max(min_r_squared, 0.90) else ""

                        if 'cdl_uF_cm2' in cdl:
                            if r2 >= min_r_squared:
                                cdl_values.append(cdl['cdl_uF_cm2'])
                                if 'roughness_factor' in cdl:
                                    rf_values.append(cdl['roughness_factor'])
                            print(f"  {specimen}: Cdl={cdl['cdl_uF_cm2']:.2f} uF/cm2, "
                                  f"R2={r2:.4f}{r2_flag}")
                        else:
                            if r2 >= min_r_squared:
                                cdl_values.append(cdl['cdl_F'] * 1e6)
                            print(f"  {specimen}: Cdl={cdl['cdl_F']:.4e} F, "
                                  f"R2={r2:.4f}{r2_flag}")

                        # Store individual fit figures
                        fig_fit = analyzer.plot_cdl_fit()
                        figures[f"{group_name}/{specimen}_cdl_fit"] = fig_fit
                        if export_manager:
                            clean = specimen.replace('/', '_').replace('\\', '_')
                            export_manager.save_figure(
                                fig_fit,
                                f"{clean}_cdl_fit",
                                subdir=f'plots/{group_name}'
                            )

                except Exception as e:
                    print(f"  Failed: {specimen}: {e}")

            # Group statistics
            row = {
                'group_name': group_name,
                'n_specimens': len(specimen_names),
                'specimen_names': ', '.join(specimen_names),
            }

            if cdl_values:
                n_valid = np.sum(np.isfinite(cdl_values))
                cdl_mean = np.nanmean(cdl_values)
                cdl_std = np.nanstd(cdl_values, ddof=1) if n_valid > 1 else 0.0

                if electrode_area_cm2 is not None:
                    row['cdl_mean_uF_cm2'] = cdl_mean
                    row['cdl_std_uF_cm2'] = cdl_std
                    row['cdl_formatted'] = format_error_display(cdl_mean, cdl_std)
                else:
                    row['cdl_mean_uF'] = cdl_mean
                    row['cdl_std_uF'] = cdl_std
                    row['cdl_formatted'] = format_error_display(cdl_mean, cdl_std)

                if rf_values:
                    rf_mean = np.nanmean(rf_values)
                    rf_std = np.nanstd(rf_values, ddof=1) if n_valid > 1 else 0.0
                    row['rf_mean'] = rf_mean
                    row['rf_std'] = rf_std
                    row['rf_formatted'] = format_error_display(rf_mean, rf_std)

            group_results.append(row)

        summary_df = pd.DataFrame(group_results)

        # Compute enhancement relative to baseline Cdl
        if baseline_cdl and not summary_df.empty:
            cdl_col = 'cdl_mean_uF_cm2' if electrode_area_cm2 else 'cdl_mean_uF'
            if cdl_col in summary_df.columns:
                for idx, row in summary_df.iterrows():
                    gname = row['group_name']
                    if gname in baseline_cdl and pd.notna(row.get(cdl_col)):
                        enh = row[cdl_col] / baseline_cdl[gname]
                        summary_df.at[idx, 'enhancement_mean'] = enh
                        summary_df.at[idx, 'enhancement_formatted'] = f"{enh:.1f}x"

        if export_manager and not summary_df.empty:
            export_manager.save_dataframe(
                summary_df, 'grouped_ecsa_results', subdir='data'
            )

        # Print summary
        print(f"\n{'=' * 50}")
        print("Group Analysis Summary:")
        for _, row in summary_df.iterrows():
            if 'cdl_formatted' in row:
                unit = 'uF/cm2' if electrode_area_cm2 else 'uF'
                print(f"  {row['group_name']}: Cdl = {row['cdl_formatted']} {unit}")
                if 'enhancement_formatted' in row and pd.notna(row.get('enhancement_formatted')):
                    print(f"    Enhancement = {row['enhancement_formatted']} (vs bare)")
                elif 'rf_formatted' in row and pd.notna(row.get('rf_formatted')):
                    print(f"    Roughness Factor = {row['rf_formatted']}")

        # Generate grouped comparison plot
        if specimen_cdl_data:
            fig_grouped = ECSAAnalyzer.plot_cdl_grouped(
                specimen_cdl_data, grouping,
                electrode_area_cm2=electrode_area_cm2,
                min_r_squared=min_r_squared,
                export_manager=export_manager,
            )
            figures['cdl_grouped'] = fig_grouped

        return summary_df, figures

    @staticmethod
    def plot_cdl_grouped(specimen_cdl_data: Dict[str, dict],
                         grouping: Dict[str, List[str]],
                         electrode_area_cm2: float = None,
                         min_r_squared: float = 0.0,
                         figsize: Tuple[int, int] = (8, 6),
                         export_manager=None) -> plt.Figure:
        """
        Grouped Cdl comparison plot (pooled style, like CA sensitivity).

        Shows individual specimen data points (Δi vs scan rate) colored by
        group, with a mean linear fit ± std shading per group.

        Args:
            specimen_cdl_data: {specimen: {'scan_rates': [...],
                'delta_values': [...], 'cdl_results': {...}}}
                as collected by batch_analyze_grouped.
            grouping: {group_name: [specimen_name, ...]}
            electrode_area_cm2: If provided, y-axis is current density (A/cm²)
            min_r_squared: Exclude specimens with R² below this from
                mean/std calculation (they are still plotted as open markers)
            figsize: Figure size
            export_manager: Optional ExportManager

        Returns:
            matplotlib Figure
        """
        fig, ax = plt.subplots(figsize=figsize)

        styles = [
            ('s', 'black'), ('o', 'red'), ('^', 'blue'),
            ('v', 'magenta'), ('D', 'green'), ('p', 'orange'),
            ('h', 'cyan'), ('*', 'brown'),
        ]

        max_rate = 0
        stats_lines = []

        for group_idx, (group_name, specimen_names) in enumerate(grouping.items()):
            marker, color = styles[group_idx % len(styles)]

            # Collect per-specimen slopes for mean ± std fit
            slopes = []
            intercepts = []

            for specimen in specimen_names:
                if specimen not in specimen_cdl_data:
                    continue

                data = specimen_cdl_data[specimen]
                rates = np.array(data['scan_rates'])
                deltas = np.array(data['delta_values'])
                r2 = data['cdl_results']['r_squared']
                max_rate = max(max_rate, rates.max())

                rates_mv = rates * 1000  # mV/s for display

                # Plot individual points
                if r2 >= min_r_squared:
                    ax.scatter(rates_mv, deltas, marker=marker, color=color,
                              s=35, edgecolors='black', linewidths=0.4,
                              alpha=0.6, zorder=3)
                    # Collect slope/intercept for group average
                    slope, intercept, _, _, _ = linregress(rates, deltas)
                    slopes.append(slope)
                    intercepts.append(intercept)
                else:
                    # Plot excluded specimens as open markers
                    ax.scatter(rates_mv, deltas, marker=marker, color='none',
                              s=35, edgecolors=color, linewidths=1.0,
                              alpha=0.5, zorder=3)

            # Plot mean fit line ± std shading
            if len(slopes) >= 1:
                mean_slope = np.mean(slopes)
                mean_intercept = np.mean(intercepts)

                fit_rates = np.linspace(0, max_rate * 1.1, 200)
                fit_rates_mv = fit_rates * 1000
                mean_line = mean_slope * fit_rates + mean_intercept

                ax.plot(fit_rates_mv, mean_line, color=color, linewidth=2,
                        label=group_name, zorder=4)

                if len(slopes) >= 2:
                    std_slope = np.std(slopes, ddof=1)
                    std_intercept = np.std(intercepts, ddof=1)
                    upper = (mean_slope + std_slope) * fit_rates + (mean_intercept + std_intercept)
                    lower = (mean_slope - std_slope) * fit_rates + (mean_intercept - std_intercept)
                    ax.fill_between(fit_rates_mv, lower, upper,
                                    color=color, alpha=0.15, zorder=1)

                # Compute Cdl from mean slope
                cdl_from_slope = abs(mean_slope) / 2.0
                if electrode_area_cm2 is not None:
                    cdl_display = cdl_from_slope * 1e6  # µF/cm²
                    if len(slopes) >= 2:
                        cdl_std = np.std(slopes, ddof=1) / 2.0 * 1e6
                        stats_lines.append(
                            f"{group_name}: Cdl = {cdl_display:.1f} ± {cdl_std:.1f} µF/cm²"
                            f" (n={len(slopes)})")
                    else:
                        stats_lines.append(
                            f"{group_name}: Cdl = {cdl_display:.1f} µF/cm²"
                            f" (n={len(slopes)})")
                else:
                    cdl_uF = cdl_from_slope * 1e6
                    stats_lines.append(
                        f"{group_name}: Cdl = {cdl_uF:.2f} µF (n={len(slopes)})")

        # Y-axis label
        if electrode_area_cm2 is not None:
            y_label = 'Δj (A/cm²)'
        else:
            y_label = 'Δi (A)'

        ax.set_xlabel('Scan Rate (mV/s)', fontsize=11)
        ax.set_ylabel(y_label, fontsize=11)
        ax.set_title('Cdl Comparison by Group', fontsize=12)
        ax.legend(loc='upper left', fontsize=9, framealpha=0.9)
        ax.grid(True, alpha=0.3, linestyle='--')

        # Stats annotation box
        if stats_lines:
            ax.text(0.98, 0.05, '\n'.join(stats_lines),
                    transform=ax.transAxes, fontsize=8,
                    verticalalignment='bottom', horizontalalignment='right',
                    bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

        plt.tight_layout()

        if export_manager:
            export_manager.save_figure(fig, 'cdl_grouped', subdir='plots')

            # Export pooled data points
            pooled_rows = []
            for group_name, specimen_names in grouping.items():
                for specimen in specimen_names:
                    if specimen not in specimen_cdl_data:
                        continue
                    data = specimen_cdl_data[specimen]
                    r2 = data['cdl_results']['r_squared']
                    for rate, delta in zip(data['scan_rates'],
                                           data['delta_values']):
                        pooled_rows.append({
                            'group': group_name,
                            'specimen': specimen,
                            'scan_rate_V_s': rate,
                            'delta_value': delta,
                            'r_squared': r2,
                        })
            if pooled_rows:
                export_manager.save_dataframe(
                    pd.DataFrame(pooled_rows),
                    'cdl_grouped_data', subdir='data')

        return fig

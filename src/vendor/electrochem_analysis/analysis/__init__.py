"""
Analysis modules for Electrochemistry

This package provides lightweight analyzers for EIS, CV, CIC, CA, and ECSA techniques.

Usage:
    from src.analysis import EISAnalyzer, CVAnalyzer, CoganCICAnalyzer, CAAnalyzer, CPAnalyzer, ECSAAnalyzer

    # EIS analysis
    eis_analyzer = EISAnalyzer(eis_data)
    rs, rct = eis_analyzer.calculate_impedance_parameters()
    fig = eis_analyzer.plot_nyquist()

    # CV analysis
    cv_analyzer = CVAnalyzer(cv_data)
    csc = cv_analyzer.calculate_csc(scan_rate=0.05, electrode_area=1.0)
    fig = cv_analyzer.plot_cv_with_cathodic_area()

    # CIC analysis (Cogan 2008 voltage transient method)
    cic_analyzer = CoganCICAnalyzer(cic_data)
    results = cic_analyzer.analyze_last_pulse(electrode_area=1.0)
    fig = cic_analyzer.plot_voltage_transient()

    # CA analysis (biosensor calibration)
    ca_analyzer = CAAnalyzer(ca_data)
    results = ca_analyzer.analyze_calibration(concentrations=[0.5, 1, 2, 5, 10, 20])
    fig = ca_analyzer.plot_calibration_curve()

    # CP analysis (chronopotentiometry — galvanostatic E vs t)
    cp_scans = filter_scans(load_psession('cp_run.pssession'), 'CP')
    summary_df, figures = CPAnalyzer.batch_analyze(cp_scans)

    # ECSA analysis (double-layer capacitance method)
    scans = load_psession('scan_rate_cvs.pssession')
    ecsa_analyzer = ECSAAnalyzer(scans)
    results = ecsa_analyzer.calculate_cdl(v_midpoint=0.40)
    fig = ecsa_analyzer.plot_cdl_fit()
"""

from .eis import EISAnalyzer, EISCircuitFitter
from .cv import CVAnalyzer
from .cic import CoganCICAnalyzer
from .ca import CAAnalyzer
from .cp import CPAnalyzer
from .ecsa import ECSAAnalyzer
from .smoothing import (
    plateau_average,
    moving_average,
    savgol_smooth,
    lowpass_butterworth,
    average_across_channels,
    dt_stats,
    detrend_and_smooth,
)
from .baseline import (
    compute_drift,
    compute_noise_stats,
    segmented_drift,
    detect_plateau_windows,
    fit_baseline_drift,
    flatten_signal,
    two_point_rotation_detrend,
)

# Alias for backwards compatibility
CICAnalyzer = CoganCICAnalyzer

__all__ = [
    'EISAnalyzer',
    'EISCircuitFitter',
    'CVAnalyzer',
    'CoganCICAnalyzer',
    'CICAnalyzer',  # Alias
    'CAAnalyzer',
    'CPAnalyzer',
    'ECSAAnalyzer',
    # Noise-reduction utilities (smoothing)
    'plateau_average',
    'moving_average',
    'savgol_smooth',
    'lowpass_butterworth',
    'average_across_channels',
    'dt_stats',
    'detrend_and_smooth',
    # Drift / baseline analysis
    'compute_drift',
    'compute_noise_stats',
    'segmented_drift',
    'detect_plateau_windows',
    'fit_baseline_drift',
    'flatten_signal',
    'two_point_rotation_detrend',
]

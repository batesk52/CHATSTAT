"""
Baseline drift and noise analysis for chronoamperometry traces.

Segmented drift, noise statistics, plateau detection, piecewise-linear
baseline fitting, and signal flattening. Supports the E055 revival
(Stabilization Time & Noise Filtering) by extending the original
pre-addition-only drift analysis to inter-addition and post-addition
segments, and providing a detrending utility.

The ``piecewise_linear`` model reports per-plateau slopes AND applies a
single median-slope global drift correction when flattening; plateau
means (and therefore step heights) are preserved. Per-plateau slopes
and intercepts are still returned for diagnostic purposes (downstream
analyses consume them), but only a scalar ``m_global`` slope is
subtracted so inter-plateau step transitions are not eaten.

Typical usage:
    from src.analysis.baseline import (
        compute_drift, segmented_drift, compute_noise_stats,
        detect_plateau_windows, fit_baseline_drift, flatten_signal,
    )

    drift = compute_drift(df, t_start=t_first_add - 120, t_end=t_first_add - 2)
    segments = [("pre_last_2min", t_first_add - 120, t_first_add - 2),
                ("plateau_1",     t_adds[0] + 60,    t_adds[1] - 15),
                ("plateau_2",     t_adds[1] + 60,    t_adds[2] - 15)]
    seg_results = segmented_drift(df, segments)

    plateau_windows = detect_plateau_windows(df, addition_times=t_adds, settle_s=60)
    fit = fit_baseline_drift(df, plateau_windows, model="piecewise_linear")
    df_flat = flatten_signal(df, fit)
"""

import logging
from typing import Dict, List, Tuple, Optional, Literal

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TIME_COL = "Time (s)"
CURRENT_COL = "Current (A)"
FLATTENED_COL = "Current_flattened_nA"


def _require_columns(df: pd.DataFrame, required=(TIME_COL, CURRENT_COL)) -> None:
    """Raise ``KeyError`` if any required column is missing from ``df``.

    Args:
        df: DataFrame to validate.
        required: Iterable of column names that must be present.

    Raises:
        KeyError: If one or more required columns are missing.
    """
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(
            f"DataFrame missing required column(s) {missing}; "
            f"got columns {list(df.columns)}"
        )


def compute_drift(df: pd.DataFrame, t_start: float, t_end: float) -> Dict[str, float]:
    """Linear-fit drift of current from t_start to t_end.

    Reimplementation of the inline ``compute_drift`` used in the E049/E050/E054/E069
    notebooks, generalized to accept both bounds (the inline version took only t_end
    and defaulted t_start=0).

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        t_start: Window start (seconds).
        t_end: Window end (seconds).

    Returns:
        Dict with keys 'slope_nA_per_min', 'intercept_nA', 'total_delta_nA',
        'n_points'. ``total_delta_nA`` is the raw difference between the last
        and first sample in the window (``i_na[-1] - i_na[0]``); it is NOT a
        fitted-line prediction (use ``slope_nA_per_min * (t_end-t_start)/60``
        for that).
    """
    _require_columns(df)
    mask = (df[TIME_COL] >= t_start) & (df[TIME_COL] <= t_end)
    sub = df.loc[mask]
    if len(sub) < 3:
        return {
            "slope_nA_per_min": np.nan,
            "intercept_nA": np.nan,
            "total_delta_nA": np.nan,
            "n_points": len(sub),
        }
    t = sub[TIME_COL].values
    i_na = sub[CURRENT_COL].values * 1e9
    slope, intercept = np.polyfit(t, i_na, 1)  # nA/s, nA
    return {
        "slope_nA_per_min": slope * 60,
        "intercept_nA": intercept,
        "total_delta_nA": i_na[-1] - i_na[0],
        "n_points": len(sub),
    }


def compute_noise_stats(df: pd.DataFrame, t_start: float, t_end: float) -> Dict[str, float]:
    """Noise statistics over a window, with drift removed.

    Removes linear drift before computing std/peak-to-peak so noise is not
    inflated by a sloping baseline. RMS is std of the detrended residual.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        t_start: Window start (seconds).
        t_end: Window end (seconds).

    Returns:
        Dict with keys 'std_nA', 'peak_to_peak_nA', 'rms_nA', 'n_points'.
        All values NaN if fewer than 3 points in the window.
    """
    _require_columns(df)
    mask = (df[TIME_COL] >= t_start) & (df[TIME_COL] <= t_end)
    sub = df.loc[mask]
    if len(sub) < 3:
        return {"std_nA": np.nan, "peak_to_peak_nA": np.nan, "rms_nA": np.nan, "n_points": len(sub)}
    t = sub[TIME_COL].values
    i_na = sub[CURRENT_COL].values * 1e9
    slope, intercept = np.polyfit(t, i_na, 1)
    residual = i_na - (slope * t + intercept)
    return {
        "std_nA": float(np.std(residual, ddof=1)),
        "peak_to_peak_nA": float(np.ptp(residual)),
        "rms_nA": float(np.sqrt(np.mean(residual**2))),
        "n_points": len(sub),
    }


def segmented_drift(
    df: pd.DataFrame,
    segments: List[Tuple[str, float, float]],
) -> List[Dict[str, float]]:
    """Compute drift on each of a list of named segments.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        segments: List of (label, t_start, t_end) tuples.

    Returns:
        List of dicts, one per segment, each with keys 'segment_label',
        'segment_start_s', 'segment_end_s', plus the compute_drift and
        compute_noise_stats outputs merged in.
    """
    _require_columns(df)
    rows = []
    for label, t_start, t_end in segments:
        drift = compute_drift(df, t_start, t_end)
        noise = compute_noise_stats(df, t_start, t_end)
        rows.append({
            "segment_label": label,
            "segment_start_s": t_start,
            "segment_end_s": t_end,
            **drift,
            "std_nA": noise["std_nA"],
            "peak_to_peak_nA": noise["peak_to_peak_nA"],
        })
    return rows


def detect_plateau_windows(
    addition_times: List[float],
    t_end: float,
    settle_s: float = 60.0,
    guard_s: float = 15.0,
    t_first_start: Optional[float] = None,
) -> List[Tuple[str, float, float]]:
    """Return plateau windows between consecutive addition events.

    Produces one window before the first addition (optional), one between each
    pair of additions, and one after the last addition. Each plateau starts
    ``settle_s`` after the preceding addition (enzyme response settled) and ends
    ``guard_s`` before the next addition (avoids the incoming step).

    Args:
        addition_times: Sorted list of addition timestamps (seconds).
        t_end: End of the recording (seconds).
        settle_s: Seconds after an addition before the plateau starts.
        guard_s: Seconds of guard before the next addition.
        t_first_start: If provided, include a pre-addition plateau
            (t_first_start, addition_times[0] - guard_s). Typically passed
            as ``addition_times[0] - 120`` to get the "last 2 min" window.

    Returns:
        List of (label, t_start, t_end) tuples. Windows with t_end <= t_start
        are dropped.
    """
    windows: List[Tuple[str, float, float]] = []
    if t_first_start is not None and addition_times:
        windows.append(("plateau_pre", t_first_start, addition_times[0] - guard_s))
    for i in range(len(addition_times) - 1):
        start = addition_times[i] + settle_s
        stop = addition_times[i + 1] - guard_s
        windows.append((f"plateau_{i + 1}", start, stop))
    if addition_times:
        last_start = addition_times[-1] + settle_s
        windows.append((f"plateau_{len(addition_times)}_post", last_start, t_end))
    return [(lbl, s, e) for (lbl, s, e) in windows if e > s]


def fit_baseline_drift(
    df: pd.DataFrame,
    plateau_windows: List[Tuple[str, float, float]],
    model: Literal["linear", "piecewise_linear", "exponential"] = "piecewise_linear",
) -> Dict[str, object]:
    """Fit a baseline drift model on plateau windows.

    - ``linear``: single slope/intercept over the pooled plateau data.
    - ``piecewise_linear`` (default): per-plateau slope/intercept, linearly
      interpolated across the addition transient gaps. Outside the first and
      last plateau, the baseline is extrapolated with the nearest plateau's
      slope.
    - ``exponential``: two-parameter i0 + A * exp(-t/tau) fit on pooled
      plateau data.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        plateau_windows: Output of detect_plateau_windows.
        model: Baseline model.

    Returns:
        Dict with 'model' and 'params' (model-specific). For piecewise_linear,
        ``params`` is a list of dicts with 't_start', 't_end', 'slope_nA_per_s',
        'intercept_nA'. For linear/exponential, params is a single dict. For
        piecewise_linear the result also carries ``m_global_nA_per_s``: the
        **unweighted median** of per-plateau slopes (plateaus with ``nan``
        slopes are excluded from the median but are not weighted by sample
        count).
    """
    _require_columns(df)
    if not plateau_windows:
        return {"model": model, "params": None}

    if model == "linear":
        all_t = []
        all_i = []
        for _, t_start, t_end in plateau_windows:
            mask = (df[TIME_COL] >= t_start) & (df[TIME_COL] <= t_end)
            sub = df.loc[mask]
            all_t.append(sub[TIME_COL].values)
            all_i.append(sub[CURRENT_COL].values * 1e9)
        t = np.concatenate(all_t)
        i_na = np.concatenate(all_i)
        if len(t) < 3:
            return {"model": "linear", "params": None}
        slope, intercept = np.polyfit(t, i_na, 1)
        return {"model": "linear", "params": {"slope_nA_per_s": float(slope), "intercept_nA": float(intercept)}}

    if model == "piecewise_linear":
        segments = []
        for label, t_start, t_end in plateau_windows:
            mask = (df[TIME_COL] >= t_start) & (df[TIME_COL] <= t_end)
            sub = df.loc[mask]
            if len(sub) < 3:
                segments.append({
                    "label": label, "t_start": t_start, "t_end": t_end,
                    "slope_nA_per_s": np.nan, "intercept_nA": np.nan,
                    "n_points": len(sub),
                })
                continue
            t = sub[TIME_COL].values
            i_na = sub[CURRENT_COL].values * 1e9
            slope, intercept = np.polyfit(t, i_na, 1)
            segments.append({
                "label": label, "t_start": t_start, "t_end": t_end,
                "slope_nA_per_s": float(slope), "intercept_nA": float(intercept),
                "n_points": len(sub),
            })

        # Global drift slope: unweighted median of valid per-plateau slopes.
        # Used by flatten_signal to subtract only a scalar drift line so
        # inter-plateau step heights are preserved.
        valid_slopes = [s["slope_nA_per_s"] for s in segments
                        if not np.isnan(s["slope_nA_per_s"])]
        if valid_slopes:
            m_global = float(np.median(valid_slopes))
        else:
            m_global = np.nan

        return {
            "model": "piecewise_linear",
            "params": segments,
            "m_global_nA_per_s": m_global,
        }

    if model == "exponential":
        from scipy.optimize import curve_fit

        all_t = []
        all_i = []
        for _, t_start, t_end in plateau_windows:
            mask = (df[TIME_COL] >= t_start) & (df[TIME_COL] <= t_end)
            sub = df.loc[mask]
            all_t.append(sub[TIME_COL].values)
            all_i.append(sub[CURRENT_COL].values * 1e9)
        t = np.concatenate(all_t)
        i_na = np.concatenate(all_i)
        if len(t) < 4:
            return {"model": "exponential", "params": None}

        def exp_model(t, i0, A, tau):
            return i0 + A * np.exp(-t / tau)

        try:
            p0 = [i_na.mean(), i_na[0] - i_na[-1], max(1.0, t[-1] - t[0])]
            popt, _ = curve_fit(exp_model, t, i_na, p0=p0, maxfev=5000)
            return {"model": "exponential", "params": {
                "i0_nA": float(popt[0]), "A_nA": float(popt[1]), "tau_s": float(popt[2]),
            }}
        except (RuntimeError, ValueError) as e:
            logger.warning("Exponential baseline fit failed: %s", e)
            return {"model": "exponential", "params": None}

    raise ValueError(f"Unknown baseline model: {model!r}")


def _piecewise_baseline_at(t: np.ndarray, segments: List[Dict]) -> np.ndarray:
    """Evaluate piecewise-linear baseline at arbitrary times.

    Within a plateau segment, uses that segment's slope + intercept.
    Between segments, linearly interpolates between adjacent segments' values
    at their nearer endpoints. Before the first segment, extrapolates using
    the first segment's slope. After the last segment, extrapolates using
    the last segment's slope.
    """
    valid = [s for s in segments if not np.isnan(s["slope_nA_per_s"])]
    if not valid:
        return np.full_like(t, np.nan, dtype=float)

    baseline = np.full_like(t, np.nan, dtype=float)
    for seg in valid:
        in_seg = (t >= seg["t_start"]) & (t <= seg["t_end"])
        baseline[in_seg] = seg["slope_nA_per_s"] * t[in_seg] + seg["intercept_nA"]

    valid_sorted = sorted(valid, key=lambda s: s["t_start"])
    for a, b in zip(valid_sorted[:-1], valid_sorted[1:]):
        gap = (t > a["t_end"]) & (t < b["t_start"])
        if not gap.any():
            continue
        i_end_a = a["slope_nA_per_s"] * a["t_end"] + a["intercept_nA"]
        i_start_b = b["slope_nA_per_s"] * b["t_start"] + b["intercept_nA"]
        frac = (t[gap] - a["t_end"]) / (b["t_start"] - a["t_end"])
        baseline[gap] = i_end_a + frac * (i_start_b - i_end_a)

    first = valid_sorted[0]
    before = t < first["t_start"]
    if before.any():
        baseline[before] = first["slope_nA_per_s"] * t[before] + first["intercept_nA"]

    last = valid_sorted[-1]
    after = t > last["t_end"]
    if after.any():
        baseline[after] = last["slope_nA_per_s"] * t[after] + last["intercept_nA"]

    return baseline


def two_point_rotation_detrend(
    df: pd.DataFrame,
    t_anchor_latest: float,
    window_s: float = 300.0,
    avg_window_s: float = 5.0,
) -> Dict[str, object]:
    """Rotate the trace so two anchor points land at y=0.

    Point A: mean of samples in a small window ending at t_anchor_latest (default 5 s)
    Point B: mean of samples in a ~5 s window centered at (t_anchor_latest - window_s)

    Fits a line through (t_A, y_A) and (t_B, y_B) and subtracts it from the raw
    current. Both anchor points land exactly at y=0 after subtraction. Step
    responses and plateau means are preserved -- only a single line is removed.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        t_anchor_latest: timestamp of Point A (typically t_first_addition - 1s).
        window_s: separation between A and B (default 300 s). Must be positive.
        avg_window_s: width of the averaging window around each anchor point.
            Must satisfy ``0 < avg_window_s < window_s``.

    Returns:
        Dict with keys:
            'detrended_nA': numpy array, same length as df.
            'slope_nA_per_s': fitted slope.
            'intercept_nA': fitted intercept.
            'anchor_A': (t_A, y_A) tuple.
            'anchor_B': (t_B, y_B) tuple.
        All numeric values NaN if either anchor window has <1 sample.

    Raises:
        ValueError: If ``window_s <= 0``, ``avg_window_s <= 0``, or
            ``avg_window_s >= window_s``. Also raised if the B anchor
            ``t_anchor_latest - window_s`` falls before the first sample.
        KeyError: If ``df`` is missing the required time/current columns.

    ## Offset convention
    The detrended trace is anchored so that **both** ``t_anchor_latest`` and
    ``t_anchor_latest - window_s`` are zero. The first sample of the trace is
    generally NOT zero after detrending. This is a different convention from
    :func:`flatten_signal` with ``model='piecewise_linear'``, which anchors
    the detrended trace at the first sample (``t[0]``).

    ## Caveats
    This method assumes drift is approximately **linear** over the anchor
    window ``[t_anchor_latest - window_s, t_anchor_latest]``. Traces that
    are still exponentially equilibrating (e.g. freshly-immersed electrodes)
    will be mis-detrended because a single line cannot track an exponential
    settling curve. Before applying ``two_point_rotation_detrend``, check
    :func:`segmented_drift` on the pre-addition segments to confirm the
    drift slope is roughly constant across windows.
    """
    _require_columns(df)

    if window_s <= 0:
        raise ValueError(f"window_s must be positive, got {window_s}")
    if avg_window_s <= 0 or avg_window_s >= window_s:
        raise ValueError(
            f"avg_window_s must be in (0, window_s); got "
            f"avg_window_s={avg_window_s}, window_s={window_s}"
        )

    t_B = float(t_anchor_latest - window_s)
    t_min = float(df[TIME_COL].min())
    if t_B < t_min:
        raise ValueError(
            f"anchor B at t={t_B} falls before trace start t={t_min}; "
            f"reduce window_s or check t_anchor_latest"
        )

    t = df[TIME_COL].values
    raw_nA = df[CURRENT_COL].values * 1e9

    t_A = float(t_anchor_latest)
    # t_B already computed above for validation

    # Point A: window of width avg_window_s ending at t_A (i.e. [t_A - avg_window_s, t_A])
    mask_A = (t >= t_A - avg_window_s) & (t <= t_A)
    # Point B: ~avg_window_s window centered on t_B
    mask_B = (t >= t_B - avg_window_s / 2.0) & (t <= t_B + avg_window_s / 2.0)

    if mask_A.sum() < 1 or mask_B.sum() < 1:
        logger.warning(
            "two_point_rotation_detrend: empty anchor window (A=%d, B=%d); NaN output",
            int(mask_A.sum()), int(mask_B.sum()),
        )
        return {
            "detrended_nA": np.full_like(raw_nA, np.nan, dtype=float),
            "slope_nA_per_s": np.nan,
            "intercept_nA": np.nan,
            "anchor_A": (t_A, np.nan),
            "anchor_B": (t_B, np.nan),
        }

    y_A = float(np.mean(raw_nA[mask_A]))
    y_B = float(np.mean(raw_nA[mask_B]))

    if t_A == t_B:
        logger.warning("two_point_rotation_detrend: t_A == t_B, cannot fit line; NaN output")
        return {
            "detrended_nA": np.full_like(raw_nA, np.nan, dtype=float),
            "slope_nA_per_s": np.nan,
            "intercept_nA": np.nan,
            "anchor_A": (t_A, y_A),
            "anchor_B": (t_B, y_B),
        }

    slope = (y_A - y_B) / (t_A - t_B)
    intercept = y_A - slope * t_A
    detrended = raw_nA - (slope * t + intercept)

    return {
        "detrended_nA": detrended,
        "slope_nA_per_s": float(slope),
        "intercept_nA": float(intercept),
        "anchor_A": (t_A, y_A),
        "anchor_B": (t_B, y_B),
    }


def flatten_signal(df: pd.DataFrame, baseline_fit: Dict[str, object]) -> pd.DataFrame:
    """Subtract the fitted baseline drift from the raw current.

    Returns a copy of ``df`` with two extra columns:
    - ``Current_baseline_nA``: the drift baseline subtracted at each timestamp.
    - ``Current_flattened_nA``: raw current (nA) minus the baseline.

    The raw ``Current (A)`` column is preserved unchanged so the existing
    CAAnalyzer workflow still works on the original data.

    **piecewise_linear model:** subtracts only a global scalar drift line
    ``m_global * (t - t_anchor)``, where ``m_global`` is the median of the
    per-plateau slopes and ``t_anchor`` is the first timestamp in ``df``.
    No intercept, no plateau-specific offsets. This preserves plateau means
    and therefore step heights between additions — subtracting per-plateau
    offsets or a piecewise baseline would eat the step response in the
    inter-plateau transition gaps.

    **linear / exponential models:** subtract the fitted baseline as-is.

    ## Offset convention
    For ``model='piecewise_linear'`` the detrended trace is anchored at the
    **first sample** (``t[0]``): after subtraction, ``Current_flattened_nA``
    at ``t[0]`` equals the raw current at ``t[0]`` (the subtraction is
    ``m_global * (t - t[0])``, which is zero at ``t[0]``). This is a
    different convention from :func:`two_point_rotation_detrend`, which
    anchors the detrended trace at ``t_anchor_latest`` and
    ``t_anchor_latest - window_s`` instead.

    For ``model='linear'`` and ``model='exponential'`` the baseline is
    subtracted as-is, so the detrended trace carries the fitted
    intercept / asymptote.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        baseline_fit: Output of fit_baseline_drift.

    Returns:
        Copy of df with baseline and flattened columns added. If the fit failed
        (params is None), both new columns are NaN.
    """
    _require_columns(df)
    out = df.copy()
    t = out[TIME_COL].values
    raw_na = out[CURRENT_COL].values * 1e9

    model = baseline_fit.get("model")
    params = baseline_fit.get("params")

    if params is None:
        out["Current_baseline_nA"] = np.nan
        out[FLATTENED_COL] = np.nan
        return out

    if model == "linear":
        baseline = params["slope_nA_per_s"] * t + params["intercept_nA"]
    elif model == "piecewise_linear":
        m_global = baseline_fit.get("m_global_nA_per_s", np.nan)
        if m_global is None or (isinstance(m_global, float) and np.isnan(m_global)):
            out["Current_baseline_nA"] = np.nan
            out[FLATTENED_COL] = np.nan
            return out
        t_anchor = float(t[0]) if len(t) else 0.0
        baseline = m_global * (t - t_anchor)
    elif model == "exponential":
        baseline = params["i0_nA"] + params["A_nA"] * np.exp(-t / params["tau_s"])
    else:
        raise ValueError(f"Unknown baseline model: {model!r}")

    out["Current_baseline_nA"] = baseline
    out[FLATTENED_COL] = raw_na - baseline
    return out

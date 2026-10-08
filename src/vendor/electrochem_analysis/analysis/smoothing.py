"""
Noise-reduction utilities for MUX chronoamperometry data.

Distinct from ``src.analysis.baseline``, which handles *baseline drift*
correction. This module handles *noise reduction* -- per-plateau averaging,
rolling/savgol smoothing, digital lowpass filtering, and multi-channel
averaging for SNR improvement.

All functions assume the standard repo data structure: a per-channel
DataFrame with columns ``Time (s)`` (seconds, monotonic but not strictly
uniform because of MUX jitter) and ``Current (A)`` (amps). Functions that
are sensitive to uniform sampling (e.g. ``lowpass_butterworth``) interpolate
onto a uniform time grid internally.

Sampling rate context (for reference):
    - E049/E054/E069: ``t_interval=5 s``, 4-channel MUX round ~22 s.
      Per-channel rate ~0.045-0.125 Hz; Nyquist ~0.02-0.06 Hz.
    - E050 try 2: ``t_interval=2 s``. Per-channel rate ~0.4-0.5 Hz.
    - Digital lowpass at cutoff < ~0.05 Hz is feasible for either case.

Typical usage:
    from src.analysis.smoothing import (
        plateau_average, moving_average, savgol_smooth,
        lowpass_butterworth, average_across_channels, dt_stats,
    )

    stats = dt_stats(df)  # quick: what's the sample rate?
    plateau_rows = plateau_average(df, plateau_windows, tail_s=60.0)
    df_smooth = moving_average(df, window_s=60.0)
    df_sg = savgol_smooth(df, window_s=60.0, polyorder=2)
    df_lp = lowpass_butterworth(df, cutoff_hz=0.01, order=4)
    pooled = average_across_channels(scans, blade_channels=[1, 2, 3, 4])
"""

import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TIME_COL = "Time (s)"
CURRENT_COL = "Current (A)"
SMOOTHED_COL = "Current_smoothed_nA"


def _require_columns(df: pd.DataFrame, required=(TIME_COL, CURRENT_COL)) -> None:
    """Raise ``KeyError`` if any required column is missing from ``df``.

    Per-module copy of the baseline.py helper — kept local so this module
    does not depend on baseline.py's internals.

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


def dt_stats(df: pd.DataFrame) -> Dict[str, float]:
    """Summarize time-step statistics of a per-channel trace.

    Useful for quickly identifying whether a scan was recorded at
    ``t_interval=2 s`` vs ``5 s`` and for deriving a sample-rate estimate
    for downstream filtering.

    Args:
        df: DataFrame with a 'Time (s)' column.

    Returns:
        Dict with keys 'dt_median_s', 'dt_mean_s', 'dt_std_s',
        'sample_rate_hz', 'n_points'. All sampling-rate fields are NaN if
        fewer than 2 points are present.
    """
    _require_columns(df, required=(TIME_COL,))
    n = len(df)
    if n < 2:
        logger.warning("dt_stats: fewer than 2 points (n=%d); returning NaN fields", n)
        return {
            "dt_median_s": np.nan,
            "dt_mean_s": np.nan,
            "dt_std_s": np.nan,
            "sample_rate_hz": np.nan,
            "n_points": n,
        }
    t = df[TIME_COL].values
    dt = np.diff(t)
    dt_median = float(np.median(dt))
    return {
        "dt_median_s": dt_median,
        "dt_mean_s": float(np.mean(dt)),
        "dt_std_s": float(np.std(dt, ddof=1)) if len(dt) > 1 else 0.0,
        "sample_rate_hz": float(1.0 / dt_median) if dt_median > 0 else np.nan,
        "n_points": n,
    }


def plateau_average(
    df: pd.DataFrame,
    plateau_windows: List[Tuple[str, float, float]],
    tail_s: Optional[float] = None,
) -> List[Dict[str, object]]:
    """Aggregate current over each plateau window.

    For each plateau, computes the mean, median, std, and point count of the
    current. If ``tail_s`` is provided, aggregates only the last ``tail_s``
    seconds of the plateau (useful when the early portion is still settling).

    Windows are interpreted as **half-open** ``[t_start, t_end)`` on the time
    axis so that samples falling exactly on a boundary are not
    double-counted between adjacent plateaus.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        plateau_windows: List of ``(label, t_start, t_end)`` tuples (same shape
            as ``detect_plateau_windows`` output from baseline.py).
        tail_s: If given, only the last ``tail_s`` seconds of each plateau
            are aggregated. If None, the full window is used.

    Returns:
        List of dicts, one per plateau, each with keys 'label', 't_start',
        't_end', 'current_mean_nA', 'current_median_nA', 'current_std_nA',
        'n_points', 'window_used_s'. Fields are NaN (except 'n_points' and
        'window_used_s') if the window contains no samples.
    """
    _require_columns(df)
    rows: List[Dict[str, object]] = []
    for label, t_start, t_end in plateau_windows:
        if tail_s is not None and tail_s > 0:
            effective_start = max(t_start, t_end - float(tail_s))
        else:
            effective_start = t_start
        effective_end = t_end

        # Half-open [start, end) so shared boundary samples aren't
        # double-counted between adjacent plateaus.
        mask = (df[TIME_COL] >= effective_start) & (df[TIME_COL] < effective_end)
        sub = df.loc[mask]
        n = len(sub)
        if n == 0:
            logger.warning(
                "plateau_average: no samples in window %r [%.1f, %.1f]",
                label, effective_start, effective_end,
            )
            rows.append({
                "label": label,
                "t_start": float(t_start),
                "t_end": float(t_end),
                "current_mean_nA": np.nan,
                "current_median_nA": np.nan,
                "current_std_nA": np.nan,
                "n_points": 0,
                "window_used_s": (float(effective_start), float(effective_end)),
            })
            continue

        i_na = sub[CURRENT_COL].values * 1e9
        rows.append({
            "label": label,
            "t_start": float(t_start),
            "t_end": float(t_end),
            "current_mean_nA": float(np.mean(i_na)),
            "current_median_nA": float(np.median(i_na)),
            "current_std_nA": float(np.std(i_na, ddof=1)) if n > 1 else np.nan,
            "n_points": int(n),
            "window_used_s": (float(effective_start), float(effective_end)),
        })
    return rows


def moving_average(
    df: pd.DataFrame,
    window_s: float,
    center: bool = True,
) -> pd.DataFrame:
    """Rolling (moving-average) smoother on a per-channel current trace.

    The window is specified in seconds and converted to a sample count using
    the median time step. End-effects are handled with ``min_periods=1`` so
    no NaNs appear at the boundaries.

    The raw ``Current (A)`` column is preserved; a new ``Current_smoothed_nA``
    column is added (in nanoamps).

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        window_s: Window length in seconds.
        center: Whether to center the window on each point. Default True.

    Returns:
        Copy of ``df`` with a new 'Current_smoothed_nA' column. If fewer
        than 2 points are present, the smoothed column is NaN.
    """
    _require_columns(df)
    out = df.copy()
    n = len(out)
    if n < 2:
        logger.warning("moving_average: fewer than 2 points (n=%d); NaN output", n)
        out[SMOOTHED_COL] = np.nan
        return out

    stats = dt_stats(out)
    dt_median = stats["dt_median_s"]
    if not (dt_median and dt_median > 0):
        logger.warning(
            "moving_average: invalid median dt (%.4f); NaN output", dt_median,
        )
        out[SMOOTHED_COL] = np.nan
        return out

    n_samples = max(1, int(round(window_s / dt_median)))
    effective_window_s = n_samples * dt_median
    if window_s > 0 and abs(effective_window_s - window_s) / window_s > 0.05:
        logger.warning(
            "moving_average: requested window_s=%.2f rounded to %d samples "
            "(effective %.2fs @ dt=%.2fs)",
            window_s, n_samples, effective_window_s, dt_median,
        )

    i_na = out[CURRENT_COL].values * 1e9
    smoothed = (
        pd.Series(i_na)
        .rolling(window=n_samples, center=center, min_periods=1)
        .mean()
        .values
    )
    out[SMOOTHED_COL] = smoothed
    return out


def savgol_smooth(
    df: pd.DataFrame,
    window_s: float,
    polyorder: int = 2,
) -> pd.DataFrame:
    """Savitzky-Golay smoother on a per-channel current trace.

    Converts ``window_s`` to a sample count using the median time step,
    forces the window length to odd (scipy requirement), and clamps to
    ``>= polyorder + 2`` samples. Preserves local polynomial features
    better than a simple moving average -- better for preserving step
    edges.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        window_s: Window length in seconds.
        polyorder: Polynomial order for the local fit. Default 2.

    Returns:
        Copy of ``df`` with a new 'Current_smoothed_nA' column. If there
        are too few points to satisfy the window/polyorder constraint, the
        smoothed column is NaN.
    """
    from scipy.signal import savgol_filter

    _require_columns(df)
    out = df.copy()
    n = len(out)
    if n < polyorder + 2:
        logger.warning(
            "savgol_smooth: n=%d < polyorder+2=%d; NaN output", n, polyorder + 2,
        )
        out[SMOOTHED_COL] = np.nan
        return out

    stats = dt_stats(out)
    dt_median = stats["dt_median_s"]
    if not (dt_median and dt_median > 0):
        logger.warning("savgol_smooth: invalid median dt (%.4f); NaN output", dt_median)
        out[SMOOTHED_COL] = np.nan
        return out

    window_samples = max(1, int(round(window_s / dt_median)))
    # savgol requires odd window length
    if window_samples % 2 == 0:
        window_samples += 1
    # Minimum window: polyorder + 2 (scipy needs window_length > polyorder
    # strictly; polyorder + 2 gives a safe cushion), rounded up to odd.
    min_window = polyorder + 2
    if min_window % 2 == 0:
        min_window += 1
    # scipy requires window_length > polyorder (strictly); clamp up to min_window
    # if the requested window is too small.
    if window_samples < min_window:
        logger.warning(
            "savgol_smooth: window_samples=%d < min_window=%d (scipy requires "
            "window_length > polyorder=%d); clamping to min_window",
            window_samples, min_window, polyorder,
        )
        window_samples = min_window
    # Cannot exceed n (savgol will raise)
    if window_samples > n:
        logger.warning(
            "savgol_smooth: window_s=%.2f (%d samples) exceeds trace length %d; "
            "clamping",
            window_s, window_samples, n,
        )
        # Clamp to the largest odd value <= n
        window_samples = n if n % 2 == 1 else n - 1
        if window_samples <= polyorder:
            logger.warning(
                "savgol_smooth: cannot satisfy window_length > polyorder "
                "(n=%d, polyorder=%d); NaN output",
                n, polyorder,
            )
            out[SMOOTHED_COL] = np.nan
            return out

    i_na = out[CURRENT_COL].values * 1e9
    smoothed = savgol_filter(i_na, window_length=window_samples, polyorder=polyorder)
    out[SMOOTHED_COL] = smoothed
    return out


def lowpass_butterworth(
    df: pd.DataFrame,
    cutoff_hz: float,
    order: int = 4,
) -> pd.DataFrame:
    """Digital Butterworth lowpass filter with zero-phase (filtfilt).

    Because MUX introduces time-step jitter, the signal is first interpolated
    onto a uniform grid at the median sample rate, filtered, then interpolated
    back to the original timestamps.

    Fails loud with ``ValueError`` if ``cutoff_hz >= nyquist`` rather than
    silently producing garbage.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        cutoff_hz: Lowpass cutoff frequency in Hz.
        order: Butterworth filter order. Default 4.

    Returns:
        Copy of ``df`` with a new 'Current_smoothed_nA' column (filtered
        current in nanoamps). The raw column is preserved.

    Raises:
        ValueError: If ``cutoff_hz`` is at or above the Nyquist frequency.
    """
    from scipy.signal import butter, filtfilt

    _require_columns(df)
    out = df.copy()
    n = len(out)
    if n < max(9, 3 * (order + 1)):
        # filtfilt needs padding ~ 3*(max(len(a), len(b)) - 1). Be conservative.
        logger.warning(
            "lowpass_butterworth: too few points (n=%d) for order=%d; NaN output",
            n, order,
        )
        out[SMOOTHED_COL] = np.nan
        return out

    stats = dt_stats(out)
    dt_median = stats["dt_median_s"]
    if not (dt_median and dt_median > 0):
        logger.warning(
            "lowpass_butterworth: invalid median dt (%.4f); NaN output", dt_median,
        )
        out[SMOOTHED_COL] = np.nan
        return out

    # MUX time-step jitter sanity check. Butterworth filtering assumes uniform
    # sampling; high-jitter inputs get interpolated onto a uniform grid below,
    # but if jitter is extreme the resampling itself distorts the spectrum.
    dt_values = np.diff(out[TIME_COL].values)
    if len(dt_values) > 0:
        dt_std = float(np.std(dt_values))
        if dt_median > 0 and dt_std / dt_median > 0.25:
            logger.warning(
                "lowpass_butterworth: MUX time jitter is high "
                "(dt_std/dt_median=%.2f); uniform-grid interpolation may "
                "distort the filter response",
                dt_std / dt_median,
            )

    fs = 1.0 / dt_median
    nyquist = fs / 2.0
    if cutoff_hz >= nyquist:
        raise ValueError(
            f"lowpass_butterworth: cutoff_hz={cutoff_hz:.4f} must be strictly "
            f"less than Nyquist={nyquist:.4f} Hz (fs={fs:.4f} Hz)."
        )
    if cutoff_hz <= 0:
        raise ValueError(
            f"lowpass_butterworth: cutoff_hz={cutoff_hz} must be positive."
        )

    t_raw = out[TIME_COL].values.astype(float)
    i_na_raw = out[CURRENT_COL].values.astype(float) * 1e9

    # Build a uniform time grid spanning the original times
    t_uniform = np.arange(t_raw[0], t_raw[-1] + dt_median / 2.0, dt_median)
    if len(t_uniform) < max(9, 3 * (order + 1)):
        logger.warning(
            "lowpass_butterworth: uniform grid too short (%d); NaN output",
            len(t_uniform),
        )
        out[SMOOTHED_COL] = np.nan
        return out

    i_uniform = np.interp(t_uniform, t_raw, i_na_raw)

    # Design filter and apply zero-phase
    Wn = cutoff_hz / nyquist
    b, a = butter(N=order, Wn=Wn, btype="low", analog=False)
    # filtfilt default padlen is 3*max(len(a), len(b)); ensure signal long enough
    padlen_default = 3 * max(len(a), len(b))
    if len(i_uniform) <= padlen_default:
        padlen = max(0, len(i_uniform) - 1)
        filtered_uniform = filtfilt(b, a, i_uniform, padlen=padlen)
    else:
        filtered_uniform = filtfilt(b, a, i_uniform)

    # Interpolate back onto the original (possibly jittered) timestamps
    filtered_on_raw = np.interp(t_raw, t_uniform, filtered_uniform)
    out[SMOOTHED_COL] = filtered_on_raw
    return out


def detrend_and_smooth(
    df: pd.DataFrame,
    t_first_addition_s: float,
    rotation_window_s: float = 300.0,
    rotation_avg_window_s: float = 5.0,
    ma_window_s: float = 15.0,
) -> pd.DataFrame:
    """Apply the recommended CA processing pipeline.

    Chain:
        1. ``two_point_rotation_detrend(window_s=rotation_window_s)`` -- removes
           linear drift estimated from two anchor points spanning
           ``rotation_window_s`` just before ``t_first_addition_s``.
        2. ``moving_average(window_s=ma_window_s)`` -- reduces within-plateau
           noise.

    Both rotation anchors land at y=0 by construction; step heights (the
    signal for calibration) are preserved.

    Args:
        df: DataFrame with 'Time (s)' and 'Current (A)' columns.
        t_first_addition_s: timestamp (s) of the first analyte addition.
            The detrend's Point A is anchored at ``t_first_addition_s - 1``.
        rotation_window_s: separation between anchor points (default 300 s).
        rotation_avg_window_s: averaging width around each anchor (default 5 s).
        ma_window_s: moving-average smoothing window (default 15 s).

    Returns:
        Copy of ``df`` with added columns:
            'Current_detrended_nA': raw minus the rotation baseline.
            'Current_pipeline_nA':  detrended trace after moving-average
                smoothing (the final pipeline output).
        Rotation diagnostics stored in ``df.attrs``:
            ``df.attrs['rotation_slope_nA_per_min']``
            ``df.attrs['anchor_A']``  # (t_A, y_A)
            ``df.attrs['anchor_B']``  # (t_B, y_B)

    Raises:
        KeyError: If ``df`` is missing the required time/current columns.
        ValueError: Propagated from
            :func:`src.analysis.baseline.two_point_rotation_detrend` if the
            requested anchor windows are invalid.
    """
    # Local import keeps smoothing.py's module-level imports free of
    # baseline.py (avoids a circular-looking dependency if baseline.py ever
    # grows to consume smoothing utilities).
    from src.analysis.baseline import two_point_rotation_detrend

    _require_columns(df)

    rot = two_point_rotation_detrend(
        df,
        t_anchor_latest=float(t_first_addition_s) - 1.0,
        window_s=rotation_window_s,
        avg_window_s=rotation_avg_window_s,
    )
    detrended_nA = np.asarray(rot["detrended_nA"], dtype=float)

    # Build an intermediate DataFrame with Current (A) = detrended_nA * 1e-9
    # so moving_average (which consumes Amps) runs on the detrended trace.
    intermediate_df = df[[TIME_COL]].copy()
    intermediate_df[CURRENT_COL] = detrended_nA * 1e-9
    smoothed_df = moving_average(intermediate_df, window_s=ma_window_s)
    pipeline_nA = smoothed_df[SMOOTHED_COL].values

    out = df.copy()
    out["Current_detrended_nA"] = detrended_nA
    out["Current_pipeline_nA"] = pipeline_nA

    slope_s = rot.get("slope_nA_per_s", np.nan)
    if slope_s is None or (isinstance(slope_s, float) and not np.isfinite(slope_s)):
        slope_per_min = float("nan")
    else:
        slope_per_min = float(slope_s) * 60.0

    out.attrs["rotation_slope_nA_per_min"] = slope_per_min
    out.attrs["anchor_A"] = rot.get("anchor_A")
    out.attrs["anchor_B"] = rot.get("anchor_B")

    return out


def _default_channel_from_name(scan_name: str) -> Optional[int]:
    """Fallback: parse a ``... - ChN`` or ``... Ch N`` suffix into N.

    Returns the channel number (int) or None if no match.
    """
    m = re.search(r"Ch\s*(\d+)\s*$", scan_name)
    if m:
        return int(m.group(1))
    return None


def average_across_channels(
    scans: Dict[str, pd.DataFrame],
    blade_channels: List[int],
    blade_map_fn: Optional[Callable[[str], Optional[int]]] = None,
) -> pd.DataFrame:
    """Average current across multiple MUX channels on a common time grid.

    Given a dict of per-channel scans, selects those mapped to one of
    ``blade_channels`` and returns their mean current as a function of time.
    Scans are interpolated (linear) onto a common time grid whose spacing is
    the median ``dt`` across all selected scans and whose bounds are
    [max(t_min_i), min(t_max_i)] so no extrapolation is performed.

    Args:
        scans: Dict mapping scan name -> DataFrame with 'Time (s)' and
            'Current (A)' columns.
        blade_channels: Channel numbers (integers) to pool.
        blade_map_fn: Optional callable ``scan_name -> channel_number``.
            If None, a default parser is used that looks for a trailing
            ``- ChN`` or ``ChN`` in the scan name.

    Returns:
        DataFrame with columns 'Time (s)', 'Current (A)' (mean across
        channels, still in amps), and 'n_channels' (constant int equal
        to the number of scans pooled). If no scans match any of the
        requested channels, returns an empty DataFrame with those columns.
    """
    mapper = blade_map_fn if blade_map_fn is not None else _default_channel_from_name

    selected: List[Tuple[str, int, pd.DataFrame]] = []
    for name, df in scans.items():
        ch = mapper(name)
        if ch is None:
            continue
        if ch in blade_channels:
            selected.append((name, ch, df))

    if not selected:
        logger.warning(
            "average_across_channels: no scans matched channels %r",
            blade_channels,
        )
        return pd.DataFrame({TIME_COL: [], CURRENT_COL: [], "n_channels": []})

    # Common time grid: intersection of ranges, spacing = median dt
    t_mins = []
    t_maxs = []
    dts = []
    for _, _, df in selected:
        _require_columns(df)
        if len(df) < 2:
            continue
        t = df[TIME_COL].values
        t_mins.append(float(t[0]))
        t_maxs.append(float(t[-1]))
        dts.append(float(np.median(np.diff(t))))

    if not dts:
        logger.warning("average_across_channels: all selected scans too short")
        return pd.DataFrame({TIME_COL: [], CURRENT_COL: [], "n_channels": []})

    # dt mismatch guard: if channels were sampled at substantially different
    # rates, averaging across them is apples-to-oranges.
    if len(dts) >= 2:
        dt_min = min(dts)
        dt_max = max(dts)
        if dt_min > 0 and dt_max / dt_min > 2.0:
            logger.warning(
                "average_across_channels: channel dt_median values span "
                "%.3fs to %.3fs (ratio %.1fx); channels are being averaged "
                "across mismatched sampling rates",
                dt_min, dt_max, dt_max / dt_min,
            )

    t_start = max(t_mins)
    t_end = min(t_maxs)
    if t_end <= t_start:
        logger.warning(
            "average_across_channels: empty overlap [%f, %f]", t_start, t_end,
        )
        return pd.DataFrame({TIME_COL: [], CURRENT_COL: [], "n_channels": []})

    dt_grid = float(np.median(dts))
    if dt_grid <= 0:
        logger.warning("average_across_channels: non-positive median dt; empty output")
        return pd.DataFrame({TIME_COL: [], CURRENT_COL: [], "n_channels": []})

    t_common = np.arange(t_start, t_end + dt_grid / 2.0, dt_grid)

    stacked = []
    for _, _, df in selected:
        if len(df) < 2:
            continue
        t = df[TIME_COL].values.astype(float)
        i = df[CURRENT_COL].values.astype(float)
        stacked.append(np.interp(t_common, t, i))

    if not stacked:
        return pd.DataFrame({TIME_COL: [], CURRENT_COL: [], "n_channels": []})

    arr = np.vstack(stacked)  # shape (n_channels, n_time)
    # Use nanmean so a NaN in one channel does not propagate to the pooled mean
    mean_i = np.nanmean(arr, axis=0)
    n_channels = arr.shape[0]

    return pd.DataFrame({
        TIME_COL: t_common,
        CURRENT_COL: mean_i,
        "n_channels": np.full(len(t_common), n_channels, dtype=int),
    })

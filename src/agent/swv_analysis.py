"""Generic square-wave voltammetry peak analysis for the embedded agent.

Exposes :func:`analyze_swv_curve` (the pure-numpy peak math, importable on
its own) and :func:`build_swv_analysis_tools`, which returns the
``(tool_def, handler)`` pair for the ``analyze_swv`` tool in the same shape
``vendor_analysis.build_analysis_tools`` returns -- so it plugs into
``ToolRegistry.register`` / ``build_registry(extra_tools=...)`` with no
change to ``tools.py``.

This is NEW code under ``src/agent``, not an edit to the READ-ONLY vendored
CMU.49.011 package: 49.011 has no SWV analyzer, and the vendored tree is
re-copied from upstream rather than patched in place.  The vendored
*loaders* are reused (never reimplemented) to read the .pssession, and the
shared tool-layer helpers are imported from ``vendor_analysis`` so path
resolution, error shape, payload compaction and figure emission behave
identically across every ``analyze_*`` tool.

Scope (Req 15): GENERIC peak analysis only -- baseline subtraction, peak
potential, peak current, peak SNR, peak width.  No aptamer / E-AB math
(signal-gain %, two-frequency KDM, calibration fitting): that is Req 15b,
deliberately deferred until the actual lab workflow has been observed.

Every result carries an EXPLICIT ``verdict``.  A peak that does not clear
the noise floor reports ``no_peak_found``, and a maximum sitting on the
first or last sweep point reports ``peak_at_edge`` with the peak values
NULLED and replaced by bounds -- the same discipline as
``vendor_analysis._eis_apex_assessment``: a feature that was not actually
captured must never be handed back as if it had been measured.
"""

from __future__ import annotations

# Eager native imports at module top (blueprint constraint).  The vendor
# package import comes FIRST: its __init__ forces the matplotlib Agg
# backend before any pyplot import can pick a GUI backend.
import src.vendor.electrochem_analysis  # noqa: F401  - forces Agg
import matplotlib  # noqa: F401  - eager native import
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd  # noqa: F401  - eager native import

import logging
from typing import Any, Callable, Optional

# Shared analysis-tool plumbing.  Imported rather than duplicated so all
# analyze_* tools resolve paths, report errors, compact payloads and emit
# figures identically; vendor_analysis does not import this module, so
# there is no cycle.
from src.agent.vendor_analysis import (
    FigureSinkCallable,
    _AnalysisError,
    _clean_frame,
    _compact,
    _emit_figures,
    _load_scans,
    _path_prop,
    _resolve_path,
    _safe,
    _schema,
)
from src.vendor.electrochem_analysis.dataloaders import filter_scans

logger = logging.getLogger(__name__)

__all__ = ["analyze_swv_curve", "build_swv_analysis_tools"]

#: Fraction of the sweep taken from EACH end as the peak-free "wing" used
#: to fit the sloping charging-current baseline and to measure the noise
#: floor.  0.25 -> the outer quarter on each side.
DEFAULT_BASELINE_FRACTION = 0.25

#: Floor on the peak-height-over-noise ratio.  It is a FLOOR, not the whole
#: test: see :func:`_snr_threshold`.
DEFAULT_MIN_SNR = 3.0

#: Sigma added on top of the expected largest noise excursion before a peak
#: is believed (see :func:`_snr_threshold`).  The max of n samples scatters
#: by roughly 0.4 sigma about its expectation, so ~1.5 sigma of headroom
#: puts the false-peak rate on pure noise near 1%.
_EXTREME_VALUE_MARGIN = 1.5

#: Fewer usable points than this and no peak estimate is meaningful.
_MIN_POINTS = 10

#: Column names the vendored .pssession loader emits for a voltammogram.
_POTENTIAL_COL = "Potential (V)"
_CURRENT_COL = "Current (A)"


def _snr_threshold(n: int, min_snr: float) -> float:
    """SNR a peak must beat, corrected for the number of points searched.

    A flat "3 sigma" rule is the wrong test here, because the peak is the
    MAXIMUM of n residuals, not one draw: the largest of n standard normal
    samples already sits near ``sqrt(2 * ln(2n))`` sigma -- about 3.5 for a
    200-point sweep -- so a fixed 3-sigma cut calls pure noise a peak
    roughly half the time (observed on a 201-point noise trace at SNR 3.6).
    The threshold therefore scales with the sweep length, plus
    :data:`_EXTREME_VALUE_MARGIN` of headroom for the scatter of that
    maximum, and never drops below *min_snr*.
    """
    expected_noise_max = float(np.sqrt(2.0 * np.log(2.0 * max(n, 2))))
    return max(float(min_snr), expected_noise_max + _EXTREME_VALUE_MARGIN)


def _half_width_v(
    e: np.ndarray, resid: np.ndarray, idx: int
) -> Optional[float]:
    """Full width at half maximum of the baseline-subtracted peak, in volts.

    Walks outward from *idx* to the first point on each side that has
    fallen to half the peak height, then linearly interpolates the exact
    crossing.  Returns None when either side never crosses (the peak runs
    off the swept window), which keeps a truncated peak from being
    reported with a confident width.
    """
    half = resid[idx] / 2.0
    sign = 1.0 if resid[idx] >= 0 else -1.0

    def _cross(order: range) -> Optional[float]:
        prev = idx
        for j in order:
            if sign * resid[j] <= sign * half:
                r0, r1 = float(resid[prev]), float(resid[j])
                if r1 == r0:
                    return float(e[j])
                t = (half - r0) / (r1 - r0)
                return float(e[prev] + t * (e[j] - e[prev]))
            prev = j
        return None

    left = _cross(range(idx - 1, -1, -1))
    right = _cross(range(idx + 1, len(e)))
    if left is None or right is None:
        return None
    return abs(right - left)


def analyze_swv_curve(
    potential: Any,
    current: Any,
    *,
    baseline_fraction: float = DEFAULT_BASELINE_FRACTION,
    min_snr: float = DEFAULT_MIN_SNR,
) -> dict[str, Any]:
    """Find and characterize the faradaic peak in one SWV sweep.

    A straight line is least-squares fitted through the two peak-free
    wings (the outer ``baseline_fraction`` of points at each end) and
    subtracted -- that is the sloping residual charging current SWV does
    not fully cancel.  The peak is the largest ABSOLUTE residual, so
    anodic (positive) and cathodic (negative) peaks are both found and the
    reported peak current keeps its sign.  The noise floor is the robust
    sigma (1.4826 x MAD) of the wing residuals, i.e. measured where there
    is no signal.

    Args:
        potential: Sweep potentials in volts, in acquisition order.
        current: Net SWV currents in amperes, same length as *potential*.
        baseline_fraction: Fraction of the sweep taken from EACH end as
            the baseline/noise wing (0 < f <= 0.5).
        min_snr: FLOOR on the peak-height-over-noise-floor ratio.  The
            effective cut also scales with the number of points searched
            (see :func:`_snr_threshold`) and is reported back as
            ``snr_threshold``.

    Returns:
        A JSON-safe dict.  ``verdict`` is one of ``ok`` /
        ``no_peak_found`` / ``peak_at_edge`` / ``insufficient_data``.
        ``peak_potential_v`` and ``peak_current_a`` are populated ONLY for
        ``ok``; for ``peak_at_edge`` they are None and
        ``peak_potential_bound_v`` / ``peak_current_lower_bound_a`` carry
        the bounds instead.  Also reports ``peak_snr``, ``noise_floor_a``,
        ``peak_half_width_v``, the fitted baseline, point counts, and a
        prescriptive ``note``.

    Raises:
        ValueError: If the two inputs have different lengths or
            ``baseline_fraction`` is outside (0, 0.5].
    """
    if not 0.0 < float(baseline_fraction) <= 0.5:
        raise ValueError(
            "baseline_fraction must be in (0, 0.5]; it is the fraction "
            f"taken from EACH end, got {baseline_fraction!r}."
        )
    e_all = np.asarray(potential, dtype=float).ravel()
    i_all = np.asarray(current, dtype=float).ravel()
    if e_all.size != i_all.size:
        raise ValueError(
            "potential and current must be the same length "
            f"(got {e_all.size} and {i_all.size})."
        )

    finite = np.isfinite(e_all) & np.isfinite(i_all)
    dropped = int(e_all.size - int(np.count_nonzero(finite)))
    e = e_all[finite]
    i = i_all[finite]
    n = int(e.size)

    out: dict[str, Any] = {
        "points": n,
        "dropped_points": dropped,
        "potential_range_v": (
            [float(e.min()), float(e.max())] if n else None
        ),
        "peak_potential_v": None,
        "peak_current_a": None,
        "peak_snr": None,
        "snr_threshold": None,
        "peak_half_width_v": None,
        "noise_floor_a": None,
        "baseline_slope_a_per_v": None,
        "baseline_intercept_a": None,
    }

    if n < _MIN_POINTS:
        out["verdict"] = "insufficient_data"
        out["note"] = (
            f"Only {n} usable point(s) ({dropped} dropped as NaN/inf) -- "
            f"at least {_MIN_POINTS} are needed for a peak estimate. "
            "Re-run with a smaller e_step or a wider potential window, "
            "and check whether the channel returned data at all."
        )
        return out

    # -- Baseline: straight line through the two peak-free wings ---------
    k = min(max(2, int(round(n * float(baseline_fraction)))), n // 2)
    wing = np.zeros(n, dtype=bool)
    wing[:k] = True
    wing[-k:] = True
    try:
        slope, intercept = (
            float(v) for v in np.polyfit(e[wing], i[wing], 1)
        )
    except Exception:  # noqa: BLE001 - degenerate wings, fall back flat
        logger.debug("SWV baseline fit failed; using a constant baseline")
        slope, intercept = 0.0, float(np.median(i[wing]))
    resid = i - (slope * e + intercept)
    out["baseline_slope_a_per_v"] = slope
    out["baseline_intercept_a"] = intercept

    # -- Noise floor: robust sigma of the residual WHERE THERE IS NO PEAK
    wing_resid = resid[wing]
    med = float(np.median(wing_resid))
    mad = float(np.median(np.abs(wing_resid - med)))
    sigma = 1.4826 * mad
    out["noise_floor_a"] = sigma

    idx = int(np.argmax(np.abs(resid)))
    height = float(resid[idx])
    abs_height = abs(height)
    snr = (abs_height / sigma) if sigma > 0.0 else None
    threshold = _snr_threshold(n, min_snr)
    out["peak_snr"] = snr
    out["snr_threshold"] = threshold

    # A flat or pure-noise trace has wing scatter of the same order as its
    # largest excursion, so its SNR lands below the length-corrected
    # threshold and it is correctly rejected.
    detected = abs_height > 0.0 and (snr is None or snr >= threshold)
    if not detected:
        out["verdict"] = "no_peak_found"
        out["note"] = (
            "No peak clears the noise floor: the largest baseline-"
            f"subtracted excursion is {abs_height:.3g} A against a "
            f"{sigma:.3g} A noise floor"
            + (
                f" (SNR {snr:.2f}, below the {threshold:.2f} required "
                f"over {n} searched points)"
                if snr
                else ""
            )
            + ". Do NOT report a peak potential or peak current for this "
            "scan. Either there is no analyte response, or the scan needs "
            "a larger square-wave amplitude, a lower bw_hz, or a smaller "
            "current range (cr) for resolution."
        )
        return out

    at_edge = idx == 0 or idx == n - 1
    if at_edge:
        out["verdict"] = "peak_at_edge"
        out["peak_potential_bound_v"] = float(e[idx])
        out["peak_current_lower_bound_a"] = height
        out["note"] = (
            "The largest response sits on the "
            f"{'first' if idx == 0 else 'last'} point of the sweep "
            f"({float(e[idx]):.4g} V), so the peak was NOT captured -- it "
            "lies at or beyond the edge of the swept window. Peak "
            "potential and peak current are withheld rather than "
            "extrapolated; the current there is only a LOWER BOUND on the "
            "true peak. Widen the window (move e_begin / e_end outward "
            "past the response) and re-run before quoting any peak value."
        )
        return out

    out["verdict"] = "ok"
    out["peak_potential_v"] = float(e[idx])
    out["peak_current_a"] = height
    out["peak_half_width_v"] = _half_width_v(e, resid, idx)
    out["note"] = (
        f"Peak found at {float(e[idx]):.4g} V, {height:.4g} A above the "
        f"fitted baseline"
        + (f", SNR {snr:.1f}" if snr is not None else "")
        + ". Peak current is baseline-subtracted and signed (positive = "
        "anodic). This is a generic peak read: no aptamer / E-AB "
        "normalization, signal-gain % or KDM is applied."
    )
    return out


def _pick_swv_scan(
    scans: dict[str, Any], scan_name: Optional[str]
) -> tuple[str, Any]:
    """Select the SWV (potential-vs-current) scan to analyze.

    The vendored classifier has no SWV label -- it keys on columns -- so
    any ``Potential (V)`` + ``Current (A)`` table is a candidate and the
    scan title breaks the tie toward a square-wave measurement.
    """
    candidates = filter_scans(scans, "CV")
    if scan_name:
        if scan_name not in scans:
            raise _AnalysisError(
                f"Scan {scan_name!r} not found in this session. Call "
                "load_session to list the exact scan names.",
                available_scans=sorted(scans),
            )
        if scan_name not in candidates:
            raise _AnalysisError(
                f"Scan {scan_name!r} has no {_POTENTIAL_COL!r} / "
                f"{_CURRENT_COL!r} columns, so it is not a voltammogram.",
                available_scans=sorted(scans),
            )
        return scan_name, _clean_frame(candidates[scan_name])
    if not candidates:
        raise _AnalysisError(
            "This session contains no potential-vs-current scan, so "
            "there is no SWV data to analyze.",
            available_scans=sorted(scans),
        )
    for name in sorted(candidates):
        low = name.lower()
        if "square" in low or "swv" in low:
            return name, _clean_frame(candidates[name])
    name = sorted(candidates)[0]
    return name, _clean_frame(candidates[name])


def _swv_figure(e: np.ndarray, i: np.ndarray, res: dict[str, Any]) -> Any:
    """Render the voltammogram, its fitted baseline and the peak marker."""
    fig, ax = plt.subplots(figsize=(6.0, 4.0))
    ax.plot(e, i, lw=1.2, label="SWV")
    slope = res.get("baseline_slope_a_per_v")
    intercept = res.get("baseline_intercept_a")
    if slope is not None and intercept is not None:
        ax.plot(e, slope * e + intercept, ls="--", lw=1.0, label="baseline")
    e_pk = res.get("peak_potential_v")
    if e_pk is not None:
        ax.axvline(e_pk, ls=":", lw=1.0, color="k")
        ax.annotate(
            f"{e_pk:.3g} V\n{res['peak_current_a']:.3g} A",
            xy=(e_pk, float(np.interp(e_pk, e, i))),
            xytext=(6, 6),
            textcoords="offset points",
            fontsize=8,
        )
    ax.set_xlabel("Potential (V)")
    ax.set_ylabel("Current (A)")
    ax.set_title(f"SWV -- verdict: {res.get('verdict')}")
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    return fig


def build_swv_analysis_tools(
    figure_sink: Optional[FigureSinkCallable] = None,
) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]]:
    """Build the ``analyze_swv`` ``(tool_def, handler)`` pair.

    Args:
        figure_sink: Optional callable receiving one ``{"title", "tool",
            "png"}`` dict per rendered figure, exactly as
            ``vendor_analysis.build_analysis_tools`` uses it.  When None
            the figure is skipped and only the metrics are returned.

    Returns:
        A one-element list ready for ``ToolRegistry.register(*pair)``.
        The handler is synchronous and never raises.
    """

    def analyze_swv(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args.get("path"), (".pssession",))
        scans = _load_scans(path)
        name, df = _pick_swv_scan(scans, args.get("scan_name"))
        kwargs: dict[str, Any] = {}
        if args.get("baseline_fraction") is not None:
            kwargs["baseline_fraction"] = float(args["baseline_fraction"])
        if args.get("min_snr") is not None:
            kwargs["min_snr"] = float(args["min_snr"])
        e = df[_POTENTIAL_COL].to_numpy(dtype=float)
        i = df[_CURRENT_COL].to_numpy(dtype=float)
        try:
            metrics = analyze_swv_curve(e, i, **kwargs)
        except ValueError as exc:
            raise _AnalysisError(str(exc)) from None
        figures = [(f"SWV: {name}", _swv_figure(e, i, metrics))]
        n_figs = _emit_figures(figure_sink, "analyze_swv", figures)
        return _compact({
            "ok": True,
            "tool": "analyze_swv",
            "path": path,
            "scan_name": name,
            "figures_emitted": n_figs,
            **metrics,
        })

    tool_def = {
        "name": "analyze_swv",
        "description": (
            "Analyze a square-wave voltammogram from a saved .pssession "
            "file: fit and subtract the sloping charging-current "
            "baseline, then report peak potential, baseline-subtracted "
            "peak current, peak SNR and peak half-width. Call this after "
            "run_swv -> export_session, or whenever the user asks how "
            "big / where the SWV peak is. READ THE 'verdict' BEFORE "
            "QUOTING ANY NUMBER: 'ok' means a peak was measured; "
            "'no_peak_found' means nothing cleared the noise floor and "
            "there is no peak to report; 'peak_at_edge' means the "
            "response sits on the first or last sweep point, so peak "
            "potential and current are withheld and only bounds are "
            "given -- widen e_begin / e_end and re-run instead of "
            "quoting the bound as a result. Generic peak analysis only: "
            "no aptamer / E-AB signal-gain, KDM or calibration math."
        ),
        "input_schema": _schema(
            {
                "path": _path_prop(),
                "scan_name": {
                    "type": "string",
                    "description": (
                        "Exact scan/measurement name to analyze (from "
                        "load_session). Omit to auto-select the "
                        "square-wave scan in the file."
                    ),
                },
                "baseline_fraction": {
                    "type": "number",
                    "description": (
                        "Fraction (0, 0.5] of the sweep taken from EACH "
                        "end as the peak-free baseline and noise window "
                        "(default 0.25). Lower it when the peak sits "
                        "near one end of the window."
                    ),
                },
                "min_snr": {
                    "type": "number",
                    "description": (
                        "Floor on the peak-height-over-noise ratio "
                        "(default 3). The effective cut also grows with "
                        "the number of points searched and comes back as "
                        "'snr_threshold'; below it the verdict is "
                        "'no_peak_found'."
                    ),
                },
            },
            ["path"],
        ),
    }
    return [(tool_def, _safe("analyze_swv", analyze_swv))]

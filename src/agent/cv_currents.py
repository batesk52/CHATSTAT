"""Per-channel current values for a voltammetry run result.

A run summary carries counts, not data arrays. Without any current value
the agent could not tell how much current a sweep drew, so it could not
check a resistor, pick a current range, or compare channels. This module
adds a few numbers per channel: the lowest and highest current and the
potentials where they occurred, and a straight-line fit of current
against potential (for a resistor, 1 / slope is the resistance). When
points overloaded, it suggests the next larger current range, or says
that no larger range exists.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from src.techniques.scripts import CURRENT_RANGES, next_larger_current_range

__all__ = [
    "CV_CURRENTS_NOTE",
    "cv_currents",
    "range_hint",
]

_SI_SCALE = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}


def _amperes(cr: Any) -> Optional[float]:
    """A current range such as '100u' or '1m' in amperes, or None."""
    text = str(cr or "").strip()
    if not text:
        return None
    scale = _SI_SCALE.get(text[-1])
    digits = text[:-1] if scale is not None else text
    try:
        value = float(digits) * (scale if scale is not None else 1.0)
    except ValueError:
        return None
    return value if value > 0 else None

#: What the per-channel numbers mean; carried in the result as currents_note.
CV_CURRENTS_NOTE = (
    "currents gives, per channel, the lowest and highest current of the "
    "run (amperes) and the potential where each occurred (volts), over "
    "the points that did not overload. line_fit is a straight-line fit of "
    "current against potential over all cycles, or null when the current "
    "does not change: for a resistor, r_squared is near 1 and the "
    "resistance is 1 / slope_a_per_v; for an electrode it describes "
    "nothing physical, so do not quote it there. no_potential_points "
    "carry a current but no potential and are left out. nan_points are points "
    "with no usable current (a larger range does not fix those). When a "
    "channel has overload_points, its extremes are clipped: measure again "
    "at suggested_cr (a larger range is a measuring setting and drives no "
    "extra current) before quoting any value. If rerange_exhausted is "
    "true, the run was at the largest range on ChatStat's list, so do "
    "not quote a value: report that the current exceeds that range."
)


def _potential(variables: dict[str, Any]) -> Optional[float]:
    """Return the measured potential, else the set potential, else None."""
    for key in ("potential", "set_potential"):
        value = variables.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return None


def _line_fit(xs: list[float], ys: list[float]) -> Optional[dict[str, float]]:
    """Least-squares line through (xs, ys); None when it is undefined.

    A flat current (no change at all) has no meaningful fit, so it gives
    None rather than a zero slope with a perfect r_squared.
    """
    n = len(xs)
    if n < 3:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return {
        "slope_a_per_v": slope,
        "intercept_a": mean_y - slope * mean_x,
        "r_squared": (sxy * sxy) / (sxx * syy),
    }


def cv_currents(
    result: Any, channels_requested: list[int]
) -> dict[str, Any]:
    """Summarize the currents of a voltammetry run, per channel.

    Args:
        result: The finished ``MeasurementResult``.
        channels_requested: Channels the run asked for, so a requested
            channel without usable points is reported, not left out.

    Returns:
        ``{"per_channel": {ch: {points, used_points, overload_points,
        nan_points, min_current_a, e_at_min_v, max_current_a, e_at_max_v,
        line_fit, no_potential_points}}, "suggested_cr": str | None, "rerange_exhausted":
        bool | None, "note": str}``. The numeric fields are None for a
        channel with no usable point. Only points the instrument flagged
        as overloaded count as overloads; a non-numeric current counts
        in ``nan_points`` and never triggers a range hint.
    """
    counts: dict[int, int] = {}
    overloads: dict[int, int] = {}
    nans: dict[int, int] = {}
    no_potential: dict[int, int] = {}
    pairs: dict[int, list[tuple[float, float]]] = {}
    for dp in result.data_points:
        if "current" not in dp.variables:
            continue
        ch = int(dp.channel)
        counts[ch] = counts.get(ch, 0) + 1
        if getattr(dp, "overload", False):
            overloads[ch] = overloads.get(ch, 0) + 1
            continue
        current = dp.variables["current"]
        if not (isinstance(current, (int, float)) and math.isfinite(current)):
            nans[ch] = nans.get(ch, 0) + 1
            continue
        potential = _potential(dp.variables)
        if potential is None:
            no_potential[ch] = no_potential.get(ch, 0) + 1
            continue
        pairs.setdefault(ch, []).append((potential, float(current)))

    per_channel: dict[str, dict[str, Any]] = {}
    channels = sorted({int(c) for c in channels_requested} | set(counts))
    for ch in channels:
        usable = pairs.get(ch, [])
        entry: dict[str, Any] = {
            "points": counts.get(ch, 0),
            "used_points": len(usable),
            "overload_points": overloads.get(ch, 0),
            "nan_points": nans.get(ch, 0),
            "no_potential_points": no_potential.get(ch, 0),
            "min_current_a": None,
            "e_at_min_v": None,
            "max_current_a": None,
            "e_at_max_v": None,
            "line_fit": None,
        }
        if usable:
            low = min(usable, key=lambda p: p[1])
            high = max(usable, key=lambda p: p[1])
            entry["min_current_a"] = low[1]
            entry["e_at_min_v"] = low[0]
            entry["max_current_a"] = high[1]
            entry["e_at_max_v"] = high[0]
            entry["line_fit"] = _line_fit(
                [p[0] for p in usable], [p[1] for p in usable]
            )
        per_channel[str(ch)] = entry

    hint = range_hint(result, overloaded=bool(overloads))
    return {
        "per_channel": per_channel,
        "suggested_cr": hint["suggested_cr"],
        "rerange_exhausted": hint["rerange_exhausted"],
        "note": CV_CURRENTS_NOTE,
    }


def range_hint(result: Any, overloaded: bool) -> dict[str, Any]:
    """The re-range hint for a run: a larger range, or a clear stop.

    Uses the same low-speed list and the same step as the SWV hint
    (:func:`src.techniques.scripts.next_larger_current_range`), so every
    mode-2 technique gives the same advice for the same overload. A range
    that is not an exact entry of the list (for example '500u') steps to
    the next entry above its value.

    Returns:
        ``{"suggested_cr": str | None, "rerange_exhausted": bool | None}``.
        Without overloads both are None. With overloads: ``suggested_cr``
        is the next range up, or ``rerange_exhausted`` is True at the top
        of the list; when the run's range cannot be read, both are None,
        because nothing is known about what is left.
    """
    if not overloaded:
        return {"suggested_cr": None, "rerange_exhausted": None}
    params = getattr(result, "params", None) or {}
    used_text = str(params.get("cr") or "").strip()
    used = _amperes(used_text)
    if used is None:
        return {"suggested_cr": None, "rerange_exhausted": None}
    if used_text in CURRENT_RANGES:
        suggested = next_larger_current_range(used_text)
    else:
        suggested = next(
            (
                entry for entry in CURRENT_RANGES
                if (_amperes(entry) or 0.0) > used * (1 + 1e-9)
            ),
            None,
        )
    return {
        "suggested_cr": suggested,
        "rerange_exhausted": suggested is None,
    }

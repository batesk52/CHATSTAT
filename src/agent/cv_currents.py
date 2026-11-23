"""Per-channel current values for a voltammetry run result.

A run summary carries counts, not data arrays. Without any current value
the agent could not tell how much current a sweep drew, so it could not
check a resistor, pick a current range, or compare channels. This module
adds a few numbers per channel: the lowest and highest current and the
potentials where they occurred, and a straight-line fit of current
against potential (for a resistor, 1 / slope is the resistance). When
points overloaded, it suggests the next larger current range.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from src.techniques.scripts import next_larger_current_range

__all__ = ["CV_CURRENTS_NOTE", "cv_currents", "suggest_larger_range"]

#: Entries of the low-speed range list above the instrument's 5 mA range.
_ABOVE_INSTRUMENT = frozenset({"10m", "100m"})

#: What the per-channel numbers mean; carried in the result as currents_note.
CV_CURRENTS_NOTE = (
    "currents gives, per channel, the lowest and highest current of the "
    "run (amperes) and the potential where each occurred (volts), over "
    "the points that did not overload. line_fit is a straight-line fit of "
    "current against potential over all cycles: for a resistor, r_squared "
    "is near 1 and the resistance is 1 / slope_a_per_v; for an electrode "
    "it describes nothing physical, so do not quote it there. When a "
    "channel has overload_points, its extremes are clipped: measure again "
    "at suggested_cr (a larger range is a measuring setting and drives no "
    "extra current) before quoting any value."
)


def _potential(variables: dict[str, Any]) -> Optional[float]:
    """Return the measured potential, else the set potential, else None."""
    for key in ("potential", "set_potential"):
        value = variables.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return None


def _line_fit(xs: list[float], ys: list[float]) -> Optional[dict[str, float]]:
    """Least-squares line through (xs, ys); None when it is undefined."""
    n = len(xs)
    if n < 3:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    syy = sum((y - mean_y) ** 2 for y in ys)
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    r_squared = (sxy * sxy) / (sxx * syy) if syy > 0.0 else 1.0
    return {
        "slope_a_per_v": slope,
        "intercept_a": intercept,
        "r_squared": r_squared,
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
        min_current_a, e_at_min_v, max_current_a, e_at_max_v,
        line_fit}}, "suggested_cr": str | None, "note": str}``. The
        numeric fields are None for a channel with no usable point.
        ``suggested_cr`` is the next larger current range when any point
        overloaded and a larger range exists, else None.
    """
    counts: dict[int, int] = {}
    overloads: dict[int, int] = {}
    pairs: dict[int, list[tuple[float, float]]] = {}
    for dp in result.data_points:
        if "current" not in dp.variables:
            continue
        ch = int(dp.channel)
        counts[ch] = counts.get(ch, 0) + 1
        current = dp.variables["current"]
        bad = bool(getattr(dp, "overload", False)) or not (
            isinstance(current, (int, float)) and math.isfinite(current)
        )
        if bad:
            overloads[ch] = overloads.get(ch, 0) + 1
            continue
        potential = _potential(dp.variables)
        if potential is None:
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

    return {
        "per_channel": per_channel,
        "suggested_cr": suggest_larger_range(result) if overloads else None,
        "note": CV_CURRENTS_NOTE,
    }


def suggest_larger_range(result: Any) -> Optional[str]:
    """Next larger current range above the one *result* ran at, or None.

    None when the run named no range, the range is not on the low-speed
    list, or the next step would pass the instrument's largest range
    (the low-speed list goes past 5 mA; never suggest a range the
    instrument does not have).
    """
    params = getattr(result, "params", None) or {}
    used_cr = params.get("cr")
    if not used_cr:
        return None
    suggested = next_larger_current_range(str(used_cr))
    if suggested in _ABOVE_INSTRUMENT:
        return None
    return suggested

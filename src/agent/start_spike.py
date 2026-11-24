"""Tell a start spike from an under-ranged sweep.

On a multichannel sweep the MUX connects each pad after the last one
finished. The pad has been floating, and the sweep steps it straight to
``e_begin``, so the double layer charges in the first sample. That sample
can overload even on the largest range, while the rest of the sweep fits
the range easily. A larger range does not help; a hold at ``e_begin``
before the sweep does (``t_eq``, see ``scripts.start_hold_lines``).

:func:`start_spike_hint` looks at where the bad points are. When every
overloaded or unusable point is the first point of its channel, it returns
a hold to use instead of a range step.
"""

from __future__ import annotations

from typing import Any, Optional

from src.agent.cv_currents import is_unusable_current

__all__ = ["START_SPIKE_NOTE", "start_spike_hint", "LONGEST_SUGGESTED_HOLD_S"]

#: The first hold suggested when a run had none.
FIRST_SUGGESTED_HOLD_S = 2.0

#: Past this hold a spike is not a start spike any more; the run falls back
#: to the range hint.
LONGEST_SUGGESTED_HOLD_S = 10.0

START_SPIKE_NOTE = (
    "Only the first point of channel(s) {channels} overloaded: the pad was "
    "connected and stepped to e_begin in the same instant, so its charging "
    "current landed in the first sample. A larger range does not fix this. "
    "Re-run with t_eq={hold:g} (a hold at e_begin on each channel before "
    "its sweep; nothing is recorded during it, and it adds t_eq seconds per "
    "channel). Until then the first point of those channels is not a "
    "measurement; do not quote it or a peak-to-peak that includes it."
)


def _is_bad(dp: Any) -> bool:
    if getattr(dp, "overload", False):
        return True
    variables = getattr(dp, "variables", {}) or {}
    return "current" in variables and is_unusable_current(variables["current"])


def start_spike_hint(result: Any) -> Optional[dict[str, Any]]:
    """Return ``{channels, suggested_t_eq, note}``, or None.

    None means there is no bad point, a bad point sits after the first
    point of its channel (a real range problem), or the run already held
    for :data:`LONGEST_SUGGESTED_HOLD_S` or more.
    """
    params = getattr(result, "params", {}) or {}
    try:
        used = float(params.get("t_eq") or 0.0)
    except (TypeError, ValueError):
        used = 0.0
    seen: set[int] = set()
    spiked: set[int] = set()
    for dp in getattr(result, "data_points", []) or []:
        ch = int(dp.channel)
        first = ch not in seen
        seen.add(ch)
        if _is_bad(dp):
            if not first:
                return None
            spiked.add(ch)
    if not spiked:
        return None
    hold = FIRST_SUGGESTED_HOLD_S if used <= 0 else 2.0 * used
    if hold > LONGEST_SUGGESTED_HOLD_S or used >= LONGEST_SUGGESTED_HOLD_S:
        return None
    channels = sorted(spiked)
    return {
        "channels": channels,
        "suggested_t_eq": hold,
        "note": START_SPIKE_NOTE.format(channels=channels, hold=hold),
    }

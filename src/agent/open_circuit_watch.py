"""Early stop for an impedance sweep that cannot give usable data.

Two cases are caught, both from the first points of the sweep: a channel
that passes no current, and a range that is railed (the overload flag on
every point). A full sweep on this instrument takes about 15 minutes, and
in both cases the rest of it would be thrown away.

A sweep down to 0.1 Hz takes many minutes. On a channel that is open in the
wiring in use, all of that time buys noise. The first points of a sweep are
the high-frequency ones and arrive within seconds, so they are enough to
tell: a run of them with an AC current far below the pinned range, and no
overload flag, means the rest of the sweep cannot be any better.
``OpenCircuitWatch`` looks for that run and asks for an abort.

The very first points can still pass a little current through cable
capacitance (100 pF is 16 kOhm at 100 kHz), so the watch does not give up
on the first conducting point: it looks for ``WATCH_POINTS`` no-current
points in a row anywhere in the first ``WATCH_WINDOW`` points.

A railed range is the mirror case: ``WATCH_POINTS`` points in a row with
the overload flag set stop the sweep with ``reason == "overload"``.

The watch is pure Python (no Qt): feed it data points, read ``tripped``
and ``reason``.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional

# No-current points in a row that stop the sweep.
WATCH_POINTS = 5

# The watch ends after this many points. They are the highest frequencies of
# the sweep, where a real electrode has its LOWEST impedance.
WATCH_WINDOW = 25

# Same threshold as the end-of-sweep ``no_current`` verdict.
NO_CURRENT_FRACTION = 0.005


class OpenCircuitWatch:
    """Decide from the first points of a sweep whether any current flows.

    Args:
        e_ac: AC amplitude of the sweep in volts.
        cr_amperes: The pinned current range in amperes.
        on_trip: Called once, with no arguments, when the watch decides the
            channel passes no current (typically ``engine.abort``).
        points: How many points to look at.
    """

    def __init__(
        self,
        e_ac: float,
        cr_amperes: float,
        on_trip: Callable[[], None],
        points: int = WATCH_POINTS,
    ) -> None:
        self._limit_ohm = abs(e_ac) / (NO_CURRENT_FRACTION * cr_amperes)
        self._on_trip = on_trip
        self._need = points
        self._seen = 0
        self._run = 0
        self._overload_run = 0
        self._done = False
        self.tripped = False
        self.reason: Optional[str] = None

    @staticmethod
    def _magnitude(dp: Any) -> Optional[float]:
        variables = getattr(dp, "variables", None) or {}
        zmag = variables.get("impedance")
        zr = variables.get("zreal")
        zi = variables.get("zimag")
        values = [v for v in (zmag, zr, zi) if v is not None]
        if not values:
            return None
        if any(math.isnan(float(v)) for v in values):
            return math.inf  # NaN on an open channel counts as "no current"
        if zmag is not None:
            return abs(float(zmag))
        if zr is not None and zi is not None:
            return math.hypot(float(zr), float(zi))
        return None

    def feed(self, dp: Any) -> None:
        """Take one data point. Safe to call after the decision is made."""
        if self._done:
            return
        magnitude = self._magnitude(dp)
        if magnitude is None:
            return  # not an impedance point
        self._seen += 1
        if getattr(dp, "overload", False):
            # A railed range is under-range, never an open channel.
            self._overload_run += 1
            self._run = 0
        else:
            self._overload_run = 0
            self._run = self._run + 1 if magnitude > self._limit_ohm else 0
        if self._run >= self._need:
            self._trip("no_current")
        elif self._overload_run >= self._need:
            self._trip("overload")
        elif self._seen >= WATCH_WINDOW:
            self._done = True

    def _trip(self, reason: str) -> None:
        self._done = True
        self.tripped = True
        self.reason = reason
        self._on_trip()


def overload_stop_message(
    channel: int, cr: str, suggested: Optional[str], points: int = WATCH_POINTS
) -> str:
    """Text returned to the agent when a railed sweep was stopped early."""
    advice = (
        f"Re-run at a LARGER current range, e.g. {suggested} (jump several "
        "rungs if the current is far above the range)."
        if suggested
        else "This is already the largest range."
    )
    return (
        f"Sweep stopped early: channel {channel} railed the {cr} range "
        f"(overload on the first {points} points). The range is too small "
        f"for this cell. {advice}"
    )


def early_stop_message(channel: int, cr: str, points: int = WATCH_POINTS) -> str:
    """Text returned to the agent when a sweep was stopped early."""
    return (
        f"Sweep stopped early: channel {channel} passed almost no current "
        f"at {cr} (AC current under 0.5% of the range on {points} points in "
        "a row at the high-frequency end). The channel is open in the "
        "RE/CE wiring used, or the range is far too large. Do NOT step the "
        "range up. Check the wiring with a short CA hold (the wiring "
        "check); if the channel conducts in another wiring, repeat the "
        "sweep there at a range that fits its current."
    )

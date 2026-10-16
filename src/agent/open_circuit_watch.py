"""Early stop for an impedance sweep that cannot give usable data.

Three cases are caught, all from the first points of the sweep: a channel
that passes no current, a range that is far too large for a real cell, and
a range that is railed (the overload flag on every point). A full sweep on
this instrument takes about 15 minutes, and in each case the rest of it
would be thrown away.

The first points of a sweep are the high-frequency ones and arrive within
seconds, so they are enough to tell: a run of them with an AC current far
below the pinned range, and no overload flag, means the rest of the sweep
cannot be any better. ``OpenCircuitWatch`` looks for that run and asks for
an abort.

A small current has two causes, and they need opposite answers: an open
channel (check the wiring) and a real cell on a range that is far too large
(use a smaller range). The watch reports ``reason == "no_current"`` or
``reason == "range_too_large"``. It tells them apart in two steps.

Noise is an open channel: the phase jumps from point to point, where a real
cell gives neighbouring points that agree in phase and in size.

A steady signal is not yet a cell. Measured on the MUX16 with the vendor
resistor test cell (2026-10-11): a channel with nothing connected in the
wiring in use still passes a steady, capacitive current at high frequency,
through about 6 pF of board and cable capacitance (about 600 kOhm near
50 kHz). So a steady capacitive signal below ``STRAY_CAPACITANCE_F`` is
reported as ``no_current``, with the capacitance in
``stray_capacitance_f``. An electrode that small (a bare microelectrode of
a few micrometres) cannot be told from an open channel this way; the
message says so.

The very first points can still pass a little current through cable
capacitance (100 pF is 16 kOhm at 100 kHz), so the watch does not give up
on the first conducting point: it looks for ``WATCH_POINTS`` no-current
points in a row anywhere in the first ``WATCH_WINDOW`` points.

A railed range is the mirror case: ``WATCH_POINTS`` points in a row with
the overload flag set stop the sweep with ``reason == "overload"``.

A sweep is judged by its LARGEST current, which is at the top of the
sweep. A capacitive electrode passes less and less current as the frequency
falls, so a run of low-current points further down says nothing about the
range: the range must hold the first point. Once any point has carried real
current, the watch stops only for the one case that is still certain, a
steady capacitance of stray size; otherwise it stands down and the sweep
runs to the end. The smaller range it suggests is sized from the largest
current it saw (``min_impedance_ohm``), never from the median.

Only sweeps that run from high to low frequency are watched.

A NaN point decides nothing. The instrument writes NaN both for an open
channel and for a range that is too small, so a NaN neither adds to a run
nor breaks one. A sweep of nothing but NaN is left to run and is judged by
the end-of-sweep verdict.

The watch is pure Python (no Qt): feed it data points, read ``tripped``,
``reason`` and ``median_impedance_ohm``.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Callable, Optional

# No-current points in a row that stop the sweep.
WATCH_POINTS = 5

# The watch ends after this many points. They are the highest frequencies of
# the sweep, where a real electrode has its LOWEST impedance.
WATCH_WINDOW = 25

# AC current below this fraction of the pinned range counts as "no current".
# engine_adapter imports it for the end-of-sweep verdict: one number.
NO_CURRENT_FRACTION = 0.005

# Neighbouring points of a real cell agree this closely; noise does not.
# The size of a passive cell changes no faster than the frequency does (a
# capacitor: in proportion), and its phase turns by well under 70 degrees
# per decade, so both limits widen with the frequency step of the sweep.
STEADY_PHASE_DEG = 30.0
STEADY_PHASE_DEG_PER_DECADE = 60.0
STEADY_SIZE_RATIO = 2.0

# A steady capacitive signal below this is board and cable capacitance, not a
# cell. About 6 pF was measured on an unconnected MUX16 channel; the margin
# covers longer cables.
STRAY_CAPACITANCE_F = 20e-12
STRAY_PHASE_DEG = -45.0  # more capacitive than this counts as a capacitor


def series_capacitance(points: list[tuple]) -> Optional[float]:
    """Median series capacitance of ``(zreal, zimag, frequency_hz)`` points.

    Returns None unless every point is capacitive (phase below
    ``STRAY_PHASE_DEG``) and carries its frequency.
    """
    values = []
    for point in points:
        if len(point) < 3 or not point[2] or point[2] <= 0.0:
            return None
        zr, zi, freq = point[0], point[1], point[2]
        if zi >= 0.0 or math.degrees(math.atan2(zi, zr)) > STRAY_PHASE_DEG:
            return None
        values.append(1.0 / (2.0 * math.pi * freq * abs(zi)))
    return statistics.median(values) if values else None


def is_steady(points: list[tuple]) -> bool:
    """True when neighbouring points agree in phase and size.

    Each point is ``(zreal, zimag)`` or ``(zreal, zimag, frequency_hz)``.
    A real cell gives a signal that changes smoothly from one frequency to
    the next. Noise has a random phase, so four neighbouring pairs all
    within the phase limit happen by chance about once in a thousand on a
    sweep of ten points per decade.
    """
    if len(points) < 2:
        return False
    for a, b in zip(points, points[1:]):
        m0, m1 = math.hypot(a[0], a[1]), math.hypot(b[0], b[1])
        if m0 <= 0.0 or m1 <= 0.0:
            return False
        f0 = a[2] if len(a) > 2 else None
        f1 = b[2] if len(b) > 2 else None
        f_ratio = 1.0
        if f0 and f1 and f0 > 0.0 and f1 > 0.0:
            f_ratio = max(f0 / f1, f1 / f0)
        size_limit = STEADY_SIZE_RATIO * f_ratio
        if not 1.0 / size_limit <= m1 / m0 <= size_limit:
            return False
        step = math.degrees(math.atan2(b[1], b[0]) - math.atan2(a[1], a[0]))
        step = (step + 180.0) % 360.0 - 180.0
        phase_limit = STEADY_PHASE_DEG + STEADY_PHASE_DEG_PER_DECADE * math.log10(
            f_ratio
        )
        if abs(step) > phase_limit:
            return False
    return True


class OpenCircuitWatch:
    """Decide from the first points of a sweep whether it can give data.

    Args:
        e_ac: AC amplitude of the sweep in volts.
        cr_amperes: The pinned current range in amperes.
        on_trip: Called once, with no arguments, when the watch decides the
            sweep should stop (typically ``engine.abort``).
        points: How many points in a row decide.

    Attributes:
        tripped: True once the watch has asked for the stop.
        reason: ``"no_current"``, ``"range_too_large"`` or ``"overload"``.
        median_impedance_ohm: Median ``|Z|`` of the points that decided
            (not set for ``"overload"``).
        min_impedance_ohm: Smallest ``|Z|`` of those points: the largest
            current, the one a smaller range must hold.
        stray_capacitance_f: Set when a steady capacitive signal was read
            as stray capacitance only (``reason == "no_current"``).
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
        self._run: list[tuple] = []
        self._overload_run = 0
        self._done = False
        self.tripped = False
        self.reason: Optional[str] = None
        self.median_impedance_ohm: Optional[float] = None
        self.min_impedance_ohm: Optional[float] = None
        self.stray_capacitance_f: Optional[float] = None
        self._conducted = False  # a point with real current has been seen

    @staticmethod
    def _read(dp: Any) -> Optional[tuple]:
        """``(zreal, zimag, |Z|, frequency)`` of an impedance point, or None."""
        variables = getattr(dp, "variables", None) or {}
        freq = variables.get("set_frequency")
        zmag = variables.get("impedance")
        zr = variables.get("zreal")
        zi = variables.get("zimag")
        values = [v for v in (zmag, zr, zi) if v is not None]
        if not values:
            return None
        if any(math.isnan(float(v)) for v in values):
            return (None, None, math.nan, freq)  # open, or range too small
        if zr is not None and zi is not None:
            return (float(zr), float(zi), math.hypot(float(zr), float(zi)), freq)
        if zmag is not None:
            return (None, None, abs(float(zmag)), freq)
        return None

    def feed(self, dp: Any) -> None:
        """Take one data point. Safe to call after the decision is made."""
        if self._done:
            return
        point = self._read(dp)
        if point is None:
            return  # not an impedance point
        magnitude = point[2]
        self._seen += 1
        if math.isnan(magnitude) and not getattr(dp, "overload", False):
            if self._seen >= WATCH_WINDOW:
                self._done = True
            return
        if getattr(dp, "overload", False):
            # A railed range is under-range, never an open channel.
            self._overload_run += 1
            self._run = []
        else:
            self._overload_run = 0
            if magnitude > self._limit_ohm:
                self._run.append(point)
            else:
                self._conducted = True
                self._run = []
        if len(self._run) >= self._need:
            pairs = [(p[0], p[1], p[3]) for p in self._run]
            steady = all(q[0] is not None for q in pairs) and is_steady(pairs)
            capacitance = series_capacitance(pairs) if steady else None
            stray = capacitance is not None and capacitance < STRAY_CAPACITANCE_F
            if self._conducted and not stray:
                # Real current at the top of the sweep that falls with
                # frequency: a cell, not a fault the watch can name.
                self._done = True
                return
            self.median_impedance_ohm = statistics.median(p[2] for p in self._run)
            self.min_impedance_ohm = min(p[2] for p in self._run)
            if stray:
                self.stray_capacitance_f = capacitance
                self._trip("no_current")  # only stray capacitance
            else:
                self._trip("range_too_large" if steady else "no_current")
        elif self._overload_run >= self._need:
            self._trip("overload")
        elif self._seen >= WATCH_WINDOW:
            self._done = True

    def _trip(self, reason: str) -> None:
        self._done = True
        self.reason = reason
        self.tripped = True
        self._on_trip()


def overload_stop_message(
    channel: int, cr: str, suggested: Optional[str], points: int = WATCH_POINTS
) -> str:
    """Text returned to the agent when a railed sweep was stopped early."""
    advice = (
        f"Re-run at a LARGER current range, e.g. {suggested} (jump several "
        "rungs if the current is far above the range)."
        if suggested
        else "There is no larger range to step to from here."
    )
    return (
        f"Sweep stopped early: channel {channel} railed the {cr} range "
        f"(overload on the first {points} points). The range is too small "
        f"for this cell. {advice}"
    )


def range_too_large_message(
    channel: int, cr: str, suggested: Optional[str], impedance_ohm: float
) -> str:
    """Text returned when a real cell was swept on a range far too large."""
    advice = (
        f"Re-run at a SMALLER current range, e.g. {suggested}, or with "
        "eis_range_mode 'auto'."
        if suggested
        else "Re-run with eis_range_mode 'auto'."
    )
    return (
        f"Sweep stopped early: channel {channel} is connected (steady "
        f"readings near {impedance_ohm:.3g} ohm) but its AC current is under "
        f"0.5% of the {cr} range, so the range is far too large to resolve "
        f"it. The wiring is not the problem. {advice}"
    )


def early_stop_message(
    channel: int,
    cr: str,
    points: int = WATCH_POINTS,
    stray_capacitance_f: Optional[float] = None,
) -> str:
    """Text returned to the agent when a sweep was stopped early."""
    if stray_capacitance_f is not None:
        seen = (
            "the readings are what an unconnected channel gives: a "
            f"capacitance of about {stray_capacitance_f * 1e12:.1f} pF, the "
            "size of board and cable capacitance"
        )
    else:
        seen = "the readings are noise, not a steady signal"
    return (
        f"Sweep stopped early: channel {channel} passed almost no current "
        f"at {cr} (AC current under 0.5% of the range on {points} points in "
        f"a row at the high-frequency end, and {seen}). The channel is open "
        "in the RE/CE wiring used, unless the electrode itself is only a "
        "few picofarads (a bare microelectrode a few micrometres across). Do "
        "NOT step the range up. Check the wiring: on a cell that conducts "
        "DC (a resistor, a redox couple) use a short CA hold (the wiring "
        "check). A bare electrode in electrolyte passes no steady DC "
        "current, so a CA hold cannot clear it: for that, repeat this sweep "
        "over the top decade only with eis_range_mode 'auto'. If the "
        "channel conducts in another wiring, repeat the sweep there at a "
        "range that fits its current."
    )

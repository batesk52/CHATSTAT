"""Tests for the early stop of an impedance sweep on an open channel."""

from __future__ import annotations

import math

from src.agent.open_circuit_watch import (
    WATCH_POINTS,
    WATCH_WINDOW,
    OpenCircuitWatch,
    early_stop_message,
    is_steady,
    overload_stop_message,
    range_too_large_message,
)
from src.data.models import DataPoint


def _point(zreal: float, zimag: float, overload: bool = False) -> DataPoint:
    dp = DataPoint(
        timestamp=0.0,
        channel=1,
        variables={"set_frequency": 1e5, "zreal": zreal, "zimag": zimag},
    )
    if overload:
        dp.overload = True
    return dp


_PHASES = [20.0, 170.0, -95.0, 60.0, -150.0, 110.0, -30.0]
_noise_count = 0


def _noise(overload: bool = False) -> DataPoint:
    """An open-channel point: huge |Z| with a phase that jumps about."""
    global _noise_count
    angle = math.radians(_PHASES[_noise_count % len(_PHASES)])
    _noise_count += 1
    return _point(4e8 * math.cos(angle), 4e8 * math.sin(angle), overload=overload)


def _watch(cr_amperes: float = 1e-6):
    trips: list[int] = []
    watch = OpenCircuitWatch(0.01, cr_amperes, lambda: trips.append(1))
    return watch, trips


def test_open_channel_trips_after_the_watch_points() -> None:
    watch, trips = _watch()
    for _ in range(WATCH_POINTS - 1):
        watch.feed(_noise())
    assert not watch.tripped
    watch.feed(_noise())
    assert watch.tripped
    assert trips == [1]


def test_trips_once_only() -> None:
    watch, trips = _watch()
    for _ in range(WATCH_POINTS * 3):
        watch.feed(_noise())
    assert trips == [1]


def test_nan_points_decide_nothing() -> None:
    # NaN is what the instrument writes for an open channel AND for a range
    # that is too small, so a sweep of NaN must not be called "no current".
    watch, trips = _watch()
    for _ in range(60):
        watch.feed(_point(math.nan, math.nan))
    assert not watch.tripped
    assert trips == []


def test_nan_points_do_not_break_a_no_current_run() -> None:
    # Measured on an open channel: noise with a NaN point here and there.
    watch, _ = _watch()
    for _ in range(WATCH_POINTS - 1):
        watch.feed(_noise())
    watch.feed(_point(math.nan, math.nan))
    watch.feed(_noise())
    assert watch.tripped
    assert watch.reason == "no_current"


def test_conducting_channel_never_trips() -> None:
    # 1 kOhm at 10 mV is 10 uA: far above a 1 uA range, not below it.
    watch, trips = _watch()
    for _ in range(50):
        watch.feed(_point(1000.0, -5.0))
    assert not watch.tripped
    assert trips == []


def test_cable_capacitance_at_the_top_does_not_hide_an_open_channel() -> None:
    # A few conducting points first (stray capacitance), then nothing.
    watch, trips = _watch()
    for _ in range(6):
        watch.feed(_point(100.0, -16e3))
    for _ in range(WATCH_POINTS):
        watch.feed(_noise())
    assert watch.tripped
    assert trips == [1]


def test_a_conducting_point_resets_the_run() -> None:
    watch, _ = _watch()
    for _ in range(WATCH_POINTS - 1):
        watch.feed(_noise())
    watch.feed(_point(1000.0, -5.0))
    for _ in range(WATCH_POINTS - 1):
        watch.feed(_noise())
    assert not watch.tripped


def test_the_watch_ends_after_its_window() -> None:
    # No current only at the low-frequency end is left for the final verdict.
    watch, _ = _watch()
    for _ in range(WATCH_WINDOW):
        watch.feed(_point(1000.0, -5.0))
    for _ in range(50):
        watch.feed(_noise())
    assert not watch.tripped


def test_railed_range_trips_as_overload() -> None:
    # Measured: 1 kOhm at 10 mV on the 1 uA range flags every point.
    watch, trips = _watch()
    for _ in range(WATCH_POINTS):
        watch.feed(_point(-300.0, 200.0, overload=True))
    assert watch.tripped
    assert watch.reason == "overload"
    assert trips == [1]


def test_open_channel_reason_is_no_current() -> None:
    watch, _ = _watch()
    for _ in range(WATCH_POINTS):
        watch.feed(_noise())
    assert watch.reason == "no_current"


def test_an_overload_point_is_never_counted_as_no_current() -> None:
    watch, _ = _watch()
    for _ in range(WATCH_POINTS - 1):
        watch.feed(_noise())
    watch.feed(_noise(overload=True))
    assert not watch.tripped


def test_a_few_overload_points_do_not_stop_a_good_sweep() -> None:
    watch, _ = _watch()
    for _ in range(WATCH_POINTS - 1):
        watch.feed(_point(1000.0, -5.0, overload=True))
    for _ in range(50):
        watch.feed(_point(1000.0, -5.0))
    assert not watch.tripped


def test_overload_message_names_a_larger_range() -> None:
    text = overload_stop_message(1, "1u", "6u")
    assert "LARGER" in text
    assert "6u" in text


def test_high_impedance_electrode_on_a_fitting_range_is_left_alone() -> None:
    # 1 MOhm at 10 mV is 10 nA: 1% of a 1 uA range, above the 0.5% line.
    watch, _ = _watch()
    for _ in range(50):
        watch.feed(_point(1e6, -1e5))
    assert not watch.tripped


def test_non_impedance_points_are_ignored() -> None:
    watch, _ = _watch()
    for _ in range(50):
        watch.feed(DataPoint(timestamp=0.0, channel=1, variables={"current": 0.0}))
    assert not watch.tripped


def test_steady_small_signal_is_a_range_that_is_too_large() -> None:
    # A capacitive electrode, 1 MOhm at 1 kHz, on the 100 uA range: a real
    # cell. Its points agree in phase, so it must not be called open.
    trips: list[int] = []
    watch = OpenCircuitWatch(0.01, 100e-6, lambda: trips.append(1))
    for k in range(25):
        freq = 1e5 * 10 ** (-k / 6.0)
        watch.feed(_point(5e3, -1e6 * 1e3 / freq))
        if watch.tripped:
            break
    assert watch.tripped
    assert watch.reason == "range_too_large"
    assert watch.median_impedance_ohm > 20e3
    assert trips == [1]


def test_steady_resistor_on_a_huge_range_is_too_large_not_open() -> None:
    # 8.66 kOhm at 10 mV on the 5 mA range: 0.023% of the range, phase near 0.
    watch = OpenCircuitWatch(0.01, 5e-3, lambda: None)
    for _ in range(WATCH_POINTS):
        watch.feed(_point(8660.0, -5.0))
    assert watch.reason == "range_too_large"
    assert abs(watch.median_impedance_ohm - 8660.0) < 1.0


def test_magnitude_only_points_cannot_be_called_steady() -> None:
    # Without Z' and Z'' there is no phase to compare: stay with no_current.
    watch = OpenCircuitWatch(0.01, 1e-6, lambda: None)
    for _ in range(WATCH_POINTS):
        watch.feed(DataPoint(timestamp=0.0, channel=1, variables={"impedance": 4e8}))
    assert watch.reason == "no_current"


def test_is_steady_rejects_jumps_in_phase_and_in_size() -> None:
    assert is_steady([(1e6, -1e3)] * 5)
    assert not is_steady([(1e6, 0.0), (-1e6, 0.0), (1e6, 0.0)])
    assert not is_steady([(1e6, 0.0), (1e7, 0.0)])
    assert not is_steady([(1e6, 0.0)])


def test_range_too_large_message_points_down_not_at_the_wiring() -> None:
    text = range_too_large_message(3, "100u", "1u", 2.5e5)
    assert "SMALLER" in text
    assert "1u" in text
    assert "wiring is not the problem" in text


def test_message_points_at_the_wiring() -> None:
    text = early_stop_message(1, "1u").lower()
    assert "wiring" in text
    assert "do not step the range up" in text
    assert "channel 1" in text

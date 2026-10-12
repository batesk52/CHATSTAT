"""Tests for the ``no_current`` EIS verdict.

A sweep on a channel that is open in the wiring in use returns noise: some
NaN points, some negative Z'. That used to be read as an under-ranged sweep
and answered with a LARGER current range, the wrong direction. The verdict
is now ``no_current`` and the note points at the wiring.
"""

from __future__ import annotations

import math

from src.agent.engine_adapter import EngineAdapter, eis_quality
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig


def _point(channel: int, zreal: float, zimag: float, overload: bool = False):
    dp = DataPoint(
        timestamp=0.0,
        channel=channel,
        variables={"set_frequency": 1e3, "zreal": zreal, "zimag": zimag},
    )
    if overload:
        dp.overload = True
    return dp


def _result(points, cr: str, e_ac: float = 0.01) -> MeasurementResult:
    return MeasurementResult(
        data_points=points,
        technique="eis",
        params={"cr": cr, "e_ac": e_ac},
        channels=sorted({p.channel for p in points}),
    )


def _open_channel(channel: int = 16) -> list:
    """Shape of a real open-channel sweep: huge |Z|, NaN and negative points."""
    pts = [_point(channel, 4e8, -2e8) for _ in range(29)]
    pts += [_point(channel, -3e8, 1e8) for _ in range(14)]
    pts += [_point(channel, math.nan, math.nan) for _ in range(7)]
    return pts


def test_open_channel_is_no_current_not_underranged() -> None:
    q = eis_quality(_result(_open_channel(), cr="1u"), [16], "1u")
    ch = q["per_channel"]["16"]
    assert ch["verdict"] == "no_current"
    assert ch["nan_points"] == 7
    assert ch["ac_current_fraction_of_range"] < 0.005
    assert q["quality_ok"] is False


def test_no_current_never_suggests_a_larger_range() -> None:
    q = eis_quality(_result(_open_channel(), cr="1u"), [16], "1u")
    assert q["suggested_cr"] is None
    assert q["rerange_exhausted"] is False
    note = q["note"].lower()
    assert "wiring" in note
    assert "do not step the range up" in note


def test_conducting_channel_on_a_fitting_range_is_ok() -> None:
    # 8.66 kOhm at 10 mV is 1.15 uA: 19% of a 6 uA range.
    pts = [_point(16, 8660.0, -5.0) for _ in range(50)]
    q = eis_quality(_result(pts, cr="6u"), [16], "6u")
    assert q["per_channel"]["16"]["verdict"] == "ok"
    assert q["quality_ok"] is True


def test_railed_range_stays_underranged() -> None:
    # A real under-range: the overload bit is set, and the current is large.
    pts = [_point(1, 1000.0, -5.0, overload=True) for _ in range(10)]
    pts += [_point(1, 1000.0, -5.0) for _ in range(40)]
    q = eis_quality(_result(pts, cr="1u"), [1], "1u")
    assert q["per_channel"]["1"]["verdict"] == "underranged"
    assert q["suggested_cr"] == "6u"


def test_overload_bit_blocks_the_no_current_verdict() -> None:
    # Huge |Z| but the device says the range railed: trust the device.
    pts = [_point(1, 4e8, -2e8, overload=True) for _ in range(50)]
    q = eis_quality(_result(pts, cr="1u"), [1], "1u")
    assert q["per_channel"]["1"]["verdict"] == "underranged"


def test_without_e_ac_the_check_is_skipped() -> None:
    # Galvanostatic sweeps and old results carry no e_ac: behave as before.
    pts = _open_channel()
    result = MeasurementResult(
        data_points=pts, technique="eis", params={"cr": "1u"}, channels=[16]
    )
    q = eis_quality(result, [16], "1u")
    assert q["per_channel"]["16"]["verdict"] == "underranged"
    assert "ac_current_fraction_of_range" not in q["per_channel"]["16"]


def test_steady_small_signal_is_range_too_large_with_a_smaller_range() -> None:
    # 8.66 kOhm at 10 mV is 1.15 uA: 0.023% of the 5 mA range, clean data.
    pts = [_point(16, 8660.0, -5.0) for _ in range(50)]
    q = eis_quality(_result(pts, cr="5m"), [16], "5m")
    assert q["per_channel"]["16"]["verdict"] == "range_too_large"
    assert q["quality_ok"] is False
    assert q["suggested_cr"] == "6u"  # smallest range of at least 3 x 1.15 uA
    note = q["note"]
    assert "SMALLER" in note
    assert "wiring is not the problem" in note


def test_autorange_sweep_is_never_called_no_current() -> None:
    # In "auto" mode the range is a ceiling: a clean high-impedance electrode
    # far below it is a good sweep, not an open channel.
    pts = [_point(1, 1e6, -1e5) for _ in range(50)]
    result = MeasurementResult(
        data_points=pts,
        technique="eis",
        params={"cr": "100u", "e_ac": 0.01, "eis_range_mode": "auto"},
        channels=[1],
    )
    q = eis_quality(result, [1], "100u")
    assert q["per_channel"]["1"]["verdict"] == "ok"
    assert q["quality_ok"] is True
    assert "ac_current_fraction_of_range" not in q["per_channel"]["1"]


def test_summarize_carries_the_no_current_note() -> None:
    result = _result(_open_channel(), cr="1u")
    config = TechniqueConfig(
        technique="eis", params={"cr": "1u", "e_ac": 0.01}, channels=[16]
    )
    summary = EngineAdapter._summarize(result, config)
    assert summary["quality_ok"] is False
    assert summary["quality"]["16"]["verdict"] == "no_current"
    assert "suggested_cr" not in summary

"""Tests for the EIS auto-range data-quality summary (agent layer).

These pin the signal the agent uses to detect an under-ranged EIS sweep
(current railed the pinned range -> overload / NaN / negative real-Z) and
re-range up the mode-3 ladder, plus the wiring of that block into the run
summary returned by ``EngineAdapter._summarize``.
"""

from __future__ import annotations

import math

from src.agent.engine_adapter import EngineAdapter, eis_quality
from src.agent.tools import build_tool_defs
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig
from src.techniques.scripts import EIS_CURRENT_RANGES


def _eis_point(
    channel: int,
    freq: float,
    zreal: float,
    zimag: float,
    overload: bool = False,
) -> DataPoint:
    """Build one EIS DataPoint with the real packet's variable vocabulary."""
    if math.isnan(zreal) or math.isnan(zimag):
        zmag = float("nan")
    else:
        zmag = math.hypot(zreal, zimag)
    return DataPoint(
        timestamp=0.0,
        channel=channel,
        variables={
            "set_frequency": freq,
            "zreal": zreal,
            "zimag": zimag,
            "impedance": zmag,
        },
        overload=overload,
    )


def _result(
    points: list[DataPoint], technique: str = "eis", cr: str = "100u"
) -> MeasurementResult:
    return MeasurementResult(
        data_points=points,
        technique=technique,
        params={"cr": cr},
        channels=sorted({p.channel for p in points}),
    )


def test_clean_sweep_is_quality_ok() -> None:
    pts = [
        _eis_point(1, f, 300.0 + i, -50.0 - i)
        for i, f in enumerate([1e5, 1e4, 1e3, 100.0, 10.0])
    ]
    q = eis_quality(_result(pts), [1], "100u")
    assert q["quality_ok"] is True
    assert q["suggested_cr"] is None
    assert q["per_channel"]["1"]["verdict"] == "ok"


def test_overload_flags_underranged_and_suggests_larger() -> None:
    pts = [
        _eis_point(1, 1e5 / (i + 1), 300.0, -50.0, overload=(i < 5))
        for i in range(10)
    ]
    q = eis_quality(_result(pts, cr="1u"), [1], "1u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["1"]["verdict"] == "underranged"
    assert q["per_channel"]["1"]["overload_points"] == 5
    # One rung up the mode-3 ladder from 1u.
    assert q["suggested_cr"] == "6u"


def test_nan_points_flag_underranged() -> None:
    pts = [_eis_point(1, 1e5, 300.0, -50.0) for _ in range(8)]
    pts += [
        _eis_point(1, 1e5, float("nan"), float("nan")) for _ in range(2)
    ]
    q = eis_quality(_result(pts, cr="25u"), [1], "25u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["1"]["nan_points"] == 2
    assert q["suggested_cr"] == "50u"


def test_inverted_arc_majority_negative_flags_underranged() -> None:
    # A large negative-Z' fraction (inverted Nyquist arc) is a genuine
    # corruption signature mains pickup cannot produce -> flag.
    pts = [_eis_point(1, 1e5, -100.0, -50.0) for _ in range(7)]
    pts += [_eis_point(1, 1e3, 300.0, -50.0) for _ in range(3)]
    q = eis_quality(_result(pts, cr="100u"), [1], "100u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["1"]["neg_zreal_points"] == 7
    assert q["suggested_cr"] == "200u"


def test_scattered_negative_z_is_not_underranged() -> None:
    """Regression: a few mains-band negative-Z' points on a GOOD, correctly-
    ranged sweep must NOT be flagged (stepping the range up can't fix mains
    pickup; the project's own bench data attributes these to 50/60 Hz)."""
    pts = [_eis_point(1, 1e5 / (i + 1), 300.0, -50.0) for i in range(47)]
    # 3 scattered negative points (≈6% — like the mains-harmonic residual).
    pts += [_eis_point(1, 150.0, -10.0, -5.0) for _ in range(3)]
    q = eis_quality(_result(pts, cr="100u"), [1], "100u")
    assert q["quality_ok"] is True
    assert q["per_channel"]["1"]["neg_zreal_points"] == 3
    assert q["per_channel"]["1"]["verdict"] == "ok"
    assert q["suggested_cr"] is None


def test_requested_channel_with_no_data_is_no_data() -> None:
    pts = [_eis_point(1, 1e5, 300.0, -50.0) for _ in range(5)]
    # Request channels 1 and 2; channel 2 produced nothing.
    q = eis_quality(_result(pts), [1, 2], "100u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["2"]["verdict"] == "no_data"
    assert q["per_channel"]["1"]["verdict"] == "ok"
    # Re-ranging cannot conjure data for an open/dead channel: no suggestion,
    # and the note points at wiring/contact rather than the current range.
    assert q["suggested_cr"] is None
    assert "open" in q["note"].lower() or "wiring" in q["note"].lower()


def test_largest_range_still_bad_points_to_cell_not_range() -> None:
    pts = [
        _eis_point(1, 1e5, 300.0, -50.0, overload=True) for _ in range(10)
    ]
    q = eis_quality(_result(pts, cr="5m"), [1], "5m")
    assert q["quality_ok"] is False
    assert q["suggested_cr"] is None
    assert q["rerange_exhausted"] is True
    note = q["note"].lower()
    assert "cell" in note or "wiring" in note


def test_single_overload_on_small_sweep_not_flagged() -> None:
    """Absolute floor: one bad point on a small sweep must not flag (without
    the floor, 1/10 reaches the 10% fraction)."""
    pts = [_eis_point(1, 1e5 / (i + 1), 300.0, -50.0) for i in range(9)]
    pts.append(_eis_point(1, 1.0, 300.0, -50.0, overload=True))
    q = eis_quality(_result(pts), [1], "100u")
    assert q["quality_ok"] is True
    assert q["per_channel"]["1"]["overload_points"] == 1
    assert q["per_channel"]["1"]["verdict"] == "ok"


def test_boundary_five_of_fifty_flagged() -> None:
    """5 bad of 50 == exactly 10% and >= the 2-point floor -> flagged."""
    pts = [_eis_point(1, 1e5 / (i + 1), 300.0, -50.0) for i in range(45)]
    pts += [_eis_point(1, 1.0, 300.0, -50.0, overload=True) for _ in range(5)]
    q = eis_quality(_result(pts), [1], "100u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["1"]["verdict"] == "underranged"


def test_unknown_used_range_falls_back_to_mid_ladder() -> None:
    pts = [
        _eis_point(1, 1e5, 300.0, -50.0, overload=True) for _ in range(10)
    ]
    # '2u' is a mode-2 value, not on the mode-3 ladder.
    q = eis_quality(_result(pts, cr="2u"), [1], "2u")
    assert q["quality_ok"] is False
    assert q["suggested_cr"] == "100u"


def test_stray_single_bad_point_below_threshold_stays_ok() -> None:
    pts = [_eis_point(1, 1e5, 300.0, -50.0) for _ in range(49)]
    pts.append(_eis_point(1, 1e5, 300.0, -50.0, overload=True))
    q = eis_quality(_result(pts), [1], "100u")
    assert q["quality_ok"] is True
    assert q["per_channel"]["1"]["verdict"] == "ok"


def test_summarize_attaches_quality_for_eis() -> None:
    pts = [
        _eis_point(1, 1e5, 300.0, -50.0, overload=True) for _ in range(10)
    ]
    result = _result(pts, cr="1u")
    config = TechniqueConfig(
        technique="eis",
        params={"cr": "1u"},
        channels=[1],
        electrode_config_mode="external",
    )
    summary = EngineAdapter._summarize(result, config)
    assert summary["quality_ok"] is False
    assert summary["suggested_cr"] == "6u"
    assert summary["overload_points"] == 10
    assert summary["rerange_exhausted"] is False
    assert summary["quality"]["1"]["verdict"] == "underranged"


def test_summarize_rerange_exhausted_at_largest_range() -> None:
    pts = [
        _eis_point(1, 1e5, 300.0, -50.0, overload=True) for _ in range(10)
    ]
    result = _result(pts, cr="5m")
    config = TechniqueConfig(
        technique="eis",
        params={"cr": "5m"},
        channels=[1],
        electrode_config_mode="external",
    )
    summary = EngineAdapter._summarize(result, config)
    assert summary["quality_ok"] is False
    assert summary["rerange_exhausted"] is True
    # No retry to suggest at the top of the ladder.
    assert "suggested_cr" not in summary


def test_summarize_no_quality_block_for_non_eis() -> None:
    pt = DataPoint(
        timestamp=0.0,
        channel=1,
        variables={"current": 1e-6, "set_potential": 0.2},
    )
    result = MeasurementResult(
        data_points=[pt], technique="ca", params={"cr": "100u"}
    )
    config = TechniqueConfig(
        technique="ca", params={}, channels=[1]
    )
    summary = EngineAdapter._summarize(result, config)
    assert "quality" not in summary
    assert "quality_ok" not in summary
    assert "rerange_exhausted" not in summary
    assert summary["overload_points"] == 0


def test_run_eis_cr_description_lists_full_mode3_ladder() -> None:
    """The model-facing cr description must stay in sync with the canonical
    ladder (it is interpolated from EIS_CURRENT_RANGES, not hand-typed)."""
    defs = {d["name"]: d for d in build_tool_defs()}
    cr_desc = defs["run_eis"]["input_schema"]["properties"]["cr"]["description"]
    for rung in EIS_CURRENT_RANGES:
        assert f"'{rung}'" in cr_desc, (
            f"EIS range {rung!r} missing from run_eis cr description"
        )

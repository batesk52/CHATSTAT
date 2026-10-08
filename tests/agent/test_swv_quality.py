"""Tests for the SWV auto-range data-quality summary (agent layer, Req 15).

The square-wave counterpart of ``test_eis_quality.py``: these pin the
signal the agent uses to detect an under-ranged SWV scan (the current
railed the range -> overload bit / NaN) and re-range up the LOW-SPEED
(mode-2) ladder, plus the wiring of that block into the run summary
returned by ``EngineAdapter._summarize``.

The mode-2 ladder matters: SWV must never be handed an EIS mode-3
suggestion, and vice versa.
"""

from __future__ import annotations

import math

from src.agent.engine_adapter import EngineAdapter, swv_quality
from src.agent.tools import MEASUREMENT_TECHNIQUES, build_tool_defs
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig
from src.techniques.scripts import CURRENT_RANGES, EIS_CURRENT_RANGES


def _swv_point(
    channel: int,
    potential: float,
    current: float,
    overload: bool = False,
) -> DataPoint:
    """Build one SWV DataPoint with the real packet's variable vocabulary."""
    return DataPoint(
        timestamp=0.0,
        channel=channel,
        variables={"set_potential": potential, "current": current},
        overload=overload,
    )


def _sweep(
    channel: int = 1,
    n: int = 50,
    overload_from: int | None = None,
    nan_from: int | None = None,
    n_bad: int = 0,
) -> list[DataPoint]:
    """A clean SWV sweep, optionally with railed samples spliced in."""
    points: list[DataPoint] = []
    for i in range(n):
        e = -0.5 + i * 0.02
        bad_overload = (
            overload_from is not None
            and overload_from <= i < overload_from + n_bad
        )
        bad_nan = nan_from is not None and nan_from <= i < nan_from + n_bad
        points.append(
            _swv_point(
                channel,
                e,
                float("nan") if bad_nan else 1.0e-6,
                overload=bad_overload,
            )
        )
    return points


def _result(
    points: list[DataPoint], cr: str = "100u"
) -> MeasurementResult:
    return MeasurementResult(
        data_points=points,
        technique="swv",
        params={"cr": cr},
        channels=sorted({p.channel for p in points}),
    )


def _config(channels: list[int], cr: str = "100u") -> TechniqueConfig:
    return TechniqueConfig(
        technique="swv",
        params={"cr": cr},
        channels=channels,
        electrode_config_mode="external",
    )


# -- Clean run ------------------------------------------------------------

def test_clean_scan_is_quality_ok_with_no_suggestion() -> None:
    q = swv_quality(_result(_sweep()), [1], "100u")
    assert q["quality_ok"] is True
    assert q["per_channel"]["1"]["verdict"] == "ok"
    assert q["per_channel"]["1"]["bad_points"] == 0
    assert q["suggested_cr"] is None
    assert q["rerange_exhausted"] is False


def test_single_stray_bad_point_does_not_flag() -> None:
    """Absolute floor: one railed sample is not an under-ranged scan."""
    pts = _sweep(overload_from=10, n_bad=1)
    q = swv_quality(_result(pts), [1], "100u")
    assert q["quality_ok"] is True
    assert q["per_channel"]["1"]["overload_points"] == 1
    assert q["per_channel"]["1"]["verdict"] == "ok"
    assert q["suggested_cr"] is None


# -- Under-ranged run -----------------------------------------------------

def test_overload_flags_underranged_and_suggests_one_rung_up() -> None:
    pts = _sweep(overload_from=20, n_bad=6)
    q = swv_quality(_result(pts, cr="8u"), [1], "8u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["1"]["verdict"] == "underranged"
    assert q["per_channel"]["1"]["overload_points"] == 6
    # One rung up the MODE-2 ladder from 8u.
    assert q["suggested_cr"] == "16u"
    assert q["rerange_exhausted"] is False


def test_nan_currents_flag_underranged() -> None:
    pts = _sweep(nan_from=20, n_bad=4)
    q = swv_quality(_result(pts, cr="2u"), [1], "2u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["1"]["nan_points"] == 4
    assert q["per_channel"]["1"]["verdict"] == "underranged"
    assert q["suggested_cr"] == "4u"


def test_clipped_peak_is_flagged_despite_a_small_bad_fraction() -> None:
    """The reason SWV has no fraction gate.

    A railed PEAK is a handful of points on a long sweep -- under the 10%
    fraction EIS uses -- but it is exactly the number the scan exists to
    produce, so it must flag.
    """
    pts = _sweep(n=200, overload_from=95, n_bad=6)  # 3% of the sweep
    q = swv_quality(_result(pts, cr="16u"), [1], "16u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["1"]["verdict"] == "underranged"
    assert q["suggested_cr"] == "32u"


def test_suggestions_walk_the_whole_mode2_ladder() -> None:
    """Every rung suggests its own successor, none suggests a mode-3 value."""
    for lower, upper in zip(CURRENT_RANGES, CURRENT_RANGES[1:]):
        pts = _sweep(overload_from=5, n_bad=5)
        q = swv_quality(_result(pts, cr=lower), [1], lower)
        assert q["suggested_cr"] == upper, lower
        assert q["rerange_exhausted"] is False


# -- Top of the ladder ----------------------------------------------------

def test_largest_range_still_bad_stops_reranging() -> None:
    pts = _sweep(overload_from=5, n_bad=10)
    top = CURRENT_RANGES[-1]
    q = swv_quality(_result(pts, cr=top), [1], top)
    assert q["quality_ok"] is False
    assert q["suggested_cr"] is None
    assert q["rerange_exhausted"] is True
    note = q["note"].lower()
    assert "cell" in note or "wiring" in note


def test_mode3_only_range_falls_back_to_mid_ladder() -> None:
    """'6u' is EIS-only; suggest a valid mode-2 range, not a neighbour."""
    assert "6u" in EIS_CURRENT_RANGES and "6u" not in CURRENT_RANGES
    pts = _sweep(overload_from=5, n_bad=5)
    q = swv_quality(_result(pts, cr="6u"), [1], "6u")
    assert q["quality_ok"] is False
    assert q["suggested_cr"] == "100u"
    assert q["rerange_exhausted"] is False


# -- Missing channels -----------------------------------------------------

def test_requested_channel_with_no_data_is_no_data() -> None:
    q = swv_quality(_result(_sweep(channel=1)), [1, 2], "100u")
    assert q["quality_ok"] is False
    assert q["per_channel"]["2"]["verdict"] == "no_data"
    assert q["per_channel"]["1"]["verdict"] == "ok"
    # Re-ranging cannot conjure data for an open/dead channel.
    assert q["suggested_cr"] is None
    assert "open" in q["note"].lower() or "wiring" in q["note"].lower()


# -- Summary wiring -------------------------------------------------------

def test_summarize_attaches_quality_for_swv() -> None:
    pts = _sweep(overload_from=10, n_bad=8)
    summary = EngineAdapter._summarize(
        _result(pts, cr="8u"), _config([1], "8u")
    )
    assert summary["quality_ok"] is False
    assert summary["quality"]["1"]["verdict"] == "underranged"
    assert summary["suggested_cr"] == "16u"
    assert summary["rerange_exhausted"] is False
    assert summary["overload_points"] == 8


def test_summarize_clean_swv_has_no_suggestion() -> None:
    summary = EngineAdapter._summarize(_result(_sweep()), _config([1]))
    assert summary["quality_ok"] is True
    assert "suggested_cr" not in summary
    assert summary["rerange_exhausted"] is False


def test_summarize_swv_carries_the_four_output_var_contract() -> None:
    """All four declared SWV outputs are accounted for by role."""
    summary = EngineAdapter._summarize(_result(_sweep()), _config([1]))
    block = summary["swv_output_vars"]
    assert set(block["roles"]) == {
        "set_potential", "current", "current_forward", "current_reverse"
    }
    # Net current is never silently collapsed away...
    assert "current" in block["returned"]
    assert "set_potential" in block["returned"]
    # ...and forward/reverse are reported as absent rather than faked.
    assert block["not_returned"] == ["current_forward", "current_reverse"]
    assert "do not infer" in block["note"].lower()


def test_summarize_swv_gets_quality_not_the_cv_noise_block() -> None:
    """SWV takes its own branch, so it is not scored as a CV/CA trace."""
    summary = EngineAdapter._summarize(_result(_sweep()), _config([1]))
    assert "quality" in summary
    assert "noise" not in summary
    assert "noise_ok" not in summary


def test_eis_summary_is_untouched_by_the_swv_branch() -> None:
    """Regression guard: EIS keeps its own (mode-3) quality block."""
    pts = [
        DataPoint(
            timestamp=0.0,
            channel=1,
            variables={
                "set_frequency": 1e5,
                "zreal": 300.0,
                "zimag": -50.0,
                "impedance": math.hypot(300.0, 50.0),
            },
            overload=True,
        )
        for _ in range(10)
    ]
    result = MeasurementResult(
        data_points=pts, technique="eis", params={"cr": "1u"}, channels=[1]
    )
    config = TechniqueConfig(
        technique="eis", params={"cr": "1u"}, channels=[1]
    )
    summary = EngineAdapter._summarize(result, config)
    # '6u' is the mode-3 successor of '1u'; the mode-2 ladder has no '1u'.
    assert summary["suggested_cr"] == "6u"
    assert "swv_output_vars" not in summary


# -- Tool surface ---------------------------------------------------------

def test_run_swv_is_on_the_tool_surface_with_swv_parameters() -> None:
    assert "swv" in MEASUREMENT_TECHNIQUES
    defs = {d["name"]: d for d in build_tool_defs()}
    assert "run_swv" in defs
    props = defs["run_swv"]["input_schema"]["properties"]
    for field in (
        "e_begin", "e_end", "e_step", "amplitude", "frequency",
        "t_eq", "cr", "bw_hz", "channels", "electrode_config_mode",
        "re_ce_channels",
    ):
        assert field in props, field
    assert defs["run_swv"]["input_schema"]["required"] == ["channels"]
    assert defs["run_swv"]["input_schema"]["additionalProperties"] is False


def test_run_swv_defaults_match_the_script_generator() -> None:
    """The schema documents the generator's defaults, not invented ones."""
    from src.techniques.scripts import technique_params

    defaults = technique_params("swv")
    defs = {d["name"]: d for d in build_tool_defs()}
    props = defs["run_swv"]["input_schema"]["properties"]
    assert "0.005" in props["e_step"]["description"]
    assert "0.025" in props["amplitude"]["description"]
    assert "25" in props["frequency"]["description"]
    assert defaults["e_step"] == 0.005
    assert defaults["amplitude"] == 0.025
    assert defaults["frequency"] == 25.0

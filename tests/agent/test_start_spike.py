"""A first-point-only overload asks for a hold, not a larger range.

Seen on the instrument: a 16-channel CV on a Pt-black device overloaded on
the first point of every channel after the first, at 100u and again at 1m,
so the range hint ran out and said stop. The rest of every sweep fitted
100u. These tests pin the hint that tells the two cases apart.
"""

from __future__ import annotations

import pytest

from src.agent.engine_adapter import EngineAdapter
from src.agent.start_spike import start_spike_hint
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig


def _cv(spiked=(2, 3), late=None, t_eq=0.0, cr="100u", channels=(1, 2, 3)):
    result = MeasurementResult(
        technique="cv",
        params={"cr": cr, "t_eq": t_eq},
        channels=list(channels),
    )
    for ch in channels:
        for i in range(40):
            dp = DataPoint(
                timestamp=0.0,
                channel=ch,
                variables={"current": 1e-6 * i, "set_potential": -0.6 + i * 0.01},
            )
            if (i == 0 and ch in spiked) or (late and (ch, i) in late):
                dp.overload = True
            result.add_point(dp)
    return result


def _config(t_eq=0.0, cr="100u"):
    return TechniqueConfig(
        technique="cv",
        params={"cr": cr, "t_eq": t_eq},
        channels=[1, 2, 3],
        electrode_config_mode="external",
    )


def test_first_points_only_give_a_hold():
    hint = start_spike_hint(_cv())
    assert hint["channels"] == [2, 3]
    assert hint["suggested_t_eq"] == 2.0
    assert "t_eq=2" in hint["note"]


def test_a_later_bad_point_is_a_range_problem():
    assert start_spike_hint(_cv(late={(2, 5)})) is None


def test_a_clean_run_gives_nothing():
    assert start_spike_hint(_cv(spiked=())) is None


@pytest.mark.parametrize("t_eq", [0.5, 2.0, 10.0])
def test_after_a_hold_a_railed_first_point_is_real_current(t_eq):
    assert start_spike_hint(_cv(t_eq=t_eq)) is None


def test_after_a_hold_the_range_hint_applies():
    summary = EngineAdapter._summarize(_cv(t_eq=2.0), _config(t_eq=2.0))
    assert summary["suggested_cr"] == "1m"
    assert "suggested_t_eq" not in summary


def test_a_nan_first_point_counts_like_an_overload():
    result = _cv(spiked=())
    first = next(dp for dp in result.data_points if dp.channel == 3)
    first.variables["current"] = float("nan")
    assert start_spike_hint(result)["channels"] == [3]


def test_the_summary_carries_the_hold_and_no_range_step():
    summary = EngineAdapter._summarize(_cv(), _config())
    assert summary["start_spike_channels"] == [2, 3]
    assert summary["suggested_t_eq"] == 2.0
    assert "suggested_cr" not in summary
    assert "rerange_exhausted" not in summary


@pytest.mark.parametrize("cr", ["1m", "100m"])
def test_at_the_top_range_a_start_spike_is_not_exhausted(cr):
    summary = EngineAdapter._summarize(_cv(cr=cr), _config(cr=cr))
    assert "rerange_exhausted" not in summary
    assert summary["suggested_t_eq"] == 2.0


def test_a_real_overload_still_gets_the_range_step():
    summary = EngineAdapter._summarize(_cv(late={(2, 5), (2, 6)}), _config())
    assert summary["suggested_cr"] == "1m"
    assert "suggested_t_eq" not in summary

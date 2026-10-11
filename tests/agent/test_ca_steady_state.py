"""Tests for the per-channel settled-current block of a CA run summary.

The block lets the agent read every channel of a multichannel hold from the
run result itself, without one analysis call per channel.
"""

from __future__ import annotations

import math

import pytest

from src.agent.engine_adapter import EngineAdapter, ca_steady_state
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig


def _hold(channel: int, currents: list[float]) -> list[DataPoint]:
    return [
        DataPoint(
            timestamp=i * 0.1,
            channel=channel,
            variables={"set_potential": 0.1, "current": c},
        )
        for i, c in enumerate(currents)
    ]


def _result(points: list[DataPoint]) -> MeasurementResult:
    return MeasurementResult(
        data_points=points,
        technique="ca",
        params={"cr": "100u", "e_dc": 0.1},
        channels=sorted({p.channel for p in points}),
    )


def test_mean_uses_last_half_and_skips_the_transient() -> None:
    # First half is a start-up transient; the settled value is 1e-4 A.
    currents = [5e-4] * 10 + [1e-4] * 10
    block = ca_steady_state(_result(_hold(1, currents)), [1])
    ch = block["per_channel"]["1"]
    assert ch["points"] == 20
    assert ch["used_points"] == 10
    assert ch["mean_current_a"] == pytest.approx(1e-4)
    assert ch["std_current_a"] == pytest.approx(0.0)
    assert ch["drift_pct"] == pytest.approx(0.0)


def test_each_channel_gets_its_own_value() -> None:
    points = _hold(1, [1e-4] * 20) + _hold(16, [1.155e-5] * 20)
    block = ca_steady_state(_result(points), [1, 16])
    assert block["per_channel"]["1"]["mean_current_a"] == pytest.approx(1e-4)
    assert block["per_channel"]["16"]["mean_current_a"] == pytest.approx(
        1.155e-5
    )
    # 0.1 V over the settled current gives the load resistance.
    assert 0.1 / block["per_channel"]["16"]["mean_current_a"] == pytest.approx(
        8658.0, rel=1e-3
    )


def test_requested_channel_without_points_is_reported() -> None:
    block = ca_steady_state(_result(_hold(1, [1e-4] * 20)), [1, 2])
    ch = block["per_channel"]["2"]
    assert ch["points"] == 0
    assert ch["mean_current_a"] is None
    assert ch["drift_pct"] is None


def test_nan_points_are_counted_and_left_out_of_the_mean() -> None:
    currents = [1e-4] * 18 + [math.nan, math.nan]
    ch = ca_steady_state(_result(_hold(1, currents)), [1])["per_channel"]["1"]
    assert ch["overload_points"] == 2
    assert ch["mean_current_a"] == pytest.approx(1e-4)


def test_drift_is_reported_for_an_unsettled_hold() -> None:
    currents = [1e-4 + i * 1e-6 for i in range(40)]
    ch = ca_steady_state(_result(_hold(1, currents)), [1])["per_channel"]["1"]
    assert ch["drift_pct"] > 1.0


def test_summarize_attaches_steady_state_for_ca_only() -> None:
    result = _result(_hold(3, [2e-5] * 30))
    config = TechniqueConfig(technique="ca", params={}, channels=[3])
    summary = EngineAdapter._summarize(result, config)
    assert summary["steady_state"]["3"]["mean_current_a"] == pytest.approx(2e-5)
    assert "steady_state_note" in summary

    cv = MeasurementResult(
        data_points=_hold(3, [2e-5] * 30),
        technique="cv",
        params={"cr": "100u"},
        channels=[3],
    )
    cv_summary = EngineAdapter._summarize(
        cv, TechniqueConfig(technique="cv", params={}, channels=[3])
    )
    assert "steady_state" not in cv_summary

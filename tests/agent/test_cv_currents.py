"""A CV run result carries current values, and an overload carries a range hint.

Before this, a CV summary had counts only, so the agent could not read
how much current a sweep drew. These tests pin the per-channel numbers
(extremes, the potentials where they occur, a line fit) and the one-step
re-range hint on an overloaded run.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from src.agent.cv_currents import cv_currents, suggest_larger_range
from src.agent.engine_adapter import EngineAdapter
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig

RESISTANCE = {1: 1000.0, 2: 2000.0}   # ohm


def _resistor_cv(cr="1m", overload_channel=None, technique="cv"):
    result = MeasurementResult(
        technique=technique,
        start_time=datetime(2026, 11, 22, 12, 0, 0),
        params={"cr": cr},
        channels=[1, 2],
    )
    sweep = list(np.linspace(-0.2, 0.2, 41)) + list(np.linspace(0.2, -0.2, 41))
    for ch in (1, 2):
        for v in sweep:
            dp = DataPoint(
                timestamp=0.0,
                channel=ch,
                variables={"current": v / RESISTANCE[ch], "set_potential": v},
            )
            if ch == overload_channel and v > 0.15:
                dp.overload = True
            result.add_point(dp)
    return result


def test_extremes_and_line_fit_per_channel():
    out = cv_currents(_resistor_cv(), [1, 2])
    for ch, r in RESISTANCE.items():
        entry = out["per_channel"][str(ch)]
        assert entry["max_current_a"] == pytest.approx(0.2 / r)
        assert entry["e_at_max_v"] == pytest.approx(0.2)
        assert entry["min_current_a"] == pytest.approx(-0.2 / r)
        assert entry["e_at_min_v"] == pytest.approx(-0.2)
        fit = entry["line_fit"]
        assert 1.0 / fit["slope_a_per_v"] == pytest.approx(r)
        assert fit["r_squared"] == pytest.approx(1.0)
    assert out["suggested_cr"] is None


def test_overloaded_points_are_left_out_and_a_range_is_suggested():
    out = cv_currents(_resistor_cv(cr="100u", overload_channel=1), [1, 2])
    entry = out["per_channel"]["1"]
    assert entry["overload_points"] > 0
    assert entry["max_current_a"] < 0.15 / RESISTANCE[1] + 1e-12
    assert out["suggested_cr"] is not None
    assert out["suggested_cr"] != "100u"


def test_a_requested_channel_without_points_is_reported():
    out = cv_currents(_resistor_cv(), [1, 2, 3])
    assert out["per_channel"]["3"]["used_points"] == 0
    assert out["per_channel"]["3"]["max_current_a"] is None


def test_no_suggestion_past_the_instrument_range():
    assert suggest_larger_range(_resistor_cv(cr="5m")) is None


def test_the_run_summary_carries_the_currents():
    config = TechniqueConfig(technique="cv", params={}, channels=[1, 2])
    summary = EngineAdapter._summarize(_resistor_cv(), config)
    assert set(summary["currents"]) == {"1", "2"}
    assert "currents_note" in summary


def test_an_overloaded_ca_also_gets_a_range_hint():
    config = TechniqueConfig(technique="ca", params={}, channels=[1, 2])
    result = _resistor_cv(cr="100u", overload_channel=2, technique="ca")
    summary = EngineAdapter._summarize(result, config)
    assert summary["overload_points"] > 0
    assert summary.get("suggested_cr")
    assert "currents" not in summary


def test_the_range_probe_rule_reaches_dock_and_mcp_clients():
    from src.agent.agent_worker import DEFAULT_SYSTEM_PROMPT, INSTRUMENT_GUIDE

    assert "Current range probe" in INSTRUMENT_GUIDE
    assert "suggested_cr" in INSTRUMENT_GUIDE
    assert INSTRUMENT_GUIDE in DEFAULT_SYSTEM_PROMPT

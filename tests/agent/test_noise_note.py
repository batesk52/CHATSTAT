"""The noise note says only what was judged.

Seen on the instrument: on a 21-point run the channel's verdict was
insufficient_data while the note said every channel looked clean.
"""

from __future__ import annotations

from src.agent.engine_adapter import cv_noise
from src.data.models import DataPoint, MeasurementResult


def _run(points_per_channel):
    result = MeasurementResult(technique="cv", params={}, channels=sorted(points_per_channel))
    for ch, n in points_per_channel.items():
        for i in range(n):
            result.add_point(DataPoint(
                timestamp=0.0, channel=ch,
                variables={"current": 1e-6 * i, "set_potential": 0.35 + 0.001 * i},
            ))
    return result


def test_too_few_points_is_not_called_clean():
    out = cv_noise(_run({1: 5}), [1])
    assert out["per_channel"]["1"]["verdict"] == "insufficient_data"
    assert "clean" not in out["note"]
    assert "[\'1\']" in out["note"] and "too few" in out["note"]


def test_a_mixed_run_names_each_group():
    out = cv_noise(_run({1: 5, 2: 400}), [1, 2])
    assert out["per_channel"]["2"]["verdict"] == "clean"
    assert "['2'] look clean" in out["note"]
    assert "['1'] have too few" in out["note"]

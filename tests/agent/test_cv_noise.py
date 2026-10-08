"""Tests for the CV/CA high-frequency ripple ("noise scope") summary.

These pin the signal the agent uses to detect a noisy trace (e.g. 50/60 Hz
mains pickup) and re-acquire at a lower bandwidth, plus the wiring of that
block into ``EngineAdapter._summarize``.
"""

from __future__ import annotations

import math

from src.agent.engine_adapter import EngineAdapter, cv_noise
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig


def _cv_points(channel: int, currents: list[float]) -> list[DataPoint]:
    return [
        DataPoint(
            timestamp=i * 0.1,
            channel=channel,
            variables={"set_potential": -0.5 + i * 0.01, "current": c},
        )
        for i, c in enumerate(currents)
    ]


def _smooth(n: int = 60) -> list[float]:
    """A smooth duck-like sweep (no high-frequency content)."""
    return [math.sin(i / 10.0) for i in range(n)]


def _noisy(n: int = 60, amp: float = 0.1) -> list[float]:
    """Smooth sweep + alternating ripple (stand-in for mains pickup)."""
    return [math.sin(i / 10.0) + amp * (-1) ** i for i in range(n)]


def _duck(n: int) -> list[float]:
    """A clean (zero-ripple) CV with sharp faradaic peaks — worst case for a
    curvature-contaminated metric."""
    out = []
    for i in range(n):
        x = -1.0 + 2.0 * i / (n - 1)
        cap = 0.3 * x
        ox = math.exp(-((x - 0.3) / 0.12) ** 2) if i < n // 2 else 0.0
        red = -math.exp(-((x + 0.1) / 0.12) ** 2) if i >= n // 2 else 0.0
        out.append(cap + ox + red)
    return out


def _ripple(curr: list[float], amp: float, period: float) -> list[float]:
    return [c + amp * math.cos(2 * math.pi * i / period) for i, c in enumerate(curr)]


def _result(points: list[DataPoint], technique: str = "cv") -> MeasurementResult:
    return MeasurementResult(
        data_points=points,
        technique=technique,
        params={"cr": "100u", "bw_hz": 400},
        channels=sorted({p.channel for p in points}),
    )


def test_smooth_cv_is_clean() -> None:
    q = cv_noise(_result(_cv_points(1, _smooth())), [1])
    assert q["noise_ok"] is True
    assert q["per_channel"]["1"]["verdict"] == "clean"
    assert q["per_channel"]["1"]["ripple_ratio"] < 0.02


def test_mains_ripple_is_elevated() -> None:
    q = cv_noise(_result(_cv_points(1, _noisy())), [1])
    assert q["noise_ok"] is False
    assert q["per_channel"]["1"]["verdict"] == "elevated"
    assert q["per_channel"]["1"]["ripple_ratio"] >= 0.02
    note = q["note"].lower()
    assert "bw_hz" in note or "bandwidth" in note or "mains" in note


def test_lowering_ripple_amplitude_reads_as_lower_ripple_ratio() -> None:
    """The metric is comparable: less ripple -> smaller ripple_ratio (the agent
    drives this down across re-runs)."""
    high = cv_noise(_result(_cv_points(1, _noisy(amp=0.2))), [1])
    low = cv_noise(_result(_cv_points(1, _noisy(amp=0.02))), [1])
    assert (
        low["per_channel"]["1"]["ripple_ratio"]
        < high["per_channel"]["1"]["ripple_ratio"]
    )


def test_insufficient_points_reported() -> None:
    q = cv_noise(_result(_cv_points(1, [0.1, 0.2, 0.3])), [1, 2])
    assert q["per_channel"]["1"]["verdict"] == "insufficient_data"
    assert q["per_channel"]["1"]["ripple_ratio"] is None
    # Channel 2 requested but produced nothing.
    assert q["per_channel"]["2"]["verdict"] == "insufficient_data"


def test_multichannel_mixed_clean_and_noisy() -> None:
    pts = _cv_points(1, _smooth()) + _cv_points(2, _noisy())
    q = cv_noise(_result(pts), [1, 2])
    assert q["per_channel"]["1"]["verdict"] == "clean"
    assert q["per_channel"]["2"]["verdict"] == "elevated"
    assert q["noise_ok"] is False


def test_summarize_attaches_noise_for_cv() -> None:
    result = _result(_cv_points(1, _noisy()))
    config = TechniqueConfig(technique="cv", params={}, channels=[1])
    summary = EngineAdapter._summarize(result, config)
    assert summary["noise_ok"] is False
    assert summary["noise"]["1"]["verdict"] == "elevated"
    assert "noise_note" in summary
    # CV is not EIS -> no impedance quality block.
    assert "quality" not in summary


def test_summarize_eis_gets_quality_not_noise() -> None:
    """The EIS branch is exclusive: impedance runs get the quality block, not
    the current-ripple block (EIS packets carry no 'current')."""
    pts = [
        DataPoint(
            timestamp=0.0,
            channel=1,
            variables={
                "set_frequency": 1e5,
                "zreal": 300.0,
                "zimag": -50.0,
                "impedance": 304.1,
            },
        )
        for _ in range(5)
    ]
    result = MeasurementResult(
        data_points=pts, technique="eis", params={"cr": "100u"}, channels=[1]
    )
    config = TechniqueConfig(
        technique="eis", params={"cr": "100u"}, channels=[1]
    )
    summary = EngineAdapter._summarize(result, config)
    assert "quality" in summary
    assert "noise" not in summary


def test_clean_trace_with_faradaic_peaks_not_flagged() -> None:
    """Regression: a clean zero-ripple CV with sharp peaks must NOT read noisy —
    the old raw-2nd-difference metric false-positived on curvature."""
    assert (
        cv_noise(_result(_cv_points(1, _duck(60))), [1])["per_channel"]["1"][
            "verdict"
        ]
        == "clean"
    )
    assert (
        cv_noise(_result(_cv_points(1, _duck(40))), [1])["per_channel"]["1"][
            "verdict"
        ]
        == "clean"
    )


def test_off_nyquist_ripple_is_flagged() -> None:
    """Regression: ripple at a non-alternating apparent period (6 samples) must
    flag — the old 2nd-difference metric was frequency-blind and read it clean."""
    noisy = _ripple(_duck(60), 0.1, 6)
    q = cv_noise(_result(_cv_points(1, noisy)), [1])
    assert q["per_channel"]["1"]["verdict"] == "elevated"


def test_undersampled_scan_is_insufficient_not_noisy() -> None:
    """Below the point floor a sharp peak is indistinguishable from ripple, so
    report insufficient_data rather than phantom 'elevated'."""
    q = cv_noise(_result(_cv_points(1, _duck(18))), [1])
    assert q["per_channel"]["1"]["verdict"] == "insufficient_data"
    assert q["per_channel"]["1"]["ripple_ratio"] is None


def test_heavily_overloaded_trace_is_insufficient() -> None:
    """Half the points railed (NaN) → too decimated to trust; report it with the
    drop count rather than estimating ripple across the gaps."""
    curr = [
        float("nan") if i % 2 else 0.3 * math.sin(i / 10.0) for i in range(60)
    ]
    pc = cv_noise(_result(_cv_points(1, curr)), [1])["per_channel"]["1"]
    assert pc["verdict"] == "insufficient_data"
    assert pc["dropped_points"] == 30

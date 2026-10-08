"""Tests for the generic SWV peak analysis (agent layer, Req 15).

These pin the EXPLICIT verdicts the tool is built around -- a peak that
does not clear the noise floor must come back ``no_peak_found``, and a
maximum sitting on a sweep edge must come back ``peak_at_edge`` with the
peak values withheld -- plus the peak math itself, checked against
analytically-known synthetic curves rather than against the
implementation's own output.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.agent.swv_analysis import (
    analyze_swv_curve,
    build_swv_analysis_tools,
)

# Peak potential 0.1 V, Gaussian width parameter 0.05 V, so the analytic
# FWHM is 2 * 0.05 * sqrt(ln 2) = 0.08326 V and the peak height is 1 uA.
_E = np.linspace(-0.5, 0.5, 201)
_PEAK_V = 0.1
_PEAK_A = 1.0e-6
_WIDTH = 0.05
_ANALYTIC_FWHM = 2.0 * _WIDTH * float(np.sqrt(np.log(2.0)))


def _gaussian(centre: float = _PEAK_V, amp: float = _PEAK_A) -> np.ndarray:
    return amp * np.exp(-(((_E - centre) / _WIDTH) ** 2))


def test_clean_peak_is_ok_with_correct_potential_and_height() -> None:
    res = analyze_swv_curve(_E, _gaussian())
    assert res["verdict"] == "ok"
    assert res["peak_potential_v"] == pytest.approx(_PEAK_V, abs=0.01)
    assert res["peak_current_a"] == pytest.approx(_PEAK_A, rel=0.01)
    # Width is checked against the analytic FWHM, not against whatever the
    # implementation happens to return.
    assert res["peak_half_width_v"] == pytest.approx(
        _ANALYTIC_FWHM, rel=0.02
    )


def test_sloping_baseline_is_subtracted_before_the_peak_is_measured() -> None:
    """A charging-current ramp must not inflate the reported peak."""
    slope, offset = 3.0e-6, 5.0e-7
    res = analyze_swv_curve(_E, _gaussian() + slope * _E + offset)
    assert res["verdict"] == "ok"
    assert res["baseline_slope_a_per_v"] == pytest.approx(slope, rel=0.05)
    assert res["baseline_intercept_a"] == pytest.approx(offset, abs=5e-8)
    # Peak current is the height ABOVE the ramp, not the raw ordinate
    # (which is ~3.5 uA at 0.1 V).
    assert res["peak_current_a"] == pytest.approx(_PEAK_A, rel=0.05)


def test_cathodic_peak_is_found_and_keeps_its_sign() -> None:
    res = analyze_swv_curve(_E, _gaussian(centre=-0.2, amp=-2.0e-7))
    assert res["verdict"] == "ok"
    assert res["peak_potential_v"] == pytest.approx(-0.2, abs=0.01)
    assert res["peak_current_a"] < 0


def test_pure_noise_reports_no_peak_found() -> None:
    """The headline guard: nothing above the noise floor is NOT a peak.

    A fixed 3-sigma cut fails here -- the largest of ~200 noise samples
    routinely reaches 3.5 sigma -- so the threshold is length-corrected.
    """
    rng = np.random.default_rng(0)
    false_peaks = 0
    for _ in range(50):
        trace = 3.0e-6 * _E + 1.0e-6 + 1.0e-9 * rng.standard_normal(_E.size)
        res = analyze_swv_curve(_E, trace)
        if res["verdict"] != "no_peak_found":
            false_peaks += 1
    assert false_peaks == 0


def test_no_peak_found_withholds_the_numbers() -> None:
    res = analyze_swv_curve(_E, np.zeros(_E.size))
    assert res["verdict"] == "no_peak_found"
    assert res["peak_potential_v"] is None
    assert res["peak_current_a"] is None


def test_snr_threshold_grows_with_the_number_of_points_searched() -> None:
    short = analyze_swv_curve(np.linspace(-0.5, 0.5, 20), np.zeros(20))
    long = analyze_swv_curve(np.linspace(-0.5, 0.5, 2000), np.zeros(2000))
    assert long["snr_threshold"] > short["snr_threshold"]


def test_a_real_peak_well_above_noise_still_passes() -> None:
    """Length correction must not cost sensitivity on genuine peaks."""
    rng = np.random.default_rng(1)
    trace = _gaussian(amp=2.0e-8) + 1.0e-9 * rng.standard_normal(_E.size)
    res = analyze_swv_curve(_E, trace)
    assert res["verdict"] == "ok"
    assert res["peak_potential_v"] == pytest.approx(_PEAK_V, abs=0.02)


def test_peak_on_the_last_point_is_peak_at_edge_and_withholds_values() -> None:
    res = analyze_swv_curve(_E, _gaussian(centre=0.5))
    assert res["verdict"] == "peak_at_edge"
    # Mirrors the EIS apex rule: an uncaptured feature is never reported
    # as measured -- only bounded.
    assert res["peak_potential_v"] is None
    assert res["peak_current_a"] is None
    assert res["peak_potential_bound_v"] == pytest.approx(0.5)
    assert res["peak_current_lower_bound_a"] > 0
    assert "widen" in res["note"].lower()


def test_peak_on_the_first_point_is_peak_at_edge() -> None:
    res = analyze_swv_curve(_E, _gaussian(centre=-0.5))
    assert res["verdict"] == "peak_at_edge"
    assert res["peak_potential_bound_v"] == pytest.approx(-0.5)


def test_too_few_points_is_insufficient_data() -> None:
    res = analyze_swv_curve(_E[:6], np.zeros(6))
    assert res["verdict"] == "insufficient_data"
    assert res["peak_potential_v"] is None


def test_non_finite_points_are_dropped_not_propagated() -> None:
    trace = _gaussian()
    trace[3] = np.nan
    trace[7] = np.inf
    res = analyze_swv_curve(_E, trace)
    assert res["verdict"] == "ok"
    assert res["dropped_points"] == 2
    assert res["peak_potential_v"] == pytest.approx(_PEAK_V, abs=0.01)


def test_mismatched_lengths_and_bad_fraction_raise() -> None:
    with pytest.raises(ValueError):
        analyze_swv_curve(_E, _gaussian()[:10])
    with pytest.raises(ValueError):
        analyze_swv_curve(_E, _gaussian(), baseline_fraction=0.9)
    with pytest.raises(ValueError):
        analyze_swv_curve(_E, _gaussian(), baseline_fraction=0.0)


def test_tool_def_is_registrable_and_never_raises() -> None:
    pairs = build_swv_analysis_tools(figure_sink=None)
    assert len(pairs) == 1
    tool_def, handler = pairs[0]
    assert tool_def["name"] == "analyze_swv"
    assert tool_def["input_schema"]["type"] == "object"
    assert tool_def["input_schema"]["additionalProperties"] is False
    assert tool_def["input_schema"]["required"] == ["path"]
    # A missing file must come back as a structured failure, not an
    # exception into the dispatch layer.
    result = handler({"path": "no_such_file.pssession"})
    assert result["ok"] is False
    assert result["tool"] == "analyze_swv"

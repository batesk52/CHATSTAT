"""analyze_cv reports the cathodic, anodic and total charge storage capacity.

The vendored CVAnalyzer math is read-only (covered upstream). These tests pin
the owned tool-layer behaviour: three labelled numbers, no unlabelled one, a
note that says what each is, and the right answers on synthetic CVs whose
charge is known in closed form.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import src.agent.vendor_analysis as va
from src.vendor.electrochem_analysis.analysis.cv import CVAnalyzer

V_MIN, V_MAX = -0.6, 0.8
I0 = 1e-5          # A
SCAN_RATE = 0.1    # V/s
AREA = 0.01        # cm^2
ONE_BRANCH = I0 * (V_MAX - V_MIN) / SCAN_RATE / AREA * 1000.0   # mC/cm^2 (14.0)


def _rectangle(n: int = 700, closed: bool = True) -> pd.DataFrame:
    """Current +I0 going up, -I0 going down: a capacitor-like loop."""
    up = np.linspace(V_MIN, V_MAX, n)
    down = np.linspace(V_MAX, V_MIN if closed else 0.4, n)[1:]
    potential = np.concatenate([up, down])
    current = np.concatenate([np.full(n, I0), np.full(n - 1, -I0)])
    return pd.DataFrame({"Potential (V)": potential, "Current (A)": current})


@pytest.fixture
def cv_handler(monkeypatch):
    def _install(df: pd.DataFrame):
        monkeypatch.setattr(
            va, "_resolve_path", lambda raw, *a, **k: "synthetic.pssession"
        )
        monkeypatch.setattr(va, "_load_scans", lambda path: {"synthetic": df})
        monkeypatch.setattr(
            va,
            "_pick_scan",
            lambda scans, technique, scan_name, prefer_keyword=None: ("CV scan", df),
        )
        for tool_def, handler in va.build_analysis_tools(figure_sink=None):
            if tool_def["name"] == "analyze_cv":
                return handler
        raise AssertionError("analyze_cv tool not registered")

    return _install


def test_three_labelled_csc_values_are_reported(cv_handler):
    out = cv_handler(_rectangle())(
        {"path": "x", "scan_rate": SCAN_RATE, "electrode_area_cm2": AREA}
    )
    assert out["ok"] is True
    m = out["metrics"]
    assert m["csc_cathodic_mc_per_cm2"] == pytest.approx(ONE_BRANCH, rel=0.01)
    assert m["csc_anodic_mc_per_cm2"] == pytest.approx(ONE_BRANCH, rel=0.01)
    assert m["csc_total_mc_per_cm2"] == pytest.approx(2 * ONE_BRANCH, rel=0.01)
    assert m["csc_total_mc_per_cm2"] == pytest.approx(
        2 * m["csc_cathodic_mc_per_cm2"], rel=0.01
    )
    assert m["loop_status"] == "closed"


def test_no_unlabelled_csc_is_left_in_the_result(cv_handler):
    m = cv_handler(_rectangle())(
        {"path": "x", "scan_rate": SCAN_RATE, "electrode_area_cm2": AREA}
    )["metrics"]
    assert "csc_mc_per_cm2" not in m


def test_a_note_says_what_each_number_is(cv_handler):
    m = cv_handler(_rectangle())(
        {"path": "x", "scan_rate": SCAN_RATE, "electrode_area_cm2": AREA}
    )["metrics"]
    note = m["csc_note"]
    assert "csc_total_mc_per_cm2" in note and "|i|" in note
    assert "cathodic" in note and "anodic" in note
    assert "Say which one you quote" in note


def test_the_tool_description_names_the_three_values():
    description = next(
        d["description"]
        for d, _ in va.build_analysis_tools(figure_sink=None)
        if d["name"] == "analyze_cv"
    )
    for key in ("csc_total_mc_per_cm2", "csc_cathodic_mc_per_cm2", "csc_anodic_mc_per_cm2"):
        assert key in description


def test_no_csc_without_both_scan_rate_and_area(cv_handler):
    handler = cv_handler(_rectangle())
    for args in ({"path": "x"}, {"path": "x", "scan_rate": SCAN_RATE}):
        out = handler(args)
        assert out["ok"] is True
        assert not [k for k in out["metrics"] if k.startswith("csc")]
    out = handler({"path": "x", "scan_rate": SCAN_RATE})
    assert any("CSC needs BOTH" in n for n in out["notes"])


def test_an_open_loop_is_flagged_for_the_total(cv_handler):
    out = cv_handler(_rectangle(closed=False))(
        {"path": "x", "scan_rate": SCAN_RATE, "electrode_area_cm2": AREA}
    )
    assert out["metrics"]["loop_status"] == "open"
    assert any("covers only the part" in n for n in out["notes"])
    # 1.4 V up plus 0.4 V down were recorded.
    expected = I0 * (V_MAX - V_MIN + 0.4) / SCAN_RATE / AREA * 1000.0
    assert out["metrics"]["csc_total_mc_per_cm2"] == pytest.approx(expected, rel=0.01)


def test_the_tool_agrees_with_the_vendored_analyzer(cv_handler):
    df = _rectangle()
    analyzer = CVAnalyzer(df)
    analyzer.calculate_csc(SCAN_RATE, AREA)
    m = cv_handler(df)(
        {"path": "x", "scan_rate": SCAN_RATE, "electrode_area_cm2": AREA}
    )["metrics"]
    assert m["csc_total_mc_per_cm2"] == pytest.approx(analyzer.csc_total, rel=1e-5)
    assert m["csc_cathodic_mc_per_cm2"] == pytest.approx(analyzer.csc_cathodic, rel=1e-5)
    assert m["csc_anodic_mc_per_cm2"] == pytest.approx(analyzer.csc_anodic, rel=1e-5)


def test_the_vendored_analyzer_has_the_kind_argument_and_keeps_the_default():
    analyzer = CVAnalyzer(_rectangle())
    default = analyzer.calculate_csc(SCAN_RATE, AREA)
    assert default == analyzer.csc == analyzer.csc_cathodic
    assert analyzer.calculate_csc(SCAN_RATE, AREA, kind="total") == analyzer.csc_total
    with pytest.raises(ValueError, match="kind"):
        analyzer.calculate_csc(SCAN_RATE, AREA, kind="net")


def test_a_cycle_started_mid_range_is_counted_in_full():
    """The first vendored copy undercounted a cycle that starts mid-range;
    the re-copy integrates the whole window."""
    up = np.linspace(V_MIN, V_MAX, 700)
    down = np.linspace(V_MAX, V_MIN, 700)[1:]
    cycle = np.concatenate([up, down])
    start = 350                       # start the record half way up the first sweep
    potential = np.concatenate([cycle[start:], cycle[1:start + 1]])
    direction = np.sign(np.diff(potential, append=potential[-1] - 1e-9))
    frame = pd.DataFrame({"Potential (V)": potential, "Current (A)": I0 * direction})
    analyzer = CVAnalyzer(frame)
    analyzer.calculate_csc(SCAN_RATE, AREA)
    assert analyzer.csc_cathodic == pytest.approx(ONE_BRANCH, rel=0.02)
    assert analyzer.csc_total == pytest.approx(2 * ONE_BRANCH, rel=0.02)

"""Tests for the analyze_cv Randles-Sevcik / reversibility branch.

The vendored CVAnalyzer math is read-only (covered upstream in CMU.49.011).
These tests pin the OWNED tool-layer behaviour in
``src.agent.vendor_analysis.analyze_cv``:

* a known electroactive area must be recovered from a synthetic CV whose
  anodic peak current is built from Randles-Sevcik (ground-truth check, per
  the project's "validation must check ground truth" lesson),
* the reversibility metrics surface in the result,
* the area is skipped (not silently wrong) when no concentration is given,
  when scan_rate is missing, or when the peaks sit at a sweep endpoint,
* explicit JSON ``null`` for the optional numeric inputs must NOT crash the
  tool (regression for the int(None)/float(None) TypeError).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

import src.agent.vendor_analysis as va
from src.vendor.electrochem_analysis.analysis.cv import (
    DEFAULT_DIFFUSION_COEFF_CM2_S,
    RANDLES_SEVCIK_CONSTANT,
)


def _synthetic_reversible_cv(
    area_cm2: float,
    *,
    n: int = 1,
    diffusion_coeff: float = DEFAULT_DIFFUSION_COEFF_CM2_S,
    conc_mM: float = 5.0,
    scan_rate: float = 0.05,
    epa: float = 0.20,
    delta_ep_v: float = 0.059,
    sigma: float = 0.03,
    npts: int = 400,
) -> pd.DataFrame:
    """Ideal reversible CV encoding a known area via Randles-Sevcik.

    The anodic peak current is set so ip = k*n^1.5*A*sqrt(D)*C*sqrt(v),
    i.e. analyze_cv with the SAME scan_rate / concentration must recover
    ``area_cm2``. Forward and reverse sweeps carry mirror-image Gaussian
    peaks at Epa / Epc = Epa - delta_ep, both well inside their sweeps.
    """
    c_mol_cm3 = conc_mM * 1e-6
    ipa = (
        RANDLES_SEVCIK_CONSTANT
        * (n ** 1.5)
        * area_cm2
        * math.sqrt(diffusion_coeff)
        * c_mol_cm3
        * math.sqrt(scan_rate)
    )
    epc = epa - delta_ep_v
    v_fwd = np.linspace(-0.3, 0.3, npts)
    v_rev = np.linspace(0.3, -0.3, npts)
    i_fwd = ipa * np.exp(-((v_fwd - epa) ** 2) / (2 * sigma ** 2))
    i_rev = -ipa * np.exp(-((v_rev - epc) ** 2) / (2 * sigma ** 2))
    return pd.DataFrame(
        {
            "Potential (V)": np.concatenate([v_fwd, v_rev]),
            "Current (A)": np.concatenate([i_fwd, i_rev]),
        }
    )


def _synthetic_monotonic_cv(npts: int = 400) -> pd.DataFrame:
    """CV whose current rises monotonically with potential.

    The anodic max and cathodic min therefore land at the sweep endpoints,
    so extract_peaks succeeds but flags peaks_well_defined=False.
    """
    v_fwd = np.linspace(-0.3, 0.3, npts)
    v_rev = np.linspace(0.3, -0.3, npts)
    voltage = np.concatenate([v_fwd, v_rev])
    return pd.DataFrame(
        {"Potential (V)": voltage, "Current (A)": 1e-5 * voltage}
    )


@pytest.fixture
def cv_handler(monkeypatch):
    """Return analyze_cv wired to a synthetic in-memory CV scan.

    Patches the file-IO helpers so the handler never touches disk; the
    injected DataFrame is the scan analyze_cv analyses.
    """

    def _install(df: pd.DataFrame):
        monkeypatch.setattr(
            va, "_resolve_path", lambda raw, *a, **k: "synthetic.pssession"
        )
        monkeypatch.setattr(va, "_load_scans", lambda path: {"synthetic": df})
        monkeypatch.setattr(
            va,
            "_pick_scan",
            lambda scans, technique, scan_name, prefer_keyword=None: (
                "CV scan",
                df,
            ),
        )
        for tool_def, handler in va.build_analysis_tools(figure_sink=None):
            if tool_def["name"] == "analyze_cv":
                return handler
        raise AssertionError("analyze_cv tool not registered")

    return _install


def test_recovers_known_area_and_flags_reversible(cv_handler):
    handler = cv_handler(_synthetic_reversible_cv(area_cm2=0.05))
    out = handler({"path": "x", "scan_rate": 0.05, "concentration_mM": 5.0})

    assert out["ok"] is True
    m = out["metrics"]
    assert m["reversible"] is True
    assert m["peaks_well_defined"] is True
    assert m["delta_ep_mv"] == pytest.approx(59.0, abs=2.0)
    assert m["electroactive_area_cm2"] == pytest.approx(0.05, rel=0.05)
    assert m["electroactive_area_mm2"] == pytest.approx(5.0, rel=0.05)


def test_no_concentration_means_no_area(cv_handler):
    handler = cv_handler(_synthetic_reversible_cv(area_cm2=0.05))
    out = handler({"path": "x", "scan_rate": 0.05})

    assert out["ok"] is True
    assert "electroactive_area_cm2" not in out["metrics"]


def test_concentration_without_scan_rate_notes_and_skips(cv_handler):
    handler = cv_handler(_synthetic_reversible_cv(area_cm2=0.05))
    out = handler({"path": "x", "concentration_mM": 5.0})

    assert out["ok"] is True
    assert "electroactive_area_cm2" not in out["metrics"]
    assert any("scan_rate" in note for note in out.get("notes", []))


def test_explicit_null_optional_args_do_not_crash(cv_handler):
    # Regression: int(None)/float(None) used to raise TypeError, which the
    # tool wrapper turned into ok=False, discarding the peak metrics.
    handler = cv_handler(_synthetic_reversible_cv(area_cm2=0.05))
    out = handler(
        {
            "path": "x",
            "scan_rate": 0.05,
            "concentration_mM": 5.0,
            "n_electrons": None,
            "diffusion_coeff_cm2_s": None,
            "peak": None,
        }
    )

    assert out["ok"] is True
    assert out["metrics"]["electroactive_area_cm2"] == pytest.approx(
        0.05, rel=0.05
    )


def test_area_skipped_when_peaks_not_well_defined(cv_handler):
    handler = cv_handler(_synthetic_monotonic_cv())
    out = handler({"path": "x", "scan_rate": 0.05, "concentration_mM": 5.0})

    assert out["ok"] is True
    assert out["metrics"]["peaks_well_defined"] is False
    assert "electroactive_area_cm2" not in out["metrics"]
    assert any("well-defined" in note for note in out.get("notes", []))

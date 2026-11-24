"""analyze_cv says which cycle of a multi-cycle CV it read, and can pick one.

ChatStat exports a CV with n_scans cycles as one curve per cycle per
channel, and the vendored loader returns one of them (the last, unless
asked). Seen on the instrument: the agent could not tell which cycle its
CSC came from. These tests pin the choice, the count and the refusal.
"""

from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pytest

import src.agent.vendor_analysis as va
from src.agent.cv_curves import CycleChoiceError, check_cycle, count_cycles
from src.data.models import DataPoint, MeasurementResult
from src.data.pssession_exporter import PsSessionExporter

CDL = 5e-6     # F
RATE = 0.02    # V/s
AREA = 0.01767  # cm^2


def _one_cycle(lo=-0.6, hi=0.8, step=0.01):
    up = np.round(np.arange(lo, hi + step / 2, step), 6)
    return np.concatenate([up, up[::-1][1:]])     # lo .. hi .. lo


def _cv_file(tmp_path, n_cycles, growth=0.0, channels=(1,), name="cv.pssession"):
    """A capacitor-like CV whose current grows by *growth* per cycle."""
    one = _one_cycle()
    result = MeasurementResult(
        technique="cv",
        start_time=datetime(2026, 11, 23, 12, 0, 0),
        params={"e_begin": -0.6, "e_vtx1": 0.8, "e_vtx2": -0.6,
                "e_step": 0.01, "scan_rate": RATE, "n_scans": n_cycles},
        channels=list(channels),
        re_ce_channels=list(channels),
        electrode_config_mode="manual",
    )
    for ch in channels:
        for cycle in range(n_cycles):
            scale = 1.0 + growth * cycle
            for k, v in enumerate(one):
                going_up = k < len(one) // 2
                sign = 1.0 if going_up else -1.0
                result.add_point(DataPoint(
                    timestamp=0.0,
                    channel=ch,
                    variables={"current": sign * CDL * RATE * scale,
                               "set_potential": float(v)},
                ))
    return PsSessionExporter().export_pssession(
        result, os.path.join(tmp_path, name)
    )


@pytest.fixture
def analyze_cv():
    return {d["name"]: h for d, h in va.build_analysis_tools(None)}["analyze_cv"]


@pytest.mark.parametrize("choice,expected", [(1, 1), (3, 3), (-1, -1), (2.0, 2)])
def test_good_cycle_choices(choice, expected):
    assert check_cycle(choice) == expected


@pytest.mark.parametrize(
    "choice", [0, -2, 1.5, True, "1", None, float("inf"), float("nan")]
)
def test_bad_cycle_choices(choice):
    with pytest.raises(CycleChoiceError):
        check_cycle(choice)


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("channels", [(1,), (1, 2)])
def test_the_cycles_are_counted(tmp_path, n, channels):
    path = _cv_file(tmp_path, n, channels=channels)
    for ch in channels:
        assert count_cycles(path, f"Cyclic Voltammetry - Ch{ch}") == n


def test_an_unknown_scan_counts_nothing(tmp_path):
    assert count_cycles(_cv_file(tmp_path, 3), "Something else - Ch1") is None


def test_each_cycle_can_be_chosen(tmp_path, analyze_cv):
    path = _cv_file(tmp_path, 3, growth=0.5)
    args = {"path": path, "scan_rate": RATE, "electrode_area_cm2": AREA}
    charge = {}
    for cycle in (1, 2, 3, -1):
        out = analyze_cv({**args, "cycle": cycle})
        assert out["ok"] is True, out
        assert out["cycles_found"] == 3
        assert out["cycle_used"] == (3 if cycle == -1 else cycle)
        charge[cycle] = out["metrics"]["csc_total_mc_per_cm2"]
    assert charge[2] == pytest.approx(1.5 * charge[1], rel=0.02)
    assert charge[3] == pytest.approx(2.0 * charge[1], rel=0.02)
    assert charge[-1] == pytest.approx(charge[3])


def test_without_cycle_the_last_is_used_and_said(tmp_path, analyze_cv):
    path = _cv_file(tmp_path, 3, growth=0.5)
    out = analyze_cv({"path": path, "scan_rate": RATE, "electrode_area_cm2": AREA})
    last = analyze_cv({"path": path, "scan_rate": RATE,
                       "electrode_area_cm2": AREA, "cycle": 3})
    assert out["cycle_used"] == 3 and out["cycles_found"] == 3
    assert any("LAST one (cycle 3)" in n for n in out["notes"])
    assert out["metrics"]["csc_total_mc_per_cm2"] == pytest.approx(
        last["metrics"]["csc_total_mc_per_cm2"]
    )


def test_a_single_cycle_needs_no_note(tmp_path, analyze_cv):
    out = analyze_cv({"path": _cv_file(tmp_path, 1)})
    assert out["cycles_found"] == 1 and out["cycle_used"] == 1
    assert not any("cycles" in n for n in out.get("notes", []))


def test_a_cycle_that_does_not_exist_is_refused(tmp_path, analyze_cv):
    out = analyze_cv({"path": _cv_file(tmp_path, 3), "cycle": 4})
    assert out["ok"] is False
    assert "holds 3 cycle" in out["error"]
    assert out["cycles_found"] == 3


def test_a_bad_cycle_value_is_refused(tmp_path, analyze_cv):
    out = analyze_cv({"path": _cv_file(tmp_path, 3), "cycle": 0})
    assert out["ok"] is False
    assert "-1 for the last" in out["error"]


def test_the_second_channel_gets_its_own_chosen_cycle(tmp_path, analyze_cv):
    path = _cv_file(tmp_path, 3, growth=0.5, channels=(1, 2))
    args = {"path": path, "scan_rate": RATE, "electrode_area_cm2": AREA,
            "scan_name": "Cyclic Voltammetry - Ch2"}
    first = analyze_cv({**args, "cycle": 1})
    last = analyze_cv({**args, "cycle": -1})
    assert first["scan_name"] == "Cyclic Voltammetry - Ch2"
    assert last["metrics"]["csc_total_mc_per_cm2"] == pytest.approx(
        2.0 * first["metrics"]["csc_total_mc_per_cm2"], rel=0.02
    )


def test_each_channel_counts_its_own_cycles(tmp_path):
    path = _cv_file(tmp_path, 3, channels=(4, 7))
    assert count_cycles(path, "Cyclic Voltammetry - Ch4") == 3
    assert count_cycles(path, "Cyclic Voltammetry - Ch7") == 3
    assert count_cycles(path, "Cyclic Voltammetry - Ch5") is None


def test_a_whole_float_n_scans_still_writes_each_cycle(tmp_path, analyze_cv):
    one = _one_cycle()
    result = MeasurementResult(
        technique="cv",
        start_time=datetime(2026, 11, 23, 12, 0, 0),
        params={"e_begin": -0.6, "e_vtx1": 0.8, "e_vtx2": -0.6,
                "e_step": 0.01, "scan_rate": RATE, "n_scans": 3.0},
        channels=[1], re_ce_channels=[1], electrode_config_mode="manual",
    )
    for _ in range(3):
        for k, v in enumerate(one):
            sign = 1.0 if k < len(one) // 2 else -1.0
            result.add_point(DataPoint(timestamp=0.0, channel=1, variables={
                "current": sign * CDL * RATE, "set_potential": float(v)}))
    path = PsSessionExporter().export_pssession(
        result, os.path.join(tmp_path, "float.pssession"))
    assert analyze_cv({"path": path})["cycles_found"] == 3

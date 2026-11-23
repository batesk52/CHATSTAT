"""analyze_ecsa over several files: one scan rate per file, a fit per channel.

The agent records one scan rate per run_cv call and exports each run to
its own .pssession file. These tests export synthetic capacitor-like CVs
(current = +C * rate going up, -C * rate going down) through the real
exporter, then check that the multi-file form reads each file's scan rate
from its stored method and returns the known capacitance per channel.
"""

from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pytest

import src.agent.vendor_analysis as va
from src.agent.ecsa_files import channel_of, session_scan_rate, session_scan_rates
from src.data.models import DataPoint, MeasurementResult
from src.data.pssession_exporter import PsSessionExporter

CAPACITANCE = {1: 2e-6, 2: 5e-6}   # F, per channel
RATES = (0.02, 0.04, 0.06, 0.08, 0.1, 0.12)   # V/s


def _cv(rate: float, second: int, channels=(1, 2)) -> MeasurementResult:
    result = MeasurementResult(
        technique="cv",
        start_time=datetime(2026, 11, 22, 12, 0, second),
        params={
            "e_begin": 0.35, "e_vtx1": 0.35, "e_vtx2": 0.45,
            "e_step": 0.002, "scan_rate": rate, "n_scans": 1,
        },
        channels=list(channels),
        re_ce_channels=list(channels),
        electrode_config_mode="manual",
    )
    t = 0.0
    for ch in channels:
        up = np.linspace(0.35, 0.45, 51)
        down = np.linspace(0.45, 0.35, 51)[1:]
        for sign, sweep in ((1.0, up), (-1.0, down)):
            for v in sweep:
                result.add_point(DataPoint(
                    timestamp=t,
                    channel=ch,
                    variables={
                        "current": sign * CAPACITANCE[ch] * rate,
                        "set_potential": float(v),
                    },
                ))
                t += 0.01
    return result


@pytest.fixture
def files(tmp_path):
    exporter = PsSessionExporter()
    return [
        exporter.export_pssession(
            _cv(rate, i), os.path.join(tmp_path, f"cv_{i}.pssession")
        )
        for i, rate in enumerate(RATES)
    ]


@pytest.fixture
def ecsa():
    for tool_def, handler in va.build_analysis_tools(figure_sink=None):
        if tool_def["name"] == "analyze_ecsa":
            return handler
    raise AssertionError("analyze_ecsa tool not registered")


def test_scan_rate_is_read_from_the_stored_method(files):
    assert [session_scan_rate(p) for p in files] == pytest.approx(list(RATES))


def test_channel_is_read_from_the_scan_name():
    assert channel_of("Cyclic Voltammetry - Ch1") == 1
    assert channel_of("Cyclic Voltammetry - Ch16") == 16
    assert channel_of("CV 50mVps") is None


def test_each_channel_gets_its_own_capacitance(files, ecsa):
    out = ecsa({"paths": files})
    assert out["ok"] is True
    assert set(out["per_channel"]) == {"1", "2"}
    for ch, cap in CAPACITANCE.items():
        fit = out["per_channel"][str(ch)]
        assert fit["ok"] is True
        assert fit["cdl_F"] == pytest.approx(cap, rel=1e-3)
        assert fit["r_squared"] == pytest.approx(1.0, abs=1e-6)
        assert fit["n_scans_used"] == len(RATES)
    assert [f["scan_rate_v_per_s"] for f in out["files"]] == pytest.approx(
        list(RATES)
    )


def test_one_channel_only_and_its_figure(files):
    figures = []
    handler = {
        d["name"]: h for d, h in va.build_analysis_tools(figures.append)
    }["analyze_ecsa"]
    out = handler({"paths": files, "channel": 2, "electrode_area_cm2": 0.01767})
    assert set(out["per_channel"]) == {"2"}
    assert out["per_channel"]["2"]["cdl_F"] == pytest.approx(5e-6, rel=1e-3)
    assert out["figures_emitted"] == 1
    assert len(figures) == 1


def test_one_file_is_refused_with_the_reason(files, ecsa):
    out = ecsa({"paths": files[:1]})
    assert out["ok"] is False
    assert "two or more" in out["error"]


def test_path_and_paths_together_are_refused(files, ecsa):
    out = ecsa({"paths": files, "path": files[0]})
    assert out["ok"] is False
    assert "not both" in out["error"]


def test_files_at_one_rate_give_a_per_channel_reason(tmp_path, ecsa):
    exporter = PsSessionExporter()
    same = [
        exporter.export_pssession(
            _cv(0.05, i), os.path.join(tmp_path, f"same_{i}.pssession")
        )
        for i in range(2)
    ]
    out = ecsa({"paths": same})
    assert out["ok"] is False
    assert "No channel could be fitted" in out["error"]


def test_the_single_file_form_still_needs_a_path(ecsa):
    out = ecsa({})
    assert out["ok"] is False
    assert "path" in out["error"]


def test_scan_rates_with_paths_are_refused(files, ecsa):
    out = ecsa({"paths": files, "scan_rates": {"x": 0.05}})
    assert out["ok"] is False
    assert "single-file form" in out["error"]


def _session_file(tmp_path, name, methods):
    import json as _json

    text = _json.dumps({
        "MethodForMeasurement": methods[0],
        "Measurements": [{"Method": m} for m in methods[1:]],
    })
    path = tmp_path / name
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    return str(path)


def test_a_file_with_several_cv_rates_gives_no_single_rate(tmp_path):
    path = _session_file(tmp_path, "multi.pssession", [
        "METHOD_ID=cv\nSCAN_RATE=2.0E-02",
        "METHOD_ID=cv\nSCAN_RATE=5.0E-02",
    ])
    assert session_scan_rates(path) == pytest.approx([0.02, 0.05])
    assert session_scan_rate(path) is None


def test_another_techniques_rate_is_not_counted(tmp_path):
    path = _session_file(tmp_path, "mixed.pssession", [
        "METHOD_ID=cv\nSCAN_RATE=2.0E-02",
        "METHOD_ID=lsv\nSCAN_RATE=5.0E-02",
    ])
    assert session_scan_rate(path) == pytest.approx(0.02)


def test_two_files_with_one_name_are_refused(tmp_path, ecsa):
    exporter = PsSessionExporter()
    paths = []
    for i, rate in enumerate((0.02, 0.05)):
        folder = tmp_path / f"run_{i}"
        folder.mkdir()
        paths.append(exporter.export_pssession(
            _cv(rate, i), os.path.join(folder, "cv.pssession")
        ))
    out = ecsa({"paths": paths})
    assert out["ok"] is False
    assert "Two files are named" in out["error"]


def test_a_non_json_file_without_a_cv_method_gives_no_rate(tmp_path):
    path = tmp_path / "lsv.txt"
    lines = ["METHOD_ID=lsv", "SCAN_RATE=5.0E-02", ""]
    path.write_text(chr(10).join(lines), encoding="utf-8")
    assert session_scan_rates(str(path)) == []


def test_a_non_json_file_with_a_cv_method_gives_its_rate(tmp_path):
    path = tmp_path / "cv.txt"
    lines = ["METHOD_ID=cv", "SCAN_RATE=5.0E-02", ""]
    path.write_text(chr(10).join(lines), encoding="utf-8")
    assert session_scan_rates(str(path)) == pytest.approx([0.05])

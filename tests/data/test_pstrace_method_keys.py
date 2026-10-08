"""Tests for PSTrace method-string key fidelity.

PSTrace's method parser reads specific KEY=value tokens; our generic
uppercased param dump used different names, so PSTrace ignored them and
showed template defaults. These guard the param->PSTrace-key mapping and
the TECHNIQUE= enum against native PSTrace reference files (CA/CV/SWV/EIS).
See docs/references/pstrace_method_keys.md.
"""

from __future__ import annotations

from datetime import datetime

from src.data.models import MeasurementResult
from src.data.pssession_exporter import build_method_string


def _method(technique: str, params: dict) -> str:
    res = MeasurementResult(
        technique=technique,
        start_time=datetime(2026, 6, 4),
        params=params,
        channels=[1],
        re_ce_channels=[15],
        electrode_config_mode="external",
    )
    return build_method_string(technique, res)


def _keys(method: str) -> set[str]:
    return {
        ln.split("=", 1)[0]
        for ln in method.split("\r\n")
        if ln and not ln.startswith("#") and "=" in ln
    }


def test_cv_uses_vtx_keys_and_technique_5() -> None:
    m = _method(
        "cv",
        {"t_eq": 2.0, "e_begin": -0.5, "e_vertex1": 0.5, "e_vertex2": -0.5,
         "e_step": 0.01, "scan_rate": 0.1, "n_scans": 2},
    )
    keys = _keys(m)
    assert {"E_VTX1", "E_VTX2", "T_EQUIL", "E_BEGIN", "E_STEP",
            "SCAN_RATE", "N_SCANS"} <= keys
    assert not ({"E_VERTEX1", "E_VERTEX2", "T_EQ"} & keys)
    assert "TECHNIQUE=5" in m


def test_swv_uses_eamp_freq_and_technique_2() -> None:
    m = _method(
        "swv",
        {"t_eq": 3.0, "e_begin": -0.5, "e_end": 0.0, "e_step": 0.005,
         "amplitude": 0.04, "frequency": 60.0},
    )
    keys = _keys(m)
    assert {"E_AMP", "FREQ", "T_EQUIL", "E_BEGIN", "E_END"} <= keys
    assert not ({"AMPLITUDE", "FREQUENCY", "T_EQ"} & keys)
    assert "TECHNIQUE=2" in m


def test_eis_uses_minmax_freq_e_amplitude_and_technique_14() -> None:
    m = _method(
        "eis",
        {"t_eq": 5.0, "e_dc": 0.0, "e_ac": 0.01, "freq_start": 1e5,
         "freq_end": 0.1, "n_freq": 50},
    )
    keys = _keys(m)
    assert {"MAX_FREQ", "MIN_FREQ", "E", "AMPLITUDE", "N_FREQ",
            "T_EQUIL"} <= keys
    assert not ({"FREQ_START", "FREQ_END", "E_DC", "E_AC"} & keys)
    assert "TECHNIQUE=14" in m


def test_ca_uses_e_key_and_technique_7() -> None:
    m = _method(
        "ca",
        {"t_eq": 0.0, "e_dc": 0.7, "t_run": 10.0, "t_interval": 0.1},
    )
    keys = _keys(m)
    assert {"E", "T_EQUIL", "T_RUN", "T_INTERVAL"} <= keys
    assert not ({"E_DC", "T_EQ"} & keys)
    assert "TECHNIQUE=7" in m


def test_voltammetry_technique_numbers_match_table5() -> None:
    """lsv/dpv/swv were 2/3/4 (wrong); manual Table 5 is 0/1/2."""
    from src.data.pssession_exporter import _TECHNIQUE_NUMBER

    assert _TECHNIQUE_NUMBER["lsv"] == 0
    assert _TECHNIQUE_NUMBER["dpv"] == 1
    assert _TECHNIQUE_NUMBER["swv"] == 2
    assert _TECHNIQUE_NUMBER["cv"] == 5
    assert _TECHNIQUE_NUMBER["ca"] == 7
    assert _TECHNIQUE_NUMBER["eis"] == 14

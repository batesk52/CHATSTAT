"""Tests for electrode-config provenance in CSV + .pssession exports.

Batch 2 of WS-electrode-config-modes adds two new lines to per-channel
CSV headers (``# Electrode config:`` and ``# RE/CE channel:``) and two
new METHOD entries inside the .pssession method string
(``ELECTRODE_CONFIG_MODE`` and ``RE_CE_CHANNELS``). These tests pin
the header content and backward-compat defaults when the model fields
are empty.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from src.data.exporters import CSVExporter
from src.data.models import DataPoint, MeasurementResult
from src.data.pssession_exporter import build_method_string


# ---------------------------------------------------------------------------
# CSV header
# ---------------------------------------------------------------------------


def _make_result(
    channels: list[int],
    mode: str,
    re_ce: list[int] | None = None,
) -> MeasurementResult:
    """Build a minimal MeasurementResult for header inspection."""
    res = MeasurementResult(
        technique="cv",
        start_time=datetime(2026, 5, 23, 12, 0, 0),
        params={"e_step": 0.01},
        channels=channels,
        re_ce_channels=re_ce or [],
        electrode_config_mode=mode,
    )
    for ch in channels:
        res.add_point(
            DataPoint(
                timestamp=0.0,
                channel=ch,
                variables={"current": 1e-6, "set_potential": 0.0},
            )
        )
    return res


def _read_header(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.startswith("#")]


def test_csv_header_emits_external_mode_with_ch15(tmp_path) -> None:
    """External-mode runs report mode + CH15 in every per-channel CSV."""
    result = _make_result(
        channels=[1, 2],
        mode="external",
        re_ce=[15, 15],
    )
    CSVExporter().export_csv(result, str(tmp_path))

    header = _read_header(str(tmp_path / "ch01.csv"))
    assert "# Electrode config: external" in header
    assert "# RE/CE channel: 15" in header


def test_csv_header_emits_manual_pair_per_channel(tmp_path) -> None:
    """Manual mode pairs WE -> RE/CE by index into result.channels."""
    result = _make_result(
        channels=[3, 7, 11],
        mode="manual",
        re_ce=[1, 7, 13],
    )
    CSVExporter().export_csv(result, str(tmp_path))

    h3 = _read_header(str(tmp_path / "ch03.csv"))
    h7 = _read_header(str(tmp_path / "ch07.csv"))
    h11 = _read_header(str(tmp_path / "ch11.csv"))

    assert "# Electrode config: manual" in h3
    assert "# RE/CE channel: 1" in h3
    assert "# Electrode config: manual" in h7
    assert "# RE/CE channel: 7" in h7
    assert "# Electrode config: manual" in h11
    assert "# RE/CE channel: 13" in h11


def test_csv_header_defaults_when_metadata_absent(tmp_path) -> None:
    """Legacy results (empty mode + empty re_ce list) default cleanly."""
    # Don't pass electrode_config_mode kwarg — accept dataclass default
    # but blank the value to simulate an older pickle.
    res = MeasurementResult(
        technique="cv",
        params={},
        channels=[5],
        re_ce_channels=[],
        electrode_config_mode="",
    )
    res.add_point(
        DataPoint(
            timestamp=0.0,
            channel=5,
            variables={"current": 0.0},
        )
    )
    CSVExporter().export_csv(res, str(tmp_path))
    header = _read_header(str(tmp_path / "ch05.csv"))
    assert "# Electrode config: external" in header
    # Empty mode collapses to "external", whose RE/CE position is 15
    # (the mode-derived fallback — not the legacy hardcoded 1).
    assert "# RE/CE channel: 15" in header


# ---------------------------------------------------------------------------
# .pssession method string
# ---------------------------------------------------------------------------


def test_pssession_method_string_carries_external_mode() -> None:
    """build_method_string emits ELECTRODE_CONFIG_MODE for external."""
    res = _make_result(
        channels=[1, 2],
        mode="external",
        re_ce=[15, 15],
    )
    method_str = build_method_string("cv", res)
    assert "ELECTRODE_CONFIG_MODE=external" in method_str
    assert "RE_CE_CHANNELS=1:15,2:15" in method_str


def test_pssession_method_string_carries_manual_pairs() -> None:
    """Manual mode emits per-channel WE:RE_CE pairs in order."""
    res = _make_result(
        channels=[2, 4, 6],
        mode="manual",
        re_ce=[2, 4, 6],
    )
    method_str = build_method_string("cv", res)
    assert "ELECTRODE_CONFIG_MODE=manual" in method_str
    assert "RE_CE_CHANNELS=2:2,4:4,6:6" in method_str


def test_pssession_method_string_omits_re_ce_when_empty() -> None:
    """No RE_CE_CHANNELS line when re_ce_channels list is empty."""
    res = MeasurementResult(
        technique="cv",
        params={},
        channels=[1],
        re_ce_channels=[],
        electrode_config_mode="external",
    )
    method_str = build_method_string("cv", res)
    assert "ELECTRODE_CONFIG_MODE=external" in method_str
    assert "RE_CE_CHANNELS=" not in method_str


# ---------------------------------------------------------------------------
# Per-Curve / per-EISData / session-level electrode-config metadata
# ---------------------------------------------------------------------------


def _result_with_ca_data(
    channels: list[int],
    mode: str,
    re_ce: list[int],
) -> MeasurementResult:
    """Build a CA result with two samples per channel for curve export."""
    res = MeasurementResult(
        technique="ca",
        start_time=datetime(2026, 5, 23, 12, 0, 0),
        params={},
        channels=channels,
        re_ce_channels=re_ce,
        electrode_config_mode=mode,
    )
    for ch in channels:
        for t in (0.0, 0.1):
            res.add_point(
                DataPoint(
                    timestamp=t,
                    channel=ch,
                    variables={"current": 1e-6, "potential": 0.2},
                )
            )
    return res


def test_curve_metadata_carries_mux_and_re_ce_per_channel() -> None:
    """Each CA curve carries MUXChannel + ReCeChannel + mode."""
    from src.data.pssession_curves import build_curves_measurement

    res = _result_with_ca_data(
        channels=[1, 3], mode="manual", re_ce=[13, 1]
    )
    meas = build_curves_measurement(res, "Chronoamperometry", "method")
    curves = meas["Curves"]
    assert len(curves) == 2
    by_we = {c["MUXChannel"]: c for c in curves}
    assert by_we[1]["ReCeChannel"] == 13
    assert by_we[3]["ReCeChannel"] == 1
    assert all(c["ElectrodeConfigMode"] == "manual" for c in curves)


def test_curve_metadata_falls_back_when_re_ce_absent() -> None:
    """Results without re_ce_channels record the mode-derived RE/CE.

    Empty mode collapses to "external", whose RE/CE position is 15 — the
    mode-derived fallback that keeps provenance consistent with the
    declared mode (rather than the legacy hardcoded 1).
    """
    from src.data.pssession_curves import build_curves_measurement

    res = _result_with_ca_data(
        channels=[1, 2], mode="", re_ce=[]
    )
    meas = build_curves_measurement(res, "Chronoamperometry", "method")
    curves = meas["Curves"]
    assert all(c["ReCeChannel"] == 15 for c in curves)
    # Empty mode collapses to "external" backward-compat default
    assert all(c["ElectrodeConfigMode"] == "external" for c in curves)


def test_eis_entry_metadata_carries_mux_and_re_ce() -> None:
    """Each EISData entry carries MUXChannel + ReCeChannel + mode."""
    from src.data.pssession_eis import build_eis_measurement

    res = MeasurementResult(
        technique="eis",
        start_time=datetime(2026, 5, 23, 12, 0, 0),
        params={},
        channels=[1, 4],
        re_ce_channels=[15, 15],
        electrode_config_mode="external",
    )
    for ch in (1, 4):
        for f in (1000.0, 100.0):
            res.add_point(
                DataPoint(
                    timestamp=0.0,
                    channel=ch,
                    variables={
                        "set_frequency": f,
                        "zreal": 1.0,
                        "zimag": -1.0,
                    },
                )
            )
    meas = build_eis_measurement(res, "EIS", "method")
    entries = meas["EISDataList"]
    assert len(entries) == 2
    by_we = {e["MUXChannel"]: e for e in entries}
    assert by_we[1]["ReCeChannel"] == 15
    assert by_we[4]["ReCeChannel"] == 15
    assert all(e["ElectrodeConfigMode"] == "external" for e in entries)


def test_session_dict_carries_session_level_metadata(tmp_path) -> None:
    """Top-level session dict surfaces mode + ReCeChannels for re-imports."""
    from src.data.pssession_exporter import PsSessionExporter

    res = _result_with_ca_data(
        channels=[1, 3], mode="manual", re_ce=[13, 1]
    )
    exporter = PsSessionExporter()
    # Use the internal builder so we can inspect the dict before encoding.
    session = exporter._build_session(res)
    assert session["ElectrodeConfigMode"] == "manual"
    assert session["ReCeChannels"] == [13, 1]

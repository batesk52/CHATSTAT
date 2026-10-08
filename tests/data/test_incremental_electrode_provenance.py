"""Auto-saved CSVs must carry the same electrode provenance as exports.

Regression guard for a real gap found 2026-07-28: the Req 10
electrode-config metadata (``# Electrode config:`` / ``# RE/CE
channel:``) was added to :class:`CSVExporter` but never backported to
:class:`IncrementalCSVWriter`.  Auto-save is the crash-safety path used
for in-vivo runs, so those CSVs silently lost their wiring provenance
while both the README and the writer's own docstring claimed the two
formats were "identical".

These tests assert against the OTHER writer's real output rather than
against a hardcoded string, so the two cannot drift apart again without
failing here.
"""

from __future__ import annotations

import os

from src.data.exporters import CSVExporter
from src.data.incremental_writer import IncrementalCSVWriter
from src.data.models import DataPoint, MeasurementResult


def _comment_lines(path: str) -> list[str]:
    """Return the ``#`` header lines of a CSV, in order."""
    with open(path, encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh if ln.startswith("#")]


def _provenance(path: str) -> list[str]:
    """Return only the electrode-provenance header lines."""
    return [
        ln
        for ln in _comment_lines(path)
        if ln.startswith(("# Electrode config:", "# RE/CE channel:"))
    ]


def _points(channels: list[int]) -> list[DataPoint]:
    return [
        DataPoint(
            timestamp=float(i),
            channel=ch,
            variables={"potential": 0.1 * i, "current": 1e-6 * i},
        )
        for i, ch in enumerate(channels)
    ]


def _run_both(tmp_path, mode: str, channels: list[int], re_ce: list[int]):
    """Write the same run through both writers; return their dirs."""
    points = _points(channels)

    auto_dir = os.path.join(str(tmp_path), "auto")
    os.makedirs(auto_dir, exist_ok=True)
    writer = IncrementalCSVWriter()
    writer.start(
        technique="ca",
        params={"e_dc": 0.85},
        device_info={},
        channels=channels,
        output_dir=auto_dir,
        exact_dir=True,
        electrode_config_mode=mode,
        re_ce_channels=re_ce,
    )
    writer.flush_points(points)
    writer.finish()

    result = MeasurementResult(
        technique="ca",
        params={"e_dc": 0.85},
        channels=channels,
        electrode_config_mode=mode,
        re_ce_channels=re_ce,
    )
    for dp in points:
        result.data_points.append(dp)
    export_dir = os.path.join(str(tmp_path), "export")
    os.makedirs(export_dir, exist_ok=True)
    CSVExporter().export_csv(result, export_dir)
    return auto_dir, export_dir


def test_manual_mode_provenance_matches_exporter(tmp_path) -> None:
    """Mode C: each channel's own RE/CE pairing reaches the auto-save."""
    channels = [1, 2, 3]
    re_ce = [11, 12, 13]
    auto_dir, export_dir = _run_both(tmp_path, "manual", channels, re_ce)

    for ch in channels:
        name = f"ch{ch:02d}.csv"
        auto = _provenance(os.path.join(auto_dir, name))
        exported = _provenance(os.path.join(export_dir, name))
        assert auto == exported, (
            f"ch{ch}: auto-save provenance {auto} != export {exported}"
        )

    # Ground truth: channel 2 really is paired with RE/CE 12, so the
    # test would fail if both writers shared the same wrong default.
    assert "# RE/CE channel: 12" in _provenance(
        os.path.join(auto_dir, "ch02.csv")
    )


def test_external_mode_falls_back_to_mode_default(tmp_path) -> None:
    """With no explicit pairing, both writers use the mode's default."""
    channels = [1, 2]
    auto_dir, export_dir = _run_both(tmp_path, "external", channels, [])

    for ch in channels:
        name = f"ch{ch:02d}.csv"
        assert _provenance(os.path.join(auto_dir, name)) == _provenance(
            os.path.join(export_dir, name)
        )

    lines = _provenance(os.path.join(auto_dir, "ch01.csv"))
    assert "# Electrode config: external" in lines
    assert "# RE/CE channel: 15" in lines


def test_provenance_present_even_with_no_config(tmp_path) -> None:
    """A bare run still records a mode rather than omitting the lines.

    This is the exact shape of the original defect: the lines being
    absent entirely from the auto-save path.
    """
    writer = IncrementalCSVWriter()
    writer.start(
        technique="ca",
        params={},
        device_info={},
        channels=[1],
        output_dir=str(tmp_path),
        exact_dir=True,
    )
    writer.flush_points(_points([1]))
    writer.finish()

    lines = _provenance(os.path.join(str(tmp_path), "ch01.csv"))
    assert len(lines) == 2, f"provenance lines missing: {lines}"
    assert "# Electrode config: external" in lines

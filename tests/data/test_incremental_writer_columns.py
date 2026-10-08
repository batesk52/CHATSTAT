"""Tests for IncrementalCSVWriter column handling (D1 regression).

The header is frozen on the first flush per channel (CSV headers can't
be rewritten mid-stream), so columns are seeded from the technique's
canonical schema. These tests guard against the regression where a
variable first seen in a *later* packet would be silently dropped.
"""

from __future__ import annotations

import csv

from src.data.incremental_writer import IncrementalCSVWriter
from src.data.models import DataPoint


def _read_csv_rows(path: str) -> tuple[list[str], list[list[str]]]:
    """Return (header_row, data_rows), skipping ``#`` comment lines."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if not (r and r[0].startswith("#"))]
    return rows[0], rows[1:]


def test_later_packet_variable_is_not_dropped(tmp_path) -> None:
    """A variable absent from the first packet still gets a column.

    EIS is the realistic case: the canonical schema is seeded so that
    even if the first packet is sparse, every schema column is present
    and later values land under it instead of being discarded.
    """
    writer = IncrementalCSVWriter()
    writer.start(
        technique="eis",
        params={},
        device_info={},
        channels=[1],
        output_dir=str(tmp_path),
    )

    # First packet is SPARSE — missing zimag/phase that appear later.
    writer.flush_points([
        DataPoint(
            timestamp=0.0,
            channel=1,
            variables={"set_frequency": 1000.0, "zreal": 100.0},
        )
    ])
    # Later packet introduces the full variable set.
    writer.flush_points([
        DataPoint(
            timestamp=1.0,
            channel=1,
            variables={
                "set_frequency": 100.0,
                "zreal": 90.0,
                "zimag": -45.0,
                "phase": -26.0,
                "impedance": 100.6,
            },
        )
    ])
    paths = writer.finish()
    assert len(paths) == 1

    header, data = _read_csv_rows(paths[0])
    # Every EIS schema variable must have a column despite the sparse
    # first packet — otherwise the later zimag/phase/impedance are lost.
    for col in ("set_frequency", "zreal", "zimag", "phase", "impedance"):
        assert col in header, f"{col} missing from header {header}"

    # The later point's zimag value must actually be written, not dropped.
    zimag_idx = header.index("zimag")
    assert data[1][zimag_idx] == "-45.0"


def test_columns_match_csv_exporter_for_uniform_packets(tmp_path) -> None:
    """Uniform packets produce the documented amperometry column order."""
    writer = IncrementalCSVWriter()
    writer.start(
        technique="ca",
        params={},
        device_info={},
        channels=[1],
        output_dir=str(tmp_path),
    )
    writer.flush_points([
        DataPoint(
            timestamp=0.0,
            channel=1,
            variables={"current": 1e-6, "set_potential": 0.2},
        )
    ])
    paths = writer.finish()
    header, _ = _read_csv_rows(paths[0])
    # timestamp first, then technique-preferred order (current before
    # set_potential for amperometry).
    assert header[0] == "timestamp"
    assert header.index("current") < header.index("set_potential")

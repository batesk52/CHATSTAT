"""IncrementalCSVWriter exact-dir mode (PR #13 review finding #1).

With ``exact_dir=True`` the writer writes directly into the caller's
directory instead of creating its second-resolution
``<ts>_<technique>_autosave`` leaf — the sequencer relies on this so two
same-second repeats of one technique land in their own ``stepNN`` dirs
and can never truncate each other's CSVs.
"""

from __future__ import annotations

import os

from src.data.incremental_writer import IncrementalCSVWriter
from src.data.models import DataPoint


def _point(ch=1):
    return DataPoint(
        timestamp=0.1,
        channel=ch,
        variables={"potential": 0.1, "current": 1e-5},
    )


def test_exact_dir_writes_directly_into_given_dir(tmp_path):
    """exact_dir=True uses output_dir verbatim (no timestamped leaf)."""
    target = tmp_path / "step01_ca"
    writer = IncrementalCSVWriter()
    out = writer.start(
        technique="ca",
        params={},
        device_info={},
        channels=[1],
        output_dir=str(target),
        exact_dir=True,
    )
    writer.flush_points([_point()])
    writer.finish()

    assert os.path.normpath(out) == os.path.normpath(str(target))
    assert (target / "ch01.csv").exists()
    # No nested *_autosave leaf was created.
    assert [p.name for p in target.iterdir()] == ["ch01.csv"]


def test_default_mode_still_creates_timestamped_leaf(tmp_path):
    """Without exact_dir the legacy timestamped leaf behavior holds."""
    writer = IncrementalCSVWriter()
    out = writer.start(
        technique="ca",
        params={},
        device_info={},
        channels=[1],
        output_dir=str(tmp_path),
    )
    writer.flush_points([_point()])
    writer.finish()

    leaf = os.path.basename(out)
    assert leaf.endswith("_ca_autosave")
    assert os.path.dirname(os.path.normpath(out)) == os.path.normpath(
        str(tmp_path)
    )


def test_two_exact_dir_runs_same_second_do_not_collide(tmp_path):
    """Same-second back-to-back runs land in distinct dirs (regression).

    This is the repeat-collision scenario: with the old shared parent,
    both runs resolved to one ``<ts>_ca_autosave`` dir and run 2's
    mode-"w" open truncated run 1's ch01.csv.
    """
    for i in range(2):
        writer = IncrementalCSVWriter()
        writer.start(
            technique="ca",
            params={"run": i},
            device_info={},
            channels=[1],
            output_dir=str(tmp_path / f"step{i + 1:02d}_ca"),
            exact_dir=True,
        )
        writer.flush_points([_point()])
        writer.finish()

    assert (tmp_path / "step01_ca" / "ch01.csv").exists()
    assert (tmp_path / "step02_ca" / "ch01.csv").exists()
    # Run 1's data survived run 2 (no truncation).
    content = (tmp_path / "step01_ca" / "ch01.csv").read_text()
    assert "current" in content  # header intact

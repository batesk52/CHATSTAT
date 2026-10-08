"""Vendored CMU.49.011 electrochemistry analysis toolkit (READ-ONLY).

PROVENANCE
    Source repo:   CMU.49.011-Electrochemistry
    Source commit: e35ccfc ("updated analysis")
    Copied:        2026-06-09
    Packages:      src/analysis, src/dataloaders, src/utils
    Mechanical edits only: absolute imports rewritten from
    ``src.<pkg>`` / ``<pkg>`` to ``src.vendor.electrochem_analysis.<pkg>``;
    internal sample and project IDs removed from comments and
    docstrings for the public release, and the module docstring of
    ``utils/path_utils.py`` made raw to silence an invalid-escape
    warning (2026-10-08).
    No behavioral edits. Fixes go upstream in CMU.49.011, then re-copy.

The analysis modules import ``matplotlib.pyplot`` at module top; the
Agg backend is forced here, before any of them load, so the vendored
code renders figures headlessly and never spins up a Qt/Tk canvas
inside the host application (Windows DLL/threading safety).
"""

import matplotlib

matplotlib.use("Agg", force=True)

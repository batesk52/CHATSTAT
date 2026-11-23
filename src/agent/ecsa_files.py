"""Gather scan-rate CVs for ECSA from several session files.

The agent records one scan rate per run, and every run exports to its own
.pssession file. The ECSA (double-layer capacitance) fit needs the CVs of
one electrode at several scan rates together. This module reads each
file's scan rate from its stored method (``SCAN_RATE=``), groups the CV
scans by multiplexer channel (scan names end in ``- Ch<N>``), and returns
one set of scans and rates per channel for the fit.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

__all__ = [
    "EcsaFilesError",
    "channel_of",
    "group_cv_scans",
    "session_scan_rate",
]

_SCAN_RATE_RE = re.compile(r"SCAN_RATE=\s*([-+0-9.eE]+)")
_CHANNEL_RE = re.compile(r"-\s*Ch\s*(\d+)\s*$", re.IGNORECASE)

#: Group key for CV scans whose name carries no channel number.
NO_CHANNEL = "all"


class EcsaFilesError(Exception):
    """A file cannot take part in a multi-file ECSA fit."""


def session_scan_rate(path: str) -> Optional[float]:
    """Return the scan rate (V/s) stored in a .pssession file, or None.

    The method text is stored inside the session JSON as lines such as
    ``SCAN_RATE=2.0000000E-02``. The file is UTF-16 with a byte-order
    mark; other encodings are tried in case a tool rewrote it.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-16", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        match = _SCAN_RATE_RE.search(text)
        if match:
            try:
                rate = float(match.group(1))
            except ValueError:
                return None
            return rate if rate > 0 else None
    return None


def channel_of(scan_name: str) -> Optional[int]:
    """Return the multiplexer channel at the end of a scan name, or None."""
    match = _CHANNEL_RE.search(scan_name)
    return int(match.group(1)) if match else None


def group_cv_scans(
    paths: list[str],
    load_scans: Callable[[str], dict[str, Any]],
    pick_cv: Callable[[dict[str, Any]], dict[str, Any]],
    channel: Optional[int] = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, float]], list]:
    """Group the CV scans of several files by channel, with their rates.

    Args:
        paths: Existing .pssession files, one scan rate each.
        load_scans: Loads a file into ``{scan_name: DataFrame}``.
        pick_cv: Keeps the CV scans of a loaded file.
        channel: When given, keep only this channel's scans.

    Returns:
        ``(scans, rates, files)``: ``scans[key][label]`` is a CV DataFrame
        and ``rates[key][label]`` its scan rate in V/s, where ``key`` is
        the channel number as a string (or ``"all"`` for scan names with
        no channel) and ``label`` names the file and the scan; ``files``
        lists ``{"path", "scan_rate_v_per_s", "cv_scans"}`` per file.

    Raises:
        EcsaFilesError: A file has no CV scan or no stored scan rate.
    """
    scans: dict[str, dict[str, Any]] = {}
    rates: dict[str, dict[str, float]] = {}
    files: list[dict[str, Any]] = []
    for path in paths:
        cv = pick_cv(load_scans(path))
        name = os.path.basename(path)
        if not cv:
            raise EcsaFilesError(f"{name} contains no CV scan.")
        rate = session_scan_rate(path)
        if rate is None:
            raise EcsaFilesError(
                f"{name} stores no scan rate (no SCAN_RATE in its "
                "method). Use the single-file form with 'path' and "
                "'scan_rates' for such a file."
            )
        files.append({
            "path": path,
            "scan_rate_v_per_s": rate,
            "cv_scans": sorted(cv),
        })
        for scan_name, frame in cv.items():
            ch = channel_of(scan_name)
            if channel is not None and ch != channel:
                continue
            key = NO_CHANNEL if ch is None else str(ch)
            label = f"{name} | {scan_name}"
            scans.setdefault(key, {})[label] = frame
            rates.setdefault(key, {})[label] = rate
    return scans, rates, files

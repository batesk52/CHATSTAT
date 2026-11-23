"""Gather scan-rate CVs for ECSA from several session files.

The agent records one scan rate per run, and every run exports to its own
.pssession file. The ECSA (double-layer capacitance) fit needs the CVs of
one electrode at several scan rates together. This module reads each
file's scan rate from its stored method (``SCAN_RATE=``), groups the CV
scans by multiplexer channel (scan names end in ``- Ch<N>``), and returns
one set of scans and rates per channel for the fit.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

__all__ = [
    "EcsaFilesError",
    "channel_of",
    "group_cv_scans",
    "session_scan_rate",
    "session_scan_rates",
]

_SCAN_RATE_RE = re.compile(r"SCAN_RATE=\s*([-+0-9.eE]+)")
_CHANNEL_RE = re.compile(r"-\s*Ch\s*(\d+)\s*$", re.IGNORECASE)
_CV_METHOD_RE = re.compile(
    r"^(METHOD_ID=cv|TECHNIQUE=5)\s*$", re.IGNORECASE | re.MULTILINE
)

#: Group key for CV scans whose name carries no channel number.
NO_CHANNEL = "all"


class EcsaFilesError(Exception):
    """A file cannot take part in a multi-file ECSA fit."""


def _session_text(path: str) -> str:
    """Decode a .pssession file: UTF-16 when it has a UTF-16 byte-order
    mark (as PSTrace and ChatStat write it), else UTF-8."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _cv_method_texts(text: str) -> list[str]:
    """The stored method strings of the CV measurements in a session.

    Method strings sit in the session JSON under ``MethodForMeasurement``
    and ``Method`` keys; a CV method says ``METHOD_ID=cv`` or
    ``TECHNIQUE=5``. When the text is not JSON, it is split into method
    blocks at each ``METHOD_ID=`` line and only the CV blocks are kept.
    """
    try:
        data = json.loads(text.strip("\ufeff\x00 \r\n\t"))
    except ValueError:
        blocks = re.split(r"(?m)^(?=METHOD_ID=)", text)
        return [b for b in blocks if _CV_METHOD_RE.search(b)]
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("MethodForMeasurement", "Method") and isinstance(
                    value, str
                ):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return [
        m for m in found
        if _CV_METHOD_RE.search(m)
    ]


def session_scan_rates(path: str) -> list[float]:
    """Every distinct positive CV scan rate (V/s) stored in a .pssession file.

    The method text is stored inside the session JSON as lines such as
    ``SCAN_RATE=2.0000000E-02``; a file with several measurement passes
    carries one method per pass. Only CV methods are read, so another
    technique's scan rate in the same file is not counted.
    """
    rates: set[float] = set()
    methods = _cv_method_texts(_session_text(path))
    for value in _SCAN_RATE_RE.findall("\n".join(methods)):
        try:
            rate = float(value)
        except ValueError:
            continue
        if rate > 0:
            rates.add(rate)
    return sorted(rates)


def session_scan_rate(path: str) -> Optional[float]:
    """The one scan rate (V/s) a .pssession file stores, or None.

    None when the file stores no scan rate or more than one (a file with
    several rates belongs to the single-file form of analyze_ecsa).
    """
    rates = session_scan_rates(path)
    return rates[0] if len(rates) == 1 else None


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
        if any(os.path.basename(f["path"]) == name for f in files):
            raise EcsaFilesError(
                f"Two files are named {name}; give files with different "
                "names so no scan rate is lost."
            )
        if not cv:
            raise EcsaFilesError(f"{name} contains no CV scan.")
        stored = session_scan_rates(path)
        if len(stored) != 1:
            what = (
                "no scan rate in a CV method (no CV method with SCAN_RATE)"
                if not stored
                else f"{len(stored)} scan rates ({stored} V/s)"
            )
            raise EcsaFilesError(
                f"{name} stores {what}; the 'paths' form needs one "
                "rate per file. Use the single-file form with 'path' "
                "and 'scan_rates' for such a file."
            )
        rate = stored[0]
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

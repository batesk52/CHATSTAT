"""Which cycle of a multi-cycle CV the analysis reads, and how many exist.

ChatStat exports a CV with ``n_scans`` cycles as one curve per cycle per
channel. The vendored loader returns ONE of them per channel: the curve
picked by ``curve_index`` (1 = first, -1 = last, the default), and it
clamps an index out of range without saying so. This module counts the
curves, so the tool can refuse a cycle that does not exist and always say
which one it used. The vendored code is read-only; this only reads it.
"""

from __future__ import annotations

from typing import Any, Optional

from src.vendor.electrochem_analysis.dataloaders.psession_parser import (
    _detect_channel_groups,
    parse_pssession_file,
)

__all__ = ["CycleChoiceError", "count_cycles", "check_cycle"]


class CycleChoiceError(ValueError):
    """The requested cycle is not a valid choice."""


def _curves(measurement: dict[str, Any]) -> list[Any]:
    curves = measurement.get("curves") or measurement.get("Curves") or []
    return curves if isinstance(curves, list) else []


def count_cycles(path: str, scan_name: str) -> Optional[int]:
    """Number of cycles (curves per channel) behind *scan_name*, or None.

    The loader names a multichannel scan ``"<title> - Ch<N>"``; the curves
    of that measurement are grouped by channel, and every channel of one CV
    run has the same number. A later measurement with the same title wins,
    as it does in the loader.
    """
    base = scan_name.rsplit(" - Ch", 1)[0]
    try:
        sessions = parse_pssession_file(path)
    except Exception:  # noqa: BLE001 - an unknown count, not a failed tool
        return None
    if not isinstance(sessions, list):
        return None
    several = len(sessions) > 1
    found: Optional[int] = None
    for obj_idx, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        measurements = (
            session.get("measurements") or session.get("Measurements") or []
        )
        for i, measurement in enumerate(measurements):
            if not isinstance(measurement, dict):
                continue
            title = (
                measurement.get("title")
                or measurement.get("Title")
                or f"Measurement_{i + 1}"
            )
            if several:
                title = f"{title}_obj{obj_idx}"
            if title not in (base, scan_name):
                continue
            curves = _curves(measurement)
            if not curves:
                continue
            groups = _detect_channel_groups(curves)
            found = (
                max(len(v) for v in groups.values()) if groups else len(curves)
            )
    return found


def check_cycle(choice: Any) -> int:
    """Validate the tool's ``cycle`` argument: 1, 2, ... or -1 (last)."""
    message = (
        "cycle must be a whole number: 1 for the first cycle, 2 for the "
        "second, ..., or -1 for the last."
    )
    if isinstance(choice, bool) or not isinstance(choice, (int, float)):
        raise CycleChoiceError(message)
    if float(choice) != int(choice):
        raise CycleChoiceError(message)
    c = int(choice)
    if c == -1 or c >= 1:
        return c
    raise CycleChoiceError(message)

"""Score an agent's final answer against a goal's expected values.

Deliberately simple. Two kinds of check are supported:

* ``resistance_ohm``: a map of channel number to resistance in ohms,
  with ``tolerance_pct`` (default 5). The answer counts for a channel
  only when it names the channel and gives a value WITH a unit
  (``ohm``, ``kOhm``, ``MOhm`` or the omega sign), for example
  ``Channel 3: 2.02 kOhm`` or ``| CH16 | 8.70 kOhm |``. A table whose
  header names the unit once (``| Channel | Resistance (ohm) |``) also
  counts: its bare numbers take the unit of that column, and such a table
  outranks values in running text. Otherwise the first value given for a
  channel is the one scored.
* ``mentions``: strings that must appear in the answer (case-insensitive).

A goal with no expected values gets no score.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_UNIT = r"(?P<prefix>[kKM])?\s*(?:ohms?|Ohms?|OHMS?|Ω|Ω)"
_VALUE = r"(?P<value>\d+(?:\.\d+)?)\s*" + _UNIT
_CHANNEL_RE = re.compile(
    r"(?:\bch(?:annel)?\.?\s*#?\s*|^\s*\|\s*(?:ch\s*)?)(?P<ch>\d{1,2})\b"
    r"[^\n]{0,40}?" + _VALUE,
    re.IGNORECASE | re.MULTILINE,
)
_SCALE = {None: 1.0, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6}
_HEADER_UNIT_RE = re.compile(_UNIT)
_CHANNEL_HEADER_RE = re.compile(r"^\s*(?:ch(?:annel)?\.?|#)\s*$", re.IGNORECASE)
_CHANNEL_CELL_RE = re.compile(r"^\s*(?:ch(?:annel)?\.?\s*)?(\d{1,2})\s*$", re.I)
# A cell value, optionally marked approximate ("~1004", "about 1004").
_BARE_NUMBER_RE = re.compile(
    r"^\s*(?:~|about|approx\.?)?\s*(\d+(?:[.,]\d+)?)\s*$", re.IGNORECASE
)


def _cells(line: str) -> list[str]:
    """Split one Markdown table row into its cells."""
    return [c.strip(" *`") for c in line.strip().strip("|").split("|")]


def _table_resistances(text: str) -> dict[int, float]:
    """Read ``{channel: ohms}`` from tables whose header carries the unit.

    A header row needs a channel column (``Channel``, ``Ch`` or ``#``) and a
    column whose title names an ohm unit. Rows below it give the channel
    number and a bare number in that column.
    """
    found: dict[int, float] = {}
    ch_col: Optional[int] = None
    val_col: Optional[int] = None
    scale = 1.0
    for line in (text or "").splitlines():
        if not line.strip().startswith("|"):
            ch_col = val_col = None
            continue
        cells = _cells(line)
        if ch_col is None:
            units = [
                (i, _HEADER_UNIT_RE.search(c)) for i, c in enumerate(cells)
            ]
            units = [(i, m) for i, m in units if m]
            chans = [
                i for i, c in enumerate(cells) if _CHANNEL_HEADER_RE.match(c)
            ]
            if units and chans:
                ch_col, (val_col, match) = chans[0], units[0]
                scale = _SCALE[match.group("prefix")]
            continue
        if max(ch_col, val_col) >= len(cells):
            continue
        ch_match = _CHANNEL_CELL_RE.match(cells[ch_col])
        val_match = _BARE_NUMBER_RE.match(cells[val_col])
        if not ch_match or not val_match:
            continue
        channel = int(ch_match.group(1))
        if 1 <= channel <= 16 and channel not in found:
            found[channel] = float(val_match.group(1).replace(",", "")) * scale
    return found


def extract_resistances(text: str) -> dict[int, float]:
    """Return ``{channel: ohms}`` for every channel the text reports.

    A table whose header names the unit is read first: it is the structured
    answer. Values with their own unit fill in the channels it does not
    cover. Reading prose first let a range sentence ("about 1.0 kohm on
    channel 1 to about 8.6 kohm on channel 16") overwrite the table's value
    for channel 1.
    """
    found: dict[int, float] = dict(_table_resistances(text))
    # Table rows next (a unit in each cell), then running text.
    table_rows = "\n".join(
        line for line in (text or "").splitlines() if line.lstrip().startswith("|")
    )
    for source in (table_rows, text or ""):
        for match in _CHANNEL_RE.finditer(source):
            channel = int(match.group("ch"))
            if not 1 <= channel <= 16 or channel in found:
                continue
            found[channel] = float(match.group("value")) * _SCALE[
                match.group("prefix")
            ]
    return found


def score_answer(
    text: str, expected: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Score *text* against *expected*.

    Args:
        text: The agent's final message.
        expected: The goal's ``expected`` block, or None.

    Returns:
        None when there is nothing to check; otherwise ``{"checks",
        "passed", "score", "details"}`` with one detail line per check.
    """
    expected = expected or {}
    details: list[str] = []
    passed = 0
    checks = 0

    targets = expected.get("resistance_ohm") or {}
    if targets:
        tol = float(expected.get("tolerance_pct", 5.0)) / 100.0
        reported = extract_resistances(text)
        for channel, ohms in sorted(
            (int(k), float(v)) for k, v in targets.items()
        ):
            checks += 1
            got = reported.get(channel)
            ok = got is not None and abs(got - ohms) <= tol * ohms
            passed += int(ok)
            details.append(
                f"ch{channel}: expected {ohms:g} ohm, "
                f"got {'none' if got is None else f'{got:g} ohm'}"
                f" -> {'pass' if ok else 'fail'}"
            )

    lowered = (text or "").lower()
    for phrase in expected.get("mentions") or []:
        checks += 1
        ok = str(phrase).lower() in lowered
        passed += int(ok)
        details.append(f"mentions {phrase!r} -> {'pass' if ok else 'fail'}")

    if checks == 0:
        return None
    return {
        "checks": checks,
        "passed": passed,
        "score": passed / checks,
        "details": details,
    }

"""Score an agent's final answer against a goal's expected values.

Deliberately simple. Two kinds of check are supported:

* ``resistance_ohm``: a map of channel number to resistance in ohms,
  with ``tolerance_pct`` (default 5). The answer counts for a channel
  only when it names the channel and gives a value WITH a unit
  (``ohm``, ``kOhm``, ``MOhm`` or the omega sign), for example
  ``Channel 3: 2.02 kOhm`` or ``| CH16 | 8.70 kOhm |``. The first value
  given for a channel is the one scored.
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


def extract_resistances(text: str) -> dict[int, float]:
    """Return ``{channel: ohms}`` for every channel the text reports."""
    found: dict[int, float] = {}
    for match in _CHANNEL_RE.finditer(text or ""):
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

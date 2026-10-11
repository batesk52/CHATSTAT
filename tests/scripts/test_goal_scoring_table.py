"""Tests for reading resistances from a table whose header names the unit."""

from __future__ import annotations

import os
import sys

import pytest

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from goal_scoring import extract_resistances, score_answer  # noqa: E402

TABLE_OHM = """Here are the values.

| Channel | Resistance (ohm) | Wiring |
|---|---|---|
| 1 | 1003 | manual (RE/CE = 1) |
| 15 | 7495 | external (RE/CE = 15) |
| 16 | 8648 | on_board (RE/CE = 16) |

- Pattern: the values rise by a steady factor.
"""

TABLE_KOHM = """| Ch | I (uA) | R (kOhm) |
|---|---|---|
| CH1 | 99.7 | 1.003 |
| CH16 | 11.56 | 8.648 |
"""


def test_header_unit_applies_to_bare_numbers() -> None:
    assert extract_resistances(TABLE_OHM) == {
        1: pytest.approx(1003.0),
        15: pytest.approx(7495.0),
        16: pytest.approx(8648.0),
    }


def test_header_prefix_scales_the_column() -> None:
    found = extract_resistances(TABLE_KOHM)
    assert found[1] == pytest.approx(1003.0)
    assert found[16] == pytest.approx(8648.0)


def test_current_column_is_not_taken_for_resistance() -> None:
    # The uA column comes first; only the kOhm column may be read.
    assert extract_resistances(TABLE_KOHM)[16] != pytest.approx(11.56)


def test_table_without_a_unit_gives_nothing() -> None:
    text = "| Channel | Value |\n|---|---|\n| 1 | 1003 |\n"
    assert extract_resistances(text) == {}


def test_inline_value_with_unit_still_wins() -> None:
    text = "Channel 1: 1.00 kOhm\n\n" + TABLE_OHM
    assert extract_resistances(text)[1] == pytest.approx(1000.0)


def test_score_passes_on_a_header_unit_table() -> None:
    result = score_answer(
        TABLE_OHM, {"resistance_ohm": {1: 1000, 16: 8660}, "tolerance_pct": 5}
    )
    assert result is not None
    assert result["score"] == 1.0

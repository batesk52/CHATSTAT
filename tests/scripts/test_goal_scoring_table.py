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


def test_table_outranks_a_range_sentence() -> None:
    # Real answer shape: the sentence names channel 1 next to channel 16's
    # value. The table, not the sentence, gives channel 1.
    text = TABLE_OHM + (
        "- Pattern: the resistances rise smoothly from about 1.0 kohm on "
        "channel 1 to about 8.6 kohm on channel 16.\n"
    )
    found = extract_resistances(text)
    assert found[1] == pytest.approx(1003.0)
    assert found[16] == pytest.approx(8648.0)


def test_approximate_marks_in_cells_are_accepted() -> None:
    # Real answer shape: every value written as "~1004".
    text = (
        "| Ch | Wiring (RE/CE) | Current (uA) | R (ohm) |\n"
        "|----|----------------|--------------|---------|\n"
        "| 1  | manual (1)     | 99.62 | ~1004 |\n"
        "| 16 | on_board (16)  | 11.56 | ~8648 |\n"
    )
    found = extract_resistances(text)
    assert found[1] == pytest.approx(1004.0)
    assert found[16] == pytest.approx(8648.0)


def test_rows_with_a_unit_in_each_cell_outrank_the_opening_sentence() -> None:
    # Real answer shape: the header has no unit, each cell does, and the
    # first sentence names channel 1 beside channel 16's value.
    text = (
        "All 16 channels are working. The resistances run from about 1.0 kOhm "
        "on channel 1 to about 8.65 kOhm on channel 16.\n\n"
        "| Ch | Wiring (RE/CE position) | Current | Resistance |\n"
        "|---|---|---|---|\n"
        "| 1 | manual (1) | 199.08 uA | 1005 ohm |\n"
        "| 16 | on_board (16) | 23.12 uA | 8650 ohm |\n"
    )
    found = extract_resistances(text)
    assert found[1] == pytest.approx(1005.0)
    assert found[16] == pytest.approx(8650.0)


def test_inline_values_fill_channels_the_table_lacks() -> None:
    text = TABLE_OHM + "Channel 3: 1.33 kOhm\n"
    assert extract_resistances(text)[3] == pytest.approx(1330.0)


def test_score_passes_on_a_header_unit_table() -> None:
    result = score_answer(
        TABLE_OHM, {"resistance_ohm": {1: 1000, 16: 8660}, "tolerance_pct": 5}
    )
    assert result is not None
    assert result["score"] == 1.0

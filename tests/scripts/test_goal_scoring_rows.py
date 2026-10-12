"""Scorer reads wide table rows and numbers that carry a note."""

from __future__ import annotations

from scripts.goal_scoring import extract_resistances

WIDE = """All 16 channels pass current. The resistances step up evenly from about
1.0 kOhm on channel 1 to about 8.65 kOhm on channel 16.

| Ch | Wiring (RE/CE position) | Range | Settled current | Resistance |
|----|---------------------------|-------|-----------------|------------|
| 1  | manual (RE/CE 1)          | 1m    | 199.08 uA       | ~1005 ohm  |
| 2  | manual (RE/CE 2)          | 1m    | 173.24 uA       | ~1154 ohm  |
| 15 | external (RE/CE 15)       | 100u  | 26.62 uA        | ~7512 ohm  |
| 16 | on_board (RE/CE 16)       | 100u  | 23.11 uA        | ~8653 ohm  |
"""

NOTED = """| Channel | RE/CE wiring | Mean current (uA) | Resistance (ohm) |
|---|---|---|---|
| 14 | manual (own RE/CE) | 15.43 | 6482 |
| 15 | external (RE/CE at 15) | 13.33 | 7502 (see caveat) |
| 16 | on_board (RE/CE at 16) | 11.56 | 8647 |
"""


def test_wide_rows_outrank_the_range_sentence() -> None:
    assert extract_resistances(WIDE) == {
        1: 1005.0,
        2: 1154.0,
        15: 7512.0,
        16: 8653.0,
    }


def test_current_cells_are_not_taken_for_resistance() -> None:
    assert extract_resistances(WIDE)[1] != 199.08


def test_number_with_a_trailing_note() -> None:
    assert extract_resistances(NOTED) == {14: 6482.0, 15: 7502.0, 16: 8647.0}

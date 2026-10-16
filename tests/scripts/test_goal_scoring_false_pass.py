"""The scorer must not pass an answer that gives no value, or a wrong one.

A goal about one channel reads the first resistance in the answer when no
channel is named next to it. These cases are the ways that reading could
pass a wrong answer: the target value quoted as a question, a bound or a
nominal figure; number forms read in part; a nominal column in a table.
"""

from __future__ import annotations

import pytest

from scripts.goal_scoring import extract_resistances, score_answer

ONE = {"resistance_ohm": {1: 1000}}
S16 = {"resistance_ohm": {16: 8660}}


@pytest.mark.parametrize("text", [
    # No measured value; the target appears as a question, a bound or a nominal.
    "I could not measure a resistance: the 2 uA range overloaded. "
    "Is the resistor the nominal 1 kOhm?",
    "All points overloaded on the 500 uA range, so the resistance is below "
    "about 1 kOhm. I have no value.",
    # Wrong value, nominal value quoted first.
    "The cell's nominal value is 1 kOhm, but I measured 2 uA at 0.5 V, so "
    "the resistance is 250 kOhm.",
    # Real answer shape (no value, stops to ask).
    "All 50 points still overloaded, so the current is above 100 uA and the "
    "resistance is below about 5 kOhm. Should I re-run at the 1 mA range? "
    "It would read down to about 500 Ohm at 0.5 V.",
    # Number forms.
    "Channel 1: 51,000 ohm",
    "Channel 1: 5.00e1000 ohm",
    "Channel 1: -1000 ohm",
    # Tables with a nominal column, either side.
    "| Ch | Measured | Nominal |\n|---|---|---|\n| 1 | 1200 ohm | 1000 ohm |",
    "| Channel | Nominal (ohm) | Measured (ohm) |\n|---|---|---|\n"
    "| 1 | 1000 | 1200 |",
])
def test_wrong_or_empty_answer_does_not_pass(text: str) -> None:
    assert score_answer(text, ONE)["passed"] == 0


def test_uncertainty_is_not_the_value() -> None:
    assert score_answer("Channel 16: 12.4 +/- 8.66 kOhm", S16)["passed"] == 0
    assert extract_resistances("Channel 16: 8.66 +/- 0.02 kOhm") == {16: 8660.0}


def test_number_forms_are_read_whole() -> None:
    assert extract_resistances("Channel 16: 8,660 ohm") == {16: 8660.0}
    assert extract_resistances("Channel 1: 1.00e3 ohm") == {1: 1000.0}


def test_milliohm_does_not_raise() -> None:
    text = "Contact adds about 20 mohm. Channel 1: 1.00 kOhm."
    assert score_answer(text, ONE)["passed"] == 1


def test_bound_before_the_result_does_not_hide_it() -> None:
    text = ("The 2 uA range overloaded, so R is below 250 kOhm. "
            "On 1 mA the current was 498 uA: 1004 ohm.")
    assert score_answer(text, ONE)["passed"] == 1


def test_real_headline_answer_still_passes() -> None:
    text = ("I could not get a valid reading on the 2 uA range you asked for. "
            "The valid result came from the 1 mA range, and it gives about "
            "1.00 kOhm. The 2 uA range can only measure resistances above "
            "about 250 kOhm at 0.5 V.")
    assert score_answer(text, ONE)["passed"] == 1


@pytest.mark.xfail(reason="known gap: the hedge word comes after the value", strict=True)
def test_known_gap_hedge_after_the_value() -> None:
    text = "A 1 kOhm limit applies on channel 1. I measured nothing."
    assert score_answer(text, ONE)["passed"] == 0

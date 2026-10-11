"""Scorer reads a value written before its channel, and single-channel answers."""

from __future__ import annotations

from scripts.goal_scoring import extract_resistances, score_answer

REAL = (
    "I measured about 8.66 kOhm on channel 16 using the 6u current range.\n\n"
    "**Wiring:** I ran a 3 s check at 0.1 V in the default \"external\" mode "
    "and got only about 4 nA, so the channel read as open."
)


def test_value_before_channel() -> None:
    assert extract_resistances(REAL) == {16: 8660.0}
    result = score_answer(REAL, {"resistance_ohm": {16: 8660}})
    assert result["score"] == 1.0


def test_channel_first_still_wins() -> None:
    text = "Channel 1: 1004 ohm. That is 1.0 kOhm on channel 1 roughly."
    assert extract_resistances(text)[1] == 1004.0


def test_single_channel_goal_without_channel_name() -> None:
    result = score_answer("The slope gives R = 2.74 kOhm.", {"resistance_ohm": {8: 2740}})
    assert result["score"] == 1.0


def test_no_fallback_for_many_channels() -> None:
    result = score_answer("R = 1.0 kOhm.", {"resistance_ohm": {1: 1000, 16: 8660}})
    assert result["score"] == 0.0

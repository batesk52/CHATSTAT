"""The current-range value is pasted verbatim into MethodSCRIPT lines.

A free-text ``cr`` reaching the generator could carry a line break and
so inject a second script line (found by the 2026-10-08 manuscript
audit). ``TechniqueConfig`` is the single construction point for every
run, so it refuses anything that is not a current-range token, and
``send_script`` refuses any list item that contains a line break.
"""
from __future__ import annotations

import pytest

from src.comms.serial_connection import PicoConnection
from src.data.models import TechniqueConfig


@pytest.mark.parametrize("cr", ["100n", "2u", "63u", "100u", "1m", "5m", "10m"])
def test_valid_cr_tokens_accepted(cr: str) -> None:
    cfg = TechniqueConfig(technique="ca", params={"cr": cr}, channels=[1])
    assert cfg.params["cr"] == cr


@pytest.mark.parametrize(
    "cr",
    [
        "100u\nset_gpio 0x3FFi",  # injected second line
        "100u\r\ncell_off",
        "100 u",  # whitespace
        "2.5u",  # decimal mantissa is a device syntax error anyway
        "auto",
        "",
        100,  # not a string
    ],
)
def test_malformed_cr_rejected(cr) -> None:
    with pytest.raises(ValueError, match="cr must be"):
        TechniqueConfig(technique="cv", params={"cr": cr}, channels=[1])


def test_missing_cr_is_fine() -> None:
    cfg = TechniqueConfig(technique="cv", params={}, channels=[1])
    assert "cr" not in cfg.params


def test_send_script_refuses_embedded_line_break() -> None:
    conn = PicoConnection()
    conn._ensure_connected = lambda: None  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="Line break inside script line"):
        conn.send_script(["e", "set_cr 100u\nset_gpio 0x3FFi", "cell_on"])

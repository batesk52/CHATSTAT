"""Tests for PacketParser, especially NaN/overload handling."""

from __future__ import annotations

import math

import pytest

from src.comms.protocol import PacketParser, ParsedPacket


@pytest.fixture
def parser() -> PacketParser:
    return PacketParser()


def test_normal_eis_packet_decodes_three_variables(parser: PacketParser) -> None:
    """Sanity check: a real CH01 packet from the field decodes cleanly."""
    line = "PdcAFAF080m;cc80509BEm,10,288;cd4F0E6CCu,10"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    values = result.values
    assert values["set_frequency"] == pytest.approx(50000.0)
    assert values["zreal"] == pytest.approx(330.174, rel=1e-4)
    assert values["zimag"] == pytest.approx(-51.32114, rel=1e-4)


def test_nan_straddling_hex_and_prefix_does_not_crash(
    parser: PacketParser,
) -> None:
    """NaN sentinel where 'nan' straddles hex_str (5sp + 'na') + prefix ('n').

    This is the exact format that crashed the engine on CH3 with
    espico1601 firmware: ``int('     na', 16)`` raised ValueError and
    propagated up, killing the measurement.
    """
    var = "cc     nan,10,288"
    line = f"P{var}"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    assert len(result.variables) == 1
    var_out = result.variables[0]
    assert var_out.name == "zreal"
    assert math.isnan(var_out.value)


def test_nan_with_padded_hex_only(parser: PacketParser) -> None:
    """NaN where 'nan' fits inside hex_str slot with whitespace padding."""
    line = "Pcb    nan m"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    assert math.isnan(result.variables[0].value)


def test_unparseable_hex_falls_back_to_nan(parser: PacketParser) -> None:
    """Defense in depth: any garbage in the hex slot becomes NaN, not a crash."""
    line = "Pcc!!!!!!!u"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    assert math.isnan(result.variables[0].value)


def test_mixed_packet_one_nan_one_valid(parser: PacketParser) -> None:
    """A NaN in one variable does not contaminate the others in the same packet."""
    line = "PdcAFAF080m;cc     nan,10,288;cd4F0E6CCu,10"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    values = result.values
    assert values["set_frequency"] == pytest.approx(50000.0)
    assert math.isnan(values["zreal"])
    assert values["zimag"] == pytest.approx(-51.32114, rel=1e-4)


def test_has_overload_true_when_status_bit_set(parser: PacketParser) -> None:
    """Status field ',12' decodes to 0x2 (STATUS_OVERLOAD) -> has_overload."""
    line = "Pcc80509BEm,12,288"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    assert result.variables[0].status == 0x2
    assert result.has_overload is True


def test_has_overload_false_for_ok_status(parser: PacketParser) -> None:
    """A normal field (',10' -> status 0x0) is not an overload."""
    line = "Pcc80509BEm,10,288"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    assert result.has_overload is False


def test_has_overload_excludes_overload_warning(parser: PacketParser) -> None:
    """The soft overload-WARNING bit (0x8) is not a hard overload."""
    line = "Pcc80509BEm,18,288"

    result = parser.parse_line(line)

    assert isinstance(result, ParsedPacket)
    assert result.variables[0].status == 0x8
    assert result.has_overload is False

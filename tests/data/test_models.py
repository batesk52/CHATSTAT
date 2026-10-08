"""Tests for TechniqueConfig electrode-config-mode validation.

Three wiring modes are supported:

* ``external`` — external RE/CE wired into MUX position 15; WE may
  occupy any of CH1–CH16.
* ``on_board`` — on-board RE/CE on MUX position 16; WE may occupy
  any of CH1–CH16.
* ``manual``   — operator chooses both WE and RE/CE positions, both
  constrained to CH1–CH14 (CH15+CH16 reserved as infrastructure).

These tests pin the defaulting behaviour, channel-range validation,
and the actionable error messages required when CH15/CH16 leak into
Mode C.
"""

from __future__ import annotations

import pytest

from src.data.models import (
    EXTERNAL_RE_CE_CHANNEL,
    MODE_C_MAX_CHANNEL,
    ON_BOARD_RE_CE_CHANNEL,
    DataPoint,
    TechniqueConfig,
)


def test_external_mode_defaults_re_ce_to_15() -> None:
    """External mode populates re_ce_channels with position 15 per step."""
    cfg = TechniqueConfig(
        technique="cv",
        params={},
        channels=[1, 2, 3],
        electrode_config_mode="external",
    )
    assert cfg.re_ce_channels == [
        EXTERNAL_RE_CE_CHANNEL,
        EXTERNAL_RE_CE_CHANNEL,
        EXTERNAL_RE_CE_CHANNEL,
    ]


def test_on_board_mode_defaults_re_ce_to_16() -> None:
    """On-board mode populates re_ce_channels with position 16 per step."""
    cfg = TechniqueConfig(
        technique="cv",
        params={},
        channels=[1, 2, 3],
        electrode_config_mode="on_board",
    )
    assert cfg.re_ce_channels == [
        ON_BOARD_RE_CE_CHANNEL,
        ON_BOARD_RE_CE_CHANNEL,
        ON_BOARD_RE_CE_CHANNEL,
    ]


def test_external_mode_accepts_we_1_through_16() -> None:
    """External mode allows WE positions across the full 1–16 range."""
    cfg = TechniqueConfig(
        technique="cv",
        params={},
        channels=[15, 16],
        electrode_config_mode="external",
    )
    assert cfg.re_ce_channels == [
        EXTERNAL_RE_CE_CHANNEL,
        EXTERNAL_RE_CE_CHANNEL,
    ]


def test_on_board_mode_accepts_we_1_through_16() -> None:
    """On-board mode also allows WE across the full 1–16 range."""
    cfg = TechniqueConfig(
        technique="cv",
        params={},
        channels=[15, 16],
        electrode_config_mode="on_board",
    )
    assert cfg.re_ce_channels == [
        ON_BOARD_RE_CE_CHANNEL,
        ON_BOARD_RE_CE_CHANNEL,
    ]


def test_manual_mode_requires_explicit_re_ce() -> None:
    """Mode C requires the caller to supply re_ce_channels explicitly."""
    with pytest.raises(ValueError) as excinfo:
        TechniqueConfig(
            technique="cv",
            params={},
            channels=[1, 2],
            electrode_config_mode="manual",
        )
    msg = str(excinfo.value)
    assert "manual" in msg.lower(), (
        f"Error message must mention 'manual' mode; got: {msg!r}"
    )


def test_manual_we_15_or_16_rejected() -> None:
    """Mode C must reject WE on CH15 or CH16 (infrastructure-reserved)."""
    with pytest.raises(ValueError) as excinfo_15:
        TechniqueConfig(
            technique="cv",
            params={},
            channels=[15],
            electrode_config_mode="manual",
            re_ce_channels=[1],
        )
    msg_15 = str(excinfo_15.value)
    assert "15" in msg_15, f"Error must name offending channel 15: {msg_15!r}"
    assert (
        f"1-{MODE_C_MAX_CHANNEL}" in msg_15
        or "infrastructure" in msg_15.lower()
    ), f"Error must name the 1-{MODE_C_MAX_CHANNEL} rule: {msg_15!r}"

    with pytest.raises(ValueError) as excinfo_16:
        TechniqueConfig(
            technique="cv",
            params={},
            channels=[16],
            electrode_config_mode="manual",
            re_ce_channels=[1],
        )
    msg_16 = str(excinfo_16.value)
    assert "16" in msg_16, f"Error must name offending channel 16: {msg_16!r}"


def test_manual_re_ce_15_or_16_rejected() -> None:
    """Mode C must reject RE/CE on CH15 or CH16 (infrastructure-reserved)."""
    with pytest.raises(ValueError) as excinfo_15:
        TechniqueConfig(
            technique="cv",
            params={},
            channels=[1],
            electrode_config_mode="manual",
            re_ce_channels=[15],
        )
    msg_15 = str(excinfo_15.value)
    assert "15" in msg_15, (
        f"Error must name offending RE/CE 15: {msg_15!r}"
    )
    assert (
        f"1-{MODE_C_MAX_CHANNEL}" in msg_15
        or "infrastructure" in msg_15.lower()
    ), f"Error must name the 1-{MODE_C_MAX_CHANNEL} rule: {msg_15!r}"

    with pytest.raises(ValueError) as excinfo_16:
        TechniqueConfig(
            technique="cv",
            params={},
            channels=[1],
            electrode_config_mode="manual",
            re_ce_channels=[16],
        )
    msg_16 = str(excinfo_16.value)
    assert "16" in msg_16, (
        f"Error must name offending RE/CE 16: {msg_16!r}"
    )


def test_datapoint_overload_defaults_false() -> None:
    """A DataPoint built the legacy way (no overload kwarg) is not overloaded."""
    dp = DataPoint(timestamp=0.0, channel=1, variables={"zreal": 1.0})
    assert dp.overload is False


def test_datapoint_overload_settable() -> None:
    """The engine can mark a point overloaded via the new field."""
    dp = DataPoint(
        timestamp=0.0, channel=1, variables={"zreal": 1.0}, overload=True
    )
    assert dp.overload is True

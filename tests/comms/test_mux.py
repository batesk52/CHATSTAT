"""Tests for the MUX16 RE/CE addressing extension.

The MUX16 GPIO address is a 10-bit value with WE in bits[3:0] and
RE/CE in bits[7:4]. Prior to the electrode-config work, RE/CE was
hardcoded to position 1 (bits[7:4] == 0). These tests pin the
extended behaviour:

* ``channel_address`` accepts an optional ``re_ce_channel`` kwarg.
* ``scan_channels_script_with_body`` keeps the compact ``loop i <= e``
  pattern when RE/CE is constant across all steps, and falls back to
  the sequential per-channel script when RE/CE varies.
* Per-step GPIO addresses encode the right WE + RE/CE bits in
  varying-RE/CE (Mode C) layouts.
"""

from __future__ import annotations

import pytest

from src.comms.mux import MuxController, MuxError


@pytest.fixture
def mux() -> MuxController:
    """Return a fresh MuxController for each test."""
    return MuxController()


def test_channel_address_re_ce_default(mux: MuxController) -> None:
    """Default RE/CE (position 1) preserves the historical encoding."""
    assert mux.channel_address(1) == 0x000
    assert mux.channel_address(2) == 0x001


def test_channel_address_re_ce_external(mux: MuxController) -> None:
    """External-mode RE/CE on position 15 → bits[7:4] = 14 = 0xE."""
    assert mux.channel_address(1, re_ce_channel=15) == 0x0E0
    assert mux.channel_address(5, re_ce_channel=15) == 0x0E4


def test_channel_address_re_ce_on_board(mux: MuxController) -> None:
    """On-board RE/CE on position 16 → bits[7:4] = 15 = 0xF."""
    assert mux.channel_address(1, re_ce_channel=16) == 0x0F0
    assert mux.channel_address(14, re_ce_channel=16) == 0x0FD


def test_channel_address_validation(mux: MuxController) -> None:
    """RE/CE outside 1–16 must raise MuxError (mux.py is wiring-agnostic)."""
    with pytest.raises(MuxError):
        mux.channel_address(1, re_ce_channel=0)
    with pytest.raises(MuxError):
        mux.channel_address(1, re_ce_channel=17)


def test_scan_consecutive_uses_compact_loop_when_re_ce_constant(
    mux: MuxController,
) -> None:
    """Consecutive WE + constant RE/CE → compact ``loop i <= e``."""
    lines = mux.scan_channels_script_with_body(
        channels=[1, 2, 3, 4],
        body_lines=["meas_x"],
        re_ce_channels=[15, 15, 15, 15],
    )
    assert any("loop i <= e" in line for line in lines), (
        f"Expected compact loop pattern; got:\n" + "\n".join(lines)
    )


def test_scan_varying_re_ce_uses_sequential(
    mux: MuxController,
) -> None:
    """Varying RE/CE between steps forces the sequential script."""
    lines = mux.scan_channels_script_with_body(
        channels=[1, 3],
        body_lines=["meas_x"],
        re_ce_channels=[13, 1],
    )
    assert not any("loop i <= e" in line for line in lines), (
        "Varying RE/CE must NOT use compact loop; got:\n"
        + "\n".join(lines)
    )
    # Sequential layout emits per-step set_gpio commands.
    set_gpio_lines = [line for line in lines if "set_gpio 0x" in line]
    assert len(set_gpio_lines) == 2, (
        f"Sequential mode should emit one set_gpio per WE step; "
        f"got: {set_gpio_lines}"
    )


def test_scan_emits_correct_gpio_per_step_in_manual(
    mux: MuxController,
) -> None:
    """Per-step GPIO addresses must encode WE + RE/CE bits correctly.

    For channels=[1,3], re_ce_channels=[13,1]:
        Step 1: WE=1 (bits[3:0]=0), RE/CE=13 (bits[7:4]=12 = 0xC)
                → address = (12 << 4) | 0 = 0xC0
        Step 2: WE=3 (bits[3:0]=2), RE/CE=1  (bits[7:4]=0  = 0x0)
                → address = (0  << 4) | 2 = 0x02
    """
    lines = mux.scan_channels_script_with_body(
        channels=[1, 3],
        body_lines=["meas_x"],
        re_ce_channels=[13, 1],
    )
    script = "\n".join(lines)
    assert "set_gpio 0x0C0i" in script, (
        f"Missing expected step-1 GPIO 0x0C0i in:\n{script}"
    )
    assert "set_gpio 0x002i" in script, (
        f"Missing expected step-2 GPIO 0x002i in:\n{script}"
    )

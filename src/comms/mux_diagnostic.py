"""MUX16 bit-3 sweep diagnostic.

Runs a single-point CA on each of the 16 MUX channels, printing the
GPIO address, the state of bit 3 of the WE select line, and the
measured current. Intended to isolate hardware/fixture faults from
any round-robin, `meas_loop_ca`, or engine-layer behaviour.

Example output (textbook hardware fault)::

    CH01  addr=0x000  bit3=0  current=0.000000e+00 A
    CH02  addr=0x001  bit3=0  current=0.000000e+00 A
    ...
    CH08  addr=0x007  bit3=0  current=0.000000e+00 A
    CH09  addr=0x008  bit3=1  current=4.647980e-06 A
    ...
    CH16  addr=0x00F  bit3=1  current=4.763917e-06 A

Usage::

    python -m src.comms.mux_diagnostic --port COM5 [--e-dc 0.7] [--cr 2u]
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from src.comms.mux import MuxController
from src.comms.protocol import LoopMarker, PacketParser, ParsedPacket
from src.comms.serial_connection import (
    MEASUREMENT_TIMEOUT,
    PicoConnection,
    PicoConnectionError,
)

logger = logging.getLogger(__name__)


def build_diagnostic_script(
    e_dc: float = 0.7,
    cr: str = "2u",
    settle_ms: int = 500,
    sample_ms: int = 100,
) -> list[str]:
    """Build a 16-channel single-point CA diagnostic script.

    Args:
        e_dc: DC potential in volts.
        cr: Current range token (e.g. "2u", "100u").
        settle_ms: Settle time after each GPIO switch (milliseconds).
        sample_ms: Sample window for the single CA data point (milliseconds).

    Returns:
        List of MethodSCRIPT lines ready for ``send_script``.
    """
    mux = MuxController()
    lines: list[str] = []
    lines.append("var p")
    lines.append("var c")
    lines.append("set_pgstat_chan 1")
    lines.append("set_pgstat_mode 0")
    lines.append("set_pgstat_chan 0")
    lines.append("set_pgstat_mode 2")
    lines.append("set_max_bandwidth 400")
    lines.append("set_pot_range -1 1")
    lines.append(f"set_cr {cr}")
    lines.append(f"set_autoranging 100n {cr}")
    lines.append("cell_on")
    lines.extend(mux.gpio_config_script())
    for ch in range(1, 17):
        addr = mux.channel_address(ch)
        lines.append(f"set_gpio 0x{addr:03X}i")
        lines.append(f"wait {settle_ms}m")
        lines.append(
            f"meas_loop_ca p c {e_dc * 1000:.0f}m {sample_ms}m {sample_ms}m"
        )
        lines.append("    pck_start")
        lines.append("    pck_add p")
        lines.append("    pck_add c")
        lines.append("    pck_end")
        lines.append("endloop")
    lines.append("on_finished:")
    lines.append("  cell_off")
    return lines


def run_diagnostic(
    port: str,
    e_dc: float = 0.7,
    cr: str = "2u",
) -> list[Optional[float]]:
    """Connect, run the diagnostic, and return per-channel current.

    Args:
        port: Serial port (e.g. ``COM5`` or ``/dev/ttyACM0``).
        e_dc: DC potential in volts.
        cr: Current range token.

    Returns:
        List of 16 current values (A); entries are ``None`` for any
        channel that did not produce a packet.
    """
    currents: list[Optional[float]] = [None] * 16
    conn = PicoConnection(port)
    conn.connect()
    try:
        script = build_diagnostic_script(e_dc=e_dc, cr=cr)
        parser = PacketParser()
        conn.send_script(script)

        ch_idx = 0
        while ch_idx < 16:
            line = conn.read_response(timeout=MEASUREMENT_TIMEOUT)
            if not line:
                continue
            if line.startswith("!"):
                raise PicoConnectionError(f"Device error: {line}")
            result = parser.parse_line(line)
            if isinstance(result, ParsedPacket):
                current = result.values.get("current")
                if current is not None:
                    currents[ch_idx] = float(current)
            elif result == LoopMarker.END_LOOP:
                ch_idx += 1
            elif result == LoopMarker.END_MEAS:
                break
    finally:
        conn.disconnect()
    return currents


def format_report(currents: list[Optional[float]]) -> str:
    """Format the per-channel diagnostic report."""
    mux = MuxController()
    lines: list[str] = []
    for ch in range(1, 17):
        addr = mux.channel_address(ch)
        bit3 = (addr >> 3) & 1
        cur = currents[ch - 1]
        cur_str = f"{cur:+.6e} A" if cur is not None else "(no packet)"
        lines.append(
            f"CH{ch:02d}  addr=0x{addr:03X}  bit3={bit3}  current={cur_str}"
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep CH1..CH16 with single-point CA measurements to "
            "isolate MUX-hardware faults (e.g. bit-3 select line)."
        )
    )
    parser.add_argument("--port", required=True, help="Serial port, e.g. COM5")
    parser.add_argument(
        "--e-dc", type=float, default=0.7, help="DC potential in V"
    )
    parser.add_argument(
        "--cr", default="2u", help="Current range token, e.g. 2u, 100u, 1m"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        currents = run_diagnostic(args.port, e_dc=args.e_dc, cr=args.cr)
    except PicoConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_report(currents))
    return 0


if __name__ == "__main__":
    sys.exit(main())

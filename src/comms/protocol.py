"""MethodSCRIPT data packet parser for the EmStat Pico.

Decodes hex-encoded data packets from the device's measurement output
stream. Packets follow the format ``P<var1>;<var2>;...\\n`` where each
variable is a 2-char type code + 7-char hex value + 1-char SI prefix.

The 28-bit hex values are decoded as:
    ``(hex_to_uint(value) - 2^27) * 10^(SI_exponent)``

This module also parses measurement loop markers (M, *, L, +) used for
technique and channel tracking in multiplexed measurements.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SI prefix characters mapped to their base-10 exponents.
# The space character (' ') represents unity (10^0).
SI_PREFIXES: dict[str, int] = {
    "a": -18,  # atto
    "f": -15,  # femto
    "p": -12,  # pico
    "n": -9,   # nano
    "u": -6,   # micro
    "m": -3,   # milli
    " ": 0,    # unity
    "k": 3,    # kilo
    "M": 6,    # mega
    "G": 9,    # giga
    "T": 12,   # tera
    "P": 15,   # peta
    "E": 18,   # exa
    "i": 0,    # integer (no scaling)
}

# 2-character variable type codes → human-readable measurement names.
# Derived from MethodSCRIPT V1.6 specification Appendix.
VAR_TYPES: dict[str, str] = {
    "aa": "unknown",
    "ab": "potential",           # WE vs RE (V)
    "ac": "potential_ce",        # CE vs GND (V)
    "ad": "potential_se",        # SE vs GND (V)
    "ae": "potential_re",        # RE vs GND (V)
    "af": "potential_we",        # WE vs GND (V)
    "ag": "potential_we_ce",     # WE vs CE (V)
    "ba": "current",             # WE current (A)
    "ca": "phase",               # Phase (degrees)
    "cb": "impedance",           # |Z| magnitude (ohms)
    "cc": "zreal",               # Z' real (ohms)
    "cd": "zimag",               # Z'' imaginary (ohms)
    "ce": "eis_e_tdd",           # EIS E time-domain (V)
    "cf": "eis_i_tdd",           # EIS I time-domain (A)
    "cg": "eis_sampling_freq",   # EIS sampling frequency (Hz)
    "ch": "eis_e_ac",            # EIS E AC component (Vrms)
    "ci": "eis_e_dc",            # EIS E DC component (V)
    "cj": "eis_i_ac",            # EIS I AC component (Arms)
    "ck": "eis_i_dc",            # EIS I DC component (A)
    "da": "set_potential",       # Applied potential (V)
    "db": "set_current",         # Applied current (A)
    "dc": "set_frequency",       # Applied frequency (Hz)
    "dd": "set_amplitude",       # Applied AC amplitude (Vrms)
    "ea": "channel",             # Channel number
    "eb": "time",                # Time (s)
    "ec": "pin_mask",            # Pin mask
    "ed": "temperature",         # Temperature (C)
    "ee": "count",               # Generic count
    "as": "ain0",                # Analog input 0 (V)
    "at": "ain1",                # Analog input 1 (V)
    "au": "ain2",                # Analog input 2 (V)
    "av": "ain3",                # Analog input 3 (V)
    "aw": "ain4",                # Analog input 4 (V)
    "ax": "ain5",                # Analog input 5 (V)
    "ay": "ain6",                # Analog input 6 (V)
    "az": "ain7",                # Analog input 7 (V)
    "ha": "generic_current_1",   # Generic current 1 (A)
    "hb": "generic_current_2",   # Generic current 2 (A)
    "hc": "generic_current_3",   # Generic current 3 (A)
    "hd": "generic_current_4",   # Generic current 4 (A)
    "ia": "generic_potential_1", # Generic potential 1 (V)
    "ib": "generic_potential_2", # Generic potential 2 (V)
    "ic": "generic_potential_3", # Generic potential 3 (V)
    "id": "generic_potential_4", # Generic potential 4 (V)
    "ja": "misc_generic_1",
    "jb": "misc_generic_2",
    "jc": "misc_generic_3",
    "jd": "misc_generic_4",
}

# Status bit masks (metadata after comma in variable field)
STATUS_OK = 0x0000
STATUS_OVERLOAD = 0x0002
STATUS_UNDERLOAD = 0x0004
STATUS_OVERLOAD_WARNING = 0x0008

# Offset for signed 28-bit decoding
_OFFSET_28BIT = 2**27  # 134217728


class LoopMarker(Enum):
    """Measurement loop markers in the MethodSCRIPT response stream."""

    BEGIN = "M"       # Start of measurement loop
    END_LOOP = "*"    # End of loop iteration (more to come)
    END_MEAS = "+"    # End of measurement
    SUB_BEGIN = "L"   # Start of sub-loop (e.g., inner MUX channel loop)
    SCAN_START = "C"  # Start of scan (nscans > 1)
    SCAN_END = "-"    # End of scan (nscans > 1)


@dataclass
class ParsedVariable:
    """A single decoded variable from a data packet.

    Attributes:
        var_type: 2-character type code (e.g., 'ba').
        name: Human-readable name (e.g., 'current').
        value: Decoded float value.
        status: Optional status/metadata value.
        current_range: Optional current range index.
    """

    var_type: str
    name: str
    value: float
    status: Optional[int] = None
    current_range: Optional[int] = None


@dataclass
class ParsedPacket:
    """A fully decoded data packet from a ``P...`` line.

    Attributes:
        variables: List of decoded variables in packet order.
        values: Convenience dict mapping variable names to float values.
    """

    variables: list[ParsedVariable] = field(default_factory=list)

    @property
    def values(self) -> dict[str, float]:
        """Return a dict of variable name → decoded value."""
        return {v.name: v.value for v in self.variables}

    @property
    def has_overload(self) -> bool:
        """Return True if any variable reported a hard current overload.

        The device sets :data:`STATUS_OVERLOAD` (0x0002) in a variable's
        status metadata when its reading railed the (pinned) current range.
        This is the device's authoritative "the range is too small" signal;
        the engine carries it onto the ``DataPoint`` so the agent layer can
        flag an under-ranged run and re-range. The softer overload-*warning*
        bit (0x0008, "approaching range") is deliberately excluded so the
        flag means an actually-clipped reading, not a near-miss.
        """
        return any(
            v.status is not None and bool(v.status & STATUS_OVERLOAD)
            for v in self.variables
        )


# ---------------------------------------------------------------------------
# PacketParser
# ---------------------------------------------------------------------------


class PacketParser:
    """Stateful parser for MethodSCRIPT response lines.

    Tracks measurement loop state (current technique, channel index)
    across multiple response lines. Each ``P...`` line is decoded into
    a ``ParsedPacket`` containing one or more ``ParsedVariable`` entries.

    Typical usage::

        parser = PacketParser()
        for line in response_lines:
            result = parser.parse_line(line)
            if isinstance(result, ParsedPacket):
                print(result.values)

    Attributes:
        loop_depth: Current nesting depth of measurement loops.
        channel_index: Current MUX channel index (0-based) within a
            multi-channel scan, incremented by sub-loop markers.
    """

    def __init__(self) -> None:
        self.loop_depth: int = 0
        self.channel_index: int = 0

    def reset(self) -> None:
        """Reset parser state for a new measurement."""
        self.loop_depth = 0
        self.channel_index = 0

    # -- Public API ---------------------------------------------------------

    def parse_line(
        self, line: str
    ) -> Optional[ParsedPacket | LoopMarker]:
        """Parse a single response line from the device.

        Args:
            line: A stripped response line (no trailing newline).

        Returns:
            A ``ParsedPacket`` for data lines (starting with 'P'),
            a ``LoopMarker`` for loop-control lines (M, *, +, L),
            or ``None`` for unrecognised/empty lines.
        """
        if not line:
            return None

        first = line[0]

        if first == "P":
            return self.parse_packet(line)
        if first in ("M", "*", "+", "L", "C", "-"):
            return self._handle_loop_marker(first)

        # Unrecognised line (could be echo, error code, etc.)
        logger.debug("Unrecognised response line: %r", line)
        return None

    def parse_packet(self, line: str) -> ParsedPacket:
        """Decode a ``P<var1>;<var2>;...`` data packet line.

        Args:
            line: The full packet line including the leading 'P'.

        Returns:
            A ``ParsedPacket`` with all decoded variables.
        """
        # Strip leading 'P'
        body = line[1:] if line.startswith("P") else line
        packet = ParsedPacket()

        # Variables are separated by ';'. Do NOT use bare .strip() here:
        # the SI prefix can be a literal space character (unity, 10^0),
        # so stripping the trailing space silently truncates the field
        # and corrupts every reading whose value lands on the unity-prefix
        # scale (e.g., set_potential exactly 0 V in CV zero-crossings).
        # Strip only line endings.
        for var_str in body.split(";"):
            var_str = var_str.strip("\r\n")
            if not var_str:
                continue
            parsed = self._parse_variable(var_str)
            if parsed is not None:
                packet.variables.append(parsed)

        return packet

    def decode_value(self, hex_str: str, prefix: str) -> float:
        """Decode a 7-char hex string with SI prefix to a float.

        The 28-bit unsigned hex value is offset-decoded:
            ``(value - 2^27) * 10^(SI_exponent)``

        Args:
            hex_str: 7-character hexadecimal string (e.g., '8000800').
            prefix: Single SI prefix character (e.g., 'u' for micro).

        Returns:
            The decoded float value.

        Raises:
            ValueError: If the prefix is not recognised or hex is invalid.
        """
        if prefix not in SI_PREFIXES:
            raise ValueError(
                f"Unknown SI prefix {prefix!r}. "
                f"Valid prefixes: {list(SI_PREFIXES.keys())}"
            )
        raw = int(hex_str, 16)
        exponent = SI_PREFIXES[prefix]
        return (raw - _OFFSET_28BIT) * (10**exponent)

    @staticmethod
    def parse_var_type(code: str) -> str:
        """Map a 2-character variable type code to a human-readable name.

        Args:
            code: Two-character code (e.g., 'ba', 'da').

        Returns:
            Human-readable name (e.g., 'current', 'set_potential').
            Returns ``'unknown_<code>'`` for unrecognised codes.
        """
        return VAR_TYPES.get(code, f"unknown_{code}")

    # -- Internal -----------------------------------------------------------

    def _parse_variable(self, var_str: str) -> Optional[ParsedVariable]:
        """Parse a single variable field from a packet.

        A variable field has the structure:
            ``<type:2><hex:7><prefix:1>[,<metadata>...]``

        Metadata fields (after commas) may include status bits and
        current range information.
        """
        # Split off metadata (comma-separated fields after main value)
        parts = var_str.split(",")
        main = parts[0]

        if len(main) < 10:
            logger.warning("Variable field too short: %r", var_str)
            return None

        var_type = main[:2]
        hex_str = main[2:9]
        prefix = main[9]

        name = self.parse_var_type(var_type)

        # PalmSens emits "nan" (sometimes whitespace-padded) for overload /
        # out-of-range readings. The literal can land entirely in hex_str or
        # straddle hex_str+prefix depending on padding, so check the combined
        # field. Treat any unparseable hex as NaN rather than crashing — the
        # device's channel loop will advance regardless.
        combined = (hex_str + prefix).strip().lower()
        if combined == "nan" or "nan" in combined:
            value = float("nan")
        else:
            try:
                value = self.decode_value(hex_str, prefix)
            except ValueError as exc:
                logger.warning(
                    "Unparseable variable %r (%s); treating as NaN",
                    var_str,
                    exc,
                )
                value = float("nan")

        # Parse optional metadata fields
        status = None
        current_range = None
        for meta in parts[1:]:
            meta = meta.strip()
            if not meta:
                continue
            if meta.startswith("1"):
                try:
                    status = int(meta[1:], 16)
                except ValueError:
                    pass
            elif meta.startswith("2"):
                try:
                    current_range = int(meta[1:], 16)
                except ValueError:
                    pass

        return ParsedVariable(
            var_type=var_type,
            name=name,
            value=value,
            status=status,
            current_range=current_range,
        )

    def _handle_loop_marker(self, char: str) -> LoopMarker:
        """Process a loop marker character and update parser state."""
        if char == "M":
            self.loop_depth += 1
            logger.debug("Loop begin (depth=%d)", self.loop_depth)
            return LoopMarker.BEGIN
        elif char == "*":
            self.channel_index = 0
            logger.debug("Loop end iteration (depth=%d)", self.loop_depth)
            return LoopMarker.END_LOOP
        elif char == "+":
            self.loop_depth = max(0, self.loop_depth - 1)
            logger.debug("Measurement end (depth=%d)", self.loop_depth)
            return LoopMarker.END_MEAS
        elif char == "L":
            self.channel_index += 1
            logger.debug(
                "Sub-loop begin (channel_index=%d)", self.channel_index
            )
            return LoopMarker.SUB_BEGIN
        elif char == "C":
            logger.debug("Scan start")
            return LoopMarker.SCAN_START
        elif char == "-":
            logger.debug("Scan end")
            return LoopMarker.SCAN_END
        # Should not reach here
        return LoopMarker.BEGIN  # pragma: no cover

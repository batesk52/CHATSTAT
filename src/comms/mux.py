"""MUX16 channel address calculation and GPIO control script generation.

The PalmSens MUX16 multiplexer switches 16 electrode channels via a
10-bit GPIO address. Both the Working Electrode (WE) and the
Reference/Counter Electrode (RE/CE) positions are independently
addressable.

Address bit layout (10 bits)::

    Bits [9:8]  — Enable (inverted: 0 = enabled, 1 = disabled)
    Bits [7:4]  — RE/CE channel select (1-indexed position, encoded
                  as (re_ce_channel - 1); set per-call so callers
                  may share RE/CE on any of the 16 positions)
    Bits [3:0]  — WE channel select (1-indexed, encoded as
                  (channel - 1))

Hardware channels are 1-indexed (CH1–CH16). This module is wiring-
agnostic: it accepts any RE/CE position in 1–16 and leaves the
electrode-config policy (e.g. external/on-board/manual modes that
reserve CH15/CH16) to the GUI/model layer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Number of channels supported by the MUX16 module
MAX_CHANNELS = 16

# GPIO configuration mask: all 10 bits as outputs (0x3FF = 1023)
GPIO_CONFIG_MASK = 0x3FF

# Enable bits mask: bits 9 and 8
_ENABLE_MASK = 0x300  # 0b11_0000_0000

# When enabled, enable bits are 0 (inverted logic)
_ENABLED = 0x000
_DISABLED = _ENABLE_MASK  # 0x300


class MuxError(Exception):
    """Raised for invalid MUX channel operations."""


class MuxController:
    """Manages MUX16 channel addressing and MethodSCRIPT GPIO commands.

    Provides methods to calculate 10-bit GPIO addresses for channels
    1–16 and to generate MethodSCRIPT script fragments for GPIO
    initialisation, channel selection, and multi-channel scanning.

    Both WE (bits[3:0]) and RE/CE (bits[7:4]) are independently
    addressable. The default RE/CE position is 1 to preserve the
    historical ``channel_address(ch)`` behaviour; higher-level callers
    (electrode-config modes) pass an explicit RE/CE position
    (e.g. 15 = external, 16 = on-board).

    Example::

        mux = MuxController()
        addr = mux.channel_address(1)                       # 0x000
        addr = mux.channel_address(16)                      # 0x00F
        addr = mux.channel_address(1, re_ce_channel=15)     # 0x0E0
        script = mux.select_channel_script(5)
    """

    def channel_address(
        self, channel: int, re_ce_channel: int = 1
    ) -> int:
        """Calculate the 10-bit GPIO address for a MUX16 channel.

        Args:
            channel: 1-indexed WE channel number (1–16).
            re_ce_channel: 1-indexed RE/CE channel position (1–16).
                Defaults to 1 for backward compatibility with the
                historical "shared RE/CE on position 1" behaviour.

        Returns:
            10-bit integer address with enable bits cleared (enabled),
            RE/CE at ``(re_ce_channel - 1)`` in bits[7:4], and WE at
            ``(channel - 1)`` in bits[3:0].

        Raises:
            MuxError: If either channel is outside the valid range 1–16.
        """
        self._validate_channel(channel)
        self._validate_channel(re_ce_channel)
        we_idx = channel - 1
        re_ce_idx = re_ce_channel - 1
        # WE = bits[3:0], RE/CE = bits[7:4], enable = bits[9:8] = 0
        address = (re_ce_idx << 4) | we_idx
        return address

    def channel_address_disabled(self) -> int:
        """Return the GPIO address with MUX outputs disabled.

        Enable bits (9:8) set to 1 (disabled in inverted logic).
        Channel bits are zeroed.

        Returns:
            10-bit address with enable bits set (disabled).
        """
        return _DISABLED

    def gpio_config_script(self) -> list[str]:
        """Generate MethodSCRIPT lines to configure GPIO for MUX16.

        Configures all 10 GPIO pins as outputs using
        ``set_gpio_cfg 0x3FFi 1``.

        Returns:
            List of MethodSCRIPT lines (without trailing newlines).
        """
        return [f"set_gpio_cfg 0x{GPIO_CONFIG_MASK:03X}i 1i"]

    def select_channel_script(
        self, channel: int, re_ce_channel: int = 1
    ) -> list[str]:
        """Generate MethodSCRIPT lines to switch to a specific channel.

        Includes a 100 ms settle time after switching to allow the
        MUX hardware to stabilise before measurement begins.

        Args:
            channel: 1-indexed WE channel number (1–16).
            re_ce_channel: 1-indexed RE/CE channel position (1–16).

        Returns:
            List of MethodSCRIPT lines to set the GPIO address and wait.

        Raises:
            MuxError: If either channel is outside the valid range.
        """
        addr = self.channel_address(channel, re_ce_channel=re_ce_channel)
        return [f"set_gpio 0x{addr:03X}i", "wait 100m"]

    def disable_script(self) -> list[str]:
        """Generate MethodSCRIPT lines to disable MUX outputs.

        Returns:
            List of MethodSCRIPT lines to set enable bits high (disabled).
        """
        addr = self.channel_address_disabled()
        return [f"set_gpio 0x{addr:03X}i"]

    def scan_channels_script(
        self,
        channels: list[int],
        re_ce_channels: list[int] | None = None,
    ) -> list[str]:
        """Generate a MethodSCRIPT loop that iterates over channels.

        Uses ``meas_loop_for`` with ``add_var`` to step through the
        selected channels. The loop variable can be used inside the
        measurement to index the current channel.

        For each channel, the script:
        1. Sets the GPIO to the channel address
        2. Provides a placeholder comment for the measurement body

        The caller is responsible for inserting the actual measurement
        commands inside the loop body.

        Args:
            channels: List of 1-indexed WE channel numbers to scan.
            re_ce_channels: Optional list of 1-indexed RE/CE positions
                parallel to ``channels``. If ``None``, RE/CE position
                1 is used for every step (legacy behaviour).

        Returns:
            List of MethodSCRIPT lines for the channel scan loop.

        Raises:
            MuxError: If any channel is outside the valid range.
            ValueError: If the channel list is empty or the
                ``re_ce_channels`` length does not match ``channels``.
        """
        if not channels:
            raise ValueError("Channel list must not be empty.")
        for ch in channels:
            self._validate_channel(ch)

        re_ce_list = self._resolve_re_ce(channels, re_ce_channels)

        lines: list[str] = []

        # Configure GPIO
        lines.extend(self.gpio_config_script())

        # Build a loop that steps through each channel
        n_channels = len(channels)
        lines.append(
            f"meas_loop_for p c {n_channels}i"
        )

        for i, (ch, re_ce) in enumerate(zip(channels, re_ce_list)):
            addr = self.channel_address(ch, re_ce_channel=re_ce)
            if i == 0:
                # First channel — set GPIO at loop start
                lines.append(f"  set_gpio 0x{addr:03X}i")
            else:
                # Subsequent channels — use add_var to step
                lines.append(f"  add_var p 1i 0i")
                lines.append(f"  set_gpio 0x{addr:03X}i")
            lines.append("  wait 100m")

        lines.append("endloop")

        return lines

    def scan_channels_script_with_body(
        self,
        channels: list[int],
        body_lines: list[str],
        re_ce_channels: list[int] | None = None,
    ) -> list[str]:
        """Generate multi-channel measurement script with GPIO switching.

        Uses the compact ``loop i <= e`` pattern for consecutive
        channels when RE/CE is also constant (e.g. CH1–CH8 all on
        RE/CE 15), keeping script size constant regardless of channel
        count. Falls back to sequential repetition for non-consecutive
        channels or varying RE/CE positions (limited to ~5 channels
        by device script memory).

        The compact loop requires ``var i`` and ``var e`` to be declared
        in the script preamble.

        Args:
            channels: 1-indexed WE channel numbers (1–16).
            body_lines: MethodSCRIPT lines to execute per channel.
            re_ce_channels: Optional list of 1-indexed RE/CE positions
                parallel to ``channels``. If ``None`` or all values
                identical, the compact loop pattern is used (when
                channels are also consecutive). When values vary, the
                sequential per-channel script is emitted instead.

        Returns:
            Complete MethodSCRIPT lines for the multi-channel scan.

        Raises:
            MuxError: If any channel is outside the valid range.
            ValueError: If channels or body_lines are empty, or if the
                ``re_ce_channels`` length does not match ``channels``.
        """
        if not channels:
            raise ValueError("Channel list must not be empty.")
        if not body_lines:
            raise ValueError("Measurement body must not be empty.")
        for ch in channels:
            self._validate_channel(ch)

        re_ce_list = self._resolve_re_ce(channels, re_ce_channels)

        re_ce_constant = len(set(re_ce_list)) == 1
        if re_ce_constant and self._is_consecutive(channels):
            return self._compact_loop_script(
                channels, body_lines, re_ce_channel=re_ce_list[0]
            )
        else:
            return self._sequential_script(
                channels, body_lines, re_ce_channels=re_ce_list
            )

    def _resolve_re_ce(
        self,
        channels: list[int],
        re_ce_channels: list[int] | None,
    ) -> list[int]:
        """Normalise the RE/CE list, defaulting to position 1 per step.

        Args:
            channels: List of WE channels (validated by caller).
            re_ce_channels: Optional parallel RE/CE positions or
                ``None`` to default to ``[1] * len(channels)``.

        Returns:
            A list of validated RE/CE positions, one per WE step.

        Raises:
            ValueError: If ``re_ce_channels`` length does not match
                ``channels``.
            MuxError: If any RE/CE value is outside 1–16.
        """
        if re_ce_channels is None:
            return [1] * len(channels)
        if len(re_ce_channels) != len(channels):
            raise ValueError(
                "re_ce_channels length must match channels length "
                f"(got {len(re_ce_channels)} vs {len(channels)})."
            )
        for re_ce in re_ce_channels:
            self._validate_channel(re_ce)
        return list(re_ce_channels)

    def _is_consecutive(self, channels: list[int]) -> bool:
        """Check if channels form a consecutive sequence."""
        return channels == list(
            range(channels[0], channels[0] + len(channels))
        )

    def _compact_loop_script(
        self,
        channels: list[int],
        body_lines: list[str],
        re_ce_channel: int = 1,
    ) -> list[str]:
        """Generate compact loop script for consecutive channels.

        Uses ``loop i <= e`` with ``set_gpio i`` to iterate channels
        in ~30 lines regardless of channel count. Matches the PalmSens
        reference pattern from ca_mux_16chan_low.mscr.

        Args:
            channels: Consecutive 1-indexed WE channels.
            body_lines: MethodSCRIPT measurement lines per step.
            re_ce_channel: Constant RE/CE position for all iterations
                (encoded into bits[7:4] of the start and end addresses).
        """
        start_addr = self.channel_address(
            channels[0], re_ce_channel=re_ce_channel
        )
        end_addr = self.channel_address(
            channels[-1], re_ce_channel=re_ce_channel
        )

        lines: list[str] = []
        lines.extend(self.gpio_config_script())
        lines.append(f"store_var i {start_addr}i aa")
        lines.append(f"store_var e {end_addr}i aa")
        lines.append("loop i <= e")
        lines.append("    set_gpio i")
        lines.append("    wait 100m")
        for body_line in body_lines:
            lines.append(f"    {body_line}")
        lines.append("    add_var i 1i")
        lines.append("endloop")
        return lines

    def _sequential_script(
        self,
        channels: list[int],
        body_lines: list[str],
        re_ce_channels: list[int] | None = None,
    ) -> list[str]:
        """Generate sequential per-channel script.

        Repeats the measurement body for each channel, emitting a
        per-step ``set_gpio`` with the right WE+RE/CE address. Limited
        to ~5 channels by the device's script memory (~60 lines max).

        Args:
            channels: 1-indexed WE channels (any order).
            body_lines: MethodSCRIPT measurement lines per step.
            re_ce_channels: Parallel list of RE/CE positions, or
                ``None`` to default to position 1 for every step.
        """
        re_ce_list = (
            [1] * len(channels)
            if re_ce_channels is None
            else list(re_ce_channels)
        )
        lines: list[str] = []
        lines.extend(self.gpio_config_script())
        for ch, re_ce in zip(channels, re_ce_list):
            addr = self.channel_address(ch, re_ce_channel=re_ce)
            lines.append(f"set_gpio 0x{addr:03X}i")
            lines.append("wait 100m")
            lines.extend(body_lines)
        return lines

    @staticmethod
    def _validate_channel(channel: int) -> None:
        """Raise MuxError if channel is outside [1, 16].

        Args:
            channel: Channel number to validate.

        Raises:
            MuxError: If channel < 1 or channel > 16.
        """
        if not isinstance(channel, int) or channel < 1 or channel > MAX_CHANNELS:
            raise MuxError(
                f"Channel must be an integer between 1 and {MAX_CHANNELS}, "
                f"got {channel!r}."
            )

"""Background measurement thread for the EmStat Pico MUX16.

Orchestrates electrochemical measurements in a QThread, keeping all
serial I/O off the GUI thread.  The engine builds a MethodSCRIPT from
the requested technique, parameters, and MUX channels, sends it to the
device, then reads the streaming response line-by-line, decoding data
packets in real time.  Decoded data points are emitted as Qt signals
for live plotting, and buffered into a MeasurementResult for post-run
export.

Abort, halt, and resume commands are forwarded to the device via the
thread-safe PicoConnection write methods and can be called from any
thread (typically the GUI thread).

Typical usage from the GUI layer::

    engine = MeasurementEngine()
    engine.data_point_ready.connect(plot_widget.on_data_point)
    engine.measurement_finished.connect(on_done)
    engine.start_measurement(connection, config)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from src.comms.protocol import LoopMarker, PacketParser, ParsedPacket
from src.comms.serial_connection import (
    MEASUREMENT_TIMEOUT,
    PicoConnection,
    PicoConnectionError,
)
from src.data.incremental_writer import IncrementalCSVWriter
from src.data.models import DataPoint, MeasurementResult, TechniqueConfig
from src.techniques.scripts import generate

logger = logging.getLogger(__name__)

# Response line prefixes that signal the end of a measurement
_END_MARKERS = frozenset({"*", "+"})

# Device error line prefix (MethodSCRIPT errors start with '!')
_ERROR_PREFIX = "!"

# Empty-line sentinel returned when the device has nothing more to send
_EMPTY_LINE = ""

# Short read timeout used to confirm end-of-measurement once the device
# has stopped sending data. The full MEASUREMENT_TIMEOUT is only needed
# while waiting for the next data packet (slow EIS/CA inter-packet gaps);
# once a read comes back empty the device is almost certainly done, so the
# confirmation reads fail fast instead of each blocking MEASUREMENT_TIMEOUT.
# This is what stops the "save?" prompt from lagging minutes (or never
# firing) when a clean '+' END_MEAS marker isn't received.
_EMPTY_CONFIRM_TIMEOUT = 2.0  # seconds

# EIS/GEIS sweep one frequency per packet, and the device holds the serial
# line silent for the entire duration of each point. That per-point time
# climbs steeply as frequency falls — bench captures show ~1.4 s at 25 Hz,
# ~5 s at 10 Hz, ~22 s at 5 Hz, roughly quadrupling every half-decade, which
# puts a single point near 1 Hz well over 100 s. So the "expecting data"
# read timeout is scaled to the lowest swept frequency (freq_end) instead of
# the flat MEASUREMENT_TIMEOUT; otherwise a slow low-frequency point reads
# empty mid-sweep, gets mistaken for end-of-measurement, and truncates a
# multi-channel run after the first channel's sweep.
_EIS_FAST_BAND_MIN_HZ = 2.0   # at/above this every point is < ~25 s (120 s
                              # is ample) — kept identical to legacy behaviour
_EIS_POINT_S_PER_HZ = 350.0   # silent-wait budget for one point at 1 Hz
                              # (~2x the ~150 s a 1 Hz point actually takes)
_EIS_READ_TIMEOUT_MAX = 900.0  # ceiling so a stalled device still ends in
                               # <=15 min rather than hanging indefinitely


def _eis_data_read_timeout(freq_end: float) -> float:
    """Per-read timeout (seconds) for an EIS/GEIS sweep ending at ``freq_end``.

    A single EIS point keeps the serial line silent for its whole duration,
    which grows sharply at low frequency (~22 s at 5 Hz, >100 s near 1 Hz).
    The read timeout must exceed the slowest point or the engine reads empty
    mid-sweep, mistakes it for the end of the measurement, and stops before
    later channels run.

    At or above :data:`_EIS_FAST_BAND_MIN_HZ` every point finishes well inside
    :data:`MEASUREMENT_TIMEOUT`, so the flat timeout is returned unchanged
    (preserving legacy behaviour for the common high-frequency range). Below
    it, the timeout scales inversely with ``freq_end`` and is clamped to
    ``[MEASUREMENT_TIMEOUT, _EIS_READ_TIMEOUT_MAX]``.

    Args:
        freq_end: Lowest swept frequency in Hz (``params['freq_end']``).

    Returns:
        Read timeout in seconds.
    """
    if freq_end >= _EIS_FAST_BAND_MIN_HZ:
        return MEASUREMENT_TIMEOUT
    # Guard against a zero/negative freq_end from a malformed config so the
    # division can't blow up; clamping below caps the result regardless.
    safe_freq = max(freq_end, 1e-3)
    scaled = _EIS_POINT_S_PER_HZ / safe_freq
    return min(_EIS_READ_TIMEOUT_MAX, max(MEASUREMENT_TIMEOUT, scaled))


class MeasurementEngine(QThread):
    """Background QThread that executes electrochemical measurements.

    Accepts a ``PicoConnection``, a ``TechniqueConfig``, and runs the
    full measurement lifecycle: script generation, transmission, real-
    time response parsing, and result buffering.  Data flows to the GUI
    exclusively through Qt signals.

    Signals:
        data_point_ready(DataPoint): Emitted for every decoded data
            packet.  Connect to a plot widget for live updates.
        measurement_started(str): Emitted once the script is sent to
            the device.  Payload is the technique name.
        measurement_finished(MeasurementResult): Emitted on normal
            completion with the full buffered result.
        measurement_error(str): Emitted when a fatal error occurs
            (serial disconnect, device error code, script error).
        channel_changed(int): Emitted when the MUX switches to a new
            channel (1-indexed).

    Attributes:
        result: The ``MeasurementResult`` being built during the run.
            Only valid after ``measurement_finished`` is emitted.
    """

    # ---- Qt signals (class-level) ----------------------------------------
    data_point_ready = pyqtSignal(object)  # DataPoint
    measurement_started = pyqtSignal(str)  # technique name
    measurement_finished = pyqtSignal(object)  # MeasurementResult
    measurement_error = pyqtSignal(str)  # error message
    channel_changed = pyqtSignal(int)  # 1-indexed channel
    auto_save_completed = pyqtSignal(str)  # output dir path

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._connection: Optional[PicoConnection] = None
        self._config: Optional[TechniqueConfig] = None
        self._abort_requested: bool = False
        self._halted: bool = False
        self.result: Optional[MeasurementResult] = None
        self._writer: Optional[IncrementalCSVWriter] = None
        self._last_flush_index: int = 0

    # ---- Public API (called from GUI thread) -----------------------------

    def start_measurement(
        self,
        connection: PicoConnection,
        config: TechniqueConfig,
    ) -> None:
        """Configure and launch the measurement thread.

        This must be called from the GUI thread.  It stores the
        connection and config, then calls ``QThread.start()`` which
        invokes ``run()`` in the background thread.

        Args:
            connection: An already-connected ``PicoConnection``.
            config: Technique configuration (technique name, parameters,
                and channel list).

        Raises:
            RuntimeError: If the engine is already running.
        """
        if self.isRunning():
            raise RuntimeError(
                "MeasurementEngine is already running. "
                "Abort or wait for completion before starting again."
            )
        self._connection = connection
        self._config = config
        self._abort_requested = False
        self._halted = False
        self.result = None
        self.start()

    def abort(self) -> None:
        """Request measurement abort.

        Sends the 'Z' command to the device via the thread-safe
        ``PicoConnection.abort()`` method.  The run loop will detect
        the abort flag and exit cleanly.

        Safe to call from any thread (typically the GUI thread).
        """
        self._abort_requested = True
        if self._connection is not None and self._connection.is_connected:
            try:
                self._connection.abort()
                logger.info("Abort requested by user.")
            except PicoConnectionError as exc:
                logger.warning("Error sending abort: %s", exc)

    def halt(self) -> None:
        """Pause the running measurement.

        Sends the 'h' command.  The device pauses data output until
        ``resume()`` is called.

        Safe to call from any thread.
        """
        self._halted = True
        if self._connection is not None and self._connection.is_connected:
            try:
                self._connection.halt()
                logger.info("Halt requested by user.")
            except PicoConnectionError as exc:
                logger.warning("Error sending halt: %s", exc)

    def resume(self) -> None:
        """Resume a halted measurement.

        Sends the 'H' command.

        Safe to call from any thread.
        """
        self._halted = False
        if self._connection is not None and self._connection.is_connected:
            try:
                self._connection.resume()
                logger.info("Resume requested by user.")
            except PicoConnectionError as exc:
                logger.warning("Error sending resume: %s", exc)

    # ---- QThread entry point (runs in background thread) -----------------

    def run(self) -> None:
        """Execute the measurement lifecycle.

        This method is invoked by ``QThread.start()`` and runs entirely
        in the background thread.  It should **never** be called
        directly.

        Lifecycle:
            1. Validate inputs
            2. Generate MethodSCRIPT
            3. Send script to device
            4. Read response lines in a loop, parsing data packets
            5. Emit signals for each data point and loop markers
            6. On completion or error, emit the appropriate signal
        """
        try:
            self._run_measurement()
        except PicoConnectionError as exc:
            self._ensure_cell_off()
            msg = f"Serial communication error: {exc}"
            logger.error(msg)
            self.measurement_error.emit(msg)
        except Exception as exc:
            self._ensure_cell_off()
            msg = f"Unexpected error: {exc}"
            logger.error(msg, exc_info=True)
            self.measurement_error.emit(msg)
        except BaseException:
            # Cover control-flow exceptions (KeyboardInterrupt/SystemExit)
            # so the cell is de-energized even on an abnormal thread exit;
            # re-raise — we must not swallow these.
            self._ensure_cell_off()
            raise

    # ---- Internal implementation -----------------------------------------

    def _run_measurement(self) -> None:
        """Core measurement loop (runs in background thread)."""
        connection = self._connection
        config = self._config

        # -- Validate inputs -----------------------------------------------
        if connection is None or not connection.is_connected:
            self.measurement_error.emit(
                "No active connection. Connect to the device first."
            )
            return

        if config is None:
            self.measurement_error.emit(
                "No technique configuration provided."
            )
            return

        if not config.channels:
            self.measurement_error.emit(
                "No channels selected. Select at least one channel."
            )
            return

        # Validate channel numbers (1-16)
        for ch in config.channels:
            if not isinstance(ch, int) or ch < 1 or ch > 16:
                self.measurement_error.emit(
                    f"Invalid channel number: {ch}. "
                    "Channels must be integers between 1 and 16."
                )
                return

        technique = config.technique
        params = config.params
        channels = config.channels
        # config.re_ce_channels is populated by TechniqueConfig.__post_init__
        # from electrode_config_mode; legacy callers without the attribute
        # fall back to None so generate() uses the historical default.
        re_ce_channels = getattr(config, "re_ce_channels", None) or None

        # -- Generate MethodSCRIPT -----------------------------------------
        # generate() may stash technique metadata (e.g. ``_n_rounds`` for
        # ca_alt_mux) into the params dict it receives. Pass a copy so the
        # caller's config.params (shared with the GUI) is never mutated and
        # the metadata never leaks into saved CSV/.pssession params; read
        # the metadata back from the copy below.
        gen_params = dict(params)
        try:
            script_lines = generate(
                technique, gen_params, channels,
                re_ce_channels=re_ce_channels,
            )
        except Exception as exc:
            self.measurement_error.emit(
                f"Script generation failed: {exc}"
            )
            return

        logger.info(
            "Generated %d-line MethodSCRIPT for %s on channels %s.",
            len(script_lines),
            technique,
            channels,
        )

        # -- Initialise result buffer --------------------------------------
        self.result = MeasurementResult(
            technique=technique,
            start_time=datetime.now(),
            device_info={
                "firmware": connection.firmware_version or "",
                "serial": connection.serial_number or "",
            },
            params=dict(params),
            channels=list(channels),
            re_ce_channels=list(re_ce_channels)
            if re_ce_channels is not None
            else [],
            electrode_config_mode=getattr(
                config, "electrode_config_mode", "external"
            ),
        )

        # -- Initialise auto-save writer if enabled -------------------------
        self._writer = None
        self._last_flush_index = 0
        script_save_path: Optional[str] = None
        if (
            config.auto_save is not None
            and config.auto_save.enabled
            and config.auto_save.output_dir
        ):
            self._writer = IncrementalCSVWriter()
            auto_dir = self._writer.start(
                technique=technique,
                params=params,
                device_info=self.result.device_info,
                channels=channels,
                output_dir=config.auto_save.output_dir,
                exact_dir=config.auto_save.exact_dir,
                electrode_config_mode=getattr(
                    config, "electrode_config_mode", ""
                ),
                re_ce_channels=getattr(config, "re_ce_channels", None),
            )
            logger.info("Auto-save enabled: %s", auto_dir)
            script_save_path = os.path.join(auto_dir, "_script.mscr")

        # -- Run measurement (single or continuous) -------------------------
        continuous = config.continuous
        t_run = float(params.get("t_run", 0.0))
        measurement_start_time = time.monotonic()
        parser = PacketParser()
        n_scans = int(params.get("n_scans", 1))
        round_num = 0

        while not self._abort_requested:
            # -- Send script to device ------------------------------------
            # Persist the script only on the first round so we don't
            # overwrite the diagnostic copy during continuous runs.
            try:
                connection.send_script(
                    script_lines,
                    save_to=script_save_path if round_num == 0 else None,
                )
            except (PicoConnectionError, ValueError) as exc:
                self._finish_writer()
                self.measurement_error.emit(
                    f"Failed to send script: {exc}"
                )
                return

            if round_num == 0:
                self.measurement_started.emit(technique)
                logger.info("Measurement started: %s", technique)

            # -- Track channels for this round ----------------------------
            current_channel_idx = 0
            current_channel = channels[current_channel_idx]
            self.channel_changed.emit(current_channel)
            channel_start_time = time.monotonic()
            parser.reset()  # reset loop_depth between rounds
            # Diagnostic: log the first raw packet for each channel so
            # the CH1-8-all-zero pattern can be distinguished between
            # device-side zero vs missing packet vs decode artefact.
            logged_first_packet: set[int] = set()

            scan_counter = 0
            loops_this_round = 0
            # Per-marker DEBUG log shows the device's exact marker
            # sequence — invaluable for script-flow debugging. Bump
            # root logger to DEBUG to capture.
            packets_since_marker = 0
            markers_seen: list[str] = []
            # ca_alt_mux uses a self-looping script: total loops =
            # n_rounds * n_channels, all in a single script run.
            # n_rounds is computed once by generate() and read back from
            # the gen_params copy (keeps config.params unmutated).
            if technique == "ca_alt_mux":
                n_rounds = int(gen_params["_n_rounds"])
                loops_expected = n_rounds * len(channels)
            else:
                loops_expected = n_scans * len(channels)
            # Per-channel-visit averaging buffer. When ca_alt_mux runs
            # with samples_per_visit > 1, the device emits N packets per
            # channel between channel switches; we buffer them and emit a
            # single averaged DataPoint on END_LOOP. Acts as a built-in
            # box-car anti-alias filter for stirrer-modulated noise.
            samples_per_visit = max(
                1, int(params.get("samples_per_visit", 1))
            )
            avg_buffer: list[tuple[float, dict[str, float], bool]] = []
            consecutive_empty = 0
            # Per-read timeout while expecting data. For EIS/GEIS this is
            # scaled to the lowest swept frequency, because the device stays
            # silent for the full (and at low frequency, long) duration of
            # each point; a flat 120 s timeout reads empty mid-sweep on a
            # ~1 Hz point and truncates multi-channel runs after channel 1.
            # Fast-cadence techniques (CA, CV, SWV, …) keep the flat timeout —
            # their packets arrive sub-second.
            if technique in ("eis", "geis"):
                data_read_timeout = _eis_data_read_timeout(
                    float(params.get("freq_end", 0.1))
                )
            else:
                data_read_timeout = MEASUREMENT_TIMEOUT
            read_timeout = data_read_timeout
            # Once the device has stayed silent for a whole data-read timeout
            # (already sized to outlast the slowest expected point), it is
            # almost certainly finished, so confirm end-of-measurement with
            # short reads instead of blocking another full timeout each. This
            # keeps the "save?" prompt snappy when a clean '+' is missed.
            confirm_timeout = _EMPTY_CONFIRM_TIMEOUT

            # -- Read one round of responses ------------------------------
            while not self._abort_requested:
                try:
                    line = connection.read_response(timeout=read_timeout)
                except PicoConnectionError as exc:
                    self._ensure_cell_off()
                    self.measurement_error.emit(
                        f"Serial read error: {exc}"
                    )
                    return

                if line == _EMPTY_LINE:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        # Completed via the idle fallback rather than a
                        # clean '+' END_MEAS marker. Record what the device
                        # actually sent so a dropped/absent terminator can
                        # be confirmed on hardware (firmware variance or RX
                        # buffer loss on long runs are the usual causes).
                        if LoopMarker.END_MEAS.name not in markers_seen:
                            logger.warning(
                                "Ended without '+' END_MEAS marker — used "
                                "idle fallback after %d points. Markers "
                                "seen this round: %s",
                                self.result.num_points,
                                markers_seen[-12:],
                            )
                        break
                    # Device has gone quiet — almost certainly finished but
                    # without a clean '+' marker. Confirm with short reads
                    # instead of blocking the full MEASUREMENT_TIMEOUT each
                    # (no-op for EIS/GEIS, which keep the full timeout).
                    read_timeout = confirm_timeout
                    continue
                # Real data resumed: restore the long (per-technique) timeout
                # for the next (possibly slow) inter-packet gap.
                consecutive_empty = 0
                read_timeout = data_read_timeout

                if line.startswith(_ERROR_PREFIX):
                    error_msg = f"Device error: {line}"
                    logger.error(error_msg)
                    self._ensure_cell_off()
                    self.measurement_error.emit(error_msg)
                    return

                result = parser.parse_line(line)

                if result is None:
                    logger.debug("Skipping unrecognised line: %r", line)
                    continue

                if isinstance(result, ParsedPacket):
                    packets_since_marker += 1
                    # Use global time for continuous or self-looping
                    # techniques (ca_alt_mux); per-channel for others
                    if continuous or technique == "ca_alt_mux":
                        elapsed = time.monotonic() - measurement_start_time
                    else:
                        elapsed = time.monotonic() - channel_start_time
                    if current_channel not in logged_first_packet:
                        logged_first_packet.add(current_channel)
                        logger.debug(
                            "First packet CH%02d raw=%r decoded=%s",
                            current_channel,
                            line,
                            dict(result.values),
                        )
                    values = dict(result.values)
                    # Carry the device's hard-overload flag (current railed
                    # the pinned range) onto the point so the agent summary
                    # can detect an under-ranged run. EIS more often signals
                    # under-ranging via NaN/negative-Z values than this bit,
                    # but when the bit is set it is authoritative.
                    overload = result.has_overload
                    # ca_alt_mux with samples_per_visit > 1: buffer the
                    # N packets per visit and emit a single averaged
                    # DataPoint on END_LOOP. Otherwise emit immediately.
                    if (
                        technique == "ca_alt_mux"
                        and samples_per_visit > 1
                    ):
                        avg_buffer.append((elapsed, values, overload))
                    else:
                        data_point = DataPoint(
                            timestamp=elapsed,
                            channel=current_channel,
                            variables=values,
                            overload=overload,
                        )
                        self.result.add_point(data_point)
                        self.data_point_ready.emit(data_point)

                elif isinstance(result, LoopMarker):
                    # Per-marker DEBUG log shows the device's exact marker
                    # sequence — invaluable for diagnosing multi-channel
                    # script bugs. Bump root logger to DEBUG to capture.
                    marker_name = result.name
                    markers_seen.append(marker_name)
                    logger.debug(
                        "MARKER %-9s (#%d total, +%d packets since last, "
                        "engine thinks CH%02d)",
                        marker_name,
                        len(markers_seen),
                        packets_since_marker,
                        current_channel,
                    )
                    packets_since_marker = 0

                    if result == LoopMarker.SUB_BEGIN:
                        pass  # compact loop marker, ignore

                    elif result == LoopMarker.END_LOOP:
                        # Flush per-visit averaging buffer (ca_alt_mux
                        # with samples_per_visit > 1). One averaged
                        # DataPoint per channel visit.
                        if avg_buffer:
                            ts_mean = sum(
                                t for t, _, _ in avg_buffer
                            ) / len(avg_buffer)
                            keys = avg_buffer[0][1].keys()
                            vmean = {
                                k: sum(v[k] for _, v, _ in avg_buffer)
                                / len(avg_buffer)
                                for k in keys
                            }
                            # Any railed sample in the visit marks the averaged
                            # point overloaded (conservative; the average itself
                            # may sit inside range).
                            any_overload = any(
                                o for _, _, o in avg_buffer
                            )
                            data_point = DataPoint(
                                timestamp=ts_mean,
                                channel=current_channel,
                                variables=vmean,
                                overload=any_overload,
                            )
                            self.result.add_point(data_point)
                            self.data_point_ready.emit(data_point)
                            avg_buffer.clear()
                        scan_counter += 1
                        loops_this_round += 1
                        self._flush_auto_save()
                        # Per-cycle progress log. Skipped for ca_alt_mux
                        # because that technique can run thousands of
                        # rounds × n_channels markers — the log would
                        # drown out everything else.
                        if technique != "ca_alt_mux":
                            logger.info(
                                "Channel %d cycle %d complete",
                                current_channel,
                                scan_counter,
                            )
                        if scan_counter >= n_scans:
                            scan_counter = 0
                            if current_channel_idx + 1 < len(channels):
                                current_channel_idx += 1
                            else:
                                # Wrap to first channel (for self-
                                # looping scripts like ca_alt_mux)
                                current_channel_idx = 0
                            current_channel = channels[
                                current_channel_idx
                            ]
                            if not continuous:
                                channel_start_time = time.monotonic()
                            self.channel_changed.emit(
                                current_channel
                            )
                        # Device emits END_MEAS ('+') after on_finished:
                        # runs — that's the authoritative terminator.
                        # We previously broke on a counted-marker check
                        # (loops_this_round >= loops_expected) but that
                        # is fragile when scripts use device-side loops
                        # whose marker count doesn't match Python-side
                        # n_scans (e.g., wrapped CV with n_scans > 1).
                        # The count below is a 4x-headroom safety net
                        # for the case where '+' never arrives — much
                        # higher than the natural count to avoid early
                        # termination when an extra '*' slips through.
                        if loops_this_round >= loops_expected * 4 + 8:
                            logger.warning(
                                "Safety-net break after %d markers "
                                "(expected %d) — device did not emit "
                                "END_MEAS.",
                                loops_this_round,
                                loops_expected,
                            )
                            break

                    elif result == LoopMarker.END_MEAS:
                        logger.info(
                            "End-of-measurement marker received."
                        )
                        break

                    elif result == LoopMarker.BEGIN:
                        pass

            round_num += 1
            logger.info(
                "Round %d complete: %d points total.",
                round_num,
                self.result.num_points,
            )

            # Single-run mode: exit after one round
            if not continuous:
                break

            # Continuous mode: stop after t_run elapsed
            elapsed = time.monotonic() - measurement_start_time
            if t_run > 0 and elapsed >= t_run:
                logger.info(
                    "t_run reached (%.1fs). Stopping.", t_run
                )
                break

            # Pause between rounds — send_script() handles buffer
            # clearing and device readiness internally
            time.sleep(0.5)

        # -- Cleanup -------------------------------------------------------
        self._flush_auto_save()
        self._finish_writer()

        if self._abort_requested:
            logger.info("Measurement aborted by user.")
            self.measurement_error.emit("Measurement aborted by user.")
        else:
            logger.info(
                "Measurement complete: %d data points collected.",
                self.result.num_points,
            )
            self.measurement_finished.emit(self.result)

    def _ensure_cell_off(self) -> None:
        """Best-effort: stop any running device script after an abnormal
        exit so the cell is not left energized.

        The script's ``on_finished: cell_off`` only runs on *normal*
        completion; on a read error, a device error line, or an unexpected
        exception the script is still executing with the cell driven onto
        the electrode. Sending an abort (``Z``) halts that script. Safe to
        call from the engine thread; swallows errors since we are already
        on a failure path. Not called on normal completion (where
        ``cell_off`` runs) or on user abort (where ``abort()`` already
        sent ``Z``).
        """
        conn = self._connection
        if conn is None or not conn.is_connected:
            return
        try:
            conn.abort()
            logger.info("Sent abort to de-energize cell after error.")
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning(
                "Best-effort cell-off after error failed: %s", exc
            )

    def _flush_auto_save(self) -> None:
        """Flush new data points to the incremental writer."""
        if self._writer is None or self.result is None:
            return
        new_points = self.result.data_points[
            self._last_flush_index :
        ]
        if new_points:
            count = self._writer.flush_points(new_points)
            self._last_flush_index = len(self.result.data_points)
            logger.debug(
                "Auto-saved %d points at loop boundary.", count
            )

    def _finish_writer(self) -> None:
        """Close the incremental writer and emit completion signal."""
        if self._writer is not None and self._writer.is_active:
            paths = self._writer.finish()
            if paths:
                self.auto_save_completed.emit(
                    self._writer.output_dir
                )
            self._writer = None

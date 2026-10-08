"""No-hardware mock engine and connection for the embedded agent.

``MockMeasurementEngine`` is a standalone QObject that mirrors the
public surface of :class:`src.engine.measurement_engine.
MeasurementEngine` -- identical signal set, ``start_measurement(
connection, config)`` / ``abort()`` / ``halt()`` / ``resume()`` /
``isRunning()`` methods, and a ``result`` attribute -- without touching
serial I/O or subclassing the real engine.  On start it validates the
inputs the same way the real engine does, then emits a plausible
synthetic data stream (channel_changed + DataPoints per channel,
followed by measurement_finished with a fully populated
``MeasurementResult``) on QTimer ticks.  Emission therefore requires a
running Qt event loop; bare construction does not.

``MockConnection`` mirrors the small slice of the ``PicoConnection``
surface the engine adapter and engine use (``connect`` / ``disconnect``
/ ``is_connected`` / ``port`` / ``firmware_version`` /
``serial_number`` / ``abort`` / ``halt`` / ``resume``).

Together they let the bridge, adapter, tool layer, and validation gate
exercise the real concurrency path with zero hardware.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime
from typing import Any, Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from src.data.models import DataPoint, MeasurementResult, TechniqueConfig

logger = logging.getLogger(__name__)

# Matches the real engine's user-abort message exactly so downstream
# consumers (GUI, agent tools) can treat mock and real runs uniformly.
ABORT_MESSAGE = "Measurement aborted by user."

# Default synthetic-stream shape. The tick interval is shrunk
# automatically so the whole run stays under _MAX_TOTAL_MS regardless
# of channel count.
_DEFAULT_POINTS_PER_CHANNEL = 12
_DEFAULT_TICK_INTERVAL_MS = 25
_MAX_TOTAL_MS = 1500


def _linspace(start: float, stop: float, num: int) -> list[float]:
    """Return *num* evenly spaced floats from *start* to *stop*."""
    if num <= 1:
        return [float(start)]
    step = (stop - start) / (num - 1)
    return [start + step * i for i in range(num)]


def _logspace(start: float, stop: float, num: int) -> list[float]:
    """Return *num* log-spaced floats from *start* to *stop* (both > 0)."""
    start = max(abs(start), 1e-9)
    stop = max(abs(stop), 1e-9)
    return [
        10.0 ** v
        for v in _linspace(math.log10(start), math.log10(stop), num)
    ]


class MockConnection:
    """Drop-in stand-in for ``PicoConnection`` with no serial I/O.

    Attributes:
        port: The (fake) port name passed to the constructor or
            ``connect()``.
        firmware_version: Plausible firmware string after ``connect()``.
        serial_number: Plausible serial number after ``connect()``.
    """

    def __init__(self, port: Optional[str] = None) -> None:
        """Initialize the mock connection.

        Args:
            port: Optional fake port name (e.g. ``"MOCK1"``).
        """
        self.port: Optional[str] = port
        self.firmware_version: Optional[str] = None
        self.serial_number: Optional[str] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Return True after ``connect()`` and before ``disconnect()``."""
        return self._connected

    def connect(self, port: Optional[str] = None) -> None:
        """Mark the mock device as connected.

        Args:
            port: Optional fake port name; overrides the constructor
                value when given.
        """
        if port is not None:
            self.port = port
        if self.port is None:
            self.port = "MOCK1"
        self.firmware_version = "espico mock 1.4"
        self.serial_number = "MOCK-0000-0001"
        self._connected = True
        logger.info("MockConnection connected on %s.", self.port)

    def disconnect(self) -> None:
        """Mark the mock device as disconnected."""
        self._connected = False
        logger.info("MockConnection disconnected.")

    def abort(self) -> None:
        """No-op (real connection sends 'Z')."""

    def halt(self) -> None:
        """No-op (real connection sends 'h')."""

    def resume(self) -> None:
        """No-op (real connection sends 'H')."""


class MockMeasurementEngine(QObject):
    """Standalone QObject mimicking ``MeasurementEngine``'s surface.

    Emits a short synthetic data stream driven by a QTimer, so a
    running Qt event loop is required for emission (but NOT for bare
    construction).  All emission happens on the thread this object
    lives in -- normally the GUI thread, exactly like the real engine's
    queued signal delivery.

    Signals:
        data_point_ready(object): One ``DataPoint`` per synthetic
            sample.
        measurement_started(str): Technique name, once per run.
        measurement_finished(object): The populated
            ``MeasurementResult`` on normal completion.
        measurement_error(str): Validation failures and user aborts.
        channel_changed(int): 1-indexed channel before its points.
        auto_save_completed(str): Present for surface parity; the mock
            never auto-saves, so it is never emitted.

    Attributes:
        result: The ``MeasurementResult`` being built during the run.
            Only valid after ``measurement_finished`` is emitted.
    """

    data_point_ready = pyqtSignal(object)  # DataPoint
    measurement_started = pyqtSignal(str)  # technique name
    measurement_finished = pyqtSignal(object)  # MeasurementResult
    measurement_error = pyqtSignal(str)  # error message
    channel_changed = pyqtSignal(int)  # 1-indexed channel
    auto_save_completed = pyqtSignal(str)  # output dir path (parity only)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        points_per_channel: int = _DEFAULT_POINTS_PER_CHANNEL,
        tick_interval_ms: int = _DEFAULT_TICK_INTERVAL_MS,
    ) -> None:
        """Initialize the mock engine.

        Args:
            parent: Optional QObject parent.
            points_per_channel: Synthetic DataPoints emitted per
                channel.
            tick_interval_ms: Nominal QTimer tick interval; shrunk
                automatically so total runtime stays under ~1.5 s.
        """
        super().__init__(parent)
        self._points_per_channel = max(1, int(points_per_channel))
        self._tick_interval_ms = max(1, int(tick_interval_ms))
        # Timer is created lazily in start_measurement so that bare
        # construction works in any context; start_measurement runs on
        # this object's (GUI) thread via the bridge, which is also
        # where the timer must live.
        self._timer: Optional[QTimer] = None
        self._events: deque[tuple[str, Any]] = deque()
        self._connection: Any = None
        self._running = False
        self._halted = False
        self._abort_requested = False
        self.result: Optional[MeasurementResult] = None

    # ---- Public API (same contract as the real engine) -------------------

    def isRunning(self) -> bool:  # noqa: N802 - matches QThread.isRunning
        """Return True while a synthetic measurement is in progress."""
        return self._running

    def start_measurement(
        self, connection: Any, config: TechniqueConfig
    ) -> None:
        """Validate inputs and launch the synthetic measurement.

        Mirrors the real engine: invalid inputs are reported through
        ``measurement_error`` (deferred one event-loop turn, like the
        real engine reporting from its worker thread), not raised; only
        the already-running case raises.

        Args:
            connection: A connected ``PicoConnection``-like object
                (``MockConnection`` works).
            config: Technique configuration.

        Raises:
            RuntimeError: If the engine is already running (same
                contract and wording as the real engine).
        """
        if self._running:
            raise RuntimeError(
                "MeasurementEngine is already running. "
                "Abort or wait for completion before starting again."
            )

        error = self._validate(connection, config)
        if error is not None:
            logger.error("Mock start rejected: %s", error)
            # Deferred so the caller's completion slots (connected
            # before start) observe the same post-return ordering as
            # with the real engine.
            QTimer.singleShot(
                0, lambda msg=error: self.measurement_error.emit(msg)
            )
            return

        self._connection = connection
        self._abort_requested = False
        self._halted = False
        self.result = MeasurementResult(
            technique=config.technique,
            start_time=datetime.now(),
            device_info={
                "firmware": connection.firmware_version or "",
                "serial": connection.serial_number or "",
            },
            params=dict(config.params),
            channels=list(config.channels),
            re_ce_channels=list(config.re_ce_channels),
            electrode_config_mode=config.electrode_config_mode,
        )

        self._events = deque()
        for ch in config.channels:
            self._events.append(("channel", ch))
            for point in self._synthesize_channel(config, ch):
                self._events.append(("point", point))

        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._on_tick)

        # Keep total runtime well under ~2 s regardless of channel count.
        interval = min(
            self._tick_interval_ms,
            max(1, _MAX_TOTAL_MS // max(1, len(self._events))),
        )
        self._running = True
        self.measurement_started.emit(config.technique)
        logger.info(
            "Mock measurement started: %s on channels %s (%d events, "
            "%d ms tick).",
            config.technique,
            config.channels,
            len(self._events),
            interval,
        )
        self._timer.start(interval)

    def abort(self) -> None:
        """Request abort; emits ``measurement_error(ABORT_MESSAGE)``.

        Safe to call from any thread: when called off the engine's
        thread it only sets a flag that the next timer tick honours
        (QTimer must be stopped from its own thread).
        """
        self._abort_requested = True
        if not self._running:
            return
        connection = self._connection
        if connection is not None and connection.is_connected:
            connection.abort()  # parity with the real engine ('Z')
        if QThread.currentThread() is self.thread():
            self._finish_aborted()

    def halt(self) -> None:
        """Pause emission (ticks become no-ops until ``resume()``)."""
        self._halted = True
        logger.info("Mock halt requested.")

    def resume(self) -> None:
        """Resume emission after ``halt()``."""
        self._halted = False
        logger.info("Mock resume requested.")

    # ---- Internal implementation ------------------------------------------

    @staticmethod
    def _validate(connection: Any, config: Any) -> Optional[str]:
        """Return an error message for invalid inputs, or None.

        Mirrors the validation (and wording) at the top of the real
        engine's ``_run_measurement``.
        """
        if connection is None or not connection.is_connected:
            return "No active connection. Connect to the device first."
        if config is None:
            return "No technique configuration provided."
        if not config.channels:
            return "No channels selected. Select at least one channel."
        for ch in config.channels:
            if not isinstance(ch, int) or ch < 1 or ch > 16:
                return (
                    f"Invalid channel number: {ch}. "
                    "Channels must be integers between 1 and 16."
                )
        return None

    def _on_tick(self) -> None:
        """Emit the next queued event (runs on this object's thread)."""
        if self._abort_requested:
            self._finish_aborted()
            return
        if self._halted:
            return
        if not self._events:
            if self._timer is not None:
                self._timer.stop()
            result = self.result
            logger.info(
                "Mock measurement complete: %d data points collected.",
                result.num_points if result is not None else 0,
            )
            # Emit while still running -- the real engine's isRunning()
            # is True during emission (the flag only drops when run()
            # returns) -- then clear the flag even if a slot raises.
            try:
                self.measurement_finished.emit(result)
            finally:
                self._running = False
            return

        kind, payload = self._events.popleft()
        if kind == "channel":
            self.channel_changed.emit(payload)
        else:
            assert self.result is not None
            self.result.add_point(payload)
            self.data_point_ready.emit(payload)

    def _finish_aborted(self) -> None:
        """Stop the run and emit the abort error (engine thread only)."""
        if not self._running:
            return
        if self._timer is not None:
            self._timer.stop()
        self._events.clear()
        logger.info("Mock measurement aborted by user.")
        # Same ordering contract as the normal finish path: emit while
        # isRunning() is still True, then clear the flag.
        try:
            self.measurement_error.emit(ABORT_MESSAGE)
        finally:
            self._running = False

    # ---- Synthetic data ----------------------------------------------------

    def _synthesize_channel(
        self, config: TechniqueConfig, channel: int
    ) -> list[DataPoint]:
        """Build a plausible synthetic point list for one channel.

        Args:
            config: The technique configuration (params drive the
                synthetic shapes).
            channel: 1-indexed channel the points belong to.

        Returns:
            List of ``DataPoint`` with technique-appropriate variable
            names matching the real packet decoder's vocabulary.
        """
        technique = config.technique
        params = config.params
        n = self._points_per_channel
        ch_offset = 1.0e-8 * channel  # small per-channel separation

        if technique in ("cv", "fcv"):
            return self._synth_cv(params, channel, n, ch_offset)
        if technique in ("eis", "geis"):
            return self._synth_eis(params, channel, n)
        if technique in ("ca", "fca", "ca_alt_mux"):
            return self._synth_ca(params, channel, n, ch_offset)
        if technique == "cp":
            return self._synth_cp(params, channel, n)
        if technique == "ocp":
            return self._synth_ocp(params, channel, n)
        # Generic potential sweep (lsv, dpv, swv, npv, acv, lsp, pad...)
        return self._synth_sweep(params, channel, n, ch_offset)

    @staticmethod
    def _synth_cv(
        params: dict[str, Any], channel: int, n: int, ch_offset: float
    ) -> list[DataPoint]:
        """Triangular potential sweep with a tanh-shaped wave + hysteresis."""
        e_begin = float(params.get("e_begin", -0.5))
        v1 = float(params.get("e_vertex1", 0.5))
        v2 = float(params.get("e_vertex2", -0.5))
        seg = max(2, n // 3)
        potentials = (
            _linspace(e_begin, v1, seg)
            + _linspace(v1, v2, 2 * seg)[1:]
            + _linspace(v2, e_begin, seg)[1:]
        )
        scan_rate = max(1e-6, float(params.get("scan_rate", 0.1)))
        e_step = float(params.get("e_step", 0.01))
        dt = max(1e-3, abs(e_step) / scan_rate)
        points = []
        half = len(potentials) // 2
        for idx, e in enumerate(potentials):
            hysteresis = 2.0e-7 if idx < half else -2.0e-7
            current = (
                1.0e-6 * e
                + 5.0e-7 * math.tanh(8.0 * e)
                + hysteresis
                + ch_offset
            )
            points.append(
                DataPoint(
                    timestamp=idx * dt,
                    channel=channel,
                    variables={
                        "set_potential": e,
                        "current": current,
                    },
                )
            )
        return points

    @staticmethod
    def _synth_eis(
        params: dict[str, Any], channel: int, n: int
    ) -> list[DataPoint]:
        """Single-RC Randles response over a log frequency sweep."""
        f_start = float(params.get("freq_start", 1.0e5))
        f_end = float(params.get("freq_end", 0.1))
        r_s = 100.0 + 5.0 * channel
        r_ct = 1000.0 + 50.0 * channel
        cap = 1.0e-6
        points = []
        for idx, freq in enumerate(_logspace(f_start, f_end, n)):
            omega = 2.0 * math.pi * freq
            z = r_s + r_ct / (1.0 + 1j * omega * r_ct * cap)
            points.append(
                DataPoint(
                    timestamp=idx * 0.2,
                    channel=channel,
                    variables={
                        "set_frequency": freq,
                        "zreal": z.real,
                        "zimag": z.imag,
                        "impedance": abs(z),
                        "phase": math.degrees(
                            math.atan2(z.imag, z.real)
                        ),
                    },
                )
            )
        return points

    @staticmethod
    def _synth_ca(
        params: dict[str, Any], channel: int, n: int, ch_offset: float
    ) -> list[DataPoint]:
        """Cottrell-like decaying current at fixed potential."""
        e_dc = float(params.get("e_dc", 0.2))
        t_interval = max(1e-3, float(params.get("t_interval", 0.1)))
        points = []
        for idx in range(n):
            t = (idx + 1) * t_interval
            current = 1.0e-6 / math.sqrt(t) + ch_offset
            points.append(
                DataPoint(
                    timestamp=t,
                    channel=channel,
                    variables={
                        "set_potential": e_dc,
                        "current": current,
                    },
                )
            )
        return points

    @staticmethod
    def _synth_cp(
        params: dict[str, Any], channel: int, n: int
    ) -> list[DataPoint]:
        """Slowly drifting potential at fixed applied current."""
        i_dc = float(params.get("i_dc", 1.0e-4))
        t_interval = max(1e-3, float(params.get("t_interval", 0.1)))
        points = []
        for idx in range(n):
            t = (idx + 1) * t_interval
            potential = 0.25 + 0.02 * math.log1p(t) + 1.0e-3 * channel
            points.append(
                DataPoint(
                    timestamp=t,
                    channel=channel,
                    variables={
                        "current": i_dc,
                        "potential": potential,
                    },
                )
            )
        return points

    @staticmethod
    def _synth_ocp(
        params: dict[str, Any], channel: int, n: int
    ) -> list[DataPoint]:
        """Open-circuit potential settling toward an asymptote."""
        t_interval = max(1e-3, float(params.get("t_interval", 1.0)))
        points = []
        for idx in range(n):
            t = (idx + 1) * t_interval
            potential = 0.15 + 0.05 * math.exp(-t) + 1.0e-3 * channel
            points.append(
                DataPoint(
                    timestamp=t,
                    channel=channel,
                    variables={"potential": potential},
                )
            )
        return points

    @staticmethod
    def _synth_sweep(
        params: dict[str, Any], channel: int, n: int, ch_offset: float
    ) -> list[DataPoint]:
        """Generic linear potential sweep (lsv/dpv/swv/... fallback)."""
        e_begin = float(params.get("e_begin", -0.5))
        e_end = float(params.get("e_end", 0.5))
        points = []
        for idx, e in enumerate(_linspace(e_begin, e_end, n)):
            current = (
                1.0e-6 * e + 5.0e-7 * math.tanh(8.0 * e) + ch_offset
            )
            points.append(
                DataPoint(
                    timestamp=idx * 0.05,
                    channel=channel,
                    variables={
                        "set_potential": e,
                        "current": current,
                    },
                )
            )
        return points

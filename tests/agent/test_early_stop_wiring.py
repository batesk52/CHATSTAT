"""The early stop, end to end: adapter, watch, abort and clean-up on the mock engine.

The watch class has its own unit tests. These run ``run_technique`` with the
watch attached, the way the agent does: the engine lives on the Qt thread,
the coroutine on a worker thread with its own asyncio loop.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio  # noqa: E402
import math  # noqa: E402
import threading  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)

# The mock cell is about 105 ohm at 100 kHz. 1 mV on the 5 mA range puts the
# no-current line at 40 ohm, so every point is far below the range. The mock
# data are a clean cell, so the watch reads them as a range that is too large.
OPEN = {"e_ac": 0.001, "cr": "5m"}


class _SerialFaultEngine(MockMeasurementEngine):
    """A real fault arrives right after the watch asks for the abort."""

    def _finish_aborted(self) -> None:
        if not self._running:
            return
        self._timer.stop()
        self._events.clear()
        try:
            self.measurement_error.emit("Serial read error: device disconnected")
        finally:
            self._running = False


class _DeafEngine(MockMeasurementEngine):
    """The abort does not land: the engine stays busy."""

    def abort(self) -> None:
        self._halted = True


class _NoiseEngine(MockMeasurementEngine):
    """An open channel: huge impedance with a phase that jumps about."""

    @staticmethod
    def _synth_eis(params, channel, n):
        points = MockMeasurementEngine._synth_eis(params, channel, n)
        phases = [20.0, 170.0, -95.0, 60.0, -150.0, 110.0, -30.0]
        for k, p in enumerate(points):
            angle = math.radians(phases[k % len(phases)])
            p.variables["zreal"] = 4e8 * math.cos(angle)
            p.variables["zimag"] = 4e8 * math.sin(angle)
            if "impedance" in p.variables:
                p.variables["impedance"] = 4e8
        return points


class _SlowToStopEngine(MockMeasurementEngine):
    """Reports "running" for a moment after it has emitted "finished".

    The real engine is a thread: it emits the signal from inside ``run`` and
    ends just after.
    """

    def __init__(self) -> None:
        super().__init__()
        self._finished_at = None
        self.measurement_finished.connect(self._mark)

    def _mark(self, _result) -> None:
        import time

        self._finished_at = time.monotonic()

    def isRunning(self) -> bool:  # noqa: N802
        import time

        if self._finished_at is not None:
            if time.monotonic() - self._finished_at < 0.3:
                return True
            self._finished_at = None
        return super().isRunning()


class _RailedEngine(MockMeasurementEngine):
    """Every impedance point carries the overload flag."""

    @staticmethod
    def _synth_eis(params, channel, n):
        points = MockMeasurementEngine._synth_eis(params, channel, n)
        for p in points:
            p.overload = True
        return points


class _DroppedDeviceEngine(MockMeasurementEngine):
    """The serial layer's own error for a dropped device says "aborted"."""

    def _finish_aborted(self) -> None:
        if not self._running:
            return
        self._timer.stop()
        self._events.clear()
        try:
            self.measurement_error.emit(
                "Serial read error: ClearCommError failed (The I/O operation "
                "has been aborted because of either a thread exit or an "
                "application request.)"
            )
        finally:
            self._running = False


class _NanEngine(MockMeasurementEngine):
    """Every impedance point is NaN, with no overload flag."""

    @staticmethod
    def _synth_eis(params, channel, n):
        points = MockMeasurementEngine._synth_eis(params, channel, n)
        for p in points:
            for key in ("zreal", "zimag", "impedance"):
                if key in p.variables:
                    p.variables[key] = math.nan
        return points


_APP = None  # keeps the application alive for the whole module


def _adapter(cls=MockMeasurementEngine):
    global _APP
    # A QApplication, not a QCoreApplication: the GUI tests in the same
    # process reuse whichever instance exists and need widgets.
    _APP = QCoreApplication.instance() or QApplication([])
    bridge.install()
    engine = cls()  # a QObject with a QTimer: built on the Qt thread
    connection = MockConnection()
    connection.connect("MOCK1")
    return engine, EngineAdapter(engine, connection)


def _run(*coroutine_factories):
    """Run coroutines in order on a worker thread while Qt spins here."""
    app = QCoreApplication.instance()
    results: list = []

    async def chain():
        for factory in coroutine_factories:
            results.append(await factory())

    def worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(chain())
        finally:
            loop.close()
            QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(app.quit)
    guard.start(30000)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    app.exec()
    thread.join(5.0)
    guard.stop()
    assert not thread.is_alive(), "the adapter call did not return"
    return results


def _receivers(engine) -> int:
    return engine.receivers(engine.data_point_ready)


def test_open_channel_sweep_is_stopped_and_cleaned_up() -> None:
    engine, adapter = _adapter()
    before = _receivers(engine)
    stopped, following = _run(
        lambda: adapter.run_technique("eis", dict(OPEN), channels=[1]),
        lambda: adapter.run_technique("eis", {}, channels=[1]),
    )
    assert stopped["ok"] is False
    assert stopped["stopped_early"] is True
    assert stopped["verdict"] == "range_too_large"
    assert stopped["suggested_cr"] in ("50u", "100u", "200u")
    assert stopped["median_impedance_ohm"] > 40.0
    assert "SMALLER" in stopped["error"]
    assert _receivers(engine) == before  # the watch is detached
    assert not engine.isRunning()
    assert following["ok"] is True  # no stale abort, no stale watch


def test_noise_on_an_open_channel_is_stopped_as_no_current() -> None:
    engine, adapter = _adapter(_NoiseEngine)
    (result,) = _run(
        lambda: adapter.run_technique("eis", {"cr": "1u"}, channels=[1])
    )
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["verdict"] == "no_current"
    assert "suggested_cr" not in result
    assert "wiring" in result["error"].lower()


def test_back_to_back_runs_do_not_find_the_engine_busy() -> None:
    # Seen on the instrument: a run started the instant the last one
    # returned was refused with "Engine is busy".
    engine, adapter = _adapter(_SlowToStopEngine)
    first, second = _run(
        lambda: adapter.run_technique("eis", {}, channels=[1]),
        lambda: adapter.run_technique("eis", {}, channels=[1]),
    )
    assert first["ok"] is True
    assert second["ok"] is True


def test_railed_sweep_is_stopped_with_a_larger_range() -> None:
    engine, adapter = _adapter(_RailedEngine)
    (result,) = _run(
        lambda: adapter.run_technique("eis", {"cr": "1u"}, channels=[1])
    )
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["verdict"] == "underranged"
    assert result["suggested_cr"] == "6u"
    assert "LARGER" in result["error"]


def test_a_dropped_device_is_not_reported_as_an_early_stop() -> None:
    engine, adapter = _adapter(_DroppedDeviceEngine)
    (result,) = _run(lambda: adapter.run_technique("eis", dict(OPEN), channels=[1]))
    assert result["ok"] is False
    assert "stopped_early" not in result
    assert "Serial read error" in result["error"]


def test_a_low_to_high_sweep_is_not_watched() -> None:
    engine, adapter = _adapter()
    params = dict(OPEN, freq_start=10.0, freq_end=100000.0)
    (result,) = _run(lambda: adapter.run_technique("eis", params, channels=[1]))
    assert "stopped_early" not in result


def test_two_channels_are_not_watched() -> None:
    engine, adapter = _adapter()
    (result,) = _run(lambda: adapter.run_technique("eis", dict(OPEN), channels=[1, 2]))
    assert result["ok"] is True
    assert "stopped_early" not in result


def test_autorange_is_not_watched() -> None:
    engine, adapter = _adapter()
    params = dict(OPEN, eis_range_mode="auto")
    (result,) = _run(lambda: adapter.run_technique("eis", params, channels=[1]))
    assert result["ok"] is True
    assert "stopped_early" not in result


def test_a_real_error_after_the_trip_keeps_its_own_text() -> None:
    engine, adapter = _adapter(_SerialFaultEngine)
    (result,) = _run(lambda: adapter.run_technique("eis", dict(OPEN), channels=[1]))
    assert result["ok"] is False
    assert "stopped_early" not in result
    assert "Serial read error" in result["error"]


def test_a_timeout_after_the_trip_is_reported_as_a_timeout() -> None:
    engine, adapter = _adapter(_DeafEngine)
    (result,) = _run(
        lambda: adapter.run_technique("eis", dict(OPEN), channels=[1], timeout=1.0)
    )
    assert result["ok"] is False
    assert "stopped_early" not in result
    assert "timed out" in result["error"].lower()


def test_a_sweep_of_nan_is_not_called_no_current() -> None:
    engine, adapter = _adapter(_NanEngine)
    (result,) = _run(lambda: adapter.run_technique("eis", dict(OPEN), channels=[1]))
    assert "stopped_early" not in result
    assert result.get("verdict") != "no_current"
    quality = (result.get("quality") or {}).get("1", {})
    assert quality.get("verdict") != "no_current"

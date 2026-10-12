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
# no-current line at 40 ohm, so every point counts as "no current".
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
    assert stopped["verdict"] == "no_current"
    assert "wiring" in stopped["error"].lower()
    assert _receivers(engine) == before  # the watch is detached
    assert not engine.isRunning()
    assert following["ok"] is True  # no stale abort, no stale watch


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

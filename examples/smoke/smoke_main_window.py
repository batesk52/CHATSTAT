"""Smoke test: MainWindow + agent dock wiring, offscreen.

Runs with QT_QPA_PLATFORM=offscreen and EMSTAT_AGENT_MOCK=1 (both set
below, before any Qt import), no hardware, no API key, no network.

Constructs the REAL MainWindow and asserts:

* a QDockWidget in the RIGHT dock area exists and its widget is the
  AgentDockPanel;
* the agent stack honours the mock toggle: the agent engine is a
  MockMeasurementEngine and the agent connection a connected
  MockConnection, while the window's real engine/connection are
  untouched;
* the agent registry exposes the built-in measurement tools AND the
  vendored-analysis tools;
* dispatching run_cv through the agent registry (from a worker thread
  with its own asyncio loop, the mandated threading model) drives the
  MOCK engine and the resulting data_point_ready stream reaches the
  MainWindow plot handler (points land in the live plot widget) and
  the channel/status handlers;
* the window closes cleanly: closeEvent shuts the agent worker down
  with no hang.

A hard watchdog force-exits with code 2 after 90 s.  Prints
"SMOKE PASS" and exits 0 on success.

Run from the repo root:
    python examples/smoke/smoke_main_window.py
"""

from __future__ import annotations

# Eager-import native deps at module top, before any asyncio loop is
# created (avoids the Windows DLL-load deadlock).
import numpy  # noqa: F401  - eager native import

import asyncio
import json
import logging
import os
import sys
import threading

# Environment knobs MUST be set before PyQt6 / MainWindow imports.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["EMSTAT_AGENT_MOCK"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)  # prove no key is needed

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6.QtCore import QMetaObject, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDockWidget  # noqa: E402

from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)
from src.agent.tools import dispatch_tool  # noqa: E402
from src.gui.agent_dock import AgentDockPanel  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_main_window")

WATCHDOG_SECONDS = 90.0
CV_INPUT = {
    "channels": [1],
    "e_begin": -0.2,
    "e_vertex1": 0.3,
    "e_vertex2": -0.2,
    "e_step": 0.05,
    "scan_rate": 0.5,
}
ANALYSIS_TOOLS = {
    "load_session", "analyze_cv", "analyze_eis", "analyze_ca",
    "analyze_cp", "analyze_ecsa", "analyze_cic",
}


def _watchdog_fire() -> None:
    """Force-exit: the smoke gate must never hang."""
    print(
        "SMOKE FAIL: watchdog fired after %.0f s" % WATCHDOG_SECONDS,
        flush=True,
    )
    os._exit(2)


def main() -> int:
    """Entry point. Returns the process exit code."""
    failures: list[str] = []

    app = QApplication(sys.argv)
    window = MainWindow()
    # Headless: suppress the modal export prompt that follows a
    # finished measurement (a real user clicks it away).
    window._prompt_export = lambda result: None
    window.show()

    # ---- Right-area dock containing the AgentDockPanel ---------------------
    agent_docks = [
        dock
        for dock in window.findChildren(QDockWidget)
        if isinstance(dock.widget(), AgentDockPanel)
    ]
    if len(agent_docks) != 1:
        failures.append(
            f"expected exactly one AgentDockPanel dock, found "
            f"{len(agent_docks)}"
        )
    else:
        dock = agent_docks[0]
        if dock.windowTitle() != "Agent":
            failures.append(f"dock title wrong: {dock.windowTitle()!r}")
        area = window.dockWidgetArea(dock)
        if area != Qt.DockWidgetArea.RightDockWidgetArea:
            failures.append(f"agent dock not in the right area: {area!r}")

    # ---- Mock toggle: agent stack uses the mocks, real path untouched -------
    if not isinstance(window._agent_engine, MockMeasurementEngine):
        failures.append(
            "agent engine is not MockMeasurementEngine: "
            f"{type(window._agent_engine).__name__}"
        )
    if not isinstance(window._agent_connection, MockConnection):
        failures.append(
            "agent connection is not MockConnection: "
            f"{type(window._agent_connection).__name__}"
        )
    elif not window._agent_connection.is_connected:
        failures.append("mock connection is not connected")
    if window._agent_engine is window._engine:
        failures.append("mock toggle did not swap the agent engine")
    if type(window._engine).__name__ != "MeasurementEngine":
        failures.append(
            f"real engine replaced: {type(window._engine).__name__}"
        )

    # ---- Registry surface: built-ins + analysis tools ------------------------
    names = set(window._agent_registry.names())
    if not ANALYSIS_TOOLS.issubset(names):
        failures.append(
            f"analysis tools missing from registry: "
            f"{sorted(ANALYSIS_TOOLS - names)!r}"
        )
    if "run_cv" not in names or "device_status" not in names:
        failures.append(f"built-in tools missing: {sorted(names)!r}")

    # ---- run_cv through the agent registry drives the MOCK engine ------------
    points_seen: list[int] = []
    window._agent_engine.data_point_ready.connect(
        lambda dp: points_seen.append(dp.channel)
    )
    scenario: dict = {}

    async def _scenario() -> None:
        result_json, is_error = await dispatch_tool(
            window._agent_registry, "run_cv", CV_INPUT
        )
        scenario["result"] = json.loads(result_json)
        scenario["is_error"] = is_error

    def worker() -> None:
        """Agent-style thread: owns its own asyncio loop."""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_scenario())
        except BaseException as exc:  # noqa: BLE001 - report, don't hang
            failures.append(f"scenario raised: {exc!r}")
        finally:
            loop.close()
            QMetaObject.invokeMethod(
                app, "quit", Qt.ConnectionType.QueuedConnection
            )

    thread = threading.Thread(target=worker, name="agent-loop", daemon=True)
    thread.start()
    app.exec()
    thread.join(timeout=10.0)
    app.processEvents()  # drain queued plot/status updates

    result = scenario.get("result", {})
    if scenario.get("is_error") or result.get("ok") is not True:
        failures.append(f"run_cv via agent registry failed: {result!r}")
    elif result.get("num_points", 0) <= 0:
        failures.append(f"run_cv produced no points: {result!r}")
    if not points_seen:
        failures.append("data_point_ready never fired on the mock engine")
    # The SAME signal feeds the plot handler: points must be in the
    # live plot widget's per-channel buffers.
    plot_points = window._plot_container.nyquist._x_data.get(1, [])
    if not plot_points:
        failures.append(
            "mock data_point_ready did not reach the plot handler "
            "(no channel-1 points in the live plot)"
        )
    if window._status_channel.text() != "CH: 1":
        failures.append(
            f"channel_changed did not reach the status bar: "
            f"{window._status_channel.text()!r}"
        )
    if window._last_result is None:
        failures.append(
            "measurement_finished did not reach the MainWindow handler"
        )

    # ---- Clean close (closeEvent -> panel.shutdown, no hang) -----------------
    worker_thread = window._agent_panel.worker
    window.close()
    app.processEvents()
    if worker_thread.isRunning():
        failures.append("agent worker still running after window.close()")
    if window._engine.isRunning() or window._agent_engine.isRunning():
        failures.append("an engine still reports running after close")

    if failures:
        for failure in failures:
            print("SMOKE FAIL:", failure)
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()
    code = main()
    watchdog.cancel()
    sys.exit(code)

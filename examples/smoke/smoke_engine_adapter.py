"""Smoke test: mock CV through the EngineAdapter.

Headless, no hardware, no API key.  Exercises the full mandated
concurrency path: QCoreApplication on the main (GUI) thread, the
bridge invoker installed there, and ONE worker thread running its own
asyncio loop that awaits ``EngineAdapter.run_cv`` against the
MockMeasurementEngine + MockConnection.  Also checks the busy-rejection
path and config validation.

A hard watchdog force-exits with code 2 after 30 s so this script can
never hang.  Prints "SMOKE PASS" and exits 0 on success.

Run from the repo root:
    python examples/smoke/smoke_engine_adapter.py
"""

from __future__ import annotations

# Eager-import native deps at module top, before any asyncio loop is
# created (avoids the Windows DLL-load deadlock).
import numpy  # noqa: F401  - eager native import

import asyncio
import logging
import os
import sys
import threading

# sys.path[0] is examples/smoke/ when run as a script; make the repo
# root importable so "import src...." works when run from the root.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.engine_adapter import (  # noqa: E402
    EngineAdapter,
    build_technique_config,
)
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_engine_adapter")

WATCHDOG_SECONDS = 30.0
CV_PARAMS = {
    "e_begin": -0.2,
    "e_vertex1": 0.3,
    "e_vertex2": -0.3,
    "e_step": 0.05,
    "scan_rate": 0.5,
}
CHANNELS = [1, 2]


def _watchdog_fire() -> None:
    """Force-exit: the smoke gate must never hang."""
    print(
        "SMOKE FAIL: watchdog fired after %.0f s" % WATCHDOG_SECONDS,
        flush=True,
    )
    os._exit(2)


def _check_summary(summary: dict, failures: list[str]) -> None:
    """Assert the compact CV summary looks right."""
    if summary.get("ok") is not True:
        failures.append(f"run_cv returned ok != True: {summary!r}")
        return
    if summary.get("technique") != "cv":
        failures.append(f"technique != 'cv': {summary.get('technique')!r}")
    if not summary.get("num_points", 0) > 0:
        failures.append(f"num_points not > 0: {summary.get('num_points')!r}")
    if summary.get("measured_channels") != CHANNELS:
        failures.append(
            f"measured_channels != {CHANNELS}: "
            f"{summary.get('measured_channels')!r}"
        )
    counts = summary.get("points_per_channel", {})
    for ch in CHANNELS:
        if not counts.get(str(ch), 0) > 0:
            failures.append(f"no points recorded for channel {ch}: {counts!r}")
    if "current" not in summary.get("variables", []):
        failures.append(
            f"'current' missing from variables: {summary.get('variables')!r}"
        )
    if summary.get("params", {}).get("e_vertex1") != CV_PARAMS["e_vertex1"]:
        failures.append(
            f"params echo lost user override: {summary.get('params')!r}"
        )


async def _await_idle(adapter: EngineAdapter, timeout: float = 5.0) -> bool:
    """Poll until the engine reports not-running. True if it settled in time.

    Bounded on purpose: an engine that never goes idle is a REAL failure and
    must still be caught, so this returns False rather than waiting forever.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not adapter.device_status()["engine_running"]:
            return True
        await asyncio.sleep(0.01)
    return False


async def _scenario(adapter: EngineAdapter, failures: list[str]) -> None:
    """Run the smoke scenario inside the agent thread's asyncio loop."""
    # Device status against the mock connection.
    status = adapter.device_status()
    if not (status["ok"] and status["connected"]):
        failures.append(f"device_status not connected: {status!r}")

    # Main path: CV on two channels, busy-rejection probed mid-run.
    task = asyncio.ensure_future(
        adapter.run_cv(CV_PARAMS, channels=CHANNELS)
    )
    await asyncio.sleep(0.15)  # let the mock run start on the GUI thread
    busy = await adapter.run_cv({}, channels=[1])
    busy_error = busy.get("error", "")
    if busy.get("ok") is not False or (
        "busy" not in busy_error and "running" not in busy_error
    ):
        failures.append(f"busy rejection missing/odd: {busy!r}")

    summary = await task
    logger.info("run_cv summary: %s", summary)
    _check_summary(summary, failures)

    # The engine emits measurement_finished from INSIDE its still-running
    # thread, so `await task` can return while isRunning() is briefly still
    # True (the same race SequenceRunner handles by re-polling -- the
    # runner waits for the engine to go idle).  A call issued in that window
    # is refused by the busy guard BEFORE it ever reaches parameter
    # validation, so the next check saw "Engine is busy" instead of the
    # bogus_param error and failed ~35% of runs.  Wait for the engine to
    # actually settle first.
    if not await _await_idle(adapter):
        failures.append("engine never went idle after the run completed")

    # Error path: unknown technique parameter is rejected cleanly.
    bad = await adapter.run_cv({"bogus_param": 1.0}, channels=[1])
    if bad.get("ok") is not False or "bogus_param" not in bad.get("error", ""):
        failures.append(f"unknown-param rejection missing/odd: {bad!r}")


def main() -> int:
    """Entry point. Returns the process exit code."""
    failures: list[str] = []

    # Pure config-builder checks (no Qt needed).
    try:
        build_technique_config("cv", {"bogus_param": 1})
        failures.append("build_technique_config accepted an unknown param")
    except ValueError:
        pass
    try:
        build_technique_config("cv", {"channels": [1], "re_ce_channels": [2],
                                      "electrode_config_mode": "manual"})
    except ValueError as exc:
        failures.append(f"valid manual-mode config rejected: {exc}")

    app = QCoreApplication(sys.argv)
    bridge.install()

    engine = MockMeasurementEngine()
    connection = MockConnection()
    connection.connect("MOCK1")
    adapter = EngineAdapter(engine, connection)

    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()

    def worker() -> None:
        """Agent thread: owns its own asyncio loop (mandated model)."""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_scenario(adapter, failures))
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
    thread.join(timeout=5.0)
    watchdog.cancel()

    if engine.isRunning():
        failures.append("engine still reports running after the scenario")

    if failures:
        for failure in failures:
            print("SMOKE FAIL:", failure)
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

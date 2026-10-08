"""Validation gate for the run -> export -> analyze chain.

Headless, no hardware, no API key, no network.  Builds the
MockMeasurementEngine + MockConnection + EngineAdapter harness (same
pattern as smoke_tools.py), registers the built-in AND vendored-
analysis tools, then -- from a worker thread running its own asyncio
loop -- exercises the full characterization chain the export_session
tool exists for:

1. export_session with no finished run -> structured ok=false.
2. run_cv (closed cycle, channels [1, 2]) through the mock engine.
3. export_session {} -> ok, returns an absolute .pssession path under
   a temp directory (path argument given as a directory).
4. analyze_cv on the exported file -> ok metrics from the vendored
   CVAnalyzer, proving the exporter output round-trips through the
   49.011 loader.
5. load_session on the same file lists a CV technique.

A hard watchdog force-exits with code 2 after 90 s so this script can
never hang.  Prints "SMOKE PASS" and exits 0 on success.

Run from the repo root:
    python examples/smoke/smoke_export_session.py
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
import tempfile
import threading

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)
from src.agent.tools import build_registry, dispatch_tool  # noqa: E402
from src.agent.vendor_analysis import build_analysis_tools  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_export_session")

WATCHDOG_SECONDS = 90.0
CV_INPUT = {
    "channels": [1, 2],
    "e_begin": -0.2,
    "e_vertex1": 0.3,
    "e_vertex2": -0.2,  # closed cycle: must equal e_begin on hardware
    "e_step": 0.05,
    "scan_rate": 0.5,
}


def _watchdog_fire() -> None:
    """Force-exit: the smoke gate must never hang."""
    print(
        "SMOKE FAIL: watchdog fired after %.0f s" % WATCHDOG_SECONDS,
        flush=True,
    )
    os._exit(2)


async def _scenario(
    registry, export_dir: str, failures: list[str]
) -> None:
    """Run the export chain from the agent thread's asyncio loop."""
    # 1. Export before any run -> clean structured error.
    result_json, is_error = await dispatch_tool(
        registry, "export_session", {}
    )
    payload = json.loads(result_json)
    if not is_error or payload.get("ok") is not False or (
        "No finished measurement" not in payload.get("error", "")
    ):
        failures.append(f"export-before-run path wrong: {payload!r}")

    # 2. Mock CV run.
    result_json, is_error = await dispatch_tool(
        registry, "run_cv", CV_INPUT
    )
    summary = json.loads(result_json)
    if is_error or summary.get("ok") is not True or (
        summary.get("num_points", 0) <= 0
    ):
        failures.append(f"run_cv failed: {summary!r}")
        return

    # 3. Export into a temp directory (directory form of 'path').
    result_json, is_error = await dispatch_tool(
        registry, "export_session", {"path": export_dir}
    )
    export = json.loads(result_json)
    if is_error or export.get("ok") is not True:
        failures.append(f"export_session failed: {export!r}")
        return
    path = export.get("path", "")
    if not (
        os.path.isabs(path)
        and path.lower().endswith(".pssession")
        and os.path.isfile(path)
        and os.path.getsize(path) > 0
        and os.path.dirname(path) == os.path.abspath(export_dir)
    ):
        failures.append(f"export path wrong: {export!r}")
        return
    if export.get("technique") != "cv" or export.get(
        "num_points"
    ) != summary.get("num_points"):
        failures.append(f"export summary mismatch: {export!r}")
    logger.info("Exported mock CV to %s", path)

    # 4. The exported file round-trips through the vendored loader
    #    and analyzer (the whole point of the tool).
    result_json, is_error = await dispatch_tool(
        registry, "analyze_cv", {"path": path}
    )
    analysis = json.loads(result_json)
    if is_error or analysis.get("ok") is not True:
        failures.append(f"analyze_cv on export failed: {analysis!r}")
    else:
        logger.info("analyze_cv on exported session: %s", result_json)

    # 5. load_session sees a CV technique in the exported file.
    result_json, is_error = await dispatch_tool(
        registry, "load_session", {"path": path}
    )
    listing = json.loads(result_json)
    if is_error or listing.get("ok") is not True or not any(
        "volt" in str(name).lower() or "cv" in str(name).lower()
        for name in json.dumps(listing).lower().split('"')
    ):
        failures.append(f"load_session listing wrong: {listing!r}")


def main() -> int:
    """Entry point. Returns the process exit code."""
    failures: list[str] = []

    app = QCoreApplication(sys.argv)
    bridge.install()

    engine = MockMeasurementEngine()
    connection = MockConnection()
    connection.connect("MOCK1")
    adapter = EngineAdapter(engine, connection)
    registry = build_registry(
        adapter, extra_tools=build_analysis_tools(figure_sink=None)
    )

    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()

    export_dir = tempfile.mkdtemp(prefix="smoke_export_")

    def worker() -> None:
        """Agent thread: owns its own asyncio loop (mandated model)."""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                _scenario(registry, export_dir, failures)
            )
        except BaseException as exc:  # noqa: BLE001 - report, don't hang
            failures.append(f"scenario raised: {exc!r}")
        finally:
            loop.close()
            QMetaObject.invokeMethod(
                app, "quit", Qt.ConnectionType.QueuedConnection
            )

    thread = threading.Thread(
        target=worker, name="agent-loop", daemon=True
    )
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

"""Smoke test: the whole SWV chain over the mocks.

Headless, no hardware, no API key, no network.  Dispatches the real tool
surface end to end -- ``run_swv`` (two channels) -> ``export_session`` ->
``analyze_swv`` -- and asserts the pieces the SWV tools promise: the
run summary carries the mode-2 auto-range quality block and the
four-output-variable contract (and NOT the CV/CA noise block), the
session exports, and the peak analysis returns an explicit verdict.

A hard watchdog force-exits with code 2 after 30 s so this script can
never hang.  Prints "SMOKE PASS" and exits 0 on success.

Run from the repo root:
    python examples/smoke/smoke_swv_chain.py
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

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)
from src.agent.swv_analysis import build_swv_analysis_tools  # noqa: E402
from src.agent.tools import build_registry, dispatch_tool  # noqa: E402

logging.basicConfig(level=logging.WARNING)

WATCHDOG_SECONDS = 30.0
SWV_INPUT = {
    "channels": [1, 2],
    "e_begin": -0.4,
    "e_end": 0.4,
    "e_step": 0.005,
    "amplitude": 0.025,
    "frequency": 25.0,
}
#: Verdicts analyze_swv is allowed to return; anything else is a bug.
VERDICTS = {"ok", "no_peak_found", "peak_at_edge", "insufficient_data"}


def _watchdog_fire() -> None:
    """Force-exit: the smoke gate must never hang."""
    print(
        "SMOKE FAIL: watchdog fired after %.0f s" % WATCHDOG_SECONDS,
        flush=True,
    )
    os._exit(2)


async def _await_idle(registry, timeout: float = 5.0) -> bool:
    """Poll device_status until the engine reports not-running.

    Bounded on purpose: an engine that never goes idle is a REAL failure
    and must still be caught, so this returns False rather than waiting
    forever.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result_json, _ = await dispatch_tool(registry, "device_status", {})
        if not json.loads(result_json).get("engine_running"):
            return True
        await asyncio.sleep(0.01)
    return False


async def _scenario(registry, out_dir: str, failures: list[str]) -> None:
    """Run SWV, export it, analyze it -- all through dispatch_tool."""
    result_json, is_error = await dispatch_tool(
        registry, "run_swv", SWV_INPUT
    )
    summary = json.loads(result_json)
    if is_error or summary.get("ok") is not True:
        failures.append(f"run_swv failed: {summary!r}")
        return
    if summary.get("technique") != "swv":
        failures.append(f"technique != swv: {summary!r}")
    if summary.get("measured_channels") != [1, 2]:
        failures.append(
            f"measured_channels != [1, 2]: "
            f"{summary.get('measured_channels')!r}"
        )
    # Mode-2 auto-range block (the EIS-parity contract).
    for key in ("quality_ok", "quality", "quality_note",
                "rerange_exhausted"):
        if key not in summary:
            failures.append(f"run_swv summary missing {key!r}")
    if summary.get("quality_ok") is not True:
        failures.append(f"clean mock run not quality_ok: {summary!r}")
    if "suggested_cr" in summary:
        failures.append("clean run should carry no suggested_cr")
    # SWV takes its own summary branch, so the CV/CA ripple block must
    # not also be attached.
    if "noise" in summary or "noise_ok" in summary:
        failures.append("CV/CA noise block leaked onto an SWV summary")
    # Four-output-variable contract.
    block = summary.get("swv_output_vars") or {}
    if set(block.get("roles", {})) != {
        "set_potential", "current", "current_forward", "current_reverse"
    }:
        failures.append(f"swv_output_vars roles wrong: {block!r}")
    if "current" not in block.get("returned", []):
        failures.append(f"net current not reported as returned: {block!r}")
    if block.get("not_returned") != [
        "current_forward", "current_reverse"
    ]:
        failures.append(f"forward/reverse accounting wrong: {block!r}")
    if len(result_json) > 6000 or "data_points" in result_json:
        failures.append("run_swv result leaked raw data arrays")

    if not await _await_idle(registry):
        failures.append("engine never went idle after the SWV run")

    # Export, then analyze the exported session.
    result_json, is_error = await dispatch_tool(
        registry, "export_session", {"path": out_dir}
    )
    export = json.loads(result_json)
    if is_error or export.get("ok") is not True:
        failures.append(f"export_session failed: {export!r}")
        return
    if not str(export.get("path", "")).lower().endswith(".pssession"):
        failures.append(f"export path is not a .pssession: {export!r}")

    result_json, is_error = await dispatch_tool(
        registry, "analyze_swv", {"path": export["path"]}
    )
    analysis = json.loads(result_json)
    if is_error or analysis.get("ok") is not True:
        failures.append(f"analyze_swv failed: {analysis!r}")
        return
    if analysis.get("verdict") not in VERDICTS:
        failures.append(f"unknown verdict: {analysis.get('verdict')!r}")
    # A withheld peak must be withheld, not None-by-accident: whenever the
    # verdict is not 'ok', the peak numbers must be absent.
    if analysis.get("verdict") != "ok" and (
        analysis.get("peak_potential_v") is not None
    ):
        failures.append(
            f"non-ok verdict still reported a peak: {analysis!r}"
        )

    # A bad path must come back as a structured failure, never an
    # exception into the dispatch layer.
    result_json, is_error = await dispatch_tool(
        registry, "analyze_swv", {"path": "no_such_file.pssession"}
    )
    payload = json.loads(result_json)
    if not is_error or payload.get("ok") is not False:
        failures.append(f"analyze_swv bad-path path wrong: {payload!r}")


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
        adapter, extra_tools=build_swv_analysis_tools(figure_sink=None)
    )
    if "run_swv" not in registry or "analyze_swv" not in registry:
        failures.append(f"SWV tools not registered: {registry.names()!r}")

    out_dir = tempfile.mkdtemp(prefix="swv_smoke_")

    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()

    def worker() -> None:
        """Agent thread: owns its own asyncio loop (mandated model)."""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_scenario(registry, out_dir, failures))
        except BaseException as exc:  # noqa: BLE001 - report, don't hang
            failures.append(f"scenario raised: {exc!r}")
        finally:
            loop.close()
            app.quit()

    threading.Thread(target=worker, daemon=True).start()
    app.exec()
    watchdog.cancel()

    if failures:
        for failure in failures:
            print(f"SMOKE FAIL: {failure}", flush=True)
        return 1
    print("SMOKE PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

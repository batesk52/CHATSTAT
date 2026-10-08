"""Smoke test: tool registry + dispatch over the mocks.

Headless, no hardware, no API key, no network.  Builds the
MockMeasurementEngine + MockConnection + EngineAdapter (same harness as
smoke_engine_adapter.py), builds the tool registry, validates the tool
definitions (unique names, object schemas, prescriptive descriptions),
then -- from a worker thread running its own asyncio loop -- dispatches
device_status and run_cv (channels [1, 2]) through dispatch_tool and
asserts ok results.  Also probes the unknown-tool path, the
structured-error path (bad parameter), the handler-exception path, and
the register() seam.

A hard watchdog force-exits with code 2 after 30 s so this script can
never hang.  Prints "SMOKE PASS" and exits 0 on success.

Run from the repo root:
    python examples/smoke/smoke_tools.py
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
from src.agent.tools import (  # noqa: E402
    MEASUREMENT_TECHNIQUES,
    build_registry,
    build_tool_defs,
    dispatch_tool,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_tools")

WATCHDOG_SECONDS = 30.0
# Derived from MEASUREMENT_TECHNIQUES so retiring a technique does not
# leave this gate asserting its own stale expectation -- which is exactly
# what happened when run_geis and run_cp were retired on 2026-07-28.
# The non-run_* half IS hardcoded on purpose: those six are the stable
# device/export surface, and an unplanned change there should fail here.
EXPECTED_TOOLS = {f"run_{t}" for t in MEASUREMENT_TECHNIQUES} | {
    "list_ports", "connect_device", "disconnect_device",
    "device_status", "abort_measurement", "export_session",
}
#: Techniques deliberately removed from the tool surface (2026-07-28).
RETIRED_TOOLS = {"run_geis", "run_cp"}
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


def _check_tool_defs(failures: list[str]) -> None:
    """Validate the static tool definitions."""
    defs = build_tool_defs()
    names = [d.get("name") for d in defs]
    if len(names) != len(set(names)):
        failures.append(f"duplicate tool names: {names!r}")
    if set(names) != EXPECTED_TOOLS:
        failures.append(
            f"tool name set mismatch: {sorted(names)!r} != "
            f"{sorted(EXPECTED_TOOLS)!r}"
        )
    for tool_def in defs:
        name = tool_def.get("name")
        schema = tool_def.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            failures.append(f"{name}: input_schema is not type 'object'")
            continue
        if schema.get("additionalProperties") is not False:
            failures.append(f"{name}: additionalProperties is not false")
        if not isinstance(tool_def.get("description"), str) or len(
            tool_def["description"]
        ) < 40:
            failures.append(f"{name}: description missing or too short")
        for prop, spec in schema.get("properties", {}).items():
            if not spec.get("description"):
                failures.append(f"{name}.{prop}: missing description")
            if not spec.get("type"):
                failures.append(f"{name}.{prop}: missing type")
    # JSON-serializability of the full def list (what goes on the wire).
    try:
        json.dumps(defs)
    except (TypeError, ValueError) as exc:
        failures.append(f"tool defs not JSON-serializable: {exc}")
    # Measurement tools require channels; connect requires port.
    # Derive the expected run_* set from MEASUREMENT_TECHNIQUES rather than
    # hardcoding it: this list held run_cp/run_geis after both were retired
    # on 2026-07-28, so the gate failed on its own stale expectation.
    by_name = {d["name"]: d for d in defs}
    for tech_name in MEASUREMENT_TECHNIQUES:
        tech = f"run_{tech_name}"
        if tech not in by_name:
            failures.append(f"{tech}: in MEASUREMENT_TECHNIQUES but no tool def")
            continue
        if by_name[tech]["input_schema"].get("required") != ["channels"]:
            failures.append(f"{tech}: 'channels' not the required key")
    # Retired techniques must NOT reappear on the surface.
    for retired in RETIRED_TOOLS:
        if retired in by_name:
            failures.append(f"{retired}: retired 2026-07-28 but still defined")
    if by_name["connect_device"]["input_schema"].get("required") != ["port"]:
        failures.append("connect_device: 'port' not the required key")


async def _await_idle(registry, timeout: float = 5.0) -> bool:
    """Poll device_status until the engine reports not-running.

    Bounded on purpose: an engine that never goes idle is a REAL failure and
    must still be caught, so this returns False rather than waiting forever.

    smoke_engine_adapter.py has the same race and the same fix.
    See the call site.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result_json, _ = await dispatch_tool(registry, "device_status", {})
        if not json.loads(result_json).get("engine_running"):
            return True
        await asyncio.sleep(0.01)
    return False


async def _scenario(registry, failures: list[str]) -> None:
    """Dispatch tools from inside the agent thread's asyncio loop."""
    # device_status round-trip.
    result_json, is_error = await dispatch_tool(
        registry, "device_status", {}
    )
    status = json.loads(result_json)
    if is_error or status.get("ok") is not True or not status.get(
        "connected"
    ):
        failures.append(
            f"device_status dispatch wrong: is_error={is_error}, "
            f"{status!r}"
        )

    # run_cv on channels [1, 2] through the full mock path.
    result_json, is_error = await dispatch_tool(registry, "run_cv", CV_INPUT)
    summary = json.loads(result_json)
    if is_error:
        failures.append(f"run_cv dispatch flagged is_error: {summary!r}")
    if summary.get("ok") is not True:
        failures.append(f"run_cv returned ok != True: {summary!r}")
    else:
        if summary.get("technique") != "cv":
            failures.append(f"technique != cv: {summary!r}")
        if not summary.get("num_points", 0) > 0:
            failures.append(f"num_points not > 0: {summary!r}")
        if summary.get("measured_channels") != [1, 2]:
            failures.append(
                f"measured_channels != [1, 2]: "
                f"{summary.get('measured_channels')!r}"
            )
        if summary.get("params", {}).get("scan_rate") != 0.5:
            failures.append(f"params echo lost override: {summary!r}")
    if "data_points" in result_json or len(result_json) > 4000:
        failures.append("run_cv result leaked raw data arrays")

    # Unknown tool -> is_error=True with a helpful message.
    result_json, is_error = await dispatch_tool(registry, "nope", {})
    payload = json.loads(result_json)
    if not is_error or "Unknown tool" not in payload.get("error", ""):
        failures.append(f"unknown-tool path wrong: {payload!r}")
    if "run_cv" not in payload.get("error", ""):
        failures.append("unknown-tool error does not list available tools")

    # measurement_finished fires from inside the engine's still-running
    # thread, so the run_cv dispatch above can return while isRunning() is
    # briefly still True. A call issued in that window is refused by the busy
    # guard BEFORE it reaches parameter validation, so the bogus_param check
    # below intermittently saw "Engine is busy" instead. Wait for idle first
    # (the same fix as in smoke_engine_adapter.py).
    if not await _await_idle(registry):
        failures.append("engine never went idle after the run completed")

    # Structured failure (bad param) -> ok=False AND is_error=True so
    # the agent loop emits tool_call_error and the model sees a failure.
    result_json, is_error = await dispatch_tool(
        registry, "run_cv", {"channels": [1], "bogus_param": 1.0}
    )
    payload = json.loads(result_json)
    if not is_error or payload.get("ok") is not False or (
        "bogus_param" not in payload.get("error", "")
    ):
        failures.append(
            f"structured-error path wrong: is_error={is_error}, "
            f"{payload!r}"
        )

    # Handler exception (registered via the register() seam) -> is_error.
    result_json, is_error = await dispatch_tool(registry, "boom", {})
    payload = json.loads(result_json)
    if not is_error or "RuntimeError" not in payload.get("error", ""):
        failures.append(f"handler-exception path wrong: {payload!r}")

    # abort with nothing running -> structured ok=False, flagged as
    # is_error like every other structured failure.
    result_json, is_error = await dispatch_tool(
        registry, "abort_measurement", {}
    )
    payload = json.loads(result_json)
    if not is_error or payload.get("ok") is not False:
        failures.append(f"abort-idle path wrong: {payload!r}")


def main() -> int:
    """Entry point. Returns the process exit code."""
    failures: list[str] = []

    _check_tool_defs(failures)

    app = QCoreApplication(sys.argv)
    bridge.install()

    engine = MockMeasurementEngine()
    connection = MockConnection()
    connection.connect("MOCK1")
    adapter = EngineAdapter(engine, connection)

    async def _boom(_tool_input):
        raise RuntimeError("intentional smoke failure")

    boom_def = {
        "name": "boom",
        "description": (
            "Intentionally failing smoke-test tool exercising the "
            "handler-exception capture path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    registry = build_registry(adapter, extra_tools=[(boom_def, _boom)])
    if len(registry) != len(EXPECTED_TOOLS) + 1:
        failures.append(f"registry size wrong: {len(registry)}")
    if "run_cv" not in registry or registry.get("device_status") is None:
        failures.append("registry lookups failed for built-in tools")
    try:
        registry.register(boom_def, _boom)
        failures.append("duplicate register() did not raise")
    except ValueError:
        pass

    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()

    def worker() -> None:
        """Agent thread: owns its own asyncio loop (mandated model)."""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_scenario(registry, failures))
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

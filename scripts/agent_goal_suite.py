"""Run a suite of high-level goals through the agent, without the GUI.

Each goal in a YAML file is sent to a FRESH agent session, ``repeats``
times. A session ends when the agent finishes its turn, or is stopped
after ``max_tool_calls`` tool calls or ``max_minutes`` minutes. Every
session writes its own JSON Lines log (the same format as the app's
agent dock), and the suite writes one ``summary.csv`` row per session.

The agent uses the same tools as in the app: device, run and analysis
tools. The preset and sequence tools are left out, because they open
file dialogs. Exports the agent makes without a path land in
``<out>/agent_exports/``.

Examples (from the repository root)::

    python scripts/agent_goal_suite.py examples/goal_suites/resistor_test_cell.yaml --port COM5
    python scripts/agent_goal_suite.py examples/goal_suites/resistor_test_cell.yaml --mock --repeats 1

The API key comes from File > Agent Settings (app settings) or the
``ANTHROPIC_API_KEY`` environment variable. It is never printed or
logged.
"""

from __future__ import annotations

# Native imports first, before any asyncio loop exists (see CLAUDE.md).
import numpy  # noqa: F401
import anthropic  # noqa: F401

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from typing import Any, Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml  # noqa: E402
from PyQt6.QtCore import (  # noqa: E402
    QCoreApplication, QEventLoop, QObject, Qt, QTimer, pyqtSlot,
)

from goal_scoring import score_answer  # noqa: E402
from src import __version__  # noqa: E402
from src.agent import bridge  # noqa: E402
from src.agent.agent_worker import DEFAULT_MODEL, AgentWorker  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.session_log import SessionLog, read_events  # noqa: E402
from src.agent.swv_analysis import build_swv_analysis_tools  # noqa: E402
from src.agent.tools import ToolRegistry, build_registry  # noqa: E402
from src.agent.vendor_analysis import build_analysis_tools  # noqa: E402

SUMMARY_FIELDS = [
    "goal_id", "run", "completed", "stop_reason", "tool_calls",
    "wall_time_s", "score", "score_details", "input_tokens",
    "output_tokens", "final_message", "log_path",
]
#: Seconds to wait for a stopped session to wind down.
_STOP_GRACE_S = 60


class _Monitor(QObject):
    """Counts tool calls and enforces the limits.

    ``on_tool_started`` runs on the agent thread (direct connection);
    the other slots run on the GUI thread. ``stop`` only touches the
    lock-guarded log and the thread-safe ``request_stop``.
    """

    def __init__(self, worker: AgentWorker, log: SessionLog,
                 max_tool_calls: int) -> None:
        super().__init__()
        self._worker = worker
        self._log = log
        self._max = max_tool_calls
        self.calls = 0
        self.stop_reason: Optional[str] = None

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self._log.note("stop_requested", reason=reason)
            self._worker.request_stop()

    @pyqtSlot(object)
    def on_tool_started(self, _payload: object) -> None:
        self.calls += 1
        if self.calls > self._max:
            self.stop("tool_call_limit")

    @pyqtSlot()
    def on_turn_done(self) -> None:
        # One goal is one user turn: end the session when it ends.
        self._worker.request_stop()

    @pyqtSlot()
    def on_time_limit(self) -> None:
        self.stop("time_limit")


def build_stack(mock: bool, port: Optional[str]) -> tuple[Any, Any, Any]:
    """Build engine, connection and adapter (needs a QCoreApplication)."""
    bridge.install()
    if mock:
        from src.agent.mock_engine import MockConnection, MockMeasurementEngine
        engine, connection = MockMeasurementEngine(), MockConnection()
        connection.connect("MOCK1")
    else:
        from src.comms.serial_connection import PicoConnection
        from src.engine.measurement_engine import MeasurementEngine
        engine, connection = MeasurementEngine(), PicoConnection()
        if port:
            connection.connect(port)
    return engine, connection, EngineAdapter(engine, connection)


def build_suite_registry(adapter: EngineAdapter) -> ToolRegistry:
    """Device, run and analysis tools (no figures, no file dialogs)."""
    registry = build_registry(adapter)
    for build in (build_analysis_tools, build_swv_analysis_tools):
        for tool_def, handler in build(figure_sink=lambda _p: None):
            registry.register(tool_def, handler)
    return registry


def _wait_idle(engine: Any, timeout_s: float = 30.0) -> None:
    """Abort a run left behind and spin events until the engine is idle."""
    if engine.isRunning():
        engine.abort()
    deadline = time.monotonic() + timeout_s
    while engine.isRunning() and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.05)


def run_session(
    registry: ToolRegistry, adapter: EngineAdapter, engine: Any, *,
    prompt: str, log_dir: str, mode: str, model: str,
    api_key: Optional[str], max_tool_calls: int, max_minutes: float,
    extra: dict[str, Any],
    client_factory: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Run one goal as one fresh agent session; return its summary row."""
    log = SessionLog(
        log_dir, app_version=__version__, mode=mode,
        port_fn=adapter.connected_port,
        provenance_fn=adapter.script_provenance, extra=extra,
    )
    worker = AgentWorker(registry, api_key=api_key, model=model,
                         session_log=log, client_factory=client_factory)
    monitor = _Monitor(worker, log, max_tool_calls)
    # Direct connection: the slot runs on the agent thread at emit time,
    # so the stop flag is set before the over-limit tool is dispatched.
    worker.tool_call_started.connect(
        monitor.on_tool_started, Qt.ConnectionType.DirectConnection
    )
    worker.agent_turn_done.connect(monitor.on_turn_done)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    limit = QTimer()
    limit.setSingleShot(True)
    limit.timeout.connect(monitor.on_time_limit)
    limit.start(int(max_minutes * 60_000))

    started = time.monotonic()
    worker.start()
    worker.submit_user_message(prompt)
    loop.exec()
    wall = time.monotonic() - started
    limit.stop()
    if not worker.wait(_STOP_GRACE_S * 1000):
        monitor.stop("hung")
    _wait_idle(engine)
    log.end("suite_session_done")  # no-op when the worker already ended it
    return _summarize(log, monitor, wall, extra)


def _summarize(log: SessionLog, monitor: _Monitor, wall: float,
               extra: dict[str, Any]) -> dict[str, Any]:
    events = read_events(log.path) if log.path else []
    replies = [e for e in events if e["event"] == "assistant_message"]
    errors = [e for e in events if e["event"] == "error"]
    final = replies[-1] if replies else {}
    last_stop = final.get("stop_reason")
    completed = (
        monitor.stop_reason is None and not errors and last_stop == "end_turn"
    )
    tokens = {"input_tokens": 0, "output_tokens": 0}
    for reply in replies:
        for key in tokens:
            tokens[key] += int((reply.get("usage") or {}).get(key, 0))
    return {
        "goal_id": extra.get("goal_id"),
        "run": extra.get("run"),
        "completed": "y" if completed else "n",
        "stop_reason": monitor.stop_reason or ("error" if errors else last_stop),
        "tool_calls": sum(1 for e in events if e["event"] == "tool_call"),
        "wall_time_s": round(wall, 1),
        "final_message": final.get("text", ""),
        "log_path": log.path or "",
        **tokens,
    }


def run_suite(
    suite: dict[str, Any], out_dir: str, *, mock: bool, port: Optional[str],
    model: str, api_key: Optional[str], repeats: Optional[int] = None,
    only: Optional[list[str]] = None,
    client_factory: Optional[Callable[..., Any]] = None,
) -> str:
    """Run every goal of *suite*; return the path of ``summary.csv``.

    A ``QCoreApplication`` (or ``QApplication``) must already exist.
    """
    defaults = suite.get("defaults") or {}
    engine, _connection, adapter = build_stack(mock, port)
    registry = build_suite_registry(adapter)
    log_dir = os.path.join(out_dir, "sessions")
    os.makedirs(log_dir, exist_ok=True)
    summary_path = os.path.join(out_dir, "summary.csv")
    with open(summary_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        for goal in suite.get("goals") or []:
            if only and goal["id"] not in only:
                continue
            n = int(repeats or goal.get("repeats") or defaults.get("repeats", 1))
            for run in range(1, n + 1):
                print(f"[{goal['id']}] run {run}/{n} ...", flush=True)
                row = run_session(
                    registry, adapter, engine,
                    prompt=str(goal["prompt"]).strip(), log_dir=log_dir,
                    mode="mock" if mock else "hardware", model=model,
                    api_key=api_key,
                    max_tool_calls=int(goal.get(
                        "max_tool_calls", defaults.get("max_tool_calls", 40))),
                    max_minutes=float(goal.get(
                        "max_minutes", defaults.get("max_minutes", 20))),
                    extra={"suite": suite.get("suite", ""),
                           "goal_id": goal["id"], "run": run},
                    client_factory=client_factory,
                )
                try:
                    scored = score_answer(
                        row["final_message"], goal.get("expected"))
                except Exception as exc:  # noqa: BLE001 - keep the run going
                    # A scorer fault must not end a run on the instrument.
                    scored = None
                    row["score_details"] = f"scorer error: {exc!r}"
                if scored is not None:
                    row["score"] = round(scored["score"], 3)
                    row["score_details"] = "; ".join(scored["details"])
                writer.writerow(row)
                fh.flush()
                print(f"    completed={row['completed']} "
                      f"tool_calls={row['tool_calls']} "
                      f"wall={row['wall_time_s']} s", flush=True)
    return summary_path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("suite", help="YAML file of goals")
    parser.add_argument("--port", help="connect to this port before the suite")
    parser.add_argument("--mock", action="store_true",
                        help="use the mock engine (no instrument)")
    parser.add_argument("--model", help=f"model id (default: app setting "
                        f"or {DEFAULT_MODEL})")
    parser.add_argument("--repeats", type=int, help="override every goal")
    parser.add_argument("--goal", action="append", dest="only",
                        help="run only this goal id (repeatable)")
    parser.add_argument("--out", help="output folder (default: "
                        "agent_goal_runs/<stamp>_<suite>)")
    args = parser.parse_args(argv)

    from src.data.app_settings import get_agent_api_key, get_agent_model
    api_key = get_agent_api_key() or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("No API key: set one in File > Agent Settings or "
              "ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2
    model = args.model or get_agent_model() or DEFAULT_MODEL

    with open(args.suite, encoding="utf-8") as fh:
        suite = yaml.safe_load(fh)
    name = suite.get("suite") or os.path.splitext(os.path.basename(args.suite))[0]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.abspath(
        args.out or os.path.join("agent_goal_runs", f"{stamp}_{name}"))
    os.makedirs(out_dir, exist_ok=True)

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    os.chdir(out_dir)  # default agent exports land in <out>/agent_exports/
    summary = run_suite(suite, out_dir, mock=args.mock, port=args.port,
                        model=model, api_key=api_key, repeats=args.repeats,
                        only=args.only)
    del app
    print(f"Summary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

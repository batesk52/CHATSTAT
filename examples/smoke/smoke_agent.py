"""Smoke test: end-to-end agent loop with a FAKE client.

No network, no API key (ANTHROPIC_API_KEY is explicitly removed from
the environment at the top).  Builds the mock engine + connection +
EngineAdapter + tool registry, installs the bridge on the main (GUI)
thread, then starts an AgentWorker whose client is a FakeAsyncAnthropic
injected through the client_factory seam.

The fake scripts a two-call conversation: call 1 streams text deltas
and ends with stop_reason "tool_use" carrying a run_cv tool_use block
(channels [1, 2]); call 2 -- detected because the request messages now
contain a tool_result block -- streams more text and ends with
"end_turn".  The worker therefore exercises the REAL agentic loop:
streaming, history building, dispatch through the EngineAdapter (the
MOCK ENGINE actually runs the CV), tool_result feedback, and the full
Qt signal surface, collected by a recorder QObject on the GUI thread
via queued connections.

A hard watchdog force-exits with code 2 after 45 s.  Prints
"SMOKE PASS" and exits 0 on success.

Run from the repo root:
    python examples/smoke/smoke_agent.py
"""

from __future__ import annotations

# Eager-import native deps at module top, before any asyncio loop is
# created (avoids the Windows DLL-load deadlock).
import numpy  # noqa: F401  - eager native import

import json
import logging
import os
import sys
import threading
from types import SimpleNamespace

# The agent must run with NO API key: prove it.
os.environ.pop("ANTHROPIC_API_KEY", None)

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt6.QtCore import QCoreApplication, QObject, Qt  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)
from src.agent.tools import build_registry  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_agent")

WATCHDOG_SECONDS = 45.0
TOOL_USE_ID = "toolu_01"
CV_INPUT = {
    "channels": [1, 2],
    "e_begin": -0.2,
    "e_vertex1": 0.3,
    "e_vertex2": -0.2,  # closed cycle: must equal e_begin on hardware
    "e_step": 0.05,
    "scan_rate": 0.5,
}
DELTAS_CALL1 = ["I will ", "run a quick CV ", "on channels 1 and 2."]
DELTAS_CALL2 = ["Done: ", "the CV finished on both channels."]


def _watchdog_fire() -> None:
    """Force-exit: the smoke gate must never hang."""
    print(
        "SMOKE FAIL: watchdog fired after %.0f s" % WATCHDOG_SECONDS,
        flush=True,
    )
    os._exit(2)


# ---------------------------------------------------------------------------
# Fake AsyncAnthropic (shaped like the slice of the SDK the worker reads)
# ---------------------------------------------------------------------------

def _delta_event(text: str) -> SimpleNamespace:
    """One text_delta stream event."""
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _noise_events() -> list[SimpleNamespace]:
    """Non-text events the worker must skip without crashing."""
    return [
        SimpleNamespace(type="message_start"),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="text"),
        ),
    ]


class _FakeStream:
    """Async context manager + async iterator over scripted events."""

    def __init__(self, deltas: list[str], final: SimpleNamespace) -> None:
        self._events = _noise_events() + [_delta_event(t) for t in deltas]
        self._final = final

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            for event in self._events:
                yield event
        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self._final


def _find_tool_result(messages: list) -> dict | None:
    """Return the first tool_result dict in the request messages."""
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return block
    return None


class FakeAsyncAnthropic:
    """Minimal fake of AsyncAnthropic for the manual agentic loop.

    ``messages.stream(**kwargs)`` returns the scripted call-1 stream
    (text + run_cv tool_use) until the request history contains a
    tool_result, after which it returns the call-2 end_turn stream.
    Each call records a snapshot of the request kwargs for the
    post-run assertions.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)
        self._final1 = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="".join(DELTAS_CALL1)),
                SimpleNamespace(
                    type="tool_use",
                    id=TOOL_USE_ID,
                    name="run_cv",
                    input=dict(CV_INPUT),
                ),
            ],
            stop_reason="tool_use",
        )
        self._final2 = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="".join(DELTAS_CALL2)),
            ],
            stop_reason="end_turn",
        )

    def _stream(self, **kwargs) -> _FakeStream:
        messages = kwargs.get("messages", [])
        tool_result = _find_tool_result(messages)
        self.calls.append({
            "model": kwargs.get("model"),
            "max_tokens": kwargs.get("max_tokens"),
            "thinking": kwargs.get("thinking"),
            "system": kwargs.get("system"),
            "tool_names": [
                t.get("name") for t in kwargs.get("tools", [])
            ],
            "forbidden_keys": sorted(
                k for k in (
                    "temperature", "top_p", "top_k", "budget_tokens"
                ) if k in kwargs
            ),
            "n_messages": len(messages),
            "tool_result": dict(tool_result) if tool_result else None,
        })
        if tool_result is not None:
            return _FakeStream(DELTAS_CALL2, self._final2)
        return _FakeStream(DELTAS_CALL1, self._final1)


# ---------------------------------------------------------------------------
# GUI-thread signal recorder
# ---------------------------------------------------------------------------

class Recorder(QObject):
    """Collects AgentWorker signals on the GUI thread."""

    def __init__(self, worker: AgentWorker) -> None:
        super().__init__()
        self._worker = worker
        self.text_deltas: list[str] = []
        self.tool_started: list[dict] = []
        self.tool_finished: list[dict] = []
        self.tool_errors: list[dict] = []
        self.agent_errors: list[str] = []
        self.turns_started = 0
        self.turns_done = 0
        queued = Qt.ConnectionType.QueuedConnection
        worker.agent_text_delta.connect(self.on_text_delta, queued)
        worker.tool_call_started.connect(self.on_tool_started, queued)
        worker.tool_call_finished.connect(self.on_tool_finished, queued)
        worker.tool_call_error.connect(self.on_tool_error, queued)
        worker.agent_error.connect(self.on_agent_error, queued)
        worker.agent_turn_started.connect(self.on_turn_started, queued)
        worker.agent_turn_done.connect(self.on_turn_done, queued)

    def on_text_delta(self, text: str) -> None:
        self.text_deltas.append(text)

    def on_tool_started(self, payload: object) -> None:
        self.tool_started.append(dict(payload))

    def on_tool_finished(self, payload: object) -> None:
        self.tool_finished.append(dict(payload))

    def on_tool_error(self, payload: object) -> None:
        self.tool_errors.append(dict(payload))

    def on_agent_error(self, message: str) -> None:
        self.agent_errors.append(message)

    def on_turn_started(self) -> None:
        self.turns_started += 1

    def on_turn_done(self) -> None:
        self.turns_done += 1
        # Turn complete: shut the worker down cleanly (the assertions
        # run after the app loop exits).
        self._worker.request_stop()


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def _check(recorder: Recorder, fake: FakeAsyncAnthropic,
           engine: MockMeasurementEngine, failures: list[str]) -> None:
    """Assert the full end-to-end behavior."""
    if recorder.agent_errors:
        failures.append(f"agent_error fired: {recorder.agent_errors!r}")
    if recorder.turns_started != 1 or recorder.turns_done != 1:
        failures.append(
            f"turn lifecycle wrong: started={recorder.turns_started}, "
            f"done={recorder.turns_done}"
        )

    # Streamed text deltas from both calls arrived.
    text = "".join(recorder.text_deltas)
    if "".join(DELTAS_CALL1) not in text or (
        "".join(DELTAS_CALL2) not in text
    ):
        failures.append(f"text deltas missing/incomplete: {text!r}")

    # Tool-call signal surface.
    if len(recorder.tool_started) != 1 or len(recorder.tool_finished) != 1:
        failures.append(
            f"tool signals wrong: started={recorder.tool_started!r}, "
            f"finished={recorder.tool_finished!r}"
        )
    else:
        started = recorder.tool_started[0]
        finished = recorder.tool_finished[0]
        if started.get("name") != "run_cv" or (
            started.get("id") != TOOL_USE_ID
        ):
            failures.append(f"tool_call_started payload wrong: {started!r}")
        if started.get("input", {}).get("channels") != [1, 2]:
            failures.append(f"tool input lost channels: {started!r}")
        result = json.loads(finished.get("result", "{}"))
        if result.get("ok") is not True or result.get("technique") != "cv":
            failures.append(f"tool_call_finished result wrong: {result!r}")
    if recorder.tool_errors:
        failures.append(f"tool_call_error fired: {recorder.tool_errors!r}")

    # The MOCK ENGINE actually ran the CV.
    if engine.result is None or not engine.result.num_points > 0:
        failures.append(
            "mock engine did not run: result="
            f"{getattr(engine.result, 'num_points', None)!r}"
        )
    elif engine.result.measured_channels != [1, 2]:
        failures.append(
            f"engine measured wrong channels: "
            f"{engine.result.measured_channels!r}"
        )

    # API-surface assertions on the recorded request kwargs.
    if len(fake.calls) != 2:
        failures.append(f"expected 2 API calls, saw {len(fake.calls)}")
        return
    first, second = fake.calls
    for label, call in (("call1", first), ("call2", second)):
        if call["model"] != "claude-fable-5-1":
            failures.append(f"{label}: model wrong: {call['model']!r}")
        if call["thinking"] != {"type": "adaptive"}:
            failures.append(
                f"{label}: thinking != adaptive: {call['thinking']!r}"
            )
        if call["forbidden_keys"]:
            failures.append(
                f"{label}: forbidden params sent: "
                f"{call['forbidden_keys']!r}"
            )
        if "run_cv" not in call["tool_names"]:
            failures.append(f"{label}: run_cv missing from tools")
        system = call["system"]
        if not (
            isinstance(system, list)
            and system
            and system[0].get("cache_control") == {"type": "ephemeral"}
            and system[0].get("text")
        ):
            failures.append(f"{label}: system/cache_control wrong")
    if first["system"] != second["system"]:
        failures.append("system prompt not byte-identical across calls")
    if first["tool_result"] is not None:
        failures.append("call1 unexpectedly contained a tool_result")
    tool_result = second["tool_result"]
    if tool_result is None:
        failures.append("call2 missing the tool_result block")
    else:
        if tool_result.get("tool_use_id") != TOOL_USE_ID:
            failures.append(f"tool_result id wrong: {tool_result!r}")
        if "is_error" in tool_result:
            failures.append(
                f"is_error present on a success result: {tool_result!r}"
            )
        content = json.loads(tool_result.get("content", "{}"))
        if content.get("ok") is not True or not content.get(
            "num_points", 0
        ) > 0:
            failures.append(f"tool_result content wrong: {content!r}")
    if second["n_messages"] != 3:  # user, assistant, tool_result-user
        failures.append(
            f"call2 history length != 3: {second['n_messages']}"
        )


def main() -> int:
    """Entry point. Returns the process exit code."""
    failures: list[str] = []

    app = QCoreApplication(sys.argv)
    bridge.install()

    engine = MockMeasurementEngine()
    connection = MockConnection()
    connection.connect("MOCK1")
    adapter = EngineAdapter(engine, connection)
    registry = build_registry(adapter)

    fake = FakeAsyncAnthropic()
    worker = AgentWorker(
        registry,
        model="claude-fable-5-1",
        client_factory=lambda **kwargs: fake,
    )
    recorder = Recorder(worker)
    worker.finished.connect(app.quit, Qt.ConnectionType.QueuedConnection)

    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()

    worker.start()
    worker.submit_user_message("run a quick CV on channels 1 and 2")
    app.exec()

    if not worker.wait(5000):
        failures.append("worker thread did not finish after request_stop")
    watchdog.cancel()

    _check(recorder, fake, engine, failures)
    if engine.isRunning():
        failures.append("engine still reports running after the scenario")
    if worker.is_busy():
        failures.append("worker still reports busy after shutdown")

    if failures:
        for failure in failures:
            print("SMOKE FAIL:", failure)
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

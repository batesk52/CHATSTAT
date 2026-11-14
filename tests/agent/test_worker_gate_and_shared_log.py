"""The in-app worker's tool gate and its share of the session log.

The window sets a gate on the dock's worker while the MCP server runs, so the
in-app agent is refused while an MCP client drives the instrument; with no
gate the worker calls its tools exactly as before. When an MCP call opened
the shared session log first, the worker records its own model once.
A real event loop and a fake client; no network, no key, no QThread.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6")

from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.session_log import CALL_SOURCE, SessionLog, read_events  # noqa: E402
from src.agent.tools import ToolRegistry  # noqa: E402


class _Stream:
    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            if False:
                yield None
        return _gen()

    async def get_final_message(self):
        return self._final


class _Client:
    """One tool_use round, then a final answer."""

    def __init__(self, tool_name: str) -> None:
        self.calls = 0
        self.seen_results: list = []
        self.messages = SimpleNamespace(stream=self._stream)
        self._replies = [
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", id="toolu_1", name=tool_name, input={"channels": [1]})],
                stop_reason="tool_use",
                usage=None,
            ),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Done.")],
                stop_reason="end_turn",
                usage=None,
            ),
        ]

    def _stream(self, **kwargs):
        for message in kwargs.get("messages", []):
            if message.get("role") == "user" and isinstance(message.get("content"), list):
                self.seen_results.extend(message["content"])
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Stream(reply)

    async def close(self):
        return None


def _converse(tmp_path, *, gate=None, log=None):
    ran: list = []
    registry = ToolRegistry()

    async def run_ca(args):
        ran.append(args)
        return {"ok": True}

    registry.register(
        {"name": "run_ca", "description": "fake run", "input_schema": {"type": "object", "properties": {}}},
        run_ca,
    )
    log = log or SessionLog(str(tmp_path))
    client = _Client("run_ca")
    worker = AgentWorker(
        registry, api_key=None, model="claude-sonnet-5-5", session_log=log,
        client_factory=lambda **_k: client,
    )
    if gate is not None:
        worker.set_tool_gate(gate)

    async def scenario():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        worker._loop, worker._queue = loop, queue  # noqa: SLF001
        main = asyncio.ensure_future(worker._main())  # noqa: SLF001
        queue.put_nowait("Measure channel 1.")
        for _ in range(300):
            await asyncio.sleep(0.01)
            if client.calls >= 2 and not worker.is_busy():
                break
        worker.request_stop()
        await asyncio.wait_for(main, timeout=5)

    asyncio.run(scenario())
    return ran, client, log


def test_no_gate_runs_the_tool_as_before(tmp_path) -> None:
    ran, client, log = _converse(tmp_path)
    assert ran == [{"channels": [1]}]
    assert json.loads(client.seen_results[-1]["content"]) == {"ok": True}
    kinds = [e["event"] for e in read_events(log.path)]
    assert kinds[0] == "session_start" and "in_app_agent_start" not in kinds


def test_a_refusing_gate_runs_nothing_and_the_model_is_told(tmp_path) -> None:
    asked: list = []

    async def gate(name):
        asked.append(name)
        return "An MCP client is driving the instrument."

    ran, client, log = _converse(tmp_path, gate=gate)
    assert ran == []
    assert asked == ["run_ca"]
    result = client.seen_results[-1]
    assert result["is_error"] is True
    assert json.loads(result["content"]) == {"ok": False, "error": "An MCP client is driving the instrument."}
    logged = [e for e in read_events(log.path) if e["event"] == "tool_result"]
    assert logged[-1]["is_error"] is True and "source" not in logged[-1]


def test_a_passing_gate_runs_the_tool(tmp_path) -> None:
    async def gate(name):
        return None

    ran, _client, _log = _converse(tmp_path, gate=gate)
    assert ran == [{"channels": [1]}]


def test_a_log_opened_by_an_mcp_call_gets_the_agents_model_once(tmp_path) -> None:
    log = SessionLog(str(tmp_path))
    log.start(None, ["run_ca"])                  # as the MCP server opens it
    _ran, _client, log = _converse(tmp_path, log=log)
    events = read_events(log.path)
    assert events[0]["event"] == "session_start" and events[0]["model"] is None
    notes = [e for e in events if e["event"] == "in_app_agent_start"]
    assert len(notes) == 1 and notes[0]["model"] == "claude-sonnet-5-5"


def test_notes_written_during_an_mcp_call_carry_its_source(tmp_path) -> None:
    log = SessionLog(str(tmp_path))
    log.start("m", [])
    log.note("cell_switch_requested", request_id="a")
    marker = CALL_SOURCE.set({"source": "mcp", "client": "claude-code"})
    try:
        log.note("cell_switch_requested", request_id="b")
    finally:
        CALL_SOURCE.reset(marker)
    events = [e for e in read_events(log.path) if e["event"] == "cell_switch_requested"]
    assert "source" not in events[0]
    assert events[1]["source"] == "mcp" and events[1]["client"] == "claude-code"

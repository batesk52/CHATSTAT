"""The per-session agent log: one JSON Lines file per conversation.

The worker's ``_main()`` runs on a real event loop with a fake client
(no network, no key, no QThread), so these tests exercise the same
code path the dock uses: session start, user and assistant messages,
tool calls with their results and durations, errors and session end.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")

from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.session_log import SessionLog, read_events  # noqa: E402
from src.agent.tools import ToolRegistry  # noqa: E402

#: Shaped like a real key so the pattern scrubber is exercised too.
SECRET = "sk-ant-api03-" + "Zq7" * 30
#: A second key-shaped string the worker never saw.
STRANGER = "sk-ant-admin01-" + "Xy9" * 12


class _Stream:
    def __init__(self, final: SimpleNamespace) -> None:
        self._final = final

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            for block in self._final.content:
                if block.type == "text":
                    yield SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(type="text_delta", text=block.text),
                    )
        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self._final


class _Client:
    """Scripted replies: one tool_use round, then a final answer."""

    def __init__(self, tool_name: str, tool_input: dict) -> None:
        self.calls = 0
        self.messages = SimpleNamespace(stream=self._stream)
        self._replies = [
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="Checking the cell."),
                    SimpleNamespace(
                        type="tool_use",
                        id="toolu_1",
                        name=tool_name,
                        input=tool_input,
                    ),
                ],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=120, output_tokens=30),
            ),
            SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=f"Channel 1 reads 1.00 kOhm. ({SECRET})",
                    )
                ],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=200, output_tokens=12),
            ),
        ]

    def _stream(self, **kwargs):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Stream(reply)

    async def close(self) -> None:
        return None


def _registry(result: dict, provenance: dict) -> ToolRegistry:
    """One fake run tool that 'sends a script' while it runs."""
    registry = ToolRegistry()

    async def _run(tool_input: dict) -> dict:
        provenance["scripts_sent"] += 1
        provenance["script_digest"] = "0123456789abcdef"
        await asyncio.sleep(0.01)
        return dict(result)

    registry.register(
        {
            "name": "run_eis",
            "description": "fake run",
            "input_schema": {"type": "object", "properties": {}},
        },
        _run,
    )
    return registry


def _run_conversation(
    tmp_path, result: dict, *, client_factory=None, api_key: str = SECRET
):
    provenance = {"scripts_sent": 0, "script_digest": None}
    log = SessionLog(
        str(tmp_path),
        app_version="9.9.9",
        mode="mock",
        port_fn=lambda: "MOCK1",
        provenance_fn=lambda: dict(provenance),
    )
    client = _Client("run_eis", {"channels": [1], "cr": "100u"})
    worker = AgentWorker(
        _registry(result, provenance),
        api_key=api_key,
        model="claude-fable-5",
        session_log=log,
        client_factory=client_factory or (lambda **_k: client),
    )

    async def _scenario() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        worker._loop, worker._queue = loop, queue
        main = asyncio.ensure_future(worker._main())
        queue.put_nowait("Measure channel 1.")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not worker.is_busy() and queue.empty() and log.started:
                break
        worker.request_stop()
        await asyncio.wait_for(main, timeout=5)

    asyncio.run(_scenario())
    return log


def test_log_records_the_whole_conversation(tmp_path) -> None:
    log = _run_conversation(
        tmp_path,
        {"ok": True, "technique": "eis", "num_points": 33},
    )
    assert log.path is not None
    assert re.fullmatch(
        r"\d{8}_\d{6}_[0-9a-f]{8}\.jsonl", os.path.basename(log.path)
    )
    events = read_events(log.path)
    kinds = [e["event"] for e in events]
    assert kinds == [
        "session_start",
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
        "session_end",
    ]
    for i, event in enumerate(events, start=1):
        assert event["seq"] == i
        datetime.fromisoformat(event["ts"])  # ISO timestamp on every line

    start = events[0]
    assert start["app_version"] == "9.9.9"
    assert start["model"] == "claude-fable-5"
    assert start["tools"] == ["run_eis"]
    assert start["mode"] == "mock"
    assert start["port"] == "MOCK1"

    assert events[1]["text"] == "Measure channel 1."
    assert events[2]["stop_reason"] == "tool_use"
    assert events[2]["tool_calls"] == 1
    assert events[2]["usage"] == {"input_tokens": 120, "output_tokens": 30}

    call, result = events[3], events[4]
    assert call["name"] == "run_eis"
    assert call["arguments"] == {"channels": [1], "cr": "100u"}
    assert result["id"] == call["id"] == "toolu_1"
    assert result["result"] == {"ok": True, "technique": "eis", "num_points": 33}
    assert result["is_error"] is False
    assert result["duration_ms"] >= 5
    assert result["artifacts"] == {
        "script_digest": "0123456789abcdef",
        "scripts_sent": 1,
    }
    assert events[5]["stop_reason"] == "end_turn"
    assert events[6]["reason"] == "stopped"


def test_export_path_is_recorded_as_an_artifact(tmp_path) -> None:
    out = str(tmp_path / "eis_20261009_101500.pssession")
    log = _run_conversation(tmp_path, {"ok": True, "path": out})
    tool_result = [
        e for e in read_events(log.path) if e["event"] == "tool_result"
    ][0]
    assert tool_result["artifacts"]["export_paths"] == [out]


def test_no_key_like_string_reaches_the_file(tmp_path) -> None:
    # The fake tool echoes both the worker's key and a stranger key;
    # the model's final text also quotes the worker's key.
    log = _run_conversation(
        tmp_path,
        {"ok": True, "note": f"leak {SECRET} and {STRANGER}"},
    )
    with open(log.path, encoding="utf-8") as fh:
        raw = fh.read()
    assert SECRET not in raw
    assert STRANGER not in raw
    assert not re.search(r"sk-ant-[A-Za-z0-9_\-]{8,}", raw)
    lowered = raw.lower()
    for header in ("x-api-key", "authorization", "anthropic-version"):
        assert header not in lowered
    assert "[REDACTED]" in raw


def test_client_error_is_logged_without_the_key(tmp_path) -> None:
    def _broken_factory(**_kwargs):
        raise RuntimeError(f"bad key {SECRET}")

    log = _run_conversation(
        tmp_path, {"ok": True}, client_factory=_broken_factory
    )
    events = read_events(log.path)
    errors = [e for e in events if e["event"] == "error"]
    assert len(errors) == 1
    assert "Could not create the Anthropic client" in errors[0]["message"]
    assert SECRET not in json.dumps(events)
    assert events[-1]["event"] == "session_end"


def test_lines_are_on_disk_before_the_session_ends(tmp_path) -> None:
    """A crash mid-run must leave every finished event readable."""
    log = SessionLog(str(tmp_path), app_version="1", mode="hardware")
    log.start("claude-fable-5", ["run_cv"])
    log.user_message("hello")
    # Not ended: read the file as a crash would leave it.
    assert [e["event"] for e in read_events(log.path)] == [
        "session_start",
        "user_message",
    ]
    # A torn final line (power loss mid-write) is skipped, not fatal.
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-10-09T10:00:00", "event": "tool_')
    assert len(read_events(log.path)) == 2
    log.end("crash-test")


def test_no_file_without_a_conversation(tmp_path) -> None:
    log = SessionLog(str(tmp_path / "sessions"))
    log.end("closed")
    assert log.path is None
    assert not (tmp_path / "sessions").exists()

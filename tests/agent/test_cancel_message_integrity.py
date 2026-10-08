"""Cancelling mid-tool-call must leave ``_messages`` API-valid.

The Anthropic Messages API requires every ``tool_use`` block to be answered
by a ``tool_result`` with a matching id in the immediately-following user
message.  ``_handle_tool_use`` collects results in a local list and commits
them only after its loop finishes, while the assistant message carrying the
``tool_use`` blocks is appended BEFORE the loop runs.

So a cancellation landing inside a tool call used to unwind without ever
committing results, leaving a dangling ``tool_use``.  The next request then
400s -- and so does every request after it, because nothing repairs
``_messages``.  Pressing Stop mid-measurement wedged the session
permanently, which is precisely what per-turn cancel exists to avoid.

``_handle_tool_use`` also SWALLOWS the ``CancelledError`` after committing
the synthesized results (2026-09-13 fix), instead of re-raising it. Before
that fix, the turn died silently right there: the tool's chip stayed stuck
"running" forever (nothing ever emitted ``tool_call_error`` for it), and the
model was never asked to react to the cancellation -- it only saw the
cancelled ``tool_result`` whenever the user happened to send a NEXT message.
Swallowing it lets ``_run_turn``'s loop make one more request with the
cancelled result already in history, so the model can acknowledge the stop
and ask what to do next right away. These tests assert both the message-
history contract AND that ``_handle_tool_use`` now returns normally.

Cancelling while the model is still STREAMING is a different, safe case: the
assistant message is not appended until the stream finishes, so no dangling
tool_use exists, and that path is unchanged (still propagates). These tests
target the tool-call path.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")

from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.tools import ToolRegistry  # noqa: E402


class _Block:
    """Minimal stand-in for an SDK tool_use content block."""

    def __init__(self, block_id: str, name: str = "run_eis") -> None:
        self.type = "tool_use"
        self.id = block_id
        self.name = name
        self.input: dict = {"channels": [1]}


class _Response:
    def __init__(self, *blocks: _Block) -> None:
        self.content = list(blocks)


def _worker_with_tools(handlers: dict) -> AgentWorker:
    """Worker whose registry dispatches to the supplied handlers."""
    registry = ToolRegistry()
    for name, handler in handlers.items():
        registry.register(
            {
                "name": name,
                "description": "x" * 50,
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler,
        )
    return AgentWorker(registry, api_key=None)


def _tool_use_ids(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if getattr(b, "type", None) == "tool_use":
                out.append(b.id)
    return out


def _tool_result_ids(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append(b["tool_use_id"])
    return out


def _assert_history_valid(worker: AgentWorker) -> None:
    """Every tool_use id must have a matching tool_result id."""
    used = _tool_use_ids(worker._messages)
    answered = _tool_result_ids(worker._messages)
    dangling = [i for i in used if i not in answered]
    assert not dangling, (
        f"dangling tool_use ids {dangling} -- the next API call would 400. "
        f"messages={worker._messages}"
    )


def test_cancel_during_tool_call_leaves_history_valid() -> None:
    """The headline case: Stop pressed while a measurement is running.

    ``_handle_tool_use`` must return normally (not raise) so the turn's
    while-loop can make a follow-up request; see the module docstring.
    """

    async def _slow(_inp):
        await asyncio.sleep(10)  # stands in for a live measurement
        return {"ok": True}

    w = _worker_with_tools({"run_eis": _slow})
    response = _Response(_Block("toolu_1"))
    w._messages.append({"role": "assistant", "content": response.content})

    errors: list[dict] = []
    w.tool_call_error.connect(lambda payload: errors.append(dict(payload)))

    async def _scenario() -> None:
        task = asyncio.ensure_future(w._handle_tool_use(response))
        await asyncio.sleep(0)  # let it reach the await
        task.cancel()
        await task  # must NOT raise -- the cancellation is swallowed here

    asyncio.run(_scenario())
    _assert_history_valid(w)
    assert [e["id"] for e in errors] == ["toolu_1"], (
        "tool_call_error was not emitted for the cancelled call -- its chip "
        f"would stay stuck 'running' forever in the dock: {errors!r}"
    )


def test_unstarted_parallel_tool_calls_are_also_answered() -> None:
    """Blocks that never dispatched still need results.

    With parallel tool calls, cancellation on the first leaves the later
    blocks completely untouched -- answering only the in-flight one would
    still 400.
    """

    async def _slow(_inp):
        await asyncio.sleep(10)
        return {"ok": True}

    w = _worker_with_tools({"run_eis": _slow})
    response = _Response(
        _Block("toolu_1"), _Block("toolu_2"), _Block("toolu_3")
    )
    w._messages.append({"role": "assistant", "content": response.content})

    errors: list[dict] = []
    w.tool_call_error.connect(lambda payload: errors.append(dict(payload)))

    async def _scenario() -> None:
        task = asyncio.ensure_future(w._handle_tool_use(response))
        await asyncio.sleep(0)
        task.cancel()
        await task  # must NOT raise

    asyncio.run(_scenario())
    _assert_history_valid(w)
    assert len(_tool_result_ids(w._messages)) == 3
    assert sorted(e["id"] for e in errors) == ["toolu_1", "toolu_2", "toolu_3"], (
        f"expected all 3 blocks to get a tool_call_error chip update: {errors!r}"
    )


def test_completed_results_are_not_discarded_on_cancel() -> None:
    """A finished measurement's data survives a later cancellation.

    The old code threw away everything collected in the round, so an
    already-completed run's result was lost along with the interrupted one.
    """
    done: dict = {}

    async def _fast(_inp):
        done["ran"] = True
        return {"ok": True, "points": 197}

    async def _slow(_inp):
        await asyncio.sleep(10)
        return {"ok": True}

    w = _worker_with_tools({"fast": _fast, "run_eis": _slow})
    response = _Response(_Block("toolu_1", "fast"), _Block("toolu_2"))
    w._messages.append({"role": "assistant", "content": response.content})

    async def _scenario() -> None:
        task = asyncio.ensure_future(w._handle_tool_use(response))
        for _ in range(6):  # let the fast tool complete first
            await asyncio.sleep(0)
        task.cancel()
        await task  # must NOT raise

    asyncio.run(_scenario())
    assert done.get("ran") is True, "fast tool never ran; test is not exercising the case"
    _assert_history_valid(w)

    results = [
        b
        for m in w._messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    kept = [r for r in results if "197" in str(r.get("content", ""))]
    assert kept, f"completed result was discarded: {results}"


def test_uncancelled_round_is_unchanged() -> None:
    """The normal path still commits exactly one user message."""

    async def _ok(_inp):
        return {"ok": True}

    w = _worker_with_tools({"run_eis": _ok})
    response = _Response(_Block("toolu_1"))
    w._messages.append({"role": "assistant", "content": response.content})

    asyncio.run(w._handle_tool_use(response))

    _assert_history_valid(w)
    assert len(w._messages) == 2
    entry = w._messages[1]["content"][0]
    assert entry["tool_use_id"] == "toolu_1"
    assert "is_error" not in entry, "successful result wrongly flagged as error"

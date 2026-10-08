"""Bug report (Karl, 2026-09-13): "it fails if I cancel a test. It won't
accept new commands or ask what to do next."

Root cause: ``_handle_tool_use``'s ``except asyncio.CancelledError:`` branch
synthesized the cancelled ``tool_result`` (so the next API request stayed
valid) but then RE-RAISED, which unwound the whole turn immediately. Two
visible symptoms followed:

1. The cancelled tool's chip in the dock never got a ``tool_call_error`` /
   ``tool_call_finished`` signal, so it stayed stuck showing "running..."
   forever (see ``tests/agent/test_cancel_message_integrity.py``).
2. The model was never invoked again to react to the cancellation -- the
   synthesized "Cancelled by user" tool_result just sat in history, unseen
   by the model until the user happened to send a completely separate
   message. From the chat, cancelling looked like "the agent is not
   responding" (matching "won't accept new commands or ask what to do
   next"), even though ``submit_user_message`` itself was never broken --
   see ``tests/agent/test_turn_cancel.py``.

The user-visible fix: once the cancelled tool round is closed out,
``_handle_tool_use`` now returns normally instead of re-raising, so
``_run_turn``'s while-loop makes ONE more request with the cancelled result
already in history -- giving the model a chance to acknowledge the stop and
ask what to do next, right in the same turn, with no second user message
required.

These tests drive the real ``_main()`` loop (per
``tests/agent/test_cancel_turn_live.py``'s pattern: the loop/queue are wired
manually, without a QThread) with a scripted fake client, so the fix is
exercised end to end rather than just at the ``_handle_tool_use`` unit level.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")

from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.tools import ToolRegistry  # noqa: E402


class _ToolBlock:
    def __init__(self, block_id: str, name: str = "run_eis") -> None:
        self.type = "tool_use"
        self.id = block_id
        self.name = name
        self.input: dict = {}


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, content: list, stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = None


class _ScriptedStream:
    """Async CM/iterator that yields nothing and returns a scripted final."""

    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> "_ScriptedStream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            return
            yield  # pragma: no cover

        return _gen()

    async def get_final_message(self) -> _Response:
        return self._response


class _HangingStream:
    """Async CM whose iteration blocks until released (a slow model reply)."""

    def __init__(self, entered: asyncio.Event, release: asyncio.Event, final: _Response) -> None:
        self._entered = entered
        self._release = release
        self._final = final

    async def __aenter__(self) -> "_HangingStream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            self._entered.set()
            await self._release.wait()
            if False:
                yield None

        return _gen()

    async def get_final_message(self) -> _Response:
        return self._final


class _ScriptedClient:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.requests: list[list] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs.get("messages", []))
        if not self._responses:
            raise AssertionError(f"unexpected extra API call #{self.calls}")
        return _ScriptedStream(self._responses.pop(0))

    async def close(self) -> None:
        return None


def _registry_with_slow_tool(
    entered: asyncio.Event, release: asyncio.Event, name: str = "run_eis"
) -> ToolRegistry:
    """A tool registry with ONE handler that blocks until *release* is set."""
    registry = ToolRegistry()

    async def _slow(_inp):
        entered.set()
        await release.wait()
        return {"ok": True}

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
        _slow,
    )
    return registry


def _wire(worker: AgentWorker) -> "asyncio.Queue":
    """Wire the loop/queue the way run() does, without a real QThread."""
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue" = asyncio.Queue()
    worker._loop, worker._queue = loop, queue
    return queue


async def _drain_until_idle(worker: AgentWorker, timeout: float = 5.0) -> bool:
    """Poll until the worker reports not-busy. True if it settled in time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not worker.is_busy():
            return True
        await asyncio.sleep(0.01)
    return False


def test_cancelled_tool_round_gets_a_follow_up_reply_with_no_new_message() -> None:
    """The headline fix: cancel mid-measurement, and the SAME turn still
    ends with a model reply -- no second user message needed."""

    async def _scenario() -> dict:
        entered, release = asyncio.Event(), asyncio.Event()
        registry = _registry_with_slow_tool(entered, release)
        client = _ScriptedClient([
            _Response([_ToolBlock("toolu_1")], "tool_use"),
            _Response(
                [_TextBlock("Stopped. What would you like to do next?")],
                "end_turn",
            ),
        ])
        w = AgentWorker(registry, api_key="test-key", client_factory=lambda **_k: client)
        queue = _wire(w)

        main = asyncio.ensure_future(w._main())
        queue.put_nowait("please run something slow")
        await asyncio.wait_for(entered.wait(), timeout=5)

        w.cancel_turn()
        settled = await _drain_until_idle(w)

        w.request_stop()
        release.set()
        try:
            await asyncio.wait_for(main, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        return {
            "settled": settled,
            "calls": client.calls,
            "messages": list(w._messages),
        }

    result = asyncio.run(_scenario())

    assert result["settled"], "worker never went idle after cancel_turn()"
    assert result["calls"] == 2, (
        "expected exactly 2 API calls (the tool_use turn + ONE automatic "
        f"follow-up), got {result['calls']} -- the model was not asked to "
        "react to the cancellation"
    )

    texts = [
        getattr(block, "text", None)
        for message in result["messages"]
        if message.get("role") == "assistant"
        for block in (message.get("content") or [])
        if getattr(block, "type", None) == "text"
    ]
    assert any("what would you like to do next" in (t or "").lower() for t in texts), (
        f"no follow-up acknowledgement landed in history: {result['messages']!r}"
    )


def test_cancelled_tool_result_tells_the_model_not_to_retry() -> None:
    """The synthesized tool_result wording nudges the model away from just
    re-issuing the same (cancelled) tool call."""

    async def _scenario() -> list:
        entered, release = asyncio.Event(), asyncio.Event()
        registry = _registry_with_slow_tool(entered, release)
        client = _ScriptedClient([
            _Response([_ToolBlock("toolu_1")], "tool_use"),
            _Response([_TextBlock("ok")], "end_turn"),
        ])
        w = AgentWorker(registry, api_key="test-key", client_factory=lambda **_k: client)
        queue = _wire(w)

        main = asyncio.ensure_future(w._main())
        queue.put_nowait("please run something slow")
        await asyncio.wait_for(entered.wait(), timeout=5)
        w.cancel_turn()
        await _drain_until_idle(w)

        w.request_stop()
        release.set()
        try:
            await asyncio.wait_for(main, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return list(w._messages)

    messages = asyncio.run(_scenario())
    result_contents = [
        block.get("content")
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert result_contents, f"no tool_result found in history: {messages!r}"
    assert any("do not retry" in str(c).lower() for c in result_contents), (
        f"cancelled tool_result text does not discourage auto-retry: {result_contents!r}"
    )


def test_worker_still_accepts_a_real_new_message_after_cancel() -> None:
    """End-to-end version of the bug report: cancel, then send a genuinely
    NEW user message, and confirm it gets its own reply."""

    async def _scenario() -> dict:
        entered, release = asyncio.Event(), asyncio.Event()
        registry = _registry_with_slow_tool(entered, release)
        client = _ScriptedClient([
            _Response([_ToolBlock("toolu_1")], "tool_use"),
            _Response([_TextBlock("Stopped, what next?")], "end_turn"),
            _Response([_TextBlock("Sure, doing that now.")], "end_turn"),
        ])
        w = AgentWorker(registry, api_key="test-key", client_factory=lambda **_k: client)
        queue = _wire(w)

        main = asyncio.ensure_future(w._main())
        queue.put_nowait("please run something slow")
        await asyncio.wait_for(entered.wait(), timeout=5)
        w.cancel_turn()
        first_settled = await _drain_until_idle(w)

        # A genuinely new message, exactly like a user typing after Stop.
        w.submit_user_message("go ahead and do the next thing")
        # Poll for the THIRD call specifically (not just "not busy"): right
        # after cancel_turn() settles, is_busy() is already False, and the
        # new message has not been picked off the queue yet -- a bare
        # "wait until idle" would pass instantly without the second turn
        # ever having run.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline and client.calls < 3:
            await asyncio.sleep(0.01)
        second_settled = client.calls >= 3 and await _drain_until_idle(w)

        w.request_stop()
        release.set()
        try:
            await asyncio.wait_for(main, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        return {
            "first_settled": first_settled,
            "second_settled": second_settled,
            "calls": client.calls,
        }

    result = asyncio.run(_scenario())
    assert result["first_settled"], "worker stuck after the cancelled turn"
    assert result["second_settled"], (
        "worker never finished the SECOND message's turn -- it is stuck "
        "and won't accept new commands, which is the reported bug"
    )
    assert result["calls"] == 3, (
        f"expected 3 API calls (tool_use + auto follow-up + new message), "
        f"got {result['calls']}"
    )


def test_second_cancel_during_the_automatic_follow_up_still_works() -> None:
    """Cancelling AGAIN while the automatic follow-up is itself in flight
    (streaming, not a tool call) must still end cleanly and keep the worker
    usable -- swallowing the first cancellation must not make the turn
    uncancellable."""

    async def _scenario() -> dict:
        entered, release = asyncio.Event(), asyncio.Event()
        registry = _registry_with_slow_tool(entered, release)
        follow_up_entered, follow_up_release = asyncio.Event(), asyncio.Event()

        class _Client:
            def __init__(self) -> None:
                self.calls = 0
                self.messages = SimpleNamespace(stream=self._stream)

            def _stream(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _ScriptedStream(_Response([_ToolBlock("toolu_1")], "tool_use"))
                return _HangingStream(
                    follow_up_entered,
                    follow_up_release,
                    _Response([_TextBlock("late reply")], "end_turn"),
                )

            async def close(self) -> None:
                return None

        client = _Client()
        w = AgentWorker(registry, api_key="test-key", client_factory=lambda **_k: client)
        queue = _wire(w)

        main = asyncio.ensure_future(w._main())
        queue.put_nowait("please run something slow")
        await asyncio.wait_for(entered.wait(), timeout=5)

        w.cancel_turn()  # first cancel: interrupts the tool call
        await asyncio.wait_for(follow_up_entered.wait(), timeout=5)

        w.cancel_turn()  # second cancel: interrupts the automatic follow-up
        settled = await _drain_until_idle(w)

        queue.put_nowait("still there?")
        loop_alive = not main.done()

        follow_up_release.set()
        w.request_stop()
        try:
            await asyncio.wait_for(main, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        return {"settled": settled, "loop_alive": loop_alive}

    result = asyncio.run(_scenario())
    assert result["settled"], "worker never settled after the second cancel"
    assert result["loop_alive"], "the worker loop died after the second cancel"

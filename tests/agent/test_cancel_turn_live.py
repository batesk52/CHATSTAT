"""``cancel_turn()`` really cancels a running turn and the worker survives.

The other cancellation tests construct an ``AgentWorker`` without starting
its thread, so ``self._loop`` is None and both ``cancel_turn()`` and
``request_stop()`` short-circuit at their ``if loop is None: return`` guard
— the closures that actually call ``task.cancel()`` are never reached.
A mutation that turned that call into a silent no-op (Stop wired up but
never cancelling anything) left all of them passing.

These tests drive ``_main()`` on a real event loop with a fake client that
hangs mid-stream, so the cancellation path is genuinely exercised: the turn
must end, the loop must SURVIVE, and a message queued afterwards must still
be processed.
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


class _HangingStream:
    """Async CM whose iteration blocks until released."""

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def __aenter__(self) -> "_HangingStream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self):
        async def _gen():
            self._entered.set()
            await self._release.wait()  # cancellation lands here
            if False:
                yield None
        return _gen()

    async def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[], stop_reason="end_turn")


class _FakeClient:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.calls = 0
        self._entered = entered
        self._release = release
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **_kwargs):
        self.calls += 1
        return _HangingStream(self._entered, self._release)

    async def close(self) -> None:
        return None


def _worker(client: _FakeClient) -> AgentWorker:
    return AgentWorker(
        ToolRegistry(), api_key="test-key", client_factory=lambda **_k: client
    )


def test_cancel_turn_ends_the_turn_and_the_loop_survives() -> None:
    """The core promise: the turn stops, the agent keeps running."""
    results: dict = {}

    async def _scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        client = _FakeClient(entered, release)
        w = _worker(client)

        # Wire the loop/queue the way run() does, without a QThread.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        w._loop, w._queue = loop, queue

        main = asyncio.ensure_future(w._main())
        queue.put_nowait("first message")
        await asyncio.wait_for(entered.wait(), timeout=5)

        w.cancel_turn()  # the call under test
        await asyncio.sleep(0.05)

        results["turn_cleared"] = w._turn_task is None
        results["loop_alive"] = not main.done()
        results["not_stopping"] = w._stop_requested is False

        # A message queued AFTER the cancel must still be picked up.
        entered.clear()
        queue.put_nowait("second message")
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            results["second_turn_ran"] = True
        except asyncio.TimeoutError:
            results["second_turn_ran"] = False

        release.set()
        w.request_stop()
        try:
            await asyncio.wait_for(main, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        results["calls"] = client.calls

    asyncio.run(_scenario())

    assert results["turn_cleared"], "turn task was not cleared after cancel"
    assert results["loop_alive"], (
        "_main exited on cancel -- the worker died, which is the bug this "
        "feature exists to fix"
    )
    assert results["not_stopping"], "cancel_turn latched the shutdown flag"
    assert results["second_turn_ran"], (
        "no turn ran after the cancel -- the agent was left unusable"
    )
    assert results["calls"] == 2, f"expected 2 stream calls, got {results['calls']}"


def test_request_stop_does_end_the_loop() -> None:
    """Contrast: app-close still terminates _main (unchanged behaviour)."""
    ended: dict = {}

    async def _scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        client = _FakeClient(entered, release)
        w = _worker(client)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        w._loop, w._queue = loop, queue

        main = asyncio.ensure_future(w._main())
        queue.put_nowait("hello")
        await asyncio.wait_for(entered.wait(), timeout=5)

        w.request_stop()
        release.set()
        try:
            await asyncio.wait_for(main, timeout=5)
            ended["clean"] = True
        except asyncio.TimeoutError:
            ended["clean"] = False
        except asyncio.CancelledError:
            ended["clean"] = True

    asyncio.run(_scenario())
    assert ended["clean"], "request_stop did not terminate the main loop"

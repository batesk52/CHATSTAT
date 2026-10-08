"""Pressing Stop during an agent measurement must abort the engine.

The agent dock's Stop cancels the worker's turn task; that raises
``asyncio.CancelledError`` inside ``EngineAdapter.run_technique`` at the
``await asyncio.wrap_future(future)`` point. Because CancelledError is a
BaseException it is NOT caught by the adapter's ``except Exception``, so
without an explicit handler the LLM turn unwinds but the engine keeps
sweeping the remaining channels (and, for banded EIS, the remaining bands).
This guards that the adapter aborts the in-flight engine run on cancel.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

import pytest

from src.agent import engine_adapter as ea_mod
from src.agent.engine_adapter import EngineAdapter


class _AbortRecordingEngine:
    """Minimal engine stub recording abort() and exposing the signals
    ``run_technique`` references (await_signal is monkeypatched, so the
    signal objects are never actually connected)."""

    def __init__(self) -> None:
        self.aborted = False
        self.measurement_finished = object()
        self.measurement_error = object()

    def isRunning(self) -> bool:
        return False

    def start_measurement(self, connection, config) -> None:
        pass

    def abort(self) -> None:
        self.aborted = True


def test_run_technique_aborts_engine_when_turn_cancelled(monkeypatch):
    engine = _AbortRecordingEngine()
    adapter = EngineAdapter(engine, object())

    # await_signal returns a future that never resolves, so the measurement
    # await hangs until the task is cancelled (simulating Stop mid-run).
    never: concurrent.futures.Future = concurrent.futures.Future()
    monkeypatch.setattr(ea_mod, "await_signal", lambda *a, **k: never)

    started = asyncio.Event()

    async def _fake_run_on_gui(func, *args):
        # Mirror the real start: marks the agent run + 'starts' the engine.
        result = func(*args)
        started.set()
        return result

    monkeypatch.setattr(ea_mod, "run_on_gui", _fake_run_on_gui)

    async def _drive():
        task = asyncio.ensure_future(
            adapter.run_technique("eis", {}, channels=[1])
        )
        # Wait until the run has 'started' and is parked on the await.
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0)  # let it reach wrap_future(never)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())

    assert engine.aborted is True, (
        "engine.abort() must be called when the agent turn is cancelled "
        "mid-measurement, or the device keeps sweeping after Stop"
    )

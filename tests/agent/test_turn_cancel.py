"""Stop cancels the turn, it does not kill the worker.

Before 2026-07-28 the dock's Stop button called ``shutdown()`` ->
``request_stop()``, which sets ``_stop_requested``.  That makes
``submit_user_message`` refuse new input and makes the main loop break out,
ending the QThread -- so one press ended the conversation until the app was
relaunched.

The distinction under test is between the two entry points, so these tests
assert the FLAG and the follow-up behaviour, not just that a call succeeded.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6")
pytest.importorskip("anthropic")

from src.agent.agent_worker import AgentWorker  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import (  # noqa: E402
    MockConnection,
    MockMeasurementEngine,
)
from src.agent.tools import build_registry  # noqa: E402


def _worker() -> AgentWorker:
    """A worker over the mock engine; no API key, no turn is ever run."""
    registry = build_registry(
        EngineAdapter(MockMeasurementEngine(), MockConnection())
    )
    return AgentWorker(registry, api_key=None)


def test_cancel_turn_does_not_set_stop_flag() -> None:
    """cancel_turn must not latch the shutdown flag."""
    w = _worker()
    w.cancel_turn()
    assert w._stop_requested is False, (
        "cancel_turn latched the shutdown flag; Stop would kill the worker"
    )


def test_request_stop_still_sets_stop_flag() -> None:
    """App-close behaviour is unchanged."""
    w = _worker()
    w.request_stop()
    assert w._stop_requested is True


def test_messages_still_accepted_after_cancel_turn() -> None:
    """The agent remains usable after Stop.

    This is the actual user-visible bug: submit_user_message drops input
    once _stop_requested is set, so the chat went dead.
    """
    w = _worker()
    w.cancel_turn()
    w.submit_user_message("still listening?")
    assert w._pending == ["still listening?"], (
        "message was dropped after cancel_turn; the agent is dead"
    )


def test_messages_dropped_after_request_stop() -> None:
    """Contrast: real shutdown DOES refuse new input (unchanged)."""
    w = _worker()
    w.request_stop()
    w.submit_user_message("too late")
    assert w._pending == []


def test_cancel_turn_is_safe_with_no_loop_running() -> None:
    """Idempotent and harmless before the thread ever starts."""
    w = _worker()
    w.cancel_turn()
    w.cancel_turn()
    assert w._stop_requested is False

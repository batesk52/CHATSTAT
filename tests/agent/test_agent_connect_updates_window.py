"""The window learns about a connect or disconnect the agent made.

On the instrument, an MCP client connected through connect_device and ran
measurements while the Connection panel still said "Disconnected": the
agent opens the port directly, not through the Connect button. The
adapter now calls a callback after each connect and disconnect.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("PyQt6")

import src.agent.engine_adapter as ea  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import MockConnection  # noqa: E402


def _direct(fn, *args, **kwargs):
    """Stands in for ``run_on_gui``: call at once, return an awaitable."""
    future = asyncio.get_running_loop().create_future()
    try:
        future.set_result(fn(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001
        future.set_exception(exc)
    return future


class _IdleEngine:
    def isRunning(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _no_qt_hop(monkeypatch):
    monkeypatch.setattr(ea, "run_on_gui", _direct)


def test_connect_and_disconnect_call_back_with_the_new_state():
    connection = MockConnection()
    seen = []
    adapter = EngineAdapter(
        _IdleEngine(),
        connection,
        on_connection_changed=lambda: seen.append(connection.is_connected),
    )
    assert asyncio.run(adapter.connect_device("COM6"))["ok"] is True
    assert asyncio.run(adapter.disconnect_device())["ok"] is True
    assert seen == [True, False]


def test_a_failing_callback_does_not_fail_the_connect():
    def boom():
        raise RuntimeError("panel gone")

    adapter = EngineAdapter(
        _IdleEngine(), MockConnection(), on_connection_changed=boom
    )
    assert asyncio.run(adapter.connect_device("COM6"))["ok"] is True


def test_no_callback_is_fine():
    adapter = EngineAdapter(_IdleEngine(), MockConnection())
    assert asyncio.run(adapter.connect_device("COM6"))["ok"] is True

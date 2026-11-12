"""End to end: an MCP client drives the real window's tools on the mock engine.

The main window is built offscreen in mock mode (EMSTAT_AGENT_MOCK=1), its
MCP server is turned on at an ephemeral 127.0.0.1 port, and the official MCP
client connects from a worker thread with its own event loop while Qt spins
here. The test plays the person at the window (the cell-switch banner). No
instrument, no API key; the session log goes to a temp folder.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent import bridge, session_log  # noqa: E402
from src.agent.memory_tools import runs_since_last_save  # noqa: E402
from src.agent.session_log import read_events  # noqa: E402
from src.data import app_settings  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402
from src.gui.mcp_controls import claude_code_command  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    bridge.install()
    yield app


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("EMSTAT_AGENT_MOCK", "1")
    monkeypatch.setattr(session_log, "SESSION_LOG_DIR", str(tmp_path / "sessions"))
    win = MainWindow()
    yield win
    win.close()


def _drive(client_coroutine, on_tick, limit_ms: int = 60000):
    """Run the client on a worker thread while Qt spins; ``on_tick`` is the person."""
    app = QCoreApplication.instance()
    results: list = []

    def worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            results.append(loop.run_until_complete(client_coroutine()))
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            loop.close()
            QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    ticker = QTimer()
    ticker.timeout.connect(on_tick)
    ticker.start(20)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(app.quit)
    guard.start(limit_ms)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    app.exec()
    thread.join(10.0)
    ticker.stop()
    guard.stop()
    assert not thread.is_alive(), "the MCP client did not finish"
    result = results[0]
    if isinstance(result, BaseException):
        raise result
    return result


async def _connect(url: str, token: str, body):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0, read=120.0)) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await body(session)


def _json(result) -> dict:
    return json.loads(result.content[0].text)


def test_the_server_is_off_by_default(window) -> None:
    control = window._mcp_control  # noqa: SLF001
    assert control.url is None and control.server is None
    assert control.status_label.text() == "MCP: off"
    assert not control.toggle_action.isChecked()
    assert app_settings.get_mcp_server_enabled() is False


def test_an_mcp_client_drives_the_window_like_the_in_app_agent(window) -> None:
    control = window._mcp_control  # noqa: SLF001
    assert control.set_enabled(True, port=0)
    url = control.url
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
    assert control.status_label.text() == f"MCP: {url}"
    assert control.toggle_action.isChecked()
    token = app_settings.get_mcp_server_token()
    registry = window._agent_registry  # noqa: SLF001
    banner = window._agent_panel.cell_switch_prompt  # noqa: SLF001
    log = window._agent_session_log  # noqa: SLF001

    async def session_body(session):
        out: dict = {}
        tools = await session.list_tools()
        out["tools"] = [t.name for t in tools.tools]
        out["status"] = _json(await session.call_tool("device_status", {}))
        out["run"] = await session.call_tool(
            "run_ca", {"channels": [1], "e_dc": 0.2, "t_run": 1, "t_interval": 0.1}
        )
        out["limit"] = await session.call_tool(
            "run_ca", {"channels": [1], "e_dc": 2.0, "t_run": 1}
        )
        # Busy: a long multichannel run is in the engine; a second run waits for nothing.
        first = asyncio.ensure_future(session.call_tool(
            "run_ca", {"channels": list(range(1, 17)), "t_run": 1, "t_interval": 0.1}
        ))
        for _ in range(200):
            if _json(await session.call_tool("device_status", {}))["engine_running"]:
                break
            await asyncio.sleep(0.01)
        out["busy"] = await session.call_tool("run_ca", {"channels": [2], "t_run": 1})
        out["first"] = await first
        # The person confirms the cell switch at the window.
        out["switch"] = await session.call_tool(
            "request_cell_switch", {"new_medium": "PBS, pH 7.4", "reason": "rinse"}
        )
        # While the in-app agent is in a turn, only read-only tools answer.
        await bridge.run_on_gui(setattr, window, "_agent_turn_active", True)
        out["in_app_busy"] = await session.call_tool("run_ca", {"channels": [1], "t_run": 1})
        out["in_app_status"] = await session.call_tool("device_status", {})
        await bridge.run_on_gui(setattr, window, "_agent_turn_active", False)
        return out

    async def client():
        out = await _connect(url, token, session_body)
        try:
            await _connect(url, "not-the-token", lambda s: s.list_tools())
        except BaseException as exc:  # noqa: BLE001 - the HTTP 401 surfaces as an error
            out["bad_token"] = exc
        else:
            out["bad_token"] = None
        return out

    pressed: list = []

    def person():
        if banner.pending_request_id and not pressed:
            pressed.append(banner._message.text())  # noqa: SLF001
            banner._continue_button.click()  # noqa: SLF001

    out = _drive(client, person)

    # Same tools as the in-app agent, every one of them.
    assert sorted(out["tools"]) == registry.names()
    assert len(out["tools"]) == len(registry) == len(registry.tool_defs)
    assert out["status"]["connected"] is True

    run = out["run"]
    assert run.isError is False
    body = _json(run)
    assert body["ok"] is True and body["num_points"] > 0 and body["measured_channels"] == [1]

    limit = _json(out["limit"])
    assert out["limit"].isError is True and limit["refused_by"] == "request_limits"

    assert out["busy"].isError is True
    assert _json(out["busy"])["error"].startswith("Engine is busy")
    assert out["first"].isError is False

    switch = _json(out["switch"])
    assert switch["performed"] is True and switch["confirmation"] == "confirmed"
    assert "Switch the cell to PBS, pH 7.4" in pressed[0]
    assert banner.isHidden()

    assert out["in_app_busy"].isError is True
    assert "in-app agent" in _json(out["in_app_busy"])["error"]
    assert out["in_app_status"].isError is False

    assert out["bad_token"] is not None

    # The session log has every call, marked as MCP, and the memory note's
    # run list (copied from the log) includes them.
    events = read_events(log.path)
    calls = [e for e in events if e["event"] == "tool_call"]
    assert calls and all(e["source"] == "mcp" for e in calls)
    names = [e["name"] for e in calls]
    assert names.count("run_ca") == 5 and "request_cell_switch" in names
    kinds = [e["event"] for e in events]
    assert "limit_refused" in kinds
    assert "cell_switch_requested" in kinds and "cell_switch_resolved" in kinds
    runs = runs_since_last_save(events)
    assert "run_ca (via mcp client" in runs
    assert token not in open(log.path, encoding="utf-8").read()

    control.set_enabled(False)
    assert control.url is None and control.status_label.text() == "MCP: off"
    assert app_settings.get_mcp_server_enabled() is False


def test_the_refusal_covers_the_in_app_agent_and_a_sequence(window) -> None:
    assert window._mcp_refusal() is None  # noqa: SLF001
    window._sequence_active = True  # noqa: SLF001
    assert "sequence" in window._mcp_refusal()  # noqa: SLF001
    window._sequence_active = False  # noqa: SLF001
    window._agent_panel.worker.agent_turn_started.emit()  # noqa: SLF001
    QCoreApplication.processEvents()
    assert "in-app agent" in window._mcp_refusal()  # noqa: SLF001
    window._agent_panel.worker.agent_turn_done.emit()  # noqa: SLF001
    QCoreApplication.processEvents()
    assert window._mcp_refusal() is None  # noqa: SLF001


def test_the_setting_starts_the_server_with_the_window(qapp, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EMSTAT_AGENT_MOCK", "1")
    monkeypatch.setattr(session_log, "SESSION_LOG_DIR", str(tmp_path / "sessions"))
    app_settings.set_mcp_server_enabled(True)
    # The control reads the port through get_mcp_server_port; 0 = a free port.
    import src.gui.mcp_controls as controls

    monkeypatch.setattr(controls, "get_mcp_server_port", lambda: 0)
    win = MainWindow()
    try:
        assert win._mcp_control.url is not None  # noqa: SLF001
        assert win._mcp_control.toggle_action.isChecked()  # noqa: SLF001
    finally:
        win.close()
    assert win._mcp_control.url is None  # noqa: SLF001


def test_the_claude_code_command() -> None:
    assert claude_code_command("http://127.0.0.1:47615/mcp", "abc") == (
        'claude mcp add --transport http chatstat http://127.0.0.1:47615/mcp '
        '--header "Authorization: Bearer abc"'
    )

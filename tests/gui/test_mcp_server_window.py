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
import subprocess
import sys
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from src.agent import bridge, session_log  # noqa: E402
from src.agent.driver_lease import MCP_DRIVING_MESSAGE  # noqa: E402
from src.agent.engine_adapter import build_technique_config  # noqa: E402
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
        # A second MCP run while the first drives: refused by the lease.
        out["second"] = await session.call_tool("run_ca", {"channels": [2], "t_run": 1})
        out["first"] = await first
        # A run the lease does not know about (as the person's own run on the
        # instrument would be): the engine's busy check refuses the MCP run.
        config = build_technique_config(
            "ca", {"channels": list(range(1, 17)), "t_run": 1, "t_interval": 0.1}
        )
        await bridge.run_on_gui(
            window._agent_engine.start_measurement,  # noqa: SLF001
            window._agent_connection,  # noqa: SLF001
            config,
        )
        out["busy"] = await session.call_tool("run_ca", {"channels": [2], "t_run": 1})
        for _ in range(500):
            if not _json(await session.call_tool("device_status", {}))["engine_running"]:
                break
            await asyncio.sleep(0.01)
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

    assert out["second"].isError is True
    assert _json(out["second"])["error"].startswith("Another MCP call is driving")
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
    assert names.count("run_ca") == 6 and "request_cell_switch" in names
    kinds = [e["event"] for e in events]
    assert "limit_refused" in kinds
    assert "cell_switch_requested" in kinds and "cell_switch_resolved" in kinds
    switch_lines = [e for e in events if e["event"].startswith("cell_switch_")]
    assert all(e["source"] == "mcp" and e["client"] for e in switch_lines)
    runs = runs_since_last_save(events)
    assert "run_ca (via mcp client" in runs
    assert token not in open(log.path, encoding="utf-8").read()

    control.set_enabled(False)
    assert control.url is None and control.status_label.text() == "MCP: off"
    assert app_settings.get_mcp_server_enabled() is False


def test_the_refusal_covers_the_in_app_agent_and_a_sequence(window) -> None:
    take, give_back = window._mcp_take_driver, window._mcp_give_back_driver  # noqa: SLF001
    window._sequence_active = True  # noqa: SLF001
    assert "sequence" in take("run_ca", "c1", "x")
    window._sequence_active = False  # noqa: SLF001
    window._agent_panel.worker.agent_turn_started.emit()  # noqa: SLF001
    QCoreApplication.processEvents()
    assert "in-app agent" in take("run_ca", "c1", "x")
    window._agent_panel.worker.agent_turn_done.emit()  # noqa: SLF001
    QCoreApplication.processEvents()
    assert take("run_ca", "c1", "x") is None          # taken
    assert "Another MCP call" in take("run_ca", "c2", "y")
    give_back("c2")                                    # not the holder: ignored
    assert "Another MCP call" in take("run_ca", "c2", "y")
    give_back("c1")
    assert take("run_ca", "c2", "y") is None
    give_back("c2")
    assert window._driver_lease.holder is None  # noqa: SLF001


def test_the_dock_lease_and_the_mcp_lease_exclude_each_other(window) -> None:
    lease = window._driver_lease  # noqa: SLF001
    dock_take = window._dock_take_driver  # noqa: SLF001
    assert dock_take("device_status") is None and lease.holder is None   # read-only
    assert dock_take("run_ca") is None and lease.holder == "dock"
    assert "in-app agent" in window._mcp_take_driver("run_ca", "c1", "x")  # noqa: SLF001
    window._agent_panel.worker.agent_turn_done.emit()  # noqa: SLF001 - the turn ends
    QCoreApplication.processEvents()
    assert lease.holder is None
    assert window._mcp_take_driver("run_ca", "c1", "x") is None  # noqa: SLF001
    assert dock_take("run_ca") == MCP_DRIVING_MESSAGE
    assert dock_take("analyze_cv") == MCP_DRIVING_MESSAGE     # pyplot stays on one thread
    assert dock_take("list_memories") is None
    window._mcp_give_back_driver("c1")  # noqa: SLF001
    assert dock_take("run_ca") is None


def _start_window_server(window):
    control = window._mcp_control  # noqa: SLF001
    assert control.set_enabled(True, port=0)
    return control, control.url, app_settings.get_mcp_server_token()


def test_while_an_mcp_cell_switch_waits_no_other_driver_runs(window, monkeypatch) -> None:
    """Review round 1, finding 1, both directions plus two clients.

    Client A asks for a cell switch; while the banner is up: the dock's agent
    is refused (through the worker's own gate), Start and the sequencer are
    refused, and a second MCP client can neither run, abort nor replace the
    banner. Read-only tools still answer. Then the person presses Continue.
    """
    control, url, token = _start_window_server(window)
    banner = window._agent_panel.cell_switch_prompt  # noqa: SLF001
    worker = window._agent_panel.worker  # noqa: SLF001
    engine = window._agent_engine  # noqa: SLF001
    warnings: list = []
    monkeypatch.setattr(
        "src.gui.main_window.QMessageBox.warning",
        lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""),
    )
    monkeypatch.setattr(
        "src.gui.sequence_panel.QMessageBox.warning",
        lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""),
    )
    state: dict = {}

    async def second_client(session):
        out = {}
        out["run"] = await session.call_tool("run_ca", {"channels": [2], "t_run": 1})
        out["abort"] = await session.call_tool("abort_measurement", {})
        out["switch"] = await session.call_tool(
            "request_cell_switch", {"new_medium": "something else", "reason": "x"}
        )
        out["status"] = await session.call_tool("device_status", {})
        return out

    async def client():
        async def first(session):
            task = asyncio.ensure_future(session.call_tool(
                "request_cell_switch", {"new_medium": "PBS, pH 7.4", "reason": "rinse"}
            ))
            for _ in range(500):
                if await bridge.run_on_gui(lambda: banner.pending_request_id):
                    break
                await asyncio.sleep(0.01)
            state["banner_id"] = await bridge.run_on_gui(lambda: banner.pending_request_id)
            # The dock's agent, through the worker's own gate.
            state["dock"] = await worker._gated_dispatch(  # noqa: SLF001
                "run_ca", {"channels": [3], "e_dc": 0.1, "t_run": 1, "t_interval": 0.1}
            )
            state["dock_status"] = await worker._gated_dispatch("device_status", {})  # noqa: SLF001
            state["engine_ran"] = engine.isRunning()
            state["other"] = await _connect(url, token, second_client)
            state["banner_after"] = await bridge.run_on_gui(lambda: banner.pending_request_id)
            # The person at the window presses Start and Run Sequence.
            await bridge.run_on_gui(window._on_start_measurement)  # noqa: SLF001
            await bridge.run_on_gui(window._sequence_panel._on_run)  # noqa: SLF001
            state["go"] = True
            return await task

        return await _connect(url, token, first)

    def person():
        if state.get("go") and banner.pending_request_id:
            banner._continue_button.click()  # noqa: SLF001

    result = _drive(client, person)

    assert _json(result)["performed"] is True
    dock_json, dock_error = state["dock"]
    assert dock_error is True and json.loads(dock_json)["error"] == MCP_DRIVING_MESSAGE
    assert state["dock_status"][1] is False
    assert state["engine_ran"] is False
    other = state["other"]
    for key in ("run", "abort", "switch"):
        assert other[key].isError is True, key
        assert "Another MCP call" in _json(other[key])["error"], key
    assert other["status"].isError is False
    assert state["banner_after"] == state["banner_id"]          # not replaced
    assert warnings == [MCP_DRIVING_MESSAGE, MCP_DRIVING_MESSAGE]
    assert window._driver_lease.holder is None  # noqa: SLF001 - given back
    assert window._sequence_active is False  # noqa: SLF001


def test_an_mcp_call_is_refused_while_the_dock_drives(window) -> None:
    control, url, token = _start_window_server(window)
    worker = window._agent_panel.worker  # noqa: SLF001

    async def client():
        state = {}
        state["dock"] = await worker._gated_dispatch(  # noqa: SLF001
            "run_ca", {"channels": [1], "t_run": 1, "t_interval": 0.1}
        )

        async def body(session):
            refused = await session.call_tool("run_ca", {"channels": [2], "t_run": 1})
            status = await session.call_tool("device_status", {})
            # The dock's turn ends.
            await bridge.run_on_gui(worker.agent_turn_done.emit)
            for _ in range(100):
                if await bridge.run_on_gui(lambda: window._driver_lease.holder is None):  # noqa: SLF001
                    break
                await asyncio.sleep(0.01)
            ran = await session.call_tool("run_ca", {"channels": [2], "t_run": 1, "t_interval": 0.1})
            return refused, status, ran

        state["mcp"] = await _connect(url, token, body)
        return state

    state = _drive(client, lambda: None)
    assert state["dock"][1] is False
    refused, status, ran = state["mcp"]
    assert refused.isError is True and "in-app agent" in _json(refused)["error"]
    assert status.isError is False
    assert ran.isError is False and _json(ran)["measured_channels"] == [2]


def test_with_the_server_off_the_dock_has_no_gate(window) -> None:
    worker = window._agent_panel.worker  # noqa: SLF001
    assert worker._tool_gate is None  # noqa: SLF001
    _start_window_server(window)
    assert worker._tool_gate is not None  # noqa: SLF001
    window._mcp_control.set_enabled(False)  # noqa: SLF001
    assert worker._tool_gate is None  # noqa: SLF001


def test_a_client_killed_during_a_cell_switch_takes_the_banner_down(window) -> None:
    """Review round 1, finding 3, with the window and a killed client process."""
    control, url, token = _start_window_server(window)
    banner = window._agent_panel.cell_switch_prompt  # noqa: SLF001
    helper = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "agent", "_mcp_client_process.py"
    )
    proc = subprocess.Popen(
        [sys.executable, helper, url, token, "request_cell_switch",
         json.dumps({"new_medium": "PBS", "reason": "x"})],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _spin_until(lambda: banner.pending_request_id is not None, 30)
        proc.kill()
        proc.wait(10)
        assert _spin_until(lambda: banner.pending_request_id is None, 10), (
            "the banner stayed up for a client that is gone"
        )
        assert banner.isHidden()
        assert _spin_until(lambda: window._driver_lease.holder is None, 5)  # noqa: SLF001
    finally:
        if proc.poll() is None:
            proc.kill()
    events = read_events(window._agent_session_log.path)  # noqa: SLF001
    resolved = [e for e in events if e["event"] == "cell_switch_resolved"]
    assert resolved and resolved[-1]["confirmation"] == "stopped"


def test_turning_off_asks_first_while_a_call_runs(window, monkeypatch) -> None:
    """Review round 1, finding 5."""
    control, url, token = _start_window_server(window)
    banner = window._agent_panel.cell_switch_prompt  # noqa: SLF001
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    asked: list = []

    def question(*args, **kwargs):
        asked.append(args[2])
        return answers.pop(0)

    monkeypatch.setattr("src.gui.mcp_controls.QMessageBox.question", question)
    seen: dict = {}

    async def client():
        async def body(session):
            # The server goes away mid-call; do not wait for the client's
            # own read timeout.
            return await asyncio.wait_for(session.call_tool(
                "request_cell_switch", {"new_medium": "PBS", "reason": "x"}
            ), 10)
        try:
            return await _connect(url, token, body)
        except BaseException:  # noqa: BLE001 - the server went away
            return "server gone"

    def person():
        if banner.pending_request_id and "first" not in seen:
            seen["first"] = True
            control.toggle_action.setChecked(False)       # answered No
            seen["still_on"] = control.url is not None
            seen["ticked"] = control.toggle_action.isChecked()
            control.toggle_action.setChecked(False)       # answered Yes
            seen["off"] = control.url is None

    _drive(client, person, limit_ms=30000)
    assert seen["still_on"] is True and seen["ticked"] is True
    assert seen["off"] is True
    assert len(asked) == 2 and "request_cell_switch" in asked[0]
    assert _spin_until(lambda: banner.pending_request_id is None, 5)


def test_without_the_packages_the_menu_item_is_off_and_says_why(window, monkeypatch) -> None:
    """Review round 1, finding 6: a missing mcp package must not stop the window."""
    import src.gui.mcp_controls as controls

    monkeypatch.setattr(controls, "McpToolServer", None)
    app_settings.set_mcp_server_enabled(True)
    control = controls.McpServerControl(
        window._agent_registry,  # noqa: SLF001
        session_log=None,
        take=lambda *a: None,
        give_back=lambda *a: None,
    )
    assert control.status_label.text() == "MCP: not installed"
    assert not control.toggle_action.isEnabled()
    assert "pip install -r requirements.txt" in control.toggle_action.toolTip()
    assert control.set_enabled(True) is False and control.url is None


# ---- the reviewer's probes (round 1), kept as tests ---------------------------------


def test_hostile_calls_are_refused_by_the_handlers_and_run_nothing(window) -> None:
    control, url, token = _start_window_server(window)
    engine = window._agent_engine  # noqa: SLF001
    started: list = []
    engine.measurement_started.connect(lambda *a: started.append(a))
    cases = [
        ("run_ca", {"channels": [1], "e_dc": "2.0", "t_run": 1}),
        ("run_ca", {"channels": [1], "e_dc": 1.0000001, "t_run": 1}),
        ("run_ca", {"channels": [1], "params": {"e_dc": 5}, "t_run": 1}),
        ("run_ca", {"channels": [0], "e_dc": 0.2, "t_run": 1}),
        ("run_ca", {"channels": [17], "e_dc": 0.2, "t_run": 1}),
        ("run_ca", {"channels": "1", "e_dc": 0.2, "t_run": 1}),
        ("run_ca", {"channels": [1], "e_dc": 0.2, "t_run": 1, "e_begin": 5}),
        ("run_cv", {"channels": [1], "e_vertex1": 3.0}),
        ("run_cv", {"electrode_setup": "..\\..\\x"}),
        ("run_ca", {"channels": [1], "e_dc": float("inf"), "t_run": 1}),
        ("", {}),
        ("__class__", {}),
    ]

    async def client():
        async def body(session):
            return [await session.call_tool(name, args) for name, args in cases]
        return await _connect(url, token, body)

    results = _drive(client, lambda: None)
    assert all(r.isError for r in results), [r.content[0].text[:80] for r in results]
    assert all(_json(r)["ok"] is False and _json(r)["error"] for r in results)
    QCoreApplication.processEvents()
    assert started == [] and not engine.isRunning()


def test_restarts_on_a_fixed_port_free_it_each_time(window) -> None:
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    control = window._mcp_control  # noqa: SLF001
    for _ in range(3):
        assert control.set_enabled(True, port=port), control._error  # noqa: SLF001
        control.set_enabled(False)
        free = socket.socket()
        free.bind(("127.0.0.1", port))          # raises if still held
        free.close()
    assert not [t for t in threading.enumerate() if t.name == "chatstat-mcp"]


def test_the_duration_limit_applies_over_mcp(window) -> None:
    app_settings.set_agent_max_duration_s(5.0)
    control, url, token = _start_window_server(window)

    async def client():
        return await _connect(url, token, lambda s: s.call_tool(
            "run_ca", {"channels": [1], "e_dc": 0.2, "t_run": 1e9}
        ))

    result = _drive(client, lambda: None)
    assert result.isError is True and _json(result)["refused_by"] == "request_limits"


def test_stopping_the_server_mid_run_aborts_the_run(window) -> None:
    control, url, token = _start_window_server(window)
    engine = window._agent_engine  # noqa: SLF001
    aborts: list = []
    original = engine.abort

    def spy(*args, **kwargs):
        aborts.append(threading.current_thread().name)
        return original(*args, **kwargs)

    engine.abort = spy
    state: dict = {}

    async def client():
        async def body(session):
            return await asyncio.wait_for(session.call_tool(
                "run_ca", {"channels": list(range(1, 17)), "e_dc": 0.2, "t_run": 3, "t_interval": 0.1}
            ), 10)
        try:
            return await _connect(url, token, body)
        except BaseException:  # noqa: BLE001 - the server went away
            return "server gone"

    def person():
        if engine.isRunning() and "stopped" not in state:
            began = time.monotonic()
            control.set_enabled(False)
            state["stopped"] = time.monotonic() - began

    _drive(client, person, limit_ms=30000)
    assert aborts, "the run was not aborted"
    assert state["stopped"] < 2.0
    assert _spin_until(lambda: not engine.isRunning(), 5)
    assert window._driver_lease.holder is None  # noqa: SLF001


def test_a_clean_client_exit_mid_wait_takes_the_banner_down(window) -> None:
    control, url, token = _start_window_server(window)
    banner = window._agent_panel.cell_switch_prompt  # noqa: SLF001
    seen: dict = {}

    async def client():
        async def body(session):
            task = asyncio.ensure_future(session.call_tool(
                "request_cell_switch", {"new_medium": "PBS", "reason": "x"}
            ))
            for _ in range(500):
                if await bridge.run_on_gui(lambda: banner.pending_request_id):
                    break
                await asyncio.sleep(0.01)
            task.cancel()          # the client gives up and closes its session
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
            return "left"
        return await _connect(url, token, body)

    def person():
        if banner.pending_request_id:
            seen["up"] = True

    assert _drive(client, person) == "left"
    assert seen.get("up") is True
    assert _spin_until(lambda: banner.pending_request_id is None, 10)


def test_a_client_killed_mid_run_aborts_the_run(window) -> None:
    control, url, token = _start_window_server(window)
    engine = window._agent_engine  # noqa: SLF001
    aborts: list = []
    original = engine.abort
    engine.abort = lambda *a, **k: (aborts.append(1), original(*a, **k))[1]
    helper = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "agent", "_mcp_client_process.py"
    )
    proc = subprocess.Popen(
        [sys.executable, helper, url, token, "run_ca",
         json.dumps({"channels": list(range(1, 17)), "e_dc": 0.2, "t_run": 3, "t_interval": 0.1})],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _spin_until(engine.isRunning, 30)
        proc.kill()
        proc.wait(10)
        assert _spin_until(lambda: bool(aborts), 10), "the run was not aborted"
        assert _spin_until(lambda: not engine.isRunning(), 10)
        assert _spin_until(lambda: window._driver_lease.holder is None, 5)  # noqa: SLF001
    finally:
        if proc.poll() is None:
            proc.kill()


def _spin_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


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

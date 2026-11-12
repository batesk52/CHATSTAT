"""The MCP server on its own: no Qt, no instrument, a small registry.

The server runs on its own thread (as in the window) on an ephemeral
127.0.0.1 port; the official MCP client connects over HTTP from the test's
own asyncio loop. The end-to-end test with the window and the mock engine is
tests/gui/test_mcp_server_window.py.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Optional

import httpx
import mcp.types as types
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.agent import mcp_server
from src.agent.agent_worker import DEFAULT_SYSTEM_PROMPT, INSTRUMENT_GUIDE
from src.agent.cell_switch import SYSTEM_PROMPT_RULE as CELL_RULE
from src.agent.memory_tools import MEMORY_RULE_BODY, SYSTEM_PROMPT_RULE as MEMORY_RULE
from src.agent.mcp_server import McpToolServer, build_instructions
from src.agent.session_log import SessionLog, read_events
from src.agent.tools import ToolRegistry

TOKEN = "t" * 43


def _schema(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "additionalProperties": False}


def _registry(calls: list, slow_s: float = 0.0) -> ToolRegistry:
    registry = ToolRegistry()

    async def echo(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(("echo", args))
        return {"ok": True, "echo": args}

    async def refuse(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(("refuse", args))
        return {"ok": False, "error": "Refused by the handler: exact text."}

    async def slow(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(("slow", args))
        await asyncio.sleep(slow_s)
        return {"ok": True}

    async def status(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "connected": True}

    registry.register({"name": "echo", "description": "Echo.", "input_schema": _schema(x={"type": "integer"})}, echo)
    registry.register({"name": "refuse", "description": "Refuse.", "input_schema": _schema()}, refuse)
    registry.register({"name": "slow", "description": "Slow.", "input_schema": _schema()}, slow)
    registry.register({"name": "device_status", "description": "Status.", "input_schema": _schema()}, status)
    return registry


async def _session(url: str, token: Optional[str], body):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0, read=60.0)) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                return await body(session, init)


@pytest.fixture
def served(tmp_path):
    """Start a server; yield a helper; always stop it."""
    servers: list[McpToolServer] = []

    def make(registry: ToolRegistry, **kwargs: Any) -> McpToolServer:
        server = McpToolServer(registry, TOKEN, **kwargs)
        server.start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


# ---- guidance from one source -----------------------------------------------------------


def test_the_in_app_prompt_is_built_from_the_shared_guide() -> None:
    assert DEFAULT_SYSTEM_PROMPT == "You are the embedded lab assistant inside " + INSTRUMENT_GUIDE
    assert MEMORY_RULE == MEMORY_RULE_BODY + " (the Save chat to memory button sends that request).\n"


def test_the_instructions_carry_the_wiring_check_and_rules_but_not_the_dock() -> None:
    registry = ToolRegistry()
    text = build_instructions(registry)
    assert INSTRUMENT_GUIDE in text
    assert "6. Wiring check." in text and "no value is always better than a wrong value" in text
    assert "embedded lab assistant" not in text
    assert CELL_RULE not in text and "Memory notes." not in text   # tools not registered

    registry.register({"name": "request_cell_switch", "description": "x", "input_schema": _schema()}, lambda a: {})
    registry.register({"name": "list_memories", "description": "x", "input_schema": _schema()}, lambda a: {})
    text = build_instructions(registry)
    assert CELL_RULE in text
    assert MEMORY_RULE_BODY + ".\n" in text
    assert "Save chat to memory button" not in text


# ---- the server over HTTP -----------------------------------------------------------------


def test_it_binds_localhost_only_and_is_off_until_started(tmp_path) -> None:
    server = McpToolServer(_registry([]), TOKEN)
    assert not server.running and server.url is None
    server.start()
    try:
        assert server.url == f"http://127.0.0.1:{server.port}/mcp"
        assert mcp_server.HOST == "127.0.0.1"
    finally:
        server.stop()
    assert not server.running and server.url is None


def test_a_request_without_the_token_is_refused(served) -> None:
    server = served(_registry([]))
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        response = httpx.post(
            server.url,
            headers={**headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert response.status_code == 401
        assert TOKEN not in response.text

    async def body(session, init):
        return init

    with pytest.raises(BaseException):
        asyncio.run(_session(server.url, "wrong-token", body))


def test_tools_are_served_from_the_registry_and_follow_it(served) -> None:
    calls: list = []
    registry = _registry(calls)
    server = served(registry)

    async def body(session, init):
        first = await session.list_tools()
        registry.register({"name": "late", "description": "Added later.", "input_schema": _schema()}, lambda a: {"ok": True})
        second = await session.list_tools()
        return init, first, second

    init, first, second = asyncio.run(_session(server.url, TOKEN, body))
    assert [t.name for t in first.tools] == [d["name"] for d in _registry([]).tool_defs]
    echo = next(t for t in first.tools if t.name == "echo")
    assert echo.inputSchema == registry.tool_defs[0]["input_schema"]
    assert sorted(t.name for t in second.tools) == registry.names()
    assert init.instructions == build_instructions(_registry([]))


def test_a_call_returns_the_same_json_the_in_app_agent_sees(served, tmp_path) -> None:
    calls: list = []
    log = SessionLog(log_dir=str(tmp_path))
    server = served(_registry(calls), session_log=log)

    async def body(session, init):
        ok = await session.call_tool("echo", {"x": 3})
        refused = await session.call_tool("refuse", {})
        wrong_type = await session.call_tool("echo", {"x": "not an int"})
        unknown = await session.call_tool("nope", {})
        return ok, refused, wrong_type, unknown

    ok, refused, wrong_type, unknown = asyncio.run(_session(server.url, TOKEN, body))
    assert ok.isError is False
    assert ok.content[0].text == '{"echo":{"x":3},"ok":true}'
    assert refused.isError is True
    assert json.loads(refused.content[0].text)["error"] == "Refused by the handler: exact text."
    # No schema check in front of the handler: it gets the call as in the app.
    assert wrong_type.isError is False and calls[-1] == ("echo", {"x": "not an int"})
    assert unknown.isError is True and "Unknown tool 'nope'" in unknown.content[0].text

    events = read_events(log.path)
    results = [e for e in events if e["event"] == "tool_result"]
    assert [e["name"] for e in results] == ["echo", "refuse", "echo", "nope"]
    assert all(e["source"] == "mcp" and e["client"] for e in results)
    assert all(e["id"].startswith("mcp-") for e in results)
    calls_logged = [e for e in events if e["event"] == "tool_call"]
    assert calls_logged[0]["arguments"] == {"x": 3} and calls_logged[0]["source"] == "mcp"
    assert TOKEN not in open(log.path, encoding="utf-8").read()


def test_the_driver_check_refuses_all_but_read_only_tools(served) -> None:
    calls: list = []
    message = "The in-app agent is working."

    async def busy() -> Optional[str]:
        return message

    server = served(_registry(calls), driver_check=busy)

    async def body(session, init):
        return await session.call_tool("echo", {"x": 1}), await session.call_tool("device_status", {})

    refused, status = asyncio.run(_session(server.url, TOKEN, body))
    assert refused.isError is True and json.loads(refused.content[0].text)["error"] == message
    assert not [c for c in calls if c[0] == "echo"]          # nothing ran
    assert status.isError is False


def test_a_long_call_sends_progress_notifications(served) -> None:
    server = served(_registry([], slow_s=0.7), progress_interval_s=0.1)
    seen: list = []

    async def on_progress(progress, total, message):
        seen.append((progress, message))

    async def body(session, init):
        return await session.call_tool("slow", {}, progress_callback=on_progress)

    result = asyncio.run(_session(server.url, TOKEN, body))
    assert result.isError is False
    assert len(seen) >= 3
    assert all("slow still running" in m for _, m in seen)
    assert [p for p, _ in seen] == sorted(p for p, _ in seen)


def test_the_prompt_carries_the_same_guidance(served) -> None:
    registry = _registry([])
    server = served(registry)

    async def body(session, init):
        listed = await session.list_prompts()
        got = await session.get_prompt(mcp_server.PROMPT_NAME)
        return init, listed, got

    init, listed, got = asyncio.run(_session(server.url, TOKEN, body))
    assert [p.name for p in listed.prompts] == [mcp_server.PROMPT_NAME]
    assert got.messages[0].content.text == init.instructions == build_instructions(registry)


def test_a_cancelled_call_cancels_the_handler_and_is_logged(served, tmp_path) -> None:
    running = threading.Event()   # set on the server thread
    registry = ToolRegistry()
    seen: list = []

    async def wait_forever(args):
        running.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            seen.append("handler cancelled")
            raise

    registry.register({"name": "wait", "description": "Wait.", "input_schema": _schema()}, wait_forever)
    log = SessionLog(log_dir=str(tmp_path))
    server = served(registry, session_log=log)

    async def body(session, init):
        task = asyncio.ensure_future(session.call_tool("wait", {}))
        for _ in range(400):                    # until the handler runs
            if running.is_set():
                break
            await asyncio.sleep(0.025)
        assert running.is_set(), "the call never reached the handler"
        # What a client sends when its user stops a call (the SDK's client
        # does not send it by itself when the awaiting task is cancelled).
        request_id = session._request_id - 1  # noqa: SLF001 - the call's id
        await session.send_notification(
            types.ClientNotification(
                types.CancelledNotification(
                    params=types.CancelledNotificationParams(
                        requestId=request_id, reason="stopped by the user"
                    )
                )
            )
        )
        try:
            await asyncio.wait_for(task, 10)
        except Exception:  # noqa: BLE001 - the server answers with an error
            pass
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.05)
        return None

    asyncio.run(_session(server.url, TOKEN, body))
    assert seen == ["handler cancelled"]
    results = [e for e in read_events(log.path) if e["event"] == "tool_result"]
    assert results and results[-1]["cancelled"] is True and results[-1]["source"] == "mcp"


def test_a_port_in_use_is_reported(served) -> None:
    first = served(_registry([]))
    second = McpToolServer(_registry([]), TOKEN, port=first.port)
    with pytest.raises(OSError):
        second.start()
    assert not second.running


def test_a_token_is_required() -> None:
    with pytest.raises(ValueError):
        McpToolServer(ToolRegistry(), "")

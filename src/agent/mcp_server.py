"""MCP server inside the running ChatStat window.

An outside MCP client (Claude Code, or Claude Desktop through a bridge) can
call the SAME tools the in-app agent calls. Nothing here defines a tool: the
server lists ``registry.tool_defs`` on every request and runs each call
through :func:`src.agent.tools.dispatch_tool`, the in-app agent's own path,
so the request limits, the wiring rules, the electrode setups by name and
every refusal message apply unchanged.

Design (architecture.md, "MCP server"):

* **Transport.** Streamable HTTP at ``http://127.0.0.1:<port>/mcp``, served
  by uvicorn on a thread of its own with its own asyncio loop, the way the
  in-app agent has its own loop. The socket is bound to 127.0.0.1 only.
* **Access token.** Every request must carry ``Authorization: Bearer
  <token>``; anything else gets 401. The Host and Origin headers are also
  checked (DNS rebinding protection), so a web page cannot reach the server.
* **Threads.** Tool handlers already reach the GUI and the engine only
  through ``bridge.run_on_gui`` and thread-safe engine calls, and they work
  from any asyncio loop, so they run here unchanged.
* **One driver.** Before any tool that is not read-only, ``driver_check``
  is awaited; the window refuses while its own agent is in a turn. A run
  while a measurement is running is refused by the engine's own busy check.
* **Long calls.** While a call runs, a progress notification goes out every
  ``progress_interval_s`` seconds when the client sent a progress token.
* **Session log.** Every call goes to the same session log as the in-app
  agent's, marked ``"source": "mcp"`` with the client's name.

All third-party imports are at module top, before any asyncio loop starts
(see CLAUDE.md, "Eager native imports").
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import socket
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from src.agent.agent_worker import INSTRUMENT_GUIDE
from src.agent.cell_switch import SYSTEM_PROMPT_RULE as CELL_SWITCH_RULE
from src.agent.cell_switch import TOOL_NAME as CELL_SWITCH_TOOL
from src.agent.memory_tools import LIST_TOOL_NAME, MEMORY_RULE_BODY
from src.agent.session_log import SessionLog
from src.agent.tools import ToolRegistry, dispatch_tool

logger = logging.getLogger(__name__)

__all__ = [
    "HOST",
    "MCP_PATH",
    "PROMPT_NAME",
    "READ_ONLY_TOOLS",
    "McpToolServer",
    "build_instructions",
]

HOST = "127.0.0.1"
MCP_PATH = "/mcp"
SERVER_NAME = "chatstat"
#: The MCP prompt that carries the same guidance as the server instructions,
#: for clients that do not show server instructions to the model.
PROMPT_NAME = "chatstat_rules"
#: Seconds between progress notifications while a call runs.
PROGRESS_INTERVAL_S = 10.0
#: Tools that only read. They answer even while the in-app agent works.
READ_ONLY_TOOLS = frozenset(
    {"device_status", "list_ports", "list_memories", "read_memory"}
)

#: What an outside client needs to know that the in-app agent does not.
MCP_CLIENT_NOTE = (
    "\nOver MCP. You reach these tools from outside the ChatStat window. A "
    "person may be at the window, and its own in-app agent may be working. "
    "One driver at a time: while the in-app agent is in a turn, every tool "
    "except device_status, list_ports, list_memories and read_memory is "
    "refused and nothing is run; wait and try again, or ask the person. A "
    "run while any measurement is running is refused ('Engine is busy'). "
    "Do not call abort_measurement to clear that unless the person asked "
    "you to stop that run: it may be the person's own run. A call can take "
    "many minutes (an impedance sweep, or request_cell_switch, which waits "
    "for the person to press Continue or Cancel in the window); the server "
    "sends a progress notification every 10 s while a call runs. Plots and "
    "analysis figures appear in the window, not in your results.\n"
)

#: ``driver_check()`` returns a refusal message, or None to go ahead.
DriverCheck = Callable[[], Awaitable[Optional[str]]]
#: ``on_activity(tool_name, status)``; status is "running", "done",
#: "error" or "cancelled". Called on the server thread.
ActivityCallback = Callable[[str, str], None]


def build_instructions(registry: ToolRegistry) -> str:
    """The guidance an MCP client gets, from the in-app prompt's own text.

    The in-app prompt is ``"You are the embedded lab assistant inside " +
    INSTRUMENT_GUIDE`` plus the cell-switch and memory rules when those
    tools are registered. Here the dock-specific opening words and the
    memory rule's mention of the dock's button are left out, and a note for
    outside clients is added.
    """
    text = "These tools run inside ChatStat, " + INSTRUMENT_GUIDE
    if CELL_SWITCH_TOOL in registry:
        text += CELL_SWITCH_RULE
    if LIST_TOOL_NAME in registry:
        text += MEMORY_RULE_BODY + ".\n"
    return text + MCP_CLIENT_NOTE


def _dumps(obj: Any) -> str:
    """Compact JSON in the same form as ``tools.dispatch_tool`` results."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


async def _plain_response(send: Any, status: int, text: str) -> None:
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="chatstat"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": text.encode("utf-8")})


class McpToolServer:
    """Serves one :class:`ToolRegistry` over MCP on 127.0.0.1.

    Create it on the GUI thread, call :meth:`start` to serve and
    :meth:`stop` to end. A stopped server can be started again.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        token: str,
        *,
        port: int = 0,
        session_log: Optional[SessionLog] = None,
        driver_check: Optional[DriverCheck] = None,
        on_activity: Optional[ActivityCallback] = None,
        progress_interval_s: float = PROGRESS_INTERVAL_S,
        version: str = "",
    ) -> None:
        """Configure the server (nothing is bound or started here).

        Args:
            registry: The in-app agent's registry, served as it is.
            token: The access token clients must send as a Bearer token.
            port: TCP port on 127.0.0.1; 0 picks a free one (tests).
            session_log: The in-app agent's session log. The token is
                registered with it as a secret.
            driver_check: Awaited before every tool that is not read-only.
            on_activity: Told about each call (for the window's status).
            progress_interval_s: Seconds between progress notifications.
            version: Application version reported to clients.
        """
        if not token:
            raise ValueError("An access token is required.")
        self._registry = registry
        self._token = token
        self._port_wanted = int(port)
        self._log = session_log
        if session_log is not None:
            session_log.add_secret(token)
        self._driver_check = driver_check
        self._on_activity = on_activity
        self._interval = float(progress_interval_s)
        self._version = version
        self._port: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._uvicorn: Optional[uvicorn.Server] = None

    # ---- state ----------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while the server thread is serving."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def port(self) -> Optional[int]:
        """The bound port while running, else None."""
        return self._port if self.running else None

    @property
    def url(self) -> Optional[str]:
        """``http://127.0.0.1:<port>/mcp`` while running, else None."""
        port = self.port
        return None if port is None else f"http://{HOST}:{port}{MCP_PATH}"

    # ---- lifecycle ------------------------------------------------------

    def start(self, timeout_s: float = 10.0) -> None:
        """Bind 127.0.0.1:<port> and serve on a new thread.

        Raises:
            OSError: The port could not be bound (for example in use).
            RuntimeError: The server did not come up within ``timeout_s``.
        """
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((HOST, self._port_wanted))
            sock.listen(16)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._port = int(sock.getsockname()[1])
        app, manager = self._build_app()
        config = uvicorn.Config(
            app,
            host=HOST,
            port=self._port,
            lifespan="off",
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=1,
        )
        server = uvicorn.Server(config)
        self._uvicorn = server

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _main() -> None:
                async with manager.run():
                    await server.serve(sockets=[sock])

            try:
                loop.run_until_complete(_main())
            except BaseException:  # noqa: BLE001 - never kill the thread loudly
                logger.exception("MCP server stopped with an error.")
            finally:
                try:
                    # Streams left open by clients still have tasks here.
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("MCP server task cleanup failed.")
                try:
                    loop.close()
                finally:
                    asyncio.set_event_loop(None)
                    sock.close()

        thread = threading.Thread(target=_run, name="chatstat-mcp", daemon=True)
        self._thread = thread
        thread.start()
        deadline = time.monotonic() + timeout_s
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("The MCP server did not start.")
            time.sleep(0.02)
        logger.info("MCP server listening on %s", self.url)

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop serving and wait for the thread (idempotent).

        Calls still running are cancelled the way the Stop button cancels
        an in-app turn: a measurement is aborted, a cell-switch banner is
        taken down.
        """
        server, thread = self._uvicorn, self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
            if thread.is_alive():
                logger.warning("MCP server thread did not stop in %.0f s.", timeout_s)
        if thread is None or not thread.is_alive():
            self._thread = None
            self._uvicorn = None
        logger.info("MCP server stopped.")

    # ---- the ASGI app ---------------------------------------------------

    def _authorized(self, scope: dict[str, Any]) -> bool:
        expected = f"Bearer {self._token}".encode("utf-8")
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization":
                return hmac.compare_digest(value.strip(), expected)
        return False

    def _build_app(self) -> tuple[Callable[..., Awaitable[None]], StreamableHTTPSessionManager]:
        server = self._build_mcp_server()
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{HOST}:*", "localhost:*"],
            allowed_origins=[f"http://{HOST}:*", "http://localhost:*"],
        )
        manager = StreamableHTTPSessionManager(
            app=server, security_settings=security
        )

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") != "http":
                return
            if str(scope.get("path") or "").rstrip("/") != MCP_PATH:
                await _plain_response(send, 404, "Not found. The endpoint is /mcp.")
                return
            if not self._authorized(scope):
                # The header is never logged: it carries the token.
                logger.warning("MCP request without a valid access token refused.")
                await _plain_response(send, 401, "A valid access token is required.")
                return
            await manager.handle_request(scope, receive, send)

        return app, manager

    def _build_mcp_server(self) -> Server:
        instructions = build_instructions(self._registry)
        server: Server = Server(
            SERVER_NAME, version=self._version or None, instructions=instructions
        )

        @server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            # Read on every request: a tool registered later is served too.
            return [
                types.Tool(
                    name=d["name"],
                    description=d["description"],
                    inputSchema=d["input_schema"],
                )
                for d in self._registry.tool_defs
            ]

        # validate_input=False: the handlers check their own input and give
        # the same refusal messages as in the app; a schema check here would
        # answer some calls differently.
        @server.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await self._call(server, name, arguments or {})

        @server.list_prompts()
        async def _list_prompts() -> list[types.Prompt]:
            return [
                types.Prompt(
                    name=PROMPT_NAME,
                    description=(
                        "How to use the ChatStat tools safely: the wiring "
                        "check and the operating rules the in-app agent follows."
                    ),
                )
            ]

        @server.get_prompt()
        async def _get_prompt(
            name: str, arguments: Optional[dict[str, str]]
        ) -> types.GetPromptResult:
            if name != PROMPT_NAME:
                raise ValueError(f"Unknown prompt {name!r}; the prompt is {PROMPT_NAME!r}.")
            return types.GetPromptResult(
                description="ChatStat operating rules",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text=instructions),
                    )
                ],
            )

        return server

    # ---- one tool call --------------------------------------------------

    def _activity(self, name: str, status: str) -> None:
        if self._on_activity is None:
            return
        try:
            self._on_activity(name, status)
        except Exception:  # noqa: BLE001 - status display must not fail a call
            logger.exception("MCP activity callback failed.")

    async def _call(
        self, server: Server, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        ctx = server.request_context
        source: dict[str, Any] = {"source": "mcp"}
        client = getattr(
            getattr(getattr(ctx.session, "client_params", None), "clientInfo", None),
            "name",
            None,
        )
        if client:
            source["client"] = str(client)
        call_id = f"mcp-{uuid.uuid4().hex[:12]}"
        log = self._log
        if log is not None:
            if not log.started:
                log.start("mcp-client", self._registry.names())
            log.tool_call(call_id, name, arguments, source=source)
        self._activity(name, "running")
        started = time.monotonic()

        refusal: Optional[str] = None
        if name not in READ_ONLY_TOOLS and self._driver_check is not None:
            try:
                refusal = await self._driver_check()
            except Exception as exc:  # noqa: BLE001 - refuse rather than guess
                logger.exception("MCP driver check failed.")
                refusal = f"Could not check whether the window is busy ({exc}); nothing was run."

        try:
            if refusal is not None:
                result_json, is_error = _dumps({"ok": False, "error": refusal}), True
            else:
                result_json, is_error = await self._dispatch_with_progress(
                    ctx, name, arguments, started
                )
        except asyncio.CancelledError:
            message = (
                "Cancelled by the MCP client before completion. Any running "
                "measurement was aborted."
            )
            if log is not None:
                log.tool_result(
                    call_id,
                    name,
                    _dumps({"ok": False, "error": message}),
                    is_error=True,
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    cancelled=True,
                    source=source,
                )
            self._activity(name, "cancelled")
            raise

        if log is not None:
            log.tool_result(
                call_id,
                name,
                result_json,
                is_error=is_error,
                duration_ms=(time.monotonic() - started) * 1000.0,
                source=source,
            )
        self._activity(name, "error" if is_error else "done")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result_json)],
            isError=is_error,
        )

    async def _dispatch_with_progress(
        self, ctx: Any, name: str, arguments: dict[str, Any], started: float
    ) -> tuple[str, bool]:
        """Run the call; send a progress notification every interval."""
        meta = getattr(ctx, "meta", None)
        progress_token = getattr(meta, "progressToken", None) if meta else None
        task = asyncio.ensure_future(dispatch_tool(self._registry, name, arguments))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self._interval)
                if done:
                    return task.result()
                if progress_token is None:
                    continue
                elapsed = time.monotonic() - started
                try:
                    await ctx.session.send_progress_notification(
                        progress_token,
                        round(elapsed, 1),
                        None,
                        f"{name} still running ({elapsed:.0f} s)",
                        related_request_id=str(ctx.request_id),
                    )
                except Exception:  # noqa: BLE001 - progress is a courtesy
                    logger.debug("Progress notification failed.", exc_info=True)
        except asyncio.CancelledError:
            # The handler's own CancelledError path aborts a measurement or
            # takes the cell-switch banner down, as the Stop button does.
            task.cancel()
            raise

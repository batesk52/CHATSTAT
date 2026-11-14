"""MCP server inside the running ChatStat window.

An outside MCP client (Claude Code, or Claude Desktop through a bridge) can
call the SAME tools the in-app agent calls. Nothing here defines a tool: the
server lists ``registry.tool_defs`` on every request and runs each call
through :func:`src.agent.tools.dispatch_tool`, the in-app agent's own path,
so the request limits, the wiring rules, the electrode setups by name and
every refusal message apply unchanged.

Design (architecture.md, "MCP server inside the window"):

* **Transport.** Streamable HTTP at ``http://127.0.0.1:<port>/mcp``, served
  by uvicorn on a thread of its own with its own asyncio loop, the way the
  in-app agent has its own loop. The socket is bound to 127.0.0.1 only.
  ``mcp_http.guarded_app`` checks the path and the access token.
* **Threads.** Tool handlers reach the GUI and the engine only through
  ``bridge.run_on_gui`` and thread-safe engine calls, and they work from
  any asyncio loop, so they run here unchanged.
* **One driver.** A call to a tool that is not read-only runs only if no
  other MCP call is running (an ``asyncio.Lock`` on this loop, tried, never
  waited on) and ``acquire`` takes the window's driver lease (checked and
  taken in one GUI-thread call). The lease is held for the whole call and
  given back with ``release`` when it ends, however it ends.
* **Long calls.** While a call runs, a progress notification goes out every
  ``progress_interval_s`` seconds when the client sent a progress token. A
  call whose client connection goes away is stopped the way the dock's
  Stop button stops a turn.
* **Session log.** Every call goes to the same session log as the in-app
  agent's, marked ``"source": "mcp"`` with the client's name; lines the
  tools write themselves during the call are marked the same way.

All third-party imports are at module top, before any asyncio loop starts
(see CLAUDE.md, "Eager native imports").
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from typing import Optional

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from src.agent.mcp_calls import (
    ANOTHER_MCP_CALL,
    CLIENT_GONE,
    ActivityCallback,
    DriverAcquire,
    DriverRelease,
    ToolCallRunner,
)
from src.agent.mcp_guidance import PROMPT_NAME, READ_ONLY_TOOLS, build_instructions
from src.agent.mcp_http import MCP_PATH, guarded_app
from src.agent.mcp_protocol import build_mcp_server
from src.agent.session_log import SessionLog
from src.agent.tools import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "ANOTHER_MCP_CALL",
    "CLIENT_GONE",
    "HOST",
    "MCP_PATH",
    "PROMPT_NAME",
    "READ_ONLY_TOOLS",
    "McpToolServer",
    "build_instructions",
]

HOST = "127.0.0.1"
#: Seconds between progress notifications while a call runs.
PROGRESS_INTERVAL_S = 10.0


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
        acquire: Optional[DriverAcquire] = None,
        release: Optional[DriverRelease] = None,
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
            acquire: Takes the window's driver lease before every tool that
                is not read-only (see :data:`DriverAcquire`).
            release: Gives the lease back when that call ends.
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
        self._calls = ToolCallRunner(
            registry,
            session_log=session_log,
            acquire=acquire,
            release=release,
            on_activity=on_activity,
            progress_interval_s=progress_interval_s,
        )
        self._version = version
        self._port: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._uvicorn: Optional[uvicorn.Server] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown: Optional[asyncio.Event] = None

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

    def running_calls(self) -> list[str]:
        """Names of the tools whose calls are running now (any thread)."""
        return self._calls.running_calls()

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
        server_mcp = build_mcp_server(self._registry, self._calls, self._version)
        manager = StreamableHTTPSessionManager(
            app=server_mcp,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"{HOST}:*", "localhost:*"],
                allowed_origins=[f"http://{HOST}:*", "http://localhost:*"],
            ),
        )
        config = uvicorn.Config(
            guarded_app(manager.handle_request, self._token),
            host=HOST,
            port=self._port,
            lifespan="off",
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(config)
        self._uvicorn = server
        ready = threading.Event()

        async def _main() -> None:
            self._loop = asyncio.get_running_loop()
            self._shutdown = asyncio.Event()
            self._calls.prepare_loop()
            ready.set()
            async with manager.run():
                serving = asyncio.ensure_future(server.serve(sockets=[sock]))
                stopping = asyncio.ensure_future(self._shutdown.wait())
                await asyncio.wait(
                    {serving, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                stopping.cancel()
                # Leaving this block ends every MCP session first: calls
                # still running are cancelled (Stop semantics) and their
                # response streams close, so uvicorn then has nothing left
                # to wait for and shuts down at once.
            server.should_exit = True
            await serving

        def _run() -> None:
            # A selector loop, not Windows' default proactor: the proactor
            # can fail to report a connection a client reset, which leaves
            # uvicorn waiting for it on shutdown. Sockets are all this loop
            # serves, which the selector loop handles on every platform.
            loop = asyncio.SelectorEventLoop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_main())
            except BaseException:  # noqa: BLE001 - never kill the thread loudly
                logger.exception("MCP server stopped with an error.")
            finally:
                try:
                    # Streams left open by clients may still have tasks here.
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
        while not (ready.is_set() and server.started):
            if not thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("The MCP server did not start.")
            time.sleep(0.02)
        logger.info("MCP server listening on %s", self.url)

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop serving and wait for the thread (idempotent).

        Calls still running are cancelled the way the Stop button cancels
        an in-app turn: a measurement is aborted, a cell-switch banner is
        taken down, and the driver lease is given back.
        """
        server, thread = self._uvicorn, self._thread
        loop, shutdown = self._loop, self._shutdown
        if loop is not None and shutdown is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(shutdown.set)
            except RuntimeError:  # loop closed in the meantime
                pass
        elif server is not None:
            server.should_exit = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
            if thread.is_alive():
                logger.warning("MCP server thread did not stop in %.0f s.", timeout_s)
        if thread is None or not thread.is_alive():
            self._thread = None
            self._uvicorn = None
            self._loop = None
            self._shutdown = None
            self._calls.clear()
        logger.info("MCP server stopped.")

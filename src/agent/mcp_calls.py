"""One MCP tool call, from the request to the logged result.

:class:`ToolCallRunner` is what the MCP server does with each ``tools/call``:

1. Log the call in the in-app agent's session log, marked as MCP.
2. For a tool that is not read-only, become THE driver: no other MCP call
   may be running (an ``asyncio.Lock`` on the server loop, tried and never
   waited on), and ``acquire`` must take the window's driver lease (checked
   and taken in one GUI-thread call). Otherwise the call is refused and
   nothing runs.
3. Run the tool through ``tools.dispatch_tool``, the in-app agent's path,
   sending a progress notification every interval, and stopping the call
   (Stop semantics) if the client's connection goes away.
4. Give the lease back, however the call ended, and log the result.

Everything here runs on the server's event loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import mcp.types as types

from src.agent.mcp_guidance import READ_ONLY_TOOLS
from src.agent.mcp_http import DISCONNECT_KEY
from src.agent.session_log import CALL_SOURCE, SessionLog
from src.agent.tools import ToolRegistry, dispatch_tool

logger = logging.getLogger(__name__)

__all__ = [
    "ANOTHER_MCP_CALL",
    "CLIENT_GONE",
    "ActivityCallback",
    "DriverAcquire",
    "DriverRelease",
    "ToolCallRunner",
]

#: Refusal while another MCP call is running.
ANOTHER_MCP_CALL = (
    "Another MCP call is driving the instrument. One driver at a time: "
    "nothing was run. Wait until it has finished and try again."
)
#: Logged (nobody receives it) when the client's connection closes mid-call.
CLIENT_GONE = (
    "The MCP client's connection closed during the call. The call was "
    "stopped: any running measurement was aborted and a cell-switch prompt "
    "was taken down."
)
_CANCELLED = (
    "Cancelled by the MCP client before completion. Any running measurement "
    "was aborted."
)

#: ``acquire(tool, call_id, client)`` takes the driver lease and returns
#: None, or returns a refusal message. Awaited on the server loop.
DriverAcquire = Callable[[str, str, Optional[str]], Awaitable[Optional[str]]]
#: ``release(call_id)`` gives the lease back. Called on the server thread,
#: also during a cancellation, so it must not block or wait.
DriverRelease = Callable[[str], None]
#: ``on_activity(tool_name, status)``; status is "running", "done",
#: "error" or "cancelled". Called on the server thread.
ActivityCallback = Callable[[str, str], None]


class _ClientGone(Exception):
    """The client's connection closed while its call ran."""


def _dumps(obj: Any) -> str:
    """Compact JSON in the same form as ``tools.dispatch_tool`` results."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_dumps({"ok": False, "error": message}))],
        isError=True,
    )


class ToolCallRunner:
    """Runs MCP tool calls against one registry (server loop only)."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        session_log: Optional[SessionLog],
        acquire: Optional[DriverAcquire],
        release: Optional[DriverRelease],
        on_activity: Optional[ActivityCallback],
        progress_interval_s: float,
    ) -> None:
        self._registry = registry
        self._log = session_log
        self._acquire = acquire
        self._release = release
        self._on_activity = on_activity
        self._interval = float(progress_interval_s)
        self._driving_lock: Optional[asyncio.Lock] = None
        self._running_lock = threading.Lock()
        self._running: dict[str, str] = {}

    def prepare_loop(self) -> None:
        """Make the loop-bound lock; call on the server loop at start."""
        self._driving_lock = asyncio.Lock()

    def running_calls(self) -> list[str]:
        """Names of the tools whose calls are running now (any thread)."""
        with self._running_lock:
            return list(self._running.values())

    def clear(self) -> None:
        """Forget running calls (after the server thread has ended)."""
        with self._running_lock:
            self._running.clear()

    # ---- helpers ----------------------------------------------------------

    def _activity(self, name: str, status: str) -> None:
        if self._on_activity is None:
            return
        try:
            self._on_activity(name, status)
        except Exception:  # noqa: BLE001 - status display must not fail a call
            logger.exception("MCP activity callback failed.")

    def _log_result(
        self, call_id: str, name: str, result_json: str, is_error: bool,
        started: float, source: dict[str, Any], cancelled: bool = False,
    ) -> None:
        if self._log is None:
            return
        self._log.tool_result(
            call_id,
            name,
            result_json,
            is_error=is_error,
            duration_ms=(time.monotonic() - started) * 1000.0,
            cancelled=cancelled,
            source=source,
        )

    async def _take_driver(self, name: str, call_id: str, client: Optional[str]) -> Optional[str]:
        """Take this loop's lock and the window's lease, or say why not."""
        lock = self._driving_lock
        if lock is not None:
            if lock.locked():
                return ANOTHER_MCP_CALL
            await lock.acquire()      # free: returns at once
        if self._acquire is None:
            return None
        try:
            refusal = await self._acquire(name, call_id, client)
        except asyncio.CancelledError:
            # The GUI may have taken the lease before the cancel landed:
            # give it back (a release by a non-holder is ignored).
            self._give_back_driver(call_id)
            raise
        except Exception as exc:  # noqa: BLE001 - refuse rather than guess
            # The GUI may have taken the lease before the error: give it
            # back, and the lock with it (a release by a non-holder is
            # ignored).
            logger.exception("MCP driver check failed.")
            self._give_back_driver(call_id)
            return f"Could not check whether the window is busy ({exc}); nothing was run."
        if refusal is not None and lock is not None:
            lock.release()
        return refusal

    def _give_back_driver(self, call_id: str) -> None:
        if self._release is not None:
            try:
                self._release(call_id)
            except Exception:  # noqa: BLE001 - must never fail the unwinding
                logger.exception("MCP driver release failed.")
        lock = self._driving_lock
        if lock is not None and lock.locked():
            lock.release()

    # ---- the call ---------------------------------------------------------

    async def call(self, ctx: Any, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Run one ``tools/call`` (``ctx`` is the SDK's request context)."""
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
                # No model is known here; the in-app agent notes its own
                # model when it first writes to this log.
                log.start(None, self._registry.names())
            log.tool_call(call_id, name, arguments, source=source)
        self._activity(name, "running")
        started = time.monotonic()
        marker = CALL_SOURCE.set(source)
        driving = False
        try:
            refusal: Optional[str] = None
            if name not in READ_ONLY_TOOLS:
                refusal = await self._take_driver(name, call_id, client)
                driving = refusal is None
            if refusal is not None:
                result_json, is_error = _dumps({"ok": False, "error": refusal}), True
            else:
                with self._running_lock:
                    self._running[call_id] = name
                result_json, is_error = await self._dispatch_with_progress(
                    ctx, name, arguments, started
                )
        except _ClientGone:
            self._log_result(
                call_id, name, _dumps({"ok": False, "error": CLIENT_GONE}), True,
                started, source, cancelled=True,
            )
            self._activity(name, "cancelled")
            return _error_result(CLIENT_GONE)
        except asyncio.CancelledError:
            self._log_result(
                call_id, name, _dumps({"ok": False, "error": _CANCELLED}), True,
                started, source, cancelled=True,
            )
            self._activity(name, "cancelled")
            raise
        finally:
            with self._running_lock:
                self._running.pop(call_id, None)
            if driving:
                self._give_back_driver(call_id)
            CALL_SOURCE.reset(marker)

        self._log_result(call_id, name, result_json, is_error, started, source)
        self._activity(name, "error" if is_error else "done")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result_json)],
            isError=is_error,
        )

    async def _dispatch_with_progress(
        self, ctx: Any, name: str, arguments: dict[str, Any], started: float
    ) -> tuple[str, bool]:
        """Run the call; send progress; stop it if the client goes away."""
        meta = getattr(ctx, "meta", None)
        progress_token = getattr(meta, "progressToken", None) if meta else None
        scope = getattr(getattr(ctx, "request", None), "scope", None)
        gone = scope.get(DISCONNECT_KEY) if isinstance(scope, dict) else None
        task = asyncio.ensure_future(dispatch_tool(self._registry, name, arguments))
        gone_wait = asyncio.ensure_future(gone.wait()) if gone is not None else None
        waiters = {task} | ({gone_wait} if gone_wait is not None else set())
        try:
            while True:
                done, _ = await asyncio.wait(
                    waiters, timeout=self._interval, return_when=asyncio.FIRST_COMPLETED
                )
                if task in done:
                    return task.result()
                if gone_wait is not None and gone_wait in done:
                    # Nobody is waiting for the answer: stop the call the way
                    # Stop does (abort the run, take the banner down).
                    task.cancel()
                    await asyncio.wait({task}, timeout=5.0)
                    raise _ClientGone()
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
        finally:
            if gone_wait is not None:
                gone_wait.cancel()

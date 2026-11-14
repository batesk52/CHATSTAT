"""The HTTP side of the MCP server: access token, path, lost connections.

:func:`guarded_app` wraps the SDK's session manager in a plain ASGI app that

* answers 404 for any path but ``/mcp``;
* answers 401 unless ``Authorization`` carries the access token (the scheme
  name ``Bearer`` in any case, the token compared in constant time);
* puts an :class:`asyncio.Event` into each request's ASGI scope under
  :data:`DISCONNECT_KEY` and sets it when the server sees the client's
  connection go away (``http.disconnect``). A tool call reads it from the
  request the SDK hands the handler, so a call whose client died (killed,
  crashed, network gone) is stopped instead of waiting for nobody.

No Qt and no tool code here.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = ["DISCONNECT_KEY", "MCP_PATH", "authorized", "guarded_app"]

MCP_PATH = "/mcp"
#: ASGI scope key of the request's "client went away" event.
DISCONNECT_KEY = "chatstat.client_disconnected"

Asgi = Callable[[dict[str, Any], Any, Any], Awaitable[None]]


def authorized(scope: dict[str, Any], token: str) -> bool:
    """True when the request carries ``Authorization: Bearer <token>``.

    The scheme name is matched in any case (HTTP auth schemes are
    case-insensitive); the token is compared with ``hmac.compare_digest``.
    """
    expected = token.encode("utf-8")
    for key, value in scope.get("headers") or []:
        if key.lower() != b"authorization":
            continue
        parts = value.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != b"bearer":
            return False
        return hmac.compare_digest(parts[1].strip(), expected)
    return False


async def _plain_response(send: Any, status: int, text: str) -> None:
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="chatstat"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": text.encode("utf-8")})


def guarded_app(handle: Asgi, token: str) -> Asgi:
    """The ASGI app: path and token checks, then *handle* (the SDK)."""

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        if str(scope.get("path") or "").rstrip("/") != MCP_PATH:
            await _plain_response(send, 404, "Not found. The endpoint is /mcp.")
            return
        if not authorized(scope, token):
            # The header is never logged: it carries the token.
            logger.warning("MCP request without a valid access token refused.")
            await _plain_response(send, 401, "A valid access token is required.")
            return
        gone = asyncio.Event()
        scope[DISCONNECT_KEY] = gone

        async def watched_receive() -> Any:
            message = await receive()
            if message.get("type") == "http.disconnect":
                gone.set()
            return message

        try:
            await handle(scope, watched_receive, send)
        finally:
            # The response is over (sent, or the connection is gone): a call
            # still waiting on this request has nobody to answer.
            gone.set()

    return app

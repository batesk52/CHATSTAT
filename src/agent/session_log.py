"""Per-session agent log: one JSON Lines file per agent conversation.

Every event (session start, user message, assistant message, tool call,
tool result, error, session end) is one JSON object on its own line,
written and flushed as it happens, so a crash mid-run still leaves a
readable file. Files go to ``<USER_DATA_DIR>/agent_sessions/`` and are
named ``YYYYMMDD_HHMMSS_<short id>.jsonl``.

The log never holds the API key or request headers. The worker passes
none of them in, and every line is also scrubbed before it is written:
any registered secret and anything shaped like an Anthropic key
(``sk-ant-...``) is replaced with ``[REDACTED]``.

All writes happen on the agent thread; a lock still guards the file so
an app-close ``end()`` from another thread cannot interleave a line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from src.data.paths import USER_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = ["SESSION_LOG_DIR", "SessionLog", "read_events"]

#: Default folder for session logs, beside the presets and app settings.
SESSION_LOG_DIR = os.path.join(USER_DATA_DIR, "agent_sessions")

#: Anything shaped like an Anthropic API key.
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_REDACTED = "[REDACTED]"
#: Registered secrets shorter than this are not scrubbed (too likely to
#: match ordinary text).
_MIN_SECRET_LEN = 8


def _now() -> str:
    """Local time with UTC offset, millisecond precision."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _as_json_value(value: Any) -> Any:
    """Parse a JSON string tool result back to an object when possible."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def read_events(path: str) -> list[dict[str, Any]]:
    """Read a session log, skipping a torn last line after a crash.

    Args:
        path: The ``.jsonl`` file.

    Returns:
        The events in file order.
    """
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


class SessionLog:
    """Writer for one agent conversation's JSON Lines log.

    The file is created lazily by :meth:`start`, so an app launch that
    never uses the agent leaves no file behind.
    """

    def __init__(
        self,
        log_dir: Optional[str] = None,
        *,
        app_version: str = "",
        mode: str = "hardware",
        port_fn: Optional[Callable[[], Optional[str]]] = None,
        provenance_fn: Optional[Callable[[], dict[str, Any]]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """Configure the log (no file is created here).

        Args:
            log_dir: Folder for the file; default :data:`SESSION_LOG_DIR`.
            app_version: Application version for the start event.
            mode: ``"mock"`` or ``"hardware"``.
            port_fn: Returns the connected port, or None when not
                connected. Called at session start.
            provenance_fn: Returns ``{"scripts_sent": int,
                "script_digest": str | None}`` for the connection the
                agent drives. Read before and after each tool call; a
                change means the call sent a script to the instrument.
            extra: More fields for the start event (for example the
                goal id and run number of a scripted suite).
        """
        self._dir = log_dir or SESSION_LOG_DIR
        self._app_version = app_version
        self._mode = mode
        self._port_fn = port_fn
        self._provenance_fn = provenance_fn
        self._extra = dict(extra or {})
        self._secrets: set[str] = set()
        self._lock = threading.Lock()
        self._fh: Any = None
        self._path: Optional[str] = None
        self._seq = 0
        self._ended = False
        self._session_id = uuid.uuid4().hex[:8]
        self._pending_prov: dict[str, dict[str, Any]] = {}

    # ---- properties ------------------------------------------------------

    @property
    def path(self) -> Optional[str]:
        """The log file path, or None before :meth:`start`."""
        return self._path

    @property
    def started(self) -> bool:
        """True once the file is open."""
        return self._fh is not None

    @property
    def ended(self) -> bool:
        """True once :meth:`end` has run."""
        return self._ended

    def add_secret(self, secret: Optional[str]) -> None:
        """Register a value that must never reach the file."""
        if secret and len(secret) >= _MIN_SECRET_LEN:
            with self._lock:
                self._secrets.add(secret)

    # ---- events ----------------------------------------------------------

    def start(self, model: str, tool_names: list[str]) -> Optional[str]:
        """Open the file and write the ``session_start`` event.

        Idempotent: a second call returns the existing path.

        Args:
            model: Model identifier at session start.
            tool_names: Names of the tools offered to the model.

        Returns:
            The log path, or None if the file could not be created.
        """
        with self._lock:
            if self._fh is not None or self._ended:
                return self._path
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self._dir, f"{stamp}_{self._session_id}.jsonl")
            try:
                os.makedirs(self._dir, exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8", newline="\n")
            except OSError as exc:
                logger.warning("Agent session log not written: %s", exc)
                self._ended = True
                return None
            self._path = path
        port = None
        if self._port_fn is not None:
            try:
                port = self._port_fn()
            except Exception:  # noqa: BLE001 - logging must not fail a turn
                port = None
        self._write({
            "event": "session_start",
            "session_id": self._session_id,
            "app_version": self._app_version,
            "model": model,
            "tools": sorted(tool_names),
            "mode": self._mode,
            "port": port,
            **self._extra,
        })
        logger.info("Agent session log: %s", path)
        return path

    def user_message(self, text: str) -> None:
        """Record a user message."""
        self._write({"event": "user_message", "text": text})

    def assistant_message(
        self,
        text: str,
        *,
        model: str,
        stop_reason: Optional[str],
        tool_calls: int,
        usage: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record one complete assistant message (its text blocks)."""
        event: dict[str, Any] = {
            "event": "assistant_message",
            "model": model,
            "text": text,
            "stop_reason": stop_reason,
            "tool_calls": tool_calls,
        }
        if usage:
            event["usage"] = usage
        self._write(event)

    def tool_call(self, call_id: str, name: str, arguments: Any) -> None:
        """Record a tool call before it runs."""
        self._pending_prov[call_id] = self._provenance()
        self._write({
            "event": "tool_call",
            "id": call_id,
            "name": name,
            "arguments": arguments,
        })

    def tool_result(
        self,
        call_id: str,
        name: str,
        result: Any,
        *,
        is_error: bool,
        duration_ms: Optional[float],
        cancelled: bool = False,
    ) -> None:
        """Record a tool result exactly as the model saw it.

        ``result`` is the JSON string handed to the model; it is stored
        as the parsed object when it parses. ``artifacts`` links the
        line to files on disk: the digest of any script sent to the
        instrument during the call and any exported file path.
        """
        parsed = _as_json_value(result)
        event: dict[str, Any] = {
            "event": "tool_result",
            "id": call_id,
            "name": name,
            "is_error": bool(is_error),
            "duration_ms": (
                None if duration_ms is None else round(duration_ms, 1)
            ),
            "result": parsed,
        }
        if cancelled:
            event["cancelled"] = True
        artifacts = self._artifacts(call_id, parsed)
        if artifacts:
            event["artifacts"] = artifacts
        self._write(event)

    def error(self, message: str, kind: str = "agent_error") -> None:
        """Record an error (API, client or loop failure)."""
        self._write({"event": "error", "kind": kind, "message": message})

    def note(self, kind: str, **fields: Any) -> None:
        """Record another lifecycle event (for example a cancelled turn)."""
        self._write({"event": kind, **fields})

    def end(self, reason: str = "closed") -> None:
        """Write ``session_end`` and close the file (idempotent)."""
        if self._fh is None or self._ended:
            self._ended = True
            return
        self._write({"event": "session_end", "reason": reason})
        with self._lock:
            self._ended = True
            fh, self._fh = self._fh, None
        try:
            fh.close()
        except OSError:
            pass

    # ---- internals -------------------------------------------------------

    def _provenance(self) -> dict[str, Any]:
        if self._provenance_fn is None:
            return {}
        try:
            return dict(self._provenance_fn() or {})
        except Exception:  # noqa: BLE001 - logging must not fail a turn
            return {}

    def _artifacts(self, call_id: str, result: Any) -> dict[str, Any]:
        before = self._pending_prov.pop(call_id, {})
        after = self._provenance()
        artifacts: dict[str, Any] = {}
        sent = int(after.get("scripts_sent") or 0) - int(
            before.get("scripts_sent") or 0
        )
        if sent > 0:
            artifacts["script_digest"] = after.get("script_digest")
            artifacts["scripts_sent"] = sent
        if isinstance(result, dict):
            paths = [
                result[key]
                for key in ("path", "output_dir", "auto_save_dir")
                if isinstance(result.get(key), str)
            ]
            if paths:
                artifacts["export_paths"] = paths
        return artifacts

    def _redact(self, line: str) -> str:
        for secret in self._secrets:
            line = line.replace(secret, _REDACTED)
        return _KEY_RE.sub(_REDACTED, line)

    def _write(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self._fh is None or self._ended:
                return
            self._seq += 1
            record = {"ts": _now(), "seq": self._seq, **event}
            try:
                line = json.dumps(record, ensure_ascii=True, default=str)
                self._fh.write(self._redact(line) + "\n")
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except (OSError, ValueError) as exc:
                logger.warning("Agent session log write failed: %s", exc)

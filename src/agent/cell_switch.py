"""The agent asks for the solution in the cell to be changed, and waits.

Some experiments change the solution in the cell between steps (for example
PBS, then a plating bath, then PBS again). The application cannot do that;
a person (later, perhaps a robot) can. ``request_cell_switch`` is the seam:
the agent says what the cell should hold next and why, the session pauses
in a clear "waiting for cell switch" state until the actuator reports back,
and the request and its outcome are written to the session log.

The actuator is a :class:`CellSwitchPrompt`. The GUI one is a banner in the
agent dock with Continue and Cancel buttons
(:class:`src.gui.cell_switch_banner.CellSwitchBanner`). A robot would
implement the same two methods and report ``actuator="robot"``. Nothing else
in the agent changes.

The record of one switch is a plain dict, the same whoever acts::

    {"record": "cell_switch", "request_id": "3fa9c1d2",
     "requested": {"medium": ..., "reason": ..., "timestamp": ...},
     "performed": {"done": bool, "timestamp": ..., "operator": ...,
                   "actuator": "human"},
     "confirmation": "confirmed" | "cancelled" | "stopped",
     "waited_s": 41.2}

``confirmation`` is "confirmed" when the actuator reports the cell switched,
"cancelled" when it declines, and "stopped" when the turn was cancelled
(Stop button) while waiting.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import getpass
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Protocol

from src.agent.bridge import run_on_gui

logger = logging.getLogger(__name__)

__all__ = [
    "SYSTEM_PROMPT_RULE",
    "TOOL_NAME",
    "CellSwitchOutcome",
    "CellSwitchPrompt",
    "CellSwitchRequest",
    "build_cell_switch_tool",
    "make_record",
    "now_iso",
]

TOOL_NAME = "request_cell_switch"

#: Added to the system prompt when the tool is registered.
SYSTEM_PROMPT_RULE = (
    "7. Changing the solution. Only the person can change the solution in "
    "the cell. Before ANY step that needs a different solution than the "
    "one now in the cell (for example PBS, then a plating bath, then PBS "
    "again), call request_cell_switch with the new medium and the reason, "
    "and wait: it returns only when the person confirms the cell is "
    "switched. Start no run for the new solution until it returns with "
    "performed true. If it returns cancelled or stopped, run nothing "
    "further for that step; say what you were about to do and ask how to "
    "proceed. Never assume the cell was switched, and never call it while "
    "a measurement is running.\n"
)

_MAX_MEDIUM_CHARS = 200
_MAX_REASON_CHARS = 400


def now_iso() -> str:
    """Local time with UTC offset, to the second."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class CellSwitchRequest:
    """What the agent asked for."""

    request_id: str
    new_medium: str
    reason: str
    requested_at: str


@dataclass(frozen=True)
class CellSwitchOutcome:
    """What the actuator reports.

    Attributes:
        confirmed: True if the cell now holds the new medium.
        operator: Who did it or declined (a login name, or a device name).
        confirmed_at: When, as an ISO time stamp.
        actuator: ``"human"``, or ``"robot"`` for a machine.
    """

    confirmed: bool
    operator: Optional[str] = None
    confirmed_at: Optional[str] = None
    actuator: str = "human"


class CellSwitchPrompt(Protocol):
    """The actuator side. Both methods run on the GUI thread."""

    def present(
        self,
        request: CellSwitchRequest,
        resolve: Callable[[CellSwitchOutcome], None],
    ) -> None:
        """Show the request and return at once; call ``resolve`` once later."""

    def withdraw(self, request_id: str) -> None:
        """Take the request down without answering it (the turn was stopped)."""


def default_operator() -> str:
    """The local login name, or ``"unknown"``."""
    try:
        return getpass.getuser() or "unknown"
    except Exception:  # noqa: BLE001 - a missing name must not stop a run
        return "unknown"


def make_record(
    request: CellSwitchRequest,
    outcome: Optional[CellSwitchOutcome],
    confirmation: str,
    waited_s: float,
) -> dict[str, Any]:
    """Build the generic record of one switch (see the module note)."""
    done = bool(outcome and outcome.confirmed)
    return {
        "record": "cell_switch",
        "request_id": request.request_id,
        "requested": {
            "medium": request.new_medium,
            "reason": request.reason,
            "timestamp": request.requested_at,
        },
        "performed": {
            "done": done,
            "timestamp": outcome.confirmed_at if outcome else None,
            "operator": outcome.operator if outcome else None,
            "actuator": outcome.actuator if outcome else None,
        },
        "confirmation": confirmation,
        "waited_s": round(float(waited_s), 1),
    }


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def build_cell_switch_tool(
    prompt: CellSwitchPrompt,
    *,
    is_busy: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[..., None]] = None,
    call_on_gui: Callable[..., Any] = run_on_gui,
) -> tuple[dict[str, Any], Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
    """Build the ``(tool_def, handler)`` pair for ``request_cell_switch``.

    Args:
        prompt: The actuator (the dock banner in the app).
        is_busy: Zero-argument predicate, for example ``engine.isRunning``.
            While it is true the tool refuses, so the cell is never changed
            under a running measurement.
        log: ``log(kind, **fields)``, for example ``SessionLog.note``. It
            gets ``cell_switch_requested`` before the wait and
            ``cell_switch_resolved`` (the full record) after it.
        call_on_gui: Runs a callable on the GUI thread and returns an
            awaitable. Replaced in tests that have no Qt.

    Returns:
        The tool definition and its async handler. The handler never raises
        except ``CancelledError``, which it passes on after taking the
        request down and logging it as "stopped".
    """

    def _log(kind: str, **fields: Any) -> None:
        if log is None:
            return
        try:
            log(kind, **fields)
        except Exception:  # noqa: BLE001 - logging must not fail the tool
            logger.exception("Could not log %s", kind)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        medium = _text(args.get("new_medium"), _MAX_MEDIUM_CHARS)
        reason = _text(args.get("reason"), _MAX_REASON_CHARS)
        if not medium:
            return {
                "ok": False,
                "error": (
                    "A non-empty 'new_medium' is required: say what the "
                    "cell should hold next."
                ),
            }
        if is_busy is not None and is_busy():
            return {
                "ok": False,
                "error": (
                    "A measurement is running. Wait for it to finish, or "
                    "abort it, before asking for a cell switch."
                ),
            }

        request = CellSwitchRequest(
            request_id=uuid.uuid4().hex[:8],
            new_medium=medium,
            reason=reason,
            requested_at=now_iso(),
        )
        _log(
            "cell_switch_requested",
            request_id=request.request_id,
            requested={
                "medium": medium,
                "reason": reason,
                "timestamp": request.requested_at,
            },
        )

        answer: concurrent.futures.Future = concurrent.futures.Future()

        def resolve(outcome: CellSwitchOutcome) -> None:
            if not answer.done():          # the first answer counts
                answer.set_result(outcome)

        started = time.monotonic()
        try:
            await call_on_gui(prompt.present, request, resolve)
            outcome = await asyncio.wrap_future(answer)
        except asyncio.CancelledError:
            # Stop pressed while waiting: take the prompt down, record it,
            # and let the turn unwind.
            call_on_gui(prompt.withdraw, request.request_id)
            _log(
                "cell_switch_resolved",
                **make_record(request, None, "stopped", time.monotonic() - started),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - the prompt could not be shown
            logger.exception("Cell switch prompt failed")
            call_on_gui(prompt.withdraw, request.request_id)
            record = make_record(request, None, "cancelled", time.monotonic() - started)
            _log("cell_switch_resolved", **record)
            return {
                "ok": False,
                "performed": False,
                "confirmation": "cancelled",
                "error": (
                    f"The cell-switch prompt could not be shown ({exc}); the "
                    f"cell was NOT switched to {medium}. Do not run any step "
                    "that needs it."
                ),
                "record": record,
            }

        waited = time.monotonic() - started
        if outcome.confirmed:
            record = make_record(request, outcome, "confirmed", waited)
            _log("cell_switch_resolved", **record)
            return {
                "ok": True,
                "performed": True,
                "confirmation": "confirmed",
                "medium": medium,
                "waited_s": record["waited_s"],
                "message": (
                    f"The person confirmed the cell now holds {medium}. "
                    "You may continue."
                ),
                "record": record,
            }
        record = make_record(request, outcome, "cancelled", waited)
        _log("cell_switch_resolved", **record)
        return {
            "ok": False,
            "performed": False,
            "confirmation": "cancelled",
            "error": (
                f"The person cancelled: the cell was NOT switched to {medium}. "
                "Do not run any step that needs it. Say what you were about "
                "to do and ask how to proceed."
            ),
            "record": record,
        }

    tool_def = {
        "name": TOOL_NAME,
        "description": (
            "Ask the person to change the solution in the electrochemical "
            "cell, then WAIT until they confirm. Call this BEFORE any step "
            "that needs a different solution than the one now in the cell "
            "(for example PBS, then a plating bath, then PBS again). The "
            "session pauses with a 'Waiting for cell switch' prompt that "
            "tells the person what to put in the cell; the call returns "
            "only when they press Continue (performed true) or Cancel "
            "(performed false), or when you are stopped. Do not start a "
            "run for the new solution before it returns performed true. "
            "Never call it while a measurement is running. The result "
            "carries a record: what was requested and why, whether and "
            "when it was done, by whom, and how long it took."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "new_medium": {
                    "type": "string",
                    "description": (
                        "What the cell should hold next, for example "
                        "'PBS, pH 7.4' or 'Pt-black plating bath'."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why, in one sentence, for example 'the next step "
                        "is the Pt-black deposition'."
                    ),
                },
            },
            "required": ["new_medium", "reason"],
            "additionalProperties": False,
        },
    }
    return tool_def, handler

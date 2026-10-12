"""AgentWorker: the async streaming agentic loop in a worker QThread.

Concurrency model (architecture.md, "Embedded Claude Agent -
Threading/Async Bridge"): ``AgentWorker.run()`` creates and owns its
OWN asyncio event loop.  The AsyncAnthropic streaming manual tool-use
loop lives entirely inside that loop; engine/GUI interaction happens
only through the tool handlers, which go through the
:class:`~src.agent.engine_adapter.EngineAdapter` (and therefore
``bridge.run_on_gui``).  The worker NEVER touches a Qt widget; it
communicates outward exclusively through the class-level Qt signals,
which the dock panel consumes on the GUI thread via queued delivery.

Hard API rules baked in (Fable 5 surface):

* ``thinking={"type": "adaptive"}`` is sent ONLY for models known to
  support it (:data:`ADAPTIVE_THINKING_MODELS`); for other models the
  ``thinking`` parameter is omitted entirely.  ``budget_tokens``,
  ``temperature``, ``top_p``, ``top_k`` and
  ``thinking={"type": "disabled"}`` are never sent (400 on Fable 5).
* The module imports with NO API key set: ``anthropic`` is imported at
  module top (eager, before any asyncio loop) but the client is only
  instantiated lazily inside the worker's running loop.  The
  ``client_factory`` constructor argument is the testability seam --
  when provided it is called with ``api_key=...`` to produce the
  client instead of constructing ``AsyncAnthropic``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

# Eager third-party import at module top, before any asyncio loop
# (blueprint constraint).  No client is constructed at import time.
import anthropic
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.agent.session_log import SessionLog
from src.agent.tools import ToolRegistry, dispatch_tool

logger = logging.getLogger(__name__)

__all__ = [
    "ADAPTIVE_THINKING_MODELS",
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "AgentWorker",
]

DEFAULT_MODEL = "claude-fable-5"

#: Models that accept thinking={"type": "adaptive"}.  For any model not
#: in this set the thinking parameter is omitted entirely (never send
#: {"type": "disabled"} -- 400 on Fable 5).
ADAPTIVE_THINKING_MODELS = frozenset({
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
})

#: Stable system prompt (byte-identical across turns for prompt
#: caching; a cache_control breakpoint is attached at request time).
DEFAULT_SYSTEM_PROMPT = (
    "You are the embedded lab assistant inside a desktop application "
    "that controls a PalmSens EmStat Pico potentiostat with a MUX16 "
    "16-channel multiplexer. The board has two independent 16-way "
    "multiplexers: one selects the working electrode (WE) position, "
    "the other selects the RE/CE position. The RE/CE wiring modes "
    "set the second one: 'external' (the default) holds RE/CE at "
    "position 15 for every measured channel; 'on_board' holds RE/CE "
    "at position 16; 'manual' routes RE/CE to the positions in "
    "re_ce_channels, one per measured channel. A channel passes "
    "current only if the cell connects that WE position to the RE/CE "
    "position in use. Pairing WE N with RE/CE N is valid: it is how "
    "a cell with a separate RE/CE for every channel is measured.\n"
    "\n"
    "Your tools drive the SAME live instrument and plots the user "
    "sees: run_cv, run_ca, run_cp, run_eis and run_geis start real "
    "measurements and block until completion; list_ports, "
    "connect_device, disconnect_device and device_status manage the "
    "serial connection; abort_measurement stops a running "
    "measurement.\n"
    "\n"
    "Operating rules:\n"
    "1. Check device_status (and connect with connect_device if "
    "needed) before starting any measurement.\n"
    "2. Never start a measurement while one is running; if the user "
    "asks for a new run or to stop, call abort_measurement first and "
    "confirm via device_status.\n"
    "3. Only the channels the user asks for: channels are integers "
    "1-16. Ask for clarification rather than guessing parameters "
    "that could damage a cell (applied potentials, applied currents, "
    "durations). The current RANGE of a potential-controlled "
    "measurement is a measuring setting: a larger or smaller range "
    "does not drive more current through the cell. When the range "
    "the user named overloads or cannot resolve the current, "
    "measure again at a range that fits, report that value, and "
    "say plainly that you changed the range and why.\n"
    "4. Tool results are compact JSON summaries; report them back "
    "concisely (technique, channels, point counts, key parameters). "
    "Never fabricate data the tools did not return.\n"
    "5. Use plain ASCII in all replies (no Unicode symbols or "
    "emojis).\n"
    "6. Wiring check. When the user did not name the RE/CE wiring, "
    "start in 'external'. On a cell with a separate RE/CE for each "
    "channel, 'external' passes current only on channel 15 (and "
    "'on_board' only on 16); every other channel reads open, "
    "whether you measure one channel or sixteen. So when a channel "
    "reads open or near zero current (under about 0.5% of the "
    "current range; 'near_zero' in a CA result), suspect the wiring "
    "before the current range and before the cell. A few nA on an "
    "open channel is steady and can look well above the noise: it "
    "is still not a measurement, and you must never report a "
    "resistance computed from it. Do not stop there and do not "
    "report the channel as open: re-measure the SAME channels (and "
    "only those) with RE/CE at each channel's own position: those "
    "among 1-14 in 'manual' mode with re_ce_channels equal to "
    "channels, channel 15 in 'external', channel 16 in 'on_board'. "
    "Then report each of them and say which wiring each value came "
    "from. Report a channel as open or miswired only if it still "
    "passes no current in that same-position check. Make this "
    "check with a short CA hold (a few seconds), and make it BEFORE "
    "any long measurement such as an impedance sweep: a sweep on an "
    "open channel returns nothing usable, and a multichannel or "
    "autorange sweep is not stopped early, so it can take many "
    "minutes. If you cannot get a value you trust, say so and say "
    "why: no value is always better than a wrong value.\n"
)

#: Queue sentinel telling the main coroutine to exit.
_STOP = object()


class AgentWorker(QThread):
    """Worker QThread running the AsyncAnthropic agentic loop.

    Signals (class-level; connect from the GUI thread -- cross-thread
    delivery is queued automatically):

    * ``agent_text_delta(str)``: streamed assistant text.
    * ``tool_call_started(object)``: ``{"id", "name", "input"}``.
    * ``tool_call_finished(object)``: ``{"id", "name", "result"}``.
    * ``tool_call_error(object)``: ``{"id", "name", "result"}``.
    * ``agent_turn_started()`` / ``agent_turn_done()``: turn lifecycle
      (``agent_turn_done`` fires at the end of EVERY turn, including
      error turns, so the GUI can always re-enable input).
    * ``agent_error(str)``: API/auth/refusal errors; the worker stays
      alive and keeps serving subsequent messages.

    Thread-safe public methods callable from the GUI thread:
    :meth:`submit_user_message`, :meth:`request_stop`,
    :meth:`set_model`, :meth:`set_api_key`, :meth:`is_busy`.
    """

    agent_text_delta = pyqtSignal(str)
    tool_call_started = pyqtSignal(object)
    tool_call_finished = pyqtSignal(object)
    tool_call_error = pyqtSignal(object)
    agent_turn_started = pyqtSignal()
    agent_turn_done = pyqtSignal()
    agent_error = pyqtSignal(str)

    def __init__(
        self,
        registry: ToolRegistry,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        system_prompt: Optional[str] = None,
        client_factory: Optional[Callable[..., Any]] = None,
        max_tokens: int = 16000,
        session_log: Optional[SessionLog] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """Initialize the worker (no client/network activity here).

        Args:
            registry: Tool registry providing ``tool_defs`` and the
                handlers resolved by ``dispatch_tool``.
            api_key: Anthropic API key; when None the SDK's normal
                environment resolution applies at first use.
            model: Model id (default ``claude-fable-5``).
            system_prompt: Override for :data:`DEFAULT_SYSTEM_PROMPT`.
                Keep it stable across turns for prompt caching.
            client_factory: Testability seam -- called as
                ``client_factory(api_key=...)`` to produce the client
                object instead of constructing ``AsyncAnthropic``.
            max_tokens: Per-response output token cap.
            session_log: Optional per-session JSON Lines log. It is
                opened on the first user message and closed when the
                loop stops. The API key is registered with it as a
                secret so it can never reach the file.
            parent: Optional QObject parent.
        """
        super().__init__(parent)
        self._registry = registry
        self._client_factory = client_factory
        self._max_tokens = int(max_tokens)
        self._system_prompt = (
            system_prompt if system_prompt is not None
            else DEFAULT_SYSTEM_PROMPT
        )

        self._lock = threading.Lock()
        self._api_key = api_key
        self._log = session_log
        if session_log is not None:
            session_log.add_secret(api_key)
        self._model = model
        self._client_stale = False
        self._stop_requested = False
        self._pending: list[str] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional["asyncio.Queue[Any]"] = None

        # Loop-thread-only state (no lock needed there).
        self._client: Any = None
        self._messages: list[dict[str, Any]] = []
        self._turn_task: Optional["asyncio.Task[None]"] = None
        self._busy = False

    # ---- Thread-safe public API (GUI thread) -------------------------------

    def submit_user_message(self, text: str) -> None:
        """Queue a user message for the next turn (any thread).

        Messages submitted before the loop is up are buffered and
        drained when :meth:`run` starts.
        """
        with self._lock:
            if self._stop_requested:
                logger.warning(
                    "submit_user_message ignored: stop requested."
                )
                return
            loop, queue = self._loop, self._queue
            if loop is None or queue is None:
                self._pending.append(text)
                return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, text)
        except RuntimeError:  # loop already closed (shutdown race)
            logger.warning(
                "submit_user_message dropped: agent loop is closed."
            )

    def request_stop(self) -> None:
        """Cancel the current turn and shut the loop down cleanly.

        Idempotent and thread-safe; after the loop drains, ``run()``
        returns and the QThread finishes (``finished`` signal fires).
        The app calls this on close, then ``wait()``s the thread.
        """
        with self._lock:
            self._stop_requested = True
            loop, queue = self._loop, self._queue
        if loop is None:
            return

        def _shutdown() -> None:
            if self._turn_task is not None:
                self._turn_task.cancel()
            if queue is not None:
                queue.put_nowait(_STOP)

        try:
            loop.call_soon_threadsafe(_shutdown)
        except RuntimeError:
            pass  # loop already closed

    def cancel_turn(self) -> None:
        """Cancel the in-flight turn but KEEP the worker alive.

        This is the Stop button's action.  :meth:`request_stop` is the
        app-close action and is deliberately different: it also sets
        ``_stop_requested``, which makes :meth:`submit_user_message` refuse
        new input and makes the main loop break out, ending the QThread --
        so wiring Stop to it killed the agent until the app was relaunched.

        Cancelling the task raises ``CancelledError`` at whatever point the
        turn is currently suspended, and what happens next depends on where
        that is:

        * Still streaming the model's reply (no tool call in flight): the
          error propagates straight out of :meth:`_run_turn`, nothing new is
          appended to ``_messages`` (the assistant message is only committed
          after the stream finishes), and the turn simply ends.
        * Inside :meth:`_handle_tool_use` (a tool call -- e.g. a running
          measurement -- was in flight): the tool is aborted, every
          unanswered ``tool_use`` id gets a synthesized cancelled
          ``tool_result``, and :meth:`_handle_tool_use` SWALLOWS the
          ``CancelledError`` rather than propagating it.  That lets
          :meth:`_run_turn`'s loop make ONE more request with the cancelled
          result already in history, so the model can acknowledge the stop
          and ask what to do next instead of the turn dying silently.
          Pressing Stop again during that follow-up request still cancels
          it normally.

        Either way ``agent_turn_done`` still fires from :meth:`_run_turn`'s
        ``finally`` once the turn (initial or follow-up) truly ends, so the
        dock re-enables itself through the normal path, and the main loop
        goes back to awaiting the next queued message.

        Idempotent and thread-safe; a no-op when no turn is running.
        """
        with self._lock:
            loop = self._loop
        if loop is None:
            return

        def _cancel() -> None:
            task = self._turn_task
            if task is not None and not task.done():
                logger.info("Cancelling in-flight agent turn.")
                task.cancel()

        try:
            loop.call_soon_threadsafe(_cancel)
        except RuntimeError:  # loop already closed
            pass

    def set_model(self, model: str) -> None:
        """Switch the model id; applied at the next turn start."""
        with self._lock:
            self._model = str(model)
        logger.info("Agent model set to %s (next turn).", model)

    def set_api_key(self, api_key: Optional[str]) -> None:
        """Replace the API key; the client is rebuilt at next turn."""
        with self._lock:
            self._api_key = api_key
            self._client_stale = True
        if self._log is not None:
            self._log.add_secret(api_key)
        logger.info("Agent API key updated (client rebuild next turn).")

    @property
    def session_log(self) -> Optional[SessionLog]:
        """The session log, or None when logging is off."""
        return self._log

    def is_busy(self) -> bool:
        """Return True while a turn is being processed."""
        return self._busy

    # ---- QThread entry point ------------------------------------------------

    def run(self) -> None:  # noqa: D102 - QThread override
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        with self._lock:
            self._loop = loop
            self._queue = queue
            for text in self._pending:
                queue.put_nowait(text)
            self._pending.clear()
            stop = self._stop_requested
        logger.info("Agent worker loop starting.")
        try:
            if not stop:
                loop.run_until_complete(self._main())
        except BaseException:  # noqa: BLE001 - never kill the QThread loudly
            logger.exception("Agent worker loop crashed.")
        finally:
            with self._lock:
                self._loop = None
                self._queue = None
            try:
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                if tasks:
                    loop.run_until_complete(
                        asyncio.gather(*tasks, return_exceptions=True)
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Agent loop task cleanup failed.")
            finally:
                loop.close()
                asyncio.set_event_loop(None)
            logger.info("Agent worker loop stopped.")

    # ---- Main coroutine (loop thread) ----------------------------------------

    async def _main(self) -> None:
        """Pull user messages and process one turn at a time."""
        assert self._queue is not None
        try:
            while True:
                item = await self._queue.get()
                if item is _STOP or self._stop_requested:
                    break
                self._turn_task = asyncio.ensure_future(
                    self._run_turn(str(item))
                )
                try:
                    await self._turn_task
                except asyncio.CancelledError:
                    logger.info("Agent turn cancelled.")
                    if self._log is not None:
                        self._log.note("turn_cancelled")
                finally:
                    self._turn_task = None
                if self._stop_requested:
                    break
        finally:
            if self._log is not None:
                self._log.end("stopped")
            await self._close_client()

    async def _close_client(self) -> None:
        """Best-effort close of the (possibly fake) client."""
        client, self._client = self._client, None
        if client is None:
            return
        close = getattr(client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("Client close failed.")

    async def _ensure_client(self) -> Any:
        """Create (or rebuild) the client lazily inside the loop."""
        with self._lock:
            api_key = self._api_key
            stale = self._client_stale
            self._client_stale = False
        if self._client is not None and not stale:
            return self._client
        await self._close_client()
        if self._client_factory is not None:
            self._client = self._client_factory(api_key=api_key)
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    def _request_kwargs(self, model: str) -> dict[str, Any]:
        """Build the per-request kwargs (stable for prompt caching).

        Never includes ``temperature``/``top_p``/``top_k`` or
        ``budget_tokens``; ``thinking`` is adaptive-or-absent.
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": self._registry.tool_defs,
            "messages": self._messages,
        }
        if model in ADAPTIVE_THINKING_MODELS:
            kwargs["thinking"] = {"type": "adaptive"}
        return kwargs

    def _report_error(self, message: str) -> None:
        """Emit ``agent_error`` and record it in the session log."""
        if self._log is not None:
            self._log.error(message)
        self.agent_error.emit(message)

    def _log_assistant(self, response: Any, model: str) -> None:
        """Record one finished assistant message in the session log."""
        if self._log is None:
            return
        content = getattr(response, "content", None) or []
        text = "".join(
            getattr(block, "text", "") or ""
            for block in content
            if getattr(block, "type", None) == "text"
        )
        n_tools = sum(
            1 for block in content
            if getattr(block, "type", None) == "tool_use"
        )
        usage_obj = getattr(response, "usage", None)
        usage = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = getattr(usage_obj, key, None)
            if isinstance(value, int):
                usage[key] = value
        self._log.assistant_message(
            text,
            model=model,
            stop_reason=getattr(response, "stop_reason", None),
            tool_calls=n_tools,
            usage=usage or None,
        )

    async def _run_turn(self, text: str) -> None:
        """Process ONE user turn: stream, dispatch tools, loop to end."""
        self._busy = True
        self.agent_turn_started.emit()
        try:
            with self._lock:
                model = self._model
            if self._log is not None:
                if not self._log.started:
                    self._log.start(model, self._registry.names())
                self._log.user_message(text)
            try:
                client = await self._ensure_client()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Client construction failed.")
                self._report_error(
                    "Could not create the Anthropic client: "
                    f"{exc}. Set a valid API key in the panel or the "
                    "ANTHROPIC_API_KEY environment variable."
                )
                return

            self._messages.append({"role": "user", "content": text})

            while True:
                try:
                    async with client.messages.stream(
                        **self._request_kwargs(model)
                    ) as stream:
                        async for event in stream:
                            if (
                                event.type == "content_block_delta"
                                and event.delta.type == "text_delta"
                            ):
                                self.agent_text_delta.emit(
                                    event.delta.text
                                )
                        response = await stream.get_final_message()
                except asyncio.CancelledError:
                    raise
                except anthropic.AuthenticationError as exc:
                    self._report_error(
                        "Authentication failed: set a valid Anthropic "
                        f"API key and try again. ({exc})"
                    )
                    return
                except anthropic.RateLimitError as exc:
                    self._report_error(
                        f"Rate limited by the Anthropic API; wait a "
                        f"moment and retry. ({exc})"
                    )
                    return
                except anthropic.APIConnectionError as exc:
                    self._report_error(
                        "Could not reach the Anthropic API; check the "
                        f"network connection. ({exc})"
                    )
                    return
                except anthropic.APIError as exc:
                    self._report_error(f"Anthropic API error: {exc}")
                    return
                except Exception as exc:  # noqa: BLE001 - stay alive
                    logger.exception("Unexpected agent-loop failure.")
                    self._report_error(
                        f"Unexpected agent error: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return

                # SDK content blocks pass straight back into history.
                self._messages.append(
                    {"role": "assistant", "content": response.content}
                )
                self._log_assistant(response, model)
                stop_reason = getattr(response, "stop_reason", None)

                if stop_reason == "tool_use":
                    await self._handle_tool_use(response)
                    if self._stop_requested:
                        # Shutting down (app close or a scripted limit):
                        # make no follow-up request.
                        return
                    continue
                if stop_reason == "pause_turn":
                    # Re-send as-is; never inject extra user text.
                    logger.info("pause_turn: continuing the turn.")
                    continue
                if stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    extra = f" ({details})" if details else ""
                    self._report_error(
                        "The model refused this request for safety "
                        f"reasons.{extra}"
                    )
                    return
                if stop_reason == "max_tokens":
                    logger.warning(
                        "Turn hit max_tokens=%d; output may be "
                        "truncated.",
                        self._max_tokens,
                    )
                return  # end_turn / max_tokens / anything else
        finally:
            self._busy = False
            self.agent_turn_done.emit()

    def _append_cancelled_tool_results(
        self, response: Any, tool_results: list[dict[str, Any]]
    ) -> None:
        """Close out a cancelled tool round so ``_messages`` stays valid.

        Appends an ``is_error`` tool_result for every ``tool_use`` id in
        ``response`` that has no entry in ``tool_results`` yet, then commits
        the whole list as the user message the API expects. Each freshly-
        cancelled block also gets a ``tool_call_error`` signal emitted for
        it, so the dock's chip resolves out of "running" -- without this the
        chip blinked "running..." forever, since ``finish_tool_chip`` is
        only ever called from the normal (non-cancelled) dispatch path.

        Any results already collected are preserved -- a completed
        measurement's data is not thrown away just because a later tool in
        the same round was interrupted.

        Args:
            response: The assistant message whose tool_use blocks were being
                dispatched.
            tool_results: Results collected before the cancellation landed;
                mutated in place.
        """
        answered = {
            entry.get("tool_use_id")
            for entry in tool_results
            if entry.get("type") == "tool_result"
        }
        message = (
            "Cancelled by user before completion. Do not retry this tool "
            "call automatically -- tell the user it was stopped and ask "
            "what they would like to do next."
        )
        cancelled = 0
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if block.id in answered:
                continue
            if self._log is not None:
                self._log.tool_result(
                    block.id,
                    getattr(block, "name", "?"),
                    message,
                    is_error=True,
                    duration_ms=None,
                    cancelled=True,
                )
            self.tool_call_error.emit({
                "id": block.id,
                "name": getattr(block, "name", "?"),
                "result": message,
            })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": message,
                "is_error": True,
            })
            cancelled += 1
        if tool_results:
            self._messages.append(
                {"role": "user", "content": tool_results}
            )
        logger.info(
            "Turn cancelled mid-tool-use: %d result(s) kept, "
            "%d synthesised so the model can acknowledge the cancellation "
            "on its next reply instead of the turn dying silently.",
            len(tool_results) - cancelled,
            cancelled,
        )

    async def _handle_tool_use(self, response: Any) -> None:
        """Execute every tool_use block and append ONE result message.

        Emits ``tool_call_started`` before and ``tool_call_finished``
        or ``tool_call_error`` after each dispatch; all tool_result
        blocks for this assistant message go into a single user
        message (API requirement: one result per tool_use id).
        """
        tool_results: list[dict[str, Any]] = []
        try:
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                self.tool_call_started.emit({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                if self._stop_requested:
                    # Shutting down (app close, or a directly connected
                    # tool_call_started slot that enforces a scripted
                    # limit): start no further tool. The handler below
                    # answers this and every later id as cancelled.
                    raise asyncio.CancelledError
                if self._log is not None:
                    self._log.tool_call(block.id, block.name, block.input)
                started = time.monotonic()
                result_json, is_error = await dispatch_tool(
                    self._registry, block.name, block.input
                )
                if self._log is not None:
                    self._log.tool_result(
                        block.id,
                        block.name,
                        result_json,
                        is_error=is_error,
                        duration_ms=(time.monotonic() - started) * 1000.0,
                    )
                payload = {
                    "id": block.id,
                    "name": block.name,
                    "result": result_json,
                }
                if is_error:
                    self.tool_call_error.emit(payload)
                else:
                    self.tool_call_finished.emit(payload)
                entry: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_json,
                }
                if is_error:
                    entry["is_error"] = True
                tool_results.append(entry)
        except asyncio.CancelledError:
            # Stop pressed mid-tool-call.  The assistant message carrying
            # these tool_use blocks is ALREADY in self._messages, and the
            # API requires every tool_use id to be answered by a
            # tool_result in the immediately-following user message.
            # Unwinding without answering them leaves a dangling tool_use
            # that 400s the NEXT request -- and every request after it,
            # because nothing repairs _messages.  That would wedge the
            # session permanently, which is exactly what per-turn cancel
            # exists to avoid.
            #
            # Answer EVERY unanswered id, not just the in-flight one:
            # with parallel tool calls the later blocks never dispatched
            # at all and are equally unanswered.
            #
            # Deliberately NOT re-raised: swallowing it here (rather than
            # propagating out of _run_turn) lets the turn's while-loop make
            # ONE more request with the cancelled tool_result already in
            # history, so the model gets a chance to acknowledge the
            # cancellation and ask what to do next -- instead of the turn
            # dying silently and the agent only reacting once the user
            # happens to type something else. The underlying measurement is
            # already stopped by this point (EngineAdapter.run_technique's
            # own CancelledError handler calls engine.abort() before this
            # exception ever reaches here); only the LLM turn is being kept
            # alive. Pressing Stop again during that follow-up call still
            # cancels normally, since this only absorbs the ONE cancellation
            # already delivered.
            self._append_cancelled_tool_results(response, tool_results)
            return
        if not tool_results:
            # Defensive: stop_reason was tool_use but no tool_use
            # block was present (malformed response).  An empty user
            # content list would 400, so report instead.
            logger.error(
                "stop_reason 'tool_use' without tool_use blocks."
            )
            tool_results.append({
                "type": "text",
                "text": (
                    "No tool_use block was found in your last "
                    "message; please retry or answer directly."
                ),
            })
        self._messages.append({"role": "user", "content": tool_results})

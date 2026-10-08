"""Qt<->asyncio marshaling primitives for the embedded agent.

The agent thread runs its own asyncio event loop inside a worker
QThread (see architecture.md, "Embedded Claude Agent - Threading/Async
Bridge").  It must never touch the engine, the connection, or any
widget directly.  This module provides the two primitives that enforce
that discipline:

``run_on_gui(fn, *args, **kwargs)``
    Marshal a callable onto the GUI thread via a queued Qt signal on a
    GUI-thread-affined invoker QObject, and return a future/awaitable
    that resolves with the callable's return value (or raises its
    exception) back in the caller's context.

``await_signal(signal, error_signal=None, timeout=None)``
    Connect one-shot slots to a success signal and an optional error
    signal, returning a ``concurrent.futures.Future`` resolved by
    whichever fires first.  The slots disconnect themselves so they can
    never fire for a later run.  Consume from an asyncio loop via
    ``asyncio.wrap_future(...)``.

The invoker must be installed once from the GUI thread (after the
QCoreApplication exists) by calling :func:`install`.  Using
``run_on_gui`` before installation raises a clear ``RuntimeError``.

Only ``PyQt6.QtCore`` is imported here -- never QtWidgets.  All imports
are eager and happen at module top, before any asyncio loop exists.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Callable, Optional, Union

from PyQt6.QtCore import QCoreApplication, QObject, Qt, QThread, pyqtSignal

logger = logging.getLogger(__name__)

__all__ = [
    "BridgeNotInstalledError",
    "SignalError",
    "SignalTimeoutError",
    "await_signal",
    "install",
    "is_installed",
    "run_on_gui",
    "uninstall",
]


class BridgeNotInstalledError(RuntimeError):
    """Raised when ``run_on_gui`` is used before :func:`install`."""


class SignalError(RuntimeError):
    """Raised (via the future) when the error signal fires first.

    Attributes:
        payload: The raw payload emitted by the error signal (usually
            an error-message string).
    """

    def __init__(self, payload: Any) -> None:
        super().__init__(str(payload))
        self.payload = payload


class SignalTimeoutError(SignalError):
    """Raised (via the future) when ``await_signal`` times out."""


class _GuiInvoker(QObject):
    """QObject affined to the GUI thread that executes posted jobs.

    Jobs are delivered through a queued signal connection, so emitting
    ``_dispatch`` from any thread executes the job on the thread this
    object lives in (the GUI thread, because :func:`install` creates it
    there).
    """

    _dispatch = pyqtSignal(object)  # zero-arg callable

    def __init__(self) -> None:
        super().__init__()
        self._dispatch.connect(
            self._on_dispatch, Qt.ConnectionType.QueuedConnection
        )

    def post(self, job: Callable[[], None]) -> None:
        """Queue *job* for execution on this object's thread.

        Args:
            job: Zero-argument callable.  Must not raise (jobs built by
                :func:`run_on_gui` route exceptions into a future).
        """
        self._dispatch.emit(job)

    def _on_dispatch(self, job: Callable[[], None]) -> None:
        """Execute a posted job (runs on the GUI thread)."""
        job()


_invoker: Optional[_GuiInvoker] = None
_install_lock = threading.Lock()


def install() -> None:
    """Create the GUI-thread invoker.  Call once from the GUI thread.

    Must be called after the ``QCoreApplication`` (or ``QApplication``)
    has been created, from the application's main (GUI) thread, before
    any agent code calls :func:`run_on_gui`.  Idempotent: repeated
    calls are no-ops while the invoker is valid.

    Raises:
        RuntimeError: If no QCoreApplication exists yet, or if called
            from a thread other than the application's main thread.
    """
    global _invoker
    app = QCoreApplication.instance()
    if app is None:
        raise RuntimeError(
            "bridge.install() requires a QCoreApplication; create the "
            "application first."
        )
    if QThread.currentThread() is not app.thread():
        raise RuntimeError(
            "bridge.install() must be called from the GUI (main) "
            "thread that owns the QCoreApplication."
        )
    with _install_lock:
        if _invoker is not None and _invoker.thread() is app.thread():
            return  # already installed and still valid
        _invoker = _GuiInvoker()
    logger.debug("GUI invoker installed on thread %r.", app.thread())


def uninstall() -> None:
    """Remove the invoker (mainly for tests / app teardown)."""
    global _invoker
    with _install_lock:
        _invoker = None


def is_installed() -> bool:
    """Return True if the GUI invoker has been installed."""
    return _invoker is not None


def run_on_gui(
    fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Union["asyncio.Future[Any]", "concurrent.futures.Future[Any]"]:
    """Schedule ``fn(*args, **kwargs)`` on the GUI thread.

    The callable is queued onto the GUI thread via the installed
    invoker (queued-connection delivery) and executes there as soon as
    the GUI event loop processes the event.  The caller receives a
    future that resolves with the callable's return value or raises
    its exception.

    When called from inside a running asyncio loop (the normal agent-
    thread case) the returned object is an awaitable asyncio future
    (``asyncio.wrap_future`` over a ``concurrent.futures.Future``), so
    callers simply ``await run_on_gui(...)``.  When called with no
    running loop, the raw ``concurrent.futures.Future`` is returned and
    can be waited on with ``.result(timeout)``.

    Args:
        fn: Callable to execute on the GUI thread.
        *args: Positional arguments for *fn*.
        **kwargs: Keyword arguments for *fn*.

    Returns:
        An awaitable asyncio future (if a loop is running in the
        calling thread) or a ``concurrent.futures.Future`` otherwise.

    Raises:
        BridgeNotInstalledError: If :func:`install` has not been
            called from the GUI thread.
    """
    invoker = _invoker
    if invoker is None:
        raise BridgeNotInstalledError(
            "GUI bridge is not installed. Call src.agent.bridge."
            "install() from the GUI thread (after creating the "
            "QCoreApplication) before using run_on_gui()."
        )

    cf_future: "concurrent.futures.Future[Any]" = concurrent.futures.Future()

    def _job() -> None:
        """Run fn on the GUI thread, routing the outcome to the future."""
        if not cf_future.set_running_or_notify_cancel():
            return  # caller cancelled before the GUI thread got here
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - propagate via future
            cf_future.set_exception(exc)
        else:
            cf_future.set_result(result)

    invoker.post(_job)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return cf_future
    return asyncio.wrap_future(cf_future, loop=loop)


def await_signal(
    signal: Any,
    error_signal: Any = None,
    *,
    timeout: Optional[float] = None,
    error_factory: Optional[Callable[[str], BaseException]] = None,
) -> "concurrent.futures.Future[Any]":
    """Return a future resolved by the first of two racing Qt signals.

    One-shot slots are connected to *signal* (success) and, when given,
    *error_signal* (failure).  Whichever fires first wins the race:

    * *signal* -> ``future.set_result(payload)`` where *payload* is the
      single signal argument (or a tuple for multi-argument signals,
      or ``None`` for argument-less signals).
    * *error_signal* -> ``future.set_exception(SignalError(payload))``
      (or the exception built by *error_factory*).
    * timeout (when provided) -> ``SignalTimeoutError``.

    All slots disconnect themselves once the future is done (resolved,
    failed, or cancelled by the caller), so they can never fire for a
    later run.  Connect BEFORE triggering the operation that emits the
    signals to avoid missed-signal races.  The future is thread-safe;
    consume it from an asyncio loop via ``asyncio.wrap_future``.

    Args:
        signal: The Qt success signal (e.g. ``engine.measurement_
            finished``).
        error_signal: Optional Qt error signal racing against success.
        timeout: Optional seconds before the future fails with
            ``SignalTimeoutError``.  ``None`` waits forever.
        error_factory: Optional callable mapping the error payload
            string to the exception set on the future.  Defaults to
            :class:`SignalError`.

    Returns:
        A ``concurrent.futures.Future`` resolved as described above.
        Cancelling it detaches the slots immediately.
    """
    future: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
    lock = threading.Lock()
    state: dict[str, Any] = {"settled": False, "cleaned": False, "timer": None}

    def _claim() -> bool:
        """Atomically claim the right to settle the future (first wins)."""
        with lock:
            if state["settled"]:
                return False
            state["settled"] = True
            return True

    def _cleanup(_future: Any = None) -> None:
        """Disconnect slots and stop the timer; idempotent, any thread."""
        with lock:
            if state["cleaned"]:
                return
            state["cleaned"] = True
            timer = state["timer"]
        for sig, slot in ((signal, _on_success), (error_signal, _on_error)):
            if sig is None:
                continue
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass  # already disconnected or emitter destroyed
        if timer is not None:
            timer.cancel()

    def _on_success(*payload: Any) -> None:
        if not _claim():
            return
        if len(payload) == 1:
            value: Any = payload[0]
        elif payload:
            value = tuple(payload)
        else:
            value = None
        try:
            future.set_result(value)
        except concurrent.futures.InvalidStateError:
            pass  # caller cancelled in the same instant; result dropped

    def _on_error(*payload: Any) -> None:
        if not _claim():
            return
        message = str(payload[0]) if payload else "error signal fired"
        exc = (
            error_factory(message)
            if error_factory is not None
            else SignalError(message)
        )
        try:
            future.set_exception(exc)
        except concurrent.futures.InvalidStateError:
            pass

    def _on_timeout() -> None:
        if not _claim():
            return
        try:
            future.set_exception(
                SignalTimeoutError(
                    f"Timed out after {timeout} s waiting for signal."
                )
            )
        except concurrent.futures.InvalidStateError:
            pass

    # Cleanup runs whenever the future completes, including caller-side
    # cancel(), guaranteeing the one-shot slots never outlive the wait.
    future.add_done_callback(_cleanup)

    # DirectConnection is essential: await_signal is typically called
    # from the agent thread, and an auto/queued connection made there
    # would deliver the slot call to the agent thread's (nonexistent)
    # Qt event loop -- the slot would never run.  With a direct
    # connection the slots execute synchronously on the EMITTING thread
    # (GUI or engine thread); they only touch thread-safe state (the
    # lock, the concurrent future, signal disconnects), so that is safe.
    signal.connect(_on_success, Qt.ConnectionType.DirectConnection)
    if error_signal is not None:
        error_signal.connect(
            _on_error, Qt.ConnectionType.DirectConnection
        )
    if timeout is not None:
        watchdog = threading.Timer(timeout, _on_timeout)
        watchdog.daemon = True
        with lock:
            # If the race already settled (signal fired between connect
            # and here), cleanup has run: do not start a stale timer.
            if state["cleaned"]:
                watchdog = None
            else:
                state["timer"] = watchdog
        if watchdog is not None:
            watchdog.start()
    return future

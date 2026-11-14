"""The window's controls for the MCP server: on/off, address, token.

File > Serve Agent Tools over MCP turns the server on or off (off by default;
the choice is saved). A permanent label in the status bar shows the state and
the address. File > MCP Server Details... shows the address, the access token
(hidden until Show is pressed), a ready Claude Code command, and New Token.

The server itself is :class:`src.agent.mcp_server.McpToolServer`. This module
only owns its lifecycle and what the window shows. Everything here runs on
the GUI thread; the server tells it about calls through a queued signal.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QLabel, QMessageBox, QWidget

from src.agent import bridge as agent_bridge
from src.agent.session_log import SessionLog
from src.agent.tools import ToolRegistry
from src.data.app_settings import (
    get_mcp_server_enabled,
    get_mcp_server_port,
    get_mcp_server_token,
    set_mcp_server_enabled,
)
from src.gui.mcp_details_dialog import McpDetailsDialog, claude_code_command

logger = logging.getLogger(__name__)

# Imported eagerly (CLAUDE.md, "Eager native imports"), but a missing mcp or
# uvicorn package must not stop the window from opening: the MCP menu item
# is then shown off and greyed, saying what to install.
try:
    from src.agent.mcp_server import McpToolServer
except ImportError as _exc:  # pragma: no cover - depends on the environment
    McpToolServer = None  # type: ignore[assignment,misc]
    MCP_IMPORT_ERROR: Optional[str] = str(_exc)
else:
    MCP_IMPORT_ERROR = None

__all__ = ["MCP_IMPORT_ERROR", "McpServerControl", "claude_code_command"]

#: Shown when the packages are missing.
_NOT_INSTALLED = (
    "The MCP server needs the mcp and uvicorn packages: run "
    "pip install -r requirements.txt, then restart ChatStat."
)


class McpServerControl(QObject):
    """Owns the MCP server for one window and shows its state."""

    #: (tool name, status) from the server thread; delivered queued.
    _activity = pyqtSignal(str, str)
    #: Emitted on the GUI thread whenever the server starts or stops.
    state_changed = pyqtSignal()

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        session_log: Optional[SessionLog],
        take: Callable[[str, str, Optional[str]], Optional[str]],
        give_back: Callable[[str], None],
        version: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        """Set up the controls; the server starts only if the setting is on.

        Args:
            registry: The in-app agent's registry (served as it is).
            session_log: The in-app agent's session log.
            take: ``take(tool, call_id, client)`` runs on the GUI thread
                before each MCP call that is not read-only; it checks AND
                takes the driver lease in that one call and returns None,
                or returns a refusal message.
            give_back: ``give_back(call_id)`` runs on the GUI thread when
                that call ends.
            version: Application version reported to clients.
            parent: The main window.
        """
        super().__init__(parent)
        self._registry = registry
        self._log = session_log
        self._take = take
        self._give_back = give_back
        self._version = version
        self._parent_widget = parent
        self._server: Optional["McpToolServer"] = None
        self._error: Optional[str] = None
        self._driver_label: Optional[str] = None

        self.status_label = QLabel()
        self.status_label.setStyleSheet("padding: 0 8px; font-size: 11px;")
        self.toggle_action = QAction("Serve Agent Tools over &MCP", self)
        self.toggle_action.setCheckable(True)
        self.toggle_action.setToolTip(
            "Let an MCP client on this computer (Claude Code, Claude Desktop) "
            "use the agent's tools in this window"
        )
        self.toggle_action.toggled.connect(self._on_toggled)
        self.details_action = QAction("MCP Server &Details...", self)
        self.details_action.triggered.connect(self.show_details)
        self._activity.connect(self._on_activity)

        if McpToolServer is None:
            self.toggle_action.setEnabled(False)
            self.toggle_action.setToolTip(_NOT_INSTALLED)
            self.details_action.setEnabled(False)
            logger.warning("MCP server unavailable: %s", MCP_IMPORT_ERROR)
        elif get_mcp_server_enabled():
            self._start()
        self._sync()

    # ---- state ----------------------------------------------------------

    @property
    def server(self) -> Optional["McpToolServer"]:
        """The running server, or None."""
        return self._server if self._server and self._server.running else None

    @property
    def url(self) -> Optional[str]:
        """The address while serving, else None."""
        server = self.server
        return server.url if server else None

    def set_enabled(self, enabled: bool, *, port: Optional[int] = None) -> bool:
        """Turn the server on or off, save the choice, return whether it serves.

        ``port`` overrides the ``mcp_server_port`` setting (0 picks a free
        port; used by the tests).
        """
        set_mcp_server_enabled(bool(enabled))
        if enabled:
            self._start(port)
        else:
            self._stop()
        self._sync()
        return self.server is not None

    def shutdown(self) -> None:
        """Stop the server (window close). The saved choice is kept."""
        self._stop()

    def running_calls(self) -> list[str]:
        """Tools whose MCP calls are running now."""
        server = self.server
        return server.running_calls() if server else []

    def confirm_stopping_calls(self, action: str) -> bool:
        """Ask before *action* stops a running MCP call; True to go ahead.

        Nothing is asked when no call runs.
        """
        calls = self.running_calls()
        if not calls:
            return True
        answer = QMessageBox.question(
            self._parent_widget,
            "MCP Call Running",
            f"An MCP client's call is running ({', '.join(calls)}). {action} "
            "stops it: a running measurement is aborted and a cell-switch "
            "prompt is taken down. Go on?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def show_driver(self, label: Optional[str]) -> None:
        """Show in the status bar who drives the instrument (GUI thread)."""
        self._driver_label = label
        self._show_status()

    # ---- internals ------------------------------------------------------

    async def _acquire(
        self, tool: str, call_id: str, client: Optional[str]
    ) -> Optional[str]:
        # Server loop: the check and the take are ONE call on the GUI thread.
        return await agent_bridge.run_on_gui(self._take, tool, call_id, client)

    def _release(self, call_id: str) -> None:
        # Server thread, possibly while unwinding a cancellation: post the
        # give-back to the GUI thread and do not wait for it.
        try:
            agent_bridge.run_on_gui(self._give_back, call_id)
        except Exception:  # noqa: BLE001 - bridge gone at app close
            logger.exception("Could not give the driver lease back.")

    def _start(self, port: Optional[int] = None) -> None:
        if self.server is not None or McpToolServer is None:
            return
        self._error = None
        try:
            server = McpToolServer(
                self._registry,
                get_mcp_server_token(),
                port=get_mcp_server_port() if port is None else port,
                session_log=self._log,
                acquire=self._acquire,
                release=self._release,
                on_activity=self._activity.emit,
                version=self._version,
            )
            server.start()
        except (OSError, RuntimeError) as exc:
            self._error = str(exc)
            logger.error("MCP server could not start: %s", exc)
            return
        self._server = server

    def _stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()

    def _sync(self) -> None:
        url = self.url
        self.toggle_action.blockSignals(True)
        self.toggle_action.setChecked(url is not None)
        self.toggle_action.blockSignals(False)
        if url is None:
            self._driver_label = None
        self._show_status()
        self.state_changed.emit()

    def _show_status(self) -> None:
        url = self.url
        if McpToolServer is None:
            self.status_label.setText("MCP: not installed")
            self.status_label.setToolTip(_NOT_INSTALLED)
        elif url and self._driver_label:
            self.status_label.setText(f"MCP: {self._driver_label} is driving")
            self.status_label.setToolTip(
                f"Serving at {url}. Start, the sequencer and the in-app agent "
                "wait until this call has finished."
            )
        elif url:
            self.status_label.setText(f"MCP: {url}")
            self.status_label.setToolTip(
                "The agent's tools are served to MCP clients on this computer "
                "(127.0.0.1 only, access token required). File > MCP Server "
                "Details... shows the token."
            )
        elif self._error:
            self.status_label.setText("MCP: could not start")
            self.status_label.setToolTip(self._error)
        else:
            self.status_label.setText("MCP: off")
            self.status_label.setToolTip(
                "File > Serve Agent Tools over MCP turns it on."
            )

    @pyqtSlot(bool)
    def _on_toggled(self, checked: bool) -> None:
        if not checked and not self.confirm_stopping_calls(
            "Turning the MCP server off"
        ):
            self._sync()        # leave it on and the menu item ticked
            return
        self.set_enabled(checked)
        if checked and self.server is None and self._parent_widget is not None:
            QMessageBox.warning(
                self._parent_widget,
                "MCP Server",
                "The MCP server could not start:\n"
                f"{self._error}\n\nIs another ChatStat window already "
                "serving on this port? The port is the mcp_server_port "
                "setting in app_settings.json.",
            )

    @pyqtSlot(str, str)
    def _on_activity(self, name: str, status: str) -> None:
        parent = self._parent_widget
        text = f"MCP client: {name} {status}"
        logger.info(text)
        if parent is not None and hasattr(parent, "statusBar"):
            parent.statusBar().showMessage(text, 8000)

    # ---- the details dialog --------------------------------------------

    def show_details(self) -> None:
        """Show the address, the token and the Claude Code command."""
        dialog = McpDetailsDialog(self, self._parent_widget)
        dialog.exec()

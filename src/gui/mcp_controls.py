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

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.agent import bridge as agent_bridge
from src.agent.mcp_server import McpToolServer
from src.agent.session_log import SessionLog
from src.agent.tools import ToolRegistry
from src.data.app_settings import (
    get_mcp_server_enabled,
    get_mcp_server_port,
    get_mcp_server_token,
    new_mcp_server_token,
    set_mcp_server_enabled,
)

logger = logging.getLogger(__name__)

__all__ = ["McpServerControl", "claude_code_command"]

#: The name clients list the server under.
CLIENT_NAME = "chatstat"


def claude_code_command(url: str, token: str) -> str:
    """The ``claude mcp add`` command that connects Claude Code."""
    return (
        f"claude mcp add --transport http {CLIENT_NAME} {url} "
        f'--header "Authorization: Bearer {token}"'
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
        refusal: Callable[[], Optional[str]],
        version: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        """Set up the controls; the server starts only if the setting is on.

        Args:
            registry: The in-app agent's registry (served as it is).
            session_log: The in-app agent's session log.
            refusal: Runs on the GUI thread before each MCP call that is not
                read-only; returns a refusal message, or None to go ahead.
            version: Application version reported to clients.
            parent: The main window.
        """
        super().__init__(parent)
        self._registry = registry
        self._log = session_log
        self._refusal = refusal
        self._version = version
        self._parent_widget = parent
        self._server: Optional[McpToolServer] = None
        self._error: Optional[str] = None

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

        if get_mcp_server_enabled():
            self._start()
        self._sync()

    # ---- state ----------------------------------------------------------

    @property
    def server(self) -> Optional[McpToolServer]:
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

    # ---- internals ------------------------------------------------------

    async def _driver_check(self) -> Optional[str]:
        return await agent_bridge.run_on_gui(self._refusal)

    def _start(self, port: Optional[int] = None) -> None:
        if self.server is not None:
            return
        self._error = None
        try:
            server = McpToolServer(
                self._registry,
                get_mcp_server_token(),
                port=get_mcp_server_port() if port is None else port,
                session_log=self._log,
                driver_check=self._driver_check,
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
        if url:
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
        self.state_changed.emit()

    @pyqtSlot(bool)
    def _on_toggled(self, checked: bool) -> None:
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
        dialog = _McpDetailsDialog(self, self._parent_widget)
        dialog.exec()


class _McpDetailsDialog(QDialog):
    """Address, token (hidden until shown), and the client command."""

    def __init__(self, control: McpServerControl, parent: Optional[QWidget]) -> None:
        super().__init__(parent)
        self._control = control
        self.setWindowTitle("MCP Server")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "An MCP client on this computer, such as Claude Code, can use the "
            "same tools as the agent in this window, under the same limits. "
            "The server listens on 127.0.0.1 only and needs the access token "
            "below. It is off until you turn it on."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.enabled_box = QCheckBox("Serve the agent tools over MCP")
        self.enabled_box.toggled.connect(self._on_enabled)
        layout.addWidget(self.enabled_box)
        self.state_label = QLabel()
        self.state_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.state_label)

        token_row = QHBoxLayout()
        token_row.addWidget(QLabel("Access token:"))
        self.token_edit = QLineEdit()
        self.token_edit.setReadOnly(True)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        token_row.addWidget(self.token_edit, stretch=1)
        self.show_button = QPushButton("Show")
        self.show_button.setCheckable(True)
        self.show_button.toggled.connect(self._on_show)
        token_row.addWidget(self.show_button)
        self.new_token_button = QPushButton("New Token")
        self.new_token_button.setToolTip(
            "Make a new token; clients using the old one stop working"
        )
        self.new_token_button.clicked.connect(self._on_new_token)
        token_row.addWidget(self.new_token_button)
        layout.addLayout(token_row)

        self.copy_button = QPushButton("Copy Claude Code Command")
        self.copy_button.setToolTip(
            "Copies 'claude mcp add ...' with the address and the token"
        )
        self.copy_button.clicked.connect(self._on_copy)
        layout.addWidget(self.copy_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self) -> None:
        url = self._control.url
        self.enabled_box.blockSignals(True)
        self.enabled_box.setChecked(url is not None)
        self.enabled_box.blockSignals(False)
        self.state_label.setText(
            f"Serving at {url}" if url else "Off: no client can connect."
        )
        self.token_edit.setText(get_mcp_server_token() if url else "")
        self.copy_button.setEnabled(url is not None)
        self.new_token_button.setEnabled(url is not None)
        self.show_button.setEnabled(url is not None)

    def command(self) -> Optional[str]:
        """The Claude Code command for the running server, or None."""
        url = self._control.url
        return claude_code_command(url, get_mcp_server_token()) if url else None

    @pyqtSlot(bool)
    def _on_enabled(self, checked: bool) -> None:
        self._control.set_enabled(checked)
        self._refresh()

    @pyqtSlot(bool)
    def _on_show(self, shown: bool) -> None:
        self.token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )
        self.show_button.setText("Hide" if shown else "Show")

    @pyqtSlot()
    def _on_copy(self) -> None:
        command = self.command()
        if command:
            QGuiApplication.clipboard().setText(command)

    @pyqtSlot()
    def _on_new_token(self) -> None:
        answer = QMessageBox.question(
            self,
            "New Token",
            "Make a new access token? Clients set up with the old one stop "
            "working until you give them the new one.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        new_mcp_server_token()
        # The running server checks the token it started with: restart it.
        self._control.set_enabled(False)
        self._control.set_enabled(True)
        self._refresh()

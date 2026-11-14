"""File > MCP Server Details...: address, access token, client command.

The token is hidden until Show is pressed. Copy Claude Code Command puts the
ready ``claude mcp add`` line on the clipboard. New Token makes a new token
and restarts the server (asking first, and saying so when a call would be
stopped). Off and on go through :class:`McpServerControl`, which asks before
stopping a running MCP call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QGuiApplication
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

from src.data.app_settings import get_mcp_server_token, new_mcp_server_token

if TYPE_CHECKING:
    from src.gui.mcp_controls import McpServerControl

__all__ = ["CLIENT_NAME", "McpDetailsDialog", "claude_code_command"]

#: The name clients list the server under.
CLIENT_NAME = "chatstat"


def claude_code_command(url: str, token: str) -> str:
    """The ``claude mcp add`` command that connects Claude Code."""
    return (
        f"claude mcp add --transport http {CLIENT_NAME} {url} "
        f'--header "Authorization: Bearer {token}"'
    )


class McpDetailsDialog(QDialog):
    """Address, token (hidden until shown), and the client command."""

    def __init__(self, control: "McpServerControl", parent: Optional[QWidget]) -> None:
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
        if not checked and not self._control.confirm_stopping_calls(
            "Turning the MCP server off"
        ):
            self._refresh()
            return
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
        calls = self._control.running_calls()
        running = (
            f" The server restarts, which stops the MCP call that is running "
            f"now ({', '.join(calls)}): a running measurement is aborted and "
            "a cell-switch prompt is taken down."
            if calls
            else ""
        )
        answer = QMessageBox.question(
            self,
            "New Token",
            "Make a new access token? Clients set up with the old one stop "
            "working until you give them the new one." + running,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        new_mcp_server_token()
        # The running server checks the token it started with: restart it.
        self._control.set_enabled(False)
        self._control.set_enabled(True)
        self._refresh()

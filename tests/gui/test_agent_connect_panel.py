"""The Connection panel and status bar follow a connect the agent made.

The agent (in the dock or over MCP) opens the port with connect_device,
not with the Connect button. The window gives the adapter a callback for
the real instrument only; the mock connection leaves the panel alone.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent import bridge, session_log  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    bridge.install()
    yield app


def _window(monkeypatch, tmp_path, mock: bool) -> MainWindow:
    if mock:
        monkeypatch.setenv("EMSTAT_AGENT_MOCK", "1")
    else:
        monkeypatch.delenv("EMSTAT_AGENT_MOCK", raising=False)
    monkeypatch.setattr(
        session_log, "SESSION_LOG_DIR", str(tmp_path / "sessions")
    )
    return MainWindow()


class _Connected:
    is_connected = True
    firmware_version = "espico1601"


class _Closed:
    is_connected = False
    firmware_version = None


def test_the_panel_follows_the_real_connection(qapp, monkeypatch, tmp_path):
    win = _window(monkeypatch, tmp_path, mock=False)
    try:
        callback = win._agent_adapter._on_connection_changed
        assert callback == win._sync_connection_panel
        win._connection = _Connected()
        callback()
        assert win._conn_panel._status_label.text() == "Connected"
        assert "espico1601" in win._conn_panel._firmware_label.text()
        assert win._status_conn.text() == "Connected"
        win._connection = _Closed()
        callback()
        assert win._conn_panel._status_label.text() == "Disconnected"
        assert win._status_conn.text() == "Disconnected"
    finally:
        win.close()


def test_an_unchanged_state_resets_nothing(qapp, monkeypatch, tmp_path):
    win = _window(monkeypatch, tmp_path, mock=False)
    try:
        win._connection = _Closed()
        win._status_channel.setText("CH: 3")
        win._sync_connection_panel()
        assert win._status_channel.text() == "CH: 3"
    finally:
        win.close()


def test_the_mock_connection_leaves_the_panel_alone(qapp, monkeypatch, tmp_path):
    win = _window(monkeypatch, tmp_path, mock=True)
    try:
        assert win._agent_adapter._on_connection_changed is None
    finally:
        win.close()

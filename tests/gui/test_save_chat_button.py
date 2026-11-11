"""The dock's "Save chat to memory" button.

Headless (offscreen Qt) with a fake worker: no thread, no model, no
instrument. The tools behind the button are tested in
``tests/agent/test_memory_tools.py``.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent.memory_tools import (  # noqa: E402
    SAVE_CHAT_LABEL,
    SAVE_CHAT_MESSAGE,
    build_memory_tools,
)
from src.agent.tools import ToolRegistry  # noqa: E402
from src.gui.agent_dock import AgentDockPanel  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


class FakeWorker(QObject):
    """The signal surface the dock wires, and a record of what was sent."""

    agent_text_delta = pyqtSignal(str)
    tool_call_started = pyqtSignal(object)
    tool_call_finished = pyqtSignal(object)
    tool_call_error = pyqtSignal(object)
    agent_turn_started = pyqtSignal()
    agent_turn_done = pyqtSignal()
    agent_error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.started = 0

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self.started > 0

    def start(self) -> None:
        self.started += 1

    def is_busy(self) -> bool:
        return False

    def submit_user_message(self, text: str) -> None:
        self.sent.append(text)

    def request_stop(self) -> None:
        pass


def _panel(tmp_path, with_memory=True):
    registry = ToolRegistry()
    if with_memory:
        for tool_def, handler in build_memory_tools(lambda: str(tmp_path)):
            registry.register(tool_def, handler)
    worker = FakeWorker()
    panel = AgentDockPanel(registry, worker_factory=lambda *_a: worker)
    return panel, worker


def _say(panel, text: str) -> None:
    panel._input_edit.setText(text)  # noqa: SLF001
    panel._send_button.click()  # noqa: SLF001


def test_the_button_is_in_the_dock(qapp, tmp_path) -> None:
    panel, _ = _panel(tmp_path)
    button = panel._memory_button  # noqa: SLF001
    assert button.text() == SAVE_CHAT_LABEL == "Save chat to memory"
    assert button.isEnabled()
    assert "memory folder" in button.toolTip()


def test_an_empty_chat_has_nothing_to_save(qapp, tmp_path) -> None:
    panel, worker = _panel(tmp_path)
    panel._memory_button.click()  # noqa: SLF001
    assert worker.sent == [] and worker.started == 0
    assert "[Nothing to save yet: the chat is empty]" in panel.transcript_text()


def test_after_a_chat_the_button_sends_the_save_request(qapp, tmp_path) -> None:
    panel, worker = _panel(tmp_path)
    _say(panel, "Measure channel 3.")
    panel._memory_button.click()  # noqa: SLF001
    assert worker.sent == ["Measure channel 3.", SAVE_CHAT_MESSAGE]
    # The chat shows the short label, not the whole request.
    transcript = panel.transcript_text()
    assert SAVE_CHAT_LABEL in transcript
    assert "Call save_memory once" not in transcript


def test_the_button_is_off_while_the_agent_is_working(qapp, tmp_path) -> None:
    panel, worker = _panel(tmp_path)
    _say(panel, "Measure channel 3.")
    worker.agent_turn_started.emit()
    assert not panel._memory_button.isEnabled()  # noqa: SLF001
    panel._on_save_memory_clicked()  # noqa: SLF001 - a click that slips through
    assert worker.sent == ["Measure channel 3."]
    worker.agent_turn_done.emit()
    assert panel._memory_button.isEnabled()  # noqa: SLF001


def test_without_the_memory_tools_the_button_says_so(qapp, tmp_path) -> None:
    panel, worker = _panel(tmp_path, with_memory=False)
    _say(panel, "Measure channel 3.")
    panel._memory_button.click()  # noqa: SLF001
    assert worker.sent == ["Measure channel 3."]
    assert "[Memory notes are not available here]" in panel.transcript_text()


def test_after_shutdown_the_button_does_nothing(qapp, tmp_path) -> None:
    panel, worker = _panel(tmp_path)
    _say(panel, "Measure channel 3.")
    panel.shutdown(wait_ms=10)
    panel._memory_button.click()  # noqa: SLF001
    assert worker.sent == ["Measure channel 3."]


def test_the_application_offers_the_memory_tools_and_the_folder_menu(
    qapp, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("EMSTAT_AGENT_MOCK", "1")
    window = MainWindow()
    try:
        names = window._agent_registry.names()  # noqa: SLF001
        for name in ("save_memory", "list_memories", "read_memory"):
            assert name in names
        actions = [a.text() for a in window.menuBar().actions()[0].menu().actions()]
        assert "Agent &Memory Folder..." in actions
    finally:
        window.close()

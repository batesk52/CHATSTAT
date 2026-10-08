"""Agent configuration lives in settings, not the chat panel.

Covers the move of API key + model out of the AgentDockPanel header into
the File > Agent Settings dialog: app-settings persistence round-trips,
the dialog reports edited values, the main-window handler persists and
pushes them to the running worker, and the chat panel itself no longer
carries config fields.
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots headless (CI / WSL).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

from src.data.app_settings import (  # noqa: E402
    get_agent_api_key,
    get_agent_model,
    set_agent_api_key,
    set_agent_model,
)
import src.gui.main_window as main_window_mod  # noqa: E402
from src.gui.agent_dock import (  # noqa: E402
    AgentDockPanel,
    AgentSettingsDialog,
)
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_agent_settings_round_trip(tmp_path) -> None:
    """Key and model persist and clear through app settings."""
    settings = str(tmp_path / "app_settings.json")

    assert get_agent_api_key(path=settings) is None
    assert get_agent_model(path=settings) is None

    set_agent_api_key("sk-test-123", path=settings)
    set_agent_model("claude-sonnet-4-6", path=settings)
    assert get_agent_api_key(path=settings) == "sk-test-123"
    assert get_agent_model(path=settings) == "claude-sonnet-4-6"

    set_agent_api_key(None, path=settings)  # clear -> env fallback
    assert get_agent_api_key(path=settings) is None


def test_dialog_reports_edited_values(qapp) -> None:
    """The dialog pre-fills current settings and returns edits."""
    dlg = AgentSettingsDialog(api_key="sk-old", model="claude-fable-5")
    key, model = dlg.values()
    assert key == "sk-old"
    assert model == "claude-fable-5"

    dlg._key_edit.setText("  sk-new  ")  # noqa: SLF001
    dlg._model_combo.setCurrentText("claude-sonnet-4-6")  # noqa: SLF001
    key, model = dlg.values()
    assert key == "sk-new"  # stripped
    assert model == "claude-sonnet-4-6"


def test_panel_has_no_config_fields(qapp) -> None:
    """The chat panel no longer carries API-key/model widgets."""

    from PyQt6.QtCore import QObject, pyqtSignal

    class FakeWorker(QObject):
        # Signal surface _connect_worker wires (mirrors AgentWorker).
        agent_text_delta = pyqtSignal(str)
        tool_call_started = pyqtSignal(object)
        tool_call_finished = pyqtSignal(object)
        tool_call_error = pyqtSignal(object)
        agent_turn_started = pyqtSignal()
        agent_turn_done = pyqtSignal()
        agent_error = pyqtSignal(str)

        def __init__(self):
            super().__init__()
            self.api_keys: list = []
            self.models: list = []

        def set_api_key(self, key):
            self.api_keys.append(key)

        def set_model(self, model):
            self.models.append(model)

    from src.agent.tools import ToolRegistry

    worker = FakeWorker()
    captured = {}

    def factory(registry, api_key, model):
        captured["api_key"] = api_key
        captured["model"] = model
        return worker

    panel = AgentDockPanel(
        ToolRegistry(),
        worker_factory=factory,
        api_key="sk-injected",
        model="claude-sonnet-4-6",
    )
    # Config arrives via constructor injection, not panel widgets.
    assert captured == {
        "api_key": "sk-injected",
        "model": "claude-sonnet-4-6",
    }
    assert not hasattr(panel, "_api_key_edit")
    assert not hasattr(panel, "_model_combo")

    # Live updates push straight to the worker.
    panel.set_api_key("sk-next")
    panel.set_model("claude-fable-5")
    assert worker.api_keys == ["sk-next"]
    assert worker.models == ["claude-fable-5"]


def test_chat_view_bubbles_and_transcript(qapp) -> None:
    """ChatView renders bubbles and keeps the You:/Agent: transcript.

    Streaming deltas grow ONE agent bubble in place; a new user message
    after close_agent starts a fresh exchange; text() preserves the
    prefix contract the smoke gates assert on.
    """
    from src.gui.agent_dock import ChatView

    chat = ChatView()
    chat.add_user("run a CV on channels 1,2")
    chat.append_agent("Starting ")
    chat.append_agent("CV now.")
    chat.close_agent()
    chat.add_notice("[Agent stopped by user]")

    assert chat.text() == (
        "You: run a CV on channels 1,2\n"
        "Agent: Starting CV now.\n"
        "[Agent stopped by user]"
    )
    # Two bubbles (user + ONE streamed agent bubble), placeholder gone.
    assert len(chat._bubbles) == 2  # noqa: SLF001
    assert chat._placeholder.isHidden()  # noqa: SLF001

    # After close_agent, new deltas open a NEW bubble.
    chat.append_agent("Second turn.")
    assert len(chat._bubbles) == 3  # noqa: SLF001
    assert chat.text().endswith("Agent: Second turn.")


def test_chat_view_tool_chips_lifecycle(qapp) -> None:
    """Tool chips blink while running, resolve to done, split bubbles."""
    from src.gui.agent_dock import ChatView

    chat = ChatView()
    chat.append_agent("Let me check the device.")
    chat.add_tool_chip("call_1", "run_cv", "{'channels': [1]}")

    # Chip inserted, blink driver running, agent bubble closed.
    assert chat.tool_states() == [
        {"id": "call_1", "name": "run_cv", "status": "running"}
    ]
    assert chat._blink_timer.isActive()  # noqa: SLF001
    # Post-tool deltas open a NEW bubble (text-tool-text reading order).
    chat.append_agent("CV finished cleanly.")
    assert len(chat._bubbles) == 2  # noqa: SLF001

    chat.finish_tool_chip("call_1", "done")
    assert chat.tool_states()[0]["status"] == "done"
    assert not chat._blink_timer.isActive()  # noqa: SLF001

    # Transcript interleaves the tool line between the two bubbles.
    assert chat.text() == (
        "Agent: Let me check the device.\n"
        "[tool run_cv: done]\n"
        "Agent: CV finished cleanly."
    )


def test_agent_bubbles_render_markdown(qapp) -> None:
    """Agent markdown renders as rich text; user text stays literal."""
    from PyQt6.QtCore import Qt

    from src.gui.agent_dock import ChatView

    chat = ChatView()
    chat.add_user("**not bold** for users")
    chat.append_agent("**Technique:** CV")
    chat.close_agent()

    user_label, agent_label = chat._bubbles  # noqa: SLF001
    assert user_label.textFormat() == Qt.TextFormat.PlainText
    assert user_label.text() == "**not bold** for users"
    assert agent_label.textFormat() == Qt.TextFormat.RichText
    # Markdown converted: no literal ** markers; bold markup present.
    assert "**" not in agent_label.text()
    assert "font-weight" in agent_label.text()
    # text() still returns the raw markdown (smoke-gate contract).
    assert "Agent: **Technique:** CV" in chat.text()


def test_chat_view_figure_attachments(qapp, monkeypatch) -> None:
    """Figures render as clickable in-chat attachments with a lightbox."""
    from PyQt6.QtGui import QPixmap

    import src.gui.agent_dock as agent_dock_mod
    from src.gui.agent_dock import ChatView

    chat = ChatView()
    pixmap = QPixmap(400, 300)
    pixmap.fill()
    chat.add_figure("CV ch1", "analyze_cv", pixmap)

    assert chat.figure_count() == 1
    thumb = chat.figure_thumb(0)
    assert thumb.pixmap() is not None
    assert thumb.pixmap().width() <= 240  # small preview
    assert "[figure: CV ch1]" in chat.text()

    # Clicking the preview opens the lightbox (stubbed exec).
    opened: list[str] = []

    class StubViewer:
        def __init__(self, pm, title, parent=None):
            opened.append(title)

        def exec(self):
            return 0

    monkeypatch.setattr(agent_dock_mod, "FigureViewer", StubViewer)
    thumb.clicked.emit()
    assert opened == ["CV ch1"]


def test_typing_indicator_lifecycle(qapp) -> None:
    """The cycling-dots indicator tracks the reply-pending states.

    Shown at turn start (slow first token never looks like a hang),
    hidden when text streams or a tool chip takes over as the liveness
    signal, shown again while waiting on the model after a tool result,
    hidden at turn end. Never enters the transcript text.
    """
    from src.gui.agent_dock import ChatView

    chat = ChatView()
    chat.add_user("hi")

    chat.show_typing()
    assert chat.typing
    assert chat._blink_timer.isActive()  # noqa: SLF001
    assert chat._typing_label.text() in (".", "..", "...")  # noqa: SLF001

    # Dots cycle 1 -> 2 -> 3 -> 1.
    seen = []
    for _ in range(4):
        chat._on_blink_tick()  # noqa: SLF001
        seen.append(len(chat._typing_label.text()))  # noqa: SLF001
    assert set(seen) == {1, 2, 3}

    # First streamed text hides it.
    chat.append_agent("Here we go")
    assert not chat.typing

    # Tool flow: chip running (indicator off), then waiting again.
    chat.add_tool_chip("c1", "run_cv", "{}")
    chat.finish_tool_chip("c1", "done")
    chat.show_typing()
    assert chat.typing
    chat.hide_typing()
    assert not chat.typing
    assert not chat._blink_timer.isActive()  # noqa: SLF001

    # Ephemeral: never part of the transcript.
    assert "typing" not in chat.text().lower()
    assert chat.text().startswith("You: hi")


def test_chat_input_enter_sends_shift_enter_newlines(qapp) -> None:
    """ChatInput submits on Enter and newlines on Shift+Enter."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import QEvent

    from src.gui.agent_dock import ChatInput

    box = ChatInput()
    submits: list[bool] = []
    box.submit.connect(lambda: submits.append(True))

    box.setText("hello")
    enter = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.NoModifier,
    )
    box.keyPressEvent(enter)
    assert submits == [True]
    assert box.text() == "hello"  # Enter did not insert a newline

    shift_enter = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ShiftModifier,
    )
    box.keyPressEvent(shift_enter)
    assert submits == [True]  # no extra submit
    assert "\n" in box.text()  # newline inserted

    # QLineEdit-compatible aliases (smoke gates use them).
    box.setText("multi\nline")
    assert box.text() == "multi\nline"
    # Sized for ~3-5 lines, not a single cramped row.
    assert box.minimumHeight() > 2 * box.fontMetrics().lineSpacing()


def test_handler_persists_and_pushes(qapp, monkeypatch) -> None:
    """Accepting the dialog persists settings and updates the worker."""
    saved = {}
    monkeypatch.setattr(
        main_window_mod,
        "set_agent_api_key",
        lambda key: saved.update(key=key),
    )
    monkeypatch.setattr(
        main_window_mod,
        "set_agent_model",
        lambda model: saved.update(model=model),
    )

    class StubDialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self):
            return "sk-from-dialog", "claude-sonnet-4-6"

    monkeypatch.setattr(
        main_window_mod, "AgentSettingsDialog", StubDialog
    )

    window = MainWindow()
    try:
        pushed = {}
        monkeypatch.setattr(
            window._agent_panel,  # noqa: SLF001
            "set_api_key",
            lambda key: pushed.update(key=key),
        )
        monkeypatch.setattr(
            window._agent_panel,  # noqa: SLF001
            "set_model",
            lambda model: pushed.update(model=model),
        )

        window._on_agent_settings()  # noqa: SLF001

        assert saved == {
            "key": "sk-from-dialog",
            "model": "claude-sonnet-4-6",
        }
        assert pushed == {
            "key": "sk-from-dialog",
            "model": "claude-sonnet-4-6",
        }
    finally:
        window.close()

"""Agent dock panel: messenger-style chat, inline tool chips, figures.

``AgentDockPanel`` is the QWidget the MainWindow places inside the
"Agent" QDockWidget.  It owns the :class:`~src.agent.agent_worker.
AgentWorker` lifecycle:

* The worker QObject is constructed in ``__init__`` (via the injectable
  ``worker_factory`` testability seam) so its signals can be connected
  immediately, but its THREAD is started lazily on the first Send --
  the app pays no agent-thread cost until the panel is actually used.
* :meth:`shutdown` (called from ``MainWindow.closeEvent``) requests a
  stop and waits for the thread to finish; it is idempotent.

Threading discipline (architecture.md): every slot in this module runs
on the GUI thread.  Worker signals are connected with the DEFAULT
connection type, which Qt resolves to a queued connection because the
emitting agent thread differs from this widget's thread.  Figures
rendered by the analysis tools cross the thread boundary through
:class:`FigureSink` -- the agent-side callable is just the sink's
``figure_ready.emit``, and the queued slot turns the PNG bytes into a
pixmap on the GUI thread.  The agent thread never touches a widget.
"""

from __future__ import annotations

import html
import logging
import os
import re
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap, QTextDocument
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.agent.agent_worker import DEFAULT_MODEL, AgentWorker
from src.agent.session_log import SessionLog
from src.agent.tools import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "AgentDockPanel",
    "AgentSettingsDialog",
    "ChatView",
    "FigureSink",
]

#: Models offered in the picker (default first).
AGENT_MODELS = [
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
]

#: Longest tool input/result preview shown on a card.
_CARD_PREVIEW_CHARS = 160

#: Displayed width of analysis figures (pixels; aspect preserved).
_FIGURE_WIDTH = 420

# Oldest figures are dropped beyond this count so a long lab session
# cannot grow the figure strip (and its pixmaps) without bound.
_MAX_FIGURES = 12


class AgentSettingsDialog(QDialog):
    """Modal dialog for the agent's API key and model (File menu).

    Configuration lives here — not in the chat panel — and is persisted
    by the caller (main window) via app settings. An empty key field
    means "use the ``ANTHROPIC_API_KEY`` environment variable".
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the dialog pre-filled with the current settings.

        Args:
            api_key: Currently stored key ("" when unset).
            model: Currently selected model id.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle("Agent Settings")
        form = QFormLayout(self)

        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText(
            "blank = use ANTHROPIC_API_KEY env var"
        )
        self._key_edit.setText(api_key)
        self._key_edit.setMinimumWidth(320)
        form.addRow("API key:", self._key_edit)

        self._model_combo = QComboBox()
        self._model_combo.addItems(AGENT_MODELS)
        if model and self._model_combo.findText(model) < 0:
            self._model_combo.addItem(model)
        self._model_combo.setCurrentText(model or DEFAULT_MODEL)
        form.addRow("Model:", self._model_combo)

        note = QLabel(
            "Stored in ~/.emstat_pico_mux16/app_settings.json "
            "(plain text, same trust level as a .env file)."
        )
        note.setStyleSheet("color: #888; font-size: 11px;")
        note.setWordWrap(True)
        form.addRow(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, str]:
        """Return ``(api_key, model)`` as currently edited."""
        return (
            self._key_edit.text().strip(),
            self._model_combo.currentText(),
        )


class ChatInput(QPlainTextEdit):
    """Multi-line chat input: Enter sends, Shift+Enter inserts a newline.

    Sized to ~3 text lines so longer prompts are comfortable to write
    and review (the single-line field was too cramped). Exposes
    ``text``/``setText`` aliases so callers written against QLineEdit
    keep working.
    """

    submit = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTabChangesFocus(True)
        line = self.fontMetrics().lineSpacing()
        self.setMinimumHeight(int(line * 3) + 14)
        self.setMaximumHeight(int(line * 5) + 14)

    def text(self) -> str:
        """QLineEdit-compatible accessor."""
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        """QLineEdit-compatible setter."""
        self.setPlainText(text)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submit.emit()
            return
        super().keyPressEvent(event)


class _ClickableLabel(QLabel):
    """QLabel that emits ``clicked`` on a left-button press."""

    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class FigureViewer(QDialog):
    """Full-size figure lightbox: Escape or a click closes it."""

    def __init__(
        self,
        pixmap: QPixmap,
        title: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet("background-color: #1c1c1c;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Fit within ~90% of the available screen, never upscale.
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            max_w = int(avail.width() * 0.9)
            max_h = int(avail.height() * 0.9)
            if pixmap.width() > max_w or pixmap.height() > max_h:
                pixmap = pixmap.scaled(
                    max_w,
                    max_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        label = QLabel()
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        hint = QLabel("Esc or click to close")
        hint.setStyleSheet("color: #777; font-size: 10px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.accept()  # click anywhere closes; Esc closes via QDialog


#: One pass over fenced code, inline code, display math and inline math.
#:
#: Order is significant -- alternation is first-match-wins, so the two code
#: forms are listed FIRST and therefore win over any ``$`` inside them.
#: Matching code here (rather than substituting it out for a placeholder and
#: splicing it back afterwards) is deliberate: a placeholder has to survive
#: ``QTextDocument.setMarkdown()``, and NUL-delimited tokens do NOT -- Qt
#: rewrites them to U+FFFD, restoration silently no-ops, and every code span
#: renders as garbage. Letting code spans fall through as real markdown also
#: keeps Qt's own escaping and monospace styling, instead of re-inserting raw
#: text into serialized HTML where a ``<`` in code would corrupt the bubble.
#:
#: Display math may span lines but NOT a blank line, and must not have a
#: plain space or tab immediately inside either delimiter. Both guards exist
#: because non-greedy matching finds the NEAREST following ``$$``, so two
#: literal ``$$`` in one message would otherwise bracket ordinary prose and
#: mathtext would happily rasterize it into a formula-styled image ("this is
#: $$ moderately priced $$ gear"). Real display math is written ``$$x=1$$``
#: or with a NEWLINE after the delimiter, never ``$$ prose $$`` -- so
#: rejecting space/tab (but allowing newline) keeps the block form working.
_MATH_RE = re.compile(
    r"(?P<fence>```.*?```)"
    r"|(?P<code>`[^`\n]+`)"
    r"|\$\$(?![ \t])(?P<display>(?:[^\n]|\n(?!\s*\n))+?)(?<![ \t])\$\$"
    r"|(?<![\w$])\$(?P<inline>[^$\n]+?)\$(?![\w$])",
    re.DOTALL,
)

#: Rendered-formula cache: (latex, display, colour) -> data-URI ``<img>``.
#: Rendering is ~10 ms per formula and the same expressions recur as a
#: bubble is re-rendered on every streaming delta, so this matters.
_MATH_CACHE: dict[tuple[str, bool, str], str] = {}

#: Give up on absurd input rather than hand it to the parser.
_MATH_MAX_LEN = 400


def _render_math_png(latex: str, display: bool, colour: str) -> Optional[str]:
    """Render one LaTeX span to an inline ``<img>`` data URI.

    Uses matplotlib's mathtext (already a dependency, Agg backend forced by
    the vendored analysis layer) rather than a TeX install.  mathtext covers
    the subset an electrochemistry chat actually produces -- fractions,
    sub/superscripts, Greek, roots, common operators.

    Threading note: this runs on the GUI thread, while
    ``vendor_analysis`` drives matplotlib's pyplot API from the agent
    QThread.  matplotlib asks to be confined to one thread, so the two are
    kept apart by discipline rather than a lock: this function uses the
    object-oriented ``Figure``/``FigureCanvasAgg`` API and never touches
    pyplot's global current-figure state, and backend selection happens
    single-threaded at import time before the agent thread starts.

    Cost: ~6 ms per distinct formula once warm, and cache hits are free --
    but the FIRST call in a process pays ~325 ms building matplotlib's font
    cache.  :func:`warm_up_math_renderer` moves that off the interactive
    path; see its call site in ``main_window``.

    Args:
        latex: The formula body, without the surrounding ``$``.
        display: True for ``$$...$$`` (block, larger); False for inline.
        colour: Text colour to match the surrounding bubble.

    Returns:
        An ``<img>`` tag, or None if the formula could not be rendered --
        in which case the caller must leave the original text untouched
        rather than silently dropping it.
    """
    key = (latex, display, colour)
    cached = _MATH_CACHE.get(key)
    if cached is not None:
        return cached
    if not latex.strip() or len(latex) > _MATH_MAX_LEN:
        return None
    try:
        import base64
        import io

        from matplotlib import figure as _mpl_figure
        from matplotlib.backends.backend_agg import (
            FigureCanvasAgg as _Canvas,
        )

        fig = _mpl_figure.Figure(figsize=(0.01, 0.01), dpi=110)
        _Canvas(fig)
        fig.patch.set_alpha(0.0)
        fig.text(
            0, 0, f"${latex}$",
            fontsize=13 if display else 11,
            color=colour,
        )
        buf = io.BytesIO()
        # bbox_inches="tight" crops to the formula's own extent, so the
        # image sits on the text baseline instead of in a large blank box.
        fig.savefig(
            buf, format="png", transparent=True,
            bbox_inches="tight", pad_inches=0.02,
        )
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 - bad LaTeX must never break the chat
        # exc_info matters: without it a systemic failure (broken
        # matplotlib, unwritable font cache) is indistinguishable from
        # "that one formula was bad LaTeX", and math silently stays off.
        logger.debug(
            "mathtext render failed for %r", latex[:60], exc_info=True
        )
        return None
    style = (
        "display:block;margin:6px auto;"
        if display
        else "vertical-align:middle;"
    )
    html = f'<img src="data:image/png;base64,{encoded}" style="{style}"/>'
    _MATH_CACHE[key] = html
    return html


def warm_up_math_renderer() -> None:
    """Pre-build matplotlib's font cache so the first formula isn't slow.

    The first mathtext render in a process costs ~325 ms (font-cache and
    mathtext setup); every later one is ~6 ms.  Paid lazily that lands as a
    visible freeze the first time an agent reply contains math.  Calling
    this once at startup moves the cost somewhere nobody is waiting on it.

    Safe to call more than once (the second is a cache hit) and safe to
    fail -- a broken render here is not worth blocking startup for.
    """
    try:
        _render_math_png("x", False, "#1a1a1a")
    except Exception:  # noqa: BLE001 - never block startup
        logger.debug("math renderer warm-up failed", exc_info=True)


def _markdown_to_html(text: str, math_colour: str = "#1a1a1a") -> str:
    """Convert agent markdown to HTML via Qt's built-in parser.

    QTextDocument understands GitHub-flavored basics (bold, italics,
    lists, headers, inline/fenced code) — enough for the agent's
    summaries to render formatted instead of showing literal ``**``
    markers. No external dependency.

    LaTeX math (``$...$`` / ``$$...$$``) is rendered to inline PNGs via
    matplotlib mathtext, since QTextDocument has no math support. Math
    inside fenced or inline code is left alone, and any span that fails to
    render is left as its original text rather than dropped.
    """

    def _sub(match: "re.Match[str]") -> str:
        # Code spans matched first: hand them back untouched so Qt's
        # markdown parser sees real backticks and renders/escapes them.
        if match.group("fence") is not None or match.group("code") is not None:
            return match.group(0)
        display = match.group("display")
        body = display if display is not None else match.group("inline")
        rendered = _render_math_png(
            body.strip(), display is not None, math_colour
        )
        # Unrenderable math stays verbatim -- never silently swallowed.
        return rendered if rendered is not None else match.group(0)

    doc = QTextDocument()
    doc.setMarkdown(_MATH_RE.sub(_sub, text))
    return doc.toHtml()


class ChatView(QScrollArea):
    """Messenger-style transcript: word-wrapped bubbles per message.

    User messages render right-aligned in accent-colored bubbles, agent
    replies left-aligned in neutral bubbles (streaming deltas grow the
    open agent bubble in place), and notices (errors, stop) render as
    centered captions. Bubble text is mouse-selectable for copy/paste.
    """

    # Bubble fill colors (dark theme).
    _USER_STYLE = (
        "QLabel { background-color: #2d5a88; color: #f0f0f0;"
        " border-radius: 9px; padding: 7px 10px; }"
    )
    _AGENT_STYLE = (
        "QLabel { background-color: #3a3a3a; color: #e8e8e8;"
        " border-radius: 9px; padding: 7px 10px; }"
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        # (role, text) per entry — also the source for text().
        self._entries: list[tuple[str, str]] = []
        self._bubbles: list[QLabel] = []
        self._open_agent: Optional[QLabel] = None
        # Tool chips by call id: {"label", "name", "preview", "status"}.
        self._chips: dict[str, dict[str, Any]] = {}
        # Figure attachments: thumbnail labels + retained pixmaps for
        # the lightbox (full resolution for the newest _MAX_FIGURES,
        # thumbnail fallback beyond that to bound memory).
        self._figure_thumbs: list[QLabel] = []
        self._figure_pixmaps: list[QPixmap] = []
        self._figure_titles: list[str] = []
        # Typing indicator: an agent-style bubble with cycling dots,
        # shown while a reply is pending so a slow model never looks
        # like a hang. Ephemeral — never enters _entries / text().
        self._typing_on = False
        self._typing_label = QLabel(".")
        self._typing_label.setStyleSheet(self._AGENT_STYLE)
        typing_row = QHBoxLayout()
        typing_row.setContentsMargins(0, 0, 0, 0)
        typing_row.addWidget(self._typing_label)
        typing_row.addStretch(1)
        self._typing_wrapper = QWidget()
        self._typing_wrapper.setLayout(typing_row)
        self._typing_wrapper.setVisible(False)
        self._layout.insertWidget(
            self._layout.count() - 1, self._typing_wrapper
        )
        # Shared blink driver for running chips (started on demand).
        self._blink_phase = 0
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(400)
        self._blink_timer.timeout.connect(self._on_blink_tick)

        self._placeholder = QLabel(
            "Ask the agent to run measurements or analyze data..."
        )
        self._placeholder.setStyleSheet(
            "color: #777; font-style: italic;"
        )
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.insertWidget(0, self._placeholder)

        # Follow the newest message as content grows.
        self.verticalScrollBar().rangeChanged.connect(
            lambda _lo, hi: self.verticalScrollBar().setValue(hi)
        )

    # ---- message API -------------------------------------------------

    def add_user(self, text: str) -> None:
        """Append a right-aligned user bubble."""
        self._close_open_agent()
        self._add_bubble("You", text)

    def append_agent(self, delta: str) -> None:
        """Stream a delta into the open agent bubble (opens one)."""
        if self._typing_on:
            self.hide_typing()
        if self._open_agent is None:
            self._open_agent = self._add_bubble("Agent", "")
        self._entries[-1] = (
            "Agent",
            self._entries[-1][1] + delta,
        )
        raw = self._entries[-1][1]
        self._open_agent.setProperty("raw", raw)
        self._open_agent.setText(_markdown_to_html(raw))
        self._fit_bubble(self._open_agent)

    def close_agent(self) -> None:
        """Finish the streamed agent bubble (turn end)."""
        self._close_open_agent()

    def add_notice(self, text: str) -> None:
        """Append a centered, muted notice line (errors, stop)."""
        self._close_open_agent()
        self._entries.append(("notice", text))
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #999; font-size: 11px;")
        self._layout.insertWidget(self._layout.count() - 1, label)
        self._placeholder.setVisible(False)

    def text(self) -> str:
        """Plain-text transcript (``You:``/``Agent:`` prefixed lines)."""
        lines = []
        for role, body in self._entries:
            if role == "notice":
                lines.append(body)
            elif role == "tool":
                chip = self._chips.get(body, {})
                lines.append(
                    f"[tool {chip.get('name', '?')}:"
                    f" {chip.get('status', '?')}]"
                )
            elif role == "figure":
                lines.append(f"[figure: {body}]")
            else:
                lines.append(f"{role}: {body}")
        return "\n".join(lines)

    # ---- typing indicator ----------------------------------------------

    def show_typing(self) -> None:
        """Show the cycling-dots bubble (a reply is pending).

        Re-anchored below the newest content so it always reads as
        "the next message is being written".
        """
        self._placeholder.setVisible(False)
        # Keep the indicator last (just above the stretch item).
        self._layout.removeWidget(self._typing_wrapper)
        self._layout.insertWidget(
            self._layout.count() - 1, self._typing_wrapper
        )
        self._typing_on = True
        self._render_typing()
        self._typing_wrapper.setVisible(True)
        if not self._blink_timer.isActive():
            self._blink_timer.start()

    def hide_typing(self) -> None:
        """Hide the indicator (content arrived or the turn ended)."""
        self._typing_on = False
        self._typing_wrapper.setVisible(False)
        if not any(
            c["status"] == "running" for c in self._chips.values()
        ):
            self._blink_timer.stop()

    @property
    def typing(self) -> bool:
        """Whether the typing indicator is currently shown."""
        return self._typing_on

    def _render_typing(self) -> None:
        # 1, 2, 3 dots, then around again.
        self._typing_label.setText("." * (self._blink_phase % 3 + 1))

    # ---- figure attachments --------------------------------------------

    def add_figure(
        self, title: str, tool: str, pixmap: QPixmap
    ) -> None:
        """Append a figure as an agent image attachment.

        Renders a small clickable preview in the flow (left-aligned,
        like a shared image in a messenger); clicking opens the
        full-size figure in a lightbox that closes on Escape or click.
        """
        self._close_open_agent()
        self._placeholder.setVisible(False)

        index = len(self._figure_thumbs)
        thumb_pm = pixmap
        if thumb_pm.width() > 240:
            thumb_pm = thumb_pm.scaledToWidth(
                240, Qt.TransformationMode.SmoothTransformation
            )
        thumb = _ClickableLabel()
        thumb.setPixmap(thumb_pm)
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        thumb.setToolTip("Click to view full size (Esc closes)")
        thumb.setStyleSheet(
            "QLabel { background-color: #3a3a3a; border-radius: 9px;"
            " padding: 6px; }"
        )
        thumb.clicked.connect(lambda i=index: self._show_figure(i))

        caption = QLabel(f"{title}  ({tool})")
        caption.setStyleSheet("color: #909090; font-size: 11px;")

        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        box.addWidget(thumb)
        box.addWidget(caption)
        inner = QWidget()
        inner.setLayout(box)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(inner)
        row.addStretch(1)
        wrapper = QWidget()
        wrapper.setLayout(row)
        self._layout.insertWidget(self._layout.count() - 1, wrapper)

        self._figure_thumbs.append(thumb)
        self._figure_pixmaps.append(pixmap)
        self._figure_titles.append(title)
        self._entries.append(("figure", title))
        # Bound full-resolution retention: older figures fall back to
        # their (still-displayed) thumbnail in the lightbox.
        for i in range(len(self._figure_pixmaps) - _MAX_FIGURES):
            old = self._figure_thumbs[i].pixmap()
            if old is not None:
                self._figure_pixmaps[i] = old

    def figure_count(self) -> int:
        """Number of figure attachments in the flow."""
        return len(self._figure_thumbs)

    def figure_thumb(self, index: int) -> QLabel:
        """The thumbnail label of figure *index* (for tests)."""
        return self._figure_thumbs[index]

    def _show_figure(self, index: int) -> None:
        """Open figure *index* in the lightbox (Esc/click closes)."""
        FigureViewer(
            self._figure_pixmaps[index],
            self._figure_titles[index],
            parent=self,
        ).exec()

    # ---- tool chips (live MCP/tool-call indicators in the flow) --------

    def add_tool_chip(
        self, call_id: str, name: str, preview: str
    ) -> None:
        """Insert a small left-aligned chip for a starting tool call.

        Closes the open agent bubble first, so the agent's text after
        the tool runs starts a FRESH bubble — the conversation reads as
        text, tool activity, text instead of one merged block. The chip
        blinks (animated dots + alternating accent border) while the
        call is running.
        """
        self._close_open_agent()
        self._placeholder.setVisible(False)
        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._chips[call_id] = {
            "label": label,
            "name": name,
            "preview": preview,
            "status": "running",
        }
        self._entries.append(("tool", call_id))
        self._render_chip(call_id)

        row = QHBoxLayout()
        row.setContentsMargins(14, 0, 0, 0)  # slight indent in the flow
        row.addWidget(label)
        row.addStretch(1)
        wrapper = QWidget()
        wrapper.setLayout(row)
        self._layout.insertWidget(self._layout.count() - 1, wrapper)
        if not self._blink_timer.isActive():
            self._blink_timer.start()

    def finish_tool_chip(self, call_id: str, status: str) -> None:
        """Resolve a chip to ``done``/``error`` and stop its blink."""
        chip = self._chips.get(call_id)
        if chip is None:
            return
        chip["status"] = status
        self._render_chip(call_id)
        if not self._typing_on and not any(
            c["status"] == "running" for c in self._chips.values()
        ):
            self._blink_timer.stop()

    def tool_states(self) -> list[dict[str, str]]:
        """Chips as ``{"id", "name", "status"}`` dicts (for tests)."""
        return [
            {"id": cid, "name": c["name"], "status": c["status"]}
            for cid, c in self._chips.items()
        ]

    def _render_chip(self, call_id: str) -> None:
        chip = self._chips[call_id]
        name = html.escape(str(chip["name"]))
        preview = html.escape(str(chip["preview"]))
        status = chip["status"]
        if status == "running":
            dots = "." * (self._blink_phase % 4)
            tail = f"<span style='color:#7aa2c9;'>running{dots}</span>"
            border = (
                "#7aa2c9" if self._blink_phase % 2 == 0 else "#3a4a5a"
            )
        elif status == "done":
            tail = "<span style='color:#5cb85c;'>done</span>"
            border = "#4a4a4a"
        else:
            tail = f"<span style='color:#d9534f;'>{status}</span>"
            border = "#6a3a3a"
        chip["label"].setStyleSheet(
            "QLabel { background-color: #262626;"
            f" border: 1px solid {border};"
            " border-radius: 6px; padding: 4px 8px;"
            " color: #c8c8c8; font-size: 11px; }"
        )
        chip["label"].setText(
            f"<span style='color:#9fc5e8;'>{name}</span>"
            f" <span style='color:#888;'>{preview}</span>  {tail}"
        )

    def _on_blink_tick(self) -> None:
        self._blink_phase += 1
        for cid, chip in self._chips.items():
            if chip["status"] == "running":
                self._render_chip(cid)
        if self._typing_on:
            self._render_typing()

    # ---- internals -----------------------------------------------------

    def _add_bubble(self, role: str, text: str) -> QLabel:
        self._placeholder.setVisible(False)
        self._entries.append((role, text))
        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        label.setStyleSheet(
            self._USER_STYLE if role == "You" else self._AGENT_STYLE
        )
        # Agent replies are markdown (bold, lists, code, headers) and
        # render as rich text; user input stays literal plain text.
        label.setProperty("raw", text)
        if role == "Agent":
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setText(_markdown_to_html(text))
        else:
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setText(text)
        self._bubbles.append(label)
        self._fit_bubble(label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if role == "You":
            row.addStretch(1)
            row.addWidget(label)
        else:
            row.addWidget(label)
            row.addStretch(1)
        wrapper = QWidget()
        wrapper.setLayout(row)
        self._layout.insertWidget(self._layout.count() - 1, wrapper)
        return label

    def _close_open_agent(self) -> None:
        self._open_agent = None

    def _bubble_max_width(self) -> int:
        """Cap bubbles at ~82% of the viewport (min floor for docks)."""
        return max(240, int(self.viewport().width() * 0.82))

    def _fit_bubble(self, label: QLabel) -> None:
        """Size a bubble to its content, up to the width cap.

        A word-wrapped QLabel left to the layout picks a narrow
        preferred width (user messages wrapped at ~half the panel);
        fixing the width to the text's natural extent (plus padding)
        makes bubbles hug short messages and expand to the cap for long
        ones — proper messenger behavior for BOTH roles.
        """
        cap = self._bubble_max_width()
        metrics = label.fontMetrics()
        raw = label.property("raw") or ""
        natural = max(
            (
                metrics.horizontalAdvance(line)
                for line in str(raw).splitlines()
            ),
            default=0,
        )
        # Stylesheet padding (10px x2) + border allowance.
        label.setFixedWidth(min(natural + 26, cap))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Re-fit every bubble when the dock is resized."""
        super().resizeEvent(event)
        for label in self._bubbles:
            self._fit_bubble(label)


class FigureSink(QObject):
    """Thread-safe funnel for analysis figures into the GUI.

    The analysis tool handlers run on the agent thread and call the
    ``figure_sink`` callable with ``{"title", "tool", "png"}`` dicts.
    That callable is this object's ``figure_ready.emit`` -- emitting a
    Qt signal is thread-safe, and the panel's queued slot renders the
    pixmap on the GUI thread.
    """

    figure_ready = pyqtSignal(object)  # {"title": str, "tool": str, "png": bytes}


def _preview(value: Any) -> str:
    """One-line, length-capped rendering for card labels."""
    text = str(value)
    text = " ".join(text.split())
    if len(text) > _CARD_PREVIEW_CHARS:
        text = text[: _CARD_PREVIEW_CHARS - 3] + "..."
    return text


class AgentDockPanel(QWidget):
    """Chat panel for the embedded Claude agent (dock content).

    Layout, top to bottom:

    * Splitter with the bubble transcript (tool calls render inline as
      blinking chips) and the figure strip.
    * Input row: message field + Send (Enter sends) + Stop.  Input is
      disabled while a turn is in flight.

    API key and model are CONFIGURATION, not chat UI: they live in the
    File > Agent Settings dialog (persisted in app settings) and are
    injected here at construction, with live updates pushed via
    :meth:`set_api_key` / :meth:`set_model`.

    The panel owns the AgentWorker: it is constructed here (through
    ``worker_factory`` for testability) and its thread starts lazily on
    the first send.  Call :meth:`shutdown` before the application
    closes.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        worker_factory: Optional[
            Callable[[ToolRegistry, Optional[str], str], AgentWorker]
        ] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        session_log: Optional[SessionLog] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """Build the panel and its (not yet started) worker.

        Args:
            registry: Tool registry handed to the worker.  Analysis
                tools may still be registered into it after
                construction (the worker reads ``tool_defs`` per
                request).
            worker_factory: Testability seam called as
                ``worker_factory(registry, api_key, model)`` and
                returning an :class:`AgentWorker`-compatible object.
                Default builds the real worker.
            api_key: Anthropic API key, or ``None`` to fall back to the
                ``ANTHROPIC_API_KEY`` environment variable (the SDK
                reads it automatically).
            model: Model id; ``None`` uses :data:`DEFAULT_MODEL`.
            session_log: Optional per-session JSON Lines log handed to
                the default worker (ignored with ``worker_factory``).
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self._registry = registry
        self._shutdown_done = False


        self._sink = FigureSink(self)
        self._sink.figure_ready.connect(self._on_figure_ready)

        self._build_ui()

        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or None
        model = model or DEFAULT_MODEL
        if worker_factory is None:
            self._worker: AgentWorker = AgentWorker(
                registry,
                api_key=api_key,
                model=model,
                session_log=session_log,
            )
        else:
            self._worker = worker_factory(registry, api_key, model)
        self._connect_worker(self._worker)

    # ---- configuration (pushed from the Agent Settings dialog) -----------

    def set_api_key(self, key: Optional[str]) -> None:
        """Push a new API key to the worker (takes effect next turn)."""
        self._worker.set_api_key(key or None)

    def set_model(self, model: str) -> None:
        """Push a new model id to the worker (takes effect next turn)."""
        if model:
            self._worker.set_model(model)

    # ---- UI construction ------------------------------------------------------

    def _build_ui(self) -> None:
        """Create all child widgets (GUI thread, construction time)."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # One surface: the conversation. Tool activity renders inline
        # as chips and figures as image attachments, so no splitter or
        # side strips are needed.
        self._chat = ChatView()
        layout.addWidget(self._chat, stretch=1)

        # Input area: full-width text box with the buttons BENEATH it,
        # so the box gets the whole dock width for writing.
        self._input_edit = ChatInput()
        self._input_edit.setPlaceholderText(
            "Message the agent...  (Enter sends, Shift+Enter = newline)"
        )
        self._input_edit.submit.connect(self._on_send_clicked)
        layout.addWidget(self._input_edit)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._send_button = QPushButton("Send")
        self._send_button.clicked.connect(self._on_send_clicked)
        button_row.addWidget(self._send_button)
        self._stop_button = QPushButton("Stop")
        self._stop_button.setToolTip(
            "Cancel the in-flight turn. Any running measurement is "
            "aborted; the agent stays available for further messages."
        )
        self._stop_button.clicked.connect(self._on_stop_clicked)
        button_row.addWidget(self._stop_button)
        layout.addLayout(button_row)

    def _connect_worker(self, worker: AgentWorker) -> None:
        """Connect worker signals (default = queued across threads)."""
        worker.agent_text_delta.connect(self._on_text_delta)
        worker.tool_call_started.connect(self._on_tool_call_started)
        worker.tool_call_finished.connect(self._on_tool_call_finished)
        worker.tool_call_error.connect(self._on_tool_call_error)
        worker.agent_turn_started.connect(self._on_turn_started)
        worker.agent_turn_done.connect(self._on_turn_done)
        worker.agent_error.connect(self._on_agent_error)

    # ---- Public surface (MainWindow / analysis tools / tests) ------------------

    @property
    def worker(self) -> AgentWorker:
        """The owned AgentWorker (e.g. for app-close request_stop)."""
        return self._worker

    @property
    def figure_sink(self) -> Callable[[dict[str, Any]], None]:
        """Agent-thread-safe callable for ``build_analysis_tools``.

        The analysis handlers build the ``{"title", "tool", "png"}``
        dict themselves; the payload is shallow-copied at this boundary
        so no dict is ever shared by reference across threads.
        """
        emit = self._sink.figure_ready.emit

        def _sink_callable(payload: dict[str, Any]) -> None:
            emit(dict(payload))

        return _sink_callable

    def shutdown(self, wait_ms: int = 5000) -> None:
        """Stop the worker thread and wait for it (idempotent).

        Called from ``MainWindow.closeEvent`` before the window goes
        away so the agent thread never outlives the GUI objects its
        tools marshal onto.

        Args:
            wait_ms: Maximum milliseconds to wait for the thread.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        worker = self._worker
        try:
            worker.request_stop()
            if worker.isRunning() and not worker.wait(wait_ms):
                logger.warning(
                    "Agent worker did not stop within %d ms.", wait_ms
                )
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("Agent worker shutdown failed.")

    # Introspection helpers used by the smoke tests (GUI thread only).

    def transcript_text(self) -> str:
        """Full transcript text (for tests)."""
        return self._chat.text()

    def tool_card_states(self) -> list[dict[str, str]]:
        """Tool chips as ``{"id", "name", "status"}`` dicts (for tests).

        Name kept from the card era so the smoke gates' assertions hold;
        tool activity now renders as inline chat chips.
        """
        return self._chat.tool_states()

    def figure_count(self) -> int:
        """Number of figure attachments in the chat."""
        return self._chat.figure_count()

    def figure_label(self, index: int) -> QLabel:
        """The thumbnail QLabel of figure *index* (for tests)."""
        return self._chat.figure_thumb(index)

    # ---- GUI-thread slots -------------------------------------------------------

    @pyqtSlot()
    def _on_send_clicked(self) -> None:
        """Echo the user message and submit it to the worker."""
        if self._shutdown_done:
            return
        text = self._input_edit.text().strip()
        if not text or not self._send_button.isEnabled():
            return
        # Lazy thread start: the agent thread spins up on first use.
        if not self._worker.isRunning():
            self._worker.start()
        self._chat.add_user(text)
        self._input_edit.clear()
        self._worker.submit_user_message(text)

    @pyqtSlot()
    def _on_stop_clicked(self) -> None:
        """Cancel the current turn; the agent stays available.

        This used to call :meth:`shutdown`, which stops the worker thread
        outright -- so pressing Stop once ended the conversation until the
        app was relaunched.  Stop now cancels only the in-flight turn.
        The worker's ``agent_turn_done`` still fires (``_run_turn``'s
        ``finally`` runs on cancellation), which re-enables the input
        through the normal handler; app-close still uses ``shutdown()``.
        """
        if self._shutdown_done:
            return
        if not self._worker.is_busy():
            # Nothing to cancel. Stay silent rather than announce a turn
            # that never existed -- the button is not disabled while idle.
            return
        self._chat.add_notice("[Turn stopped by user]")
        self._set_in_flight(False)
        self._worker.cancel_turn()

    @pyqtSlot(str)
    def _on_text_delta(self, text: str) -> None:
        """Stream one assistant chunk into the open agent bubble."""
        self._chat.append_agent(text)

    @pyqtSlot(object)
    def _on_tool_call_started(self, payload: object) -> None:
        """Add a blinking 'running' chip into the chat flow."""
        # The chip's own animation takes over as the liveness signal.
        self._chat.hide_typing()
        data = dict(payload) if isinstance(payload, dict) else {}
        self._chat.add_tool_chip(
            str(data.get("id", "")),
            str(data.get("name", "?")),
            _preview(data.get("input", {})),
        )

    @pyqtSlot(object)
    def _on_tool_call_finished(self, payload: object) -> None:
        """Resolve the matching chip to done."""
        data = dict(payload) if isinstance(payload, dict) else {}
        self._chat.finish_tool_chip(str(data.get("id", "")), "done")
        # Waiting on the model's follow-up text now.
        self._chat.show_typing()

    @pyqtSlot(object)
    def _on_tool_call_error(self, payload: object) -> None:
        """Resolve the matching chip to error."""
        data = dict(payload) if isinstance(payload, dict) else {}
        self._chat.finish_tool_chip(str(data.get("id", "")), "error")
        self._chat.show_typing()

    @pyqtSlot()
    def _on_turn_started(self) -> None:
        """Disable input and show the typing indicator."""
        self._set_in_flight(True)
        self._chat.show_typing()

    @pyqtSlot()
    def _on_turn_done(self) -> None:
        """Re-enable input and close the streamed bubble."""
        self._chat.hide_typing()
        self._chat.close_agent()
        self._set_in_flight(False)

    @pyqtSlot(str)
    def _on_agent_error(self, message: str) -> None:
        """Show an agent/API error distinctly in the transcript."""
        self._chat.hide_typing()
        self._chat.add_notice(f"[Agent error] {message}")

    @pyqtSlot(object)
    def _on_figure_ready(self, payload: object) -> None:
        """Render a PNG figure payload into the figure strip."""
        data = dict(payload) if isinstance(payload, dict) else {}
        png = data.get("png")
        pixmap = QPixmap()
        if not isinstance(png, bytes) or not pixmap.loadFromData(png):
            logger.warning(
                "Dropping undecodable figure payload from tool %r.",
                data.get("tool"),
            )
            return
        self._chat.add_figure(
            str(data.get("title", "Figure")),
            str(data.get("tool", "?")),
            pixmap,
        )

    # ---- Internals -----------------------------------------------------------------

    def _set_in_flight(self, in_flight: bool) -> None:
        """Toggle the input row for turn-in-flight state."""
        if self._shutdown_done:
            return
        self._send_button.setEnabled(not in_flight)
        self._input_edit.setEnabled(not in_flight)

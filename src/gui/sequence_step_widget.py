"""Expandable, editable sequence-step block (sequencer feature).

Each step in the sequencer is one of these: a collapsed header showing a
one-line summary, expanding on click to a full editor for that step's
embedded config — technique parameters, channels, electrode mode, repeat
and delay. Edits write straight back onto the step's
:class:`~src.data.sequence.SequenceStep`, so the sequence carries the
values (the store is only the seed). Reorder/remove are driven by header
buttons; the panel owns the ordering.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.data.sequence import SequenceStep
from src.gui.parameter_form import ParameterForm

logger = logging.getLogger(__name__)

# All electrode-config modes are selectable per step. Manual (Mode C)
# reveals an RE/CE pairing field — one CE channel per WE channel.
_MODES = ("external", "on_board", "manual")
_MANUAL_MODE = "manual"


def format_channels(channels: list[int]) -> str:
    """Render a channel list as a compact comma string (``"1,4,7"``)."""
    return ",".join(str(c) for c in channels)


def parse_channels(text: str) -> list[int]:
    """Parse ``"1,4"`` / ``"1-16"`` / ``"2,2,2"`` into an ordered int list.

    Order and duplicates are preserved: the same parser serves the WE
    channel list and the position-matched RE/CE pairing (where e.g.
    ``"2,2,2"`` legitimately repeats a channel and order must align with
    the WE list), so neither is silently reordered.

    Raises:
        ValueError: On any non-numeric / malformed token, or a reversed
            range like ``"5-2"`` (which would otherwise silently parse
            to an empty list).
    """
    out: list[int] = []
    for tok in text.replace(" ", "").split(","):
        if not tok:
            continue
        if "-" in tok:
            lo, hi = tok.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            if lo_i > hi_i:
                raise ValueError(f"Reversed channel range: {tok!r}")
            out.extend(range(lo_i, hi_i + 1))
        else:
            out.append(int(tok))
    return out


class SequenceStepWidget(QFrame):
    """One reorderable, expandable, editable step block.

    Signals:
        move_up(object): Request to move this block up (passes self).
        move_down(object): Request to move this block down (passes self).
        remove(object): Request to remove this block (passes self).
        changed(): Any edit that affects the one-line summary.
    """

    move_up = pyqtSignal(object)
    move_down = pyqtSignal(object)
    remove = pyqtSignal(object)
    changed = pyqtSignal()

    def __init__(
        self, step: SequenceStep, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._step = step
        self._index = 0
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._build()
        self._load_from_step()

    # -- public API --------------------------------------------------------

    def step(self) -> SequenceStep:
        """Return the step (kept current via live write-back)."""
        return self._step

    def set_index(self, index: int) -> None:
        """Set the 1-based position shown in the header summary."""
        self._index = index
        self._refresh_summary()

    def set_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable the editor (disabled while a sequence runs)."""
        self._body.setEnabled(enabled)
        self._up_btn.setEnabled(enabled)
        self._down_btn.setEnabled(enabled)
        self._remove_btn.setEnabled(enabled)

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(2)

        # Header row: expander + summary + reorder/remove.
        header = QHBoxLayout()
        self._toggle = QToolButton()
        self._toggle.setStyleSheet("QToolButton { border: none; }")
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setCheckable(True)
        self._toggle.toggled.connect(self._on_toggled)
        header.addWidget(self._toggle)

        self._summary = QLabel()
        self._summary.setStyleSheet("font-weight: bold;")
        header.addWidget(self._summary, 1)

        self._up_btn = QToolButton()
        self._up_btn.setText("▲")
        self._up_btn.setToolTip("Move step up")
        self._up_btn.clicked.connect(lambda: self.move_up.emit(self))
        header.addWidget(self._up_btn)

        self._down_btn = QToolButton()
        self._down_btn.setText("▼")
        self._down_btn.setToolTip("Move step down")
        self._down_btn.clicked.connect(lambda: self.move_down.emit(self))
        header.addWidget(self._down_btn)

        self._remove_btn = QToolButton()
        self._remove_btn.setText("✕")
        self._remove_btn.setToolTip("Remove step")
        self._remove_btn.clicked.connect(lambda: self.remove.emit(self))
        header.addWidget(self._remove_btn)
        outer.addLayout(header)

        # Body (hidden until expanded).
        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(20, 2, 2, 6)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Channels:"))
        self._channels_edit = QLineEdit()
        self._channels_edit.setToolTip("e.g. 1,4 or 1-16")
        self._channels_edit.editingFinished.connect(self._commit_channels)
        row1.addWidget(self._channels_edit, 1)
        row1.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        for m in _MODES:
            self._mode_combo.addItem(m)
        self._mode_combo.currentTextChanged.connect(self._commit_mode)
        row1.addWidget(self._mode_combo)
        body.addLayout(row1)

        # RE/CE pairing row, shown for Mode C only: one CE channel per WE.
        self._re_ce_row = QWidget()
        re_ce_layout = QHBoxLayout(self._re_ce_row)
        re_ce_layout.setContentsMargins(0, 0, 0, 0)
        re_ce_layout.addWidget(QLabel("RE/CE per WE:"))
        self._re_ce_edit = QLineEdit()
        self._re_ce_edit.setToolTip(
            "One RE/CE channel per WE channel, e.g. 2,2,2 (CH1-CH14)"
        )
        self._re_ce_edit.editingFinished.connect(self._commit_re_ce)
        re_ce_layout.addWidget(self._re_ce_edit, 1)
        self._re_ce_row.setVisible(False)
        body.addWidget(self._re_ce_row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Repeat:"))
        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(1, 9999)
        self._repeat_spin.valueChanged.connect(self._commit_repeat_delay)
        row2.addWidget(self._repeat_spin)
        row2.addWidget(QLabel("Delay (s):"))
        self._delay_spin = QDoubleSpinBox()
        self._delay_spin.setDecimals(2)
        self._delay_spin.setRange(0.0, 86400.0)
        self._delay_spin.setSingleStep(1.0)
        self._delay_spin.valueChanged.connect(self._commit_repeat_delay)
        row2.addWidget(self._delay_spin)
        row2.addStretch()
        body.addLayout(row2)

        self._params = ParameterForm()
        self._params.params_changed.connect(self._commit_params)
        body.addWidget(self._params)

        self._body.setVisible(False)
        outer.addWidget(self._body)

    # -- load / summary ----------------------------------------------------

    def _load_from_step(self) -> None:
        step = self._step
        self._channels_edit.setText(format_channels(step.channels))
        mode = step.electrode_config_mode
        self._mode_combo.blockSignals(True)
        if self._mode_combo.findText(mode) < 0:
            self._mode_combo.addItem(mode)
        self._mode_combo.setCurrentText(mode)
        self._mode_combo.blockSignals(False)
        self._sync_re_ce_field()

        self._repeat_spin.blockSignals(True)
        self._delay_spin.blockSignals(True)
        self._repeat_spin.setValue(max(1, int(step.repeat)))
        self._delay_spin.setValue(float(step.delay_s))
        self._repeat_spin.blockSignals(False)
        self._delay_spin.blockSignals(False)

        self._params.set_config(step.technique, step.params)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        step = self._step
        tech = (step.technique or step.preset_name or "step").upper()
        parts = [f"{self._index}. {tech}"]
        if step.channels:
            parts.append(f"ch[{format_channels(step.channels)}]")
        if step.repeat and step.repeat != 1:
            parts.append(f"×{step.repeat}")
        if step.delay_s:
            parts.append(f"+{step.delay_s:g}s")
        self._summary.setText("  ".join(parts))

    # -- write-back --------------------------------------------------------

    def _on_toggled(self, expanded: bool) -> None:
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._body.setVisible(expanded)

    def _sync_re_ce_field(self) -> None:
        """Show the RE/CE field for Mode C and seed it from the step."""
        manual = self._step.electrode_config_mode == _MANUAL_MODE
        self._re_ce_row.setVisible(manual)
        if manual:
            self._re_ce_edit.blockSignals(True)
            self._re_ce_edit.setText(
                format_channels(self._step.re_ce_channels)
            )
            self._re_ce_edit.blockSignals(False)

    def _commit_channels(self) -> None:
        try:
            channels = parse_channels(self._channels_edit.text())
        except ValueError:
            # Reject malformed input: restore the last good value.
            self._channels_edit.setText(format_channels(self._step.channels))
            return
        if not channels:
            # A step with zero WE channels would pass eager validation
            # and only fail mid-run when the engine starts it ("No
            # channels selected") — halting the rest of the queue.
            # Reject it here like malformed input.
            self._channels_edit.setText(format_channels(self._step.channels))
            return
        self._step.channels = channels
        # A changed channel count invalidates an explicit RE/CE pairing;
        # drop it so external/on_board repopulate at build time and a
        # Mode-C step re-prompts for a matching pairing.
        if len(self._step.re_ce_channels) != len(channels):
            self._step.re_ce_channels = []
        self._sync_re_ce_field()
        self._refresh_summary()
        self.changed.emit()

    def _commit_mode(self, mode: str) -> None:
        self._step.electrode_config_mode = mode
        self._sync_re_ce_field()
        self.changed.emit()

    def _commit_re_ce(self) -> None:
        try:
            re_ce = parse_channels(self._re_ce_edit.text())
        except ValueError:
            self._re_ce_edit.setText(
                format_channels(self._step.re_ce_channels)
            )
            return
        self._step.re_ce_channels = re_ce
        self.changed.emit()

    def _commit_repeat_delay(self) -> None:
        self._step.repeat = self._repeat_spin.value()
        self._step.delay_s = self._delay_spin.value()
        self._refresh_summary()
        self.changed.emit()

    def _commit_params(self) -> None:
        self._step.params = self._params.get_params()

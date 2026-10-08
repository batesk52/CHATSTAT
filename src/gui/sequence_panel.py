"""Sequencer panel for the EmStat Pico MUX16 (sequencer feature — Phase 3).

A PSTrace-"Scripts"-equivalent: saved presets are stacked as reorderable
step blocks and run back-to-back through the shared
:class:`~src.engine.measurement_engine.MeasurementEngine` via a
:class:`~src.engine.sequence_runner.SequenceRunner`.

The panel owns NO engine and NO serial I/O.  It takes the
:class:`~src.data.presets.PresetManager`, the engine, and a
connection-provider callable by injection from
:class:`~src.gui.main_window.MainWindow`, builds a ``SequenceRunner`` from
the current :class:`~src.data.sequence.Sequence` on Run, and wires the
runner's progress / finished / error signals back to the progress label
and the Run/Stop enabled-state.

Each step is an expandable :class:`~src.gui.sequence_step_widget.
SequenceStepWidget` stacked in a scroll area; the visual order IS the
sequence order (reorder via each block's buttons, read back via
:meth:`build_sequence`). Clicking a block expands it to a full editor for
the step's embedded config, so the sequence carries the values.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.data.presets import PresetManager
from src.data.sequence import Sequence, SequenceStep
from src.engine.sequence_runner import SequenceRunner
from src.gui.sequence_step_widget import SequenceStepWidget

logger = logging.getLogger(__name__)

# File dialog filter for sequence files (sequencer feature).
_SEQUENCE_FILE_FILTER = "MUX16 sequences (*.mux16seq)"
_SEQUENCE_FILE_SUFFIX = ".mux16seq"

# Type alias: a zero-arg callable returning the current connection (or
# None when disconnected).  Injected so the panel never owns the
# connection lifecycle.
ConnectionProvider = Callable[[], object]

# Type alias: a zero-arg callable returning ``(base_export_dir,
# auto_save_all)`` for per-step auto-save. ``auto_save_all`` mirrors the
# GUI auto-save toggle: when True every step writes into the base dir,
# when False nothing auto-saves (auto-save is fully opt-in). Injected so
# the panel never decides the policy itself.
ExportBaseProvider = Callable[[], tuple[Optional[str], bool]]


class SequencePanel(QWidget):
    """Reorderable list of preset steps with Run / Stop and persistence.

    The panel is a thin orchestrator: it composes a :class:`Sequence`
    from its visual row order, hands it to a :class:`SequenceRunner`
    built against the injected engine + connection, and reflects the
    runner's lifecycle in its progress label and button state.

    Signals:
        sequence_started(): Emitted when a run launches (so the main
            window can suppress its single-run export prompt and disable
            the Start control).
        sequence_stopped(): Emitted when a run finishes, errors, or is
            stopped (so the main window can restore its controls).

    Args:
        preset_manager: Store the step preset names resolve against.
            May be injected later via :meth:`set_preset_manager`.
        engine: Shared measurement engine.  May be injected later via
            :meth:`set_engine`.
        connection_provider: Zero-arg callable returning the current
            connection (or ``None``).  May be injected later via
            :meth:`set_connection_provider`.
        parent: Optional Qt parent.
    """

    sequence_started = pyqtSignal()
    # Emitted on EVERY terminal path — clean finish, user stop, or step
    # error — so the main window restores controls and offers to save the
    # retained step data in one place (no path can silently discard it).
    sequence_stopped = pyqtSignal()

    def __init__(
        self,
        preset_manager: Optional[PresetManager] = None,
        engine: Optional[object] = None,
        connection_provider: Optional[ConnectionProvider] = None,
        export_base_provider: Optional[ExportBaseProvider] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._preset_mgr = preset_manager
        self._engine = engine
        self._connection_provider = connection_provider
        self._export_base_provider = export_base_provider
        self._runner: Optional[SequenceRunner] = None
        self._setup_ui()
        self._update_buttons(running=False)

    # -- injection ---------------------------------------------------------

    def set_preset_manager(self, manager: PresetManager) -> None:
        """Inject (or replace) the PresetManager the steps resolve against."""
        self._preset_mgr = manager

    def set_engine(self, engine: object) -> None:
        """Inject (or replace) the shared measurement engine."""
        self._engine = engine

    def set_connection_provider(
        self, provider: ConnectionProvider
    ) -> None:
        """Inject (or replace) the connection-provider callable."""
        self._connection_provider = provider

    def set_export_base_provider(
        self, provider: ExportBaseProvider
    ) -> None:
        """Inject the callable that decides per-step auto-save.

        The provider returns ``(base_export_dir, auto_save_all)``: the
        root exports directory (or ``None`` to disable all writing) and
        whether every step should auto-save (the GUI toggle). Auto-save
        is fully opt-in; with the toggle off nothing auto-saves.
        """
        self._export_base_provider = provider

    # -- UI construction ---------------------------------------------------

    def _setup_ui(self) -> None:
        """Build the step list, add/remove controls, and Run/Stop row."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        layout.addWidget(
            QLabel("Sequence steps (click a step to expand & edit):")
        )

        # Scrollable stack of expandable step blocks; the visual order IS
        # the sequence order. Reorder/remove is via each block's buttons.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._steps_container = QWidget()
        self._steps_layout = QVBoxLayout(self._steps_container)
        self._steps_layout.setContentsMargins(0, 0, 0, 0)
        self._steps_layout.setSpacing(3)
        self._steps_layout.addStretch(1)  # keep blocks top-aligned
        scroll.setWidget(self._steps_container)
        layout.addWidget(scroll, 1)

        # Add row (removal is per-block).
        edit_row = QHBoxLayout()
        self._add_btn = QPushButton("Add step")
        self._add_btn.setToolTip("Add a step from the loaded presets")
        self._add_btn.clicked.connect(self._on_add_step)
        edit_row.addWidget(self._add_btn)
        edit_row.addStretch()
        layout.addLayout(edit_row)

        # Save / Load row.
        io_row = QHBoxLayout()
        self._save_btn = QPushButton("Save...")
        self._save_btn.setToolTip("Save this sequence to a *.mux16seq file")
        self._save_btn.clicked.connect(self._on_save_sequence)
        io_row.addWidget(self._save_btn)

        self._load_btn = QPushButton("Load...")
        self._load_btn.setToolTip("Load a *.mux16seq file")
        self._load_btn.clicked.connect(self._on_load_sequence)
        io_row.addWidget(self._load_btn)
        layout.addLayout(io_row)

        # Run / Stop row + progress label.
        run_row = QHBoxLayout()
        self._run_btn = QPushButton("Run")
        self._run_btn.setStyleSheet(
            "QPushButton { background-color: #2ca02c; color: white; "
            "font-weight: bold; padding: 6px; }"
        )
        self._run_btn.clicked.connect(self._on_run)
        run_row.addWidget(self._run_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setStyleSheet(
            "QPushButton { background-color: #d62728; color: white; "
            "font-weight: bold; padding: 6px; }"
        )
        self._stop_btn.clicked.connect(self._on_stop)
        run_row.addWidget(self._stop_btn)
        layout.addLayout(run_row)

        self._progress_label = QLabel("Idle")
        self._progress_label.setStyleSheet(
            "color: #888; font-size: 11px;"
        )
        layout.addWidget(self._progress_label)

    # -- step model --------------------------------------------------------

    def _step_widgets(self) -> list[SequenceStepWidget]:
        """Return the step blocks in visual (sequence) order."""
        widgets: list[SequenceStepWidget] = []
        for i in range(self._steps_layout.count()):
            w = self._steps_layout.itemAt(i).widget()
            if isinstance(w, SequenceStepWidget):
                widgets.append(w)
        return widgets

    def _renumber(self) -> None:
        """Refresh each block's 1-based position label."""
        for i, w in enumerate(self._step_widgets()):
            w.set_index(i + 1)

    def add_step(self, step: SequenceStep) -> None:
        """Append a step block to the stack.

        Args:
            step: The :class:`SequenceStep` to add.
        """
        widget = SequenceStepWidget(step)
        widget.move_up.connect(self._on_move_up)
        widget.move_down.connect(self._on_move_down)
        widget.remove.connect(self._on_remove)
        # Insert before the trailing stretch so blocks stay top-aligned.
        self._steps_layout.insertWidget(
            self._steps_layout.count() - 1, widget
        )
        self._renumber()

    def build_sequence(self, name: str = "sequence") -> Sequence:
        """Compose a :class:`Sequence` from the current block order.

        Args:
            name: Display name for the sequence.

        Returns:
            A :class:`Sequence` whose ``steps`` match the visual order.
        """
        steps = [w.step() for w in self._step_widgets()]
        return Sequence(name=name, steps=steps)

    def load_sequence(self, sequence: Sequence) -> None:
        """Replace the stack with a sequence's steps.

        Args:
            sequence: The sequence whose steps populate the stack.
        """
        for w in self._step_widgets():
            self._steps_layout.removeWidget(w)
            w.deleteLater()
        for step in sequence.steps:
            self.add_step(self._upgrade_legacy_step(step))

    def _upgrade_legacy_step(self, step: SequenceStep) -> SequenceStep:
        """Embed a legacy reference step's config from the store, if possible.

        Older ``*.mux16seq`` files stored only a preset-name reference;
        embedding a full copy on load lets the step editor populate. Falls
        back to the original step when there is no store or the named
        preset is missing (it still runs by resolving against the store).

        Args:
            step: A loaded step (possibly legacy reference-only).

        Returns:
            An embedded copy, or the original step if it can't be upgraded.
        """
        if step.is_embedded or self._preset_mgr is None:
            return step
        preset = self._preset_mgr.get_preset(step.preset_name)
        if preset is None:
            return step
        upgraded = SequenceStep.from_preset(
            step.preset_name,
            preset,
            repeat=step.repeat,
            delay_s=step.delay_s,
        )
        if step.channels_override is not None:
            upgraded.channels = list(step.channels_override)
        if step.mode_override is not None:
            upgraded.electrode_config_mode = step.mode_override
        return upgraded

    # -- reorder / remove --------------------------------------------------

    def _move_widget(self, widget: SequenceStepWidget, delta: int) -> None:
        """Move a block by ``delta`` positions, clamped to the ends."""
        widgets = self._step_widgets()
        if widget not in widgets:
            return
        i = widgets.index(widget)
        j = i + delta
        if j < 0 or j >= len(widgets):
            return
        self._steps_layout.removeWidget(widget)
        # Account for the trailing stretch at the end of the layout.
        self._steps_layout.insertWidget(j, widget)
        self._renumber()

    @pyqtSlot(object)
    def _on_move_up(self, widget: SequenceStepWidget) -> None:
        """Move a block one position earlier."""
        self._move_widget(widget, -1)

    @pyqtSlot(object)
    def _on_move_down(self, widget: SequenceStepWidget) -> None:
        """Move a block one position later."""
        self._move_widget(widget, +1)

    @pyqtSlot(object)
    def _on_remove(self, widget: SequenceStepWidget) -> None:
        """Remove a block from the stack."""
        self._steps_layout.removeWidget(widget)
        widget.deleteLater()
        self._renumber()

    # -- add ---------------------------------------------------------------

    @pyqtSlot()
    def _on_add_step(self) -> None:
        """Prompt for a preset and append it as a new step."""
        if self._preset_mgr is None:
            QMessageBox.information(
                self,
                "No Presets",
                "No preset store is loaded yet.",
            )
            return
        names = self._preset_mgr.list_presets()
        if not names:
            QMessageBox.information(
                self,
                "No Presets",
                "Save or import a preset before building a sequence.",
            )
            return
        key, ok = QInputDialog.getItem(
            self,
            "Add Step",
            "Preset:",
            names,
            0,
            False,
        )
        if not ok or not key:
            return
        preset = self._preset_mgr.get_preset(key)
        if preset is None:
            QMessageBox.warning(
                self, "Unknown Preset", f"Preset {key!r} not found."
            )
            return
        # Seed a self-contained step: it copies the preset's values and is
        # independent from here on (the sequence carries the values).
        self.add_step(SequenceStep.from_preset(key, preset))

    # -- persistence -------------------------------------------------------

    @pyqtSlot()
    def _on_save_sequence(self) -> None:
        """Save the current sequence to a chosen ``*.mux16seq`` file."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save sequence",
            "",
            _SEQUENCE_FILE_FILTER,
        )
        if not path:
            return
        if not path.endswith(_SEQUENCE_FILE_SUFFIX):
            path += _SEQUENCE_FILE_SUFFIX
        try:
            self.build_sequence().save_to_path(path)
        except OSError as exc:
            logger.warning("Could not save sequence %s: %s", path, exc)
            QMessageBox.warning(
                self, "Save Failed", f"Could not save sequence: {exc}"
            )
            return
        logger.info("Saved sequence to %s", path)

    @pyqtSlot()
    def _on_load_sequence(self) -> None:
        """Load a ``*.mux16seq`` file into the list."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load sequence",
            "",
            _SEQUENCE_FILE_FILTER,
        )
        if not path:
            return
        try:
            sequence = Sequence.load_from_path(path)
        except (OSError, ValueError) as exc:
            logger.warning("Could not load sequence %s: %s", path, exc)
            QMessageBox.warning(
                self, "Load Failed", f"Could not load sequence: {exc}"
            )
            return
        self.load_sequence(sequence)
        logger.info("Loaded sequence from %s", path)

    # -- run / stop --------------------------------------------------------

    @pyqtSlot()
    def _on_run(self) -> None:
        """Build a SequenceRunner and start the current sequence."""
        if self._runner is not None:
            return
        if self._preset_mgr is None or self._engine is None:
            QMessageBox.warning(
                self,
                "Not Ready",
                "Sequencer is not wired to a preset store and engine.",
            )
            return
        # Refuse a busy engine BEFORE emitting sequence_started: the main
        # window reacts to that signal by resetting the plot tabs and
        # locking the single-run controls, so letting the runner discover
        # the busy engine afterwards would destroy the in-flight run's
        # live plot and then re-enable Start while it is still running.
        if hasattr(self._engine, "isRunning") and self._engine.isRunning():
            QMessageBox.warning(
                self,
                "Engine Busy",
                "A measurement is already running. Stop it before "
                "starting a sequence.",
            )
            return
        sequence = self.build_sequence()
        if not sequence.steps:
            QMessageBox.information(
                self, "Empty Sequence", "Add at least one step first."
            )
            return

        connection = (
            self._connection_provider()
            if self._connection_provider is not None
            else None
        )
        # Auto-save policy lives outside the panel: the provider returns
        # (base_dir, auto_save_all). Auto-save is fully opt-in — with
        # auto_save_all False, nothing auto-saves.
        base_export_dir, auto_save_all = (
            self._export_base_provider()
            if self._export_base_provider is not None
            else (None, False)
        )

        try:
            runner = SequenceRunner.from_sequence(
                self._engine,
                connection,
                sequence,
                self._preset_mgr,
                base_export_dir=base_export_dir,
                auto_save_all=auto_save_all,
                parent=self,
            )
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Invalid Sequence",
                f"Cannot start the sequence: {exc}",
            )
            return

        runner.sequence_progress.connect(self._on_progress)
        runner.sequence_finished.connect(self._on_finished)
        runner.sequence_error.connect(self._on_error)
        self._runner = runner

        self._update_buttons(running=True)
        self._progress_label.setText(
            f"Step 0 of {runner.total_steps}"
        )
        self.sequence_started.emit()
        runner.start()

    @pyqtSlot()
    def _on_stop(self) -> None:
        """Abort the running sequence and the in-flight engine step."""
        if self._runner is None:
            return
        self._runner.stop()
        # The runner owns no serial I/O; abort the in-flight engine step
        # so the current measurement actually stops.
        engine = self._engine
        if engine is not None and hasattr(engine, "abort"):
            try:
                engine.abort()
            except Exception as exc:  # noqa: BLE001 - best-effort stop
                logger.warning("Engine abort during stop failed: %s", exc)
        self._teardown_runner()
        self._progress_label.setText("Stopped")

    # -- runner signal handlers -------------------------------------------

    @pyqtSlot(int, int)
    def _on_progress(self, completed: int, total: int) -> None:
        """Update the progress label as each step completes."""
        self._progress_label.setText(f"Step {completed} of {total}")

    @pyqtSlot()
    def _on_finished(self) -> None:
        """Restore controls when the sequence completes.

        Teardown emits ``sequence_stopped`` — the single terminal hook
        where the main window restores controls and offers to save any
        retained step data (same as the stop and error paths).
        """
        self._teardown_runner()
        self._progress_label.setText("Complete")

    @pyqtSlot(str)
    def _on_error(self, message: str) -> None:
        """Restore controls and surface a sequence error."""
        self._teardown_runner()
        self._progress_label.setText(f"Error: {message}")
        QMessageBox.critical(self, "Sequence Error", message)

    # -- internal ----------------------------------------------------------

    def _teardown_runner(self) -> None:
        """Drop the finished runner and re-enable the controls."""
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.deleteLater()
        self._update_buttons(running=False)
        self.sequence_stopped.emit()

    def _update_buttons(self, running: bool) -> None:
        """Toggle which controls are enabled based on run state."""
        self._run_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._add_btn.setEnabled(not running)
        self._save_btn.setEnabled(not running)
        self._load_btn.setEnabled(not running)
        # Lock every step editor while the sequence runs.
        for w in self._step_widgets():
            w.set_controls_enabled(not running)

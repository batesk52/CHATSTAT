"""Main application window for ChatStat.

Assembles all GUI panels (connection, technique, channel, measurement
controls) with a live pyqtgraph plot in a dock-based QMainWindow layout.
Wires Qt signals between the control panels, measurement engine, and
plot widget so that data flows engine -> GUI exclusively through signals.

Typical launch::

    source .venv/Scripts/activate
    python -m src.gui.main_window

Or import and instantiate::

    from src.gui.main_window import MainWindow
    window = MainWindow()
    window.show()
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import (
    QEvent,
    QObject,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.agent import bridge as agent_bridge
from src.agent.engine_adapter import EngineAdapter
from src.agent.mock_engine import MockConnection, MockMeasurementEngine
from src.agent.preset_tools import build_preset_tools
from src.agent.swv_analysis import build_swv_analysis_tools
from src.agent.tools import build_registry
from src.agent.vendor_analysis import build_analysis_tools
from src.comms.serial_connection import PicoConnection
from src.data.exporters import (
    PsSessionExporter,
    make_sequence_dir,
    sequence_step_dirname,
)
from src.data.models import (
    EXTERNAL_RE_CE_CHANNEL,
    ON_BOARD_RE_CE_CHANNEL,
    AutoSaveConfig,
    MeasurementResult,
    TechniqueConfig,
)
from src.data.app_settings import (
    get_agent_api_key,
    get_agent_model,
    get_export_dir,
    get_last_preset_file,
    set_agent_api_key,
    set_agent_model,
    set_export_dir,
)
from src.data.presets import Preset, PresetManager
from src.engine.measurement_engine import MeasurementEngine
from src.gui.controls import (
    ChannelPanel,
    ConnectionPanel,
    ElectrodeConfigPanel,
    ManualChannelPanel,
    MeasurementControlPanel,
    TechniquePanel,
)
from src.gui.agent_dock import (
    AgentDockPanel,
    AgentSettingsDialog,
    warm_up_math_renderer,
)
from src.gui.eis_plot_container import EISPlotContainer
from src.gui.plot_widget import LivePlotWidget
from src.gui.sequence_panel import SequencePanel
from src.gui.toggle_switch import ToggleSwitch
from src.gui.workers import ConnectWorker

logger = logging.getLogger(__name__)

# Application metadata
APP_NAME = "ChatStat"
APP_VERSION = "0.1.0"

# Hover text for the pristine "Live" scratch tab (consumed by the next run).
_SCRATCH_TOOLTIP = "New plot — choose a technique and run"

class _AgentFileDialogs:
    """Native save/open dialogs for the agent's preset/sequence tools.

    Implements the :class:`src.agent.preset_tools.FileDialogProvider`
    protocol. The methods are invoked ON THE GUI THREAD (the tool handlers
    marshal them via ``run_on_gui``), so ``QFileDialog`` is safe to call
    here. The last-used directory is remembered between calls so the
    operator does not re-navigate every time.
    """

    def __init__(self, parent, start_dir: str) -> None:
        self._parent = parent
        self._dir = start_dir or ""
        # The in-flight dialog (instance, not the static helper) so it can be
        # force-closed on app shutdown — otherwise closing the window while the
        # agent's dialog is up leaves an orphaned modal floating over a dead
        # window, and the GUI thread blocks in the worker shutdown wait.
        self._active: Optional[QFileDialog] = None

    def _run(self, accept_mode, caption: str, start: str, file_filter: str):
        dlg = QFileDialog(self._parent, caption, start, file_filter)
        dlg.setAcceptMode(accept_mode)
        if accept_mode == QFileDialog.AcceptMode.AcceptSave:
            dlg.setFileMode(QFileDialog.FileMode.AnyFile)
        else:
            dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        self._active = dlg
        try:
            accepted = dlg.exec()
            files = dlg.selectedFiles() if accepted else []
            path = files[0] if files else ""
        finally:
            self._active = None
            dlg.deleteLater()
        if path:
            self._dir = os.path.dirname(path)
        return path or None

    def request_save_path(self, suggested_name: str, file_filter: str):
        """Open a native Save dialog; return the chosen path or None."""
        start = (
            os.path.join(self._dir, suggested_name)
            if self._dir
            else suggested_name
        )
        return self._run(
            QFileDialog.AcceptMode.AcceptSave, "Save", start, file_filter
        )

    def request_open_path(self, file_filter: str):
        """Open a native Open dialog; return the chosen path or None."""
        return self._run(
            QFileDialog.AcceptMode.AcceptOpen, "Open", self._dir, file_filter
        )

    def close_active(self) -> None:
        """Reject any in-flight dialog (call on the GUI thread before shutdown)."""
        dlg = self._active
        if dlg is not None:
            dlg.reject()


class _NoWheelScrollFilter(QObject):
    """Stop the mouse-wheel from changing combo/spin box values, while
    still scrolling the surrounding panel.

    Mouse-scrolling over a QComboBox or QSpinBox/QDoubleSpinBox silently
    changes its value — easy to do by accident, and dangerous on
    measurement parameters (t_eq, E vertex, current range, ...). The
    wheel over such a control NEVER changes its value (focused or not);
    instead the event is forwarded to the enclosing scroll area so the
    panel still scrolls (no dead zones over fields). The user must click
    and type, or use the dropdown, to change a value. Installed
    application-wide so every existing and future combo/spinbox is
    covered without per-widget plumbing.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and isinstance(
            obj, (QComboBox, QAbstractSpinBox)
        ):
            # Forward the wheel to the nearest scroll area so the panel
            # scrolls, then consume it from the control so its value can
            # never change.
            parent = obj.parentWidget()
            while parent is not None:
                if isinstance(parent, QScrollArea):
                    QApplication.sendEvent(parent.viewport(), event)
                    break
                parent = parent.parentWidget()
            return True
        return False


class _LogSignalBridge(QObject):
    """Thread-safe bridge: emits a signal so the GUI thread updates."""

    log_message = pyqtSignal(str)


class _LogHandler(logging.Handler):
    """Logging handler that appends formatted records to a QPlainTextEdit.

    Uses a signal to marshal messages from any thread to the GUI thread,
    since QWidget.appendPlainText must only be called from the GUI thread.
    """

    def __init__(self, text_widget: QPlainTextEdit) -> None:
        super().__init__()
        self._bridge = _LogSignalBridge()
        self._bridge.log_message.connect(text_widget.appendPlainText)

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._bridge.log_message.emit(msg)


class MainWindow(QMainWindow):
    """Main application window for ChatStat.

    Provides a dock-based layout with:

    * **Left panel** -- Connection, Technique, Channel, and Measurement
      control panels stacked vertically.
    * **Centre** -- Live pyqtgraph plot widget for real-time data.
    * **Bottom** -- Log console showing application messages.
    * **Status bar** -- Connection state, measurement progress, and
      current channel indicator.
    * **Menu bar** -- File (export, quit), Device (connect, disconnect),
      Help (about).

    All serial I/O is delegated to ``PicoConnection`` and
    ``MeasurementEngine``; the GUI thread never performs blocking I/O.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 800)

        # Core objects
        self._connection = PicoConnection()
        self._engine = MeasurementEngine(parent=self)
        self._last_result: Optional[MeasurementResult] = None
        self._preset_mgr = PresetManager()
        # Auto-load the last-used external preset file (sequencer feature) when
        # one was remembered and still exists; otherwise the default
        # per-user store stays active.
        self._load_last_preset_file()
        self._auto_save_active = False
        # True while a SequenceRunner is driving the engine.  Read in
        # _on_measurement_finished to suppress the interactive export
        # prompt (auto-save per step instead) and to keep the single-run
        # Start control disabled until the sequence ends (sequencer feature).
        self._sequence_active = False
        # Results of sequence steps that did NOT auto-save are retained
        # so the end-of-run prompt (fired from _on_sequence_stopped — the
        # one terminal hook covering finish, stop, AND error) can offer
        # to save them; auto-saved steps are already on disk and are not
        # retained (no unbounded growth on long auto-saved runs).
        self._sequence_results: list[MeasurementResult] = []
        # Auto-save dir of the step currently finishing (set by the
        # engine's auto_save_completed, cleared at each step start) —
        # how the finished handler knows a step already saved itself.
        self._step_autosave_dir: Optional[str] = None
        # Background worker for the (blocking) connect handshake.
        self._connect_worker: Optional[ConnectWorker] = None
        self._connect_port: str = ""

        # Install app-wide filter that blocks accidental wheel-scroll
        # changes on combo boxes and spin boxes.
        self._no_wheel_filter = _NoWheelScrollFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._no_wheel_filter)

        # Build UI
        self._build_central_widget()
        # Put the dock tab bar at the TOP so it doesn't collide with the
        # ManualChannelPanel's bulk-set buttons in Mode C.
        self.setTabPosition(
            Qt.DockWidgetArea.LeftDockWidgetArea,
            QTabWidget.TabPosition.North,
        )
        self._build_control_dock()
        self._build_log_dock()
        self._build_sequence_dock()
        # Tab the Log + Sequence docks behind the Controls dock so
        # Settings is the default view; the user clicks "Log" when a
        # measurement runs and "Sequence" to stack presets.
        self.tabifyDockWidget(self._control_dock, self._log_dock)
        self.tabifyDockWidget(self._control_dock, self._sequence_dock)
        self._control_dock.raise_()
        self._build_menu_bar()
        self._build_status_bar()

        # Wire signals
        self._wire_signals()

        # Embedded Claude agent (right dock + worker + tool registry)
        self._build_agent_dock()

        # Load presets into UI
        self._load_presets_into_ui()

        # Initial state
        self._update_ui_disconnected()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_central_widget(self) -> None:
        """Create the tabbed live-plot area as the central widget.

        Every measurement run gets its own browser-style plot tab and
        nothing already plotted is ever overwritten: starting a new run
        opens a fresh tab to the right (a multi-step sequence leaves one
        reviewable plot per step). Tabs carry a close button and a hover
        tooltip, exactly like browser tabs.

        Two pointers track the strip:

        * ``_plot_container`` — the tab that incoming data currently
          routes to (the run target).
        * ``_scratch_container`` — the lone pristine "Live" tab, used to
          preview the selected technique and consumed by the next run so
          a fresh app (or a technique change) never leaves an empty tab
          stranded to the left of real results. ``None`` once consumed.
        """
        self._plot_tabs = QTabWidget()
        self._plot_tabs.setMovable(True)
        self._plot_tabs.setDocumentMode(True)
        self._plot_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self._plot_tabs.setUsesScrollButtons(True)
        self._plot_tabs.setTabsClosable(True)
        self._plot_tabs.tabCloseRequested.connect(self._on_plot_tab_close)
        # Step counter for sequence tab labels; reset per sequence run.
        self._seq_plot_n = 0
        # True only while a run is actively writing to _plot_container;
        # gates closing/repreviewing the recording tab out from under it.
        self._run_in_progress = False
        self._plot_container: Optional[EISPlotContainer] = None
        self._scratch_container: Optional[EISPlotContainer] = None
        # Seed one scratch tab so the app opens with a plot and the
        # technique dropdown can preview before the first run.
        self._scratch_container = self._add_plot_tab(
            "", "Live", tooltip=_SCRATCH_TOOLTIP
        )
        self.setCentralWidget(self._plot_tabs)

    def _add_plot_tab(
        self, technique: str, label: str, tooltip: str = ""
    ) -> EISPlotContainer:
        """Create a new live-plot tab and make it the active target.

        Args:
            technique: Technique to configure the tab's plot for.
            label: Tab caption (e.g. ``"CV"`` or ``"2·EIS"``).
            tooltip: Optional hover text for the tab.

        Returns:
            The newly created (and now current) plot container.
        """
        container = EISPlotContainer(parent=self)
        container.set_technique(technique)
        index = self._plot_tabs.addTab(container, label)
        if tooltip:
            self._plot_tabs.setTabToolTip(index, tooltip)
        self._plot_tabs.setCurrentIndex(index)
        self._plot_container = container
        return container

    def _begin_run_tab(
        self, technique: str, label: str
    ) -> EISPlotContainer:
        """Target a tab for a starting run without overwriting results.

        Consumes the pristine scratch tab if one is available; otherwise
        opens a fresh tab to the right so a completed run's plot is left
        intact and reviewable.

        Args:
            technique: Technique to configure the tab's plot for.
            label: Tab caption for the run.

        Returns:
            The plot container the run will write to.
        """
        recording_tip = f"{technique.upper() or 'Run'} — recording…"
        scratch = self._scratch_container
        if scratch is not None and self._plot_tabs.indexOf(scratch) != -1:
            idx = self._plot_tabs.indexOf(scratch)
            scratch.clear_plot()
            scratch.set_technique(technique)
            self._plot_tabs.setTabText(idx, label)
            self._plot_tabs.setTabToolTip(idx, recording_tip)
            self._plot_tabs.setCurrentIndex(idx)
            self._plot_container = scratch
            self._scratch_container = None
            return scratch
        # No scratch tab (the previous run filled it): open a new one so
        # the prior plot survives.
        self._scratch_container = None
        return self._add_plot_tab(technique, label, tooltip=recording_tip)

    def _finalize_run_tooltip(self, result: MeasurementResult) -> None:
        """Enrich the recording tab's hover tooltip once a run completes.

        Cosmetic only: a tooltip is read off the result with defensive
        defaults so it can never disturb the critical finished-handler
        path (export / auto-save) on a partial result.
        """
        container = self._plot_container
        if container is None:
            return
        idx = self._plot_tabs.indexOf(container)
        if idx < 0:
            return
        tech = (getattr(result, "technique", "") or "").upper() or "Run"
        channels = getattr(result, "measured_channels", None) or []
        chans = ", ".join(str(c) for c in channels) if channels else "—"
        n_points = getattr(result, "num_points", 0)
        tip = f"{tech} · {n_points} pts · ch {chans}"
        start = getattr(result, "start_time", None)
        if start is not None:
            tip += f" · {start.strftime('%H:%M:%S')}"
        self._plot_tabs.setTabToolTip(idx, tip)

    @pyqtSlot(int)
    def _on_plot_tab_close(self, index: int) -> None:
        """Close a plot tab (browser-style X), keeping the strip valid.

        The tab a run is actively recording into can't be closed mid-run
        — that would dangle the data-routing target — so it is refused
        with a status hint. Closing the last tab reseeds a scratch tab so
        the central area is never left empty.
        """
        widget = self._plot_tabs.widget(index)
        if widget is None:
            return
        if self._run_in_progress and widget is self._plot_container:
            self.statusBar().showMessage(
                "Can't close the plot that's recording — stop the run first."
            )
            return
        closing_active = widget is self._plot_container
        self._plot_tabs.removeTab(index)
        if widget is self._scratch_container:
            self._scratch_container = None
        widget.deleteLater()
        if self._plot_tabs.count() == 0:
            # Never leave the central area tab-less.
            self._scratch_container = self._add_plot_tab(
                self._tech_panel.selected_technique() or "",
                "Live",
                tooltip=_SCRATCH_TOOLTIP,
            )
        elif closing_active:
            # The routing target was closed (not mid-run): fall back to
            # whatever tab is now visible.
            self._plot_container = self._plot_tabs.currentWidget()

    def _reset_plot_tabs(self) -> None:
        """Remove every plot tab (used by the terminal-hook safety net)."""
        while self._plot_tabs.count():
            widget = self._plot_tabs.widget(0)
            self._plot_tabs.removeTab(0)
            widget.deleteLater()
        self._plot_container = None
        self._scratch_container = None

    @pyqtSlot(object)
    def _route_data_point(self, data_point: object) -> None:
        """Forward an incoming data point to the active plot tab."""
        if self._plot_container is not None:
            self._plot_container.on_data_point(data_point)

    @pyqtSlot(str)
    def _on_technique_preview(self, technique: str) -> None:
        """Preview the selected technique on a pristine scratch tab.

        Previewing must never reconfigure a completed run's axes, so the
        preview always lands on the lone scratch tab — reusing it if one
        exists, else spawning a fresh one to the right. No-op while a run
        is recording (the dropdown change can't disturb live data).
        """
        if self._run_in_progress:
            return
        scratch = self._scratch_container
        if scratch is not None and self._plot_tabs.indexOf(scratch) != -1:
            scratch.set_technique(technique)
            self._plot_tabs.setCurrentIndex(self._plot_tabs.indexOf(scratch))
        else:
            self._scratch_container = self._add_plot_tab(
                technique, "Live", tooltip=_SCRATCH_TOOLTIP
            )

    def _build_control_dock(self) -> None:
        """Build left dock with stacked control panels."""
        dock = QDockWidget("Settings", self)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self._control_dock = dock

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._conn_panel = ConnectionPanel()
        layout.addWidget(self._conn_panel)

        # Verbose-logging switch. Off = INFO (normal), on = DEBUG, which
        # surfaces the per-marker device trace and the "Ended without '+'
        # END_MEAS marker" warning — used to diagnose slow/missing
        # end-of-measurement save prompts. Centered directly under the
        # connection box so it's reachable before starting a run.
        log_row = QHBoxLayout()
        log_row.setContentsMargins(0, 2, 0, 2)
        log_row.setSpacing(6)
        self._log_caption = QLabel("Log output:")
        self._log_caption.setStyleSheet(
            "color: #f5f5f5; font-weight: bold; font-size: 12px;"
        )
        self._info_label = QLabel("INFO")
        self._debug_label = QLabel("DEBUG")
        self._log_switch = ToggleSwitch()
        self._log_switch.setToolTip(
            "Set logging to DEBUG. Shows the device's raw marker stream "
            "and end-of-measurement diagnostics. Verbose — leave off for "
            "normal use."
        )
        self._log_switch.toggled.connect(self._on_debug_log_toggled)
        log_row.addStretch()
        log_row.addWidget(self._log_caption)
        log_row.addSpacing(4)
        log_row.addWidget(self._info_label)
        log_row.addWidget(self._log_switch)
        log_row.addWidget(self._debug_label)
        log_row.addStretch()
        layout.addLayout(log_row)
        self._update_log_switch_labels(False)

        self._tech_panel = TechniquePanel()
        layout.addWidget(self._tech_panel)

        # Electrode-config radio sits between Technique and Channels so
        # the user picks the wiring mode before configuring channels.
        self._electrode_config_panel = ElectrodeConfigPanel()
        layout.addWidget(self._electrode_config_panel)

        # Channel grid (modes A and B: 16-WE workflow).
        self._chan_panel = ChannelPanel()
        layout.addWidget(self._chan_panel)

        # Manual channel pairing (mode C: 14-row per-WE table).  Hidden
        # by default; shown when the electrode-config panel switches to
        # "manual".
        self._manual_channel_panel = ManualChannelPanel()
        self._manual_channel_panel.setVisible(False)
        layout.addWidget(self._manual_channel_panel)

        self._meas_panel = MeasurementControlPanel()
        layout.addWidget(self._meas_panel)

        layout.addStretch()
        # Wrap container in a scroll area so the panel stack (especially
        # Mode C's 14-row ManualChannelPanel) doesn't overflow the dock.
        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _build_log_dock(self) -> None:
        """Build left dock with a log console, tabbed behind Settings."""
        dock = QDockWidget("Log", self)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )

        self._log_text = QPlainTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumBlockCount(2000)
        dock.setWidget(self._log_text)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self._log_dock = dock

        # Attach a logging handler to the root logger. Keep a reference so
        # closeEvent() can remove it — otherwise a stale handler keeps
        # firing log records into this (destroyed) widget after the window
        # closes, leaking the handler and risking a crash on a second
        # MainWindow (e.g. in tests).
        self._log_handler = _LogHandler(self._log_text)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logging.getLogger().addHandler(self._log_handler)

    def _build_agent_dock(self) -> None:
        """Build the right 'Agent' dock and the agent tool stack.

        Creates the EngineAdapter + ToolRegistry (built-in measurement/
        device tools plus the vendored-analysis tools) and the
        AgentDockPanel that owns the AgentWorker.  ``agent_bridge.
        install()`` is called here, on the GUI thread, before any tool
        can run (idempotent).

        Mock toggle: when the EMSTAT_AGENT_MOCK environment variable is
        truthy ("1"/"true"/"yes"/"on"), the AGENT drives a
        MockMeasurementEngine + MockConnection instead of the real
        hardware pair, and the mock engine's signals are connected to
        the SAME plot/handler slots as the real engine (mirroring
        _wire_signals) so the live plots animate during agent-driven
        mock runs.  The real engine path is untouched either way.
        """
        # The bridge must exist before any agent tool marshals onto the
        # GUI thread; install() is idempotent and GUI-thread-checked.
        agent_bridge.install()

        mock_flag = os.environ.get("EMSTAT_AGENT_MOCK", "")
        if mock_flag.strip().lower() in ("1", "true", "yes", "on"):
            engine = MockMeasurementEngine(parent=self)
            connection = MockConnection()
            connection.connect("MOCK1")
            # Mirror the real-engine wiring (_wire_signals) so agent-
            # driven mock measurements animate the same live plots.
            # _on_measurement_started creates and configures the run's
            # plot tab (tabbed live plots), so no separate plot-prep
            # handler is needed; data points route to the active tab.
            engine.measurement_started.connect(
                self._on_measurement_started
            )
            engine.data_point_ready.connect(self._route_data_point)
            engine.measurement_finished.connect(
                self._on_agent_measurement_finished
            )
            engine.measurement_error.connect(
                self._on_measurement_error
            )
            engine.channel_changed.connect(
                self._on_channel_changed
            )
            logger.info(
                "EMSTAT_AGENT_MOCK set: agent uses the mock engine."
            )
        else:
            engine = self._engine
            connection = self._connection
        self._agent_engine = engine
        self._agent_connection = connection

        self._agent_adapter = EngineAdapter(engine, connection)
        self._agent_registry = build_registry(self._agent_adapter)
        # API key + model are configuration (File > Agent Settings...),
        # persisted in app settings; the panel just receives them.
        self._agent_panel = AgentDockPanel(
            self._agent_registry,
            api_key=get_agent_api_key(),
            model=get_agent_model(),
        )
        # First mathtext render costs ~325 ms (font cache); pay it here
        # rather than as a freeze on the first agent reply containing math.
        warm_up_math_renderer()
        for tool_def, handler in build_analysis_tools(
            figure_sink=self._agent_panel.figure_sink
        ):
            self._agent_registry.register(tool_def, handler)
        # analyze_swv lives outside vendor_analysis: the vendored 49.011
        # package has no SWV analyzer and is read-only, so the SWV peak
        # math is native code that registers through the same seam.
        for tool_def, handler in build_swv_analysis_tools(
            figure_sink=self._agent_panel.figure_sink
        ):
            self._agent_registry.register(tool_def, handler)
        # Preset/sequence tools (save_preset/save_sequence/load_preset/
        # load_sequence). They open native file dialogs so the operator
        # chooses the location, exactly like the sequencer's Save/Load.
        self._agent_file_dialogs = _AgentFileDialogs(
            self, self._default_export_dir()
        )
        for tool_def, handler in build_preset_tools(
            file_dialog=self._agent_file_dialogs,
            is_busy=self._agent_engine.isRunning,
        ):
            self._agent_registry.register(tool_def, handler)

        dock = QDockWidget("Agent", self)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        dock.setWidget(self._agent_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        # Sane first-open width so the dock does not crush the plots.
        self._agent_panel.setMinimumWidth(340)
        self.resizeDocks([dock], [420], Qt.Orientation.Horizontal)
        self._agent_dock = dock

    @pyqtSlot(object)
    def _on_agent_measurement_finished(
        self, result: MeasurementResult
    ) -> None:
        """Finish handling for agent-driven (mock) runs.

        Mirrors ``_on_measurement_finished`` minus the modal dialogs:
        an agent-initiated run must never pop a blocking export/auto-
        save prompt mid-conversation. The result stays exportable via
        File > Export.

        Args:
            result: The finished run's MeasurementResult.
        """
        self._run_in_progress = False
        self._last_result = result
        self._meas_panel.set_idle()
        self._export_action.setEnabled(True)
        self._status_progress.setText("Idle")
        if self._plot_container is not None:
            self._plot_container.on_measurement_finished()
            self._finalize_run_tooltip(result)
        n_points = result.num_points
        n_channels = len(result.measured_channels)
        self.statusBar().showMessage(
            f"Agent measurement complete: {n_points} points "
            f"across {n_channels} channel(s)"
        )
        logger.info(
            "Agent measurement complete: %d points, %d channel(s).",
            n_points,
            n_channels,
        )

    def _build_sequence_dock(self) -> None:
        """Build the Sequence dock hosting the preset sequencer panel.

        Mirrors ``_build_log_dock``: a movable/floatable ``QDockWidget``
        titled "Sequence" hosting a :class:`SequencePanel` that is wired
        to the shared PresetManager, engine, and a connection accessor by
        injection (the panel owns no engine of its own).  Sequence
        start/stop is bridged into the export-suppression + Start-disable
        state via :meth:`_on_sequence_started` / :meth:`_on_sequence_stopped`.
        """
        dock = QDockWidget("Sequence", self)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self._sequence_panel = SequencePanel(
            preset_manager=self._preset_mgr,
            engine=self._engine,
            connection_provider=self._sequence_connection,
            export_base_provider=self._sequence_export_base,
        )
        self._sequence_panel.sequence_started.connect(
            self._on_sequence_started
        )
        self._sequence_panel.sequence_stopped.connect(
            self._on_sequence_stopped
        )
        dock.setWidget(self._sequence_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self._sequence_dock = dock

    def _sequence_connection(self) -> Optional[PicoConnection]:
        """Return the connection for the sequencer, or None if down.

        The sequencer reuses the main window's single ``PicoConnection``
        so every step runs against the same already-open serial link.
        """
        if self._connection.is_connected:
            return self._connection
        return None

    def _sequence_export_base(self) -> tuple[str, bool]:
        """Return ``(base_export_dir, auto_save_all)`` for a sequence run.

        Mirrors the single-run policy exactly: the base resolves to the
        explicit auto-save folder (else the configured export dir), and
        ``auto_save_all`` is the GUI auto-save toggle. The base is ALWAYS
        supplied so the runner has a valid root whenever auto-save is on.
        """
        base = self._meas_panel.auto_save_directory() or get_export_dir()
        return base, self._meas_panel.is_auto_save_enabled()

    def _build_menu_bar(self) -> None:
        """Create the menu bar with File, Device, and Help menus."""
        menu_bar = self.menuBar()

        # -- File menu ----
        file_menu = menu_bar.addMenu("&File")

        self._export_action = QAction("&Export Results...", self)
        self._export_action.setShortcut("Ctrl+E")
        self._export_action.setEnabled(False)
        self._export_action.triggered.connect(self._on_export)
        file_menu.addAction(self._export_action)

        set_export_dir_action = QAction("Set Export &Folder...", self)
        set_export_dir_action.triggered.connect(self._on_set_export_dir)
        file_menu.addAction(set_export_dir_action)

        agent_settings_action = QAction("&Agent Settings...", self)
        agent_settings_action.triggered.connect(self._on_agent_settings)
        file_menu.addAction(agent_settings_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # -- Device menu ----
        device_menu = menu_bar.addMenu("&Device")

        self._connect_action = QAction("&Connect", self)
        self._connect_action.triggered.connect(self._on_connect)
        device_menu.addAction(self._connect_action)

        self._disconnect_action = QAction("&Disconnect", self)
        self._disconnect_action.setEnabled(False)
        self._disconnect_action.triggered.connect(self._on_disconnect)
        device_menu.addAction(self._disconnect_action)

        # -- Help menu ----
        help_menu = menu_bar.addMenu("&Help")

        about_action = QAction("&About", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _build_status_bar(self) -> None:
        """Create permanent status bar widgets."""
        self._status_conn = QLabel("Disconnected")
        self._status_conn.setStyleSheet(
            "padding: 0 8px; font-size: 11px;"
        )
        self.statusBar().addPermanentWidget(self._status_conn)

        self._status_progress = QLabel("Idle")
        self._status_progress.setStyleSheet(
            "padding: 0 8px; font-size: 11px;"
        )
        self.statusBar().addPermanentWidget(self._status_progress)

        self._status_channel = QLabel("CH: --")
        self._status_channel.setStyleSheet(
            "padding: 0 8px; font-size: 11px;"
        )
        self.statusBar().addPermanentWidget(self._status_channel)

        self.statusBar().showMessage("Ready")

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _wire_signals(self) -> None:
        """Connect all panel, engine, and plot signals."""
        # Connection panel -> main window
        self._conn_panel.connect_requested.connect(self._on_connect)
        self._conn_panel.disconnect_requested.connect(
            self._on_disconnect
        )

        # Technique panel -> plot container
        self._tech_panel.technique_changed.connect(
            self._on_technique_preview
        )

        # Electrode-config panel -> swap visible channel panel
        self._electrode_config_panel.mode_changed.connect(
            self._on_electrode_mode_changed
        )

        # Measurement controls -> engine actions
        self._meas_panel.start_requested.connect(
            self._on_start_measurement
        )
        self._meas_panel.stop_requested.connect(
            self._on_stop_measurement
        )
        self._meas_panel.halt_requested.connect(
            self._on_halt_measurement
        )
        self._meas_panel.resume_requested.connect(
            self._on_resume_measurement
        )

        # Preset selector -> main window
        self._tech_panel.preset_selected.connect(
            self._on_preset_selected
        )

        # Save preset button -> main window
        self._tech_panel.save_preset_requested.connect(
            self._on_save_preset
        )
        self._tech_panel.delete_preset_requested.connect(
            self._on_delete_preset
        )
        # "Import preset file..." entry repointed the active store; the
        # panel already repopulated its own dropdown, so just refresh the
        # rest of the preset-dependent UI.
        self._tech_panel.presets_imported.connect(
            self._on_presets_imported
        )

        # Engine signals -> GUI updates
        self._engine.data_point_ready.connect(
            self._route_data_point
        )
        self._engine.measurement_started.connect(
            self._on_measurement_started
        )
        self._engine.measurement_finished.connect(
            self._on_measurement_finished
        )
        self._engine.measurement_error.connect(
            self._on_measurement_error
        )
        self._engine.channel_changed.connect(
            self._on_channel_changed
        )
        self._engine.auto_save_completed.connect(
            self._on_auto_save_completed
        )

    @pyqtSlot(bool)
    def _on_debug_log_toggled(self, enabled: bool) -> None:
        """Switch root logging between DEBUG (verbose) and INFO.

        Affects both the console and the in-app log dock, since both
        attach to the root logger.

        Args:
            enabled: True for DEBUG, False for INFO.
        """
        level = logging.DEBUG if enabled else logging.INFO
        logging.getLogger().setLevel(level)
        self._update_log_switch_labels(enabled)
        logger.info(
            "Logging level set to %s.", logging.getLevelName(level)
        )

    def _update_log_switch_labels(self, debug: bool) -> None:
        """Emphasize the active side of the INFO/DEBUG switch."""
        # Match the "Log output:" caption: same 12px size and bold weight
        # so the three labels read as one consistent group — only the
        # colour changes to mark the active side.
        active = "color: #f5f5f5; font-weight: bold; font-size: 12px;"
        inactive = "color: #808080; font-weight: bold; font-size: 12px;"
        self._info_label.setStyleSheet(inactive if debug else active)
        self._debug_label.setStyleSheet(active if debug else inactive)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    @pyqtSlot()
    def _on_connect(self, port: str = "") -> None:
        """Handle connect request from panel or menu.

        Args:
            port: COM port path. If empty, reads from the connection
                panel's current selection.
        """
        if not port:
            port = self._conn_panel.selected_port()
        if not port:
            self._conn_panel.set_error("No port selected")
            return
        # A connect attempt is already in flight — ignore re-entry.
        if (
            self._connect_worker is not None
            and self._connect_worker.isRunning()
        ):
            return

        # The connect handshake does blocking serial I/O, so run it on a
        # worker thread and update the UI from its result signals — never
        # block the GUI event loop.
        self._conn_panel.set_connecting()
        self._connect_port = port
        worker = ConnectWorker(self._connection, port, self)
        worker.succeeded.connect(self._on_connect_succeeded)
        worker.failed.connect(self._on_connect_failed)
        # Free the QThread once it finishes so workers don't accumulate as
        # MainWindow children across repeated connect attempts.
        worker.finished.connect(worker.deleteLater)
        self._connect_worker = worker
        worker.start()

    @pyqtSlot(str)
    def _on_connect_succeeded(self, firmware: str) -> None:
        """Handle a successful connect handshake (GUI thread)."""
        self._connect_worker = None
        self._conn_panel.set_connected(firmware)
        self._update_ui_connected()
        logger.info(
            "Connected to %s (firmware: %s)",
            self._connect_port,
            firmware,
        )

    @pyqtSlot(str)
    def _on_connect_failed(self, message: str) -> None:
        """Handle a failed connect handshake (GUI thread)."""
        self._connect_worker = None
        self._conn_panel.set_error(message)
        logger.error("Connection failed: %s", message)

    @pyqtSlot()
    def _on_disconnect(self) -> None:
        """Handle disconnect request."""
        if self._engine.isRunning():
            self._engine.abort()
            self._engine.wait(3000)

        self._connection.disconnect()
        self._conn_panel.set_disconnected()
        self._update_ui_disconnected()
        logger.info("Disconnected.")

    # ------------------------------------------------------------------
    # Measurement lifecycle
    # ------------------------------------------------------------------

    def _default_export_dir(self) -> str:
        """Return the configured base directory for auto-saved run folders.

        Delegates to the configurable export-dir setting so forced and
        opt-in auto-save share one user-set location (File > Set Export
        Folder), rather than the legacy in-repo ``exports/``.
        """
        return get_export_dir()

    @pyqtSlot()
    def _on_start_measurement(self) -> None:
        """Gather config from panels and start the measurement engine."""
        if not self._connection.is_connected:
            self.statusBar().showMessage("Not connected to device.")
            return

        technique = self._tech_panel.selected_technique()
        params = self._tech_panel.get_params()

        # Resolve WE + RE/CE channels from the electrode-config mode.
        mode = self._electrode_config_panel.selected_mode()
        re_ce_channels: list[int] = []
        if mode == "manual":
            channels, re_ce_channels = (
                self._manual_channel_panel.selected_pairs()
            )
            if not channels:
                QMessageBox.warning(
                    self,
                    "No Channels",
                    "Enable at least one CH1-CH14 row before starting "
                    "in manual mode.",
                )
                self.statusBar().showMessage(
                    "No channels enabled in manual mode."
                )
                return
        else:
            channels = self._chan_panel.selected_channels()
            if not channels:
                QMessageBox.warning(
                    self,
                    "No Channels",
                    "Select at least one channel before starting.",
                )
                return
            # Mirror TechniqueConfig.__post_init__ defaulting so the
            # GUI side intent is explicit and visible.
            if mode == "external":
                re_ce_channels = [EXTERNAL_RE_CE_CHANNEL] * len(channels)
            else:  # on_board
                re_ce_channels = [ON_BOARD_RE_CE_CHANNEL] * len(channels)

        # Build auto-save config purely from the GUI toggle — auto-save is
        # fully opt-in for every technique (no forced provenance saves).
        # Reset the active flag first so a stale True from a previous run
        # that ended in error can't mislabel this run.
        auto_save = None
        self._auto_save_active = False
        if self._meas_panel.is_auto_save_enabled():
            auto_dir = (
                self._meas_panel.auto_save_directory()
                or self._default_export_dir()
            )
            auto_save = AutoSaveConfig(enabled=True, output_dir=auto_dir)
            self._auto_save_active = True

        try:
            config = TechniqueConfig(
                technique=technique,
                params=params,
                channels=channels,
                auto_save=auto_save,
                continuous=False,
                re_ce_channels=re_ce_channels,
                electrode_config_mode=mode,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Invalid Electrode Configuration",
                str(exc),
            )
            self.statusBar().showMessage(
                f"Invalid electrode config: {exc}"
            )
            return

        # The live-plot tab is created when the engine emits
        # measurement_started (see _on_measurement_started).
        try:
            self._engine.start_measurement(self._connection, config)
        except RuntimeError as exc:
            QMessageBox.warning(
                self, "Engine Busy", str(exc)
            )

    @pyqtSlot()
    def _on_stop_measurement(self) -> None:
        """Abort the running measurement."""
        self._engine.abort()

    @pyqtSlot()
    def _on_halt_measurement(self) -> None:
        """Pause the running measurement."""
        self._engine.halt()
        self._meas_panel.set_halted()
        self._status_progress.setText("Halted")

    @pyqtSlot()
    def _on_resume_measurement(self) -> None:
        """Resume a halted measurement."""
        self._engine.resume()
        self._meas_panel.set_running()
        self._status_progress.setText("Running")

    @pyqtSlot()
    def _on_sequence_started(self) -> None:
        """Enter sequence mode: suppress export prompts, lock Start.

        While a sequence runs, every step's ``measurement_finished`` is
        auto-saved (no modal prompt) and the single-run Start control
        stays disabled so the user can't launch a competing measurement.
        """
        self._sequence_active = True
        # Per-step tabs accumulate to the right of any existing plots
        # (nothing already plotted is overwritten); the first step
        # consumes the scratch tab, later steps each open a new one.
        self._seq_plot_n = 0
        # Start collecting the results of steps that don't auto-save, for
        # the end-of-run save prompt.
        self._sequence_results = []
        self._meas_panel.set_disabled()
        self._status_progress.setText("Sequence running")

    @pyqtSlot()
    def _on_sequence_stopped(self) -> None:
        """Leave sequence mode and offer to save any unsaved step data.

        This is the SINGLE terminal hook — the panel emits
        ``sequence_stopped`` on clean completion, user stop, and step
        error alike — so no ending path can silently discard the
        retained (non-auto-saved) step results.
        """
        self._sequence_active = False
        # Restore Start only when a device is still connected; the engine
        # is idle (or winding down an abort) once the sequence has stopped.
        if self._connection.is_connected:
            self._meas_panel.set_idle()
        else:
            self._meas_panel.set_disabled()
        self._status_progress.setText("Idle")
        # Never leave the central area tab-less (a first step that fails
        # before measurement_started would otherwise leave zero tabs and
        # a dead technique preview until the next run).
        if self._plot_tabs.count() == 0:
            self._scratch_container = self._add_plot_tab(
                self._tech_panel.selected_technique() or "",
                "Live",
                tooltip=_SCRATCH_TOOLTIP,
            )
        self._offer_sequence_save()

    def _offer_sequence_save(self) -> None:
        """Prompt to save retained (non-auto-saved) sequence step data."""
        results = self._sequence_results
        self._sequence_results = []
        if not results:
            return

        n = len(results)
        reply = QMessageBox.question(
            self,
            "Save Sequence Data",
            f"The sequence ended with {n} completed step(s) that were "
            "not auto-saved. Save them now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("Sequence data discarded.")
            return

        try:
            parent = self._save_sequence_results(results)
        except Exception as exc:  # noqa: BLE001 - surface, don't crash
            logger.error("Sequence save failed: %s", exc)
            QMessageBox.warning(
                self, "Save Failed", f"Could not save sequence: {exc}"
            )
            return
        self.statusBar().showMessage(f"Sequence saved to {parent}")
        QMessageBox.information(
            self,
            "Sequence Saved",
            f"Saved {n} step(s) to:\n{parent}",
        )

    def _save_sequence_results(
        self, results: list[MeasurementResult]
    ) -> str:
        """Write every retained step into one shared sequence folder.

        Uses the same layout helpers as the runner's live auto-save path
        (``make_sequence_dir`` + ``sequence_step_dirname``), so both ways
        of saving a sequence land as a ``<stamp>_sequence`` parent with
        one ``stepNN_<technique>`` subfolder per step (per-channel CSVs
        plus a ``.pssession`` each). A modal progress dialog keeps the
        UI responsive on long multi-channel saves.

        Args:
            results: The per-step results collected during the run.

        Returns:
            The absolute path of the created ``<stamp>_sequence`` parent.
        """
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        parent = make_sequence_dir(get_export_dir(), stamp)
        progress = QProgressDialog(
            "Saving sequence steps...", None, 0, len(results), self
        )
        progress.setWindowModality(
            Qt.WindowModality.ApplicationModal
        )
        progress.setMinimumDuration(500)
        try:
            for i, result in enumerate(results):
                progress.setValue(i)
                technique = result.technique or "unknown"
                step_dir = os.path.join(
                    parent, sequence_step_dirname(i, technique)
                )
                os.makedirs(step_dir, exist_ok=True)
                self._write_csv_files(result, step_dir)
                try:
                    PsSessionExporter().export_pssession(
                        result,
                        os.path.join(
                            step_dir, f"{technique}.pssession"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - CSVs written
                    logger.error(
                        "pssession export failed for step %d: %s",
                        i + 1,
                        exc,
                    )
            progress.setValue(len(results))
        finally:
            progress.close()
        logger.info(
            "Saved %d sequence step(s) to %s", len(results), parent
        )
        return parent

    @pyqtSlot(str)
    def _on_measurement_started(self, technique: str) -> None:
        """Handle measurement_started signal from engine."""
        # Give the run its own live-plot tab to the right and never
        # overwrite an existing plot: a single run opens a fresh tab (or
        # consumes the pristine scratch tab), and a sequence accumulates
        # one tab per step. Each tab is configured for the technique
        # actually starting, so a mixed CV/EIS/CA sequence never plots one
        # step on another's axes.
        if self._sequence_active:
            self._seq_plot_n += 1
            self._begin_run_tab(
                technique, f"{self._seq_plot_n}·{technique.upper()}"
            )
        else:
            self._begin_run_tab(technique, technique.upper() or "Run")
        self._run_in_progress = True
        # Prevent Export from silently writing the prior run's data
        # if the user invokes it before this run completes.
        self._last_result = None
        # Fresh per-step auto-save tracking: auto_save_completed (emitted
        # before measurement_finished) fills this in when the run wrote.
        self._step_autosave_dir = None
        self._export_action.setEnabled(False)
        self._meas_panel.set_running()
        self._status_progress.setText(
            f"Running: {technique.upper()}"
        )
        self.statusBar().showMessage(
            f"Measurement started: {technique.upper()}"
        )
        logger.info("Measurement started: %s", technique)

    @pyqtSlot(object)
    def _on_measurement_finished(
        self, result: MeasurementResult
    ) -> None:
        """Handle measurement_finished signal from engine.

        Stores the result, updates UI to idle, and prompts for export.
        Agent-driven runs are delegated to the modal-free handler.
        """
        # The run is no longer writing to its tab: it can now be closed.
        self._run_in_progress = False
        # A run the agent started on the REAL engine must never pop a
        # modal mid-conversation: delegate to the modal-free handler.
        # (Mock-mode agent runs connect to it directly and never reach
        # here; the engine-identity guard keeps a concurrent mock-run
        # flag from suppressing a user run's export prompt.)
        if (
            self._agent_adapter is not None
            and self._agent_engine is self._engine
            and self._agent_adapter.consume_agent_run()
        ):
            self._on_agent_measurement_finished(result)
            return

        self._last_result = result
        self._export_action.setEnabled(True)
        if self._plot_container is not None:
            self._plot_container.on_measurement_finished()
            self._finalize_run_tooltip(result)

        n_points = result.num_points
        n_channels = len(result.measured_channels)
        self.statusBar().showMessage(
            f"Measurement complete: {n_points} points "
            f"across {n_channels} channel(s)"
        )
        logger.info(
            "Measurement complete: %d points, %d channel(s).",
            n_points,
            n_channels,
        )

        # In sequence mode each step finishing must NOT pop a modal
        # prompt (it would block the queue) and must NOT re-enable the
        # single-run Start control — the SequenceRunner drives the next
        # step and the sequence_stopped signal restores the controls.
        if self._sequence_active:
            # Per-step export prompts stay suppressed mid-queue. A step
            # that auto-saved (its CSVs + _script.mscr are already on
            # disk) gets its .pssession written into the same folder now
            # — the convention is per-channel CSV + one .pssession per
            # run — and is NOT retained. Steps that didn't auto-save are
            # retained for the end-of-run save prompt.
            if self._step_autosave_dir:
                technique = result.technique or "unknown"
                try:
                    PsSessionExporter().export_pssession(
                        result,
                        os.path.join(
                            self._step_autosave_dir,
                            f"{technique}.pssession",
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - CSVs on disk
                    logger.error(
                        "Step .pssession export failed: %s", exc
                    )
            else:
                self._sequence_results.append(result)
            self._auto_save_active = False
            return

        self._meas_panel.set_idle()
        self._status_progress.setText("Idle")

        # If auto-save was active, data is already on disk
        if self._auto_save_active:
            self._auto_save_active = False
            QMessageBox.information(
                self,
                "Auto-Save Complete",
                f"Data auto-saved during measurement.\n"
                f"{n_points} points across {n_channels} channel(s).\n\n"
                "Use File > Export for additional formats.",
            )
        else:
            self._prompt_export(result)

    @pyqtSlot(str)
    def _on_measurement_error(self, message: str) -> None:
        """Handle measurement_error signal from engine."""
        # The run has terminated: release the recording tab for closing.
        self._run_in_progress = False
        # Consume agent attribution FIRST (every termination emits
        # exactly one of finished/error, so the flag must be taken on
        # both paths). An agent-driven run's error is already returned
        # to the agent as a structured tool result — surfacing it again
        # as a blocking dialog mid-conversation helps nobody; status
        # bar + log retain the record.
        agent_run = (
            self._agent_adapter is not None
            and self._agent_engine is self._engine
            and self._agent_adapter.consume_agent_run()
        )
        self._meas_panel.set_idle()
        # Clear the auto-save flag: a run that ended in error (including a
        # user abort, which routes through measurement_error) must not
        # leave the flag set, or the next finished run would be mislabeled
        # as auto-saved.
        self._auto_save_active = False
        self._status_progress.setText("Error")
        self.statusBar().showMessage(f"Error: {message}")
        logger.error("Measurement error: %s", message)

        # Enable export if we collected any data (e.g. abort mid-EIS)
        if (
            self._engine.result is not None
            and self._engine.result.num_points > 0
        ):
            self._last_result = self._engine.result
            self._export_action.setEnabled(True)

        if "aborted" not in message.lower() and not agent_run:
            QMessageBox.critical(
                self, "Measurement Error", message
            )

    @pyqtSlot(int)
    def _on_channel_changed(self, channel: int) -> None:
        """Update status bar with the current MUX channel."""
        self._status_channel.setText(f"CH: {channel}")

    @pyqtSlot(str)
    def _on_electrode_mode_changed(self, mode: str) -> None:
        """Swap which channel panel is visible based on wiring mode.

        Modes ``external`` and ``on_board`` use the 4x4 grid panel
        because all 16 WE channels are valid.  Mode ``manual``
        switches to the 14-row pairing table because CH15+CH16 are
        infrastructure-reserved.
        """
        if mode == "manual":
            self._chan_panel.setVisible(False)
            self._manual_channel_panel.setVisible(True)
        else:
            self._chan_panel.setVisible(True)
            self._manual_channel_panel.setVisible(False)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _prompt_export(self, result: MeasurementResult) -> None:
        """Ask the user whether to export results after measurement.

        Args:
            result: The completed measurement result.
        """
        reply = QMessageBox.question(
            self,
            "Export Results",
            f"Measurement complete ({result.num_points} points).\n"
            "Export results to CSV and .pssession?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._do_export(result)

    @pyqtSlot()
    def _on_export(self) -> None:
        """Handle File > Export menu action."""
        if self._last_result is not None:
            self._do_export(self._last_result)
        else:
            QMessageBox.information(
                self,
                "No Data",
                "No measurement results to export.",
            )

    @pyqtSlot()
    def _on_agent_settings(self) -> None:
        """Edit + persist the agent's API key and model.

        Configuration lives here (File menu + app settings), not in the
        chat panel. Accepted values are persisted across launches and
        pushed to the running agent worker (effective next turn). An
        empty key clears the stored one so the ANTHROPIC_API_KEY
        environment variable is used.
        """
        dlg = AgentSettingsDialog(
            api_key=get_agent_api_key() or "",
            model=get_agent_model() or "",
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        key, model = dlg.values()
        set_agent_api_key(key or None)
        set_agent_model(model or None)
        self._agent_panel.set_api_key(key or None)
        self._agent_panel.set_model(model)
        self.statusBar().showMessage("Agent settings saved.")
        logger.info("Agent settings updated (model=%s).", model)

    @pyqtSlot()
    def _on_set_export_dir(self) -> None:
        """Choose and persist the default export directory.

        The selected folder is remembered across launches (via
        ``app_settings``) and used as the base for manual exports,
        auto-save, and sequence step output. Picking nothing leaves the
        current setting unchanged.
        """
        current = get_export_dir()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Set Export Folder",
            current,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not chosen:
            return
        set_export_dir(chosen)
        self.statusBar().showMessage(f"Export folder set to: {chosen}")
        logger.info("Export folder set to %s", chosen)

    def _do_export(self, result: MeasurementResult) -> None:
        """Export measurement results to a user-chosen directory.

        Creates a timestamped subdirectory under the chosen path and
        writes per-channel CSV files. If the exporters module is
        available, also writes a .pssession file.

        Args:
            result: The measurement result to export.
        """
        default_dir = get_export_dir()

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Export Directory",
            default_dir,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not directory:
            return

        # Create timestamped subdirectory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        technique = result.technique or "unknown"
        export_dir = os.path.join(
            directory, f"{timestamp}_{technique}"
        )
        os.makedirs(export_dir, exist_ok=True)

        try:
            self._write_csv_files(result, export_dir)

            # Write .pssession file alongside the CSVs
            pssession_path = os.path.join(
                export_dir, f"{technique}.pssession"
            )
            ps_exporter = PsSessionExporter()
            ps_exporter.export_pssession(result, pssession_path)

            self.statusBar().showMessage(
                f"Results exported to {export_dir}"
            )
            logger.info("Results exported to %s", export_dir)
        except Exception as exc:
            logger.error("Export failed: %s", exc)
            QMessageBox.critical(
                self, "Export Error", f"Export failed: {exc}"
            )

    def _write_csv_files(
        self, result: MeasurementResult, export_dir: str
    ) -> None:
        """Write per-channel CSV files for the measurement result.

        If the ``src.data.exporters`` module is available, delegates
        to ``CSVExporter``.  Otherwise, performs a basic CSV write.

        Args:
            result: The measurement result.
            export_dir: Directory to write files into.
        """
        try:
            from src.data.exporters import CSVExporter

            exporter = CSVExporter()
            exporter.export(result, export_dir)
            return
        except ImportError:
            logger.debug(
                "Exporters module not available; "
                "falling back to basic CSV export."
            )

        # Fallback: basic CSV export per channel
        for ch in result.measured_channels:
            ch_data = result.channel_data(ch)
            if not ch_data.data_points:
                continue
            filepath = os.path.join(
                export_dir, f"ch{ch:02d}.csv"
            )
            # Collect all variable names across data points
            all_vars: set[str] = set()
            for dp in ch_data.data_points:
                all_vars.update(dp.variables.keys())
            var_names = sorted(all_vars)
            with open(filepath, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp"] + var_names)
                for dp in ch_data.data_points:
                    row = [dp.timestamp]
                    for vn in var_names:
                        row.append(dp.variables.get(vn, ""))
                    writer.writerow(row)

    # ------------------------------------------------------------------
    # Presets and auto-save
    # ------------------------------------------------------------------

    def _load_last_preset_file(self) -> None:
        """Adopt the remembered last-used preset file, if any.

        Reads the ``last_preset_file`` pointer from app settings and, when
        it names an existing file, loads it into the active
        :class:`PresetManager` so the user's chosen preset store reappears
        across launches.  A stale/missing pointer is ignored silently so
        the default per-user store remains active.
        """
        remembered = get_last_preset_file()
        if not remembered or not os.path.isfile(remembered):
            return
        try:
            self._preset_mgr.load_from_path(remembered)
        except OSError as e:
            logger.warning(
                "Could not load last preset file %s: %s",
                remembered,
                e,
            )

    def _load_presets_into_ui(self) -> None:
        """Populate the technique panel preset combo box."""
        # Hand the panel the active manager so its "Import preset
        # file..." entry can repoint the store (sequencer feature).
        self._tech_panel.set_preset_manager(self._preset_mgr)
        presets = {
            k: p.name
            for k, p in self._preset_mgr.get_all().items()
        }
        deletable = {
            k for k in presets if not self._preset_mgr.is_builtin(k)
        }
        self._tech_panel.refresh_presets(presets, deletable=deletable)

    @pyqtSlot(str)
    def _on_presets_imported(self, path: str) -> None:
        """React to a preset-file import from the technique panel.

        The panel already reloaded the manager and repopulated its own
        dropdown; refresh the deletable-key bookkeeping by re-running the
        shared populate path and surface the change in the status bar.

        Args:
            path: The imported preset file path.
        """
        self._load_presets_into_ui()
        self.statusBar().showMessage(
            f"Imported presets from {os.path.basename(path)}"
        )
        logger.info("Presets imported from %s", path)

    @pyqtSlot()
    def _on_save_preset(self) -> None:
        """Prompt the user for a name and save current settings."""
        name, ok = QInputDialog.getText(
            self,
            "Save Preset",
            "Preset name:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        # Derive a key from the name (lowercase, underscores)
        key = name.lower().replace(" ", "_")

        technique = self._tech_panel.selected_technique()
        params = self._tech_panel.get_params()
        auto_save = self._meas_panel.is_auto_save_enabled()

        # Capture the live electrode-config mode so the preset round-trips
        # the wiring policy, not just technique params + channels
        # (sequencer feature). In manual mode the WE + RE/CE pairing both come
        # from the manual panel; in external/on_board the channel grid
        # supplies WE and re_ce_channels stays empty (TechniqueConfig
        # repopulates the mode default at run time).
        mode = self._electrode_config_panel.selected_mode()
        if mode == "manual":
            channels, re_ce_channels = (
                self._manual_channel_panel.selected_pairs()
            )
        else:
            channels = self._chan_panel.selected_channels()
            re_ce_channels = []

        preset = Preset(
            name=name,
            technique=technique,
            params=params,
            channels=channels,
            auto_save=auto_save,
            description=f"User preset: {name}",
            electrode_config_mode=mode,
            re_ce_channels=re_ce_channels,
        )
        self._preset_mgr.add_preset(key, preset)

        # Refresh the preset combo box
        self._load_presets_into_ui()

        self.statusBar().showMessage(f"Preset saved: {name}")
        logger.info("Saved preset: %s (key=%s)", name, key)

    @pyqtSlot(str)
    def _on_delete_preset(self, key: str) -> None:
        """Confirm and delete a user preset."""
        preset = self._preset_mgr.get_preset(key)
        if preset is None:
            return
        reply = QMessageBox.question(
            self,
            "Delete Preset",
            f'Delete preset "{preset.name}"? This cannot be undone.',
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._preset_mgr.delete_preset(key):
            self._load_presets_into_ui()
            self.statusBar().showMessage(
                f"Deleted preset: {preset.name}"
            )
            logger.info(
                "Deleted preset: %s (key=%s)", preset.name, key
            )
        else:
            QMessageBox.warning(
                self,
                "Delete Failed",
                f"Could not delete '{preset.name}' "
                "(built-in presets cannot be removed).",
            )

    @pyqtSlot(str)
    def _on_preset_selected(self, key: str) -> None:
        """Load a preset into the technique, channel, and auto-save panels."""
        preset = self._preset_mgr.get_preset(key)
        if preset is None:
            return

        self._tech_panel.set_technique(preset.technique)
        self._tech_panel.set_params(preset.params)

        # Restore the wiring mode and route the channels to the matching
        # panel (sequencer feature). Manual presets carry an explicit per-WE
        # RE/CE pairing; external/on_board presets only drive the grid.
        mode = preset.electrode_config_mode or "external"
        self._electrode_config_panel.set_mode(mode)
        if mode == "manual":
            self._manual_channel_panel.set_pairs(
                preset.channels, preset.re_ce_channels
            )
        else:
            self._chan_panel.set_channels(preset.channels)

        # Auto-save is opt-in and the user's checkbox choice is theirs:
        # selecting a preset never CLEARS a manually enabled auto-save,
        # and a preset's saved auto_save flag never forces it on.
        self._meas_panel.set_auto_save(
            self._meas_panel.is_auto_save_enabled(),
            self._default_export_dir(),
        )

        self.statusBar().showMessage(
            f"Loaded preset: {preset.name}"
        )
        logger.info("Loaded preset: %s", preset.name)

    @pyqtSlot(str)
    def _on_auto_save_completed(self, output_dir: str) -> None:
        """Record the run's auto-save dir and update the status bar.

        The engine emits this BEFORE ``measurement_finished``, so the
        finished handler can tell an auto-saved sequence step (gets its
        ``.pssession`` written here, not retained) from one that needs
        retaining for the end-of-run save prompt.
        """
        self._step_autosave_dir = output_dir
        dirname = os.path.basename(output_dir)
        self.statusBar().showMessage(
            f"Auto-saved to: {dirname}"
        )

    # ------------------------------------------------------------------
    # UI state management
    # ------------------------------------------------------------------

    def _update_ui_connected(self) -> None:
        """Update UI elements to reflect a connected state."""
        self._connect_action.setEnabled(False)
        self._disconnect_action.setEnabled(True)
        self._meas_panel.set_idle()
        self._status_conn.setText("Connected")

    def _update_ui_disconnected(self) -> None:
        """Update UI elements to reflect a disconnected state."""
        self._connect_action.setEnabled(True)
        self._disconnect_action.setEnabled(False)
        self._meas_panel.set_disabled()
        self._status_conn.setText("Disconnected")
        self._status_channel.setText("CH: --")
        self._status_progress.setText("Idle")

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_about(self) -> None:
        """Show the About dialog."""
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
            "Python GUI for the PalmSens EmStat Pico with MUX16 "
            "multiplexer.<br><br>"
            "Runs five verified electrochemical techniques with real-time "
            "multi-channel plotting and CSV/.pssession export."
            "<br><br>"
            "Built with PyQt6 and pyqtgraph.",
        )

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Ensure clean shutdown on window close."""
        # Reject any in-flight agent file dialog BEFORE the blocking worker
        # shutdown below: closing the window while the agent's modal dialog is
        # open otherwise leaves it orphaned over a dead window and freezes the
        # GUI thread in worker.wait(). Rejecting it lets the marshaled call
        # return (None = cancelled) so the worker can stop promptly.
        if getattr(self, "_agent_file_dialogs", None) is not None:
            self._agent_file_dialogs.close_active()
        # Stop the agent worker thread first so no tool can marshal
        # work onto the GUI/engine while the window tears down.
        if getattr(self, "_agent_panel", None) is not None:
            self._agent_panel.shutdown()
        if self._engine.isRunning():
            self._engine.abort()
            self._engine.wait(3000)
        # Let an in-flight connect handshake finish so its thread isn't
        # destroyed while running.
        if (
            self._connect_worker is not None
            and self._connect_worker.isRunning()
        ):
            self._connect_worker.wait(6000)
        if self._connection.is_connected:
            self._connection.disconnect()
        # Detach the log handler so it stops emitting into this widget
        # once the window is gone (prevents leak / use-after-free).
        if getattr(self, "_log_handler", None) is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        event.accept()


# ------------------------------------------------------------------
# Application entry point
# ------------------------------------------------------------------


def main() -> None:
    """Launch the ChatStat application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    import traceback

    def exception_hook(exc_type, exc_value, exc_tb):
        """Print unhandled exceptions instead of silently crashing."""
        traceback.print_exception(exc_type, exc_value, exc_tb)
        logging.error(
            "Unhandled exception: %s", exc_value, exc_info=True
        )

    sys.excepthook = exception_hook

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

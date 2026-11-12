"""GUI control panels for connection, technique, channels, and measurement.

Provides five panel widgets that compose together in the main window:

- ``ConnectionPanel`` -- COM port selection, connect/disconnect, status
- ``TechniquePanel`` -- technique dropdown with dynamic parameter fields
- ``ElectrodeConfigPanel`` -- picks a saved electrode setup by name
- ``ElectrodeWiringPanel`` -- 16-row table: each working electrode and
  the RE/CE position it uses (the one wiring model)
- ``MeasurementControlPanel`` -- Start, Stop, Halt, Resume buttons

All panels emit signals for state changes. No panel performs serial I/O
directly; the main window wires panel signals to the engine and connection.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from src.data.app_settings import set_last_preset_file
from src.data.models import (
    EXTERNAL_RE_CE_CHANNEL,
    MODE_C_MAX_CHANNEL,
    ON_BOARD_RE_CE_CHANNEL,
)
from src.data.presets import PresetManager
from src.techniques.scripts import supported_techniques, technique_params

logger = logging.getLogger(__name__)

# Sentinel ``itemData`` value marking the trailing "Import preset file..."
# entry in the preset combo box.  Selecting it opens a file dialog rather
# than loading a preset (sequencer feature — Phase 1).
_IMPORT_PRESET_SENTINEL = "__import_preset__"

# File dialog filter for externalized preset files (sequencer feature).
_PRESET_FILE_FILTER = "MUX16 presets (*.mux16)"

# -----------------------------------------------------------------------
# Friendly technique display names
# -----------------------------------------------------------------------

_TECHNIQUE_LABELS: dict[str, str] = {
    "cv": "Cyclic Voltammetry (CV)",
    "lsv": "Linear Sweep Voltammetry (LSV)",
    "dpv": "Differential Pulse Voltammetry (DPV)",
    "swv": "Square Wave Voltammetry (SWV)",
    "npv": "Normal Pulse Voltammetry (NPV)",
    "acv": "AC Voltammetry (ACV)",
    "ca": "Chronoamperometry (CA)",
    "fca": "Fixed-Potential CA (FCA)",
    "cp": "Chronopotentiometry (CP)",
    "ocp": "Open Circuit Potential (OCP)",
    "eis": "EIS (Potentiostatic)",
    "geis": "EIS (Galvanostatic)",
    "pad": "Preconcentration + DPV (PAD)",
    "lsp": "Linear Sweep Potentiometry (LSP)",
    "fcv": "Fast Cyclic Voltammetry (FCV)",
    "ca_alt_mux": "CA (MUX-Alternating)",
    # cp_alt_mux / ocp_alt_mux were listed here until 2026-07-28 but no
    # generator or defaults ever existed for them, so they were
    # unreachable. ca_alt_mux is the only MUX-alternating technique the
    # script generator implements.
}

# Parameter labels/ranges and the widget factory live in
# src/gui/parameter_form.py (the single authority shared with the
# sequence step editor); aliases kept for in-module readability.
from src.gui.parameter_form import (  # noqa: E402
    BANDWIDTH_HZ as _BANDWIDTH_HZ,
    CURRENT_RANGES as _CURRENT_RANGES,
    PARAM_LABELS as _PARAM_LABELS,
    create_param_widget,
    read_param_widget,
)


# =======================================================================
# ConnectionPanel
# =======================================================================


class ConnectionPanel(QGroupBox):
    """COM port selection, connect/disconnect, and status display.

    Signals:
        connect_requested(str): Emitted with the selected port name
            when the user clicks Connect.
        disconnect_requested(): Emitted when the user clicks Disconnect.
    """

    connect_requested = pyqtSignal(str)
    disconnect_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Connection", parent)
        self._connected = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Build the panel layout."""
        layout = QVBoxLayout(self)

        # Port selection row
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self._port_combo = QComboBox()
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(120)
        port_row.addWidget(self._port_combo, 1)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setToolTip("Scan for available COM ports")
        self._refresh_btn.clicked.connect(self.refresh_ports)
        port_row.addWidget(self._refresh_btn)
        layout.addLayout(port_row)

        # Connect / Disconnect buttons
        btn_row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        btn_row.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        btn_row.addWidget(self._disconnect_btn)
        layout.addLayout(btn_row)

        # Status indicator + firmware version
        status_row = QHBoxLayout()
        self._status_label = QLabel("Disconnected")
        self._status_label.setStyleSheet("color: #888; font-weight: bold;")
        status_row.addWidget(self._status_label)
        layout.addLayout(status_row)

        self._firmware_label = QLabel("Firmware: —")
        self._firmware_label.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self._firmware_label)

        # Initial port scan
        self.refresh_ports()

    # ---- Public API ------------------------------------------------------

    def refresh_ports(self) -> None:
        """Scan system for available serial ports and update combo box."""
        self._port_combo.clear()
        try:
            from serial.tools.list_ports import comports

            ports = sorted(comports(), key=lambda p: p.device)
            for port_info in ports:
                label = f"{port_info.device}"
                if port_info.description and port_info.description != "n/a":
                    label += f" — {port_info.description}"
                self._port_combo.addItem(label, port_info.device)
        except ImportError:
            logger.warning("serial.tools.list_ports not available.")

    def selected_port(self) -> str:
        """Return the currently selected port device path.

        Returns:
            The port string (e.g., 'COM3' or '/dev/ttyUSB0').
            Falls back to the combo box text if no data is stored.
        """
        data = self._port_combo.currentData()
        if data:
            return str(data)
        return self._port_combo.currentText().split(" — ")[0].strip()

    def set_connected(self, firmware: str = "") -> None:
        """Update UI to reflect a connected state.

        Args:
            firmware: Firmware version string to display.
        """
        self._connected = True
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._port_combo.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._status_label.setText("Connected")
        self._status_label.setStyleSheet(
            "color: #2ca02c; font-weight: bold;"
        )
        if firmware:
            self._firmware_label.setText(f"Firmware: {firmware}")

    def set_disconnected(self) -> None:
        """Update UI to reflect a disconnected state."""
        self._connected = False
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._port_combo.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._status_label.setText("Disconnected")
        self._status_label.setStyleSheet(
            "color: #888; font-weight: bold;"
        )
        self._firmware_label.setText("Firmware: —")

    def set_error(self, message: str) -> None:
        """Display an error state.

        Args:
            message: Short error description to show.
        """
        self._status_label.setText(f"Error: {message}")
        self._status_label.setStyleSheet(
            "color: #d62728; font-weight: bold;"
        )
        # Re-enable the controls so the user can retry after a failure.
        self._connect_btn.setEnabled(True)
        self._port_combo.setEnabled(True)
        self._refresh_btn.setEnabled(True)

    def set_connecting(self) -> None:
        """Show a busy state while the connect handshake runs off-thread.

        Disables both connect AND disconnect controls so the user can't
        fire a second connect, or a disconnect, while a handshake is in
        flight (which would race two operations on one connection).
        """
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(False)
        self._port_combo.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._status_label.setText("Connecting...")
        self._status_label.setStyleSheet(
            "color: #888; font-weight: bold;"
        )

    @property
    def is_connected(self) -> bool:
        """Whether the panel reflects a connected state."""
        return self._connected

    # ---- Private slots ---------------------------------------------------

    def _on_connect_clicked(self) -> None:
        """Emit connect_requested with the selected port."""
        port = self.selected_port()
        if port:
            self.connect_requested.emit(port)
        else:
            self.set_error("No port selected")

    def _on_disconnect_clicked(self) -> None:
        """Emit disconnect_requested."""
        self.disconnect_requested.emit()


# =======================================================================
# TechniquePanel
# =======================================================================


class TechniquePanel(QGroupBox):
    """Technique selection with dynamic parameter fields.

    Uses ``supported_techniques()`` to populate the dropdown and
    ``technique_params()`` to generate appropriate spin boxes for
    each technique's default parameters.

    Signals:
        technique_changed(str): Emitted with the technique key when
            the user selects a different technique.
        params_changed(): Emitted when any parameter value changes.
        save_preset_requested(): Emitted when the user clicks Save.
        presets_imported(str): Emitted with the imported file path after
            the user picks a new preset file via "Import preset file...".
            The main window reacts by repopulating any preset-dependent UI.
    """

    technique_changed = pyqtSignal(str)
    params_changed = pyqtSignal()
    preset_selected = pyqtSignal(str)  # preset key
    save_preset_requested = pyqtSignal()
    delete_preset_requested = pyqtSignal(str)  # preset key to delete
    presets_imported = pyqtSignal(str)  # imported preset file path

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Technique", parent)
        self._param_widgets: dict[str, QWidget] = {}
        self._deletable_keys: set[str] = set()
        # PresetManager whose store the "Import preset file..." entry
        # repoints.  Injected by the main window via set_preset_manager;
        # without it the import entry is inert (no manager to load into).
        self._preset_mgr: Optional[PresetManager] = None
        # Path used to override the app-settings store in tests so the
        # real per-user file is never touched.
        self._settings_path: Optional[str] = None
        # Combo index selected before the import dialog opened, so a
        # cancelled dialog can restore the prior selection.
        self._pre_import_index: int = 0
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Build the panel layout."""
        layout = QVBoxLayout(self)

        # Preset selector
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self._preset_combo = QComboBox()
        self._preset_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._preset_combo.addItem("(No Preset)", "")
        self._preset_combo.currentIndexChanged.connect(
            self._on_preset_selected
        )
        preset_row.addWidget(self._preset_combo, 1)

        # Icon-only buttons to keep the preset row compact; tooltips
        # carry the labels for discoverability.
        style = self.style()

        self._save_preset_btn = QPushButton()
        self._save_preset_btn.setIcon(
            style.standardIcon(
                QStyle.StandardPixmap.SP_DialogSaveButton
            )
        )
        self._save_preset_btn.setToolTip("Save current settings as preset")
        self._save_preset_btn.setFixedWidth(32)
        self._save_preset_btn.clicked.connect(
            self.save_preset_requested.emit
        )
        preset_row.addWidget(self._save_preset_btn)

        self._delete_preset_btn = QPushButton()
        self._delete_preset_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        )
        self._delete_preset_btn.setToolTip("Delete the selected preset")
        self._delete_preset_btn.setFixedWidth(32)
        self._delete_preset_btn.setEnabled(False)
        self._delete_preset_btn.clicked.connect(self._on_delete_clicked)
        preset_row.addWidget(self._delete_preset_btn)
        layout.addLayout(preset_row)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        # Technique selector
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Technique:"))
        self._technique_combo = QComboBox()
        self._technique_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        # Populate with supported techniques
        for tech_key in supported_techniques():
            label = _TECHNIQUE_LABELS.get(tech_key, tech_key.upper())
            self._technique_combo.addItem(label, tech_key)

        self._technique_combo.currentIndexChanged.connect(
            self._on_technique_selected
        )
        selector_row.addWidget(self._technique_combo, 1)
        layout.addLayout(selector_row)

        # Parameter area (flat — the outer Settings dock scrolls).
        self._param_container = QWidget()
        self._param_layout = QGridLayout(self._param_container)
        self._param_layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self._param_container)

        # Load params for the initial technique
        if self._technique_combo.count() > 0:
            self._load_params_for_technique(
                self._technique_combo.currentData()
            )

    # ---- Public API ------------------------------------------------------

    def selected_technique(self) -> str:
        """Return the currently selected technique key.

        Returns:
            Lowercase technique identifier (e.g., 'cv', 'dpv').
        """
        return self._technique_combo.currentData() or ""

    def get_params(self) -> dict[str, Any]:
        """Collect current parameter values from the spin boxes.

        Returns:
            Dict mapping parameter names to their current values.
        """
        return {
            name: read_param_widget(name, widget)
            for name, widget in self._param_widgets.items()
        }

    def set_technique(self, technique: str) -> None:
        """Programmatically select a technique.

        Args:
            technique: Technique key (e.g., 'cv').
        """
        for i in range(self._technique_combo.count()):
            if self._technique_combo.itemData(i) == technique.lower():
                self._technique_combo.setCurrentIndex(i)
                return

    def set_params(self, params: dict[str, Any]) -> None:
        """Programmatically set parameter values.

        Only sets values for parameters that exist in the current
        technique's widget set.

        Args:
            params: Dict mapping parameter names to values.
        """
        for name, value in params.items():
            widget = self._param_widgets.get(name)
            if widget is None:
                continue
            if isinstance(widget, QDoubleSpinBox):
                widget.setValue(float(value))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(value))
            elif isinstance(widget, QComboBox):
                idx = widget.findText(str(value))
                if idx >= 0:
                    widget.setCurrentIndex(idx)

    def refresh_presets(
        self,
        presets: dict[str, str],
        deletable: Optional[set[str]] = None,
    ) -> None:
        """Reload the preset combo box.

        Args:
            presets: Dict mapping preset keys to display names.
            deletable: Keys the user is allowed to delete (built-ins
                excluded). If None, no preset is deletable.
        """
        self._deletable_keys = deletable or set()
        # Preserve the current selection across the rebuild so a refresh
        # (e.g. the main window re-running its populate path right after
        # an import) cannot visually reset the dropdown to "(No Preset)"
        # while the parameter form still shows the selected preset.
        prev_key = self._preset_combo.currentData()
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        self._preset_combo.addItem("(No Preset)", "")
        for key, name in sorted(presets.items()):
            self._preset_combo.addItem(name, key)
        # Trailing "Import preset file..." action entry (sequencer feature).
        # Always last so it reads as a command, not a preset.
        self._preset_combo.addItem(
            "Import preset file...", _IMPORT_PRESET_SENTINEL
        )
        restored = -1
        if prev_key and prev_key != _IMPORT_PRESET_SENTINEL:
            restored = self._preset_combo.findData(prev_key)
            if restored >= 0:
                self._preset_combo.setCurrentIndex(restored)
        self._preset_combo.blockSignals(False)
        self._delete_preset_btn.setEnabled(
            restored >= 0 and prev_key in self._deletable_keys
        )

    def set_preset_manager(
        self,
        manager: PresetManager,
        settings_path: Optional[str] = None,
    ) -> None:
        """Inject the PresetManager the import entry loads into.

        Args:
            manager: Active :class:`PresetManager`; "Import preset
                file..." calls ``load_from_path`` on it.
            settings_path: Optional app-settings file override (tests
                point this at a temp file so the real per-user store is
                untouched).
        """
        self._preset_mgr = manager
        self._settings_path = settings_path

    # ---- Internal --------------------------------------------------------

    def _on_preset_selected(self, index: int) -> None:
        """Handle preset combo box selection change."""
        key = self._preset_combo.itemData(index)
        # The trailing "Import preset file..." entry is an action, not a
        # preset: route to the file dialog and never treat it as a key.
        if key == _IMPORT_PRESET_SENTINEL:
            self._delete_preset_btn.setEnabled(False)
            self._on_import_preset_file()
            return
        self._delete_preset_btn.setEnabled(
            bool(key) and key in self._deletable_keys
        )
        # Remember the last real selection so a cancelled import dialog
        # can fall back to it.
        self._pre_import_index = index
        if key:
            self.preset_selected.emit(key)

    def _on_import_preset_file(self) -> None:
        """Open a file dialog and load the chosen ``*.mux16`` store.

        On a successful pick the manager's active store is repointed to
        the file, the dropdown is repopulated, the path is remembered as
        last-used, and ``presets_imported`` is emitted.  A cancelled
        dialog (or a missing manager) restores the prior selection and
        does nothing else.
        """
        if self._preset_mgr is None:
            logger.warning(
                "Import preset selected with no PresetManager set."
            )
            self._restore_pre_import_selection()
            return

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import preset file",
            "",
            _PRESET_FILE_FILTER,
        )
        if not path:
            # Cancelled: leave the manager untouched, restore selection.
            self._restore_pre_import_selection()
            return

        try:
            self._preset_mgr.load_from_path(path)
        except (OSError, ValueError) as exc:
            # load_from_path parses strictly and raises BEFORE touching
            # the manager, so a corrupt/wrong file leaves the store and
            # the active path exactly as they were. Surface the failure
            # — silently doing nothing reads as a successful import.
            logger.warning(
                "Could not import preset file %s: %s", path, exc
            )
            QMessageBox.warning(
                self,
                "Import Failed",
                f"Could not import preset file:\n{path}\n\n{exc}",
            )
            self._restore_pre_import_selection()
            return

        # Persist the chosen file as last-used (sequencer feature).
        set_last_preset_file(path, path=self._settings_path)

        # Repopulate the dropdown from the freshly loaded store.
        presets = {
            k: p.name for k, p in self._preset_mgr.get_all().items()
        }
        deletable = {
            k for k in presets if not self._preset_mgr.is_builtin(k)
        }
        self.refresh_presets(presets, deletable=deletable)
        # Land on the first real preset rather than "(No Preset)" so an
        # import immediately surfaces a usable config (index 0 is the
        # "(No Preset)" placeholder; index 1 is the first real preset when
        # the imported store is non-empty).
        self._select_first_real_preset()
        self.presets_imported.emit(path)
        logger.info("Imported preset file: %s", path)

    def _select_first_real_preset(self) -> None:
        """Select the first real preset entry after a dropdown rebuild.

        Falls back to "(No Preset)" (index 0) when the store is empty.
        The first real preset sits at index 1 (index 0 is the placeholder
        and the trailing entry is the "Import preset file..." action).
        """
        first_real = -1
        for i in range(self._preset_combo.count()):
            key = self._preset_combo.itemData(i)
            if key and key != _IMPORT_PRESET_SENTINEL:
                first_real = i
                break
        idx = first_real if first_real >= 0 else 0
        self._pre_import_index = idx
        # currentIndexChanged fires _on_preset_selected, which emits
        # preset_selected so the main window loads the config.
        self._preset_combo.setCurrentIndex(idx)

    def _restore_pre_import_selection(self) -> None:
        """Restore the combo to the selection before the import dialog."""
        count = self._preset_combo.count()
        idx = self._pre_import_index
        if idx < 0 or idx >= count:
            idx = 0
        self._preset_combo.blockSignals(True)
        self._preset_combo.setCurrentIndex(idx)
        self._preset_combo.blockSignals(False)

    def _on_delete_clicked(self) -> None:
        """Emit delete request for the currently selected preset."""
        key = self._preset_combo.currentData()
        if key:
            self.delete_preset_requested.emit(key)

    def _on_technique_selected(self, index: int) -> None:
        """Handle technique combo box selection change."""
        tech_key = self._technique_combo.itemData(index)
        if tech_key:
            self._load_params_for_technique(tech_key)
            self.technique_changed.emit(tech_key)

    def _load_params_for_technique(self, technique: str) -> None:
        """Rebuild parameter fields for the selected technique.

        Args:
            technique: Technique key.
        """
        # Clear existing parameter widgets
        self._param_widgets.clear()
        # Remember the technique so the cr dropdown can offer the right
        # current-range ladder (EIS/GEIS run mode 3, a different ladder).
        self._param_technique = technique
        while self._param_layout.count():
            item = self._param_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        # Get default params for this technique
        try:
            defaults = technique_params(technique)
        except ValueError:
            logger.warning("No params for technique: %s", technique)
            return

        # Create a row for each parameter
        row = 0
        for param_name, default_value in defaults.items():
            label_text, unit = _PARAM_LABELS.get(
                param_name, (param_name, "")
            )
            display = f"{label_text}"
            if unit:
                display += f" ({unit})"

            label = QLabel(display)
            self._param_layout.addWidget(label, row, 0)

            widget = self._create_param_widget(
                param_name, default_value
            )
            self._param_layout.addWidget(widget, row, 1)
            self._param_widgets[param_name] = widget
            row += 1

        # Add vertical spacer at the bottom
        self._param_layout.setRowStretch(row, 1)

    def _create_param_widget(
        self, name: str, default: Any
    ) -> QWidget:
        """Create an appropriate input widget for a parameter.

        Delegates to the shared factory in ``parameter_form`` (the
        single authority, also used by sequence step editors) so the
        two parameter UIs cannot drift.

        Args:
            name: Parameter name.
            default: Default value.

        Returns:
            A QDoubleSpinBox, QSpinBox, or QComboBox.
        """
        return create_param_widget(
            name, default, self.params_changed.emit,
            getattr(self, "_param_technique", None),
        )


# =======================================================================
# MeasurementControlPanel
# =======================================================================


class MeasurementControlPanel(QGroupBox):
    """Start, Stop (abort), Halt, and Resume buttons.

    Button enable/disable logic:

    - **Idle**: Start enabled, others disabled
    - **Running**: Stop and Halt enabled, Start disabled, Resume disabled
    - **Halted**: Resume and Stop enabled, Start and Halt disabled

    Signals:
        start_requested(): User clicked Start.
        stop_requested(): User clicked Stop (abort).
        halt_requested(): User clicked Halt.
        resume_requested(): User clicked Resume.
    """

    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    halt_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    auto_save_changed = pyqtSignal(bool)
    auto_save_dir_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Measurement", parent)
        self._auto_save_dir: str = ""
        self._setup_ui()
        self.set_idle()

    def _setup_ui(self) -> None:
        """Build the button layout."""
        layout = QVBoxLayout(self)

        # Top row: Start / Stop
        top_row = QHBoxLayout()
        self._start_btn = QPushButton("Start")
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #2ca02c; color: white; "
            "font-weight: bold; padding: 6px; }"
        )
        self._start_btn.clicked.connect(self.start_requested.emit)
        top_row.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setStyleSheet(
            "QPushButton { background-color: #d62728; color: white; "
            "font-weight: bold; padding: 6px; }"
        )
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        top_row.addWidget(self._stop_btn)
        layout.addLayout(top_row)

        # Bottom row: Halt / Resume
        bottom_row = QHBoxLayout()
        self._halt_btn = QPushButton("Halt")
        self._halt_btn.clicked.connect(self.halt_requested.emit)
        bottom_row.addWidget(self._halt_btn)

        self._resume_btn = QPushButton("Resume")
        self._resume_btn.clicked.connect(self.resume_requested.emit)
        bottom_row.addWidget(self._resume_btn)
        layout.addLayout(bottom_row)

        # Auto-save section
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        self._auto_save_cb = QCheckBox("Auto-save CSV during measurement")
        self._auto_save_cb.toggled.connect(self._on_auto_save_toggled)
        layout.addWidget(self._auto_save_cb)

        dir_row = QHBoxLayout()
        self._dir_label = QLabel("Directory: (not set)")
        self._dir_label.setStyleSheet("color: #666; font-size: 11px;")
        dir_row.addWidget(self._dir_label, 1)

        self._browse_btn = QPushButton("Browse...")
        self._browse_btn.setMaximumWidth(80)
        self._browse_btn.clicked.connect(self._on_browse_clicked)
        dir_row.addWidget(self._browse_btn)
        layout.addLayout(dir_row)

    # ---- Public state management -----------------------------------------

    def set_idle(self) -> None:
        """Set button states for idle (no measurement running)."""
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._halt_btn.setEnabled(False)
        self._resume_btn.setEnabled(False)

    def set_running(self) -> None:
        """Set button states for an active measurement."""
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._halt_btn.setEnabled(True)
        self._resume_btn.setEnabled(False)

    def set_halted(self) -> None:
        """Set button states for a halted measurement."""
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._halt_btn.setEnabled(False)
        self._resume_btn.setEnabled(True)

    def set_disabled(self) -> None:
        """Disable all buttons (e.g., when not connected)."""
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._halt_btn.setEnabled(False)
        self._resume_btn.setEnabled(False)

    # ---- Auto-save API ---------------------------------------------------

    def is_auto_save_enabled(self) -> bool:
        """Return whether auto-save is checked."""
        return self._auto_save_cb.isChecked()

    def auto_save_directory(self) -> str:
        """Return the configured auto-save directory."""
        return self._auto_save_dir

    def set_auto_save(
        self, enabled: bool, directory: str = ""
    ) -> None:
        """Programmatically set auto-save state.

        Args:
            enabled: Whether to enable auto-save.
            directory: Output directory path.
        """
        self._auto_save_cb.blockSignals(True)
        self._auto_save_cb.setChecked(enabled)
        self._auto_save_cb.blockSignals(False)
        if directory:
            self._auto_save_dir = directory
            self._dir_label.setText(
                f"Directory: {os.path.basename(directory)}"
            )
            self._dir_label.setToolTip(directory)

    # ---- Private slots ---------------------------------------------------

    def _on_auto_save_toggled(self, checked: bool) -> None:
        """Handle auto-save checkbox toggle."""
        self.auto_save_changed.emit(checked)

    def _on_browse_clicked(self) -> None:
        """Open directory picker for auto-save output."""
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Auto-Save Directory",
            self._auto_save_dir or "",
        )
        if path:
            self._auto_save_dir = path
            self._dir_label.setText(
                f"Directory: {os.path.basename(path)}"
            )
            self._dir_label.setToolTip(path)
            self.auto_save_dir_changed.emit(path)


# =======================================================================
# ElectrodeConfigPanel
# =======================================================================


#: First entry of the electrode-setup combo: the wiring is whatever the
#: wiring table below says, not a saved setup.
_CUSTOM_SETUP_LABEL = "(Custom)"


class ElectrodeConfigPanel(QGroupBox):
    """Electrode setup: pick a saved setup by name.

    The **Setup** row picks a saved electrode setup by name (see
    :mod:`src.data.electrode_presets`); the main window then fills the
    wiring table (:class:`ElectrodeWiringPanel`) to match. "(Custom)" in
    the Setup row means the table has been edited and is not a saved
    setup. There is no wiring mode to choose: every setup is a table of
    one RE/CE position per working electrode. See
    ``docs/enclosure_design.md`` for the physical wiring.

    Signals:
        electrode_preset_selected(str): Emitted with the key of a saved
            setup when the person picks one in the Setup row.
        save_electrode_preset_requested(): The save button was clicked.
        delete_electrode_preset_requested(str): The delete button was
            clicked with this setup selected.
    """

    electrode_preset_selected = pyqtSignal(str)
    save_electrode_preset_requested = pyqtSignal()
    delete_electrode_preset_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Electrode Configuration", parent)
        self._deletable: set[str] = set()
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Build the setup row."""
        layout = QVBoxLayout(self)
        layout.setSpacing(2)

        setup_row = QHBoxLayout()
        setup_row.addWidget(QLabel("Setup:"))
        self._setup_combo = QComboBox()
        self._setup_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._setup_combo.addItem(_CUSTOM_SETUP_LABEL, "")
        self._setup_combo.currentIndexChanged.connect(self._on_setup_selected)
        setup_row.addWidget(self._setup_combo, 1)

        style = self.style()
        self._save_setup_btn = QPushButton()
        self._save_setup_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self._save_setup_btn.setToolTip(
            "Save the current electrode wiring and channels as a named setup"
        )
        self._save_setup_btn.setFixedWidth(32)
        self._save_setup_btn.clicked.connect(
            self.save_electrode_preset_requested.emit
        )
        setup_row.addWidget(self._save_setup_btn)

        self._delete_setup_btn = QPushButton()
        self._delete_setup_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        )
        self._delete_setup_btn.setToolTip("Delete the selected setup")
        self._delete_setup_btn.setFixedWidth(32)
        self._delete_setup_btn.setEnabled(False)
        self._delete_setup_btn.clicked.connect(self._on_delete_setup_clicked)
        setup_row.addWidget(self._delete_setup_btn)
        layout.addLayout(setup_row)

    # ---- Public API ------------------------------------------------------

    def refresh_electrode_presets(
        self,
        presets: dict[str, str],
        tooltips: Optional[dict[str, str]] = None,
        deletable: Optional[set[str]] = None,
    ) -> None:
        """Reload the Setup combo, keeping the current selection if it stays.

        Args:
            presets: ``{key: display name}``.
            tooltips: Optional ``{key: text}`` shown when hovering an entry.
            deletable: Keys the delete button may remove.
        """
        previous = self._setup_combo.currentData()
        self._deletable = set(deletable or ())
        self._setup_combo.blockSignals(True)
        self._setup_combo.clear()
        self._setup_combo.addItem(_CUSTOM_SETUP_LABEL, "")
        for key, name in sorted(presets.items(), key=lambda kv: kv[1].lower()):
            self._setup_combo.addItem(name, key)
            if tooltips and key in tooltips:
                self._setup_combo.setItemData(
                    self._setup_combo.count() - 1,
                    tooltips[key],
                    Qt.ItemDataRole.ToolTipRole,
                )
        restored = self._setup_combo.findData(previous) if previous else -1
        self._setup_combo.setCurrentIndex(restored if restored >= 0 else 0)
        self._setup_combo.blockSignals(False)
        self._update_delete_enabled()

    def selected_electrode_preset(self) -> str:
        """Key of the selected setup, or ``""`` for "(Custom)"."""
        return str(self._setup_combo.currentData() or "")

    def select_electrode_preset(self, key: str) -> None:
        """Show *key* as the selected setup without emitting a signal."""
        index = self._setup_combo.findData(key) if key else 0
        self._setup_combo.blockSignals(True)
        self._setup_combo.setCurrentIndex(index if index >= 0 else 0)
        self._setup_combo.blockSignals(False)
        self._update_delete_enabled()

    def show_custom(self) -> None:
        """Show "(Custom)": the wiring no longer matches a saved setup."""
        self.select_electrode_preset("")

    # ---- Private slots ---------------------------------------------------

    def _update_delete_enabled(self) -> None:
        self._delete_setup_btn.setEnabled(
            self.selected_electrode_preset() in self._deletable
        )

    def _on_setup_selected(self, index: int) -> None:
        """Emit the chosen setup's key (nothing for "(Custom)")."""
        self._update_delete_enabled()
        key = self._setup_combo.itemData(index)
        if key:
            self.electrode_preset_selected.emit(str(key))

    def _on_delete_setup_clicked(self) -> None:
        key = self.selected_electrode_preset()
        if key in self._deletable:
            self.delete_electrode_preset_requested.emit(key)


# =======================================================================
# ElectrodeWiringPanel
# =======================================================================


#: Working-electrode rows in the wiring table, and the RE/CE positions a
#: row can point at: all sixteen multiplexer positions.
_WIRING_ROWS = 16


class ElectrodeWiringPanel(QGroupBox):
    """One table: each working electrode and the RE/CE position it uses.

    This is the only wiring model. A setup with a shared external
    reference and counter is a table that points every enabled row at
    position 15 (``EXTERNAL_RE_CE_CHANNEL``); the on-board combined
    RE/CE is position 16 (``ON_BOARD_RE_CE_CHANNEL``); a cell with a
    separate RE/CE for each working electrode pairs each row with its own
    position. The internal label the run needs is derived from the table
    (:func:`src.data.models.wiring_mode_for`), never chosen.

    Each row (CH1-CH16) has an ``Enable`` checkbox and an ``RE/CE`` combo
    (CH1-CH16). By default CH1 is enabled and every row points at
    position 15, the wiring the application has always started with.

    Signals:
        pairs_changed(list, list): Emitted with two parallel lists
            (enabled WE channels, matching RE/CE channels) whenever any
            enable or RE/CE selection changes. The lists are sorted by WE
            channel number; lengths always match.
    """

    pairs_changed = pyqtSignal(list, list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Working Electrodes and RE/CE", parent)
        self._enable_boxes: list[QCheckBox] = []
        self._re_ce_combos: list[QComboBox] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Build the 16-row table and the bulk-set buttons."""
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setSpacing(4)
        grid.addWidget(QLabel("<b>WE</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Enable</b>"), 0, 1)
        grid.addWidget(QLabel("<b>RE/CE</b>"), 0, 2)

        for ch in range(1, _WIRING_ROWS + 1):
            grid.addWidget(QLabel(f"CH{ch}"), ch, 0)

            cb = QCheckBox()
            cb.setChecked(ch == 1)
            cb.toggled.connect(self._on_state_changed)
            grid.addWidget(cb, ch, 1)
            self._enable_boxes.append(cb)

            combo = QComboBox()
            for target in range(1, _WIRING_ROWS + 1):
                combo.addItem(f"CH{target}", target)
            combo.setCurrentIndex(combo.findData(EXTERNAL_RE_CE_CHANNEL))
            combo.currentIndexChanged.connect(
                lambda _: self._on_state_changed()
            )
            grid.addWidget(combo, ch, 2)
            self._re_ce_combos.append(combo)

        layout.addLayout(grid)

        select_row = QHBoxLayout()
        self._select_all_btn = QPushButton("Select All")
        self._select_all_btn.clicked.connect(self.select_all)
        select_row.addWidget(self._select_all_btn)
        self._select_none_btn = QPushButton("Select None")
        self._select_none_btn.clicked.connect(self.select_none)
        select_row.addWidget(self._select_none_btn)
        layout.addLayout(select_row)

        btn_row = QHBoxLayout()
        self._apply_same_btn = QPushButton("RE/CE = own position")
        self._apply_same_btn.setToolTip(
            "For every enabled WE in CH1-CH14, use the RE/CE at its own "
            "position"
        )
        self._apply_same_btn.clicked.connect(self._on_apply_same_position)
        btn_row.addWidget(self._apply_same_btn)

        btn_row.addWidget(QLabel("All to:"))
        self._apply_all_combo = QComboBox()
        for target in range(1, _WIRING_ROWS + 1):
            self._apply_all_combo.addItem(f"CH{target}", target)
        self._apply_all_combo.setCurrentIndex(
            self._apply_all_combo.findData(EXTERNAL_RE_CE_CHANNEL)
        )
        self._apply_all_combo.setToolTip(
            "RE/CE position for every enabled WE. Position "
            f"{EXTERNAL_RE_CE_CHANNEL} is the external reference and "
            f"counter; position {ON_BOARD_RE_CE_CHANNEL} is the on-board "
            "combined (shorted) RE/CE."
        )
        btn_row.addWidget(self._apply_all_combo)
        self._apply_all_btn = QPushButton("Apply")
        self._apply_all_btn.clicked.connect(
            lambda: self._apply_uniform_re_ce(
                int(self._apply_all_combo.currentData())
            )
        )
        btn_row.addWidget(self._apply_all_btn)
        layout.addLayout(btn_row)

    # ---- Public API ------------------------------------------------------

    def selected_pairs(self) -> tuple[list[int], list[int]]:
        """Return the (WE, RE/CE) lists for the enabled rows.

        Returns:
            ``(we_channels, re_ce_channels)``: two parallel lists sorted
            by WE channel; both empty if no row is enabled.
        """
        we: list[int] = []
        re_ce: list[int] = []
        for idx, (cb, combo) in enumerate(
            zip(self._enable_boxes, self._re_ce_combos)
        ):
            if cb.isChecked():
                we.append(idx + 1)
                data = combo.currentData()
                re_ce.append(
                    int(data) if data is not None else EXTERNAL_RE_CE_CHANNEL
                )
        return we, re_ce

    def selected_channels(self) -> list[int]:
        """The enabled WE channels, sorted."""
        return self.selected_pairs()[0]

    def set_pairs(
        self, we_channels: list[int], re_ce_channels: list[int]
    ) -> None:
        """Set the enabled rows and their RE/CE.

        Rows not in *we_channels* are disabled. When every given working
        electrode shares RE/CE position 15 or 16 (a shared external or
        on-board pair), the disabled rows are set to that position too, so
        a later Select All extends the same shared wiring instead of mixing
        it with RE/CE positions left from an earlier per-electrode table.
        Otherwise the disabled rows keep their RE/CE.

        Args:
            we_channels: 1-indexed WE channels to enable (1-16).
            re_ce_channels: Parallel RE/CE list. If the lengths differ,
                the shorter wins and the other rows keep their RE/CE.
        """
        we_set = set(we_channels)
        widgets = [*self._enable_boxes, *self._re_ce_combos]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            for idx, cb in enumerate(self._enable_boxes):
                cb.setChecked((idx + 1) in we_set)
            shared = set(re_ce_channels)
            if len(we_channels) == len(re_ce_channels) and shared in (
                {EXTERNAL_RE_CE_CHANNEL},
                {ON_BOARD_RE_CE_CHANNEL},
            ):
                target = int(next(iter(shared)))
                for combo in self._re_ce_combos:
                    target_idx = combo.findData(target)
                    if target_idx >= 0:
                        combo.setCurrentIndex(target_idx)
            for we, re_ce in zip(we_channels, re_ce_channels):
                if 1 <= we <= _WIRING_ROWS:
                    combo = self._re_ce_combos[we - 1]
                    target_idx = combo.findData(int(re_ce))
                    if target_idx >= 0:
                        combo.setCurrentIndex(target_idx)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._emit_pairs_changed()

    def select_all(self) -> None:
        """Enable every row the current wiring can extend to.

        The rule follows the rows already enabled, so the result is a table
        Start accepts whenever one exists:

        * None enabled: enable all 16 rows, each keeping its RE/CE.
        * All enabled rows share RE/CE position 15 or 16: enable all 16
          rows at that position.
        * Any other table (per-electrode RE/CE): enable CH1-CH14 only,
          because CH15 and CH16 are the shared RE/CE positions and cannot
          be working electrodes then. When every enabled row is paired
          with its own position, the newly enabled rows are paired with
          their own position too; otherwise they keep their RE/CE, and
          Start says which position is not allowed if one is left.
        """
        we, re_ce = self.selected_pairs()
        shared = set(re_ce)
        if not we:
            self._set_all_enabled(True)
            return
        if shared in ({EXTERNAL_RE_CE_CHANNEL}, {ON_BOARD_RE_CE_CHANNEL}):
            fill: Optional[int] = int(next(iter(shared)))
            rows = range(1, _WIRING_ROWS + 1)
            own = False
        else:
            fill = None
            rows = range(1, MODE_C_MAX_CHANNEL + 1)
            own = all(w == r for w, r in zip(we, re_ce))
        enabled = set(we)
        for row in rows:
            cb = self._enable_boxes[row - 1]
            combo = self._re_ce_combos[row - 1]
            cb.blockSignals(True)
            combo.blockSignals(True)
            if row not in enabled:
                target = fill if fill is not None else (row if own else None)
                if target is not None:
                    combo.setCurrentIndex(combo.findData(target))
            cb.setChecked(True)
            cb.blockSignals(False)
            combo.blockSignals(False)
        self._emit_pairs_changed()

    def select_none(self) -> None:
        """Disable every row."""
        self._set_all_enabled(False)

    # ---- Internal --------------------------------------------------------

    def _set_all_enabled(self, enabled: bool) -> None:
        for cb in self._enable_boxes:
            cb.blockSignals(True)
            cb.setChecked(enabled)
            cb.blockSignals(False)
        self._emit_pairs_changed()

    def _on_apply_same_position(self) -> None:
        """For every enabled row in CH1-CH14, set RE/CE = WE channel."""
        for idx, (cb, combo) in enumerate(
            zip(self._enable_boxes, self._re_ce_combos)
        ):
            if cb.isChecked() and idx + 1 <= MODE_C_MAX_CHANNEL:
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(idx + 1))
                combo.blockSignals(False)
        self._emit_pairs_changed()

    def _apply_uniform_re_ce(self, re_ce: int) -> None:
        """For every enabled row, set RE/CE to one position (1-16)."""
        if not (1 <= re_ce <= _WIRING_ROWS):
            return
        for cb, combo in zip(self._enable_boxes, self._re_ce_combos):
            if cb.isChecked():
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(re_ce))
                combo.blockSignals(False)
        self._emit_pairs_changed()

    def _on_state_changed(self, *args: Any) -> None:
        """Handle any enable-checkbox or RE/CE combo change."""
        self._emit_pairs_changed()

    def _emit_pairs_changed(self) -> None:
        """Emit ``pairs_changed`` with the current selection."""
        we, re_ce = self.selected_pairs()
        self.pairs_changed.emit(we, re_ce)

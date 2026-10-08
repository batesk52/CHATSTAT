"""Reusable per-technique parameter editor (sequencer feature).

This module is the single authority for parameter-editing widgets: the
label/unit table, the current-range and bandwidth option lists, the
spin-step heuristic, and the widget factory/reader. Both the single-run
technique panel (``controls.py``) and each sequence step's embedded
editor build their forms through these helpers, so the two editors
cannot drift (a new combo-backed parameter or range tweak lands in both
at once). ``controls.py`` imports from here — never the reverse.

``ParameterForm`` is the ready-made editable grid used by the sequence
step blocks: seed it with :meth:`set_config`, read back via
:meth:`get_params`.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QSpinBox,
    QWidget,
)

from src.techniques.scripts import (
    CURRENT_RANGES,
    EIS_CURRENT_RANGES,
    EIS_RANGE_MODES,
    technique_params,
)

# Parameter display names and units for field labels.
PARAM_LABELS: dict[str, tuple[str, str]] = {
    "e_begin": ("E begin", "V"),
    "e_end": ("E end", "V"),
    "e_step": ("E step", "V"),
    "e_vertex1": ("E vertex 1", "V"),
    "e_vertex2": ("E vertex 2", "V"),
    "e_pulse": ("E pulse", "V"),
    "e_dc": ("E DC", "V"),
    "e_ac": ("E AC", "V"),
    "e_cond": ("E conditioning", "V"),
    "e_dep": ("E deposition", "V"),
    "e_eq": ("E equilibration", "V"),
    "scan_rate": ("Scan rate", "V/s"),
    "t_pulse": ("t pulse", "s"),
    "t_run": ("t run", "s"),
    "t_interval": ("t interval", "s"),
    "t_base": ("t base", "s"),
    "t_cond": ("t conditioning", "s"),
    "t_dep": ("t deposition", "s"),
    "t_eq": ("t equilibration", "s"),
    "amplitude": ("Amplitude", "V"),
    "frequency": ("Frequency", "Hz"),
    "freq_start": ("Freq start", "Hz"),
    "freq_end": ("Freq end", "Hz"),
    "i_dc": ("I DC", "A"),
    "i_ac": ("I AC", "A"),
    "n_scans": ("# Scans", ""),
    "n_freq": ("# Frequencies", ""),
    "settle_time": ("Settle time", "s"),
    "samples_per_visit": ("Samples per channel visit", ""),
    "cr": ("Current range", ""),
    "bw_hz": ("Max Bandwidth", "Hz"),
    "eis_range_mode": ("Range selection", ""),
}

# Current range options for the combo box. BOTH ladders now live in
# src.techniques.scripts (the owner of mode/range knowledge) and are
# re-exported here under their historical names, so the GUI dropdown, the
# agent tool schema and the agent auto-range summaries share one definition
# and the agent layer never has to import a GUI module to learn the ladder.
#
# EIS runs the potentiostat in HIGH-SPEED mode 3, which exposes a DIFFERENT
# current-range ladder. Offering the mode-2 list for EIS is a footgun: ranges
# like 2u/63u are invalid in mode 3 and the device returns no data, while valid
# ranges (50u/200u) are absent. The mode-3 ladder lives in src.techniques.scripts
# (the owner of mode/range knowledge) and is imported here so the GUI dropdown,
# the agent tool schema, and the agent auto-range summary share one definition.
# GEIS shared this branch until 2026-07-28; it was retired from the GUI and
# agent surfaces, and the only caller below never sees a mode-3 technique
# other than EIS.


def current_ranges_for(technique: str | None) -> list[str]:
    """Current-range options valid for ``technique``'s pgstat mode.

    EIS uses the high-speed (mode-3) ladder; everything else uses the
    low-speed (mode-2) ladder.
    """
    if (technique or "").lower() == "eis":
        return EIS_CURRENT_RANGES
    return CURRENT_RANGES

# Max bandwidth options for combo box (Hz).
# Mode-2 sweep range; default 400 preserves legacy behavior.
BANDWIDTH_HZ = [0.4, 4, 40, 400, 4000, 40000, 200000]


def guess_step(value: float) -> float:
    """Guess a reasonable spin box step size from a default value.

    Args:
        value: The default parameter value.

    Returns:
        A step size (order-of-magnitude smaller than the value).
    """
    if value == 0:
        return 0.001
    abs_val = abs(value)
    if abs_val >= 100:
        return 1.0
    if abs_val >= 1:
        return 0.1
    if abs_val >= 0.01:
        return 0.001
    if abs_val >= 0.0001:
        return 0.00001
    return abs_val / 10.0


def create_param_widget(
    name: str, default: Any, on_change, technique: str | None = None
) -> QWidget:
    """Create the appropriate input widget for a single parameter.

    Mirrors ``TechniquePanel._create_param_widget`` but takes an explicit
    ``on_change`` callback so it is reusable outside that class.

    Args:
        name: Parameter name (drives special-cased widgets like ``cr``).
        default: Seed value.
        on_change: Zero-arg callable fired whenever the value changes.
        technique: Technique key, used to pick the valid current-range
            ladder for the ``cr`` combo (EIS uses mode-3 ranges).

    Returns:
        A ``QComboBox`` (``cr`` / ``bw_hz``), ``QSpinBox`` (int) or
        ``QDoubleSpinBox`` (float).
    """
    if name == "cr":
        combo = QComboBox()
        for cr in current_ranges_for(technique):
            combo.addItem(cr)
        idx = combo.findText(str(default))
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda: on_change())
        return combo

    if name == "eis_range_mode":
        combo = QComboBox()
        for mode in EIS_RANGE_MODES:
            combo.addItem(mode)
        idx = combo.findText(str(default))
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda: on_change())
        return combo

    if name == "bw_hz":
        combo = QComboBox()
        for hz in BANDWIDTH_HZ:
            label = str(int(hz)) if float(hz).is_integer() else str(hz)
            combo.addItem(label, hz)
        try:
            default_hz = float(default)
        except (TypeError, ValueError):
            default_hz = 400.0
        idx = combo.findText(
            str(int(default_hz))
            if default_hz.is_integer()
            else str(default_hz)
        )
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda: on_change())
        return combo

    if isinstance(default, int):
        spin = QSpinBox()
        spin.setRange(0, 10000)
        spin.setValue(default)
        spin.valueChanged.connect(lambda: on_change())
        return spin

    spin = QDoubleSpinBox()
    spin.setDecimals(6)
    spin.setRange(-10.0, 1e8)
    spin.setSingleStep(guess_step(default))
    spin.setValue(float(default))
    spin.valueChanged.connect(lambda: on_change())
    return spin


def read_param_widget(name: str, widget: QWidget) -> Any:
    """Read one parameter widget's current value (inverse of create)."""
    if isinstance(widget, QDoubleSpinBox):
        return widget.value()
    if isinstance(widget, QSpinBox):
        return widget.value()
    if isinstance(widget, QComboBox):
        if name == "bw_hz":
            data = widget.currentData()
            if data is None:
                try:
                    data = float(widget.currentText())
                except (TypeError, ValueError):
                    data = 400
            return data
        return widget.currentText()
    return None


class ParameterForm(QWidget):
    """An editable grid of a technique's parameters.

    Build it, call :meth:`set_config` with a technique + seed values, edit
    in the GUI, and read the result back with :meth:`get_params`.

    Signals:
        params_changed(): Emitted whenever any field changes.
    """

    params_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._technique: str = ""
        self._widgets: dict[str, QWidget] = {}
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)

    def set_config(self, technique: str, params: dict[str, Any]) -> None:
        """Rebuild the form for ``technique`` and seed it with ``params``.

        Every parameter the technique defines gets a row (so the user sees
        the full set), with the seed value applied where supplied.

        Args:
            technique: Technique identifier.
            params: Values to pre-fill (a subset is fine).
        """
        self._technique = technique.lower()
        self._widgets.clear()
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        try:
            defaults = technique_params(self._technique)
        except ValueError:
            return
        # Seed defaults with the supplied values (supplied wins).
        merged = {**defaults, **{k: params[k] for k in params if k in defaults}}

        row = 0
        for name, value in merged.items():
            label_text, unit = PARAM_LABELS.get(name, (name, ""))
            display = f"{label_text} ({unit})" if unit else label_text
            self._grid.addWidget(QLabel(display), row, 0)
            widget = create_param_widget(
                name, value, self.params_changed.emit, self._technique
            )
            self._grid.addWidget(widget, row, 1)
            self._widgets[name] = widget
            row += 1

    def get_params(self) -> dict[str, Any]:
        """Return the current values as a parameter dict."""
        return {
            name: read_param_widget(name, widget)
            for name, widget in self._widgets.items()
        }

    @property
    def technique(self) -> str:
        """The technique this form is currently configured for."""
        return self._technique

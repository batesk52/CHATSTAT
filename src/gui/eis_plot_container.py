"""Container widget for EIS Nyquist/Bode plot switching.

Provides a stacked widget that holds both a Nyquist (LivePlotWidget)
and a Bode (BodePlotWidget) plot, with a combo-box selector that is
only visible for EIS/GEIS techniques.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.data.models import DataPoint
from src.gui.bode_widget import BodePlotWidget
from src.gui.plot_widget import EIS_TECHNIQUES, LivePlotWidget

logger = logging.getLogger(__name__)


class EISPlotContainer(QWidget):
    """Switchable container for Nyquist and Bode EIS views.

    For non-EIS techniques only the Nyquist (LivePlotWidget) is
    shown and the selector row is hidden. For EIS/GEIS the user
    can switch between Nyquist and Bode views via a combo box.

    Attributes:
        nyquist: The Nyquist live-plot widget.
        bode: The Bode dual-subplot widget.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._technique: str = ""

        # -- Selector row --
        self._selector_row = QWidget()
        row_layout = QHBoxLayout(self._selector_row)
        row_layout.setContentsMargins(4, 2, 4, 2)
        row_layout.addWidget(QLabel("EIS View:"))

        self._combo = QComboBox()
        self._combo.addItem("Nyquist (Z' vs -Z'')")
        self._combo.addItem("Bode (|Z| & Phase vs Freq)")
        row_layout.addWidget(self._combo)
        row_layout.addStretch()

        # -- Stacked widget --
        self._stack = QStackedWidget()
        self.nyquist = LivePlotWidget(parent=self)
        self.bode = BodePlotWidget(parent=self)
        self._stack.addWidget(self.nyquist)   # index 0
        self._stack.addWidget(self.bode)      # index 1

        # -- Layout --
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._selector_row)
        layout.addWidget(self._stack)

        # Selector hidden by default (non-EIS)
        self._selector_row.setVisible(False)

        # Connect combo to stack
        self._combo.currentIndexChanged.connect(
            self._stack.setCurrentIndex
        )

    # ---- Public API ----------------------------------------------

    def set_technique(self, technique: str) -> None:
        """Configure technique and show/hide the EIS selector.

        Args:
            technique: Lowercase technique identifier.
        """
        self._technique = technique.lower()
        is_eis = self._technique in EIS_TECHNIQUES
        self._selector_row.setVisible(is_eis)
        self.nyquist.set_technique(self._technique)

        if not is_eis:
            self._stack.setCurrentIndex(0)
            self._combo.setCurrentIndex(0)

    @pyqtSlot(object)
    def on_data_point(self, data_point: DataPoint) -> None:
        """Forward data to Nyquist always; also to Bode for EIS.

        Args:
            data_point: The decoded data point from the engine.
        """
        self.nyquist.on_data_point(data_point)
        if self._technique in EIS_TECHNIQUES:
            self.bode.on_data_point(data_point)

    def on_measurement_finished(self) -> None:
        """Forward measurement-finished to both sub-widgets."""
        self.nyquist.on_measurement_finished()
        self.bode.on_measurement_finished()

    def clear_plot(self) -> None:
        """Clear both sub-widgets."""
        self.nyquist.clear_plot()
        self.bode.clear_plot()

    def enable_auto_range(self) -> None:
        """Re-enable auto-range on both sub-widgets."""
        self.nyquist.enable_auto_range()
        self.bode.enable_auto_range()

    def set_axes(self, x_label: str, y_label: str) -> None:
        """Forward manual axis labels to the Nyquist widget only.

        Args:
            x_label: Label for the X (bottom) axis.
            y_label: Label for the Y (left) axis.
        """
        self.nyquist.set_axes(x_label, y_label)

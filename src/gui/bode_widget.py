"""Bode plot widget for EIS impedance data.

Provides a dual-subplot widget showing |Z| vs frequency (log-log)
and Phase vs frequency (log-linear) with per-channel curves matching
the Nyquist color/marker scheme.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer, pyqtSlot

from src.data.models import DataPoint
from src.gui.plot_widget import CHANNEL_COLORS, CHANNEL_SYMBOLS

logger = logging.getLogger(__name__)


class BodePlotWidget(pg.GraphicsLayoutWidget):
    """Dual-subplot Bode plot for EIS/GEIS data.

    Displays two vertically stacked plots:

    * **Top:** |Z| vs Frequency (log-log scale)
    * **Bottom:** Phase vs Frequency (log x, linear y)

    Per-channel curves use the same color and marker palette as
    the Nyquist plot for visual consistency.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent)
        self.setBackground("w")

        # Per-channel data buffers
        self._freq: dict[int, list[float]] = {}
        self._zmag: dict[int, list[float]] = {}
        self._phase: dict[int, list[float]] = {}

        # Per-channel curve items for each subplot
        self._mag_curves: dict[int, pg.PlotDataItem] = {}
        self._phase_curves: dict[int, pg.PlotDataItem] = {}

        # Throttled rendering (see LivePlotWidget): add_point() appends and
        # marks the channel dirty; the redraw happens at most ~30 Hz.
        self._dirty_channels: set[int] = set()
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(33)  # ~30 Hz
        self._render_timer.timeout.connect(self._flush_dirty)
        self._render_timer.start()

        self._setup_plots()

    # ---- Setup ---------------------------------------------------

    def _setup_plots(self) -> None:
        """Create the two stacked subplots."""
        # Top: |Z| vs Frequency (log-log)
        self._mag_plot = self.addPlot(row=0, col=0)
        self._mag_plot.setLogMode(x=True, y=True)
        self._mag_plot.setLabel("bottom", "Frequency (Hz)")
        self._mag_plot.setLabel("left", "|Z| (\u03a9)")
        self._mag_plot.showGrid(x=True, y=True, alpha=0.3)
        self._mag_legend = self._mag_plot.addLegend(
            offset=(10, 10), labelTextSize="9pt"
        )

        # Bottom: Phase vs Frequency (log x, linear y)
        self._phase_plot = self.addPlot(row=1, col=0)
        self._phase_plot.setLogMode(x=True, y=False)
        self._phase_plot.setLabel("bottom", "Frequency (Hz)")
        self._phase_plot.setLabel("left", "Phase (\u00b0)")
        self._phase_plot.showGrid(x=True, y=True, alpha=0.3)
        self._phase_legend = self._phase_plot.addLegend(
            offset=(10, 10), labelTextSize="9pt"
        )

        # Link x axes so zoom/pan is synchronised
        self._phase_plot.setXLink(self._mag_plot)

    # ---- Public API ----------------------------------------------

    def add_point(
        self,
        channel: int,
        freq: float,
        impedance: float,
        phase: float,
    ) -> None:
        """Append a data point to both Bode subplots.

        Args:
            channel: 1-indexed MUX channel number.
            freq: Frequency in Hz.
            impedance: |Z| magnitude in ohms.
            phase: Phase angle in degrees.
        """
        if channel not in self._mag_curves:
            self._init_channel(channel)

        self._freq[channel].append(freq)
        self._zmag[channel].append(impedance)
        self._phase[channel].append(phase)

        # Defer the redraw to the throttled timer (see __init__).
        self._dirty_channels.add(channel)

    def _flush_dirty(self) -> None:
        """Redraw channels that gained points since the last tick."""
        if not self._dirty_channels:
            return
        for channel in self._dirty_channels:
            mag_curve = self._mag_curves.get(channel)
            phase_curve = self._phase_curves.get(channel)
            if mag_curve is None or phase_curve is None:
                continue
            freq_arr = np.array(self._freq[channel])
            mag_curve.setData(freq_arr, np.array(self._zmag[channel]))
            phase_curve.setData(
                freq_arr, np.array(self._phase[channel])
            )
        self._dirty_channels.clear()

    @pyqtSlot(object)
    def on_data_point(self, data_point: DataPoint) -> None:
        """Handle a ``data_point_ready`` signal for EIS data.

        Extracts set_frequency, zreal, and zimag from the data
        point, computes |Z| and phase, then updates both subplots.

        Args:
            data_point: The decoded data point from the engine.
        """
        freq = data_point.variables.get("set_frequency")
        zreal = data_point.variables.get("zreal")
        zimag = data_point.variables.get("zimag")

        if freq is None or zreal is None or zimag is None:
            return

        zmag = math.sqrt(zreal**2 + zimag**2)
        phase_deg = math.degrees(math.atan2(zimag, zreal))

        self.add_point(
            data_point.channel, freq, zmag, phase_deg
        )

    def clear_plot(self) -> None:
        """Clear all curves and data from both subplots."""
        for curve in self._mag_curves.values():
            self._mag_plot.removeItem(curve)
        for curve in self._phase_curves.values():
            self._phase_plot.removeItem(curve)

        self._mag_curves.clear()
        self._phase_curves.clear()
        self._freq.clear()
        self._zmag.clear()
        self._phase.clear()
        self._dirty_channels.clear()

        if self._mag_legend is not None:
            self._mag_legend.clear()
        if self._phase_legend is not None:
            self._phase_legend.clear()

        logger.debug("Bode plot cleared.")

    def on_measurement_finished(self) -> None:
        """Handle measurement completion by restoring auto-range."""
        # Render any points still pending from the throttle timer.
        self._flush_dirty()
        self.enable_auto_range()
        logger.debug(
            "Bode measurement finished — auto-range restored."
        )

    def enable_auto_range(self) -> None:
        """Re-enable auto-range on both subplots."""
        self._mag_plot.enableAutoRange()
        self._phase_plot.enableAutoRange()

    # ---- Internal ------------------------------------------------

    def _init_channel(self, channel: int) -> None:
        """Create curves and buffers for a new channel.

        Args:
            channel: 1-indexed channel number.
        """
        color_idx = (channel - 1) % len(CHANNEL_COLORS)
        color = CHANNEL_COLORS[color_idx]
        sym_idx = (channel - 1) % len(CHANNEL_SYMBOLS)
        symbol = CHANNEL_SYMBOLS[sym_idx]
        pen = pg.mkPen(color=color, width=2)
        brush = pg.mkBrush(color)
        sym_pen = pg.mkPen(color)

        name = f"CH{channel}"

        self._mag_curves[channel] = self._mag_plot.plot(
            [], [],
            pen=pen,
            name=name,
            symbol=symbol,
            symbolSize=8,
            symbolBrush=brush,
            symbolPen=sym_pen,
        )
        self._phase_curves[channel] = self._phase_plot.plot(
            [], [],
            pen=pen,
            name=name,
            symbol=symbol,
            symbolSize=8,
            symbolBrush=brush,
            symbolPen=sym_pen,
        )

        self._freq[channel] = []
        self._zmag[channel] = []
        self._phase[channel] = []

        logger.debug(
            "Bode: initialised curves for CH%d (color: %s)",
            channel,
            color,
        )

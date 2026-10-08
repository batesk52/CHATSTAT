"""Live pyqtgraph plot widget with per-channel curves.

Provides a ``PlotWidget`` subclass that renders real-time
electrochemical measurement data with technique-aware axis labels
and a 16-color palette for distinguishing MUX channels.

Typical usage from the main window::

    plot = LivePlotWidget()
    plot.set_technique("cv")
    engine.data_point_ready.connect(plot.on_data_point)
    engine.measurement_finished.connect(plot.on_measurement_finished)
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer, pyqtSlot

from src.data.models import DataPoint

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# 16-color palette (visually distinct, suitable for dark background)
# -----------------------------------------------------------------------

CHANNEL_COLORS: list[str] = [
    "#1f77b4",  # CH1  - muted blue
    "#ff7f0e",  # CH2  - orange
    "#2ca02c",  # CH3  - green
    "#d62728",  # CH4  - red
    "#9467bd",  # CH5  - purple
    "#8c564b",  # CH6  - brown
    "#e377c2",  # CH7  - pink
    "#7f7f7f",  # CH8  - grey
    "#bcbd22",  # CH9  - olive
    "#17becf",  # CH10 - cyan
    "#aec7e8",  # CH11 - light blue
    "#ffbb78",  # CH12 - light orange
    "#98df8a",  # CH13 - light green
    "#ff9896",  # CH14 - light red
    "#c5b0d5",  # CH15 - light purple
    "#c49c94",  # CH16 - light brown
]

# -----------------------------------------------------------------------
# 16-symbol palette for EIS per-channel markers
# -----------------------------------------------------------------------

CHANNEL_SYMBOLS: list[str] = [
    "o", "s", "d", "t", "+", "x", "star", "p",
    "h", "t1", "t2", "t3", "o", "s", "d", "t",
]

# -----------------------------------------------------------------------
# EIS technique identifiers (single source of truth)
# -----------------------------------------------------------------------

EIS_TECHNIQUES: frozenset[str] = frozenset({"eis", "geis"})

# -----------------------------------------------------------------------
# Technique axis presets
# -----------------------------------------------------------------------

# Maps technique name to (x_label, y_label, x_unit, y_unit, x_var, y_var).
# x_var and y_var are the variable names in DataPoint.variables to plot.
_TECHNIQUE_AXES: dict[str, tuple[str, str, str, str, str, str]] = {
    # Voltammetric: I vs E
    "cv": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "lsv": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "dpv": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "swv": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "npv": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "acv": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "fcv": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "lsp": ("Potential", "Current", "V", "A", "set_potential", "current"),
    "pad": ("Potential", "Current", "V", "A", "set_potential", "current"),
    # Amperometric: I vs t
    "ca": ("Time", "Current", "s", "A", "time", "current"),
    "fca": ("Time", "Current", "s", "A", "time", "current"),
    "ca_alt_mux": (
        "Time", "Current", "s", "A", "time", "current",
    ),
    # Potentiometric: E vs t
    "cp": (
        "Time", "Potential", "s", "V", "time", "potential",
    ),
    "ocp": (
        "Time", "Potential", "s", "V", "time", "potential",
    ),
    # Impedance: -Z'' vs Z' (Nyquist)
    "eis": (
        "Z' (real)", "-Z'' (imag)", "\u03a9", "\u03a9",
        "zreal", "zimag",
    ),
    "geis": (
        "Z' (real)", "-Z'' (imag)", "\u03a9", "\u03a9",
        "zreal", "zimag",
    ),
}

# Default axis config for unknown techniques
_DEFAULT_AXES = ("X", "Y", "", "", "", "")


class LivePlotWidget(pg.PlotWidget):
    """Real-time plot widget with per-channel curves.

    Subclasses ``pyqtgraph.PlotWidget`` to provide:

    * A 16-color palette for per-channel curve identification
    * Technique-aware axis labels (I vs E, I vs t, Nyquist, etc.)
    * ``add_point()`` for streaming real-time data from the engine
    * ``on_data_point()`` slot for direct signal connection
    * Auto-range that defers to manual zoom/pan when the user
      interacts with the plot

    Attributes:
        technique: Current technique identifier (lowercase).
        x_var: Name of the DataPoint variable mapped to the X axis.
        y_var: Name of the DataPoint variable mapped to the Y axis.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent, background="w")

        self.technique: str = ""
        self.x_var: str = ""
        self.y_var: str = ""

        # Per-channel data buffers: channel (1-indexed) -> (xs, ys)
        self._x_data: dict[int, list[float]] = {}
        self._y_data: dict[int, list[float]] = {}

        # Per-channel curve items
        self._curves: dict[int, pg.PlotDataItem] = {}

        # Auto-range management
        self._auto_range_enabled: bool = True
        self._user_interacted: bool = False

        # Throttled rendering: add_point() only appends + marks the channel
        # dirty; the actual setData redraw happens at most ~30 Hz via this
        # timer. Without it, a fast stream rebuilds the full curve array on
        # every point (O(N) per point → O(N^2) over the run) and can flood
        # the event loop. A trailing flush keeps the final point visible.
        self._dirty_channels: set[int] = set()
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(33)  # ~30 Hz
        self._render_timer.timeout.connect(self._flush_dirty)
        self._render_timer.start()

        # Configure plot appearance
        self._setup_plot()

        # Detect user zoom/pan to disable auto-range
        self.getPlotItem().getViewBox().sigRangeChangedManually.connect(
            self._on_manual_range_change
        )

    # ---- Setup -----------------------------------------------------------

    def _setup_plot(self) -> None:
        """Configure initial plot appearance."""
        plot_item = self.getPlotItem()
        plot_item.showGrid(x=True, y=True, alpha=0.3)
        plot_item.setLabel("bottom", "X")
        plot_item.setLabel("left", "Y")

        # Add a legend for channel identification
        self._legend = plot_item.addLegend(
            offset=(10, 10), labelTextSize="9pt"
        )

    # ---- Properties --------------------------------------------------------

    @property
    def _is_eis(self) -> bool:
        """Return True if the current technique is EIS or GEIS."""
        return self.technique in EIS_TECHNIQUES

    # ---- Public API ------------------------------------------------------

    def set_technique(self, technique: str) -> None:
        """Configure axis labels and variable mappings for a technique.

        Updates the X/Y axis labels and records which ``DataPoint``
        variable names to extract for plotting.

        Args:
            technique: Lowercase technique identifier (e.g., 'cv',
                'eis', 'ca').
        """
        self.technique = technique.lower()
        axes = _TECHNIQUE_AXES.get(self.technique, _DEFAULT_AXES)
        x_label, y_label, x_unit, y_unit, self.x_var, self.y_var = axes

        plot_item = self.getPlotItem()
        plot_item.setLabel(
            "bottom", x_label, units=x_unit if x_unit else None
        )
        plot_item.setLabel(
            "left", y_label, units=y_unit if y_unit else None
        )
        logger.debug(
            "Plot configured for %s: %s vs %s",
            technique,
            x_label,
            y_label,
        )

    def set_axes(self, x_label: str, y_label: str) -> None:
        """Manually set axis labels.

        Args:
            x_label: Label for the X (bottom) axis.
            y_label: Label for the Y (left) axis.
        """
        plot_item = self.getPlotItem()
        plot_item.setLabel("bottom", x_label)
        plot_item.setLabel("left", y_label)

    def add_point(self, channel: int, x: float, y: float) -> None:
        """Append a data point to the specified channel's curve.

        Creates the curve on first use for the channel. Updates the
        plot in place for efficient real-time rendering.

        Args:
            channel: 1-indexed MUX channel number (1-16).
            x: X-axis value.
            y: Y-axis value.
        """
        # Initialise channel data and curve if needed
        if channel not in self._curves:
            self._init_channel(channel)

        self._x_data[channel].append(x)
        self._y_data[channel].append(y)

        # Defer the actual redraw to the throttled timer (see __init__).
        self._dirty_channels.add(channel)

    def _flush_dirty(self) -> None:
        """Redraw any channels that gained points since the last tick.

        Called by the render timer (~30 Hz) and directly on completion so
        the final point is never left unrendered.
        """
        if not self._dirty_channels:
            return
        for channel in self._dirty_channels:
            curve = self._curves.get(channel)
            if curve is None:
                continue
            curve.setData(
                np.array(self._x_data[channel]),
                np.array(self._y_data[channel]),
            )
        self._dirty_channels.clear()

        # Auto-range if the user hasn't manually zoomed/panned
        if self._auto_range_enabled and not self._user_interacted:
            self.getPlotItem().enableAutoRange()

    def clear_plot(self) -> None:
        """Clear all curves and data, resetting for a new measurement.

        Removes all channel curves from the plot, clears data buffers,
        and resets auto-range behaviour.
        """
        for curve in self._curves.values():
            self.removeItem(curve)
        self._curves.clear()
        self._x_data.clear()
        self._y_data.clear()
        self._dirty_channels.clear()
        self._user_interacted = False
        self._auto_range_enabled = True

        # Clear and re-add legend
        if self._legend is not None:
            self._legend.clear()

        logger.debug("Plot cleared.")

    def reset(self) -> None:
        """Full reset: clear data and reset technique/axes.

        Clears all data and resets axis labels to defaults.
        """
        self.clear_plot()
        self.technique = ""
        self.x_var = ""
        self.y_var = ""
        plot_item = self.getPlotItem()
        plot_item.setLabel("bottom", "X")
        plot_item.setLabel("left", "Y")

    def enable_auto_range(self) -> None:
        """Re-enable auto-range after manual zoom/pan."""
        self._user_interacted = False
        self._auto_range_enabled = True
        self.getPlotItem().enableAutoRange()

    # ---- Slots for engine signals ----------------------------------------

    @pyqtSlot(object)
    def on_data_point(self, data_point: DataPoint) -> None:
        """Handle a ``data_point_ready`` signal from the engine.

        Extracts the X and Y values from the ``DataPoint`` based on
        the current technique's variable mapping and calls
        ``add_point()``.

        Args:
            data_point: The decoded data point from the engine.
        """
        channel = data_point.channel

        # Extract x and y values from the data point
        x = self._extract_x(data_point)
        y = self._extract_y(data_point)

        if x is not None and y is not None:
            self.add_point(channel, x, y)

    @pyqtSlot()
    def on_measurement_finished(self) -> None:
        """Handle measurement completion.

        Re-enables auto-range so the full dataset is visible.
        """
        # Render any points still pending from the throttle timer so the
        # finished plot shows every collected point.
        self._flush_dirty()
        self.enable_auto_range()
        logger.debug("Measurement finished — auto-range restored.")

    # ---- Internal --------------------------------------------------------

    def _init_channel(self, channel: int) -> None:
        """Create a curve and data buffers for a new channel.

        Args:
            channel: 1-indexed channel number.
        """
        color_idx = (channel - 1) % len(CHANNEL_COLORS)
        color = CHANNEL_COLORS[color_idx]
        pen = pg.mkPen(color=color, width=2)

        plot_kwargs: dict = {
            "pen": pen,
            "name": f"CH{channel}",
        }
        if self._is_eis:
            sym_idx = (channel - 1) % len(CHANNEL_SYMBOLS)
            plot_kwargs["symbol"] = CHANNEL_SYMBOLS[sym_idx]
            plot_kwargs["symbolSize"] = 8
            plot_kwargs["symbolBrush"] = pg.mkBrush(color)
            plot_kwargs["symbolPen"] = pg.mkPen(color)

        curve = self.plot([], [], **plot_kwargs)
        # NOTE: deliberately no setClipToView/setDownsampling here.
        # clip-to-view requires monotonically-increasing X, which holds for
        # time-series techniques but NOT for CV/FCV (X = swept potential,
        # forward+reverse) — enabling it there renders a wedge. The ~30 Hz
        # render throttle (see __init__) is the actual O(N^2) fix; render
        # cost per frame is acceptable for the point counts seen here.
        self._curves[channel] = curve
        self._x_data[channel] = []
        self._y_data[channel] = []
        logger.debug(
            "Initialised curve for CH%d (color: %s)",
            channel,
            color,
        )

    def _extract_x(self, dp: DataPoint) -> Optional[float]:
        """Extract the X-axis value from a data point.

        For time-based techniques, uses the data point timestamp.
        For potential-based, uses set_potential. For EIS, uses
        the impedance real component.

        Args:
            dp: The data point to extract from.

        Returns:
            The X value, or ``None`` if unavailable.
        """
        if self.x_var == "time":
            # Prefer the variable dict value, fall back to timestamp
            val = dp.variables.get("time")
            if val is not None:
                return val
            return dp.timestamp
        if self.x_var:
            return dp.variables.get(self.x_var)
        # Fallback: use timestamp
        return dp.timestamp

    def _extract_y(self, dp: DataPoint) -> Optional[float]:
        """Extract the Y-axis value from a data point.

        For EIS/GEIS Nyquist plots, the imaginary impedance is
        negated to follow the standard -Z'' convention.

        Args:
            dp: The data point to extract from.

        Returns:
            The Y value, or ``None`` if unavailable.
        """
        if self.y_var:
            val = dp.variables.get(self.y_var)
            if val is None:
                return None
            # Negate Z'' for standard Nyquist convention (-Z'' vs Z')
            if self.y_var == "zimag":
                return -val
            return val
        return None

    def _on_manual_range_change(self) -> None:
        """Disable auto-range when the user manually zooms or pans."""
        self._user_interacted = True

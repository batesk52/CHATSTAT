"""A small animated sliding on/off switch widget.

PyQt6 has no native toggle-switch widget, so :class:`ToggleSwitch`
paints a rounded track with a sliding thumb and animates between
states. It behaves as a standard checkable button (emits ``toggled``).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import (
    QEvent,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QAbstractButton, QWidget


class ToggleSwitch(QAbstractButton):
    """A small animated sliding on/off switch (checkable).

    PyQt6 has no native switch widget, so this paints a rounded track
    with a sliding thumb and animates between states. Emits the standard
    ``toggled(bool)`` signal like any checkable button.
    """

    _OFF_COLOR = QColor("#5a5a5a")
    _ON_COLOR = QColor("#e08a1e")  # amber — signals "verbose/diagnostic"
    _THUMB_COLOR = QColor("#f5f5f5")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self._width = 52
        self._height = 26
        self._margin = 3
        self.setFixedSize(self._width, self._height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumb = 0.0  # 0.0 = off (left), 1.0 = on (right)
        self._anim = QPropertyAnimation(self, b"thumbPosition", self)
        self._anim.setDuration(120)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(self._width, self._height)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._thumb)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _get_thumb(self) -> float:
        return self._thumb

    def _set_thumb(self, value: float) -> None:
        self._thumb = value
        self.update()

    thumbPosition = pyqtProperty(float, fget=_get_thumb, fset=_set_thumb)

    def paintEvent(self, _event: QEvent) -> None:
        t = self._thumb
        track = QColor(
            int(self._OFF_COLOR.red()
                + (self._ON_COLOR.red() - self._OFF_COLOR.red()) * t),
            int(self._OFF_COLOR.green()
                + (self._ON_COLOR.green() - self._OFF_COLOR.green()) * t),
            int(self._OFF_COLOR.blue()
                + (self._ON_COLOR.blue() - self._OFF_COLOR.blue()) * t),
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        radius = self._height / 2
        painter.setBrush(track)
        painter.drawRoundedRect(
            0, 0, self._width, self._height, radius, radius
        )
        diameter = self._height - 2 * self._margin
        travel = self._width - 2 * self._margin - diameter
        x = self._margin + t * travel
        painter.setBrush(self._THUMB_COLOR)
        painter.drawEllipse(QRectF(x, self._margin, diameter, diameter))

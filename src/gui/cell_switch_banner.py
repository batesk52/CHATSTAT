"""The "waiting for cell switch" banner in the agent dock.

When the agent calls ``request_cell_switch`` the dock shows this banner
above the message box: what to put in the cell, why, and two buttons.
**Continue** says the cell is switched; **Cancel** says it is not. It is not
a modal dialog, so the Stop button and the rest of the window keep working
while the agent waits.

It is the GUI actuator behind :class:`src.agent.cell_switch.CellSwitchPrompt`:
``present`` and ``withdraw`` are called on the GUI thread.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.agent.cell_switch import (
    CellSwitchOutcome,
    CellSwitchRequest,
    default_operator,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = ["CellSwitchBanner"]


class CellSwitchBanner(QFrame):
    """Shows one pending cell-switch request and reports the answer."""

    def __init__(
        self,
        operator_fn: Callable[[], str] = default_operator,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._operator_fn = operator_fn
        self._pending: Optional[
            tuple[CellSwitchRequest, Callable[[CellSwitchOutcome], None]]
        ] = None

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "CellSwitchBanner { background: #3a3214; border: 1px solid "
            "#c9a227; border-radius: 4px; } QLabel { color: #f2e6b8; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        self._title = QLabel("Waiting for cell switch")
        self._title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._title)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        layout.addWidget(self._message)

        row = QHBoxLayout()
        row.addStretch(1)
        self._continue_button = QPushButton("Continue")
        self._continue_button.setToolTip(
            "The cell now holds the new solution: let the agent go on."
        )
        self._continue_button.clicked.connect(lambda: self._answer(True))
        row.addWidget(self._continue_button)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setToolTip(
            "The cell was not switched. The agent is told and asks what to do."
        )
        self._cancel_button.clicked.connect(lambda: self._answer(False))
        row.addWidget(self._cancel_button)
        layout.addLayout(row)

        self.setVisible(False)

    # ---- the CellSwitchPrompt protocol (GUI thread) ----------------------

    def present(
        self,
        request: CellSwitchRequest,
        resolve: Callable[[CellSwitchOutcome], None],
    ) -> None:
        """Show *request*; ``resolve`` is called once with the answer."""
        if self._pending is not None:
            # One at a time; an older request that is still up is declined.
            self._answer(False)
        self._pending = (request, resolve)
        reason = f" ({request.reason})" if request.reason else ""
        self._message.setText(
            f"Switch the cell to {request.new_medium}{reason}, "
            "then press Continue."
        )
        self.setVisible(True)
        try:
            QApplication.alert(self.window())
        except Exception:  # noqa: BLE001 - attention is a nicety
            logger.debug("Could not flash the window", exc_info=True)

    def withdraw(self, request_id: str) -> None:
        """Take the request down without answering it."""
        if self._pending is not None and self._pending[0].request_id == request_id:
            self._pending = None
            self.setVisible(False)

    # ---- queries / internals ---------------------------------------------

    @property
    def pending_request_id(self) -> Optional[str]:
        """Id of the request being shown, or None."""
        return self._pending[0].request_id if self._pending else None

    def _answer(self, confirmed: bool) -> None:
        if self._pending is None:
            return
        _request, resolve = self._pending
        self._pending = None
        self.setVisible(False)
        try:
            operator = self._operator_fn()
        except Exception:  # noqa: BLE001
            operator = "unknown"
        resolve(
            CellSwitchOutcome(
                confirmed=confirmed,
                operator=operator,
                confirmed_at=now_iso(),
                actuator="human",
            )
        )

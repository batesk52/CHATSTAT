"""Background QThread workers for the GUI.

Keeps blocking serial I/O off the GUI event loop. Currently provides
:class:`ConnectWorker` for the connect handshake.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.comms.serial_connection import PicoConnection


class ConnectWorker(QThread):
    """Runs the blocking connect handshake off the GUI thread.

    ``PicoConnection.connect()`` opens the port and then performs blocking
    firmware/serial-number queries (up to several seconds on a bad port).
    Running it on the GUI thread freezes the event loop, so it is executed
    here and the result is delivered back via signals.
    """

    succeeded = pyqtSignal(str)  # firmware version
    failed = pyqtSignal(str)  # error message

    def __init__(
        self,
        connection: PicoConnection,
        port: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._connection = connection
        self._port = port

    def run(self) -> None:
        try:
            self._connection.connect(self._port)
        except Exception as exc:  # noqa: BLE001 - reported to the GUI
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(self._connection.firmware_version or "")

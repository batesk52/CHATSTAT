"""Agent-driven runs on the real engine must never pop modal dialogs.

The EngineAdapter marks each run it starts (``_start_agent_run`` sets the
flag and starts in one GUI-thread closure); the main window's
finished/error handlers consume the mark and route agent runs through the
modal-free path. User-started runs keep their export prompt / error
dialog, and a concurrent mock-mode agent flag can never suppress a user
run's prompt (engine-identity guard).
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots headless (CI / WSL).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.data.models import DataPoint, MeasurementResult  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _result(tech="cv"):
    r = MeasurementResult(technique=tech)
    r.add_point(
        DataPoint(
            timestamp=0.1,
            channel=1,
            variables={"potential": 0.1, "current": 1e-5},
        )
    )
    return r


# ---- adapter-level attribution -------------------------------------------


def test_start_agent_run_marks_and_start_failure_unwinds():
    """_start_agent_run sets the flag; a start failure clears it."""

    class StubEngine:
        def __init__(self, fail=False):
            self.fail = fail

        def start_measurement(self, connection, config):
            if self.fail:
                raise RuntimeError("busy")

    ok = EngineAdapter(StubEngine(), object())
    ok._start_agent_run(object())  # noqa: SLF001
    assert ok.consume_agent_run() is True
    assert ok.consume_agent_run() is False  # consume-once

    bad = EngineAdapter(StubEngine(fail=True), object())
    with pytest.raises(RuntimeError):
        bad._start_agent_run(object())  # noqa: SLF001
    assert bad.consume_agent_run() is False  # unwound


# ---- main-window suppression ----------------------------------------------


def test_agent_run_finish_suppresses_export_prompt(qapp, monkeypatch):
    """An agent-attributed finish never raises the export prompt."""

    def _no_modal(*a, **k):
        raise AssertionError("modal raised for an agent run")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_no_modal))
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(_no_modal)
    )
    window = MainWindow()
    try:
        # Attribute the next real-engine finish to the agent.
        assert window._agent_engine is window._engine  # noqa: SLF001
        window._agent_adapter._agent_run_active = True  # noqa: SLF001

        window._on_measurement_finished(_result())  # noqa: SLF001

        # Modal-free handling still surfaces the result.
        assert window._last_result is not None  # noqa: SLF001
        assert window._export_action.isEnabled()  # noqa: SLF001
        # Consume-once: the flag is spent.
        assert (
            window._agent_adapter.consume_agent_run()  # noqa: SLF001
            is False
        )
    finally:
        window.close()


def test_agent_run_error_suppresses_critical_dialog(qapp, monkeypatch):
    """An agent-attributed error never raises the critical dialog."""

    def _no_modal(*a, **k):
        raise AssertionError("error dialog raised for an agent run")

    monkeypatch.setattr(QMessageBox, "critical", staticmethod(_no_modal))
    window = MainWindow()
    try:
        window._agent_adapter._agent_run_active = True  # noqa: SLF001
        window._on_measurement_error("Device error: !0005")  # noqa: SLF001
        assert (
            window._agent_adapter.consume_agent_run()  # noqa: SLF001
            is False
        )
    finally:
        window.close()


def test_user_run_still_prompts(qapp, monkeypatch):
    """A user-started run (no attribution) keeps its export prompt."""
    asked: list[bool] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(
            lambda *a, **k: (
                asked.append(True),
                QMessageBox.StandardButton.No,
            )[1]
        ),
    )
    window = MainWindow()
    try:
        window._on_measurement_finished(_result())  # noqa: SLF001
        assert asked == [True]
    finally:
        window.close()


def test_mock_mode_flag_cannot_suppress_user_prompt(qapp, monkeypatch):
    """With the agent on a mock engine, a user run still prompts.

    The engine-identity guard means a concurrently running mock-mode
    agent measurement can never consume/suppress the REAL engine's
    finish handling.
    """
    asked: list[bool] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(
            lambda *a, **k: (
                asked.append(True),
                QMessageBox.StandardButton.No,
            )[1]
        ),
    )
    window = MainWindow()
    try:
        # Simulate mock mode: the agent wraps a DIFFERENT engine.
        window._agent_engine = object()  # noqa: SLF001
        window._agent_adapter._agent_run_active = True  # noqa: SLF001

        window._on_measurement_finished(_result())  # noqa: SLF001

        assert asked == [True]  # user prompt unaffected
        # The mock agent's flag was NOT consumed by the real handler.
        assert (
            window._agent_adapter.consume_agent_run()  # noqa: SLF001
            is True
        )
    finally:
        window.close()

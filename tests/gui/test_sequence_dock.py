"""Tests for the Sequence dock + export suppression (sequencer feature -- Phase 4).

Offscreen GUI tests over :class:`src.gui.main_window.MainWindow`:

* a dock titled "Sequence" exists after construction, and
* in sequence mode no modal export dialog is raised and the single-run
  Start control is disabled mid-sequence and re-enabled after.

The real ``MeasurementEngine`` (a QThread) is never started.  The
sequence panel's engine is swapped for a ``MockEngine`` that drives the
``measurement_finished`` lifecycle synchronously, and every ``QMessageBox``
entry point is monkeypatched to fail the test if a modal is raised.
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots headless (CI / WSL).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QDockWidget,
    QMessageBox,
)

from src.data.presets import Preset, PresetManager  # noqa: E402
from src.data.sequence import SequenceStep  # noqa: E402
import src.gui.main_window as main_window_mod  # noqa: E402
import src.gui.sequence_panel as sequence_panel_mod  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class MockEngine(QObject):
    """Minimal stand-in for ``MeasurementEngine`` (no QThread)."""

    measurement_finished = pyqtSignal(object)
    measurement_error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.started_configs: list[object] = []
        self._running = False

    def start_measurement(self, connection, config) -> None:
        if self._running:
            raise RuntimeError("MockEngine is already running.")
        self._running = True
        self.started_configs.append(config)

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self._running

    def abort(self) -> None:
        self._running = False

    def finish_current(self) -> None:
        """Emit measurement_finished for the in-flight step."""
        self._running = False
        self.measurement_finished.emit(object())


def _drain(qapp) -> None:
    """Process pending Qt events so deferred singleShot timers fire."""
    for _ in range(50):
        qapp.processEvents()


def _no_modal(*args, **kwargs):
    """Stand-in for any QMessageBox call -- fails the test if invoked."""
    raise AssertionError("A modal dialog was raised in sequence mode.")


def test_sequence_dock_exists(qapp) -> None:
    """A dock titled 'Sequence' is present after MainWindow construction."""
    window = MainWindow()
    try:
        titles = {
            d.windowTitle() for d in window.findChildren(QDockWidget)
        }
        assert "Sequence" in titles
    finally:
        window.close()


def test_sequence_suppresses_export_and_locks_start(
    qapp, tmp_path, monkeypatch
) -> None:
    """A sequence raises no per-step modal and keeps Start disabled.

    The only modal allowed is the end-of-run save prompt (auto-save is
    off), which we decline with No; any per-step prompt fails loudly.
    """
    # No per-step modal may block the queue.
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_no_modal))
    # The completion save-prompt is expected -- decline it (No).
    decline = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", decline)
    monkeypatch.setattr(main_window_mod.QMessageBox, "question", decline)

    window = MainWindow()
    try:
        # Pretend a device is connected so the start/stop control state
        # transitions exercise the connected branch.
        monkeypatch.setattr(
            type(window._connection),  # noqa: SLF001
            "is_connected",
            property(lambda self: True),
        )

        # Swap in a mock engine + a 2-preset store the steps resolve to.
        engine = MockEngine()
        mgr = PresetManager(path=str(tmp_path / "store.mux16"))
        mgr.add_preset(
            "cv1", Preset(name="cv1", technique="cv", channels=[1])
        )
        mgr.add_preset(
            "ca1", Preset(name="ca1", technique="ca", channels=[1])
        )

        panel = window._sequence_panel  # noqa: SLF001
        panel.set_engine(engine)
        panel.set_preset_manager(mgr)
        panel.set_connection_provider(lambda: object())
        panel.add_step(SequenceStep(preset_name="cv1"))
        panel.add_step(SequenceStep(preset_name="ca1"))

        # Also block the sequence panel's own message boxes.
        monkeypatch.setattr(
            sequence_panel_mod.QMessageBox,
            "critical",
            staticmethod(_no_modal),
        )
        monkeypatch.setattr(
            sequence_panel_mod.QMessageBox,
            "warning",
            staticmethod(_no_modal),
        )

        # Start the sequence.
        panel._on_run()  # noqa: SLF001
        assert window._sequence_active is True  # noqa: SLF001
        # Start control is disabled while the sequence runs.
        assert (
            window._meas_panel._start_btn.isEnabled() is False
        )  # noqa: SLF001

        # Step 1 finishes -> the main window's finished handler must NOT
        # prompt (it would raise via _no_modal); the queue advances.
        window._on_measurement_finished(_FakeResult())  # noqa: SLF001
        engine.finish_current()
        _drain(qapp)
        assert len(engine.started_configs) == 2

        # Step 2 finishes -> sequence completes, controls restore.
        window._on_measurement_finished(_FakeResult())  # noqa: SLF001
        engine.finish_current()
        _drain(qapp)

        assert window._sequence_active is False  # noqa: SLF001
        # Start re-enabled now the sequence is done (device connected).
        assert window._meas_panel._start_btn.isEnabled() is True  # noqa: SLF001
    finally:
        window.close()


def test_measurement_started_switches_plot_technique(qapp) -> None:
    """Each engine ``measurement_started`` retargets the live plot.

    Sequence steps launch straight through the engine (not the single-run
    Start path), so the plot must follow ``measurement_started`` or a mixed
    CV->EIS->CA sequence would draw every step on the first step's axes
    (e.g. CV data on an EIS Nyquist view).
    """
    window = MainWindow()
    try:
        # An EIS step makes the active tab EIS-mode (Nyquist/Bode selector).
        window._on_measurement_started("eis")  # noqa: SLF001
        container = window._plot_container  # noqa: SLF001
        assert container._technique == "eis"  # noqa: SLF001
        assert container._selector_row.isHidden() is False  # noqa: SLF001

        # A following CV run retargets to the time/IV view (stack 0) and
        # hides the EIS-only selector -- i.e. the CV plot is visible again.
        window._on_measurement_started("cv")  # noqa: SLF001
        container = window._plot_container  # noqa: SLF001
        assert container._technique == "cv"  # noqa: SLF001
        assert container._stack.currentIndex() == 0  # noqa: SLF001
        assert container._selector_row.isHidden() is True  # noqa: SLF001
    finally:
        window.close()


def test_sequence_creates_one_plot_tab_per_step(qapp) -> None:
    """A sequence accumulates a labelled live-plot tab for each step."""
    window = MainWindow()
    try:
        window._on_sequence_started()  # noqa: SLF001
        # Sequence start no longer wipes the strip; the lone scratch tab
        # remains and the first step consumes it.
        assert window._plot_tabs.count() == 1  # noqa: SLF001

        window._on_measurement_started("cv")  # noqa: SLF001
        window._on_measurement_started("eis")  # noqa: SLF001
        window._on_measurement_started("ca")  # noqa: SLF001

        assert window._plot_tabs.count() == 3  # noqa: SLF001
        labels = [
            window._plot_tabs.tabText(i)  # noqa: SLF001
            for i in range(3)
        ]
        assert labels == ["1·CV", "2·EIS", "3·CA"]
        # The active container tracks the most recently started step.
        assert window._plot_container._technique == "ca"  # noqa: SLF001
    finally:
        window.close()


def test_single_runs_accumulate_into_new_tabs(qapp) -> None:
    """Each single run opens its own tab; a prior plot is never overwritten."""
    window = MainWindow()
    try:
        # First run consumes the pristine scratch tab.
        window._on_measurement_started("cv")  # noqa: SLF001
        assert window._plot_tabs.count() == 1  # noqa: SLF001
        assert window._plot_tabs.tabText(0) == "CV"  # noqa: SLF001
        cv_tab = window._plot_container  # noqa: SLF001
        window._run_in_progress = False  # noqa: SLF001 (run ended)

        # Second run opens a NEW tab to the right; the CV plot survives.
        window._on_measurement_started("eis")  # noqa: SLF001
        assert window._plot_tabs.count() == 2  # noqa: SLF001
        assert window._plot_tabs.tabText(0) == "CV"  # noqa: SLF001
        assert window._plot_tabs.tabText(1) == "EIS"  # noqa: SLF001
        assert window._plot_container._technique == "eis"  # noqa: SLF001
        # The first run's container is a different, untouched tab.
        assert window._plot_container is not cv_tab  # noqa: SLF001
        assert cv_tab._technique == "cv"  # noqa: SLF001
    finally:
        window.close()


def test_recording_tab_cannot_be_closed_midrun(qapp) -> None:
    """The tab a run is writing to is protected until the run ends."""
    window = MainWindow()
    try:
        window._on_measurement_started("cv")  # noqa: SLF001
        recording = window._plot_container  # noqa: SLF001
        idx = window._plot_tabs.indexOf(recording)  # noqa: SLF001
        # Close request mid-run is refused: the tab stays.
        window._on_plot_tab_close(idx)  # noqa: SLF001
        assert window._plot_tabs.indexOf(recording) != -1  # noqa: SLF001

        # Once the run ends it can be closed; closing the last tab reseeds.
        window._run_in_progress = False  # noqa: SLF001
        window._on_plot_tab_close(window._plot_tabs.indexOf(recording))  # noqa: SLF001
        assert window._plot_tabs.count() == 1  # noqa: SLF001 (reseeded scratch)
        assert window._plot_container is not None  # noqa: SLF001
    finally:
        window.close()


def test_sequence_export_base_respects_auto_save_toggle(qapp) -> None:
    """The export-base provider mirrors the GUI auto-save toggle.

    The base dir is ALWAYS supplied so the runner has a valid root when
    auto-save is on; ``auto_save_all`` carries the GUI toggle and is the
    sole gate (auto-save is fully opt-in for every technique).
    """
    window = MainWindow()
    try:
        # Opt-in default: toggle off -> base present, all-flag False.
        assert window._meas_panel.is_auto_save_enabled() is False  # noqa: SLF001
        base, save_all = window._sequence_export_base()  # noqa: SLF001
        assert base
        assert save_all is False

        # User enables auto-save -> all-flag True.
        window._meas_panel.set_auto_save(True, "")  # noqa: SLF001
        base, save_all = window._sequence_export_base()  # noqa: SLF001
        assert base
        assert save_all is True
    finally:
        window.close()


def test_preset_selection_does_not_enable_auto_save(
    qapp, tmp_path
) -> None:
    """Selecting a preset never auto-enables auto-save (opt-in only).

    Even a preset carrying ``auto_save=True`` must leave the toggle off;
    the user activates auto-save explicitly in the GUI.
    """
    from src.data.presets import Preset, PresetManager

    window = MainWindow()
    try:
        # Swap in a temp store so we never touch the real user store.
        mgr = PresetManager(path=str(tmp_path / "store.mux16"))
        mgr.add_preset(
            "forces_save",
            Preset(
                name="forces_save",
                technique="cv",
                channels=[1],
                auto_save=True,
            ),
        )
        window._preset_mgr = mgr  # noqa: SLF001

        assert window._meas_panel.is_auto_save_enabled() is False  # noqa: SLF001
        window._on_preset_selected("forces_save")  # noqa: SLF001
        assert window._meas_panel.is_auto_save_enabled() is False  # noqa: SLF001
    finally:
        window.close()


def test_preset_selection_preserves_user_enabled_auto_save(
    qapp, tmp_path
) -> None:
    """Selecting a non-EIS preset never CLEARS a user-enabled auto-save.

    Review finding #5: the select handler used to force the checkbox to
    the technique's forced state, silently unchecking a manual choice.
    """
    from src.data.presets import Preset, PresetManager

    window = MainWindow()
    try:
        mgr = PresetManager(path=str(tmp_path / "store.mux16"))
        mgr.add_preset(
            "plain_cv",
            Preset(name="plain_cv", technique="cv", channels=[1]),
        )
        window._preset_mgr = mgr  # noqa: SLF001

        # User manually enables auto-save, then browses to a CV preset.
        window._meas_panel.set_auto_save(True, "")  # noqa: SLF001
        window._on_preset_selected("plain_cv")  # noqa: SLF001
        assert window._meas_panel.is_auto_save_enabled() is True  # noqa: SLF001
    finally:
        window.close()


def test_sequence_stop_never_leaves_zero_plot_tabs(qapp) -> None:
    """The terminal hook restores a tab if a run created none.

    Review finding #8: a first step failing before measurement_started
    left zero tabs and a dead technique preview until the next run.
    """
    window = MainWindow()
    try:
        window._on_sequence_started()  # noqa: SLF001
        # Force the zero-tab condition the hook must recover from (a first
        # step failing before measurement_started would empty the strip).
        window._reset_plot_tabs()  # noqa: SLF001
        assert window._plot_tabs.count() == 0  # noqa: SLF001
        window._on_sequence_stopped()  # noqa: SLF001
        assert window._plot_tabs.count() == 1  # noqa: SLF001
        assert window._plot_container is not None  # noqa: SLF001
    finally:
        window.close()


def test_no_retained_results_means_no_prompt(qapp, monkeypatch) -> None:
    """With nothing retained (everything auto-saved), no prompt appears.

    Auto-saved steps are not retained at all (their data is already on
    disk), so the terminal hook has nothing to offer and must not raise
    a dialog.
    """
    monkeypatch.setattr(QMessageBox, "question", staticmethod(_no_modal))
    window = MainWindow()
    try:
        window._sequence_results = []  # noqa: SLF001
        # Must not raise via _no_modal (nothing to save -> no prompt).
        window._offer_sequence_save()  # noqa: SLF001
    finally:
        window.close()


def test_sequence_end_saves_all_steps_when_accepted(
    qapp, tmp_path, monkeypatch
) -> None:
    """Retained steps + accept the prompt -> every step is written.

    Lands one ``<stamp>_sequence`` parent with a ``stepNN_<technique>``
    subfolder per step — the SAME layout the runner's live auto-save
    path produces (shared helpers).
    """
    from src.data.models import DataPoint, MeasurementResult

    monkeypatch.setattr(
        main_window_mod, "get_export_dir", lambda: str(tmp_path)
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: None)
    )
    window = MainWindow()
    try:

        def _result(tech):
            r = MeasurementResult(technique=tech)
            r.add_point(
                DataPoint(
                    timestamp=0.1,
                    channel=1,
                    variables={"potential": 0.1, "current": 1e-5},
                )
            )
            return r

        window._sequence_results = [  # noqa: SLF001
            _result("cv"),
            _result("eis"),
        ]
        window._offer_sequence_save()  # noqa: SLF001

        seq_dirs = list(tmp_path.glob("*_sequence"))
        assert len(seq_dirs) == 1
        steps = sorted(p.name for p in seq_dirs[0].iterdir())
        assert steps == ["step01_cv", "step02_eis"]
        assert (seq_dirs[0] / "step01_cv" / "ch01.csv").exists()
        assert window._sequence_results == []  # noqa: SLF001
    finally:
        window.close()


def test_stopped_sequence_still_offers_to_save(
    qapp, tmp_path, monkeypatch
) -> None:
    """A stopped/errored sequence offers its completed steps for save.

    The save offer hangs off sequence_stopped — the one terminal hook
    fired on finish, stop, AND error — so an early end can no longer
    silently discard the completed steps' retained data.
    """
    from src.data.models import DataPoint, MeasurementResult

    monkeypatch.setattr(
        main_window_mod, "get_export_dir", lambda: str(tmp_path)
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: None)
    )
    window = MainWindow()
    try:
        r = MeasurementResult(technique="ca")
        r.add_point(
            DataPoint(
                timestamp=0.1,
                channel=1,
                variables={"potential": 0.1, "current": 1e-5},
            )
        )
        window._sequence_active = True  # noqa: SLF001
        window._sequence_results = [r]  # noqa: SLF001
        # Simulate the user stopping mid-run: the terminal hook fires.
        window._on_sequence_stopped()  # noqa: SLF001

        seq_dirs = list(tmp_path.glob("*_sequence"))
        assert len(seq_dirs) == 1
        assert (seq_dirs[0] / "step01_ca" / "ch01.csv").exists()
        assert window._sequence_results == []  # noqa: SLF001
    finally:
        window.close()


def test_sequence_end_declined_saves_nothing(
    qapp, tmp_path, monkeypatch
) -> None:
    """Declining the save prompt writes no files."""
    from src.data.models import MeasurementResult

    monkeypatch.setattr(
        main_window_mod, "get_export_dir", lambda: str(tmp_path)
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )
    window = MainWindow()
    try:
        window._sequence_results = [  # noqa: SLF001
            MeasurementResult(technique="cv")
        ]
        window._offer_sequence_save()  # noqa: SLF001
        assert list(tmp_path.glob("*_sequence")) == []
        assert window._sequence_results == []  # noqa: SLF001
    finally:
        window.close()


def test_autosaved_sequence_step_gets_pssession(
    qapp, tmp_path, monkeypatch
) -> None:
    """An auto-saved sequence step gets a .pssession in its step dir.

    The live auto-save path streams CSVs only; the finished handler
    completes the convention (CSV + .pssession per run) by exporting the
    session file into the step's auto-save dir, and the step is NOT
    retained for the end prompt.
    """
    from src.data.models import DataPoint, MeasurementResult

    window = MainWindow()
    try:
        step_dir = tmp_path / "step01_eis"
        step_dir.mkdir()
        r = MeasurementResult(technique="eis")
        r.add_point(
            DataPoint(
                timestamp=0.1,
                channel=1,
                variables={
                    "zreal": 100.0,
                    "zimag": -50.0,
                    "set_frequency": 1000.0,
                },
            )
        )
        window._sequence_active = True  # noqa: SLF001
        window._sequence_results = []  # noqa: SLF001
        # Engine order: auto_save_completed fires before finished.
        window._on_auto_save_completed(str(step_dir))  # noqa: SLF001
        window._on_measurement_finished(r)  # noqa: SLF001

        assert (step_dir / "eis.pssession").exists()
        assert window._sequence_results == []  # noqa: SLF001 (not retained)
    finally:
        window.close()


class _FakeResult:
    """Minimal MeasurementResult stand-in for the finished handler."""

    num_points = 3
    measured_channels = [1]

"""Tests for the preset SequenceRunner (sequencer feature -- Phase 4).

Drives :class:`src.engine.sequence_runner.SequenceRunner` with a MOCK
engine (a ``QObject`` exposing the same ``measurement_finished`` /
``measurement_error`` signals, a ``start_measurement`` recorder, and
``isRunning()``) so no serial hardware or real QThread is involved.

Covers the three queue invariants:
  * step N+1 is not started until step N's ``measurement_finished`` fires,
  * the queue runs to completion and emits ``sequence_finished``, and
  * a ``measurement_error`` halts the queue and emits ``sequence_error``.
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots headless (CI / WSL).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.data.models import TechniqueConfig  # noqa: E402
from src.engine.sequence_runner import (  # noqa: E402
    SequenceRunner,
    _QueueEntry,
)


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class MockEngine(QObject):
    """Minimal stand-in for ``MeasurementEngine``.

    Records every ``start_measurement`` call and lets the test drive the
    lifecycle by calling :meth:`finish_current` / :meth:`error_current`.
    ``isRunning()`` reflects whether a step is in flight, mirroring the
    real engine's single-run guard so the runner's gating is exercised.
    """

    measurement_finished = pyqtSignal(object)
    measurement_error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.started_configs: list[TechniqueConfig] = []
        self._running = False

    def start_measurement(self, connection, config) -> None:
        if self._running:
            raise RuntimeError("MockEngine is already running.")
        self._running = True
        self.started_configs.append(config)

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self._running

    def finish_current(self) -> None:
        """Emit measurement_finished for the in-flight step."""
        self._running = False
        self.measurement_finished.emit(object())

    def error_current(self, message: str) -> None:
        """Emit measurement_error for the in-flight step."""
        self._running = False
        self.measurement_error.emit(message)


def _make_config(technique: str) -> TechniqueConfig:
    """Build a trivially-valid external-mode config."""
    return TechniqueConfig(
        technique=technique,
        params={},
        channels=[1],
        electrode_config_mode="external",
    )


def _queue(*techniques: str) -> list[_QueueEntry]:
    """Build a zero-delay queue of single-channel configs."""
    return [
        _QueueEntry(config=_make_config(t), delay_s=0.0)
        for t in techniques
    ]


def _drain(qapp) -> None:
    """Process pending Qt events so deferred singleShot timers fire."""
    for _ in range(50):
        qapp.processEvents()


def test_step_two_waits_for_step_one_finish(qapp) -> None:
    """Step N+1 only starts after step N's finished signal."""
    engine = MockEngine()
    runner = SequenceRunner(engine, None, _queue("cv", "ca"))

    runner.start()
    # Only the first step has launched.
    assert len(engine.started_configs) == 1
    assert engine.started_configs[0].technique == "cv"

    # Draining events must NOT advance while step 1 is still "running".
    _drain(qapp)
    assert len(engine.started_configs) == 1

    # Finish step 1 -> step 2 launches (after the deferred advance fires).
    engine.finish_current()
    _drain(qapp)
    assert len(engine.started_configs) == 2
    assert engine.started_configs[1].technique == "ca"


def test_queue_completes_to_finished(qapp) -> None:
    """Running the whole queue emits sequence_finished exactly once."""
    engine = MockEngine()
    runner = SequenceRunner(engine, None, _queue("cv", "ca", "dpv"))

    progress: list[tuple[int, int]] = []
    finished: list[bool] = []
    runner.sequence_progress.connect(
        lambda c, t: progress.append((c, t))
    )
    runner.sequence_finished.connect(lambda: finished.append(True))

    runner.start()
    for _ in range(3):
        engine.finish_current()
        _drain(qapp)

    assert len(engine.started_configs) == 3
    assert finished == [True]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert runner.sequence_mode is False


def test_error_halts_queue(qapp) -> None:
    """A measurement_error stops the queue and emits sequence_error."""
    engine = MockEngine()
    runner = SequenceRunner(engine, None, _queue("cv", "ca", "dpv"))

    errors: list[str] = []
    finished: list[bool] = []
    runner.sequence_error.connect(errors.append)
    runner.sequence_finished.connect(lambda: finished.append(True))

    runner.start()
    assert len(engine.started_configs) == 1

    engine.error_current("Device error: boom")
    _drain(qapp)

    # Queue halted: step 2 never launched, error surfaced, no finish.
    assert len(engine.started_configs) == 1
    assert errors == ["Device error: boom"]
    assert finished == []
    assert runner.sequence_mode is False


def test_repeat_expands_into_extra_runs(qapp) -> None:
    """A step's repeat count expands into that many engine runs."""
    from src.data.presets import Preset, PresetManager
    from src.data.sequence import Sequence, SequenceStep

    mgr = PresetManager(path=str(_tmp_store()))
    mgr.add_preset(
        "cv1",
        Preset(name="cv1", technique="cv", channels=[1]),
    )
    seq = Sequence(
        name="s", steps=[SequenceStep(preset_name="cv1", repeat=3)]
    )

    engine = MockEngine()
    runner = SequenceRunner.from_sequence(engine, None, seq, mgr)
    assert runner.total_steps == 3

    runner.start()
    for _ in range(3):
        engine.finish_current()
        _drain(qapp)
    assert len(engine.started_configs) == 3


def test_start_refuses_when_engine_already_running(qapp) -> None:
    """start() refuses (errors) when the engine is already busy.

    The runner must honour the engine's single-run guard: if the engine
    is mid-measurement when ``start()`` is called, no step launches and a
    ``sequence_error`` is surfaced instead.
    """
    engine = MockEngine()
    # Simulate a foreign measurement already in flight on the engine.
    engine.start_measurement(None, _make_config("cv"))
    assert engine.isRunning() is True

    runner = SequenceRunner(engine, None, _queue("ca", "dpv"))
    errors: list[str] = []
    finished: list[bool] = []
    runner.sequence_error.connect(errors.append)
    runner.sequence_finished.connect(lambda: finished.append(True))

    runner.start()

    # No NEW step from the runner launched; only the pre-existing config.
    assert len(engine.started_configs) == 1
    assert len(errors) == 1
    assert finished == []
    assert runner.sequence_mode is False


def test_late_finish_after_stop_adds_no_phantom_step(qapp) -> None:
    """A finished signal arriving after stop() must not resurrect the queue.

    Stopping clears the running flag; a delayed ``measurement_finished``
    from the already-launched step must be ignored -- no next step is
    launched, no progress is emitted, and no extra ``sequence_finished``
    fires.
    """
    engine = MockEngine()
    runner = SequenceRunner(engine, None, _queue("cv", "ca", "dpv"))

    progress: list[tuple[int, int]] = []
    finished: list[bool] = []
    runner.sequence_progress.connect(
        lambda c, t: progress.append((c, t))
    )
    runner.sequence_finished.connect(lambda: finished.append(True))

    runner.start()
    assert len(engine.started_configs) == 1

    # User stops mid-step, THEN the in-flight step's finished arrives late.
    runner.stop()
    assert runner.sequence_mode is False
    engine.finish_current()
    _drain(qapp)

    # The late finish was swallowed: no step 2, no progress, no finish.
    assert len(engine.started_configs) == 1
    assert progress == []
    assert finished == []


def test_auto_save_all_gives_every_entry_a_unique_step_dir(qapp) -> None:
    """auto_save_all gives every queue entry its own stepNN exact dir.

    Repeats expand into entries with DISTINCT directories under one
    ``<base>/<stamp>_sequence`` parent, so two same-second runs of one
    technique can never collide (the old shared-parent +
    second-resolution writer leaf overwrote chNN.csv); ``exact_dir``
    tells the writer not to add its own timestamped leaf.
    """
    from src.data.presets import Preset, PresetManager
    from src.data.sequence import Sequence, SequenceStep

    mgr = PresetManager(path=str(_tmp_store()))
    mgr.add_preset("cv1", Preset(name="cv1", technique="cv", channels=[1]))
    mgr.add_preset("ca1", Preset(name="ca1", technique="ca", channels=[1]))
    seq = Sequence(
        name="s",
        steps=[
            SequenceStep(preset_name="cv1"),
            SequenceStep(preset_name="ca1", repeat=2),
        ],
    )

    base = os.path.join("some", "export", "root")
    runner = SequenceRunner.from_sequence(
        MockEngine(), None, seq, mgr,
        base_export_dir=base, auto_save_all=True,
    )

    # repeat=2 -> 3 queued runs, each with its OWN exact step dir.
    assert runner.total_steps == 3
    dirs = [e.config.auto_save.output_dir for e in runner._queue]
    assert len(set(dirs)) == 3  # unique per entry, repeats included
    names = [os.path.basename(d) for d in dirs]
    assert names == ["step01_cv", "step02_ca", "step03_ca"]
    assert all(e.config.auto_save.enabled for e in runner._queue)
    assert all(e.config.auto_save.exact_dir for e in runner._queue)
    # All under one shared <stamp>_sequence parent == sequence_dir.
    parents = {os.path.dirname(d) for d in dirs}
    assert parents == {runner.sequence_dir}
    assert runner.sequence_dir.startswith(base)
    assert os.path.basename(runner.sequence_dir).endswith("_sequence")
    # Repeat entries are independent config objects (no aliasing).
    assert runner._queue[1].config is not runner._queue[2].config


def test_no_base_export_dir_leaves_auto_save_unset(qapp) -> None:
    """Without a base dir, steps carry no auto-save (opt-in default off)."""
    from src.data.presets import Preset, PresetManager
    from src.data.sequence import Sequence, SequenceStep

    mgr = PresetManager(path=str(_tmp_store()))
    mgr.add_preset("cv1", Preset(name="cv1", technique="cv", channels=[1]))
    seq = Sequence(name="s", steps=[SequenceStep(preset_name="cv1")])

    runner = SequenceRunner.from_sequence(MockEngine(), None, seq, mgr)

    assert all(e.config.auto_save is None for e in runner._queue)
    assert runner.sequence_dir is None


def test_eis_step_follows_toggle_and_is_not_forced(qapp) -> None:
    """EIS steps are NOT force-auto-saved: with auto_save_all=False no step
    auto-saves, EIS included. Auto-save is fully opt-in for every technique
    (the previous EIS/GEIS provenance forcing was removed)."""
    from src.data.presets import Preset, PresetManager
    from src.data.sequence import Sequence, SequenceStep

    mgr = PresetManager(path=str(_tmp_store()))
    mgr.add_preset("cv1", Preset(name="cv1", technique="cv", channels=[1]))
    mgr.add_preset(
        "eis1", Preset(name="eis1", technique="eis", channels=[1])
    )
    seq = Sequence(
        name="s",
        steps=[
            SequenceStep(preset_name="cv1"),
            SequenceStep(preset_name="eis1"),
        ],
    )

    runner = SequenceRunner.from_sequence(
        MockEngine(), None, seq, mgr,
        base_export_dir=os.path.join("root"), auto_save_all=False,
    )

    cv_entry, eis_entry = runner._queue
    assert cv_entry.config.auto_save is None  # toggle off
    assert eis_entry.config.auto_save is None  # toggle off, EIS not forced


def _tmp_store():
    """Return a throwaway preset-store path in a temp dir."""
    import tempfile

    return os.path.join(
        tempfile.mkdtemp(prefix="seqrunner_"), "store.mux16"
    )

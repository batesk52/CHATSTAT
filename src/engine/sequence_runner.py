"""Sequential preset runner for the EmStat Pico MUX16 (sequencer feature).

Drives a queue of resolved :class:`~src.data.models.TechniqueConfig`
steps back-to-back through the existing :class:`MeasurementEngine`, a
PSTrace-"Scripts" equivalent.  The runner never opens a second execution
path: it chains ``engine.start_measurement`` calls, advancing on the
engine's ``measurement_finished`` signal and gating on ``isRunning()`` so
the engine's single-run guard is honoured.

Design (see ``architecture.md`` -> "Preset Sequencer"):

* The whole queue is resolved (and therefore validated by
  ``TechniqueConfig.__post_init__``) up front in :meth:`from_sequence`, so
  a Mode-C step with no usable RE/CE pairing fails before step 0 launches
  rather than mid-run.
* A step's ``repeat`` is expanded into that many queue entries; the
  step's ``delay_s`` is applied only AFTER the last repeat, inserted via
  ``QTimer.singleShot`` so the GUI event loop keeps spinning.
* ``sequence_mode`` is exposed so the main window can suppress the
  interactive export prompt. With a ``base_export_dir`` AND
  ``auto_save_all`` (auto-save is fully opt-in), every entry writes into
  its own ``<base>/<stamp>_sequence/stepNN_<technique>/`` dir — unique per
  queue entry including repeats, so same-second runs cannot collide —
  matching the end-of-run save prompt's layout exactly. Without a base dir
  or with the toggle off, the sequence runs without writing.

Signals:
    sequence_progress(int, int): Emitted as ``(completed, total)`` each
        time a step finishes (1-indexed completed count).
    sequence_finished(): Emitted once when every step has completed.
    sequence_error(str): Emitted when a step errors; the queue halts.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from src.data.exporters import make_sequence_dir, sequence_step_dirname
from src.data.models import AutoSaveConfig, TechniqueConfig
from src.data.presets import PresetManager
from src.data.sequence import Sequence, build_config

logger = logging.getLogger(__name__)

# Re-poll cadence (ms) while waiting for the engine thread to settle
# between steps, plus a bounded number of re-polls before giving up.  At
# 20 ms a 200-poll cap gives ~4 s of grace for the engine's
# ``measurement_finished`` signal to be followed by ``isRunning()`` going
# False; past that the engine is wedged, so the sequence errors out rather
# than re-arming the timer forever.
_ADVANCE_REPOLL_MS = 20
_ADVANCE_MAX_REPOLLS = 200


@dataclass
class _QueueEntry:
    """One resolved step in the run queue.

    Attributes:
        config: The validated technique configuration to run.
        delay_s: Idle delay inserted AFTER this entry before the next
            one starts.  Non-zero only on the final repeat of a step.
    """

    config: TechniqueConfig
    delay_s: float = 0.0


class SequenceRunner(QObject):
    """Run a queue of technique configs back-to-back via the engine.

    The runner is a thin orchestrator: it owns no serial I/O and reuses
    the engine for every step.  It connects to the engine's
    ``measurement_finished`` / ``measurement_error`` signals and advances
    the queue from there, never starting a new step while the engine is
    still running.

    Args:
        engine: The shared measurement engine.  Must expose
            ``start_measurement(connection, config)``,
            ``measurement_finished``, ``measurement_error``, and
            ``isRunning()``.
        connection: The connected ``PicoConnection`` passed through to
            each ``start_measurement`` call.  May be ``None`` for the
            mock-engine unit tests, which ignore it.
        queue: Resolved queue entries to run in order.
        parent: Optional Qt parent.
    """

    sequence_progress = pyqtSignal(int, int)  # (completed, total)
    sequence_finished = pyqtSignal()
    sequence_error = pyqtSignal(str)

    def __init__(
        self,
        engine: object,
        connection: object,
        queue: list[_QueueEntry],
        sequence_dir: Optional[str] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._connection = connection
        self._queue: list[_QueueEntry] = list(queue)
        self._index: int = 0
        self._running: bool = False
        # Re-poll counter for the inter-step settle wait; reset before
        # each advance and capped so a wedged engine surfaces an error.
        self._advance_repolls: int = 0
        # Shared <base>/<stamp>_sequence/ parent the auto-saving entries
        # write into (composed by from_sequence); None when no entry
        # auto-saves.
        self._sequence_dir: Optional[str] = sequence_dir

        # Chain on the engine's lifecycle signals.  Done once here so the
        # connections survive across all steps.
        self._engine.measurement_finished.connect(self._on_step_finished)
        self._engine.measurement_error.connect(self._on_step_error)

    # -- construction helpers ---------------------------------------------

    @classmethod
    def from_sequence(
        cls,
        engine: object,
        connection: object,
        sequence: Sequence,
        preset_manager: PresetManager,
        base_export_dir: Optional[str] = None,
        auto_save_all: bool = False,
        parent: Optional[QObject] = None,
    ) -> "SequenceRunner":
        """Build a runner by resolving a sequence against a preset store.

        Each step is resolved with :func:`build_config` (which validates
        via ``TechniqueConfig.__post_init__``) and expanded by its
        ``repeat`` count — a FRESH config per queue entry, so per-entry
        settings (like the auto-save dir) can never alias across
        repeats.  The step's ``delay_s`` lands only on the last repeat.
        Resolution happens eagerly so an invalid step raises here,
        before any measurement starts.

        Auto-save policy (requires ``base_export_dir``): when
        ``auto_save_all`` is set (the GUI auto-save toggle) every entry
        auto-saves; otherwise nothing auto-saves — auto-save is fully
        opt-in for every technique.  Auto-saving entries each get
        their own ``<base>/<stamp>_sequence/stepNN_<technique>/`` dir
        (``exact_dir`` — unique per entry, repeats included, so two
        same-second runs of one technique can never collide), matching
        the layout of the end-of-run save prompt.

        Args:
            engine: Shared measurement engine.
            connection: Connected ``PicoConnection`` (or ``None``).
            sequence: The sequence of preset steps to run.
            preset_manager: Store the step preset names resolve against.
            base_export_dir: Root exports directory for per-step
                auto-save.  ``None`` disables auto-save entirely (no
                entry writes).
            auto_save_all: True when the user enabled the auto-save
                toggle — every entry auto-saves.  Auto-save is fully
                opt-in: with the toggle off, nothing auto-saves.
            parent: Optional Qt parent.

        Returns:
            A ready-to-:meth:`start` ``SequenceRunner``.

        Raises:
            KeyError: If a step references an unknown preset name.
            ValueError: If a resolved step fails ``TechniqueConfig``
                validation (e.g. Mode-C with no RE/CE pairing).
        """
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        seq_dir = (
            make_sequence_dir(base_export_dir, stamp)
            if base_export_dir
            else None
        )
        any_auto_save = False

        queue: list[_QueueEntry] = []
        for step in sequence.steps:
            # Embedded steps carry their own config; only legacy
            # reference steps still need the preset store.
            preset = None
            if not step.is_embedded:
                preset = preset_manager.get_preset(step.preset_name)
                if preset is None:
                    raise KeyError(
                        f"Sequence step references unknown preset: "
                        f"{step.preset_name!r}"
                    )
            repeat = max(1, int(step.repeat))
            for rep in range(repeat):
                # Fresh config per entry (no aliasing across repeats).
                config = build_config(step, preset)
                if seq_dir is not None and auto_save_all:
                    config.auto_save = AutoSaveConfig(
                        enabled=True,
                        output_dir=os.path.join(
                            seq_dir,
                            sequence_step_dirname(
                                len(queue), config.technique
                            ),
                        ),
                        exact_dir=True,
                    )
                    any_auto_save = True
                # Delay only after the final repeat of the step.
                delay = step.delay_s if rep == repeat - 1 else 0.0
                queue.append(_QueueEntry(config=config, delay_s=delay))
        return cls(
            engine,
            connection,
            queue,
            sequence_dir=seq_dir if any_auto_save else None,
            parent=parent,
        )

    # -- public API --------------------------------------------------------

    @property
    def sequence_mode(self) -> bool:
        """Whether a sequence is currently driving the engine.

        The main window reads this to suppress the interactive export
        prompt and auto-save each step instead.
        """
        return self._running

    @property
    def total_steps(self) -> int:
        """Total number of queued step runs (repeats expanded)."""
        return len(self._queue)

    @property
    def sequence_dir(self) -> Optional[str]:
        """Shared parent dir auto-saving steps write into (None if none)."""
        return self._sequence_dir

    def start(self) -> None:
        """Launch the first step.

        Does nothing for an empty queue beyond emitting
        ``sequence_finished``.  Refuses to start while the engine is
        already running so the single-run guard is never violated.
        """
        if self._running:
            logger.warning(
                "SequenceRunner.start() while already running."
            )
            return
        if not self._queue:
            logger.info("SequenceRunner started with an empty queue.")
            self.sequence_finished.emit()
            return
        if self._engine.isRunning():
            self.sequence_error.emit(
                "Engine is busy; cannot start a sequence."
            )
            return
        self._running = True
        self._index = 0
        self._launch_current()

    def stop(self) -> None:
        """Halt the sequence after the current step.

        Clears the running flag so a finished/queued step does not start
        the next one.  An in-flight engine run is left to the caller to
        abort (the runner owns no serial I/O).
        """
        self._running = False

    # -- internal ----------------------------------------------------------

    def _launch_current(self) -> None:
        """Start the engine on the current queue entry."""
        entry = self._queue[self._index]
        try:
            self._engine.start_measurement(
                self._connection, entry.config
            )
        except RuntimeError as exc:
            # Engine reported busy despite our guard -- surface it and
            # halt rather than silently dropping the step.
            self._running = False
            self.sequence_error.emit(str(exc))
            return
        logger.info(
            "Sequence step %d/%d started (%s).",
            self._index + 1,
            len(self._queue),
            entry.config.technique,
        )

    @pyqtSlot(object)
    def _on_step_finished(self, _result: object) -> None:
        """Advance the queue when the engine finishes a step.

        Ignored when no sequence is active so a stray standalone-run
        completion never drives the queue.
        """
        if not self._running:
            return
        completed = self._index + 1
        self.sequence_progress.emit(completed, len(self._queue))

        if completed >= len(self._queue):
            self._running = False
            logger.info("Sequence complete: %d steps.", completed)
            self.sequence_finished.emit()
            return

        delay_s = self._queue[self._index].delay_s
        self._index += 1
        delay_ms = max(0, int(delay_s * 1000))
        # Defer the next step so the engine thread fully finishes (the
        # finished signal can fire while isRunning() is briefly still
        # True) and the inter-step delay is honoured.
        self._advance_repolls = 0
        QTimer.singleShot(delay_ms, self._advance)

    def _advance(self) -> None:
        """Launch the next step once the engine has settled.

        While the engine is still winding down its previous run, this
        re-arms a short timer instead of launching (never violating the
        single-run guard).  The re-poll count is bounded: a wedged engine
        that never goes idle halts the queue with ``sequence_error``
        rather than re-polling forever.
        """
        if not self._running:
            return
        if self._engine.isRunning():
            self._advance_repolls += 1
            if self._advance_repolls > _ADVANCE_MAX_REPOLLS:
                # Engine never went idle within the grace window -- treat
                # it as wedged and surface an error instead of spinning.
                self._running = False
                msg = (
                    "Engine did not become idle between steps "
                    f"(step {self._index + 1}/{len(self._queue)}); "
                    "sequence aborted."
                )
                logger.error(msg)
                self.sequence_error.emit(msg)
                return
            # Engine not yet idle -- re-check shortly without launching.
            QTimer.singleShot(_ADVANCE_REPOLL_MS, self._advance)
            return
        self._launch_current()

    @pyqtSlot(str)
    def _on_step_error(self, message: str) -> None:
        """Halt the queue and re-emit when a step errors."""
        if not self._running:
            return
        self._running = False
        logger.error(
            "Sequence halted at step %d/%d: %s",
            self._index + 1,
            len(self._queue),
            message,
        )
        self.sequence_error.emit(message)

"""Tests for the SequencePanel (sequencer feature -- Phase 3).

Exercises :class:`src.gui.sequence_panel.SequencePanel` headless
(offscreen): step blocks reorder through the list model, the visual
order maps to ``Sequence.steps`` order, and a sequence round-trips
through a temp ``*.mux16seq`` file.

Reordering is driven via the panel's reorder handlers (the per-block
move buttons) -- NOT a physical drag -- so the test is deterministic.
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots headless (CI / WSL).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.data.sequence import Sequence, SequenceStep  # noqa: E402
from src.gui.sequence_panel import SequencePanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _step_names(panel: SequencePanel) -> list[str]:
    """Return the preset_name of every step in visual order."""
    return [s.preset_name for s in panel.build_sequence().steps]


def test_three_blocks_added_in_order(qapp) -> None:
    """Adding three step blocks preserves insertion order."""
    panel = SequencePanel()
    panel.add_step(SequenceStep(preset_name="cv1"))
    panel.add_step(SequenceStep(preset_name="ca1"))
    panel.add_step(SequenceStep(preset_name="dpv1"))

    assert _step_names(panel) == ["cv1", "ca1", "dpv1"]


def test_reorder_via_buttons_changes_sequence_order(qapp) -> None:
    """Moving a block down via its reorder control reorders Sequence.steps."""
    panel = SequencePanel()
    for name in ("cv1", "ca1", "dpv1"):
        panel.add_step(SequenceStep(preset_name=name))

    # Move the first block (cv1) to the end via the reorder handler.
    first = panel._step_widgets()[0]  # noqa: SLF001 - test introspection
    panel._on_move_down(first)  # noqa: SLF001 -> [ca1, cv1, dpv1]
    panel._on_move_down(first)  # noqa: SLF001 -> [ca1, dpv1, cv1]

    assert _step_names(panel) == ["ca1", "dpv1", "cv1"]


def test_save_reload_round_trips(qapp, tmp_path) -> None:
    """A built sequence saves to *.mux16seq and reloads equal."""
    panel = SequencePanel()
    panel.add_step(SequenceStep(preset_name="cv1", repeat=2, delay_s=1.5))
    panel.add_step(SequenceStep(preset_name="ca1"))
    panel.add_step(SequenceStep(preset_name="dpv1", delay_s=0.5))

    path = str(tmp_path / "seq.mux16seq")
    panel.build_sequence(name="round").save_to_path(path)

    reloaded = Sequence.load_from_path(path)
    # Load it back into a fresh panel and compare the recomposed model.
    panel2 = SequencePanel()
    panel2.load_sequence(reloaded)

    assert panel2.build_sequence(name="round") == panel.build_sequence(
        name="round"
    )
    # Independent ground-truth check on the persisted file.
    assert reloaded.steps[0].preset_name == "cv1"
    assert reloaded.steps[0].repeat == 2
    assert reloaded.steps[0].delay_s == 1.5
    assert [s.preset_name for s in reloaded.steps] == [
        "cv1",
        "ca1",
        "dpv1",
    ]


def test_run_refused_while_engine_busy_without_side_effects(
    qapp, tmp_path, monkeypatch
) -> None:
    """Run on a busy engine is refused BEFORE sequence_started fires.

    The old order emitted sequence_started first, which made the main
    window destroy the in-flight single run's live plot and then
    re-enable Start while the measurement was still running.
    """
    from PyQt6.QtWidgets import QMessageBox

    from src.data.presets import Preset, PresetManager

    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: warnings.append(a[1])),
    )

    class BusyEngine:
        def isRunning(self) -> bool:  # noqa: N802 - Qt naming
            return True

    mgr = PresetManager(path=str(tmp_path / "store.mux16"))
    mgr.add_preset("cv1", Preset(name="cv1", technique="cv", channels=[1]))
    panel = SequencePanel(preset_manager=mgr, engine=BusyEngine())
    panel.add_step(SequenceStep(preset_name="cv1"))

    started: list[bool] = []
    panel.sequence_started.connect(lambda: started.append(True))

    panel._on_run()  # noqa: SLF001

    assert started == []  # signal never fired
    assert panel._runner is None  # noqa: SLF001 no runner built
    assert any("Engine Busy" in w for w in warnings)


def test_legacy_step_upgraded_to_embedded_on_load(qapp, tmp_path) -> None:
    """A legacy reference step embeds its config from the store on load."""
    from src.data.presets import Preset, PresetManager

    mgr = PresetManager(path=str(tmp_path / "store.mux16"))
    mgr.add_preset(
        "cv1",
        Preset(
            name="cv1",
            technique="cv",
            params={"scan_rate": 0.1},
            channels=[1, 4],
        ),
    )
    panel = SequencePanel(preset_manager=mgr)
    # A legacy reference step (preset_name only, no embedded technique).
    panel.load_sequence(
        Sequence(name="s", steps=[SequenceStep(preset_name="cv1")])
    )

    step = panel.build_sequence().steps[0]
    assert step.is_embedded
    assert step.technique == "cv"
    assert step.channels == [1, 4]
    assert step.params["scan_rate"] == 0.1

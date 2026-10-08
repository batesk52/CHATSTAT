"""Preset SAVE captures the electrode mode (sequencer feature -- Task 5).

Verifies that "save current settings as preset" persists the live
electrode-config mode and the Mode-C per-WE RE/CE pairing, not just the
technique params + channels.  A Mode-C config is configured in the GUI,
saved, reloaded from disk, and the mode + ``re_ce_channels`` are asserted
to survive the round-trip.
"""

from __future__ import annotations

import os
import sys

import pytest

# Force offscreen platform so PyQt6 boots headless (CI / WSL).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

import PyQt6.QtWidgets as QtWidgets  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.data.presets import PresetManager  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all GUI tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_save_preset_persists_mode_c_pairing(
    qapp, tmp_path, monkeypatch
) -> None:
    """A saved Mode-C preset round-trips electrode mode + RE/CE pairing."""
    store = str(tmp_path / "store.mux16")

    window = MainWindow()
    try:
        # Repoint the active store at a temp file so the real per-user
        # store is never touched.
        window._preset_mgr = PresetManager(path=store)  # noqa: SLF001

        # Switch to manual (Mode C) and configure an explicit per-WE
        # RE/CE pairing: WE CH2 -> RE/CE CH5, WE CH3 -> RE/CE CH6.
        window._electrode_config_panel.set_mode("manual")  # noqa: SLF001
        window._manual_channel_panel.set_pairs(  # noqa: SLF001
            [2, 3], [5, 6]
        )

        # Stub the name prompt so _on_save_preset proceeds headlessly.
        monkeypatch.setattr(
            QtWidgets.QInputDialog,
            "getText",
            lambda *a, **k: ("Mode C Test", True),
        )

        window._on_save_preset()  # noqa: SLF001

        # Reload from disk into a fresh manager.
        reloaded = PresetManager(path=str(tmp_path / "other.mux16"))
        reloaded.load_from_path(store)
        preset = reloaded.get_preset("mode_c_test")

        assert preset is not None
        assert preset.electrode_config_mode == "manual"
        assert preset.channels == [2, 3]
        assert preset.re_ce_channels == [5, 6]
    finally:
        window.close()

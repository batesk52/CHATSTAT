"""Tests for the "Import preset file..." dropdown entry (sequencer feature).

Covers :class:`src.gui.controls.TechniquePanel`'s trailing import entry:
selecting it opens a file dialog (monkeypatched here), loads the chosen
``*.mux16`` store via ``PresetManager.load_from_path``, repopulates the
dropdown, and persists the path via ``set_last_preset_file``.

The app-settings path is overridden to a temp file so the real per-user
store is never touched.
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

from src.data.app_settings import get_last_preset_file  # noqa: E402
from src.data.presets import Preset, PresetManager  # noqa: E402
from src.gui.controls import (  # noqa: E402
    _IMPORT_PRESET_SENTINEL,
    TechniquePanel,
)


@pytest.fixture(scope="module")
def qapp():
    """Provide a single QApplication for all GUI tests in this module."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _combo_keys(panel: TechniquePanel) -> list[str]:
    """Return the itemData key of every preset-combo entry."""
    combo = panel._preset_combo  # noqa: SLF001 - test introspection
    return [combo.itemData(i) for i in range(combo.count())]


def _make_external_store(path: str) -> None:
    """Write a 2-preset external store to ``path``."""
    mgr = PresetManager(path=path)
    mgr.add_preset(
        "imported_cv",
        Preset(name="Imported CV", technique="cv", channels=[1, 2]),
    )
    mgr.add_preset(
        "imported_ca",
        Preset(name="Imported CA", technique="ca", channels=[3]),
    )


def test_import_entry_present_after_refresh(qapp) -> None:
    """The trailing import entry exists after a preset refresh."""
    panel = TechniquePanel()
    panel.refresh_presets({"a": "Alpha"}, deletable={"a"})
    keys = _combo_keys(panel)
    assert keys[-1] == _IMPORT_PRESET_SENTINEL
    assert keys.count(_IMPORT_PRESET_SENTINEL) == 1


def test_refresh_preserves_current_selection(qapp) -> None:
    """A rebuild keeps the selected preset selected (review finding #9).

    The main window re-runs its populate path right after an import;
    without preservation the dropdown visually reset to "(No Preset)"
    while the parameter form still showed the imported preset.
    """
    panel = TechniquePanel()
    panel.refresh_presets(
        {"a": "Alpha", "b": "Beta"}, deletable={"a", "b"}
    )
    combo = panel._preset_combo  # noqa: SLF001
    combo.setCurrentIndex(combo.findData("b"))
    assert combo.currentData() == "b"

    # A second refresh (same store) must keep "b" selected and keep the
    # delete button enabled for a deletable key.
    panel.refresh_presets(
        {"a": "Alpha", "b": "Beta"}, deletable={"a", "b"}
    )
    assert combo.currentData() == "b"
    assert panel._delete_preset_btn.isEnabled()  # noqa: SLF001

    # If the selected key disappears from the store, fall back to
    # "(No Preset)" with delete disabled.
    panel.refresh_presets({"a": "Alpha"}, deletable={"a"})
    assert combo.currentData() == ""
    assert not panel._delete_preset_btn.isEnabled()  # noqa: SLF001


def test_import_loads_repopulates_and_persists(
    qapp, tmp_path, monkeypatch
) -> None:
    """Selecting import loads the file, repopulates, and remembers it."""
    store = str(tmp_path / "external.mux16")
    settings = str(tmp_path / "app_settings.json")
    _make_external_store(store)

    mgr = PresetManager(path=str(tmp_path / "active.mux16"))
    panel = TechniquePanel()
    panel.set_preset_manager(mgr, settings_path=settings)
    panel.refresh_presets({}, deletable=set())

    imported: list[str] = []
    panel.presets_imported.connect(imported.append)

    # Stub the file dialog to "choose" the external store.
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        lambda *a, **k: (store, "MUX16 presets (*.mux16)"),
    )

    # Select the trailing import entry to trigger the dialog.
    combo = panel._preset_combo  # noqa: SLF001
    import_idx = combo.count() - 1
    combo.setCurrentIndex(import_idx)

    # Dropdown repopulated with the imported presets.
    keys = _combo_keys(panel)
    assert "imported_cv" in keys
    assert "imported_ca" in keys
    # Manager now points at the imported store.
    assert mgr.path == store
    assert "imported_cv" in mgr.list_presets()
    # Last-used pointer persisted to the temp settings file.
    assert get_last_preset_file(path=settings) == store
    # presets_imported emitted with the chosen path.
    assert imported == [store]


def test_import_cancelled_leaves_state_untouched(
    qapp, tmp_path, monkeypatch
) -> None:
    """A cancelled dialog does not reload or persist anything."""
    settings = str(tmp_path / "app_settings.json")
    mgr = PresetManager(path=str(tmp_path / "active.mux16"))
    panel = TechniquePanel()
    panel.set_preset_manager(mgr, settings_path=settings)
    panel.refresh_presets({"keep": "Keep Me"}, deletable={"keep"})

    imported: list[str] = []
    panel.presets_imported.connect(imported.append)

    # Empty path == user cancelled.
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        lambda *a, **k: ("", ""),
    )

    combo = panel._preset_combo  # noqa: SLF001
    combo.setCurrentIndex(combo.count() - 1)

    assert imported == []
    assert get_last_preset_file(path=settings) is None
    # The original preset is still in the dropdown.
    assert "keep" in _combo_keys(panel)

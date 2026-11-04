"""The Setup row: pick, save and delete named electrode setups in the GUI.

Headless (offscreen Qt). The main window is pointed at a temp store so the
person's own electrode presets are never read or written.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

import PyQt6.QtWidgets as QtWidgets  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.data import app_settings  # noqa: E402
from src.data.electrode_presets import (  # noqa: E402
    ElectrodePreset,
    ElectrodePresetManager,
)
from src.gui.controls import ElectrodeConfigPanel  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture()
def window(qapp, tmp_path):
    win = MainWindow()
    win._electrode_mgr = ElectrodePresetManager(  # noqa: SLF001
        str(tmp_path / "e.mux16el")
    )
    win._load_electrode_presets_into_ui()  # noqa: SLF001
    yield win
    win.close()


def _pick(win, key: str) -> None:
    panel = win._electrode_config_panel  # noqa: SLF001
    panel._setup_combo.setCurrentIndex(  # noqa: SLF001
        panel._setup_combo.findData(key)  # noqa: SLF001
    )


# ---- the panel on its own ---------------------------------------------------


def test_the_setup_row_starts_on_custom_and_lists_what_it_is_given(qapp) -> None:
    panel = ElectrodeConfigPanel()
    assert panel.selected_electrode_preset() == ""
    assert panel._setup_combo.itemText(0) == "(Custom)"  # noqa: SLF001
    panel.refresh_electrode_presets({"b": "Beta", "a": "alpha"}, {"a": "tip"}, {"b"})
    names = [panel._setup_combo.itemText(i) for i in range(3)]  # noqa: SLF001
    assert names == ["(Custom)", "alpha", "Beta"]
    assert panel._setup_combo.itemData(1, 3) == "tip"  # ToolTipRole  # noqa: SLF001


def test_picking_a_setup_emits_its_key_and_custom_emits_nothing(qapp) -> None:
    panel = ElectrodeConfigPanel()
    panel.refresh_electrode_presets({"a": "A", "b": "B"})
    seen: list[str] = []
    panel.electrode_preset_selected.connect(seen.append)
    panel._setup_combo.setCurrentIndex(2)  # noqa: SLF001
    panel._setup_combo.setCurrentIndex(0)  # noqa: SLF001
    assert seen == ["b"]


def test_select_and_show_custom_do_not_emit(qapp) -> None:
    panel = ElectrodeConfigPanel()
    panel.refresh_electrode_presets({"a": "A"})
    seen: list[str] = []
    panel.electrode_preset_selected.connect(seen.append)
    panel.select_electrode_preset("a")
    assert panel.selected_electrode_preset() == "a"
    panel.show_custom()
    assert panel.selected_electrode_preset() == ""
    assert seen == []


def test_only_a_deletable_setup_can_be_deleted(qapp) -> None:
    panel = ElectrodeConfigPanel()
    panel.refresh_electrode_presets({"a": "A", "b": "B"}, deletable={"b"})
    deleted: list[str] = []
    panel.delete_electrode_preset_requested.connect(deleted.append)
    panel.select_electrode_preset("a")
    assert not panel._delete_setup_btn.isEnabled()  # noqa: SLF001
    panel.select_electrode_preset("b")
    assert panel._delete_setup_btn.isEnabled()  # noqa: SLF001
    panel._delete_setup_btn.click()  # noqa: SLF001
    assert deleted == ["b"]


def test_a_refresh_keeps_the_selection_that_survives(qapp) -> None:
    panel = ElectrodeConfigPanel()
    panel.refresh_electrode_presets({"a": "A", "b": "B"})
    panel.select_electrode_preset("b")
    panel.refresh_electrode_presets({"a": "A", "b": "B", "c": "C"})
    assert panel.selected_electrode_preset() == "b"
    panel.refresh_electrode_presets({"a": "A"})
    assert panel.selected_electrode_preset() == ""


# ---- picking a setup sets the wiring, in every mode -------------------------


def test_the_built_in_setups_are_in_the_window(window) -> None:
    combo = window._electrode_config_panel._setup_combo  # noqa: SLF001
    keys = {combo.itemData(i) for i in range(combo.count())}
    assert {
        "separate_re_ce",
        "on_chip_re_ce",
        "per_electrode_re_ce",
        "die_16_external",
        "die_16_on_chip",
    } <= keys


def test_mode_a_setup_sets_the_mode_and_channels(window) -> None:
    _pick(window, "separate_re_ce")
    assert window._electrode_config_panel.selected_mode() == "external"  # noqa: SLF001
    assert window._chan_panel.selected_channels() == list(range(1, 17))  # noqa: SLF001


def test_mode_b_setup_sets_the_mode_and_channels(window) -> None:
    _pick(window, "on_chip_re_ce")
    assert window._electrode_config_panel.selected_mode() == "on_board"  # noqa: SLF001
    assert window._chan_panel.selected_channels() == list(range(1, 17))  # noqa: SLF001


def test_mode_c_setup_sets_the_mode_and_the_pairing(window) -> None:
    _pick(window, "per_electrode_re_ce")
    assert window._electrode_config_panel.selected_mode() == "manual"  # noqa: SLF001
    we, re_ce = window._manual_channel_panel.selected_pairs()  # noqa: SLF001
    assert we == re_ce == list(range(1, 15))
    assert window._manual_channel_panel.isVisibleTo(window)  # noqa: SLF001
    assert not window._chan_panel.isVisibleTo(window)  # noqa: SLF001


def test_the_die_setup_selects_sixteen_channels_on_the_external_wiring(window) -> None:
    _pick(window, "die_16_external")
    assert window._electrode_config_panel.selected_mode() == "external"  # noqa: SLF001
    assert window._chan_panel.selected_channels() == list(range(1, 17))  # noqa: SLF001
    assert window._electrode_config_panel.selected_electrode_preset() == "die_16_external"  # noqa: SLF001


def test_the_on_chip_die_setup_selects_sixteen_channels_on_the_on_board_wiring(window) -> None:
    _pick(window, "die_16_on_chip")
    assert window._electrode_config_panel.selected_mode() == "on_board"  # noqa: SLF001
    assert window._chan_panel.selected_channels() == list(range(1, 17))  # noqa: SLF001
    assert window._electrode_config_panel.selected_electrode_preset() == "die_16_on_chip"  # noqa: SLF001


def test_switching_between_the_two_die_setups_changes_only_the_wiring(window) -> None:
    _pick(window, "die_16_external")
    external_channels = window._chan_panel.selected_channels()  # noqa: SLF001
    assert window._electrode_config_panel.selected_mode() == "external"  # noqa: SLF001
    _pick(window, "die_16_on_chip")
    assert window._electrode_config_panel.selected_mode() == "on_board"  # noqa: SLF001
    assert window._chan_panel.selected_channels() == external_channels  # noqa: SLF001
    _pick(window, "die_16_external")
    assert window._electrode_config_panel.selected_mode() == "external"  # noqa: SLF001


def test_applying_a_setup_keeps_it_selected(window) -> None:
    # Applying sets the radios and boxes; that must not read as an edit.
    for key in ("separate_re_ce", "per_electrode_re_ce", "on_chip_re_ce"):
        _pick(window, key)
        assert window._electrode_config_panel.selected_electrode_preset() == key  # noqa: SLF001


# ---- leaving a setup by hand shows "(Custom)" -------------------------------


def test_changing_a_channel_by_hand_shows_custom(window) -> None:
    _pick(window, "separate_re_ce")
    window._chan_panel._checkboxes[3].setChecked(False)  # noqa: SLF001
    assert window._electrode_config_panel.selected_electrode_preset() == ""  # noqa: SLF001


def test_changing_the_mode_by_hand_shows_custom(window) -> None:
    _pick(window, "separate_re_ce")
    window._electrode_config_panel._radio_on_board.setChecked(True)  # noqa: SLF001
    assert window._electrode_config_panel.selected_electrode_preset() == ""  # noqa: SLF001


def test_changing_a_pairing_by_hand_shows_custom(window) -> None:
    _pick(window, "per_electrode_re_ce")
    combo = window._manual_channel_panel._re_ce_combos[0]  # noqa: SLF001
    combo.setCurrentIndex((combo.currentIndex() + 1) % combo.count())
    assert window._electrode_config_panel.selected_electrode_preset() == ""  # noqa: SLF001


def test_loading_a_measurement_preset_leaves_the_setup_row_honest(window) -> None:
    _pick(window, "die_16_external")
    window._on_preset_selected("default_cv")  # noqa: SLF001  (CH1 only)
    assert window._chan_panel.selected_channels() == [1]  # noqa: SLF001
    assert window._electrode_config_panel.selected_electrode_preset() == ""  # noqa: SLF001


# ---- saving and deleting ----------------------------------------------------


def _name(monkeypatch, name: str) -> None:
    monkeypatch.setattr(
        QtWidgets.QInputDialog, "getText", lambda *a, **k: (name, True)
    )


def test_saving_a_mode_c_pairing_and_applying_it_again(window, monkeypatch, tmp_path) -> None:
    window._electrode_config_panel.set_mode("manual")  # noqa: SLF001
    window._manual_channel_panel.set_pairs([2, 3], [5, 6])  # noqa: SLF001
    _name(monkeypatch, "Bench pairs")
    window._on_save_electrode_preset()  # noqa: SLF001

    # It is on disk, in a fresh manager.
    saved = ElectrodePresetManager(str(tmp_path / "e.mux16el")).get_preset("bench_pairs")
    assert saved is not None
    assert (saved.electrode_config_mode, saved.channels, saved.re_ce_channels) == (
        "manual", [2, 3], [5, 6],
    )
    assert window._electrode_config_panel.selected_electrode_preset() == "bench_pairs"  # noqa: SLF001

    # Leave it, then apply it from the list: the pairing comes back.
    _pick(window, "separate_re_ce")
    assert window._electrode_config_panel.selected_mode() == "external"  # noqa: SLF001
    _pick(window, "bench_pairs")
    assert window._electrode_config_panel.selected_mode() == "manual"  # noqa: SLF001
    assert window._manual_channel_panel.selected_pairs() == ([2, 3], [5, 6])  # noqa: SLF001


@pytest.mark.parametrize("mode, radio", [("external", "_radio_external"), ("on_board", "_radio_on_board")])
def test_saving_a_mode_a_or_b_setup(window, monkeypatch, tmp_path, mode, radio) -> None:
    getattr(window._electrode_config_panel, radio).setChecked(True)  # noqa: SLF001
    window._chan_panel.set_channels([1, 4, 9])  # noqa: SLF001
    _name(monkeypatch, f"Three {mode}")
    window._on_save_electrode_preset()  # noqa: SLF001
    saved = ElectrodePresetManager(str(tmp_path / "e.mux16el")).get_preset(
        f"three_{mode}"
    )
    assert (saved.electrode_config_mode, saved.channels, saved.re_ce_channels) == (
        mode, [1, 4, 9], [],
    )


def test_saving_with_no_channel_is_refused_not_saved(window, monkeypatch, tmp_path) -> None:
    warned: list[str] = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    window._chan_panel.select_none()  # noqa: SLF001
    _name(monkeypatch, "Empty")
    window._on_save_electrode_preset()  # noqa: SLF001
    assert warned and "channels" in warned[0]
    assert not os.path.exists(tmp_path / "e.mux16el")


def test_saving_over_an_existing_name_asks_first(window, monkeypatch, tmp_path) -> None:
    window._chan_panel.set_channels([1, 2])  # noqa: SLF001
    _name(monkeypatch, "Dup")
    window._on_save_electrode_preset()  # noqa: SLF001
    window._chan_panel.set_channels([3])  # noqa: SLF001
    asked: list[bool] = []

    def decline(*a, **k):
        asked.append(True)
        return QtWidgets.QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", decline)
    window._on_save_electrode_preset()  # noqa: SLF001
    assert asked
    assert ElectrodePresetManager(str(tmp_path / "e.mux16el")).get_preset("dup").channels == [1, 2]

    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes
    )
    window._on_save_electrode_preset()  # noqa: SLF001
    assert ElectrodePresetManager(str(tmp_path / "e.mux16el")).get_preset("dup").channels == [3]


def test_deleting_a_saved_setup(window, monkeypatch, tmp_path) -> None:
    window._chan_panel.set_channels([1, 2])  # noqa: SLF001
    _name(monkeypatch, "Gone soon")
    window._on_save_electrode_preset()  # noqa: SLF001
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes
    )
    window._on_delete_electrode_preset("gone_soon")  # noqa: SLF001
    assert ElectrodePresetManager(str(tmp_path / "e.mux16el")).get_preset("gone_soon") is None
    assert window._electrode_config_panel.selected_electrode_preset() == ""  # noqa: SLF001


def test_a_built_in_setup_has_no_delete_button(window) -> None:
    _pick(window, "die_16_external")
    assert not window._electrode_config_panel._delete_setup_btn.isEnabled()  # noqa: SLF001


# ---- choosing the setups file (File > Electrode Setups File...) ------------------


def _choose_file(monkeypatch, path) -> None:
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), "")
    )


def _combo_keys(win) -> set:
    combo = win._electrode_config_panel._setup_combo  # noqa: SLF001
    return {combo.itemData(i) for i in range(combo.count())}


def test_the_file_menu_offers_the_setups_file(window) -> None:
    file_menu = next(
        a.menu() for a in window.menuBar().actions() if a.text() == "&File"
    )
    assert "Electrode &Setups File..." in [a.text() for a in file_menu.actions()]


def test_choosing_a_setups_file_loads_it_and_remembers_it(window, monkeypatch, tmp_path) -> None:
    target = tmp_path / "lab" / "mine.mux16el"
    target.parent.mkdir()
    ElectrodePresetManager(str(target)).add_preset(
        "bench_pair", ElectrodePreset("Bench pair", "manual", [2, 3], [5, 6])
    )
    assert "bench_pair" not in _combo_keys(window)

    _choose_file(monkeypatch, target)
    window._on_set_electrode_presets_file()  # noqa: SLF001

    assert window._electrode_mgr.path == str(target)  # noqa: SLF001
    assert app_settings.get_electrode_presets_path() == str(target)
    assert "bench_pair" in _combo_keys(window)
    assert "separate_re_ce" in _combo_keys(window)  # built-ins stay
    _pick(window, "bench_pair")
    assert window._electrode_config_panel.selected_mode() == "manual"  # noqa: SLF001
    assert window._manual_channel_panel.selected_pairs() == ([2, 3], [5, 6])  # noqa: SLF001


def test_the_choice_is_what_the_next_start_uses(window, monkeypatch, tmp_path) -> None:
    target = tmp_path / "mine.mux16el"
    ElectrodePresetManager(str(target)).add_preset(
        "kept", ElectrodePreset("Kept", "external", [7])
    )
    _choose_file(monkeypatch, target)
    window._on_set_electrode_presets_file()  # noqa: SLF001
    # A fresh manager with no argument finds the file through the setting.
    assert ElectrodePresetManager().get_preset("kept").channels == [7]


def test_a_new_setups_file_is_started_on_the_first_save(window, monkeypatch, tmp_path) -> None:
    target = tmp_path / "new" / "setups.mux16el"
    _choose_file(monkeypatch, target)
    window._on_set_electrode_presets_file()  # noqa: SLF001
    assert not target.exists()  # nothing is written by choosing

    window._chan_panel.set_channels([1, 2])  # noqa: SLF001
    monkeypatch.setattr(
        QtWidgets.QInputDialog, "getText", lambda *a, **k: ("First one", True)
    )
    window._on_save_electrode_preset()  # noqa: SLF001
    assert target.is_file()
    assert ElectrodePresetManager(str(target)).get_preset("first_one").channels == [1, 2]
    assert not (tmp_path / "e.mux16el").exists()  # the old file got nothing


def test_cancelling_the_file_dialog_changes_nothing(window, monkeypatch, tmp_path) -> None:
    before = window._electrode_mgr.path  # noqa: SLF001
    _choose_file(monkeypatch, "")
    window._on_set_electrode_presets_file()  # noqa: SLF001
    assert window._electrode_mgr.path == before  # noqa: SLF001
    assert app_settings.get_electrode_presets_path() is None


def test_a_folder_is_refused_with_a_message(window, monkeypatch, tmp_path) -> None:
    warned: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning", lambda *a, **k: warned.append(a[2])
    )
    before = window._electrode_mgr.path  # noqa: SLF001
    _choose_file(monkeypatch, tmp_path)  # a folder
    window._on_set_electrode_presets_file()  # noqa: SLF001
    assert warned and "folder" in warned[0]
    assert window._electrode_mgr.path == before  # noqa: SLF001
    assert app_settings.get_electrode_presets_path() is None


def test_a_file_with_a_bad_entry_warns_and_the_rest_still_load(window, monkeypatch, tmp_path) -> None:
    target = tmp_path / "hand_edited.mux16el"
    target.write_text(
        json.dumps(
            {
                "format": "mux16-electrode-presets",
                "version": 1,
                "presets": {
                    "good": {"name": "Good", "electrode_config_mode": "external", "channels": [1]},
                    "bad": {"name": "Bad", "electrode_config_mode": "manual", "channels": [1]},
                },
            }
        ),
        encoding="utf-8",
    )
    warned: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning", lambda *a, **k: warned.append(a[2])
    )
    _choose_file(monkeypatch, target)
    window._on_set_electrode_presets_file()  # noqa: SLF001
    assert warned and "bad" in warned[0] and ".bak" in warned[0]
    assert "good" in _combo_keys(window) and "bad" not in _combo_keys(window)

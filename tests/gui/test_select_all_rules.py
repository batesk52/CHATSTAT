"""Select All after a setup: never a wiring the person did not choose.

Headless. The setup is applied in the main window, Select None / Select All
are pressed, and Start is pressed with the engine stubbed to capture the
config. Two sequences found in review:

* per-electrode setup, Select None, Select All: Start used to be refused
  ("WE channel 15 not allowed") although CH1-CH14 at their own positions
  is a valid table.
* per-electrode setup, a two-channel preset at swapped positions, Select
  All: Start used to accept CH1-CH14 with CH3-CH14 at positions left over
  from the earlier setup.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PyQt6 = pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.data.electrode_presets import ElectrodePresetManager  # noqa: E402
from src.data.models import TechniqueConfig  # noqa: E402
from src.data.presets import Preset  # noqa: E402
from src.gui.controls import ElectrodeWiringPanel  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402


class _Connected:
    is_connected = True

    def disconnect(self) -> None:
        pass


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture()
def window(qapp, tmp_path, monkeypatch):
    import src.gui.main_window as main_window_module

    win = MainWindow()
    win._electrode_mgr = ElectrodePresetManager(str(tmp_path / "e.mux16el"))  # noqa: SLF001
    win._load_electrode_presets_into_ui()  # noqa: SLF001
    win._connection = _Connected()  # noqa: SLF001
    win._meas_panel.set_auto_save(False, str(tmp_path))  # noqa: SLF001
    started: list[TechniqueConfig] = []
    win._engine.start_measurement = lambda conn, cfg: started.append(cfg)  # noqa: SLF001
    win._started = started  # noqa: SLF001
    refused: list[str] = []
    for name in ("warning", "critical"):
        monkeypatch.setattr(
            main_window_module.QMessageBox,
            name,
            lambda *args, **kwargs: refused.append(str(args[2:])),
        )
    win._refused = refused  # noqa: SLF001
    win._tech_panel.set_technique("ca")  # noqa: SLF001
    yield win
    win.close()


def _per_electrode_setup(win) -> None:
    panel = win._electrode_config_panel  # noqa: SLF001
    panel._setup_combo.setCurrentIndex(  # noqa: SLF001
        panel._setup_combo.findData("per_electrode_re_ce")  # noqa: SLF001
    )
    assert win._wiring_panel.selected_pairs() == (  # noqa: SLF001
        list(range(1, 15)),
        list(range(1, 15)),
    )


def test_select_none_then_select_all_after_a_per_electrode_setup(window) -> None:
    _per_electrode_setup(window)
    window._wiring_panel._select_none_btn.click()  # noqa: SLF001
    window._wiring_panel._select_all_btn.click()  # noqa: SLF001

    assert window._wiring_panel.selected_pairs() == (  # noqa: SLF001
        list(range(1, 15)),
        list(range(1, 15)),
    )
    window._on_start_measurement()  # noqa: SLF001
    assert window._refused == []  # noqa: SLF001
    assert len(window._started) == 1  # noqa: SLF001
    config = window._started[0]  # noqa: SLF001
    assert config.channels == list(range(1, 15))
    assert config.re_ce_channels == list(range(1, 15))
    assert config.electrode_config_mode == "manual"


def test_select_all_after_a_swapped_pair_never_reuses_old_positions(window) -> None:
    _per_electrode_setup(window)
    preset = Preset(
        name="swapped",
        technique="ca",
        params={"e_dc": 0.1, "t_run": 1.0, "t_interval": 0.1, "cr": "100u"},
        channels=[1, 2],
        electrode_config_mode="manual",
        re_ce_channels=[2, 1],
    )
    window._preset_mgr.get_preset = lambda key: preset  # noqa: SLF001
    window._on_preset_selected("swapped")  # noqa: SLF001
    assert window._wiring_panel.selected_pairs() == ([1, 2], [2, 1])  # noqa: SLF001
    window._wiring_panel._select_all_btn.click()  # noqa: SLF001

    we, re_ce = window._wiring_panel.selected_pairs()  # noqa: SLF001
    assert we == list(range(1, 15))
    # The rows the preset chose keep their positions; every newly enabled
    # row shows position 15, not a position left from the earlier setup.
    assert re_ce == [2, 1] + [15] * 12
    window._on_start_measurement()  # noqa: SLF001
    assert window._started == []  # noqa: SLF001 - nothing was sent
    assert any("15" in msg and "not allowed" in msg for msg in window._refused)  # noqa: SLF001


def test_select_all_with_no_row_and_no_rule_keeps_the_table(qapp) -> None:
    """No row enabled, and the rows neither share 15/16 nor sit at their own
    positions: Select All enables all 16 rows as they are, a table Start
    always refuses (CH15 and CH16 cannot be working electrodes in a mixed
    table), so nothing nobody chose can be sent.
    """
    panel = ElectrodeWiringPanel()
    panel.set_pairs([2, 3], [5, 6])
    panel.select_none()
    panel.select_all()
    we, re_ce = panel.selected_pairs()
    assert we == list(range(1, 17))
    assert re_ce == [15, 5, 6] + [15] * 13
    with pytest.raises(ValueError, match="not allowed"):
        TechniqueConfig(
            technique="ca",
            params={},
            channels=we,
            re_ce_channels=re_ce,
            electrode_config_mode="manual",
        )


def test_select_all_with_no_row_and_every_row_shared(qapp) -> None:
    panel = ElectrodeWiringPanel()
    panel.set_pairs([4], [15])
    panel.select_none()
    panel.select_all()
    assert panel.selected_pairs() == (list(range(1, 17)), [15] * 16)

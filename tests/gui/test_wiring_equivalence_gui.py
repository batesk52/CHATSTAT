"""The Start button sends the same script as before the single wiring model.

Headless. For every built-in electrode setup, and for measurement presets
that carry a wiring label from an older version, the setup is applied in
the main window, Start is pressed (with the engine stubbed to capture the
config), and the MethodSCRIPT built from that config is compared with the
one the old mode-based GUI built. The old GUI resolved the wiring as:
external -> RE/CE 15 for each ticked channel, on_board -> 16, manual ->
the pairing table; channels in ascending order in every case.
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
from src.gui.main_window import MainWindow  # noqa: E402
from src.techniques.scripts import generate  # noqa: E402

# Same frozen reference as tests/data/test_wiring_equivalence.py.
OLD_BUILTINS = {
    "separate_re_ce": ("external", list(range(1, 17)), []),
    "on_chip_re_ce": ("on_board", list(range(1, 17)), []),
    "per_electrode_re_ce": ("manual", list(range(1, 15)), list(range(1, 15))),
    "die_16_external": ("external", list(range(1, 17)), []),
    "die_16_on_chip": ("on_board", list(range(1, 17)), []),
}


class _Connected:
    is_connected = True


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture()
def window(qapp, tmp_path):
    win = MainWindow()
    win._electrode_mgr = ElectrodePresetManager(str(tmp_path / "e.mux16el"))  # noqa: SLF001
    win._load_electrode_presets_into_ui()  # noqa: SLF001
    win._connection = _Connected()  # noqa: SLF001
    win._meas_panel.set_auto_save(False, str(tmp_path))  # noqa: SLF001
    started: list[TechniqueConfig] = []
    win._engine.start_measurement = lambda conn, cfg: started.append(cfg)  # noqa: SLF001
    win._started = started  # noqa: SLF001
    yield win
    win.close()


def _old_gui_config(mode, channels, re_ce, technique, params) -> TechniqueConfig:
    """What the mode-based GUI built for this wiring."""
    channels = sorted(channels)
    if mode == "external":
        re_ce = [15] * len(channels)
    elif mode == "on_board":
        re_ce = [16] * len(channels)
    return TechniqueConfig(
        technique=technique,
        params=params,
        channels=channels,
        re_ce_channels=list(re_ce),
        electrode_config_mode=mode,
    )


def _script(config: TechniqueConfig) -> list[str]:
    return generate(
        config.technique,
        dict(config.params),
        list(config.channels),
        re_ce_channels=config.re_ce_channels or None,
    )


def _start(win) -> TechniqueConfig:
    win._started.clear()  # noqa: SLF001
    win._on_start_measurement()  # noqa: SLF001
    assert len(win._started) == 1  # noqa: SLF001
    return win._started[0]  # noqa: SLF001


@pytest.mark.parametrize("technique", ["cv", "ca", "eis"])
@pytest.mark.parametrize("key", sorted(OLD_BUILTINS))
def test_start_sends_the_same_script_for_every_built_in(window, key, technique) -> None:
    window._tech_panel.set_technique(technique)  # noqa: SLF001
    panel = window._electrode_config_panel  # noqa: SLF001
    panel._setup_combo.setCurrentIndex(panel._setup_combo.findData(key))  # noqa: SLF001
    new = _start(window)
    old = _old_gui_config(*OLD_BUILTINS[key], technique, dict(new.params))
    assert (new.electrode_config_mode, new.channels, new.re_ce_channels) == (
        old.electrode_config_mode,
        old.channels,
        old.re_ce_channels,
    )
    assert _script(new) == _script(old)


OLD_MEASUREMENT_PRESETS = [
    ("external", [3, 1, 16], []),
    ("on_board", [2, 5], []),
    ("manual", [2, 3], [5, 6]),
]


@pytest.mark.parametrize("mode, channels, re_ce", OLD_MEASUREMENT_PRESETS)
def test_an_old_measurement_preset_still_runs_the_same_wiring(
    window, mode, channels, re_ce
) -> None:
    preset = Preset(
        name="old",
        technique="ca",
        params={"e_dc": 0.1, "t_run": 1.0, "t_interval": 0.1, "cr": "100u"},
        channels=list(channels),
        electrode_config_mode=mode,
        re_ce_channels=list(re_ce),
    )
    window._preset_mgr.get_preset = lambda key: preset  # noqa: SLF001
    window._on_preset_selected("old")  # noqa: SLF001
    new = _start(window)
    old = _old_gui_config(mode, channels, re_ce, "ca", dict(new.params))
    assert (new.electrode_config_mode, new.channels, new.re_ce_channels) == (
        old.electrode_config_mode,
        old.channels,
        old.re_ce_channels,
    )
    assert _script(new) == _script(old)

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


def test_select_all_after_a_shared_wiring_follows_that_wiring(
    window, monkeypatch
) -> None:
    """Regression: per-electrode setup, then a 2-channel shared-15 wiring,
    then Select All used to give [15, 15, 3..14, 15, 15] and Start was
    refused. Every row must take the shared position.
    """
    import src.gui.main_window as main_window_module

    refused: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda *args, **kwargs: refused.append(str(args[2:])),
    )
    window._tech_panel.set_technique("ca")  # noqa: SLF001
    panel = window._electrode_config_panel  # noqa: SLF001
    panel._setup_combo.setCurrentIndex(  # noqa: SLF001
        panel._setup_combo.findData("per_electrode_re_ce")  # noqa: SLF001
    )
    preset = Preset(
        name="two at 15",
        technique="ca",
        params={"e_dc": 0.1, "t_run": 1.0, "t_interval": 0.1, "cr": "100u"},
        channels=[1, 2],
        electrode_config_mode="external",
        re_ce_channels=[],
    )
    window._preset_mgr.get_preset = lambda key: preset  # noqa: SLF001
    window._on_preset_selected("two at 15")  # noqa: SLF001
    window._wiring_panel._select_all_btn.click()  # noqa: SLF001

    new = _start(window)
    assert refused == []
    assert new.channels == list(range(1, 17))
    assert new.re_ce_channels == [15] * 16
    assert new.electrode_config_mode == "external"
    old = _old_gui_config("external", range(1, 17), [], "ca", dict(new.params))
    assert _script(new) == _script(old)


def _apply_two_channel_preset(window, monkeypatch, channels, re_ce):
    """Separate RE/CE setup, then a 2-channel preset, then Select All."""
    import src.gui.main_window as main_window_module

    refused: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda *args, **kwargs: refused.append(str(args[2:])),
    )
    window._tech_panel.set_technique("ca")  # noqa: SLF001
    panel = window._electrode_config_panel  # noqa: SLF001
    panel._setup_combo.setCurrentIndex(  # noqa: SLF001
        panel._setup_combo.findData("separate_re_ce")  # noqa: SLF001
    )
    preset = Preset(
        name="two",
        technique="ca",
        params={"e_dc": 0.1, "t_run": 1.0, "t_interval": 0.1, "cr": "100u"},
        channels=list(channels),
        electrode_config_mode="manual",
        re_ce_channels=list(re_ce),
    )
    window._preset_mgr.get_preset = lambda key: preset  # noqa: SLF001
    window._on_preset_selected("two")  # noqa: SLF001
    window._wiring_panel._select_all_btn.click()  # noqa: SLF001
    return refused


def test_select_all_after_an_own_position_table_extends_it(window, monkeypatch) -> None:
    """Mirror of the regression above: separate RE/CE (every row at 15), then
    a per-electrode preset on [3, 4] at [3, 4], then Select All used to give
    [15, 15, 3, 4, 15, ...] and Start was refused. Select All now extends the
    own-position pairing to CH1-CH14, the table Start accepts.
    """
    refused = _apply_two_channel_preset(window, monkeypatch, [3, 4], [3, 4])
    new = _start(window)
    assert refused == []
    assert new.channels == list(range(1, 15))
    assert new.re_ce_channels == list(range(1, 15))
    assert new.electrode_config_mode == "manual"
    old = _old_gui_config("manual", range(1, 15), range(1, 15), "ca", dict(new.params))
    assert _script(new) == _script(old)


def test_select_all_after_a_shared_on_chip_position_gives_a_clear_refusal(
    window, monkeypatch
) -> None:
    """A shared position inside CH1-CH14 ([5, 5]) is a per-electrode table:
    Select All enables CH1-CH14 only (never CH15/16 at RE/CE 5) and does
    not spread position 5 to the other rows. The rows left at 15 make Start
    refuse with a message naming position 15; nothing is sent.
    """
    import src.gui.main_window as main_window_module

    refused = _apply_two_channel_preset(window, monkeypatch, [1, 2], [5, 5])
    we, re_ce = window._wiring_panel.selected_pairs()  # noqa: SLF001
    assert we == list(range(1, 15))
    assert re_ce[:2] == [5, 5] and set(re_ce[2:]) == {15}
    window._started.clear()  # noqa: SLF001
    critical: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "critical",
        lambda *args, **kwargs: critical.append(str(args[2:])),
    )
    window._on_start_measurement()  # noqa: SLF001
    assert window._started == []  # noqa: SLF001
    assert any("15" in msg and "not allowed" in msg for msg in refused + critical)


def test_an_unsorted_setup_gives_the_same_script_in_the_gui_and_the_agent(
    window, monkeypatch
) -> None:
    """A hand-edited setup with channels [7, 2, 1] at RE/CE [5, 6, 7]: Start
    and the agent's run_ca by name build the same configuration and script.
    """
    import asyncio

    import src.agent.engine_adapter as engine_adapter_module
    from src.data.electrode_presets import ElectrodePreset

    window._electrode_mgr.add_preset(  # noqa: SLF001
        "unsorted",
        ElectrodePreset(name="Unsorted", channels=[7, 2, 1], re_ce_channels=[5, 6, 7]),
    )
    window._load_electrode_presets_into_ui()  # noqa: SLF001
    window._tech_panel.set_technique("ca")  # noqa: SLF001
    panel = window._electrode_config_panel  # noqa: SLF001
    panel._setup_combo.setCurrentIndex(panel._setup_combo.findData("unsorted"))  # noqa: SLF001
    gui = _start(window)

    seen: list[TechniqueConfig] = []
    real = engine_adapter_module.build_technique_config

    def spy(technique, args=None):
        seen.append(real(technique, args))
        raise ValueError("captured")

    monkeypatch.setattr(engine_adapter_module, "build_technique_config", spy)
    asyncio.run(
        window._agent_registry.get("run_ca")(  # noqa: SLF001
            {"electrode_setup": "unsorted", **{k: v for k, v in gui.params.items()}}
        )
    )
    assert len(seen) == 1
    agent = seen[0]
    assert (gui.channels, gui.re_ce_channels) == ([1, 2, 7], [7, 6, 5])
    assert (agent.channels, agent.re_ce_channels, agent.electrode_config_mode) == (
        gui.channels,
        gui.re_ce_channels,
        gui.electrode_config_mode,
    )
    assert _script(agent) == _script(gui)

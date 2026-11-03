"""Electrode setups drive a run on the mock engine, in every mode.

The setup's wiring goes through the same path as a person's click or an
agent's request: ``TechniqueConfig`` and the engine adapter. The mock engine
stands in for the instrument, so these runs need no hardware and no API key.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio  # noqa: E402
import threading  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.agent import bridge  # noqa: E402
from src.agent.engine_adapter import EngineAdapter  # noqa: E402
from src.agent.mock_engine import MockConnection, MockMeasurementEngine  # noqa: E402
from src.data.electrode_presets import ElectrodePreset, ElectrodePresetManager  # noqa: E402

_APP = None


def _adapter():
    global _APP
    _APP = QCoreApplication.instance() or QApplication([])
    bridge.install()
    engine = MockMeasurementEngine()
    connection = MockConnection()
    connection.connect("MOCK1")
    return engine, EngineAdapter(engine, connection)


def _run(factory):
    app = QCoreApplication.instance()
    results: list = []

    def worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            results.append(loop.run_until_complete(factory()))
        finally:
            loop.close()
            QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(app.quit)
    guard.start(30000)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    app.exec()
    thread.join(5.0)
    guard.stop()
    assert not thread.is_alive(), "the run did not return"
    return results[0]


def _run_setup(adapter, preset: ElectrodePreset) -> dict:
    mode, channels, re_ce = preset.wiring()
    return _run(
        lambda: adapter.run_ca(
            {"e_dc": 0.1, "t_run": 1.0},
            channels=channels,
            electrode_config_mode=mode,
            re_ce_channels=re_ce,
        )
    )


@pytest.mark.parametrize(
    "key, mode",
    [
        ("separate_re_ce", "external"),
        ("on_chip_re_ce", "on_board"),
        ("per_electrode_re_ce", "manual"),
        ("die_16_external", "external"),
        ("die_16_on_chip", "on_board"),
    ],
)
def test_each_built_in_setup_runs_on_the_mock_engine(tmp_path, key, mode) -> None:
    preset = ElectrodePresetManager(str(tmp_path / "e.mux16el")).get_preset(key)
    assert preset.electrode_config_mode == mode
    engine, adapter = _adapter()
    result = _run_setup(adapter, preset)
    assert result["ok"] is True, result
    assert sorted(result["measured_channels"]) == sorted(preset.channels)
    assert result["num_points"] > 0
    assert not engine.isRunning()


def test_a_saved_mode_c_pairing_runs_as_saved(tmp_path) -> None:
    path = str(tmp_path / "e.mux16el")
    ElectrodePresetManager(path).add_preset(
        "pairs", ElectrodePreset("Pairs", "manual", [2, 3], [5, 6])
    )
    preset = ElectrodePresetManager(path).get_preset("pairs")  # a fresh load
    engine, adapter = _adapter()
    seen = {}
    original = engine.start_measurement

    def spy(connection, config):
        seen["config"] = config
        return original(connection, config)

    engine.start_measurement = spy
    result = _run_setup(adapter, preset)
    assert result["ok"] is True
    config = seen["config"]
    assert config.electrode_config_mode == "manual"
    assert config.channels == [2, 3] and config.re_ce_channels == [5, 6]

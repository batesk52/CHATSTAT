"""Where the electrode setups file lives, and editing it by hand.

The location is a setting (``electrode_presets_path`` in
``app_settings.json``); the file is plain JSON a person can edit. No Qt, no
instrument. The conftest points the default settings file at a temp file, so
nothing here touches the real settings folder.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from src.data import app_settings
from src.data import electrode_presets as ep
from src.data.electrode_presets import (
    DEFAULT_ELECTRODE_PRESETS_FILE,
    ELECTRODE_PRESET_FILE_FORMAT,
    ElectrodePreset,
    ElectrodePresetManager,
    read_electrode_preset_file,
    resolve_electrode_presets_path,
)
from src.data.paths import repo_root

KEY = "electrode_presets_path"


def _settings(tmp_path) -> str:
    return str(tmp_path / "settings.json")


# ---- the setting ------------------------------------------------------------


def test_the_default_file_is_used_when_nothing_is_set(tmp_path) -> None:
    settings = _settings(tmp_path)
    assert app_settings.get_electrode_presets_path(settings) is None
    assert resolve_electrode_presets_path(settings) == DEFAULT_ELECTRODE_PRESETS_FILE
    assert DEFAULT_ELECTRODE_PRESETS_FILE.endswith("electrode_presets.mux16el")
    assert ElectrodePresetManager(settings_path=settings).path == DEFAULT_ELECTRODE_PRESETS_FILE


def test_the_setting_moves_the_file(tmp_path) -> None:
    settings = _settings(tmp_path)
    target = str(tmp_path / "lab" / "shared" / "setups.mux16el")
    app_settings.set_electrode_presets_path(target, settings)
    assert app_settings.get_electrode_presets_path(settings) == target
    mgr = ElectrodePresetManager(settings_path=settings)
    assert mgr.path == os.path.normpath(target)

    # Nothing is written until a setup is saved; then the file (and its
    # folders) appear at the chosen place, and not at the default.
    assert not os.path.exists(target)
    mgr.add_preset("mine", ElectrodePreset("Mine", "external", [1, 2]))
    assert os.path.isfile(target)
    assert ElectrodePresetManager(settings_path=settings).get_preset("mine").channels == [1, 2]


def test_an_explicit_path_beats_the_setting(tmp_path) -> None:
    settings = _settings(tmp_path)
    app_settings.set_electrode_presets_path(str(tmp_path / "from_setting.mux16el"), settings)
    explicit = str(tmp_path / "explicit.mux16el")
    assert ElectrodePresetManager(explicit, settings_path=settings).path == explicit


def test_a_tilde_means_the_home_folder(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    settings = _settings(tmp_path)
    app_settings.set_electrode_presets_path("~/lab/setups.mux16el", settings)
    assert resolve_electrode_presets_path(settings) == os.path.normpath(
        str(tmp_path / "lab" / "setups.mux16el")
    )


def test_a_relative_path_is_read_from_the_settings_folder(tmp_path, monkeypatch) -> None:
    # Not from the folder the program happened to start in.
    monkeypatch.setattr(ep, "USER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)
    app_settings.set_electrode_presets_path("my_setups.mux16el", settings)
    assert resolve_electrode_presets_path(settings) == os.path.normpath(
        str(tmp_path / "data" / "my_setups.mux16el")
    )


def test_the_setting_is_cleared_to_go_back_to_the_default(tmp_path) -> None:
    settings = _settings(tmp_path)
    app_settings.set_electrode_presets_path("x.mux16el", settings)
    for empty in (None, "", "   "):
        app_settings.set_electrode_presets_path("x.mux16el", settings)
        app_settings.set_electrode_presets_path(empty, settings)
        assert app_settings.get_electrode_presets_path(settings) is None
        assert resolve_electrode_presets_path(settings) == DEFAULT_ELECTRODE_PRESETS_FILE


def test_other_settings_survive_a_change_of_location(tmp_path) -> None:
    settings = _settings(tmp_path)
    app_settings.set_agent_model("some-model", settings)
    app_settings.set_electrode_presets_path("x.mux16el", settings)
    assert app_settings.get_agent_model(settings) == "some-model"
    app_settings.set_electrode_presets_path(None, settings)
    assert app_settings.get_agent_model(settings) == "some-model"


@pytest.mark.parametrize("bad", [5, True, ["a"], "", "   "])
def test_a_bad_stored_value_falls_back_to_the_default(tmp_path, bad, caplog) -> None:
    settings = _settings(tmp_path)
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({KEY: bad}, f)
    assert app_settings.get_electrode_presets_path(settings) is None
    assert resolve_electrode_presets_path(settings) == DEFAULT_ELECTRODE_PRESETS_FILE
    assert "must be a file path" in caplog.text


def test_a_folder_is_not_a_setups_file(tmp_path, caplog) -> None:
    settings = _settings(tmp_path)
    folder = tmp_path / "a_folder"
    folder.mkdir()
    with pytest.raises(ValueError, match="folder"):
        app_settings.set_electrode_presets_path(str(folder), settings)
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({KEY: str(folder)}, f)  # put there by hand
    assert app_settings.get_electrode_presets_path(settings) is None
    assert "is a folder" in caplog.text


def test_the_setter_refuses_what_is_not_text(tmp_path) -> None:
    with pytest.raises(ValueError, match="text"):
        app_settings.set_electrode_presets_path(5, _settings(tmp_path))  # type: ignore[arg-type]


# ---- the file is plain text a person can edit ----------------------------------

HAND_WRITTEN = {
    "format": ELECTRODE_PRESET_FILE_FORMAT,
    "version": 1,
    "presets": {
        "bench_external": {
            "name": "Bench, external RE + CE",
            "electrode_config_mode": "external",
            "channels": [1, 2, 3, 4],
            "description": "CH1 to CH4 against the external reference and counter.",
        },
        "bench_on_board": {
            "name": "Bench, on-board RE/CE",
            "electrode_config_mode": "on_board",
            "channels": [5, 6],
        },
        "bench_pairs": {
            "name": "Bench, paired",
            "electrode_config_mode": "manual",
            "channels": [2, 3],
            "re_ce_channels": [5, 6],
        },
    },
}


def _write(path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def test_a_hand_written_file_loads_in_every_mode(tmp_path) -> None:
    path = str(tmp_path / "setups.mux16el")
    _write(path, HAND_WRITTEN)
    mgr = ElectrodePresetManager(path)
    assert mgr.load_problems == []
    assert mgr.get_preset("bench_external").wiring() == ("external", [1, 2, 3, 4], [15] * 4)
    assert mgr.get_preset("bench_on_board").wiring() == ("on_board", [5, 6], [16, 16])
    assert mgr.get_preset("bench_pairs").wiring() == ("manual", [2, 3], [5, 6])
    assert mgr.get_preset("separate_re_ce") is not None  # built-ins stay


def test_the_saved_file_is_readable_and_editable_text(tmp_path) -> None:
    path = str(tmp_path / "setups.mux16el")
    ElectrodePresetManager(path).add_preset(
        "mine", ElectrodePreset("Mine", "manual", [2, 3], [5, 6], description="d")
    )
    text = open(path, encoding="utf-8").read()
    assert text.count("\n") > 8  # indented, one field per line
    data = json.loads(text)
    assert data["presets"]["mine"]["channels"] == [2, 3]
    # Edit it the way a person would, and it loads again.
    data["presets"]["mine"]["channels"] = [2, 3, 4]
    data["presets"]["mine"]["re_ce_channels"] = [5, 6, 7]
    _write(path, data)
    assert ElectrodePresetManager(path).get_preset("mine").re_ce_channels == [5, 6, 7]


def test_a_bad_hand_edit_is_reported_and_the_file_is_kept(tmp_path) -> None:
    path = str(tmp_path / "setups.mux16el")
    data = json.loads(json.dumps(HAND_WRITTEN))
    data["presets"]["bench_pairs"]["re_ce_channels"] = [5]  # one short
    _write(path, data)
    original = open(path, encoding="utf-8").read()
    mgr = ElectrodePresetManager(path)
    assert mgr.get_preset("bench_pairs") is None
    assert mgr.get_preset("bench_external") is not None  # the rest still load
    assert len(mgr.load_problems) == 1
    assert "bench_pairs" in mgr.load_problems[0] and "one RE/CE" in mgr.load_problems[0]
    mgr.add_preset("mine", ElectrodePreset("Mine", "external", [1]))
    assert open(path + ".bak", encoding="utf-8").read() == original


def test_a_file_that_is_not_json_is_reported(tmp_path) -> None:
    path = str(tmp_path / "setups.mux16el")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ oops")
    mgr = ElectrodePresetManager(path)
    assert len(mgr.load_problems) == 1 and "could not be read" in mgr.load_problems[0]
    assert mgr.get_preset("separate_re_ce") is not None


def test_reload_picks_up_an_edit_made_while_running(tmp_path) -> None:
    path = str(tmp_path / "setups.mux16el")
    mgr = ElectrodePresetManager(path)
    assert mgr.get_preset("bench_external") is None
    _write(path, HAND_WRITTEN)
    mgr.reload()
    assert mgr.get_preset("bench_external") is not None
    assert mgr.load_problems == []


def test_use_file_switches_to_another_file(tmp_path) -> None:
    first, second = str(tmp_path / "a.mux16el"), str(tmp_path / "b.mux16el")
    _write(first, HAND_WRITTEN)
    mgr = ElectrodePresetManager(first)
    assert mgr.get_preset("bench_pairs") is not None
    mgr.use_file(second)  # does not exist yet: built-ins only
    assert mgr.path == second and mgr.get_preset("bench_pairs") is None
    mgr.add_preset("mine", ElectrodePreset("Mine", "external", [3]))
    assert os.path.isfile(second)
    assert read_electrode_preset_file(first)["bench_pairs"].channels == [2, 3]  # untouched


# ---- the README example stays true ------------------------------------------------


def _readme_json_blocks() -> list:
    readme = open(os.path.join(repo_root(), "README.md"), encoding="utf-8").read()
    section = readme[readme.index("## Electrode setups"):]
    blocks = re.findall(r"```json\s*\n(.*?)\n```", section, re.S)
    assert blocks, "README has no JSON example under 'Electrode setups'"
    return [json.loads(b) for b in blocks]


def _readme_example() -> dict:
    (example,) = [b for b in _readme_json_blocks() if "presets" in b]
    return example


def test_the_readme_setting_example_is_a_valid_setting(tmp_path) -> None:
    (snippet,) = [b for b in _readme_json_blocks() if KEY in b]
    settings = _settings(tmp_path)
    app_settings.set_electrode_presets_path(snippet[KEY], settings)
    assert app_settings.get_electrode_presets_path(settings) == snippet[KEY]


def test_the_readme_example_loads(tmp_path) -> None:
    example = _readme_example()
    path = str(tmp_path / "from_readme.mux16el")
    _write(path, example)
    mgr = ElectrodePresetManager(path)
    assert mgr.load_problems == []
    modes = {p.electrode_config_mode for k, p in mgr.get_all().items() if k in example["presets"]}
    assert modes == {"external", "on_board", "manual"}  # the example shows every mode
    assert set(example["presets"]) <= set(mgr.list_presets())


def test_the_readme_names_the_setting_and_the_default_file() -> None:
    readme = open(os.path.join(repo_root(), "README.md"), encoding="utf-8").read()
    assert "electrode_presets_path" in readme
    assert "electrode_presets.mux16el" in readme

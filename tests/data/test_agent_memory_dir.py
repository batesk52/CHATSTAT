"""The memory notes folder is a setting in app_settings.json.

Headless: every test passes an explicit settings file under ``tmp_path``.
"""

from __future__ import annotations

import json
import os

import pytest

from src.data import app_settings
from src.data.app_settings import get_agent_memory_dir, set_agent_memory_dir


def _settings(tmp_path, **content) -> str:
    path = tmp_path / "settings" / "app_settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


def test_the_default_is_a_memories_folder_beside_the_settings_file(tmp_path) -> None:
    path = _settings(tmp_path)
    assert get_agent_memory_dir(path) == str(tmp_path / "settings" / "memories")


def test_the_default_settings_file_gives_a_folder_beside_it() -> None:
    # tests/conftest.py points the default settings file at a temp file, so
    # no test can reach the person's own memory folder by accident.
    expected = os.path.join(
        os.path.dirname(app_settings._DEFAULT_SETTINGS_FILE), "memories"  # noqa: SLF001
    )
    assert get_agent_memory_dir() == expected
    assert ".emstat_pico_mux16" not in get_agent_memory_dir()


def test_a_chosen_folder_is_stored_and_returned(tmp_path) -> None:
    path = _settings(tmp_path)
    shared = tmp_path / "shared drive" / "lab memories"
    set_agent_memory_dir(f"  {shared}  ", path)
    assert json.loads(open(path, encoding="utf-8").read())["agent_memory_dir"] == str(
        shared
    )
    assert get_agent_memory_dir(path) == str(shared)
    assert not shared.exists()  # the first saved note creates it


def test_other_settings_are_kept(tmp_path) -> None:
    path = _settings(tmp_path, agent_model="claude-sonnet-5-5")
    set_agent_memory_dir(str(tmp_path / "m"), path)
    assert json.loads(open(path, encoding="utf-8").read())["agent_model"] == (
        "claude-sonnet-5-5"
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_clearing_goes_back_to_the_default(tmp_path, value) -> None:
    path = _settings(tmp_path, agent_memory_dir=str(tmp_path / "m"))
    set_agent_memory_dir(value, path)
    assert "agent_memory_dir" not in json.loads(open(path, encoding="utf-8").read())
    assert get_agent_memory_dir(path) == str(tmp_path / "settings" / "memories")


def test_a_relative_folder_is_read_from_the_settings_folder(tmp_path) -> None:
    path = _settings(tmp_path, agent_memory_dir="notes/shared")
    assert get_agent_memory_dir(path) == str(tmp_path / "settings" / "notes" / "shared")


def test_a_tilde_is_the_home_folder(tmp_path) -> None:
    path = _settings(tmp_path, agent_memory_dir="~/lab_memories")
    assert get_agent_memory_dir(path) == os.path.normpath(
        os.path.join(os.path.expanduser("~"), "lab_memories")
    )


@pytest.mark.parametrize("value", [5, "", "   ", ["a"], True])
def test_a_stored_value_that_is_not_a_folder_path_falls_back(tmp_path, value) -> None:
    path = _settings(tmp_path, agent_memory_dir=value)
    assert get_agent_memory_dir(path) == str(tmp_path / "settings" / "memories")


def test_a_stored_value_that_names_a_file_falls_back(tmp_path) -> None:
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")
    path = _settings(tmp_path, agent_memory_dir=str(a_file))
    assert get_agent_memory_dir(path) == str(tmp_path / "settings" / "memories")


def test_setting_a_file_or_a_non_text_value_is_refused(tmp_path) -> None:
    path = _settings(tmp_path)
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="choose a folder"):
        set_agent_memory_dir(str(a_file), path)
    with pytest.raises(ValueError, match="must be text"):
        set_agent_memory_dir(5, path)  # type: ignore[arg-type]
    assert json.loads(open(path, encoding="utf-8").read()) == {}

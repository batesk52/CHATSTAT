"""Shared fixtures for the agent tests."""

from __future__ import annotations

import pytest

from src.data import app_settings


@pytest.fixture(autouse=True)
def _isolated_app_settings(tmp_path, monkeypatch):
    """Point the default settings file at an empty temp file.

    The agent reads its request limits (and the API key and model) from the
    person's ``app_settings.json``. A test must not depend on what the
    person has put there, so the default path is an empty file per test.
    Tests that pass ``path=`` explicitly are not affected.
    """
    monkeypatch.setattr(
        app_settings,
        "_DEFAULT_SETTINGS_FILE",
        str(tmp_path / "app_settings_default.json"),
    )

"""Shared fixtures for the test suite."""

from __future__ import annotations

import pytest

from src.data import app_settings


@pytest.fixture(autouse=True)
def _isolated_app_settings(tmp_path, monkeypatch):
    """Point the default settings file at an empty temp file.

    The program reads the agent's request limits, the API key and model, and
    the electrode setups file location from the person's
    ``app_settings.json``, and a few GUI actions write it. A test must not
    depend on what the person has put there, or change it, so the default
    path is an empty file per test. Tests that pass ``path=`` explicitly are
    not affected.
    """
    monkeypatch.setattr(
        app_settings,
        "_DEFAULT_SETTINGS_FILE",
        str(tmp_path / "app_settings_default.json"),
    )

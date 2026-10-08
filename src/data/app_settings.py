"""Lightweight persistent application settings (sequencer feature — Phase 1).

Remembers cross-launch pointers — currently the last-used preset file —
so the GUI can auto-load it on startup.

**Choice of backend:** a tiny JSON file in the per-user data directory
(``~/.emstat_pico_mux16/app_settings.json``) rather than ``QSettings``.
The JSON store imports and round-trips with NO running ``QApplication``,
so the get/set helpers and their tests are fully headless; it also lives
beside the externalized preset store, keeping all user data in one place.
The store path is overridable (``path=``) so tests can point at a temp
file and never touch the real user store.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from src.data.paths import USER_DATA_DIR, default_export_dir

__all__ = [
    "default_export_dir",
    "get_agent_api_key",
    "get_agent_model",
    "get_export_dir",
    "get_last_preset_file",
    "set_agent_api_key",
    "set_agent_model",
    "set_export_dir",
    "set_last_preset_file",
]

logger = logging.getLogger(__name__)

# Per-user data directory (shared with the preset store via
# src/data/paths.py — single source of truth).
_USER_DATA_DIR = USER_DATA_DIR
_DEFAULT_SETTINGS_FILE = os.path.join(
    _USER_DATA_DIR, "app_settings.json"
)

_LAST_PRESET_FILE_KEY = "last_preset_file"
_EXPORT_DIR_KEY = "export_dir"
_AGENT_API_KEY_KEY = "agent_api_key"
_AGENT_MODEL_KEY = "agent_model"


def _resolve_path(path: Optional[str]) -> str:
    """Return the settings-file path, defaulting to the user store.

    Args:
        path: Explicit path override, or ``None`` for the default.

    Returns:
        The resolved settings file path.
    """
    return path if path is not None else _DEFAULT_SETTINGS_FILE


def _read(path: Optional[str] = None) -> dict[str, object]:
    """Read the settings dict, tolerating a missing/corrupt file.

    Args:
        path: Optional settings-file path override.

    Returns:
        The parsed settings mapping, or an empty dict on any failure.
    """
    resolved = _resolve_path(path)
    if not os.path.isfile(resolved):
        return {}
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning(
            "Settings file %s is not an object; ignoring", resolved
        )
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "Failed to read settings from %s: %s", resolved, e
        )
        return {}


def _write(data: dict[str, object], path: Optional[str] = None) -> None:
    """Persist the settings dict, creating the directory as needed.

    Args:
        data: Settings mapping to write.
        path: Optional settings-file path override.
    """
    resolved = _resolve_path(path)
    directory = os.path.dirname(resolved)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(resolved, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_last_preset_file(
    path: Optional[str] = None,
) -> Optional[str]:
    """Return the remembered last-used preset file path.

    Args:
        path: Optional settings-file path override (for tests).

    Returns:
        The stored preset-file path, or ``None`` if unset.
    """
    value = _read(path).get(_LAST_PRESET_FILE_KEY)
    return value if isinstance(value, str) and value else None


def set_last_preset_file(
    preset_file: Optional[str], path: Optional[str] = None
) -> None:
    """Store (or clear) the last-used preset file path.

    Args:
        preset_file: Preset-file path to remember, or ``None`` to clear.
        path: Optional settings-file path override (for tests).
    """
    data = _read(path)
    if preset_file:
        data[_LAST_PRESET_FILE_KEY] = preset_file
    else:
        data.pop(_LAST_PRESET_FILE_KEY, None)
    _write(data, path)


def get_agent_api_key(path: Optional[str] = None) -> Optional[str]:
    """Return the stored Anthropic API key, or ``None`` if unset.

    ``None`` means "fall back to the ``ANTHROPIC_API_KEY`` environment
    variable" (the Anthropic SDK reads it automatically when no key is
    passed).

    Args:
        path: Optional settings-file path override (for tests).
    """
    value = _read(path).get(_AGENT_API_KEY_KEY)
    return value if isinstance(value, str) and value else None


def set_agent_api_key(
    key: Optional[str], path: Optional[str] = None
) -> None:
    """Store (or clear) the agent's Anthropic API key.

    The key is persisted in the per-user settings file (plain JSON in
    the user's home directory, same trust level as a ``.env`` file).
    Passing ``None``/"" clears it so the environment variable is used.

    Args:
        key: API key to remember, or ``None``/"" to clear.
        path: Optional settings-file path override (for tests).
    """
    data = _read(path)
    if key:
        data[_AGENT_API_KEY_KEY] = key
    else:
        data.pop(_AGENT_API_KEY_KEY, None)
    _write(data, path)


def get_agent_model(path: Optional[str] = None) -> Optional[str]:
    """Return the stored agent model id, or ``None`` if unset.

    Args:
        path: Optional settings-file path override (for tests).
    """
    value = _read(path).get(_AGENT_MODEL_KEY)
    return value if isinstance(value, str) and value else None


def set_agent_model(
    model: Optional[str], path: Optional[str] = None
) -> None:
    """Store (or clear) the agent model id.

    Args:
        model: Model id to remember, or ``None``/"" to clear.
        path: Optional settings-file path override (for tests).
    """
    data = _read(path)
    if model:
        data[_AGENT_MODEL_KEY] = model
    else:
        data.pop(_AGENT_MODEL_KEY, None)
    _write(data, path)


def get_export_dir(path: Optional[str] = None) -> str:
    """Return the configured export directory, or the build default.

    Falls back to :func:`default_export_dir` when the user has not set
    an override, so callers always receive a usable path.

    Args:
        path: Optional settings-file path override (for tests).

    Returns:
        The export directory to write results into.
    """
    value = _read(path).get(_EXPORT_DIR_KEY)
    if isinstance(value, str) and value:
        return value
    return default_export_dir()


def set_export_dir(
    export_dir: Optional[str], path: Optional[str] = None
) -> None:
    """Store (or clear) the export directory override.

    Passing ``None`` or an empty string clears the override so
    :func:`get_export_dir` reverts to :func:`default_export_dir`.

    Args:
        export_dir: Directory to remember, or ``None``/"" to clear.
        path: Optional settings-file path override (for tests).
    """
    data = _read(path)
    if export_dir:
        data[_EXPORT_DIR_KEY] = export_dir
    else:
        data.pop(_EXPORT_DIR_KEY, None)
    _write(data, path)

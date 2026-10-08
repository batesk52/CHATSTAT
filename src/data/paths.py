"""Shared filesystem locations for user data and defaults.

Single source of truth for where per-user data lives and how the
install/repo root is derived, so the preset store, app settings, and any
future persisted artifact (sequences, logs, calibration) cannot drift
into different directories. Frozen-executable awareness lives here and
only here.
"""

from __future__ import annotations

import os
import sys

# Per-user data directory shared by the preset store and app settings.
USER_DATA_DIR = os.path.join(
    os.path.expanduser("~"), ".emstat_pico_mux16"
)


def is_frozen() -> bool:
    """Return True when running as a bundled executable (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def repo_root() -> str:
    """Return the source-checkout root (this file lives at src/data/)."""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def default_export_dir() -> str:
    """Return the built-in default export directory for this build.

    * **Frozen executable:** there is no source repo and the install dir
      may be read-only, so default to a visible, user-writable folder
      under the home directory (``~/EmStatPicoMUX16/exports``).
    * **Source/dev checkout:** the in-repo ``exports/`` folder, so an
      agent or developer running from the tree keeps the familiar
      location.

    This is only the *default* — ``app_settings.get_export_dir`` returns
    the user's configured override when one is set.
    """
    if is_frozen():
        return os.path.join(
            os.path.expanduser("~"), "EmStatPicoMUX16", "exports"
        )
    return os.path.join(repo_root(), "exports")

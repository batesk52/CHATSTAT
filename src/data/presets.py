"""Measurement preset management.

Provides :class:`Preset` dataclass and :class:`PresetManager` for
saving, loading, and managing named measurement configurations.

Preset files are user data and live OUTSIDE the repository (sequencer feature —
preset sequencer, Phase 1).  The default store is a versioned file in a
per-user data directory; the in-repo ``presets/presets.json`` is treated
as a one-time migration source only.  ``_BUILTIN_PRESETS`` stays in code
as seed defaults and is always present in memory.

On-disk format is JSON under a versioned wrapper::

    {"format": "mux16-presets", "version": 1,
     "presets": {<name>: <asdict(Preset)>}}

The loader also accepts the legacy bare ``{<name>: {...}}`` map (detected
by the absence of a ``"format"`` key) so files written before the wrapper
was introduced still load cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from src.data.paths import USER_DATA_DIR

logger = logging.getLogger(__name__)

# On-disk wrapper identity.
PRESET_FILE_FORMAT = "mux16-presets"
PRESET_FILE_VERSION = 1

# Per-user data directory for the externalized preset store.  Kept out of
# the repo so presets are user data, not code; the location is shared
# with app settings via src/data/paths.py (single source of truth).
_USER_DATA_DIR = USER_DATA_DIR
_DEFAULT_PRESETS_FILE = os.path.join(_USER_DATA_DIR, "presets.mux16")

# Legacy in-repo store, kept only as a one-time migration source.
_LEGACY_PRESETS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))),
    "presets",
    "presets.json",
)


@dataclass
class Preset:
    """A named measurement configuration.

    Attributes:
        name: Unique identifier / display name.
        technique: Lowercase technique identifier.
        params: Technique-specific parameter dict.
        channels: 1-indexed MUX channel list.
        auto_save: Whether auto-save should be enabled.
        description: Human-readable description.
        electrode_config_mode: Wiring mode (``"external"`` /
            ``"on_board"`` / ``"manual"``).  Defaults to
            ``"external"`` for backward compatibility with presets
            written before batch 2 of WS-electrode-config-modes.
        re_ce_channels: Per-WE RE/CE channel list.  When empty (the
            common case for external / on_board presets),
            :class:`TechniqueConfig.__post_init__` populates the
            default at run time.  Manual-mode presets MUST supply
            a list that matches ``channels`` length and stays within
            CH1-CH14.
    """

    name: str
    technique: str
    params: dict[str, Any] = field(default_factory=dict)
    channels: list[int] = field(default_factory=list)
    auto_save: bool = False
    description: str = ""
    electrode_config_mode: str = "external"
    re_ce_channels: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------

# Presets here are injected at load time and protected from deletion via
# the GUI.  A minimal generic set ships in code so a fresh checkout or a
# packaged executable has usable starting points (the dropdown and the
# sequencer's "Add step" were otherwise empty on a machine with no
# migrated store).  Parameter values mirror the technique defaults in
# ``src/techniques/scripts.py``; user presets with the same key override.
_BUILTIN_PRESETS: dict[str, Preset] = {
    "default_cv": Preset(
        name="Default CV (CH1)",
        technique="cv",
        params={
            "t_eq": 2.0,
            "e_begin": -0.5,
            "e_vertex1": 0.5,
            "e_vertex2": -0.5,
            "e_step": 0.01,
            "scan_rate": 0.1,
            "n_scans": 1,
            "cr": "100u",
        },
        channels=[1],
        description="Built-in generic CV starting point.",
    ),
    "default_eis": Preset(
        name="Default EIS (CH1)",
        technique="eis",
        params={
            "t_eq": 2.0,
            "e_dc": 0.0,
            "e_ac": 0.01,
            "freq_start": 50000.0,
            "freq_end": 10.0,
            "n_freq": 31,
            "cr": "100u",
        },
        channels=[1],
        description="Built-in generic EIS starting point.",
    ),
    "default_ca": Preset(
        name="Default CA (CH1)",
        technique="ca",
        params={
            "e_dc": 0.1,
            "t_run": 10.0,
            "t_interval": 0.1,
            "cr": "100u",
            "bw_hz": 400,
        },
        channels=[1],
        description="Built-in generic CA starting point.",
    ),
}


# ---------------------------------------------------------------------------
# (De)serialization helpers
# ---------------------------------------------------------------------------


def _preset_from_dict(obj: dict[str, Any]) -> Preset:
    """Build a :class:`Preset` from a raw dict, tolerating extra keys.

    Filters the dict to the known ``Preset`` fields so older files
    (pre-batch-2) and newer ones carrying unknown extras both load
    cleanly.  Missing new fields fall back to their dataclass defaults.

    Args:
        obj: Raw mapping of preset field names to values.

    Returns:
        A constructed ``Preset``.
    """
    allowed = set(Preset.__dataclass_fields__.keys())
    filtered = {k: v for k, v in obj.items() if k in allowed}
    return Preset(**filtered)


def _presets_from_payload(
    data: dict[str, Any]
) -> dict[str, Preset]:
    """Extract a ``{name: Preset}`` map from a loaded JSON payload.

    Accepts both the versioned wrapper
    (``{"format": ..., "presets": {...}}``) and the legacy bare
    ``{name: {...}}`` map.  Detection is by the presence of a
    ``"format"`` key.

    Args:
        data: Parsed JSON object from a preset file.

    Returns:
        Mapping of preset key to ``Preset`` instance.
    """
    if isinstance(data, dict) and "format" in data:
        raw = data.get("presets", {})
    else:
        # Legacy bare map: {name: {...preset...}}.
        raw = data
    return {key: _preset_from_dict(obj) for key, obj in raw.items()}


def _wrap_presets(
    presets: dict[str, Preset]
) -> dict[str, Any]:
    """Wrap a ``{name: Preset}`` map in the versioned on-disk format.

    Args:
        presets: In-memory preset map.

    Returns:
        A JSON-serializable wrapper dict.
    """
    return {
        "format": PRESET_FILE_FORMAT,
        "version": PRESET_FILE_VERSION,
        "presets": {k: asdict(v) for k, v in presets.items()},
    }


def write_preset_file(path: str, presets: dict[str, Preset]) -> None:
    """Write a standalone preset file in the versioned wrapper format.

    Unlike :meth:`PresetManager.save_to_path` (which writes the whole live
    store, built-ins included), this writes exactly the given presets — used
    by the agent's "save this preset to a file" flow.

    Args:
        path: Destination ``*.mux16`` file path.
        presets: ``{key: Preset}`` map to write.

    The write is atomic: content goes to a temp file in the same directory and
    is ``os.replace``'d onto the destination, so a crash mid-write cannot leave
    a truncated/corrupt preset file (the merge in the agent's save_preset reads
    this file back, so a torn write would be self-inflicted data loss).
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_wrap_presets(presets), f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_preset_file(path: str) -> dict[str, Preset]:
    """Read a standalone preset file into a ``{key: Preset}`` map.

    No built-in seeding and no store side effects — returns only the presets
    actually in the file (wrapper or legacy bare-map format).

    Args:
        path: Source ``*.mux16`` (or legacy ``*.json``) preset file.

    Returns:
        Mapping of preset key to ``Preset``.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If it is not valid JSON, or carries a wrapper ``format``
            that is not a preset store (e.g. a ``*.mux16seq`` sequence file) —
            so a foreign file cannot masquerade as an empty preset store.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        fmt = data.get("format")
        if fmt not in (None, PRESET_FILE_FORMAT):
            raise ValueError(
                f"Not a preset file: format={fmt!r}, expected "
                f"{PRESET_FILE_FORMAT!r}."
            )
    try:
        return _presets_from_payload(data)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"Not a valid preset file: {exc}") from exc


# ---------------------------------------------------------------------------
# PresetManager
# ---------------------------------------------------------------------------


class PresetManager:
    """Load, save, and manage measurement presets from a JSON file.

    Built-in presets are always available in memory and cannot be
    deleted (but their parameters can be overridden by user presets
    with the same key).  The active store is an external, versioned
    file under a per-user data directory.

    Args:
        path: Path to the presets file.  When ``None`` the per-user
            default (``~/.emstat_pico_mux16/presets.mux16``) is used,
            with a one-time import from the legacy in-repo
            ``presets/presets.json`` if the external file does not yet
            exist.
    """

    def __init__(
        self, path: Optional[str] = None
    ) -> None:
        if path is None:
            self._path = _DEFAULT_PRESETS_FILE
            self._use_default_store = True
        else:
            self._path = path
            self._use_default_store = False
        self._presets: dict[str, Preset] = {}
        self._load()

    # -- internal -----------------------------------------------------

    def _seed_builtins(self) -> None:
        """Reset the in-memory map to a fresh copy of the built-ins."""
        self._presets = {
            k: Preset(**asdict(v))
            for k, v in _BUILTIN_PRESETS.items()
        }

    def _load(self) -> None:
        """Load presets from disk, merging on top of the built-ins.

        For the default store, a missing external file triggers a
        one-time migration from the legacy in-repo ``presets.json``
        (when present) before the external file is created.
        """
        self._seed_builtins()

        if os.path.isfile(self._path):
            self._read_into(self._path)
            return

        # External store does not exist yet.
        if self._use_default_store and os.path.isfile(
            _LEGACY_PRESETS_FILE
        ):
            # One-time migration of the shipped in-repo presets.
            self._read_into(_LEGACY_PRESETS_FILE)
            logger.info(
                "Migrated legacy presets from %s to %s",
                _LEGACY_PRESETS_FILE,
                self._path,
            )

        # Materialize the (possibly migrated, possibly built-in-only)
        # store at the active path so subsequent runs are stable.
        self._save()

    def _read_into(self, path: str) -> None:
        """Parse ``path`` and merge its presets over the built-ins.

        Args:
            path: Existing preset file to read.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = _presets_from_payload(data)
            self._presets.update(loaded)
            logger.info(
                "Loaded %d presets from %s", len(loaded), path
            )
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            KeyError,
            AttributeError,
        ) as e:
            logger.warning(
                "Failed to load presets from %s: %s", path, e
            )

    def _save(self) -> None:
        """Write all presets to the active path in wrapper format."""
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(_wrap_presets(self._presets), f, indent=2)
        logger.info(
            "Saved %d presets to %s",
            len(self._presets),
            self._path,
        )

    # -- explicit path I/O (sequencer feature) -------------------------------

    def save_to_path(self, path: str) -> None:
        """Write the current presets to an arbitrary file.

        The file is written in the versioned wrapper format.  The
        manager's active path is unchanged; use this to export to a
        new ``*.mux16`` location.

        Args:
            path: Destination file path.
        """
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_wrap_presets(self._presets), f, indent=2)
        logger.info(
            "Saved %d presets to %s", len(self._presets), path
        )

    def load_from_path(self, path: str) -> None:
        """Replace the in-memory presets with the contents of ``path``.

        The file is parsed STRICTLY first; any failure raises and leaves
        the manager completely untouched (presets, active path). This is
        what the GUI import flow relies on — a corrupt or wrong file
        must surface an error rather than silently emptying the preset
        list, repointing the store at the bad file, and letting the next
        save overwrite it.

        On success, built-ins are re-seeded, the file's presets are
        merged on top (user entries override same-named built-ins), and
        the manager's active path switches to ``path``.

        Args:
            path: Source preset file (wrapper or legacy bare map).

        Raises:
            OSError: If the file cannot be read.
            ValueError: If the file is not valid JSON or not a preset
                store (includes ``json.JSONDecodeError``).
        """
        # Strict parse BEFORE any state change.
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)  # raises JSONDecodeError (ValueError)
        try:
            loaded = _presets_from_payload(data)
        except (TypeError, KeyError, AttributeError) as e:
            raise ValueError(
                f"Not a valid preset store: {e}"
            ) from e

        self._seed_builtins()
        self._presets.update(loaded)
        self._path = path
        self._use_default_store = False
        logger.info("Loaded %d presets from %s", len(loaded), path)

    # -- queries ------------------------------------------------------

    @property
    def path(self) -> str:
        """Return the active preset file path."""
        return self._path

    def list_presets(self) -> list[str]:
        """Return sorted list of preset keys."""
        return sorted(self._presets.keys())

    def get_preset(self, key: str) -> Optional[Preset]:
        """Return a preset by key, or None if not found."""
        return self._presets.get(key)

    def get_all(self) -> dict[str, Preset]:
        """Return a copy of all presets."""
        return dict(self._presets)

    def add_preset(self, key: str, preset: Preset) -> None:
        """Add or update a preset and save to disk.

        Args:
            key: Unique key for the preset.
            preset: The preset configuration.
        """
        self._presets[key] = preset
        self._save()

    def delete_preset(self, key: str) -> bool:
        """Delete a user preset.

        Built-in presets cannot be deleted.

        Args:
            key: Preset key to delete.

        Returns:
            True if deleted, False if not found or built-in.
        """
        if key in _BUILTIN_PRESETS:
            logger.warning(
                "Cannot delete built-in preset: %s", key
            )
            return False
        if key in self._presets:
            del self._presets[key]
            self._save()
            return True
        return False

    def is_builtin(self, key: str) -> bool:
        """Return True if ``key`` refers to an undeletable built-in preset."""
        return key in _BUILTIN_PRESETS

"""Electrode setup presets: a named wiring for the MUX16.

An :class:`ElectrodePreset` is the electrode part of a measurement and
nothing else: which working electrodes (WE) are measured, and how the
reference and counter electrodes (RE/CE) are wired to the multiplexer. It
carries no technique and no parameters, so one setup can be used with any
technique, and a person picks "the 16-site die" instead of choosing between
the modes ``external``, ``on_board`` and ``manual``.

Every mode is covered:

* ``external`` (Mode A): the reference and counter electrodes are wired to
  RE/CE position 15.
* ``on_board`` (Mode B): the on-board combined RE/CE at position 16.
* ``manual`` (Mode C): each working electrode has its own RE/CE position,
  both within CH1-CH14.

The wiring rules are those of :class:`src.data.models.TechniqueConfig`; a
preset is checked by building one, so a preset that loads is a preset that
can run.

Presets live in their own plain-text file, apart from the measurement
presets, so saving a measurement preset (by hand or through the agent) can
never drop an electrode preset. By default it is
``electrode_presets.mux16el`` in the per-user data directory; the person can
point to any other file with the ``electrode_presets_path`` setting in
``app_settings.json`` (or File > Electrode Setups File...). The built-ins
stay in code and are always present; the file holds only the presets the
person added or changed, and can be edited by hand. The on-disk format is a
versioned JSON wrapper::

    {"format": "mux16-electrode-presets", "version": 1,
     "presets": {<key>: <asdict(ElectrodePreset)>}}
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from src.data import app_settings
from src.data.models import (
    ELECTRODE_CONFIG_MODES,
    TechniqueConfig,
)
from src.data.paths import USER_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ELECTRODE_PRESETS_FILE",
    "ELECTRODE_PRESET_FILE_FORMAT",
    "ElectrodePreset",
    "ElectrodePresetManager",
    "preset_key",
    "read_electrode_preset_file",
    "resolve_electrode_presets_path",
    "write_electrode_preset_file",
]

ELECTRODE_PRESET_FILE_FORMAT = "mux16-electrode-presets"
ELECTRODE_PRESET_FILE_VERSION = 1

#: The file used when the person has not chosen another.
DEFAULT_ELECTRODE_PRESETS_FILE = os.path.join(
    USER_DATA_DIR, "electrode_presets.mux16el"
)


def resolve_electrode_presets_path(settings_path: Optional[str] = None) -> str:
    """Where the electrode setups file is: the setting, else the default.

    The ``electrode_presets_path`` setting may use ``~`` for the home folder.
    A relative path is taken relative to the settings folder
    (``~/.emstat_pico_mux16/``), never the current folder, so it means the
    same thing however the program was started.

    Args:
        settings_path: Optional settings-file override (for tests).
    """
    configured = app_settings.get_electrode_presets_path(settings_path)
    if configured is None:
        return DEFAULT_ELECTRODE_PRESETS_FILE
    expanded = os.path.expanduser(configured)
    if not os.path.isabs(expanded):
        expanded = os.path.join(USER_DATA_DIR, expanded)
    return os.path.normpath(expanded)


def preset_key(name: str) -> str:
    """Return the store key for a preset name (lower case, underscores).

    Raises:
        ValueError: If nothing usable is left of *name*.
    """
    key = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    if not key:
        raise ValueError(f"Cannot make a preset key from {name!r}")
    return key


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ranges(channels: list[int]) -> str:
    """``[1, 2, 3, 5]`` -> ``'CH1-CH3, CH5'``."""
    parts: list[str] = []
    ordered = sorted(channels)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(
            f"CH{ordered[i]}" if i == j else f"CH{ordered[i]}-CH{ordered[j]}"
        )
        i = j + 1
    return ", ".join(parts)


@dataclass
class ElectrodePreset:
    """A named electrode setup.

    Attributes:
        name: Display name.
        electrode_config_mode: ``"external"``, ``"on_board"`` or
            ``"manual"``.
        channels: 1-indexed MUX working-electrode channels to measure, in
            run order, without repeats.
        re_ce_channels: Mode C only: the RE/CE position of each working
            electrode, parallel to *channels*. Must be empty in the other
            modes, where the mode sets the position.
        description: One or two plain sentences for the person choosing.
        site_labels: Optional. What each MUX channel is called on the
            device, as ``{"9": "pad 11"}`` (keys are channel numbers as
            text, as JSON needs).

    Raises:
        ValueError: At construction, if the wiring would be refused by
            :class:`~src.data.models.TechniqueConfig` or the lists do not
            fit the mode.
    """

    name: str
    electrode_config_mode: str = "external"
    channels: list[int] = field(default_factory=list)
    re_ce_channels: list[int] = field(default_factory=list)
    description: str = ""
    site_labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Check the preset, and normalise the mode and label keys.

        Raises:
            ValueError: On any problem, naming it.
        """
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("An electrode preset needs a name.")
        self.name = self.name.strip()
        self.electrode_config_mode = str(self.electrode_config_mode).lower()
        if self.electrode_config_mode not in ELECTRODE_CONFIG_MODES:
            raise ValueError(
                "electrode_config_mode must be one of "
                f"{ELECTRODE_CONFIG_MODES}, got {self.electrode_config_mode!r}"
            )
        if (
            not isinstance(self.channels, (list, tuple))
            or not self.channels
            or not all(_is_int(ch) for ch in self.channels)
        ):
            raise ValueError("channels must be a non-empty list of integers.")
        self.channels = list(self.channels)
        if len(set(self.channels)) != len(self.channels):
            raise ValueError(f"channels repeat a channel: {self.channels}")

        self.re_ce_channels = list(self.re_ce_channels or [])
        if self.electrode_config_mode == "manual":
            if len(self.re_ce_channels) != len(self.channels):
                raise ValueError(
                    "A manual preset needs one RE/CE channel per working "
                    f"electrode (got {len(self.re_ce_channels)} for "
                    f"{len(self.channels)} channels)."
                )
        elif self.re_ce_channels:
            raise ValueError(
                f"The {self.electrode_config_mode!r} mode sets the RE/CE "
                "position itself; re_ce_channels must be empty."
            )

        # The wiring rules live in TechniqueConfig; use them, do not copy.
        TechniqueConfig(
            technique="cv",
            params={},
            channels=list(self.channels),
            re_ce_channels=list(self.re_ce_channels),
            electrode_config_mode=self.electrode_config_mode,
        )

        labels: dict[str, str] = {}
        for key, label in dict(self.site_labels or {}).items():
            if not isinstance(label, str):
                raise ValueError(f"site label for {key!r} must be text.")
            try:
                channel = int(key)
            except (TypeError, ValueError):
                raise ValueError(
                    f"site label key {key!r} is not a channel."
                ) from None
            if channel not in self.channels:
                raise ValueError(
                    f"site label for CH{channel}, which is not in channels."
                )
            labels[str(channel)] = label
        self.site_labels = labels

    def wiring(self) -> tuple[str, list[int], list[int]]:
        """Return ``(mode, channels, re_ce_channels)`` ready for a run.

        The RE/CE list is filled in for the modes that imply it, exactly as
        :class:`~src.data.models.TechniqueConfig` does, so the three values
        can be passed straight to it.
        """
        config = TechniqueConfig(
            technique="cv",
            params={},
            channels=list(self.channels),
            re_ce_channels=list(self.re_ce_channels),
            electrode_config_mode=self.electrode_config_mode,
        )
        return (
            config.electrode_config_mode,
            list(config.channels),
            list(config.re_ce_channels),
        )

    def describe(self) -> str:
        """One line that says what the setup does, from its data."""
        mode, channels, re_ce = self.wiring()
        if mode == "manual":
            pairs = ", ".join(f"CH{w}->CH{r}" for w, r in zip(channels, re_ce))
            return f"Each working electrode has its own RE/CE: {pairs}."
        position = re_ce[0]
        where = "external" if mode == "external" else "on-board"
        return (
            f"Working electrodes {_ranges(channels)}; {where} reference and "
            f"counter at position {position}."
        )


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------

# One die layout is shipped as two built-ins: 18 pads in a row, the middle two
# (pads 9 and 10) not measured as working electrodes. Pads 1-8 are working
# electrodes WE1-WE8 and pads 11-18 are WE9-WE16; WEn is MUX channel CHn. The
# two built-ins differ only in where the reference and counter come from: a
# separate external pair (external wiring, position 15) or the pair on the die,
# pad 9 as reference and pad 10 as counter (on-board wiring, position 16).
_DIE_PAD_BY_CHANNEL = {
    str(ch): f"pad {ch if ch <= 8 else ch + 2}" for ch in range(1, 17)
}

_BUILTIN_ELECTRODE_PRESETS: dict[str, ElectrodePreset] = {
    "separate_re_ce": ElectrodePreset(
        name="Separate reference + counter electrodes",
        electrode_config_mode="external",
        channels=list(range(1, 17)),
        description=(
            "A reference electrode and a counter electrode wired in from "
            "outside, shared by every working electrode (RE/CE position 15)."
        ),
    ),
    "on_chip_re_ce": ElectrodePreset(
        name="Reference + counter on the chip (shorted)",
        electrode_config_mode="on_board",
        channels=list(range(1, 17)),
        description=(
            "One combined reference and counter electrode on the board, "
            "shared by every working electrode (RE/CE position 16)."
        ),
    ),
    "per_electrode_re_ce": ElectrodePreset(
        name="One RE/CE per working electrode (CH1-CH14)",
        electrode_config_mode="manual",
        channels=list(range(1, 15)),
        re_ce_channels=list(range(1, 15)),
        description=(
            "Each working electrode has its own reference/counter pair on "
            "the same position number, as on the vendor resistor test cell. "
            "Change the pairing by hand and save it under a new name."
        ),
    ),
    "die_16_external": ElectrodePreset(
        name="16-site die, external RE + CE",
        electrode_config_mode="external",
        channels=list(range(1, 17)),
        description=(
            "A die with 18 pads in a row. Pads 1-8 are CH1-CH8 and pads "
            "11-18 are CH9-CH16; the middle two (pads 9 and 10) are not "
            "measured. A separate reference electrode and counter electrode "
            "are wired in from outside (RE/CE position 15)."
        ),
        site_labels=dict(_DIE_PAD_BY_CHANNEL),
    ),
    "die_16_on_chip": ElectrodePreset(
        name="16-site die, on-chip RE + CE (pads 9, 10)",
        electrode_config_mode="on_board",
        channels=list(range(1, 17)),
        description=(
            "The same die and channels (pads 1-8 are CH1-CH8, pads 11-18 "
            "are CH9-CH16), but the reference and counter are the ones on "
            "the die: pad 9 is the reference and pad 10 the counter, so "
            "they are not measured. On-board RE/CE wiring (position 16), "
            "which shorts the reference and counter together: short pads 9 "
            "and 10 outside the die."
        ),
        site_labels=dict(_DIE_PAD_BY_CHANNEL),
    ),
}


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _from_dict(obj: dict[str, Any]) -> ElectrodePreset:
    allowed = set(ElectrodePreset.__dataclass_fields__)
    return ElectrodePreset(**{k: v for k, v in obj.items() if k in allowed})


def _read_entries(path: str) -> tuple[dict[str, ElectrodePreset], list[str]]:
    """Read a file, returning the valid presets and a note per bad entry.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If it is not JSON or not an electrode preset file.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if (
        not isinstance(data, dict)
        or data.get("format") != ELECTRODE_PRESET_FILE_FORMAT
    ):
        found = data.get("format") if isinstance(data, dict) else None
        raise ValueError(
            f"Not an electrode preset file: format={found!r}, expected "
            f"{ELECTRODE_PRESET_FILE_FORMAT!r}."
        )
    raw = data.get("presets", {})
    if not isinstance(raw, dict):
        raise ValueError(
            "Not a valid electrode preset file: 'presets' is not a map."
        )
    good: dict[str, ElectrodePreset] = {}
    problems: list[str] = []
    for key, obj in raw.items():
        try:
            good[key] = _from_dict(obj)
        except (TypeError, ValueError, AttributeError) as exc:
            problems.append(f"{key!r}: {exc}")
    return good, problems


def read_electrode_preset_file(path: str) -> dict[str, ElectrodePreset]:
    """Read an electrode preset file strictly.

    Returns:
        ``{key: ElectrodePreset}``.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If it is not JSON, is not an electrode preset file, or
            holds an entry that is not a valid preset (the message names it).
    """
    good, problems = _read_entries(path)
    if problems:
        raise ValueError(
            "Electrode preset(s) not valid: " + "; ".join(problems)
        )
    return good


def write_electrode_preset_file(
    path: str, presets: dict[str, ElectrodePreset]
) -> None:
    """Write electrode presets atomically (temp file, then replace)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "format": ELECTRODE_PRESET_FILE_FORMAT,
        "version": ELECTRODE_PRESET_FILE_VERSION,
        "presets": {k: asdict(v) for k, v in presets.items()},
    }
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ElectrodePresetManager:
    """The person's electrode presets, with the built-ins always present.

    Nothing is written when the manager is made; the file appears the first
    time a preset is added or removed. A user preset with a built-in's key
    replaces it, and deleting that preset brings the built-in back.

    Args:
        path: Preset file. ``None`` uses the file the person chose with the
            ``electrode_presets_path`` setting, else the per-user default
            (see :func:`resolve_electrode_presets_path`).
        settings_path: Settings file to read that setting from (for tests).

    Attributes:
        load_problems: One sentence per problem found when the file was
            read (an unreadable file, or entries that did not validate).
            Empty when the file loaded cleanly or does not exist yet.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        settings_path: Optional[str] = None,
    ) -> None:
        self._path = path or resolve_electrode_presets_path(settings_path)
        self._presets: dict[str, ElectrodePreset] = {}
        self._unreadable = False
        self.load_problems: list[str] = []
        self._load()

    def use_file(self, path: str) -> None:
        """Switch to another setups file and load it (built-ins stay)."""
        self._path = path
        self._load()

    def reload(self) -> None:
        """Read the file again, for example after editing it by hand."""
        self._load()

    def _load(self) -> None:
        self._presets = copy.deepcopy(_BUILTIN_ELECTRODE_PRESETS)
        self._unreadable = False
        self.load_problems = []
        if not os.path.isfile(self._path):
            return
        try:
            loaded, problems = _read_entries(self._path)
        except (OSError, ValueError) as exc:
            # json.JSONDecodeError is a ValueError. Keep the built-ins; the
            # bad file is set aside before the next save, never overwritten.
            self._unreadable = True
            self.load_problems = [f"could not be read: {exc}"]
            logger.warning(
                "Could not load electrode presets from %s: %s", self._path, exc
            )
            return
        if problems:
            # A hand-edited entry that does not validate is skipped, and the
            # file is kept as a .bak before the next save drops it.
            self._unreadable = True
            self.load_problems = [f"skipped {p}" for p in problems]
            logger.warning(
                "Skipped invalid electrode presets in %s: %s",
                self._path,
                "; ".join(problems),
            )
        self._presets.update(loaded)
        logger.info("Loaded %d electrode presets from %s", len(loaded), self._path)

    def _save(self) -> None:
        if self._unreadable and os.path.isfile(self._path):
            backup = self._path + ".bak"
            shutil.copyfile(self._path, backup)
            logger.warning("Kept the unreadable electrode preset file as %s", backup)
            self._unreadable = False
        user = {
            k: v
            for k, v in self._presets.items()
            if k not in _BUILTIN_ELECTRODE_PRESETS
            or v != _BUILTIN_ELECTRODE_PRESETS[k]
        }
        write_electrode_preset_file(self._path, user)

    # -- queries -----------------------------------------------------------

    @property
    def path(self) -> str:
        """The preset file path."""
        return self._path

    def list_presets(self) -> list[str]:
        """Sorted preset keys."""
        return sorted(self._presets)

    def get_preset(self, key: str) -> Optional[ElectrodePreset]:
        """A copy of the preset, or None."""
        preset = self._presets.get(key)
        return copy.deepcopy(preset) if preset is not None else None

    def get_all(self) -> dict[str, ElectrodePreset]:
        """A copy of every preset, built-ins included."""
        return copy.deepcopy(self._presets)

    def is_builtin(self, key: str) -> bool:
        """True for a built-in key, even when the person has replaced it."""
        return key in _BUILTIN_ELECTRODE_PRESETS

    def is_deletable(self, key: str) -> bool:
        """True if deleting *key* changes something."""
        if key not in self._presets:
            return False
        return (
            key not in _BUILTIN_ELECTRODE_PRESETS
            or self._presets[key] != _BUILTIN_ELECTRODE_PRESETS[key]
        )

    # -- changes -----------------------------------------------------------

    def add_preset(self, key: str, preset: ElectrodePreset) -> None:
        """Add or replace a preset and save.

        Raises:
            ValueError: If *key* is empty or *preset* is not valid.
        """
        if not key:
            raise ValueError("An electrode preset needs a key.")
        preset.validate()
        self._presets[key] = copy.deepcopy(preset)
        self._save()

    def delete_preset(self, key: str) -> bool:
        """Delete a user preset, or restore a replaced built-in.

        Returns:
            True if something changed; False for an unknown key and for a
            built-in the person has not replaced.
        """
        if not self.is_deletable(key):
            if key in _BUILTIN_ELECTRODE_PRESETS:
                logger.warning("Cannot delete built-in electrode preset: %s", key)
            return False
        if key in _BUILTIN_ELECTRODE_PRESETS:
            self._presets[key] = copy.deepcopy(_BUILTIN_ELECTRODE_PRESETS[key])
        else:
            del self._presets[key]
        self._save()
        return True

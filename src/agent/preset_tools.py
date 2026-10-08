"""Agent tools for saving/loading presets and sequences via file dialogs.

Exposes :func:`build_preset_tools`, which returns ``(tool_def, handler)``
pairs ready for ``ToolRegistry.register`` (or the ``extra_tools`` argument
of :func:`src.agent.tools.build_registry``) -- the same plug-in seam the
vendored-analysis tools use.

Save/load follow the app's NORMAL operation: the agent does not pick a
path, it asks the user. Each tool opens the native file dialog (the
injected ``file_dialog`` provider, run on the GUI thread via
:func:`src.agent.bridge.run_on_gui`) so the operator chooses where to save
or which file to load -- exactly like the sequencer panel's Save/Load
buttons. When no provider is available (any headless caller -- tests, or a
future non-GUI front end) the tools fall back to an explicit ``path``
argument and otherwise return a clear error.

Every configuration is validated through
:func:`src.agent.engine_adapter.build_technique_config` BEFORE it is
written, so anything the agent saves is runnable. Handlers never raise:
failures (including a user-cancelled dialog) are returned as structured
``{"ok": false, ...}`` dicts.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Awaitable, Callable, Optional, Protocol

from src.agent.bridge import run_on_gui
from src.agent.engine_adapter import build_technique_config
from src.data.presets import Preset, read_preset_file, write_preset_file
from src.data.sequence import Sequence, SequenceStep

logger = logging.getLogger(__name__)

__all__ = ["FileDialogProvider", "build_preset_tools"]

_PRESET_FILTER = "MUX16 presets (*.mux16)"
_PRESET_SUFFIX = ".mux16"
_SEQUENCE_FILTER = "MUX16 sequences (*.mux16seq)"
_SEQUENCE_SUFFIX = ".mux16seq"


class FileDialogProvider(Protocol):
    """Native file-dialog provider, called ON THE GUI THREAD.

    The GUI supplies an implementation; the tool handlers marshal the calls
    onto the GUI thread via :func:`run_on_gui`. Each method returns the
    chosen absolute path, or ``None`` if the user cancelled.
    """

    def request_save_path(
        self, suggested_name: str, file_filter: str
    ) -> Optional[str]: ...

    def request_open_path(self, file_filter: str) -> Optional[str]: ...


def _slug(name: str) -> str:
    """Filesystem-safe slug from a display name (suggested file name)."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _ensure_suffix(path: str, suffix: str) -> str:
    """Ensure ``path`` ends in ``suffix``, replacing a sibling preset/sequence
    extension rather than double-appending (``x.mux16seq`` + ``.mux16`` ->
    ``x.mux16``, not ``x.mux16seq.mux16``)."""
    low = path.lower()
    if low.endswith(suffix):
        return path
    for sibling in (_SEQUENCE_SUFFIX, _PRESET_SUFFIX):
        if sibling != suffix and low.endswith(sibling):
            return path[: -len(sibling)] + suffix
    return path + suffix


def _config_from_step(step: dict[str, Any]):
    """Validate one step/preset dict into a TechniqueConfig (raises ValueError)."""
    technique = step.get("technique")
    if not technique or not str(technique).strip():
        raise ValueError("each step requires a 'technique'.")
    args = dict(step.get("params") or {})
    args["channels"] = step.get("channels")
    mode = step.get("electrode_config_mode")
    if mode:
        args["electrode_config_mode"] = mode
    re_ce = step.get("re_ce_channels")
    if re_ce:
        args["re_ce_channels"] = re_ce
    return build_technique_config(str(technique), args)


def _store_re_ce(config: Any) -> list[int]:
    """RE/CE list to persist: explicit only for manual wiring (else [])."""
    if config.electrode_config_mode == "manual":
        return list(config.re_ce_channels)
    return []


def build_preset_tools(
    file_dialog: Optional[FileDialogProvider] = None,
    is_busy: Optional[Callable[[], bool]] = None,
) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]]:
    """Build the preset/sequence save & load ``(tool_def, handler)`` pairs.

    Args:
        file_dialog: GUI provider that opens native save/open dialogs on the
            GUI thread. ``None`` (any headless caller) makes the tools
            require an explicit ``path`` argument instead.
        is_busy: Optional zero-arg predicate (e.g. ``engine.isRunning``).
            When it returns True the tools refuse to OPEN a file dialog, so a
            modal dialog cannot freeze the live view and lock out Abort during
            a measurement. An explicit ``path`` (no dialog) is unaffected.

    Returns:
        List of pairs ready for ``ToolRegistry.register``. Handlers are
        async coroutines and never raise.
    """

    def _busy_block() -> Optional[dict[str, Any]]:
        if is_busy is not None and is_busy():
            return {
                "ok": False,
                "error": (
                    "A measurement is running — finish or abort it before "
                    "saving/loading (the file dialog would freeze the live "
                    "view and block Abort)."
                ),
            }
        return None

    async def _resolve_save_path(
        args: dict[str, Any], suggested_name: str, file_filter: str, suffix: str
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        """Return (path, error_dict). Exactly one is non-None."""
        path = args.get("path")
        if path:
            return _ensure_suffix(str(path), suffix), None
        if file_dialog is None:
            return None, {
                "ok": False,
                "error": (
                    "No 'path' given and no interactive file dialog is "
                    "available here; pass an explicit 'path'."
                ),
            }
        busy = _busy_block()
        if busy is not None:
            return None, busy
        chosen = await run_on_gui(
            file_dialog.request_save_path, suggested_name, file_filter
        )
        if not chosen:
            return None, {
                "ok": False,
                "cancelled": True,
                "message": "Save cancelled by the user.",
            }
        return _ensure_suffix(str(chosen), suffix), None

    async def _resolve_open_path(
        args: dict[str, Any], file_filter: str
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        path = args.get("path")
        if path:
            return str(path), None
        if file_dialog is None:
            return None, {
                "ok": False,
                "error": (
                    "No 'path' given and no interactive file dialog is "
                    "available here; pass an explicit 'path'."
                ),
            }
        busy = _busy_block()
        if busy is not None:
            return None, busy
        chosen = await run_on_gui(file_dialog.request_open_path, file_filter)
        if not chosen:
            return None, {
                "ok": False,
                "cancelled": True,
                "message": "Open cancelled by the user.",
            }
        return str(chosen), None

    # ---- save_preset ------------------------------------------------------

    async def save_preset(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "A non-empty 'name' is required."}
        key = _slug(name) or "preset"
        try:
            config = _config_from_step(args)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        path, err = await _resolve_save_path(
            args, f"{key}{_PRESET_SUFFIX}", _PRESET_FILTER, _PRESET_SUFFIX
        )
        if err is not None:
            return err

        preset = Preset(
            name=name,
            technique=config.technique,
            params=dict(config.params),
            channels=list(config.channels),
            auto_save=bool(args.get("auto_save", False)),
            description=str(args.get("description", "") or ""),
            electrode_config_mode=config.electrode_config_mode,
            re_ce_channels=_store_re_ce(config),
        )
        # Merge into an existing file rather than clobbering other presets the
        # user keeps alongside it -- but REFUSE if the target exists and is not a
        # readable preset store, instead of silently truncating it to just this
        # preset (a foreign/corrupt file would otherwise be destroyed).
        existing: dict[str, Preset] = {}
        if os.path.isfile(path):
            try:
                existing = read_preset_file(path)
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "error": (
                        f"Refusing to overwrite {os.path.abspath(path)!r}: it "
                        f"exists but is not a readable preset file ({exc}). "
                        "Choose a different name or location."
                    ),
                }
        replaced = key in existing
        # A different display name slugging to the same key would silently
        # clobber the prior entry; surface that so it isn't a silent loss.
        clobbered = (
            existing[key].name
            if replaced and existing[key].name != name
            else None
        )
        existing[key] = preset
        try:
            write_preset_file(path, existing)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            logger.exception("save_preset failed")
            return {
                "ok": False,
                "error": f"Failed to save preset: {type(exc).__name__}: {exc}",
            }
        logger.info("Agent saved preset %r to %s.", name, path)
        result = {
            "ok": True,
            "name": name,
            "technique": config.technique,
            "channels": list(config.channels),
            "replaced": replaced,
            "path": os.path.abspath(path),
        }
        if clobbered is not None:
            result["warning"] = (
                f"Replaced a differently-named preset {clobbered!r} that maps "
                f"to the same key {key!r} in this file."
            )
        return result

    # ---- save_sequence ----------------------------------------------------

    async def save_sequence(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "A non-empty 'name' is required."}
        key = _slug(name) or "sequence"
        raw_steps = args.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return {
                "ok": False,
                "error": "'steps' must be a non-empty list of step objects.",
            }
        steps: list[SequenceStep] = []
        for idx, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                return {"ok": False, "error": f"step {idx + 1} must be an object."}
            try:
                config = _config_from_step(raw)
            except ValueError as exc:
                return {"ok": False, "error": f"step {idx + 1}: {exc}"}
            # Coerce inside the loop's error handling so a non-numeric
            # repeat/delay_s yields a clean step-indexed error, not a raw
            # exception (the module's never-raise contract).
            try:
                repeat = max(1, int(raw.get("repeat", 1)))
                delay_s = float(raw.get("delay_s", 0.0))
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": (
                        f"step {idx + 1}: repeat must be an integer and "
                        "delay_s a number."
                    ),
                }
            steps.append(
                SequenceStep(
                    preset_name=str(raw.get("preset_name") or f"{key}_{idx + 1}"),
                    repeat=repeat,
                    delay_s=delay_s,
                    technique=config.technique,
                    params=dict(config.params),
                    channels=list(config.channels),
                    electrode_config_mode=config.electrode_config_mode,
                    re_ce_channels=_store_re_ce(config),
                )
            )

        path, err = await _resolve_save_path(
            args, f"{key}{_SEQUENCE_SUFFIX}", _SEQUENCE_FILTER, _SEQUENCE_SUFFIX
        )
        if err is not None:
            return err
        try:
            Sequence(name=name, steps=steps).save_to_path(path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            logger.exception("save_sequence failed")
            return {
                "ok": False,
                "error": f"Failed to save sequence: {type(exc).__name__}: {exc}",
            }
        logger.info("Agent saved sequence %r (%d steps) to %s.", name, len(steps), path)
        return {
            "ok": True,
            "name": name,
            "n_steps": len(steps),
            "path": os.path.abspath(path),
        }

    # ---- load_preset ------------------------------------------------------

    async def load_preset(args: dict[str, Any]) -> dict[str, Any]:
        path, err = await _resolve_open_path(args, _PRESET_FILTER)
        if err is not None:
            return err
        if not os.path.isfile(path):
            return {"ok": False, "error": f"File not found: {path!r}."}
        try:
            presets = read_preset_file(path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"Could not read preset file: {exc}"}
        if not presets:
            return {
                "ok": False,
                "error": f"No presets found in {os.path.abspath(path)!r}.",
            }
        return {
            "ok": True,
            "path": os.path.abspath(path),
            "count": len(presets),
            "presets": [
                {
                    "key": key,
                    "name": p.name,
                    "technique": p.technique,
                    "params": dict(p.params),
                    "channels": list(p.channels),
                    "electrode_config_mode": p.electrode_config_mode,
                    "re_ce_channels": list(p.re_ce_channels),
                    "description": p.description,
                }
                for key, p in sorted(presets.items())
            ],
        }

    # ---- load_sequence ----------------------------------------------------

    async def load_sequence(args: dict[str, Any]) -> dict[str, Any]:
        path, err = await _resolve_open_path(args, _SEQUENCE_FILTER)
        if err is not None:
            return err
        if not os.path.isfile(path):
            return {"ok": False, "error": f"File not found: {path!r}."}
        try:
            seq = Sequence.load_from_path(path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"Could not read sequence file: {exc}"}
        if not seq.steps:
            return {
                "ok": False,
                "error": f"No steps found in {os.path.abspath(path)!r}.",
            }
        return {
            "ok": True,
            "path": os.path.abspath(path),
            "name": seq.name,
            "n_steps": len(seq.steps),
            "steps": [
                {
                    "technique": s.technique,
                    "params": dict(s.params),
                    "channels": list(s.channels),
                    "electrode_config_mode": s.electrode_config_mode,
                    "re_ce_channels": list(s.re_ce_channels),
                    "repeat": s.repeat,
                    "delay_s": s.delay_s,
                }
                for s in seq.steps
            ],
        }

    # ---- Tool definitions -------------------------------------------------

    _channels_schema = {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 16},
        "minItems": 1,
        "description": "1-indexed MUX channels (1-16).",
    }
    _mode_schema = {
        "type": "string",
        "enum": ["external", "on_board", "manual"],
        "description": (
            "RE/CE wiring mode (default 'external'); 'manual' needs "
            "re_ce_channels."
        ),
    }
    _re_ce_schema = {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 16},
        "description": "Per-WE RE/CE positions (manual wiring only).",
    }
    _path_schema = {
        "type": "string",
        "description": (
            "Optional explicit file path. OMIT to open the native file "
            "dialog so the user chooses (the normal flow); only pass a path "
            "in a headless context."
        ),
    }

    return [
        (
            {
                "name": "save_preset",
                "description": (
                    "Save a single measurement configuration as a reusable "
                    "preset FILE. Call this when the user asks to save / "
                    "remember the current settings. By default it opens a "
                    "native Save dialog so the USER picks where to save (do "
                    "not invent a path) -- omit 'path'. Provide the technique "
                    "and the EXACT parameters that worked (e.g. the current "
                    "range dialed in via EIS auto-ranging, the bandwidth from "
                    "the CV noise scope). Validated before saving."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Display name, e.g. 'CeOx CV 100mV/s' (also "
                                "used to suggest the file name)."
                            ),
                        },
                        "technique": {
                            "type": "string",
                            "description": "'cv' / 'ca' / 'cp' / 'eis' / 'geis'.",
                        },
                        "params": {
                            "type": "object",
                            "additionalProperties": True,
                            "description": (
                                "Technique parameters to store (omitted keys "
                                "fall back to defaults)."
                            ),
                        },
                        "channels": _channels_schema,
                        "electrode_config_mode": _mode_schema,
                        "re_ce_channels": _re_ce_schema,
                        "description": {
                            "type": "string",
                            "description": "Optional human-readable note.",
                        },
                        "auto_save": {
                            "type": "boolean",
                            "description": "Auto-save runs from this preset (default false).",
                        },
                        "path": _path_schema,
                    },
                    "required": ["name", "technique", "params", "channels"],
                    "additionalProperties": False,
                },
            },
            save_preset,
        ),
        (
            {
                "name": "save_sequence",
                "description": (
                    "Save an ordered, multi-step characterization (e.g. a CV "
                    "then an EIS) as a sequence FILE that re-runs the steps "
                    "back-to-back (loadable in the sequencer panel). Call this "
                    "when the user asks to save a whole workflow / routine of "
                    "MORE THAN ONE measurement. Opens a native Save dialog by "
                    "default (omit 'path') so the user picks the location. "
                    "Each step carries its own technique, parameters, and "
                    "channels. For a single measurement use save_preset."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Display name for the sequence.",
                        },
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "description": "Ordered steps (each runs to completion).",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "technique": {
                                        "type": "string",
                                        "description": "'cv' / 'ca' / 'cp' / 'eis' / 'geis'.",
                                    },
                                    "params": {
                                        "type": "object",
                                        "additionalProperties": True,
                                        "description": "Technique parameters for this step.",
                                    },
                                    "channels": _channels_schema,
                                    "electrode_config_mode": _mode_schema,
                                    "re_ce_channels": _re_ce_schema,
                                    "repeat": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "description": "Times to run this step (default 1).",
                                    },
                                    "delay_s": {
                                        "type": "number",
                                        "description": "Idle delay (s) AFTER this step (default 0).",
                                    },
                                },
                                "required": ["technique", "params", "channels"],
                                "additionalProperties": False,
                            },
                        },
                        "path": _path_schema,
                    },
                    "required": ["name", "steps"],
                    "additionalProperties": False,
                },
            },
            save_sequence,
        ),
        (
            {
                "name": "load_preset",
                "description": (
                    "Load a saved preset FILE chosen by the user. Call this "
                    "when the user wants to reuse / open a saved preset. Opens "
                    "a native Open dialog by default (omit 'path') so the user "
                    "finds the file. Returns the preset(s) -- technique, "
                    "parameters, channels -- which you can then run."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"path": _path_schema},
                    "additionalProperties": False,
                },
            },
            load_preset,
        ),
        (
            {
                "name": "load_sequence",
                "description": (
                    "Load a saved sequence FILE (*.mux16seq) chosen by the "
                    "user. Call this when the user wants to reuse / open a "
                    "saved characterization. Opens a native Open dialog by "
                    "default (omit 'path'). Returns the ordered steps, which "
                    "you can then run back-to-back."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"path": _path_schema},
                    "additionalProperties": False,
                },
            },
            load_sequence,
        ),
    ]

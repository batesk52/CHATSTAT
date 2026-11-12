"""Electrode setups by name for the agent's tools.

A person picks an electrode setup by name in the GUI; the agent can do the
same. Every tool that takes wiring (the ``run_*`` tools, ``save_preset`` and
the steps of ``save_sequence``) accepts an optional ``electrode_setup``
argument, resolved here through the same store the GUI uses
(:class:`src.data.electrode_presets.ElectrodePresetManager`: the built-ins
plus the person's saved setups).

Rules:

* Without ``electrode_setup`` (or with it null or empty) the tool input is
  passed on untouched, so every call written before this argument existed
  sends the same script.
* With it, the working electrodes and their RE/CE positions come from the
  setup. An explicit ``channels`` list may narrow the setup to some of its
  working electrodes (each keeps its RE/CE). Giving ``re_ce_channels`` or
  ``electrode_config_mode`` as well is refused, because the two could
  disagree.
* An unknown name is refused with the list of the names that exist.

A name is looked up as the GUI and the preset files look it up
(:func:`src.data.electrode_presets.find_electrode_setup`): a key match
wins; otherwise a display name resolves only when exactly one setup has it,
and a name two setups share is refused with their keys.

The resolved input carries ``channels``, ``re_ce_channels`` and the
internal wiring label derived from that table
(:func:`src.data.models.wiring_mode_for`), never chosen.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from src.data.electrode_presets import (
    ElectrodePreset,
    available_setups_text,
    builtin_electrode_presets,
    find_electrode_setup,
    setup_wiring,
)

__all__ = [
    "SETUP_ARG",
    "SetupProvider",
    "default_setup_provider",
    "find_setup",
    "list_setups",
    "resolve_setup",
    "resolve_setup_key",
    "setup_prop",
]

#: Name of the tool argument.
SETUP_ARG = "electrode_setup"

#: Returns ``{key: ElectrodePreset}``: every setup the person can pick.
SetupProvider = Callable[[], Mapping[str, ElectrodePreset]]


def default_setup_provider() -> dict[str, ElectrodePreset]:
    """The built-in setups only (the GUI passes its own store instead)."""
    return builtin_electrode_presets()


def setup_prop() -> dict[str, Any]:
    """Schema for the ``electrode_setup`` argument."""
    return {
        "type": "string",
        "description": (
            "Name of an electrode setup, as the person picks it in the "
            "app (the setup's key or its display name; device_status "
            "lists them under 'electrode_setups'). The setup gives the "
            "working electrodes and the RE/CE position of each. "
            "'channels' may then name some of the setup's working "
            "electrodes to measure only those. Do not also give "
            "re_ce_channels."
        ),
    }


def find_setup(
    name: str, setups: Mapping[str, ElectrodePreset]
) -> Optional[tuple[str, ElectrodePreset]]:
    """Find a setup by key, else by a display name only one setup has.

    The lookup is the GUI's and the file loaders' own
    (:func:`src.data.electrode_presets.find_electrode_setup`): a key match
    wins, and a display name shared by two setups resolves to neither.

    Returns:
        ``(key, setup)``, or None when the name is unknown or ambiguous.
    """
    try:
        return find_electrode_setup(name, setups)
    except ValueError:
        return None


def list_setups(setups: Mapping[str, ElectrodePreset]) -> list[dict[str, Any]]:
    """Compact listing for the model: key, name and what the setup does."""
    return [
        {"key": key, "name": setups[key].name, "wiring": setups[key].describe()}
        for key in sorted(setups)
    ]


def resolve_setup(
    tool_input: Mapping[str, Any], setups: Mapping[str, ElectrodePreset]
) -> dict[str, Any]:
    """Replace ``electrode_setup`` in a tool input by the setup's wiring.

    Args:
        tool_input: The model's tool input (not changed).
        setups: Every setup that can be picked.

    Returns:
        A new input dict. Without ``electrode_setup`` it equals
        *tool_input*. With it, ``channels`` and ``re_ce_channels`` are the
        setup's (narrowed to the explicit ``channels``, in that order, when
        given), the label is derived from that table, and
        ``electrode_setup`` is gone.

    Raises:
        ValueError: With a message the model can act on, for an unknown
            or ambiguous name, conflicting wiring, or a channel the setup
            lacks.
    """
    args, _ = resolve_setup_key(tool_input, setups)
    return args


def resolve_setup_key(
    tool_input: Mapping[str, Any], setups: Mapping[str, ElectrodePreset]
) -> tuple[dict[str, Any], Optional[str]]:
    """Like :func:`resolve_setup`, also returning the setup's key.

    Returns:
        ``(args, key)``; *key* is None when no setup was named.
    """
    args = dict(tool_input or {})
    if SETUP_ARG not in args:
        return args, None
    name = args.pop(SETUP_ARG)
    # An explicit null or empty string means "no setup", as the conflict
    # check below treats None, [] and "" for the wiring arguments.
    if name is None or (isinstance(name, str) and not name.strip()):
        return args, None
    if not isinstance(name, str):
        raise ValueError(
            f"{SETUP_ARG} must be a setup name. Available setups: "
            f"{available_setups_text(setups)}."
        )
    conflicting = [
        key
        for key in ("re_ce_channels", "electrode_config_mode")
        if args.get(key) not in (None, [], "")
    ]
    if conflicting:
        raise ValueError(
            f"Give either {SETUP_ARG} or {', '.join(conflicting)}, not "
            f"both: the setup {name!r} already sets the RE/CE position of "
            "each working electrode. Drop "
            f"{' and '.join(conflicting)} to use the setup (optionally "
            "with 'channels' to measure only some of its working "
            f"electrodes), or drop {SETUP_ARG} to give the wiring by hand."
        )
    channels = args.get("channels")
    if channels is not None and (
        not isinstance(channels, (list, tuple)) or not channels
    ):
        raise ValueError(
            "channels must be a non-empty list of the setup's working "
            f"electrodes when {SETUP_ARG} is given."
        )
    try:
        key, channels, re_ce, label = setup_wiring(
            name, setups, list(channels) if channels is not None else None
        )
    except ValueError as exc:
        raise ValueError(
            f"{exc} Measure some of those, or pick another setup."
            if "are not working electrodes" in str(exc)
            else str(exc)
        ) from None
    args["channels"] = channels
    args["re_ce_channels"] = re_ce
    # The label follows the table, as in the GUI, so the wiring rules and
    # the exported provenance are those of the setup.
    args["electrode_config_mode"] = label
    return args, key

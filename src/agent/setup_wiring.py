"""Electrode setups by name for the agent's tools.

A person picks an electrode setup by name in the GUI; the agent can do the
same. Every tool that takes wiring (the ``run_*`` tools, ``save_preset`` and
the steps of ``save_sequence``) accepts an optional ``electrode_setup``
argument, resolved here through the same store the GUI uses
(:class:`src.data.electrode_presets.ElectrodePresetManager`: the built-ins
plus the person's saved setups).

Rules:

* Without ``electrode_setup`` the tool input is passed on untouched, so
  every call written before this argument existed sends the same script.
* With it, the working electrodes and their RE/CE positions come from the
  setup. An explicit ``channels`` list may narrow the setup to some of its
  working electrodes (each keeps its RE/CE). Giving ``re_ce_channels`` or
  ``electrode_config_mode`` as well is refused, because the two could
  disagree.
* An unknown name is refused with the list of the names that exist.

The resolved input carries ``channels`` and ``re_ce_channels`` only; the
internal wiring label is derived from the table downstream
(:func:`src.data.models.wiring_mode_for`), never chosen.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from src.data.electrode_presets import ElectrodePreset, builtin_electrode_presets

__all__ = [
    "SETUP_ARG",
    "SetupProvider",
    "default_setup_provider",
    "find_setup",
    "list_setups",
    "resolve_setup",
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


def _norm(text: str) -> str:
    return " ".join(str(text).split()).lower()


def find_setup(
    name: str, setups: Mapping[str, ElectrodePreset]
) -> Optional[tuple[str, ElectrodePreset]]:
    """Find a setup by key or display name, ignoring case and spacing."""
    wanted = _norm(name)
    for key, preset in setups.items():
        if wanted in (_norm(key), _norm(preset.name)):
            return key, preset
    return None


def list_setups(setups: Mapping[str, ElectrodePreset]) -> list[dict[str, Any]]:
    """Compact listing for the model: key, name and what the setup does."""
    return [
        {"key": key, "name": setups[key].name, "wiring": setups[key].describe()}
        for key in sorted(setups)
    ]


def _available(setups: Mapping[str, ElectrodePreset]) -> str:
    return "; ".join(
        f"{key!r} ({setups[key].name})" for key in sorted(setups)
    )


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
        given) and ``electrode_setup`` is gone.

    Raises:
        ValueError: With a message the model can act on, for an unknown
            name, conflicting wiring, or a channel the setup lacks.
    """
    args = dict(tool_input or {})
    if SETUP_ARG not in args:
        return args
    name = args.pop(SETUP_ARG)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"{SETUP_ARG} must be a setup name. Available setups: "
            f"{_available(setups)}."
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
    found = find_setup(name, setups)
    if found is None:
        raise ValueError(
            f"Unknown electrode setup {name!r}. Available setups: "
            f"{_available(setups)}."
        )
    key, preset = found
    _, setup_channels, setup_re_ce = preset.wiring()
    by_channel = dict(zip(setup_channels, setup_re_ce))

    channels = args.get("channels")
    if channels is None:
        channels = list(setup_channels)
    else:
        if not isinstance(channels, (list, tuple)) or not channels:
            raise ValueError(
                "channels must be a non-empty list of the setup's working "
                f"electrodes ({setup_channels}) when {SETUP_ARG} is given."
            )
        missing = [ch for ch in channels if ch not in by_channel]
        if missing:
            raise ValueError(
                f"Channel(s) {missing} are not working electrodes of the "
                f"setup {key!r}, which has {setup_channels}. Measure some "
                "of those, or pick another setup."
            )
        channels = list(channels)
    args["channels"] = channels
    args["re_ce_channels"] = [by_channel[ch] for ch in channels]
    args.pop("electrode_config_mode", None)
    return args

"""Anthropic tool definitions and dispatch for the embedded agent.

Single source of truth for the agent tool surface: the JSON-schema tool
definitions sent to the Anthropic API, the registry mapping tool names
to async handlers bound to an :class:`src.agent.engine_adapter.
EngineAdapter`, and the :func:`dispatch_tool` entry point the agent
loop calls to execute a tool.

Design rules (blueprint constraints):

* Importable with NO API key and NO network access -- this module never
  imports ``anthropic`` and performs no I/O at import time.
* Tool results are compact JSON strings; raw data arrays are never
  serialized (the adapter already returns small summary dicts).
* Handler failures NEVER raise into the agent loop: :func:`dispatch_tool`
  converts unknown tools and handler exceptions into
  ``(error_json, is_error=True)`` pairs.
* Batch 3 seam: analysis tools register through
  :meth:`ToolRegistry.register` (or the ``extra_tools`` argument of
  :func:`build_registry`) without touching this file.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

from src.agent.engine_adapter import EngineAdapter
from src.techniques.scripts import EIS_CURRENT_RANGES, EIS_RANGE_MODES

logger = logging.getLogger(__name__)

__all__ = [
    "MEASUREMENT_TECHNIQUES",
    "ToolHandler",
    "ToolRegistry",
    "build_registry",
    "build_tool_defs",
    "dispatch_tool",
]

#: Handlers may be plain callables or coroutine functions; both receive
#: the model-supplied input dict and return a JSON-serializable result.
ToolHandler = Callable[[dict[str, Any]], Union[Any, Awaitable[Any]]]

#: Techniques exposed as run_* tools (each maps to EngineAdapter.run_*).
#: Two were retired from the tool surface on 2026-07-28: GEIS (not a
#: measurement this lab runs) and CP (the EmStat Pico has no galvanostat, so
#: a real call can only return "e!4001 unknown command").  Their generators
#: and ``EngineAdapter`` methods remain for programmatic callers and for a
#: future EmStat4/Nexus; re-adding the name here plus its tool def in
#: :func:`build_tool_defs` brings the tool back.
#:
#: SWV joined on 2026-07-29 (Req 15).  It was NOT a technique port: SWV was
#: already hardware-verified in the script generator, the GUI dropdown and
#: the .pssession exporter, and this tuple was the single place it was
#: missing.  Adding a name here is only HALF the change -- ``build_tool_defs``
#: returns a hand-written list, and ``build_registry`` looks up
#: ``defs[f"run_{technique}"]``, so a name without a matching tool def raises
#: ``KeyError`` at registry-build time.
MEASUREMENT_TECHNIQUES = ("cv", "ca", "swv", "eis")


# ---------------------------------------------------------------------------
# Schema building blocks
# ---------------------------------------------------------------------------

def _channels_prop() -> dict[str, Any]:
    """Schema for the 1-indexed MUX channel list."""
    return {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 16},
        "minItems": 1,
        "description": (
            "1-indexed MUX16 channels to measure, integers 1-16, "
            "e.g. [1, 2]. The channels are visited in order."
        ),
    }


def _mode_prop() -> dict[str, Any]:
    """Schema for the RE/CE wiring mode."""
    return {
        "type": "string",
        "enum": ["external", "on_board", "manual"],
        "description": (
            "RE/CE wiring mode. 'external' (default): RE/CE held at "
            "multiplexer position 15 for every channel. 'on_board': "
            "RE/CE held at position 16. 'manual': RE/CE routed per "
            "channel via re_ce_channels. Leave it out for the first "
            "run unless the user names the wiring; change it when a "
            "run shows the wiring does not match the cell (see the "
            "wiring check in the system prompt)."
        ),
    }


def _re_ce_prop() -> dict[str, Any]:
    """Schema for the manual-mode RE/CE channel list."""
    return {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 16},
        "description": (
            "Required ONLY when electrode_config_mode is 'manual': one "
            "RE/CE channel position (1-16) per entry in channels. Use "
            "the same list as channels to pair each WE with the RE/CE "
            "at its own position."
        ),
    }


def _num(description: str) -> dict[str, Any]:
    """Number property with a description."""
    return {"type": "number", "description": description}


def _int(description: str) -> dict[str, Any]:
    """Integer property with a description."""
    return {"type": "integer", "description": description}


def _cr_prop(technique: str | None = None) -> dict[str, Any]:
    """Schema for the current-range SI string.

    EIS runs high-speed pgstat mode 3, whose current-range ladder differs
    from the low-speed (mode-2) one: mode-2 values like 2u/10u/63u are invalid
    in mode 3 and the device returns NO data. So EIS advertises only the
    mode-3 ranges.
    """
    if (technique or "").lower() == "eis":
        ladder = ", ".join(f"'{r}'" for r in EIS_CURRENT_RANGES)
        return {
            "type": "string",
            "description": (
                "Current range (SI-prefixed). In 'static' mode (default) it is "
                "pinned for the whole sweep; in 'auto' mode it is the maximum "
                "(ceiling) range the autoranger steps down from. EIS runs "
                "mode 3 -- use ONLY: "
                f"{ladder} (default '100u'). Mode-2 values such as "
                "'2u'/'10u'/'63u' return no data. The range must exceed the "
                "highest-frequency current or the sweep under-ranges; the run "
                "result reports per-channel quality and a 'suggested_cr' so "
                "you can re-range. If unsure of the cell's impedance, start "
                "low (e.g. '1u') and step up using 'suggested_cr'."
            ),
        }
    return {
        "type": "string",
        "description": (
            "Maximum current range as an SI-prefixed string, e.g. "
            "'100n', '2u', '8u', '100u', '1m' (default '100u')."
        ),
    }


def _bw_prop() -> dict[str, Any]:
    """Schema for the potentiostat bandwidth."""
    return _num("Potentiostat bandwidth in Hz (default 400).")


def _measurement_schema(specific: dict[str, Any]) -> dict[str, Any]:
    """Common measurement schema (channels + wiring) plus *specific* params."""
    properties: dict[str, Any] = {
        "channels": _channels_prop(),
        "electrode_config_mode": _mode_prop(),
        "re_ce_channels": _re_ce_prop(),
    }
    properties.update(specific)
    return {
        "type": "object",
        "properties": properties,
        "required": ["channels"],
        "additionalProperties": False,
    }


_RUN_RULES = (
    " The device must be connected and idle: call device_status first "
    "if unsure, and never start while another measurement is running "
    "(call abort_measurement first if the user wants to interrupt). "
    "Waits for completion and returns a compact summary (point counts "
    "per channel, variable names); raw data stays inside the app."
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def build_tool_defs() -> list[dict[str, Any]]:
    """Build the Anthropic JSON-schema tool definitions.

    Returns:
        A fresh list of tool definition dicts in the order they are
        presented to the model.  Descriptions are deliberately
        prescriptive about WHEN to call each tool.
    """
    return [
        {
            "name": "run_cv",
            "description": (
                "Run a cyclic voltammetry (CV) measurement on the "
                "EmStat Pico + MUX16. Call this whenever the user asks "
                "to run, start, record, or repeat a CV, voltammogram, "
                "or cyclic potential sweep on one or more channels "
                "(1-16). The potential sweeps e_begin -> e_vertex1 -> "
                "e_vertex2 -> e_begin (all in volts) at scan_rate "
                "(V/s) in e_step (V) increments, repeated n_scans "
                "times. IMPORTANT: the device requires a closed "
                "cycle, so e_vertex2 must equal e_begin; an open "
                "cycle is rejected at script generation on real "
                "hardware. NOISE SCOPE: the result carries a per-channel "
                "'noise' block with a 'ripple_ratio' and 'noise_ok'. To find "
                "clean acquisition settings before the full scan, run a quick "
                "CV over a SMALL potential window (e.g. e_begin=-0.1, "
                "e_vertex1=0.1, e_vertex2=-0.1) and read ripple_ratio; if "
                "noise_ok is false (elevated ripple -- usually 50/60 Hz mains "
                "pickup), lower bw_hz (e.g. 400 -> 40) and re-run, driving "
                "ripple_ratio down, then use that bandwidth for the full CV."
                + _RUN_RULES
            ),
            "input_schema": _measurement_schema({
                "e_begin": _num(
                    "Start potential in volts (default -0.5). Must "
                    "equal e_vertex2 (closed cycle)."
                ),
                "e_vertex1": _num(
                    "First vertex potential in volts (default 0.5)."
                ),
                "e_vertex2": _num(
                    "Second vertex potential in volts (default -0.5). "
                    "Must equal e_begin (closed cycle)."
                ),
                "e_step": _num(
                    "Potential step in volts (default 0.01)."
                ),
                "scan_rate": _num(
                    "Scan rate in volts per second (default 0.1)."
                ),
                "n_scans": _int(
                    "Number of consecutive scans (default 1)."
                ),
                "t_eq": _num(
                    "Equilibration time at e_begin in seconds "
                    "(default 0)."
                ),
                "cr": _cr_prop(),
                "bw_hz": _bw_prop(),
            }),
        },
        {
            "name": "run_ca",
            "description": (
                "Run a chronoamperometry (CA) measurement: hold a "
                "fixed potential e_dc (volts) and record current for "
                "t_run seconds, sampling every t_interval seconds. "
                "Call this whenever the user asks for CA, "
                "amperometry, an i-t curve, or to hold a potential "
                "while logging current on channels 1-16." + _RUN_RULES
            ),
            "input_schema": _measurement_schema({
                "e_dc": _num(
                    "Applied DC potential in volts (default 0.2)."
                ),
                "t_run": _num(
                    "Total run time in seconds (default 10)."
                ),
                "t_interval": _num(
                    "Sampling interval in seconds (default 0.1)."
                ),
                "t_eq": _num(
                    "Equilibration time in seconds (default 0)."
                ),
                "cr": _cr_prop(),
                "bw_hz": _bw_prop(),
            }),
        },
        {
            "name": "run_swv",
            "description": (
                "Run a square-wave voltammetry (SWV) measurement on the "
                "EmStat Pico + MUX16. Call this whenever the user asks to "
                "run, start, or repeat an SWV, square-wave scan, or "
                "square-wave voltammogram on one or more channels (1-16), "
                "and PREFER it over run_cv whenever the goal is to detect "
                "or quantify a faradaic peak -- SWV suppresses the "
                "capacitive background that buries small peaks in a CV, so "
                "it is the technique for aptamer / E-AB sensors, trace "
                "analyte detection, and any 'how big is the peak' "
                "question. The potential steps from e_begin to e_end "
                "(volts) in e_step increments, with a square-wave pulse of "
                "half-peak amplitude (volts) applied at frequency (hertz) "
                "on each step; the reported current is the NET current "
                "(forward pulse minus reverse pulse). Use run_cv instead "
                "for redox reversibility, scan-rate studies, surface "
                "cleaning, or electrode characterization. AUTO-RANGE: the "
                "result carries a per-channel 'quality' block plus "
                "'quality_ok' and, when a channel rails the current range "
                "(overload or NaN), a 'suggested_cr' one rung up the "
                "low-speed ladder. If quality_ok is false the peak height "
                "is CLIPPED and must not be reported: re-run the affected "
                "channel(s) at 'suggested_cr' until quality_ok, and STOP "
                "re-ranging when 'rerange_exhausted' is true (still bad at "
                "the largest range -> the fault is the cell or wiring). "
                "After a clean run, chain export_session -> analyze_swv to "
                "get peak potential, peak current and peak SNR -- do not "
                "eyeball a peak from the summary, which carries no raw "
                "data."
                + _RUN_RULES
            ),
            "input_schema": _measurement_schema({
                "e_begin": _num(
                    "Start potential in volts (default -0.5). SWV sweeps "
                    "in ONE direction, so e_begin and e_end may differ "
                    "freely (unlike CV, which needs a closed cycle)."
                ),
                "e_end": _num(
                    "End potential in volts (default 0.5). Keep the "
                    "expected peak well inside [e_begin, e_end]: a peak "
                    "landing on the first or last point cannot be "
                    "measured, only bounded."
                ),
                "e_step": _num(
                    "Potential step in volts (default 0.005). Smaller "
                    "steps resolve the peak better but lengthen the scan."
                ),
                "amplitude": _num(
                    "Square-wave HALF-peak amplitude in volts (default "
                    "0.025, i.e. 25 mV). Larger amplitude raises the peak "
                    "current but broadens the peak."
                ),
                "frequency": _num(
                    "Square-wave frequency in Hz (default 25). Higher "
                    "frequency scans faster and shifts the kinetic "
                    "window; for E-AB / aptamer work the frequency is "
                    "itself the variable being probed."
                ),
                "t_eq": _num(
                    "Equilibration time at e_begin in seconds "
                    "(default 0)."
                ),
                "cr": _cr_prop(),
                "bw_hz": _bw_prop(),
            }),
        },
        # run_cp was retired from the tool surface on 2026-07-28.  The EmStat
        # Pico has no galvanostat: MethodSCRIPT v1.6 marks meas_loop_cp and
        # set_i unavailable on the Pico, and the device answers
        # "e!4001 unknown command" (bench-confirmed 2026-05-20, the CP task
        # was cancelled on this finding).  Advertising a tool that can only fail
        # invites the agent to burn a turn discovering that.  scripts._gen_cp
        # and EngineAdapter.run_cp are untouched for a future EmStat4/Nexus;
        # restoring the tool means re-adding this definition and "cp" to
        # MEASUREMENT_TECHNIQUES.
        {
            "name": "run_eis",
            "description": (
                "Run a potentiostatic electrochemical impedance "
                "spectroscopy (EIS) measurement: a sinusoidal "
                "perturbation of amplitude e_ac (volts) around DC "
                "bias e_dc (volts), swept from freq_start down to "
                "freq_end (hertz) over n_freq points. Call this "
                "whenever the user asks for EIS, impedance, a "
                "Nyquist or Bode plot, or a frequency sweep on "
                "channels 1-16. AUTO-RANGE: the result carries a "
                "per-channel 'quality' block plus 'quality_ok' and, when a "
                "channel under-ranges (current railed the range -> overload "
                "or NaN), a 'suggested_cr'. If quality_ok is false, re-run the "
                "affected channel(s) at 'suggested_cr' (jump several rungs up "
                "the ladder if most points are bad) and repeat until "
                "quality_ok. STOP re-ranging when 'rerange_exhausted' is true "
                "(still bad at the largest range -> the fault is the "
                "cell/wiring, not the range). If a run instead returns "
                "ok=false with a device error and you used a small range, the "
                "range was likely far too small: re-run at the 'suggested_cr' "
                "on the error before giving up. Report only the good sweep."
                + _RUN_RULES
            ),
            "input_schema": _measurement_schema({
                "e_dc": _num(
                    "DC bias potential in volts (default 0)."
                ),
                "e_ac": _num(
                    "AC amplitude in volts (default 0.01)."
                ),
                "freq_start": _num(
                    "Start (highest) frequency in Hz (default 1e5)."
                ),
                "freq_end": _num(
                    "End (lowest) frequency in Hz (default 0.1)."
                ),
                "n_freq": _int(
                    "Number of frequency points (default 50)."
                ),
                "t_eq": _num(
                    "Equilibration time in seconds (default 0)."
                ),
                "cr": _cr_prop("eis"),
                "eis_range_mode": {
                    "type": "string",
                    "enum": list(EIS_RANGE_MODES),
                    "description": (
                        "Current-range mode for the sweep. 'static' (default) "
                        "PINS 'cr' for the whole sweep -- safer against the "
                        "in-loop range-switch corruption seen on some "
                        "3-electrode cells. 'auto' autoranges from the "
                        "most-sensitive floor UP TO 'cr' as a CEILING (not a "
                        "starting point -- it only steps down from 'cr' as the "
                        "low-frequency current falls), needed for "
                        "high-impedance 2-electrode IDE cells whose "
                        "low-frequency current is far below 'cr' (otherwise a "
                        "scattered/negative -Z'' tail). Prefer 'static' unless "
                        "the low-frequency points come back noisy."
                    ),
                },
            }),
        },
        # run_geis was retired from the tool surface on 2026-07-28 (GEIS is
        # not a measurement this lab runs).  EngineAdapter.run_geis and
        # scripts._gen_geis are untouched, so restoring the tool means
        # re-adding this definition and "geis" to MEASUREMENT_TECHNIQUES.
        {
            "name": "list_ports",
            "description": (
                "Enumerate the serial ports visible on this machine "
                "(device name, description, hardware id). Call this "
                "FIRST whenever the user wants to connect to the "
                "instrument and the port is not already known, or "
                "when a connection attempt failed and you need to "
                "show what is available. Read-only and always safe."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "connect_device",
            "description": (
                "Open the serial connection to the EmStat Pico on "
                "the given port. Call this when device_status shows "
                "the instrument is not connected and the user wants "
                "to connect or to run a measurement; pick the port "
                "from list_ports if the user did not name one. "
                "Refused while a measurement is running."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "port": {
                        "type": "string",
                        "description": (
                            "Serial port name, e.g. 'COM6' on "
                            "Windows or '/dev/ttyUSB0' on Linux."
                        ),
                    },
                },
                "required": ["port"],
                "additionalProperties": False,
            },
        },
        {
            "name": "disconnect_device",
            "description": (
                "Close the serial connection to the EmStat Pico. "
                "Call this ONLY when the user explicitly asks to "
                "disconnect, release the port, or power down the "
                "setup. Refused while a measurement is running "
                "(abort_measurement first)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "device_status",
            "description": (
                "Report the instrument state: connected flag, port, "
                "firmware version, serial number, and whether a "
                "measurement is currently running. Call this BEFORE "
                "starting any measurement or connect/disconnect, "
                "after finishing one, and whenever the user asks "
                "about the device. Read-only, instant, never "
                "disturbs a running measurement."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "abort_measurement",
            "description": (
                "Abort the currently running measurement "
                "immediately (cell is switched off safely). Call "
                "this when the user asks to stop, cancel, halt, or "
                "abort a run, or before starting a new measurement "
                "when device_status reports one is still running. "
                "Returns ok=false if nothing was running."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "export_session",
            "description": (
                "Save the most recent FINISHED measurement to a "
                "PSTrace-compatible .pssession file and return its "
                "absolute path. Call this after a run completes "
                "whenever the user wants the data saved, and as the "
                "FIRST step of characterization: the analyze_* tools "
                "only read .pssession files, so chain run_* -> "
                "export_session -> analyze_* (pass the returned "
                "path). Fails cleanly if a measurement is still "
                "running or none has finished yet."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional output file or directory. A "
                            "directory gets an auto-generated "
                            "<technique>_<timestamp>.pssession name; "
                            "omitted entirely, the file goes to "
                            "./agent_exports/."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Ordered mapping of Anthropic tool definitions to async handlers.

    This is the single source of truth consumed by the agent loop
    (``tools=registry.tool_defs`` on every request, handler lookup in
    :func:`dispatch_tool`).

    Batch 3 seam: analysis tools plug in via :meth:`register` -- either
    directly on a built registry or through the ``extra_tools``
    parameter of :func:`build_registry`.
    """

    def __init__(self) -> None:
        self._defs: list[dict[str, Any]] = []
        self._handlers: dict[str, ToolHandler] = {}

    def register(
        self, tool_def: dict[str, Any], handler: ToolHandler
    ) -> None:
        """Register one tool definition with its handler.

        Args:
            tool_def: Anthropic tool dict with ``name``,
                ``description`` and an object-typed ``input_schema``.
            handler: Callable (sync or async) taking the tool input
                dict and returning a JSON-serializable result.

        Raises:
            ValueError: On a malformed definition, duplicate name, or
                non-callable handler.
        """
        name = tool_def.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool_def requires a non-empty string 'name'.")
        if name in self._handlers:
            raise ValueError(f"Duplicate tool name: {name!r}.")
        if not tool_def.get("description"):
            raise ValueError(f"Tool {name!r} requires a description.")
        schema = tool_def.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(
                f"Tool {name!r} input_schema must be a JSON schema "
                "with type 'object'."
            )
        if not callable(handler):
            raise ValueError(f"Handler for tool {name!r} must be callable.")
        self._defs.append(tool_def)
        self._handlers[name] = handler

    @property
    def tool_defs(self) -> list[dict[str, Any]]:
        """Tool definitions in registration order (shallow copy)."""
        return list(self._defs)

    def get(self, name: str) -> Optional[ToolHandler]:
        """Return the handler for *name*, or None when unknown."""
        return self._handlers.get(name)

    def names(self) -> list[str]:
        """Sorted registered tool names."""
        return sorted(self._handlers)

    def __contains__(self, name: object) -> bool:
        return name in self._handlers

    def __len__(self) -> int:
        return len(self._handlers)


def _make_measurement_handler(
    adapter: EngineAdapter, technique: str
) -> ToolHandler:
    """Build the async handler for one run_* measurement tool.

    The flat tool input is split into config keys (``channels``,
    ``electrode_config_mode``, ``re_ce_channels``) and technique
    parameters, then forwarded to the matching ``EngineAdapter.run_*``
    coroutine.
    """
    runner = getattr(adapter, f"run_{technique}")

    async def handler(tool_input: dict[str, Any]) -> dict[str, Any]:
        args = dict(tool_input or {})
        channels = args.pop("channels", None)
        mode = args.pop("electrode_config_mode", None)
        re_ce = args.pop("re_ce_channels", None)
        return await runner(
            args,
            channels=channels,
            electrode_config_mode=mode,
            re_ce_channels=re_ce,
        )

    return handler


def build_registry(
    adapter: EngineAdapter,
    extra_tools: Optional[
        Iterable[tuple[dict[str, Any], ToolHandler]]
    ] = None,
) -> ToolRegistry:
    """Build the standard registry bound to *adapter*.

    Args:
        adapter: The :class:`EngineAdapter` the handlers close over
            (real or mock engine behind it).
        extra_tools: Optional ``(tool_def, handler)`` pairs appended
            after the built-in tools -- the Batch 3 analysis tools
            plug in here (or call ``registry.register`` later).

    Returns:
        A :class:`ToolRegistry` with the ten built-in tools (plus any
        extras) registered: one ``run_*`` per entry in
        :data:`MEASUREMENT_TECHNIQUES` (currently four), plus
        ``list_ports``, ``connect_device``, ``disconnect_device``,
        ``device_status``, ``abort_measurement`` and ``export_session``.
    """
    defs = {d["name"]: d for d in build_tool_defs()}
    registry = ToolRegistry()

    for technique in MEASUREMENT_TECHNIQUES:
        registry.register(
            defs[f"run_{technique}"],
            _make_measurement_handler(adapter, technique),
        )

    async def _list_ports(_tool_input: dict[str, Any]) -> dict[str, Any]:
        return adapter.list_ports()

    async def _connect(tool_input: dict[str, Any]) -> dict[str, Any]:
        port = (tool_input or {}).get("port")
        if not port:
            return {
                "ok": False,
                "error": (
                    "connect_device requires a 'port' string; call "
                    "list_ports to discover available ports."
                ),
            }
        return await adapter.connect_device(str(port))

    async def _disconnect(_tool_input: dict[str, Any]) -> dict[str, Any]:
        return await adapter.disconnect_device()

    async def _status(_tool_input: dict[str, Any]) -> dict[str, Any]:
        return adapter.device_status()

    async def _abort(_tool_input: dict[str, Any]) -> dict[str, Any]:
        return adapter.abort_measurement()

    async def _export(tool_input: dict[str, Any]) -> dict[str, Any]:
        return adapter.export_session((tool_input or {}).get("path"))

    registry.register(defs["list_ports"], _list_ports)
    registry.register(defs["connect_device"], _connect)
    registry.register(defs["disconnect_device"], _disconnect)
    registry.register(defs["device_status"], _status)
    registry.register(defs["abort_measurement"], _abort)
    registry.register(defs["export_session"], _export)

    for tool_def, handler in extra_tools or ():
        registry.register(tool_def, handler)
    return registry


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dumps(obj: Any) -> str:
    """Compact, deterministic JSON serialization for tool results."""
    return json.dumps(
        obj, separators=(",", ":"), sort_keys=True, default=str
    )


async def dispatch_tool(
    registry: ToolRegistry, name: str, tool_input: Any
) -> tuple[str, bool]:
    """Execute one tool call and return ``(result_json, is_error)``.

    Never raises into the caller: unknown tools and handler exceptions
    are converted into structured error JSON with ``is_error=True``.
    Handlers returning structured failures (``{"ok": false, ...}``)
    also carry ``is_error=True`` so the agent loop emits
    tool_call_error and the model sees an explicit error result
    (engine busy, invalid parameters, ...).

    Args:
        registry: The registry to resolve *name* in.
        name: Tool name from the model's tool_use block.
        tool_input: Tool input dict from the model (None tolerated).

    Returns:
        Tuple of the compact JSON result string and the error flag.
    """
    handler = registry.get(name)
    if handler is None:
        message = (
            f"Unknown tool {name!r}. Available tools: "
            f"{', '.join(registry.names())}."
        )
        logger.warning("dispatch_tool: %s", message)
        return _dumps({"ok": False, "error": message}), True

    if not isinstance(tool_input, dict):
        tool_input = {} if tool_input is None else {"value": tool_input}

    try:
        result = handler(tool_input)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 - must not reach the agent loop
        logger.exception("Tool %r raised", name)
        return (
            _dumps({
                "ok": False,
                "error": f"Tool {name!r} failed: "
                         f"{type(exc).__name__}: {exc}",
            }),
            True,
        )

    try:
        # Structured failures from the adapter ({"ok": False, ...}) are
        # flagged as errors so tool cards and the model both see them
        # as failures rather than successful results.
        failed = isinstance(result, dict) and result.get("ok") is False
        return _dumps(result), failed
    except (TypeError, ValueError) as exc:
        logger.exception("Tool %r returned unserializable result", name)
        return (
            _dumps({
                "ok": False,
                "error": (
                    f"Tool {name!r} returned a non-JSON-serializable "
                    f"result: {exc}"
                ),
            }),
            True,
        )

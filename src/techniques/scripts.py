"""MethodSCRIPT generator for all electrochemical techniques.

Generates parameterised MethodSCRIPT programs for the EmStat Pico.
Each script follows the template:

    preamble  (pgstat config, current range, cell_on)
    technique (measurement loop with pck_start/pck_add/pck_end)
    postamble (on_finished: cell_off)

Values are formatted with MethodSCRIPT SI prefix notation (e.g.,
``500m`` for 0.5 V). Integer values are suffixed with ``i``.

The public API consists of three functions:

- ``generate(technique, params, channels)`` -- full script lines
- ``supported_techniques()`` -- list of technique keys
- ``technique_params(technique)`` -- default parameters for a technique
"""

from __future__ import annotations

import logging
import math
from typing import Any

from src.comms.mux import MuxController

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SI prefix formatting
# ---------------------------------------------------------------------------

# Ordered from largest to smallest magnitude.
_SI_TABLE: list[tuple[str, int]] = [
    ("E", 18),
    ("P", 15),
    ("T", 12),
    ("G", 9),
    ("M", 6),
    ("k", 3),
    (" ", 0),
    ("m", -3),
    ("u", -6),
    ("n", -9),
    ("p", -12),
    ("f", -15),
    ("a", -18),
]


def _format_si(value: float) -> str:
    """Format a float using MethodSCRIPT SI prefix notation.

    MethodSCRIPT values are an INTEGER mantissa followed by an SI prefix
    (``'2565'``, ``'100k'``, ``'500m'``); a decimal point in the mantissa is a
    device syntax error (``'2.565k'`` is rejected with ``e!4004``). This selects
    the most compact prefix that yields a whole-number mantissa, rounding to
    integer units when the value cannot be represented exactly.

    Args:
        value: The numeric value to format.

    Returns:
        String such as ``'500m'``, ``'1'``, ``'2565'``, or ``'-200u'``.

    Examples:
        >>> _format_si(0.5)
        '500m'
        >>> _format_si(0.001)
        '1m'
        >>> _format_si(-0.0002)
        '-200u'
        >>> _format_si(0.0)
        '0m'
        >>> _format_si(2565.0)
        '2565'
        >>> _format_si(100000.0)
        '100k'

    A non-round value keeps full precision via a smaller prefix rather than a
    rejected decimal (``2565.02`` -> ``'2565020m'``).
    """
    if value == 0.0:
        return "0m"

    abs_val = abs(value)

    # Walk prefixes largest -> smallest. Prefer the most compact prefix whose
    # mantissa is a whole number: '2565' (unity) not '2.565k', because the
    # device rejects a decimal point in the mantissa.
    natural_mantissa = None
    natural_pfx = None
    for prefix, exp in _SI_TABLE:
        scaled = abs_val / (10**exp)
        if scaled < 1.0 and exp != -18:
            continue  # prefix too large; mantissa would be < 1
        mantissa = value / (10**exp)
        # Unity prefix: use bare number (no trailing space)
        pfx = "" if prefix == " " else prefix
        if natural_mantissa is None:
            natural_mantissa, natural_pfx = mantissa, pfx
        if abs(mantissa - round(mantissa)) < 1e-9:
            return f"{int(round(mantissa))}{pfx}"

    # No prefix yields an exact integer mantissa. Round rather than emit a
    # device-rejected decimal: for |value| >= 1 round to whole units and
    # re-format (2565.02 -> 2565 -> '2565'); for sub-unit values round the
    # natural 1..1000 mantissa (0.523 -> '523m').
    if abs_val >= 1.0:
        rounded = float(round(value))
        if rounded != value:
            return _format_si(rounded)
    return f"{int(round(natural_mantissa))}{natural_pfx}"


def _indent(lines: list[str]) -> list[str]:
    """Indent MethodSCRIPT lines with two spaces for loop body."""
    return [f"  {line}" for line in lines]


def _format_int(value: int) -> str:
    """Format an integer with the MethodSCRIPT ``i`` suffix.

    Args:
        value: Integer value.

    Returns:
        String like ``'10i'`` or ``'0i'``.
    """
    return f"{value}i"


# ---------------------------------------------------------------------------
# EIS/GEIS current-range ladder (high-speed pgstat mode 3)
# ---------------------------------------------------------------------------

# EIS and GEIS run the potentiostat in HIGH-SPEED mode 3, which exposes a
# DIFFERENT current-range ladder from the low-speed (mode-2) techniques.
# Mode-2 values such as 2u/63u are INVALID in mode 3 and the device returns
# no data, while valid mode-3 ranges (1u/6u/13u/50u/200u/5m) are absent from
# the mode-2 ladder. This is the single source of truth for the mode-3 ranges:
# the GUI dropdown (parameter_form), the agent tool schema (tools), and the
# agent's quality/auto-range summary (engine_adapter) all import it from here
# so they cannot drift. Ordered smallest -> largest (ascending full-scale).
EIS_CURRENT_RANGES: list[str] = [
    "100n", "1u", "6u", "13u", "25u", "50u", "100u", "200u", "1m", "5m",
]

# Low-speed (pgstat mode 2) current-range ladder — CV, CA, SWV and every
# other non-EIS technique. Ordered smallest -> largest (ascending full
# scale). It lived only in the GUI (`parameter_form.CURRENT_RANGES`) until
# 2026-07-29, which put it out of reach of the agent layer; it now sits
# beside the mode-3 ladder in the module that owns mode/range knowledge, and
# the GUI imports it from here. Do NOT re-inline a copy: mode-2 and mode-3
# ladders are genuinely different (2u/63u are mode-2-only, 6u/200u/5m are
# mode-3-only), and two copies of either is how they drift apart.
CURRENT_RANGES: list[str] = [
    "100n", "2u", "4u", "8u", "16u",
    "32u", "63u", "100u", "1m", "10m", "100m",
]

# EIS current-range selection modes (GUI dropdown + agent tool enum). Single
# source of truth so the GUI and agent cannot drift. "static" pins the chosen
# `cr` for the whole sweep; "auto" autoranges from the most-sensitive floor
# (1n) UP TO `cr` as a ceiling, stepping DOWN as the low-frequency current
# falls. Default is "static" (first entry). See `_preamble_eis`.
EIS_RANGE_MODES: list[str] = ["static", "auto"]


def next_larger_eis_range(cr: str) -> str | None:
    """Return the next-larger EIS current range above ``cr``, or None.

    Used by the agent layer to suggest a re-range after an under-ranged
    EIS sweep (current railed the pinned range). Steps one rung up the
    mode-3 ladder (:data:`EIS_CURRENT_RANGES`).

    Args:
        cr: The current range that was used (an SI string like ``'1u'``).
            Matching is exact against the mode-3 ladder.

    Returns:
        The next range string up the ladder (e.g. ``'1u'`` -> ``'6u'``),
        or ``None`` when ``cr`` is already the largest range (``'5m'``)
        or is not a recognised mode-3 range (caller should fall back to a
        sensible mid-ladder default such as ``'100u'``).
    """
    try:
        idx = EIS_CURRENT_RANGES.index(str(cr))
    except ValueError:
        return None
    if idx + 1 < len(EIS_CURRENT_RANGES):
        return EIS_CURRENT_RANGES[idx + 1]
    return None


def next_larger_current_range(cr: str) -> str | None:
    """Return the next-larger mode-2 current range above ``cr``, or None.

    The low-speed counterpart of :func:`next_larger_eis_range`, used by the
    agent layer to suggest a re-range after an under-ranged CV/CA/SWV run
    (the current railed the range). Steps one rung up
    :data:`CURRENT_RANGES`.

    Args:
        cr: The current range that was used (an SI string like ``'8u'``).
            Matching is exact against the mode-2 ladder, so a mode-3-only
            value such as ``'6u'`` deliberately does NOT match.

    Returns:
        The next range string up the ladder (e.g. ``'8u'`` -> ``'16u'``),
        or ``None`` when ``cr`` is already the largest range (``'100m'``)
        or is not a recognised mode-2 range (caller should fall back to a
        sensible mid-ladder default such as ``'100u'``).
    """
    try:
        idx = CURRENT_RANGES.index(str(cr))
    except ValueError:
        return None
    if idx + 1 < len(CURRENT_RANGES):
        return CURRENT_RANGES[idx + 1]
    return None


# ---------------------------------------------------------------------------
# Default parameters per technique
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, dict[str, Any]] = {
    "lsv": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_end": 0.5,
        "e_step": 0.01,
        "scan_rate": 0.1,
        "cr": "100u",
        "bw_hz": 400,
    },
    "dpv": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_end": 0.5,
        "e_step": 0.005,
        "e_pulse": 0.05,
        "t_pulse": 0.05,
        "scan_rate": 0.05,
        "cr": "100u",
        "bw_hz": 400,
    },
    "swv": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_end": 0.5,
        "e_step": 0.005,
        "amplitude": 0.025,
        "frequency": 25.0,
        "cr": "100u",
        "bw_hz": 400,
    },
    "npv": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_end": 0.5,
        "e_step": 0.01,
        "e_pulse": 0.05,
        "t_pulse": 0.05,
        "t_base": 0.5,
        "cr": "100u",
        "bw_hz": 400,
    },
    "acv": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_end": 0.5,
        "e_step": 0.005,
        "amplitude": 0.01,
        "frequency": 50.0,
        "cr": "100u",
        "bw_hz": 400,
    },
    "cv": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_vertex1": 0.5,
        "e_vertex2": -0.5,
        "e_step": 0.01,
        "scan_rate": 0.1,
        "n_scans": 1,
        "cr": "100u",
        "bw_hz": 400,
    },
    "ca": {
        "t_eq": 0.0,
        "e_dc": 0.2,
        "t_run": 10.0,
        "t_interval": 0.1,
        "cr": "100u",
        "bw_hz": 400,
    },
    "fca": {
        "t_eq": 0.0,
        "e_dc": 0.2,
        "t_run": 10.0,
        "t_interval": 0.1,
        "cr": "100u",
        "bw_hz": 400,
    },
    "cp": {
        "t_eq": 0.0,
        "i_dc": 0.0001,
        "t_run": 10.0,
        "t_interval": 0.1,
        "cr": "100u",
        "bw_hz": 400,
    },
    "ocp": {
        "t_eq": 0.0,
        "t_run": 60.0,
        "t_interval": 1.0,
        "bw_hz": 400,
    },
    "eis": {
        "t_eq": 0.0,
        "e_dc": 0.0,
        "e_ac": 0.01,
        "freq_start": 100000.0,
        "freq_end": 0.1,
        "n_freq": 50,
        "cr": "100u",
        "eis_range_mode": "static",
    },
    "geis": {
        "t_eq": 0.0,
        "i_dc": 0.0,
        "i_ac": 0.00001,
        "freq_start": 100000.0,
        "freq_end": 0.1,
        "n_freq": 50,
        "cr": "100u",
    },
    "pad": {
        "e_cond": -0.2,
        "t_cond": 0.5,
        "e_dep": -0.5,
        "t_dep": 5.0,
        "e_eq": -0.2,
        "t_eq": 1.0,
        "e_begin": -0.5,
        "e_end": 0.5,
        "e_step": 0.005,
        "e_pulse": 0.05,
        "t_pulse": 0.05,
        "scan_rate": 0.05,
        "cr": "100u",
        "bw_hz": 400,
    },
    "lsp": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_end": 0.5,
        "e_step": 0.01,
        "scan_rate": 0.1,
        "cr": "100u",
        "bw_hz": 400,
    },
    "fcv": {
        "t_eq": 0.0,
        "e_begin": -0.5,
        "e_vertex1": 0.5,
        "e_vertex2": -0.5,
        "e_step": 0.01,
        "scan_rate": 1.0,
        "n_scans": 1,
        "cr": "100u",
        "bw_hz": 400,
    },
    # MUX round-robin (continuous mode)
    "ca_alt_mux": {
        "t_eq": 0.0,
        "e_dc": 0.2,
        "t_run": 300.0,
        "t_interval": 0.1,
        "settle_time": 0.1,
        # samples_per_visit > 1 acquires N samples per channel per round
        # and averages them in the engine. Acts as a built-in anti-alias
        # filter (box-car) for stirrer-induced periodic noise.
        "samples_per_visit": 1,
        "cr": "100u",
        "bw_hz": 400,
    },
}

# ---------------------------------------------------------------------------
# Technique script builders
# ---------------------------------------------------------------------------


def _preamble(params: dict[str, Any]) -> list[str]:
    """Build the standard script preamble.

    Configures both potentiostat channels (chan 1 off, chan 0 in
    potentiostatic mode), sets current range, then turns cell on.
    The dual-channel setup is required for MUX16 operation.

    The ``set_max_bandwidth`` line is parameterised via ``bw_hz``
    (default 400 Hz) so callers can sweep the mode-2 bandwidth
    setting without code edits. Mode-3 preambles (``_preamble_eis``
    and ``_preamble_galvano``) remain hardcoded at 200 kHz for
    control-loop stability and must NOT use this parameter.
    """
    lines: list[str] = []
    lines.append("e")
    cr = params.get("cr", "100u")
    bw = _format_si(params.get("bw_hz", 400))
    lines.append("var p")
    lines.append("var c")
    lines.append("set_pgstat_chan 1")
    lines.append("set_pgstat_mode 0")
    lines.append("set_pgstat_chan 0")
    lines.append("set_pgstat_mode 2")
    lines.append(f"set_max_bandwidth {bw}")
    lines.append("set_pot_range -1 1")
    lines.append(f"set_cr {cr}")
    lines.append(f"set_autoranging 100n {cr}")
    lines.append("cell_on")
    return lines


#: Techniques whose ``t_eq`` wait is emitted INSIDE the technique body
#: rather than by :func:`generate`, so it lands after the MUX has actually
#: selected the target channel.  See :func:`_gen_eis`.
_SELF_EQUILIBRATING = ("eis",)


def _preamble_eis(params: dict[str, Any]) -> list[str]:
    """Build preamble for EIS (high-speed mode required).

    Current-range policy is selectable via ``eis_range_mode``
    (``"static"`` | ``"auto"``):

    * ``"static"`` (default) -- PIN the range for the whole sweep
      (``set_autoranging ba {cr} {cr}``, min==max disables autoranging per
      MethodSCRIPT v1.6 sec 14.50). In-loop range switching corrupts EIS on
      FW 1.6.01 for some cells -- the low-frequency Nyquist arc reverses/breaks
      and Z' goes negative (3-electrode, bench 2026-06-20). ``{cr}`` is held for
      the whole sweep and must be chosen so the highest-frequency current does
      not overload.
    * ``"auto"`` -- real autoranging from the most-sensitive floor (``1n``) up
      to ``{cr}`` as the CEILING (``set_autoranging ba 1n {cr}``). ``{cr}`` is
      the max range here, not a starting point: the autoranger only steps DOWN
      from it as the current falls. A single pinned range is badly mismatched to
      the multi-decade |Z| span of a real sweep: e.g. a high-impedance IDE runs
      ~73 uA at 100 kHz down to ~70 nA at low frequency, so a pinned 100u range
      reads the low-frequency points at <0.1% of full scale, burying them in
      quantization noise (the scattered/negative -Z'' tail). Autoranging
      restores low-frequency SNR. This is the original pre-PR#18 behaviour,
      restored as a selectable mode for 2-electrode IDE work (validated clean,
      FW 1.6.01).
    """
    lines: list[str] = []
    lines.append("e")
    cr = params.get("cr", "100u")
    autorange = str(params.get("eis_range_mode", "static")).lower() == "auto"
    lines.append("var p")
    lines.append("var c")
    lines.append("set_pgstat_chan 1")
    lines.append("set_pgstat_mode 0")
    lines.append("set_pgstat_chan 0")
    lines.append("set_pgstat_mode 3")  # high speed for EIS
    lines.append("set_max_bandwidth 200k")
    lines.append("set_range_minmax da 0 0")
    lines.append(f"set_range ba {cr}")
    if autorange:
        lines.append(f"set_autoranging ba 1n {cr}")
    else:
        lines.append(f"set_autoranging ba {cr} {cr}")
    lines.append("cell_on")
    return lines


def _preamble_galvano(
    params: dict[str, Any], pin: bool = False
) -> list[str]:
    """Build preamble for galvanostatic techniques (CP, GEIS).

    Configures chan 1 off, chan 0 in galvanostatic mode. ``pin=True`` (GEIS)
    disables current autoranging (``set_autoranging ba {cr} {cr}``, min==max)
    like EIS -- GEIS runs high-speed mode 3, where in-loop range switching
    corrupts the spectrum on FW 1.6.01; CP keeps bounded autoranging.
    """
    lines: list[str] = []
    lines.append("e")
    cr = params.get("cr", "100u")
    lines.append("var p")
    lines.append("var c")
    lines.append("set_pgstat_chan 1")
    lines.append("set_pgstat_mode 0")
    lines.append("set_pgstat_chan 0")
    lines.append("set_pgstat_mode 3")
    lines.append("set_max_bandwidth 200k")
    lines.append("set_range_minmax da 0 0")
    lines.append(f"set_range ba {cr}")
    if pin:
        lines.append(f"set_autoranging ba {cr} {cr}")
    else:
        lines.append(f"set_autoranging ba 100n {cr}")
    lines.append("cell_on")
    return lines


def _preamble_geis(params: dict[str, Any]) -> list[str]:
    """GEIS preamble: galvanostatic mode with the current range PINNED.

    GEIS runs mode 3 like EIS and inherits the same in-loop autoranging
    corruption, so the range is pinned. NOT yet hardware-validated (see
    :func:`_gen_geis`); pinning is strictly safer than autoranging.
    """
    return _preamble_galvano(params, pin=True)


def _preamble_ocp() -> list[str]:
    """Build preamble for OCP (no cell_on, open circuit).

    Configures chan 1 off, chan 0 in high-impedance mode.
    """
    lines: list[str] = []
    lines.append("e")
    lines.append("var p")
    lines.append("var c")
    lines.append("set_pgstat_chan 1")
    lines.append("set_pgstat_mode 0")
    lines.append("set_pgstat_chan 0")
    lines.append("set_pgstat_mode 2")
    return lines


def _postamble() -> list[str]:
    """Build the standard script postamble with safety cell_off."""
    return [
        "on_finished:",
        "  cell_off",
    ]


def _pck_voltammetry() -> list[str]:
    """Packet config for voltammetric techniques (potential + current)."""
    return [
        "pck_start",
        "pck_add p",
        "pck_add c",
        "pck_end",
    ]


def _pck_amperometry() -> list[str]:
    """Packet config for amperometric techniques (time + current)."""
    return [
        "pck_start",
        "pck_add p",
        "pck_add c",
        "pck_end",
    ]


def _pck_potentiometry() -> list[str]:
    """Packet config for potentiometric techniques (time + potential)."""
    return [
        "pck_start",
        "pck_add p",
        "pck_add c",
        "pck_end",
    ]


def _pck_eis() -> list[str]:
    """Packet config for EIS — uses 3 variables (h, r, j)."""
    return [
        "pck_start",
        "pck_add h",
        "pck_add r",
        "pck_add j",
        "pck_end",
    ]


# ---------------------------------------------------------------------------
# Individual technique generators
# ---------------------------------------------------------------------------


def _gen_lsv(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_lsv script body."""
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_end = _format_si(params.get("e_end", 0.5))
    e_step = _format_si(params.get("e_step", 0.01))
    scan_rate = _format_si(params.get("scan_rate", 0.1))
    lines: list[str] = []
    lines.append(
        f"meas_loop_lsv p c {e_begin} {e_end} {e_step} {scan_rate}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


def _gen_dpv(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_dpv script body."""
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_end = _format_si(params.get("e_end", 0.5))
    e_step = _format_si(params.get("e_step", 0.005))
    e_pulse = _format_si(params.get("e_pulse", 0.05))
    t_pulse = _format_si(params.get("t_pulse", 0.05))
    scan_rate = _format_si(params.get("scan_rate", 0.05))
    lines: list[str] = []
    lines.append(
        f"meas_loop_dpv p c {e_begin} {e_end} {e_step}"
        f" {e_pulse} {t_pulse} {scan_rate}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


def _gen_swv(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_swv script body."""
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_end = _format_si(params.get("e_end", 0.5))
    e_step = _format_si(params.get("e_step", 0.005))
    amplitude = _format_si(params.get("amplitude", 0.025))
    frequency = _format_si(params.get("frequency", 25.0))
    lines: list[str] = []
    lines.append(
        f"meas_loop_swv p c f r {e_begin} {e_end} {e_step}"
        f" {amplitude} {frequency}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


def _gen_npv(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_npv script body."""
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_end = _format_si(params.get("e_end", 0.5))
    e_step = _format_si(params.get("e_step", 0.01))
    e_pulse = _format_si(params.get("e_pulse", 0.05))
    t_pulse = _format_si(params.get("t_pulse", 0.05))
    t_base = _format_si(params.get("t_base", 0.5))
    lines: list[str] = []
    lines.append(
        f"meas_loop_npv p c {e_begin} {e_end} {e_step}"
        f" {e_pulse} {t_pulse} {t_base}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


def _gen_acv(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_acv script body."""
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_end = _format_si(params.get("e_end", 0.5))
    e_step = _format_si(params.get("e_step", 0.005))
    amplitude = _format_si(params.get("amplitude", 0.01))
    frequency = _format_si(params.get("frequency", 50.0))
    lines: list[str] = []
    lines.append(
        f"meas_loop_acv p c {e_begin} {e_end} {e_step}"
        f" {amplitude} {frequency}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


def _validate_cv_cycle(params: dict[str, Any]) -> None:
    """Validate that a CV sweep forms a closed cycle.

    The EmStat Pico runs the cyclic sweep ``e_begin → e_vertex1 →
    e_vertex2 → e_begin``, so the return vertex (``e_vertex2``) must
    equal ``e_begin``. An open cycle is rejected by the firmware with
    the generic ``!0007`` (invalid-parameter) error pointing only at
    the ``meas_loop_cv`` line, which is hard to diagnose — so we fail
    loudly here instead. Hardware-confirmed 2026-05-31.

    Args:
        params: Technique parameters; ``e_begin`` and ``e_vertex2``
            fall back to the CV defaults if absent.

    Raises:
        ValueError: If ``e_begin`` and ``e_vertex2`` differ.
    """
    e_begin = float(params.get("e_begin", -0.5))
    e_vertex2 = float(params.get("e_vertex2", -0.5))
    if e_begin != e_vertex2:
        raise ValueError(
            f"E begin and E vertex 2 must match! CV requires a closed "
            f"cycle (E begin = E vertex 2). Got E begin = {e_begin} V, "
            f"E vertex 2 = {e_vertex2} V."
        )


def _gen_cv(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_cv script body.

    For ``n_scans > 1`` we unroll the scans in Python (N copies of the
    meas_loop_cv block) rather than wrapping in a device-side ``loop n``.
    Why: firmware espico1601 does not iterate the outer compact MUX loop
    (``loop i <= e``) when a nested counted ``loop n < Ni`` sits inside
    it — only the first channel runs, then the script ends. Diagnosed
    2026-05-17 via marker-log capture. The compact MUX wrap folds the
    channel dimension into a single device-side loop, so script length
    scales with N only (not N × channels) and the buffer-overflow
    concern that originally motivated the device-side wrap is avoided.
    """
    _validate_cv_cycle(params)
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_vertex1 = _format_si(params.get("e_vertex1", 0.5))
    e_vertex2 = _format_si(params.get("e_vertex2", -0.5))
    e_step = _format_si(params.get("e_step", 0.01))
    scan_rate = _format_si(params.get("scan_rate", 0.1))
    n_scans = max(1, int(params.get("n_scans", 1)))
    scan_block = [
        f"meas_loop_cv p c {e_begin} {e_vertex1} {e_vertex2}"
        f" {e_step} {scan_rate}",
        *_indent(_pck_voltammetry()),
        "endloop",
    ]
    return scan_block * n_scans


def _gen_ca(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_ca script body."""
    e_dc = _format_si(params.get("e_dc", 0.2))
    t_run = _format_si(params.get("t_run", 10.0))
    t_interval = _format_si(params.get("t_interval", 0.1))
    lines: list[str] = []
    lines.append(
        f"meas_loop_ca p c {e_dc} {t_interval} {t_run}"
    )
    lines.extend(_indent(_pck_amperometry()))
    lines.append("endloop")
    return lines


def _gen_fca(params: dict[str, Any]) -> list[str]:
    """Fast Chronoamperometry — not supported on the EmStat Pico.

    The previous implementation emitted ``meas_loop_fca``, which is not a
    valid MethodSCRIPT command (the device rejects it with ``e!4001``).
    The real Fast-CA command is ``meas_fast_ca`` (MethodSCRIPT v1.6
    §14.40): it is **array-based** (results stored in an ``array``, no
    in-loop packets) and marked "EmStat Pico N" — it requires an EmStat4
    or Nexus. Rather than emit a script the Pico can't run, fail clearly.

    Raises:
        ValueError: always — FCA is unavailable on the EmStat Pico.
    """
    raise ValueError(
        "Fast Chronoamperometry (FCA) is not supported on the EmStat "
        "Pico. It requires the array-based 'meas_fast_ca' command "
        "(EmStat4/Nexus only). Use standard CA ('ca') instead."
    )


def _gen_cp(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_cp script body.

    Per MethodSCRIPT v1.6 §14.41 the argument order is
    ``p c <i_dc> <interval> <run_time>`` (interval BEFORE run, matching
    the CA family) — the previous order had interval/run swapped.

    NOTE: Chronopotentiometry needs a galvanostat and is NOT supported on
    the EmStat Pico (manual: "EmStat Pico N"); the script is valid for
    EmStat4/Nexus, and the Pico will reject it at runtime.
    """
    i_dc = _format_si(params.get("i_dc", 0.0001))
    t_run = _format_si(params.get("t_run", 10.0))
    t_interval = _format_si(params.get("t_interval", 0.1))
    lines: list[str] = []
    lines.append(
        f"meas_loop_cp p c {i_dc} {t_interval} {t_run}"
    )
    lines.extend(_indent(_pck_potentiometry()))
    lines.append("endloop")
    return lines


def _gen_ocp(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_ocp script body.

    Per MethodSCRIPT v1.6 §14.44, ``meas_loop_ocp`` takes a SINGLE output
    variable (the measured RE potential) followed by ``interval`` then
    ``run_time`` — no current variable (it is an open-circuit reading).
    Supported on the EmStat Pico.
    """
    t_run = _format_si(params.get("t_run", 60.0))
    t_interval = _format_si(params.get("t_interval", 1.0))
    lines: list[str] = []
    lines.append(f"meas_loop_ocp p {t_interval} {t_run}")
    # OCP measures potential only (no current at open circuit).
    lines.extend(_indent(["pck_start", "pck_add p", "pck_end"]))
    lines.append("endloop")
    return lines


def _gen_eis(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_eis script body.

    EIS uses 3 variables (h, r, j) not the standard (p, c). Per the
    MethodSCRIPT v1.6 manual (sec 14.46, p.88-89) the signature is::

        meas_loop_eis h r j <amplitude> <freq_start> <freq_end> <n_freq> <e_dc>

    The final argument is the **DC potential** of the applied sine wave
    (not an ``eis_opt``/averaging field — ``eis_opt`` is unsupported on the
    EmStat Pico, manual sec 9.8). It was previously hardcoded to ``0``,
    which pinned every sweep to 0 V regardless of the requested ``e_dc``;
    we now pass ``e_dc`` so the operator's DC bias is actually applied.
    ``set_e`` establishes the bias before the loop so the cell is at the
    measurement potential when the sweep begins.

    **``t_eq`` is emitted HERE, not by :func:`generate`** (see
    :data:`_SELF_EQUILIBRATING`).  The generic equilibration wait is
    appended before ``scan_channels_script_with_body`` runs -- which is what
    emits ``set_gpio_cfg`` and every ``set_gpio`` channel-select -- so on a
    multi-channel run it would fire while NO channel is selected and the
    GPIO pins are not yet outputs, biasing whatever channel a previous
    script left behind.  Emitting it inside the body puts it after each
    channel's ``set_gpio`` + 100 ms settle, so every channel equilibrates at
    ``e_dc`` on its own electrode.

    Consequence worth knowing: the body runs once per channel, so a
    multi-channel run costs ``t_eq`` per channel, not once overall. That is
    the electrochemically correct reading -- each channel is a different
    electrode and equilibrating one does nothing for the next -- but it does
    mean ``t_eq=60`` over 16 channels is 16 minutes of equilibration. The
    shipped default is ``t_eq=0``, so runs that never asked for
    equilibration are unaffected.
    """
    e_dc = _format_si(params.get("e_dc", 0.0))
    e_ac = _format_si(params.get("e_ac", 0.01))
    freq_start = _format_si(params.get("freq_start", 100000.0))
    freq_end = _format_si(params.get("freq_end", 0.1))
    n_freq = int(params.get("n_freq", 50))
    t_eq = float(params.get("t_eq", 0.0) or 0.0)
    lines: list[str] = []
    lines.append(f"set_e {e_dc}")
    if t_eq > 0:
        # Bias is set above; equilibrate AT it before sweeping.
        lines.append(f"wait {_format_si(t_eq)}")
    lines.append(
        f"meas_loop_eis h r j {e_ac}"
        f" {freq_start} {freq_end} {n_freq} {e_dc}"
    )
    lines.extend(_indent(_pck_eis()))
    lines.append("endloop")
    return lines


def _gen_geis(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_geis (galvanostatic EIS) script body.

    Mirrors :func:`_gen_eis`: the final ``meas_loop_geis`` argument is the
    **DC current** offset of the applied sine, previously hardcoded to
    ``0`` (which ignored the requested ``i_dc``). Now passes ``i_dc``.
    NOTE: GEIS is not yet hardware-validated on this device — verify the
    DC-current argument behaviour on the bench before relying on it.
    RETIRED 2026-07-28: GEIS is not a measurement this lab runs, so it is
    off the GUI (``_VERIFIED_TECHNIQUES``) and off the agent/MCP tool
    surface. This generator stays reachable for programmatic callers.
    """
    i_dc = _format_si(params.get("i_dc", 0.0))
    i_ac = _format_si(params.get("i_ac", 0.00001))
    freq_start = _format_si(params.get("freq_start", 100000.0))
    freq_end = _format_si(params.get("freq_end", 0.1))
    n_freq = int(params.get("n_freq", 50))
    lines: list[str] = []
    lines.append(f"set_i {i_dc}")
    lines.append(
        f"meas_loop_geis h r j {i_ac}"
        f" {freq_start} {freq_end} {n_freq} {i_dc}"
    )
    lines.extend(_indent(_pck_eis()))
    lines.append("endloop")
    return lines


def _gen_pad(params: dict[str, Any]) -> list[str]:
    """Generate PAD (preconcentration + stripping DPV) script body.

    PAD combines conditioning, deposition, and equilibration steps
    followed by a DPV measurement.
    """
    e_cond = _format_si(params.get("e_cond", -0.2))
    t_cond = _format_si(params.get("t_cond", 0.5))
    e_dep = _format_si(params.get("e_dep", -0.5))
    t_dep = _format_si(params.get("t_dep", 5.0))
    e_eq = _format_si(params.get("e_eq", -0.2))
    t_eq = _format_si(params.get("t_eq", 1.0))
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_end = _format_si(params.get("e_end", 0.5))
    e_step = _format_si(params.get("e_step", 0.005))
    e_pulse = _format_si(params.get("e_pulse", 0.05))
    t_pulse = _format_si(params.get("t_pulse", 0.05))
    scan_rate = _format_si(params.get("scan_rate", 0.05))
    lines: list[str] = []
    # Conditioning step
    lines.append(f"set_e {e_cond}")
    lines.append(f"wait {t_cond}")
    # Deposition step
    lines.append(f"set_e {e_dep}")
    lines.append(f"wait {t_dep}")
    # Equilibration step
    lines.append(f"set_e {e_eq}")
    lines.append(f"wait {t_eq}")
    # DPV measurement
    lines.append(
        f"meas_loop_dpv p c {e_begin} {e_end} {e_step}"
        f" {e_pulse} {t_pulse} {scan_rate}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


def _gen_lsp(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_lsp (linear sweep potentiometry) body."""
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_end = _format_si(params.get("e_end", 0.5))
    e_step = _format_si(params.get("e_step", 0.01))
    scan_rate = _format_si(params.get("scan_rate", 0.1))
    lines: list[str] = []
    lines.append(
        f"meas_loop_lsp p c {e_begin} {e_end} {e_step} {scan_rate}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


def _gen_fcv(params: dict[str, Any]) -> list[str]:
    """Generate meas_loop_fcv (fast cyclic voltammetry) body."""
    _validate_cv_cycle(params)
    e_begin = _format_si(params.get("e_begin", -0.5))
    e_vertex1 = _format_si(params.get("e_vertex1", 0.5))
    e_vertex2 = _format_si(params.get("e_vertex2", -0.5))
    e_step = _format_si(params.get("e_step", 0.01))
    scan_rate = _format_si(params.get("scan_rate", 1.0))
    n_scans = int(params.get("n_scans", 1))
    lines: list[str] = []
    lines.append(
        f"meas_loop_fcv p c {e_begin} {e_vertex1} {e_vertex2}"
        f" {e_step} {scan_rate} {_format_int(n_scans)}"
    )
    lines.extend(_indent(_pck_voltammetry()))
    lines.append("endloop")
    return lines


# --- MUX-alternating techniques ---


# Per-channel burst time in ca_alt_mux: settle_time GPIO settle (emitted in
# generate()) plus one 10 ms meas_loop_ca sample.  Used to pace the
# outer time loop so rounds land every t_interval seconds, matching
# PSTrace/MUX8-R2 (all channels sampled in rapid succession, then
# wait until the next interval).
_CA_ALT_MUX_SAMPLE_TIME = "10m"


def _gen_ca_alt_mux(params: dict[str, Any]) -> list[str]:
    """Generate round-robin CA body (N samples per channel per round).

    With ``samples_per_visit=1`` (default) this is one 10 ms sample, matching
    the historical behaviour. With N > 1, ``meas_loop_ca`` runs for
    ``N * 10 ms`` total at a 10 ms sample interval, so the device emits
    N packets per channel visit. The engine averages those packets into
    a single DataPoint per visit, providing a built-in box-car anti-alias
    filter for stirrer-induced periodic noise.

    The outer time loop and round pacing are added by ``generate()``.
    """
    e_dc = _format_si(params.get("e_dc", 0.2))
    n = max(1, int(params.get("samples_per_visit", 1)))
    # meas_loop_ca <e_dc> <t_interval> <t_run>
    interval = _CA_ALT_MUX_SAMPLE_TIME
    t_run = f"{int(n) * 10}m"  # N samples * 10 ms per sample
    lines: list[str] = []
    lines.append(f"meas_loop_ca p c {e_dc} {interval} {t_run}")
    lines.extend(_indent(_pck_amperometry()))
    lines.append("endloop")
    return lines


# ---------------------------------------------------------------------------
# Technique registry
# ---------------------------------------------------------------------------

# Maps technique key to (body_generator, preamble_builder).
# The preamble_builder is a callable that takes params and returns lines.
_TECHNIQUE_REGISTRY: dict[str, tuple] = {
    "lsv": (_gen_lsv, _preamble),
    "dpv": (_gen_dpv, _preamble),
    "swv": (_gen_swv, _preamble),
    "npv": (_gen_npv, _preamble),
    "acv": (_gen_acv, _preamble),
    "cv": (_gen_cv, _preamble),
    "ca": (_gen_ca, _preamble),
    "fca": (_gen_fca, _preamble),
    "cp": (_gen_cp, _preamble_galvano),
    "ocp": (_gen_ocp, _preamble_ocp),
    "eis": (_gen_eis, _preamble_eis),
    "geis": (_gen_geis, _preamble_geis),
    "pad": (_gen_pad, _preamble),
    "lsp": (_gen_lsp, _preamble),
    "fcv": (_gen_fcv, _preamble),
    "ca_alt_mux": (_gen_ca_alt_mux, _preamble),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(
    technique: str,
    params: dict[str, Any],
    channels: list[int],
    re_ce_channels: list[int] | None = None,
) -> list[str]:
    """Generate a complete MethodSCRIPT for the given technique.

    Assembles preamble, MUX channel setup, technique measurement body,
    and the safety postamble (``on_finished: cell_off``).

    For multi-channel runs, the technique body is wrapped in a MUX scan
    loop that iterates over each channel. MUX-alternating techniques
    (``ca_alt_mux``, ``cp_alt_mux``, ``ocp_alt_mux``) handle channel
    switching internally within their measurement loop.

    Args:
        technique: Technique identifier (case-insensitive), e.g.
            ``'cv'``, ``'dpv'``, ``'eis'``, ``'ca_alt_mux'``.
        params: Technique-specific parameters. Missing keys fall back
            to built-in defaults.
        channels: 1-indexed MUX WE channel numbers (e.g., ``[1, 2, 5]``).
        re_ce_channels: Optional parallel list of 1-indexed RE/CE
            positions, one per entry in ``channels``. When ``None``
            (legacy callers), RE/CE position 1 is used for every step
            — preserving the historical "shared RE/CE on position 1"
            behaviour. Electrode-config modes (external / on-board /
            manual) populate this list via ``TechniqueConfig``.

    Returns:
        List of MethodSCRIPT lines (no trailing newlines, no empty
        lines) ready to be sent via ``PicoConnection.send_script()``.

    Raises:
        ValueError: If the technique is not supported, ``channels`` is
            empty, or ``re_ce_channels`` length does not match
            ``channels``.
    """
    technique = technique.lower()
    if technique not in _TECHNIQUE_REGISTRY:
        raise ValueError(
            f"Unsupported technique {technique!r}. "
            f"Supported: {supported_techniques()}"
        )
    if not channels:
        raise ValueError("At least one channel must be specified.")

    # Normalise RE/CE list — None means "legacy single-channel RE/CE 1
    # for every step". Length is validated here so callers that build
    # bad lists fail loudly before script assembly.
    if re_ce_channels is None:
        re_ce_list = [1] * len(channels)
    else:
        if len(re_ce_channels) != len(channels):
            raise ValueError(
                "re_ce_channels length must match channels length "
                f"(got {len(re_ce_channels)} vs {len(channels)})."
            )
        re_ce_list = list(re_ce_channels)

    body_gen, preamble_fn = _TECHNIQUE_REGISTRY[technique]

    # Merge user params with defaults
    merged = dict(_DEFAULTS.get(technique, {}))
    merged.update(params)

    # Build preamble
    if preamble_fn == _preamble_ocp:
        script_lines = _preamble_ocp()
    else:
        script_lines = preamble_fn(merged)

    # EIS uses extra variables (h, r, j) instead of (p, c)
    if technique in ("eis", "geis"):
        insert_idx = 0
        for idx, line in enumerate(script_lines):
            if line.startswith("var "):
                insert_idx = idx + 1
        for v in ("var j", "var r", "var h"):
            script_lines.insert(insert_idx, v)

    # SWV requires 4 output vars: p (potential), c (net current),
    # f (forward current), r (reverse current).  Only p and c are
    # added to packets; f and r are needed by the command syntax.
    if technique == "swv":
        insert_idx = 0
        for idx, line in enumerate(script_lines):
            if line.startswith("var "):
                insert_idx = idx + 1
        for v in ("var r", "var f"):
            script_lines.insert(insert_idx, v)

    mux = MuxController()

    # Pre-measurement equilibration wait (applied to all techniques
    # except PAD which has its own internal equilibration sequence)
    t_eq = float(merged.get("t_eq", 0.0))
    # Techniques in _SELF_EQUILIBRATING emit their own t_eq wait inside the
    # body so it lands AFTER the MUX channel select; skip the generic one.
    emit_generic_wait = (
        t_eq > 0
        and technique != "pad"
        and technique not in _SELF_EQUILIBRATING
    )

    # Build technique body
    body = body_gen(merged)

    # -- ca_alt_mux: single self-looping script (no re-sends) -----------
    if technique == "ca_alt_mux":
        t_run = float(merged.get("t_run", 300.0))
        t_interval = float(merged.get("t_interval", 0.1))
        n_rounds = max(1, int(t_run / t_interval))
        # Store n_rounds in the caller's params dict so the engine
        # can read it instead of recomputing (avoids silent drift
        # if defaults change independently).
        params["_n_rounds"] = n_rounds

        # Add loop counter variable after existing var declarations
        insert_idx = 0
        for idx, line in enumerate(script_lines):
            if line.startswith("var "):
                insert_idx = idx + 1
        script_lines.insert(insert_idx, "var n")

        # GPIO config
        script_lines.extend(mux.gpio_config_script())

        # Equilibration
        if t_eq > 0:
            script_lines.append(f"wait {_format_si(t_eq)}")

        # Outer time loop: n_rounds iterations
        script_lines.append("store_var n 0i aa")
        script_lines.append(f"loop n < {n_rounds}i")

        # For each channel: switch GPIO, settle, measure 1 point
        settle_s = float(merged.get("settle_time", 0.1))
        settle = _format_si(settle_s)
        for ch, re_ce in zip(channels, re_ce_list):
            addr = mux.channel_address(ch, re_ce_channel=re_ce)
            script_lines.append(f"  set_gpio 0x{addr:03X}i")
            script_lines.append(f"  wait {settle}")
            for bline in body:
                script_lines.append(f"  {bline}")

        # Pace rounds to t_interval; skip if the burst already exceeds it.
        # Per-channel sample time = samples_per_visit * 10 ms (matches the
        # meas_loop_ca duration generated by _gen_ca_alt_mux).
        n_samples = max(1, int(merged.get("samples_per_visit", 1)))
        burst_s = len(channels) * (settle_s + n_samples * 0.010)
        pace_ms = int(round((t_interval - burst_s) * 1000))
        if pace_ms > 0:
            script_lines.append(f"  wait {pace_ms}m")

        script_lines.append("  add_var n 1i")
        script_lines.append("endloop")

    # -- Normal techniques -----------------------------------------------
    elif len(channels) == 1:
        # Single channel: configure GPIO, select channel, run technique
        script_lines.extend(mux.gpio_config_script())
        script_lines.extend(
            mux.select_channel_script(
                channels[0], re_ce_channel=re_ce_list[0]
            )
        )
        if emit_generic_wait:
            script_lines.append(f"wait {_format_si(t_eq)}")
        script_lines.extend(body)
    else:
        # Multi-channel: compact pattern requires both consecutive WE
        # channels and constant RE/CE (varying RE/CE forces sequential).
        re_ce_constant = len(set(re_ce_list)) == 1
        if mux._is_consecutive(channels) and re_ce_constant:
            # Insert var i / var e after the existing var declarations
            # Find insertion point (after last 'var' line)
            insert_idx = 0
            for idx, line in enumerate(script_lines):
                if line.startswith("var "):
                    insert_idx = idx + 1
            script_lines.insert(insert_idx, "var e")
            script_lines.insert(insert_idx, "var i")
        if emit_generic_wait:
            script_lines.append(f"wait {_format_si(t_eq)}")
        script_lines.extend(
            mux.scan_channels_script_with_body(
                channels, body, re_ce_channels=re_ce_list
            )
        )

    # Postamble (safety: cell_off on finish)
    script_lines.extend(_postamble())

    # Strip the leading 'e' command — it's the script-loading trigger
    # that PicoConnection.send_script() handles separately
    if script_lines and script_lines[0] == "e":
        script_lines = script_lines[1:]

    return script_lines


# Techniques verified against hardware during validation session.
# Other techniques remain in the registry but are hidden from the GUI
# until they are validated.
# DPV is deactivated: its PSTrace method-string keys (e_pulse/t_pulse)
# can't be verified without a native PSTrace DPV reference, and it isn't
# in use. The generator/defaults stay in the registry — re-add "dpv"
# here to bring it back once a reference is available.
_VERIFIED_TECHNIQUES = {"cv", "ca", "ca_alt_mux", "eis", "swv"}


def supported_techniques() -> list[str]:
    """Return a sorted list of hardware-verified technique identifiers.

    Returns:
        List of lowercase technique keys for techniques that have been
        validated against real hardware.
    """
    return sorted(_VERIFIED_TECHNIQUES)


def technique_params(technique: str) -> dict[str, Any]:
    """Return the default parameters for a technique.

    Args:
        technique: Technique identifier (case-insensitive).

    Returns:
        A copy of the default parameter dict. Returns an empty dict
        if the technique has no defined defaults.

    Raises:
        ValueError: If the technique is not supported.
    """
    technique = technique.lower()
    if technique not in _TECHNIQUE_REGISTRY:
        raise ValueError(
            f"Unsupported technique {technique!r}. "
            f"Supported: {supported_techniques()}"
        )
    return dict(_DEFAULTS.get(technique, {}))

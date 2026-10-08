"""Regression tests for MethodSCRIPT preamble bandwidth handling.

Phase 7 (bandwidth sweep) parameterised mode-2 ``set_max_bandwidth`` via the
``bw_hz`` technique param. Mode-3 preambles (``_preamble_eis`` and
``_preamble_galvano``) remain locked at 200 kHz for control-loop
stability and must NOT be influenced by ``bw_hz``.

These tests guard:

1. ``_preamble({'cr': '2u', 'bw_hz': bw})`` emits the SI-formatted
   ``set_max_bandwidth {expected}`` line for each sweep value.
2. Omitting ``bw_hz`` falls back to the legacy 400 Hz default.
3. ``_preamble_eis`` and ``_preamble_galvano`` ignore ``bw_hz`` and
   keep ``set_max_bandwidth 200k``.
"""

from __future__ import annotations

import pytest

from src.techniques.scripts import (
    EIS_CURRENT_RANGES,
    _format_si,
    _gen_eis,
    _preamble,
    _preamble_eis,
    _preamble_galvano,
    _preamble_geis,
    generate,
    next_larger_eis_range,
)


# Expected SI strings derived from ``_format_si`` (mantissa kept in
# [1, 1000) wherever possible; unity prefix emits no suffix).
_BW_CASES = [
    (0.4, "400m"),
    (4, "4"),
    (40, "40"),
    (400, "400"),
    (4000, "4k"),
    (40000, "40k"),
    (200000, "200k"),
]


@pytest.mark.parametrize("bw_hz, expected", _BW_CASES)
def test_preamble_emits_si_formatted_bandwidth(
    bw_hz: float, expected: str
) -> None:
    """_preamble must emit set_max_bandwidth using _format_si(bw_hz)."""
    lines = _preamble({"cr": "2u", "bw_hz": bw_hz})
    expected_line = f"set_max_bandwidth {expected}"
    assert expected_line in lines, (
        f"Expected line {expected_line!r} in preamble for bw_hz={bw_hz}; "
        f"got: {lines}"
    )


def test_preamble_defaults_to_400hz_when_bw_hz_missing() -> None:
    """Omitting bw_hz preserves the legacy 400 Hz default."""
    lines = _preamble({"cr": "2u"})
    assert "set_max_bandwidth 400" in lines, (
        f"Default bandwidth broken; got: {lines}"
    )


def test_preamble_eis_ignores_bw_hz_and_stays_200k() -> None:
    """Mode-3 EIS preamble must remain hardcoded at 200 kHz."""
    # Without bw_hz
    lines = _preamble_eis({"cr": "10u"})
    assert "set_max_bandwidth 200k" in lines, (
        f"EIS preamble must stay 200k; got: {lines}"
    )

    # Even if a caller incorrectly passes bw_hz, EIS must ignore it.
    lines_with_bw = _preamble_eis({"cr": "10u", "bw_hz": 4})
    assert "set_max_bandwidth 200k" in lines_with_bw, (
        f"EIS preamble must ignore bw_hz; got: {lines_with_bw}"
    )
    assert "set_max_bandwidth 4" not in lines_with_bw, (
        "EIS preamble must not switch to bw_hz value"
    )


def test_preamble_eis_pins_current_range() -> None:
    """EIS PINS the current range BY DEFAULT (eis_range_mode unset/'static').

    In-loop current-range switching corrupts EIS on FW 1.6.01 (the low-
    frequency Nyquist arc reverses/breaks, Z' goes negative -- bench-validated
    2026-06-20). So by default the preamble pins ``set_autoranging ba {cr} {cr}``
    (min==max disables autoranging) and must NOT emit a window like ``ba 1n {cr}``.
    """
    lines = _preamble_eis({"cr": "100u"})
    assert "set_autoranging ba 100u 100u" in lines, (
        f"EIS must pin the current range by default (min==max); got: {lines}"
    )
    assert "set_autoranging ba 1n 100u" not in lines, (
        "EIS must not autorange by default (in-loop switching corrupts EIS)"
    )
    # The nominal/start range is still set from cr.
    assert "set_range ba 100u" in lines


def test_preamble_eis_range_mode_static_pins() -> None:
    """eis_range_mode='static' is explicit pinning (same as the default)."""
    lines = _preamble_eis({"cr": "100u", "eis_range_mode": "static"})
    assert "set_autoranging ba 100u 100u" in lines
    assert "set_autoranging ba 1n 100u" not in lines


def test_preamble_eis_range_mode_auto_restores_window() -> None:
    """eis_range_mode='auto' restores the pre-PR#18 autoranging window.

    A pinned range buries the low-frequency points of a high-impedance cell
    (current << full scale -> scattered/negative -Z'' tail). 'auto' emits
    ``set_autoranging ba 1n {cr}`` (autorange from the most-sensitive floor up
    to {cr} as the CEILING), the original IDE-validated behaviour, while the
    default stays pinned.
    """
    auto = _preamble_eis({"cr": "100u", "eis_range_mode": "auto"})
    assert "set_autoranging ba 1n 100u" in auto, (
        f"eis_range_mode='auto' must autorange 1n..cr; got: {auto}"
    )
    assert "set_autoranging ba 100u 100u" not in auto, (
        "eis_range_mode='auto' must not pin the range"
    )
    assert "set_range ba 100u" in auto


@pytest.mark.parametrize("value, expected", [
    (0.5, "500m"),
    (0.001, "1m"),
    (-0.0002, "-200u"),
    (0.0, "0m"),
    (1.0, "1"),
    (2565.0, "2565"),       # was '2.565k' -> device e!4004
    (100000.0, "100k"),
    (10.0, "10"),
    (50000.0, "50k"),
])
def test_format_si_never_emits_decimal_mantissa(value, expected) -> None:
    """MethodSCRIPT needs an integer mantissa; a decimal point (e.g. '2.565k')
    is a device syntax error (e!4004). Regression for the EIS format bug."""
    out = _format_si(value)
    assert out == expected
    assert "." not in out


def test_gen_eis_non_round_frequencies_have_no_decimal() -> None:
    """A non-round freq must not serialise as a decimal-mantissa value."""
    body = _gen_eis({
        "e_dc": 0.0, "e_ac": 0.01,
        "freq_start": 2565.0, "freq_end": 1.0, "n_freq": 6,
    })
    eis_line = next(li for li in body if li.startswith("meas_loop_eis"))
    assert "." not in eis_line, f"decimal mantissa in {eis_line!r}"


def test_gen_eis_passes_e_dc_as_dc_potential_argument() -> None:
    """The final meas_loop_eis argument must carry e_dc, not a hardcoded 0.

    That argument is the sweep's DC potential (manual sec 14.46). Pinning
    it to 0 ran every sweep at 0 V regardless of the requested bias.
    """
    body = _gen_eis(
        {
            "e_dc": 0.22,
            "e_ac": 0.01,
            "freq_start": 100000.0,
            "freq_end": 1.0,
            "n_freq": 50,
        }
    )
    eis_line = next(li for li in body if li.startswith("meas_loop_eis"))
    # e_dc=0.22 V formats to '220m'; it must be the trailing DC arg.
    assert eis_line.endswith(" 220m"), (
        f"e_dc must be the DC-potential argument; got: {eis_line!r}"
    )
    assert not eis_line.endswith(" 0"), (
        "DC-potential argument must not be hardcoded to 0"
    )
    # set_e still establishes the bias before the loop.
    assert "set_e 220m" in body


def test_preamble_galvano_ignores_bw_hz_and_stays_200k() -> None:
    """Mode-3 galvanostatic preamble (CP / GEIS) must stay at 200 kHz."""
    # Without bw_hz
    lines = _preamble_galvano({"cr": "10u"})
    assert "set_max_bandwidth 200k" in lines, (
        f"Galvanostatic preamble must stay 200k; got: {lines}"
    )

    # Even if a caller incorrectly passes bw_hz, galvano must ignore it.
    lines_with_bw = _preamble_galvano({"cr": "10u", "bw_hz": 4})
    assert "set_max_bandwidth 200k" in lines_with_bw, (
        "Galvanostatic preamble must ignore bw_hz; got: "
        f"{lines_with_bw}"
    )
    assert "set_max_bandwidth 4" not in lines_with_bw, (
        "Galvanostatic preamble must not switch to bw_hz value"
    )


def test_preamble_geis_pins_current_range_cp_does_not() -> None:
    """GEIS pins the current range (mode-3, same in-loop autoranging corruption
    as EIS); CP keeps bounded autoranging from the shared galvano preamble."""
    geis = _preamble_geis({"cr": "100u"})
    assert "set_autoranging ba 100u 100u" in geis, (
        f"GEIS must pin the current range; got: {geis}"
    )
    assert "set_autoranging ba 100n 100u" not in geis, (
        "GEIS must not autorange (in-loop range switching corrupts mode-3)"
    )

    cp = _preamble_galvano({"cr": "100u"})
    assert "set_autoranging ba 100n 100u" in cp, (
        f"CP must keep bounded autoranging; got: {cp}"
    )


# ---------------------------------------------------------------------------
# RE/CE pass-through regression
# ---------------------------------------------------------------------------


def _set_gpio_addresses(script_lines: list[str]) -> list[int]:
    """Extract integer addresses from ``set_gpio 0xNNNi`` lines."""
    addrs: list[int] = []
    for line in script_lines:
        stripped = line.strip()
        if stripped.startswith("set_gpio 0x") and stripped.endswith("i"):
            hex_part = stripped[len("set_gpio 0x") : -1]
            try:
                addrs.append(int(hex_part, 16))
            except ValueError:
                continue
    return addrs


def _store_var_addresses(script_lines: list[str], var_name: str) -> list[int]:
    """Extract addresses from ``store_var <name> <N>i aa`` lines.

    The compact MUX loop emits ``store_var i <start>i aa`` and
    ``store_var e <end>i aa`` to seed the loop counter with the
    encoded WE+RE/CE GPIO address; we read those values back to
    verify the RE/CE bits propagated.
    """
    addrs: list[int] = []
    prefix = f"store_var {var_name} "
    for line in script_lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            # Format: "store_var i 224i aa" → token "224i"
            tokens = stripped.split()
            if len(tokens) >= 3 and tokens[2].endswith("i"):
                try:
                    addrs.append(int(tokens[2][:-1]))
                except ValueError:
                    continue
    return addrs


def test_generate_carries_external_re_ce_into_gpio_addresses() -> None:
    """External-mode ([15] per step) → bits[7:4] = 14 (≥ 0x0E0).

    Consecutive WE + constant RE/CE triggers the compact ``loop i <= e``
    pattern, so addresses are seeded into ``store_var i`` and
    ``store_var e`` rather than appearing inline on ``set_gpio``.
    Both seeds must encode RE/CE position 15 in bits[7:4].
    """
    script_lines = generate(
        technique="cv",
        params={},
        channels=[1, 2],
        re_ce_channels=[15, 15],
    )
    # Compact loop case: addresses live in store_var i / store_var e.
    seed_addrs = _store_var_addresses(script_lines, "i") + (
        _store_var_addresses(script_lines, "e")
    )
    inline_addrs = _set_gpio_addresses(script_lines)
    # Union both — some preambles emit nothing inline, others do.
    candidate_addrs = seed_addrs + inline_addrs
    assert candidate_addrs, (
        "Expected at least one set_gpio or store_var seed address; "
        "got:\n" + "\n".join(script_lines)
    )
    for addr in candidate_addrs:
        re_ce_bits = (addr & 0xF0) >> 4
        assert re_ce_bits == 14, (
            f"External mode must encode RE/CE position 15 "
            f"(bits[7:4]=14); got address 0x{addr:03X} "
            f"(bits[7:4]={re_ce_bits})"
        )


def test_generate_manual_re_ce_per_step_propagates_to_script() -> None:
    """Manual ([13, 1]) emits per-step GPIO with the right RE/CE bits."""
    script_lines = generate(
        technique="cv",
        params={},
        channels=[1, 3],
        re_ce_channels=[13, 1],
    )
    script = "\n".join(script_lines)
    # Step 1: WE=1, RE/CE=13 → addr = (12 << 4) | 0 = 0xC0
    assert "set_gpio 0x0C0i" in script, (
        f"Missing manual-mode step-1 GPIO 0x0C0i in:\n{script}"
    )
    # Step 2: WE=3, RE/CE=1  → addr = (0 << 4) | 2 = 0x02
    assert "set_gpio 0x002i" in script, (
        f"Missing manual-mode step-2 GPIO 0x002i in:\n{script}"
    )


@pytest.mark.parametrize("technique", ["cv", "fcv"])
def test_open_cycle_cv_rejected(technique: str) -> None:
    """e_begin != e_vertex2 must raise before reaching the device.

    Reproduces the 2026-05-31 hardware !0007 (begin=-0.2, vertex2=0.2)
    so the cryptic device error surfaces as a clear ValueError instead.
    """
    with pytest.raises(ValueError, match="closed cycle"):
        generate(
            technique=technique,
            params={"e_begin": -0.2, "e_vertex1": 0.8, "e_vertex2": 0.2},
            channels=[1],
        )


@pytest.mark.parametrize("technique", ["cv", "fcv"])
def test_closed_cycle_cv_accepted(technique: str) -> None:
    """e_begin == e_vertex2 generates a script without error."""
    script_lines = generate(
        technique=technique,
        params={"e_begin": -0.2, "e_vertex1": 0.8, "e_vertex2": -0.2},
        channels=[1],
    )
    assert any(f"meas_loop_{technique} " in ln for ln in script_lines), (
        f"Expected a meas_loop_{technique} line in:\n{script_lines}"
    )


# ---------------------------------------------------------------------------
# E3: meas_loop argument-order / command fixes against MethodSCRIPT v1.6
# (§14.40 meas_fast_ca, §14.41 meas_loop_cp, §14.44 meas_loop_ocp). These
# techniques are GUI-hidden; the tests pin the generated MethodSCRIPT to
# the documented signatures.
# ---------------------------------------------------------------------------


def test_ocp_signature_single_var_interval_before_run() -> None:
    """meas_loop_ocp: one output var, then interval, then run_time."""
    lines = generate("ocp", {"t_run": 60.0, "t_interval": 1.0}, channels=[1])
    loop = next(
        ln.strip() for ln in lines if ln.strip().startswith("meas_loop_ocp")
    )
    assert loop == "meas_loop_ocp p 1 60", loop
    # OCP measures potential only — no current at open circuit.
    assert any(ln.strip() == "pck_add p" for ln in lines)
    assert not any(ln.strip() == "pck_add c" for ln in lines), (
        "OCP must not pack a current variable"
    )


def test_cp_interval_before_run() -> None:
    """meas_loop_cp arg order: i_dc, interval, run (interval BEFORE run)."""
    lines = generate(
        "cp",
        {"i_dc": 0.0001, "t_run": 10.0, "t_interval": 0.5},
        channels=[1],
    )
    loop = next(
        ln.strip() for ln in lines if ln.strip().startswith("meas_loop_cp")
    )
    assert loop == "meas_loop_cp p c 100u 500m 10", loop


def test_fca_unsupported_on_pico_raises() -> None:
    """FCA: meas_loop_fca is invalid and meas_fast_ca needs EmStat4+."""
    with pytest.raises(
        ValueError, match="not supported on the EmStat Pico"
    ):
        generate("fca", {"e_dc": 0.2, "t_run": 10.0}, channels=[1])


# ---------------------------------------------------------------------------
# EIS/GEIS mode-3 current-range ladder + agent auto-range helper.
# ---------------------------------------------------------------------------


def test_eis_current_ranges_are_the_mode3_ladder() -> None:
    """The shared ladder is ascending and excludes mode-2-only values."""
    assert EIS_CURRENT_RANGES[0] == "100n"
    assert EIS_CURRENT_RANGES[-1] == "5m"
    # Mode-2 values must never appear (they return no data in mode 3).
    for mode2_only in ("2u", "4u", "8u", "16u", "32u", "63u", "10m", "100m"):
        assert mode2_only not in EIS_CURRENT_RANGES


def test_next_larger_eis_range_steps_one_rung_up() -> None:
    assert next_larger_eis_range("100n") == "1u"
    assert next_larger_eis_range("1u") == "6u"
    assert next_larger_eis_range("100u") == "200u"
    assert next_larger_eis_range("1m") == "5m"


def test_next_larger_eis_range_at_top_returns_none() -> None:
    assert next_larger_eis_range("5m") is None


def test_next_larger_eis_range_unknown_returns_none() -> None:
    # Mode-2 values and garbage are not on the ladder.
    assert next_larger_eis_range("2u") is None
    assert next_larger_eis_range("garbage") is None

"""EIS must equilibrate AT ``e_dc``, not at 0 V (noise follow-up #4).

Before 2026-07-28 the generated script emitted the ``t_eq`` wait before the
technique body, and the body's ``set_e`` after it.  A 60 s equilibration at
0.7 V therefore held the cell at 0 V for 60 s and then stepped to 0.7 V with
no settling, so the first (highest-frequency) points were measured on a cell
still relaxing from that step.

These tests assert ORDER in the emitted script rather than the presence of a
line, because presence was never the problem.
"""

from __future__ import annotations

import pytest

from src.techniques.scripts import generate

EIS_PARAMS = {
    "e_dc": 0.7,
    "e_ac": 0.01,
    "freq_start": 1e5,
    "freq_end": 1.0,
    "n_freq": 30,
}


def _stripped(technique: str, params: dict, channels: list[int]) -> list[str]:
    return [ln.strip() for ln in generate(technique, params, channels)]


def _first_index(lines: list[str], prefix: str) -> int:
    for i, ln in enumerate(lines):
        if ln.startswith(prefix):
            return i
    raise AssertionError(f"no line starting with {prefix!r} in:\n" + "\n".join(lines))


@pytest.mark.parametrize(
    "channels",
    [
        [1],           # single-channel branch
        [1, 2, 3],     # multi-channel, compact loop (consecutive)
        [1, 3],        # multi-channel, sequential fallback
    ],
)
def test_bias_is_applied_before_the_equilibration_wait(channels) -> None:
    """set_e must precede the t_eq wait on every script path."""
    lines = _stripped("eis", {**EIS_PARAMS, "t_eq": 60.0}, channels)
    assert _first_index(lines, "set_e") < _first_index(lines, "wait 60"), (
        "cell would equilibrate at 0 V and then step to e_dc:\n"
        + "\n".join(lines)
    )


@pytest.mark.parametrize("channels", [[1], [1, 2, 3], [1, 3]])
def test_bias_lands_after_the_channel_is_selected(channels) -> None:
    """The bias must be applied to the TARGET channel, not a stale one.

    This is the assertion the first version of this test lacked, and the
    omission hid a real bug: on multi-channel runs the bias and wait were
    appended before ``scan_channels_script_with_body`` -- which is what
    emits ``set_gpio_cfg`` and every ``set_gpio`` -- so e_dc was driven onto
    whatever channel a previous script left selected, for the whole t_eq
    window, before the GPIO pins were even configured as outputs. Ordering
    set_e before wait was true the whole time, so the old assertion passed.
    """
    lines = _stripped("eis", {**EIS_PARAMS, "t_eq": 60.0}, channels)
    first_select = _first_index(lines, "set_gpio ")
    assert first_select < _first_index(lines, "set_e"), (
        "bias is asserted before any channel is selected:\n"
        + "\n".join(lines)
    )


@pytest.mark.parametrize("channels", [[1], [1, 2, 3], [1, 3]])
def test_no_duplicate_bias_per_channel(channels) -> None:
    """Exactly one set_e per channel, not two.

    The first fix left the body's own set_e in place alongside a new one,
    so every sweep re-issued an identical set_e with zero settle before
    meas_loop_eis -- a needless control-loop poke at the worst moment.
    """
    lines = _stripped("eis", {**EIS_PARAMS, "t_eq": 60.0}, channels)
    n_bias = sum(ln.startswith("set_e") for ln in lines)
    # The compact loop emits the body once for all channels; the sequential
    # fallback emits it per channel.
    expected = 1 if len(channels) < 2 or _is_compact(lines) else len(channels)
    assert n_bias == expected, (
        f"expected {expected} set_e line(s), found {n_bias}:\n"
        + "\n".join(lines)
    )


def _is_compact(lines: list[str]) -> bool:
    """True when the MUX compact-loop pattern was used."""
    return any(ln.startswith("loop i <=") for ln in lines)


def test_equilibration_bias_matches_requested_e_dc() -> None:
    """The pre-wait bias is e_dc itself, not some other potential."""
    lines = _stripped("eis", {**EIS_PARAMS, "e_dc": -0.25, "t_eq": 30.0}, [1])
    bias = lines[_first_index(lines, "set_e")]
    assert bias == "set_e -250m", f"unexpected equilibration bias: {bias!r}"


def test_no_equilibration_wait_emitted_without_t_eq() -> None:
    """With t_eq=0 no equilibration wait is emitted at all.

    The shipped default is t_eq=0, so this is the path most runs take --
    they must be byte-unaffected by the equilibration work.
    """
    lines = _stripped("eis", {**EIS_PARAMS, "t_eq": 0.0}, [1])
    assert not any(ln.startswith("wait ") and ln != "wait 100m" for ln in lines)
    assert sum(ln.startswith("set_e") for ln in lines) == 1


def test_non_eis_techniques_are_unchanged() -> None:
    """CA deliberately keeps its old ordering.

    A CA step transient is often the signal of interest, so moving when the
    bias is applied would alter established workflows. If this ever changes
    it must be a deliberate decision, not a side effect of the EIS fix.
    """
    lines = _stripped(
        "ca", {"e_dc": 0.85, "t_eq": 10.0, "t_run": 5.0, "t_interval": 0.1}, [1]
    )
    wait_idx = _first_index(lines, "wait 10")
    set_e_before_wait = [
        ln for ln in lines[:wait_idx] if ln.startswith("set_e")
    ]
    assert not set_e_before_wait, (
        "CA gained a pre-wait bias; that is a separate decision"
    )

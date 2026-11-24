"""A sweep's t_eq is a hold at e_begin on each channel, after the MUX selects it.

On a multichannel CV the MUX connects each pad after the last finished. The
pad has been floating; stepping it straight to e_begin charges its double
layer in the first sample, which overloaded every channel after the first,
even on the largest range. The hold puts that charging before the first
recorded point. These tests pin the ORDER on every script path.
"""

from __future__ import annotations

import pytest

from src.techniques.scripts import generate

CV = {"e_begin": -0.6, "e_vertex1": 0.8, "e_vertex2": -0.6,
      "e_step": 0.01, "scan_rate": 0.1}
LSV = {"e_begin": -0.6, "e_end": 0.8, "e_step": 0.01, "scan_rate": 0.1}


def _lines(technique, params, channels):
    return [ln.strip() for ln in generate(technique, params, channels)]


def _index(lines, prefix, start=0):
    for i in range(start, len(lines)):
        if lines[i].startswith(prefix):
            return i
    raise AssertionError(f"no {prefix!r} after line {start}:\n" + "\n".join(lines))


@pytest.mark.parametrize("technique,params,loop", [
    ("cv", CV, "meas_loop_cv"),
    ("lsv", LSV, "meas_loop_lsv"),
])
@pytest.mark.parametrize("channels", [[1], [1, 2, 3], [1, 3]])
def test_the_hold_is_at_e_begin_after_the_channel_select(
    technique, params, loop, channels
):
    lines = _lines(technique, {**params, "t_eq": 2.0}, channels)
    gpio = _index(lines, "set_gpio ")
    hold = _index(lines, "set_e -600m", gpio)
    wait = _index(lines, "wait 2", hold)
    assert wait == hold + 1
    assert _index(lines, loop, wait) > wait


@pytest.mark.parametrize("channels", [[1, 2, 3], [1, 3]])
def test_every_channel_gets_its_own_hold(channels):
    lines = _lines("cv", {**CV, "t_eq": 2.0}, channels)
    holds = sum(1 for ln in lines if ln == "set_e -600m")
    loops = sum(1 for ln in lines if ln.startswith("meas_loop_cv"))
    # The compact loop has one body for all channels; the sequential
    # fallback repeats it. Either way there is one hold per sweep block.
    assert holds == loops


@pytest.mark.parametrize("channels", [[1], [1, 2, 3]])
def test_no_wait_runs_before_the_mux_loop(channels):
    lines = _lines("cv", {**CV, "t_eq": 2.0}, channels)
    assert _index(lines, "wait 2") > _index(lines, "set_gpio ")


def test_without_t_eq_nothing_is_added():
    lines = _lines("cv", CV, [1, 2, 3])
    assert not any(ln.startswith("set_e") for ln in lines)
    assert not any(ln == "wait 0" for ln in lines)


def test_ca_keeps_its_generic_wait():
    lines = _lines("ca", {"e_dc": 0.4, "t_run": 5, "t_interval": 0.1,
                          "t_eq": 3.0}, [1])
    assert any(ln == "wait 3" for ln in lines)

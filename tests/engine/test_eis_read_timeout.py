"""Tests for the frequency-scaled EIS/GEIS read timeout.

EIS sweeps one frequency per packet and the device holds the serial line
silent for the whole duration of each point, which climbs steeply at low
frequency. ``_eis_data_read_timeout`` sizes the "expecting data" read
timeout to outlast the slowest single point so a low-frequency sweep is not
mistaken for end-of-measurement (which truncated multi-channel runs after
channel 1). These tests pin the behaviour down without any hardware.
"""

from __future__ import annotations

import math

import pytest

from src.comms.serial_connection import MEASUREMENT_TIMEOUT
from src.engine.measurement_engine import (
    _EIS_FAST_BAND_MIN_HZ,
    _EIS_READ_TIMEOUT_MAX,
    _eis_data_read_timeout,
)


class TestEisReadTimeoutHighFrequencyUnchanged:
    """At/above the fast-band floor the legacy flat timeout is preserved."""

    @pytest.mark.parametrize(
        "freq_end", [100000.0, 1000.0, 100.0, 10.0, 5.0, 2.5, 2.0]
    )
    def test_fast_band_returns_flat_measurement_timeout(self, freq_end):
        # The user's working 5 Hz–100 kHz sweeps must behave exactly as
        # before — byte-for-byte identical timeout, no new lag.
        assert _eis_data_read_timeout(freq_end) == MEASUREMENT_TIMEOUT

    def test_fast_band_floor_is_inclusive(self):
        assert _eis_data_read_timeout(_EIS_FAST_BAND_MIN_HZ) == MEASUREMENT_TIMEOUT


class TestEisReadTimeoutLowFrequencyScaled:
    """Below the fast-band floor the timeout scales up with falling freq."""

    def test_one_hz_outlasts_a_real_one_hz_point(self):
        # Bench data: ~22 s at 5 Hz, ~quadrupling per half-decade -> a 1 Hz
        # point takes ~150 s. The timeout must comfortably exceed that, and
        # in particular exceed the old flat 120 s that caused the truncation.
        t = _eis_data_read_timeout(1.0)
        assert t > 150.0
        assert t > MEASUREMENT_TIMEOUT

    @pytest.mark.parametrize("freq_end", [1.9, 1.5, 1.0, 0.5, 0.2])
    def test_below_floor_is_at_least_measurement_timeout(self, freq_end):
        assert _eis_data_read_timeout(freq_end) >= MEASUREMENT_TIMEOUT

    def test_lower_frequency_never_gets_a_shorter_timeout(self):
        # Monotonic: a deeper sweep waits at least as long per point.
        freqs = [2.0, 1.5, 1.0, 0.5, 0.25, 0.1, 0.01]
        timeouts = [_eis_data_read_timeout(f) for f in freqs]
        assert timeouts == sorted(timeouts)

    def test_timeout_is_capped(self):
        # Very low frequencies are clamped so a stalled device still ends.
        assert _eis_data_read_timeout(0.001) == _EIS_READ_TIMEOUT_MAX
        assert _eis_data_read_timeout(0.1) <= _EIS_READ_TIMEOUT_MAX


class TestEisReadTimeoutDegenerateInputs:
    """Malformed configs must not crash or produce non-finite timeouts."""

    @pytest.mark.parametrize("freq_end", [0.0, -1.0])
    def test_zero_or_negative_freq_end_is_clamped(self, freq_end):
        t = _eis_data_read_timeout(freq_end)
        assert math.isfinite(t)
        assert MEASUREMENT_TIMEOUT <= t <= _EIS_READ_TIMEOUT_MAX

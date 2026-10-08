"""Tests for the EIS semicircle-apex guard in analyze_eis.

When the -Z'' arc never turns over (apex below freq_end), Rct from the vendored
peak-finder is meaningless (just Z' at the sweep floor minus Rs). The guard must
flag that so the agent reports Rct as unreliable / a lower bound.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.agent.vendor_analysis import _eis_apex_assessment


def _randles(freqs, rs: float = 350.0, rct: float = 10000.0, cap: float = 1e-6):
    """Ideal Randles spectrum; semicircle apex at f = 1/(2*pi*Rct*C) ~ 15.9 Hz."""
    w = 2 * np.pi * np.asarray(freqs, dtype=float)
    z = rs + rct / (1 + 1j * w * rct * cap)
    return pd.DataFrame(
        {"Frequency_Hz": freqs, "Z_real_Ohm": z.real, "Z_imag_Ohm": z.imag}
    )


def test_apex_reached_full_semicircle() -> None:
    # Sweep well below the apex (down to 1 Hz): the arc turns over.
    out = _eis_apex_assessment(_randles(np.logspace(5, 0, 50)))
    assert out["apex_reached"] is True
    assert out["rct_reliable"] is True
    # rct_ohm is NOT overridden -> the analyzer's value is kept.
    assert "rct_ohm" not in out


def test_apex_not_reached_rising_tail() -> None:
    # Stop ABOVE the apex (freq_end 50 Hz > 15.9 Hz): -Z'' still rising.
    out = _eis_apex_assessment(_randles(np.logspace(5, np.log10(50), 50)))
    assert out["apex_reached"] is False
    assert out["rct_reliable"] is False
    assert out["rct_ohm"] is None
    assert out["peak_frequency_hz"] is None
    assert out["time_constant_s"] is None
    assert "rct_lower_bound_ohm" in out
    assert "apex" in out["rct_note"].lower()


def test_apex_not_reached_on_real_testdata_shape() -> None:
    # Monotonic, accelerating -Z'' like the gold/CeOx data the user flagged.
    df = pd.DataFrame(
        {
            "Frequency_Hz": np.logspace(5, 1, 50),
            "Z_real_Ohm": np.linspace(350, 10000, 50),
            "Z_imag_Ohm": -np.linspace(40, 17000, 50),  # -Z'' rising to the end
        }
    )
    out = _eis_apex_assessment(df)
    assert out["apex_reached"] is False
    assert out["rct_ohm"] is None
    # Lower bound ~ 2 * peak(-Z'') = ~34000 ohm, far above the naive "10k".
    assert out["rct_lower_bound_ohm"] > 30000


def test_apex_assessment_tolerates_missing_columns() -> None:
    assert _eis_apex_assessment(pd.DataFrame({"foo": [1, 2, 3]})) == {}

"""EngineAdapter: agent-facing measurement and device operations.

Wraps an injected engine + connection pair (the real
``MeasurementEngine``/``PicoConnection`` or the mocks from
:mod:`src.agent.mock_engine` -- the adapter never constructs hardware
objects itself) and exposes the async tool surface the agent uses:

* ``run_cv`` / ``run_ca`` / ``run_cp`` / ``run_eis`` / ``run_geis`` --
  build a validated ``TechniqueConfig`` from technique defaults plus
  user args, marshal ``engine.start_measurement`` onto the GUI thread
  via :func:`src.agent.bridge.run_on_gui` (the engine must be started
  from the GUI thread), and await the race between
  ``measurement_finished`` and ``measurement_error`` through a
  thread-safe future connected BEFORE the start call (no missed-signal
  window).  On success a COMPACT summary dict is returned -- never the
  raw data arrays.
* Device tools: ``list_ports`` (pyserial enumeration, imported eagerly
  at module top), ``connect_device`` / ``disconnect_device`` (marshaled
  onto the GUI thread, which owns the connection), ``device_status``,
  and ``abort_measurement``.

Every operation returns a structured dict with an ``ok`` flag instead
of raising, so the tool-dispatch layer can hand results straight back
to the model.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
from datetime import datetime
from typing import Any, Optional

# Eager import per the blueprint constraint: native/third-party deps are
# imported at module top, before any asyncio loop exists.
from serial.tools import list_ports as _serial_list_ports

from PyQt6.QtCore import Qt

from src.agent.bridge import SignalTimeoutError, await_signal, run_on_gui
from src.agent.open_circuit_watch import OpenCircuitWatch, early_stop_message
from src.data.exporters import PsSessionExporter
from src.data.models import TechniqueConfig
from src.techniques.scripts import (
    CURRENT_RANGES,
    EIS_CURRENT_RANGES,
    next_larger_current_range,
    next_larger_eis_range,
    supported_techniques,
    technique_params,
)

logger = logging.getLogger(__name__)

# Default directory for agent-initiated .pssession exports (relative to
# the process working directory, i.e. the repo root when launched the
# documented way).
_DEFAULT_EXPORT_DIR = "agent_exports"

__all__ = [
    "SWV_OUTPUT_VARS",
    "EngineAdapter",
    "build_technique_config",
    "cv_noise",
    "eis_quality",
    "swv_output_vars",
    "swv_quality",
]

# Techniques whose run summary carries an EIS data-quality assessment.
_EIS_TECHNIQUES = ("eis", "geis")

# Square-wave voltammetry gets its OWN summary branch rather than the
# generic current-trace one: SWV carries a four-variable output contract
# (below) that the model must not over-read, and -- from the quality pass --
# a per-channel under-range assessment on the mode-2 ladder.
_SWV_TECHNIQUES = ("swv",)

#: SWV's four MethodSCRIPT output variables, in the order
#: ``meas_loop_swv p c f r`` declares them, paired with the ``DataPoint``
#: variable name each lands under once the device packs it and a
#: human-readable role.
#:
#: Only ``p`` and ``c`` are currently added to the data packets
#: (``scripts._pck_voltammetry``); ``f`` and ``r`` are declared because the
#: command syntax requires four variables.  Packing them as well is NOT a
#: free change -- all three currents share the ``ba`` type code, so
#: ``ParsedPacket.values`` (a name->value dict) would collapse them and the
#: last one would silently overwrite the net current everywhere: live plot,
#: CSV, and .pssession export.  Doing it properly needs positional
#: disambiguation in the protocol layer plus a hardware re-validation of the
#: bench-verified SWV script, which is Req 15b territory.  This table is the
#: seam: :func:`swv_output_vars` reports role-by-role what actually reached
#: the host, so the deferred E-AB layer inherits a declared contract instead
#: of a guess, and the model is told not to infer forward/reverse from net.
SWV_OUTPUT_VARS: tuple[tuple[str, str, str], ...] = (
    ("p", "set_potential", "step potential (V)"),
    ("c", "current", "net current, forward minus reverse (A)"),
    ("f", "current_forward", "forward-pulse current (A)"),
    ("r", "current_reverse", "reverse-pulse current (A)"),
)

# A current-vs-time trace (CV/CA/…) is flagged noisy when its ripple index — a
# robust estimate of point-to-point oscillation about the local trend, divided
# by the current span — exceeds this fraction. The dominant real-world cause is
# 50/60 Hz mains pickup, fixable by lowering the measurement bandwidth (bw_hz).
# It is an UNCALIBRATED comparative index, not a noise amplitude: drive it down
# across re-runs. Calibrate the flag against a clean-vs-noisy pair from the
# actual rig (the auto-range/noise rehearsal).
_NOISE_RIPPLE_FLAG = 0.02

# Window (points, odd) of the centered median detrend used to separate ripple
# from the smooth CV/CA trend. It passes ripple with apparent period up to
# ~2× the window; slower variation is treated as trend (see cv_noise).
_NOISE_MEDIAN_WINDOW = 7

# Minimum current points for a meaningful ripple estimate. Set well above the
# median window: below this a sharp faradaic peak spans only a few points and is
# genuinely indistinguishable from ripple (an empirically-confirmed false
# positive on coarse scans), so under-sampled scans report insufficient_data
# rather than phantom noise.
_NOISE_MIN_POINTS = 24

# A channel is flagged under-ranged when at least this fraction of its
# impedance points carry a DEVICE-AUTHORITATIVE under-range signal (the
# overload status bit or a NaN reading). On a 50-point sweep this is ≥5
# points; combined with the absolute floor below, a single stray bad point
# never trips the flag.
_EIS_BAD_FRACTION_FLAG = 0.10

# Absolute floor (points), independent of sweep length: never flag a channel
# under-ranged on a single bad point. This also fixes the small-sweep edge
# (without it, 1 bad point on a ≤10-point sweep would reach the 10% fraction).
_EIS_MIN_BAD_POINTS = 2

# Negative real-Z is NOT treated as a primary under-range signal: the project's
# own EIS noise investigation attributes scattered negative-Z' points to mains
# (50/60 Hz) pickup on CORRECTLY-ranged sweeps, and stepping the range up cannot
# remove them — so flagging on them would chase a non-existent range fault. It
# only contributes when a LARGE fraction of the sweep is negative (an inverted
# Nyquist arc that mains cannot explain); the authoritative overload/NaN signals
# drive the normal case.
_EIS_NEG_ZREAL_FRACTION_FLAG = 0.5

# Fallback range when the used range is not on the mode-3 ladder (the agent
# passed an out-of-ladder value): re-range to a safe mid-ladder default.
_EIS_FALLBACK_CR = "100u"

# A potentiostatic sweep whose AC current is below this fraction of the pinned
# range passed almost no current: the channel is open in the wiring in use, or
# the range is far too large. Its points are noise (NaN, negative Z'), which
# the under-range test would otherwise read as "range too small" and answer
# with a LARGER range -- the wrong direction. Checked first, on the median
# |Z| of the valid points; 0.5% keeps a real high-impedance electrode on a
# sensible range (1% of range and up) out of it.
_EIS_NO_CURRENT_FRACTION = 0.005

_SI_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}


def _cr_amperes(cr: str) -> Optional[float]:
    """Current range string ('1u', '100n', '1m') in amperes, or None."""
    text = str(cr).strip()
    if not text:
        return None
    scale = _SI_PREFIX.get(text[-1])
    digits = text[:-1] if scale is not None else text
    try:
        return float(digits) * (scale if scale is not None else 1.0)
    except ValueError:
        return None

# Absolute floor (points) before an SWV channel is called under-ranged: two
# device-authoritative bad samples, so a single stray reading never trips it.
#
# Note there is deliberately NO fraction gate here, unlike EIS. On an EIS
# sweep every point is an equally valid measurement, so a handful of bad ones
# among fifty can be tolerated. On SWV the railed points are precisely the
# PEAK -- the one number the scan exists to produce -- and a clipped peak is
# still a small fraction of a long sweep (a 20-point peak on a 200-point scan
# is 10%, right at the EIS threshold, and a partially-clipped one is well
# under it). Requiring a fraction would therefore let exactly the failure
# that matters slip through as "ok".
_SWV_MIN_BAD_POINTS = 2

# Fallback range when the used range is not on the mode-2 ladder (e.g. the
# agent passed a mode-3-only value such as '6u'): re-range to a mid-ladder
# default rather than guessing a neighbour.
_SWV_FALLBACK_CR = "100u"


def eis_quality(
    result: Any,
    channels_requested: list[int],
    used_cr: str,
) -> dict[str, Any]:
    """Assess EIS/GEIS data quality per channel for agent auto-ranging.

    A too-small (under-ranged) current range makes the EIS current rail the
    pinned range; the device then reports an overload status bit or emits NaN
    readings on the clipped points. Those two signals are device-authoritative
    and drive the verdict. Negative real-Z is a NOISY proxy — the project's own
    bench data attributes scattered negative-Z' to mains pickup on correctly-
    ranged sweeps — so it only contributes when a large fraction of the sweep is
    negative (an inverted arc). When a channel is flagged, the function suggests
    stepping one rung UP the mode-3 ladder. (Over-ranging is the opposite, softer
    problem — small-but-valid Z with poor resolution — and is left to the agent's
    judgement; only under-range signatures are auto-flagged here.)

    One case is checked before all of that: a potentiostatic sweep that
    passed almost no current (``e_ac / median |Z|`` under
    ``_EIS_NO_CURRENT_FRACTION`` of the range). That is an open channel, or
    a range far too large, and its noise looks like the under-range
    signature. The verdict is ``no_current`` and the note sends the agent
    to the wiring, never up the range ladder.

    Args:
        result: The finished ``MeasurementResult``.
        channels_requested: Channels the run asked for (so a requested
            channel that returned no impedance points is reported as
            ``no_data`` rather than silently omitted).
        used_cr: The current range the sweep ran at (SI string, e.g.
            ``'1u'``), used to compute the suggested next range.

    Returns:
        A compact dict: ``quality_ok`` (bool), ``per_channel`` (dict keyed
        by channel string with point/overload/NaN/negative-Z counts, the
        bad fraction, and a per-channel verdict), ``suggested_cr`` (the
        recommended larger range, or None), ``rerange_exhausted`` (True when
        the channel is still bad at the largest range — stop re-ranging), and
        ``note`` (a one-line human summary for the model).
    """
    # Bucket impedance-bearing points by channel in a single pass (avoids an
    # O(channels × points) re-scan of the full point list per channel).
    by_channel: dict[int, list[Any]] = {}
    for dp in result.data_points:
        if "zreal" in dp.variables or "impedance" in dp.variables:
            by_channel.setdefault(dp.channel, []).append(dp)

    per_channel: dict[str, dict[str, Any]] = {}
    channels = sorted(
        set(int(c) for c in channels_requested) | set(by_channel)
    )
    params = getattr(result, "params", None) or {}
    e_ac = params.get("e_ac")
    cr_a = _cr_amperes(used_cr)
    for ch in channels:
        imp_pts = by_channel.get(ch, [])
        n = len(imp_pts)
        overload_n = nan_n = neg_n = auth_bad_n = 0
        magnitudes: list[float] = []
        for dp in imp_pts:
            zr = dp.variables.get("zreal")
            zi = dp.variables.get("zimag")
            zmag = dp.variables.get("impedance")
            is_nan = any(
                v is not None and math.isnan(v) for v in (zr, zi, zmag)
            )
            if not is_nan:
                if zmag is not None:
                    magnitudes.append(abs(float(zmag)))
                elif zr is not None and zi is not None:
                    magnitudes.append(math.hypot(float(zr), float(zi)))
            is_neg = (
                zr is not None and not math.isnan(zr) and zr < 0.0
            )
            is_overload = bool(getattr(dp, "overload", False))
            if is_overload:
                overload_n += 1
            if is_nan:
                nan_n += 1
            if is_neg:
                neg_n += 1
            # Authoritative under-range signals only (overload bit / NaN).
            if is_overload or is_nan:
                auth_bad_n += 1
        if n == 0:
            verdict = "no_data"
            bad_fraction = 1.0
        else:
            bad_fraction = auth_bad_n / n
            authoritative = (
                auth_bad_n >= _EIS_MIN_BAD_POINTS
                and bad_fraction >= _EIS_BAD_FRACTION_FLAG
            )
            # Inverted-arc backstop: a large negative-Z' fraction is a genuine
            # corruption signature that mains pickup (a few scattered points)
            # cannot produce.
            inverted_arc = (
                neg_n >= _EIS_MIN_BAD_POINTS
                and (neg_n / n) >= _EIS_NEG_ZREAL_FRACTION_FLAG
            )
            verdict = (
                "underranged" if (authoritative or inverted_arc) else "ok"
            )
        # AC current as a fraction of the range (potentiostatic sweeps only).
        current_fraction: Optional[float] = None
        positive = [m for m in magnitudes if m > 0.0]
        if positive and e_ac and cr_a:
            current_fraction = (
                abs(float(e_ac)) / statistics.median(positive)
            ) / cr_a
            # No overload bit on any point: a railed range sets that bit, an
            # open channel never does.
            if (
                current_fraction < _EIS_NO_CURRENT_FRACTION
                and overload_n == 0
            ):
                verdict = "no_current"
        per_channel[str(ch)] = {
            "points": n,
            "overload_points": overload_n,
            "nan_points": nan_n,
            "neg_zreal_points": neg_n,
            "bad_fraction": round(bad_fraction, 3),
            "verdict": verdict,
        }
        if current_fraction is not None:
            per_channel[str(ch)]["ac_current_fraction_of_range"] = float(
                f"{current_fraction:.3g}"
            )

    flagged = [c for c, q in per_channel.items() if q["verdict"] != "ok"]
    underranged = [
        c for c in flagged if per_channel[c]["verdict"] == "underranged"
    ]
    no_current = [
        c for c in flagged if per_channel[c]["verdict"] == "no_current"
    ]
    quality_ok = not flagged
    suggested_cr: Optional[str] = None
    rerange_exhausted = False
    if quality_ok:
        note = "EIS data quality looks good on all measured channels."
    elif no_current:
        # Open in this wiring (or a range far too large). Never suggest a
        # larger range: that is the opposite of what would help.
        note = (
            f"Channel(s) {no_current} passed almost no current at {used_cr} "
            "(AC current under 0.5% of the range). The channel is open in "
            "the RE/CE wiring used, or the range is far too large. Do NOT "
            "step the range up. Check the wiring with a short CA hold "
            "(the wiring check); if the channel conducts in another "
            "wiring, repeat the sweep there at a range that fits its "
            "current."
        )
        if underranged:
            note += (
                f" Channel(s) {underranged} separately look under-ranged "
                "(overload / NaN); re-run those at a larger range."
            )
    elif not underranged:
        # The only flagged channels returned NO data (open/dead/disconnected).
        # Re-ranging cannot conjure data, so do not suggest a larger range.
        note = (
            f"Channel(s) {flagged} returned no impedance data. Re-ranging "
            "will not help — check the electrode contact / wiring, or whether "
            "the channel is open or dead."
        )
    else:
        suggested_cr = next_larger_eis_range(used_cr)
        if suggested_cr is None:
            if str(used_cr) == EIS_CURRENT_RANGES[-1]:
                # Already at the top of the ladder and still bad: this is no
                # longer a range problem. Signal a hard stop so the agent does
                # not loop re-running the largest range.
                rerange_exhausted = True
                note = (
                    f"Channel(s) {flagged} still show overload / NaN at the "
                    f"largest current range ({used_cr}). This is no longer a "
                    "current-range problem — STOP re-ranging and check the "
                    "electrode contact, wiring, and cell, or whether the "
                    "channel is open/dead."
                )
            else:
                # Used range not on the mode-3 ladder: re-range to a safe
                # mid-ladder default rather than guessing a neighbour.
                suggested_cr = _EIS_FALLBACK_CR
                note = (
                    f"Channel(s) {flagged} returned overload / NaN or no data "
                    f"at current range {used_cr!r}, which is not a valid "
                    f"mode-3 EIS range. Re-run at a valid range such as "
                    f"{suggested_cr}."
                )
        else:
            note = (
                f"Channel(s) {flagged} look under-ranged at {used_cr} "
                "(the current railed the range → overload / NaN). Re-run "
                f"those channel(s) at a LARGER current range, e.g. "
                f"{suggested_cr} (jump several rungs if most points are bad), "
                "then continue."
            )

    return {
        "quality_ok": quality_ok,
        "per_channel": per_channel,
        "suggested_cr": suggested_cr,
        "rerange_exhausted": rerange_exhausted,
        "note": note,
    }


def swv_output_vars(result: Any) -> dict[str, Any]:
    """Report SWV's four output variables against what the run returned.

    ``meas_loop_swv p c f r`` declares four outputs (see
    :data:`SWV_OUTPUT_VARS`).  This walks the finished result and states,
    role by role, which of them actually reached the host -- so nothing is
    silently collapsed to "current" and the deferred E-AB / aptamer layer
    (Req 15b) inherits an explicit contract rather than having to
    re-derive it.  The block also tells the model NOT to infer
    forward/reverse from the net current when they are absent, which is
    the same failure shape the EIS apex assessment guards against: an
    un-captured quantity must never be reported as a measured one.

    Args:
        result: The finished ``MeasurementResult``.

    Returns:
        ``{"roles": {var_name: role}, "returned": [...],
        "not_returned": [...], "note": str}``, where the name lists are
        ordered as the MethodSCRIPT command declares the variables.
    """
    present: set[str] = set()
    for dp in result.data_points:
        present.update(dp.variables)

    returned = [name for _, name, _ in SWV_OUTPUT_VARS if name in present]
    missing = [name for _, name, _ in SWV_OUTPUT_VARS if name not in present]

    if missing:
        note = (
            "SWV declares four output variables (p / c / f / r). This run "
            f"carried {returned} to the host; {missing} are declared for "
            "the meas_loop_swv command syntax but are not packed into the "
            "data packets, so no forward/reverse current exists for this "
            "run. Report the NET current only -- do not infer, split, or "
            "estimate forward/reverse from it."
        )
    else:
        note = (
            "All four SWV output variables (potential, net, forward and "
            "reverse current) are present in this run."
        )
    return {
        "roles": {name: role for _, name, role in SWV_OUTPUT_VARS},
        "returned": returned,
        "not_returned": missing,
        "note": note,
    }


def swv_quality(
    result: Any,
    channels_requested: list[int],
    used_cr: str,
) -> dict[str, Any]:
    """Assess SWV data quality per channel for agent auto-ranging.

    The mirror of :func:`eis_quality` for square-wave voltammetry: a
    too-small current range makes the peak rail, and the device answers
    with its authoritative overload status bit or a NaN reading on the
    clipped samples.  Those are the only signals used -- no heuristic
    proxies -- and a flagged channel gets a suggestion one rung UP the
    low-speed (mode-2) ladder, which is a DIFFERENT ladder from EIS's
    mode-3 one.

    Deliberately scoped to ACQUISITION health, not interpretation: whether
    a peak exists, where it sits and how far it stands out is
    ``analyze_swv``'s job (chain ``export_session`` -> ``analyze_swv``).
    Keeping the split means the measurement adapter does not grow a
    dependency on the analysis stack, and an unrailed scan with no peak is
    reported as a good acquisition of a blank -- which it is.

    Args:
        result: The finished ``MeasurementResult``.
        channels_requested: Channels the run asked for, so a requested
            channel that returned no current points is reported as
            ``no_data`` rather than silently omitted.
        used_cr: The current range the scan ran at (SI string, e.g.
            ``'8u'``), used to compute the suggested next range.

    Returns:
        ``{"quality_ok": bool, "per_channel": {ch: {points,
        overload_points, nan_points, bad_points, verdict}},
        "suggested_cr": str|None, "rerange_exhausted": bool,
        "note": str}`` -- the same shape ``eis_quality`` returns, so the
        agent reads one auto-range protocol for both techniques.
    """
    by_channel: dict[int, list[Any]] = {}
    for dp in result.data_points:
        if "current" in dp.variables:
            by_channel.setdefault(dp.channel, []).append(dp)

    per_channel: dict[str, dict[str, Any]] = {}
    channels = sorted(
        set(int(c) for c in channels_requested) | set(by_channel)
    )
    for ch in channels:
        pts = by_channel.get(ch, [])
        n = len(pts)
        overload_n = nan_n = bad_n = 0
        for dp in pts:
            cur = dp.variables.get("current")
            is_nan = cur is not None and math.isnan(float(cur))
            is_overload = bool(getattr(dp, "overload", False))
            if is_overload:
                overload_n += 1
            if is_nan:
                nan_n += 1
            if is_overload or is_nan:
                bad_n += 1
        if n == 0:
            verdict = "no_data"
        elif bad_n >= _SWV_MIN_BAD_POINTS:
            verdict = "underranged"
        else:
            verdict = "ok"
        per_channel[str(ch)] = {
            "points": n,
            "overload_points": overload_n,
            "nan_points": nan_n,
            "bad_points": bad_n,
            "verdict": verdict,
        }

    flagged = [c for c, q in per_channel.items() if q["verdict"] != "ok"]
    underranged = [
        c for c in flagged if per_channel[c]["verdict"] == "underranged"
    ]
    quality_ok = not flagged
    suggested_cr: Optional[str] = None
    rerange_exhausted = False
    if quality_ok:
        note = (
            "SWV acquisition looks clean on all measured channels (no "
            "railed or NaN samples). Export the session and call "
            "analyze_swv to read the peak."
        )
    elif not underranged:
        # Only no_data channels: re-ranging cannot conjure samples.
        note = (
            f"Channel(s) {flagged} returned no current data. Re-ranging "
            "will not help -- check the electrode contact / wiring, or "
            "whether the channel is open or dead."
        )
    else:
        suggested_cr = next_larger_current_range(used_cr)
        if suggested_cr is None:
            if str(used_cr) == CURRENT_RANGES[-1]:
                rerange_exhausted = True
                note = (
                    f"Channel(s) {flagged} still rail (overload / NaN) at "
                    f"the largest current range ({used_cr}). This is no "
                    "longer a current-range problem -- STOP re-ranging and "
                    "check the electrode contact, wiring and cell, or "
                    "whether the channel is shorted."
                )
            else:
                suggested_cr = _SWV_FALLBACK_CR
                note = (
                    f"Channel(s) {flagged} railed at current range "
                    f"{used_cr!r}, which is not a valid mode-2 range "
                    "(SWV runs low-speed mode 2, not the EIS mode-3 "
                    f"ladder). Re-run at a valid range such as "
                    f"{suggested_cr}."
                )
        else:
            note = (
                f"Channel(s) {flagged} look under-ranged at {used_cr} -- "
                "the current railed the range (overload / NaN), so the "
                "peak height is clipped and MUST NOT be reported. Re-run "
                f"those channel(s) at a LARGER current range, e.g. "
                f"{suggested_cr} (jump several rungs if most points are "
                "bad), then continue."
            )

    return {
        "quality_ok": quality_ok,
        "per_channel": per_channel,
        "suggested_cr": suggested_cr,
        "rerange_exhausted": rerange_exhausted,
        "note": note,
    }


def _median_detrend(values: list[float], window: int) -> list[float]:
    """Return ``values`` minus a centered rolling median (high-pass residual).

    The rolling median tracks the smooth CV/CA trend — including the sharp
    faradaic rises, which a mean would smear — so the residual is the
    point-to-point ripple about that trend. The window shrinks at the ends
    (uses the available neighbours) so every point gets a residual.
    """
    n = len(values)
    half = max(1, window // 2)
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(values[i] - statistics.median(values[lo:hi]))
    return out


def ca_steady_state(
    result: Any,
    channels_requested: list[int],
) -> dict[str, Any]:
    """Summarise the settled current of each channel of a CA hold.

    The agent receives metrics, never the raw trace. Without this block it
    has to export the run and analyse one channel per tool call to read a
    plain steady current, which costs one call per channel. The block gives
    every channel's settled current in the run result itself.

    The settled current is the mean over the LAST HALF of the channel's
    usable points, so a start-up transient does not bias it. ``drift_pct``
    compares the means of the third and the last quarter and tells the
    agent whether the hold had settled.

    Args:
        result: The finished ``MeasurementResult``.
        channels_requested: Channels the run asked for, so a requested
            channel without usable current points is reported, not omitted.

    Returns:
        ``{"per_channel": {ch: {points, used_points, overload_points,
        mean_current_a, std_current_a, drift_pct}}, "note": str}``. The
        numeric fields are None for a channel with no usable points.
    """
    by_channel: dict[int, list[float]] = {}
    overloads: dict[int, int] = {}
    counts: dict[int, int] = {}
    for dp in result.data_points:
        if "current" not in dp.variables:
            continue
        ch = int(dp.channel)
        counts[ch] = counts.get(ch, 0) + 1
        value = dp.variables["current"]
        bad = bool(getattr(dp, "overload", False)) or (
            isinstance(value, float) and math.isnan(value)
        )
        if bad:
            overloads[ch] = overloads.get(ch, 0) + 1
            continue
        by_channel.setdefault(ch, []).append(float(value))

    per_channel: dict[str, dict[str, Any]] = {}
    channels = sorted(set(int(c) for c in channels_requested) | set(counts))
    for ch in channels:
        usable = by_channel.get(ch, [])
        entry: dict[str, Any] = {
            "points": counts.get(ch, 0),
            "used_points": 0,
            "overload_points": overloads.get(ch, 0),
            "mean_current_a": None,
            "std_current_a": None,
            "drift_pct": None,
        }
        if usable:
            tail = usable[len(usable) // 2:]
            mean = statistics.fmean(tail)
            entry["used_points"] = len(tail)
            entry["mean_current_a"] = mean
            entry["std_current_a"] = (
                statistics.pstdev(tail) if len(tail) > 1 else 0.0
            )
            if len(tail) >= 4 and mean != 0.0:
                half = len(tail) // 2
                first = statistics.fmean(tail[:half])
                last = statistics.fmean(tail[half:])
                entry["drift_pct"] = 100.0 * (last - first) / abs(mean)
        per_channel[str(ch)] = entry
    return {
        "per_channel": per_channel,
        "note": (
            "mean_current_a is the mean over the last half of each "
            "channel's hold (amperes). For a resistive load, resistance "
            "= e_dc / mean_current_a. A channel whose mean is within "
            "noise of zero passed no current in this wiring. Check "
            "overload_points and drift_pct before trusting a value."
        ),
    }


def cv_noise(
    result: Any,
    channels_requested: list[int],
) -> dict[str, Any]:
    """Assess per-channel ripple on a current trace, for bandwidth scoping.

    Gives the agent a NUMBER for trace "noise" it cannot otherwise see (it only
    receives metrics, never the plot pixels), so it can scope settings the same
    way it auto-ranges EIS: run a quick CV, read the ripple, lower the
    measurement bandwidth (``bw_hz``), and re-run until the ripple drops.

    ``ripple_ratio`` is the robust standard deviation of the current's residual
    after a short centered median detrend, divided by the current span. The
    median detrend removes the smooth CV/CA trend (and its curvature) so only
    point-to-point ripple remains — so, unlike a raw second difference, a clean
    but coarsely-sampled / small-window scan does NOT read as noisy, and ripple
    over a broad band (not just the alternating/Nyquist component) is detected.

    Caveats the agent should respect: it is an UNCALIBRATED, comparative index
    (drive it down across re-runs at fixed scan_rate/e_step), not a noise
    amplitude. Mains pickup whose apparent period (set by scan_rate/e_step) is
    longer than ~2× the detrend window is treated as trend and will not register,
    so a "clean" verdict is necessary, not sufficient.

    Args:
        result: The finished ``MeasurementResult``.
        channels_requested: Channels the run asked for (so a requested channel
            with too few usable current points is reported ``insufficient_data``).

    Returns:
        ``{"noise_ok": bool, "per_channel": {ch: {points, dropped_points,
        ripple_ratio, verdict}}, "note": str}``. ``ripple_ratio`` is None when
        there are too few points; ``verdict`` is ``"clean"`` / ``"elevated"`` /
        ``"insufficient_data"``.
    """
    by_channel: dict[int, list[float]] = {}
    dropped: dict[int, int] = {}
    for dp in result.data_points:
        c = dp.variables.get("current")
        if c is None:
            continue
        c = float(c)
        if math.isnan(c):
            # Railed/overload sample: count it so a heavily-clipped trace is
            # reported unreliable rather than silently decimated (the residual
            # would otherwise treat non-adjacent survivors as consecutive).
            dropped[dp.channel] = dropped.get(dp.channel, 0) + 1
            continue
        by_channel.setdefault(dp.channel, []).append(c)

    per_channel: dict[str, dict[str, Any]] = {}
    channels = sorted(
        set(int(c) for c in channels_requested) | set(by_channel)
    )
    for ch in channels:
        cur = by_channel.get(ch, [])
        n = len(cur)
        n_dropped = dropped.get(ch, 0)
        # Too few usable points, or as many railed as survived, to trust a
        # ripple estimate.
        if n < _NOISE_MIN_POINTS or n_dropped >= n:
            per_channel[str(ch)] = {
                "points": n,
                "dropped_points": n_dropped,
                "ripple_ratio": None,
                "verdict": "insufficient_data",
            }
            continue
        span = max(cur) - min(cur)
        resid = _median_detrend(cur, _NOISE_MEDIAN_WINDOW)
        med = statistics.median(resid)
        mad = statistics.median([abs(r - med) for r in resid])
        sigma = 1.4826 * mad  # robust residual standard deviation
        ripple_ratio = (sigma / span) if span > 0 else 0.0
        verdict = (
            "elevated" if ripple_ratio >= _NOISE_RIPPLE_FLAG else "clean"
        )
        per_channel[str(ch)] = {
            "points": n,
            "dropped_points": n_dropped,
            "ripple_ratio": round(ripple_ratio, 4),
            "verdict": verdict,
        }

    flagged = [c for c, q in per_channel.items() if q["verdict"] == "elevated"]
    noise_ok = not flagged
    if noise_ok:
        note = (
            "Traces look clean (low ripple index) on all measured channels. "
            "Note this only catches ripple faster than ~the detrend window; a "
            "clean reading is not a guarantee of no pickup."
        )
    else:
        note = (
            f"Channel(s) {flagged} show an elevated ripple index — usually "
            "50/60 Hz mains pickup. Lower the max bandwidth (bw_hz, e.g. "
            "400 -> 40) and re-run at the SAME scan_rate/e_step; compare "
            "ripple_ratio to confirm it dropped."
        )
    return {"noise_ok": noise_ok, "per_channel": per_channel, "note": note}


def build_technique_config(
    technique: str, args: Optional[dict[str, Any]] = None
) -> TechniqueConfig:
    """Build a validated ``TechniqueConfig`` from defaults plus args.

    Starts from ``technique_params(technique)`` and overlays the user
    args.  Unknown parameter keys are rejected with a message listing
    the allowed keys.  Wiring validation (electrode mode, RE/CE list,
    channel ranges) is delegated to ``TechniqueConfig.__post_init__``;
    its ``ValueError`` is re-raised with a clean message.

    Args:
        technique: Technique identifier (case-insensitive, e.g.
            ``"cv"``).
        args: Optional dict of technique parameters plus the config
            keys ``channels`` (default ``[1]``),
            ``electrode_config_mode`` (default ``"external"``),
            ``re_ce_channels`` (required for ``"manual"`` mode), and
            ``continuous``.

    Returns:
        A fully validated ``TechniqueConfig``.

    Raises:
        ValueError: For unknown techniques, unknown parameter keys,
            malformed channel lists, or wiring-rule violations.
    """
    merged_args = dict(args or {})
    channels = merged_args.pop("channels", None)
    mode = str(merged_args.pop("electrode_config_mode", "external")).lower()
    re_ce_channels = merged_args.pop("re_ce_channels", None)
    continuous = bool(merged_args.pop("continuous", False))

    try:
        defaults = technique_params(technique)
    except ValueError:
        raise ValueError(
            f"Unknown technique {technique!r}. "
            f"Hardware-verified techniques: {supported_techniques()}"
        ) from None

    unknown = sorted(set(merged_args) - set(defaults))
    if unknown:
        raise ValueError(
            f"Unknown parameter(s) for technique {technique!r}: "
            f"{unknown}. Allowed parameters: {sorted(defaults)}"
        )
    params = {**defaults, **merged_args}

    if channels is None:
        channels = [1]
    if (
        not isinstance(channels, (list, tuple))
        or not channels
        or any(
            not isinstance(ch, int) or isinstance(ch, bool)
            for ch in channels
        )
    ):
        raise ValueError(
            "channels must be a non-empty list of integers "
            f"(1-16), got {channels!r}"
        )

    if mode == "manual" and not re_ce_channels:
        raise ValueError(
            "electrode_config_mode 'manual' requires an explicit "
            "re_ce_channels list (one RE/CE position per channel)."
        )

    try:
        return TechniqueConfig(
            technique=technique.lower(),
            params=params,
            channels=list(channels),
            continuous=continuous,
            re_ce_channels=(
                list(re_ce_channels) if re_ce_channels else []
            ),
            electrode_config_mode=mode,
        )
    except ValueError as exc:
        raise ValueError(f"Invalid technique configuration: {exc}") from None


class EngineAdapter:
    """Async adapter between the agent tool layer and the engine.

    The engine and connection are injected so the real hardware pair
    and the mocks substitute cleanly; this class never imports the real
    engine or opens serial ports itself.

    Required engine surface (real ``MeasurementEngine`` and
    ``MockMeasurementEngine`` both provide it): signals
    ``measurement_finished(object)`` / ``measurement_error(str)``,
    methods ``start_measurement(connection, config)`` / ``abort()`` /
    ``isRunning()``.

    Required connection surface: ``connect(port)`` / ``disconnect()`` /
    ``is_connected`` / ``port`` / ``firmware_version`` /
    ``serial_number``.

    All ``run_*`` coroutines follow the mandated sequence: connect
    one-shot completion slots first, then marshal the start call onto
    the GUI thread, then suspend on the wrapped future.  They must be
    awaited from the agent thread's asyncio loop.
    """

    def __init__(self, engine: Any, connection: Any) -> None:
        """Initialize the adapter.

        Args:
            engine: A ``MeasurementEngine``-compatible object (real or
                mock).  Must live on the GUI thread.
            connection: A ``PicoConnection``-compatible object (real or
                mock).
        """
        self._engine = engine
        self._connection = connection
        # True while the run currently in (or just out of) the engine
        # was started by the agent. GUI-thread-confined: set inside the
        # run_on_gui start closure, consumed by the main window's
        # finished/error handlers (also GUI thread), so the window can
        # suppress its modal prompts for agent-driven runs without any
        # cross-thread race.
        self._agent_run_active = False

    def script_provenance(self) -> dict[str, Any]:
        """Script count and last digest of the driven connection.

        The session log reads this before and after each tool call; a
        changed count means the call sent a script, and the digest is
        the one the serial layer logs (``Script digest=...``). The mock
        connection sends no script, so it reports zero.
        """
        return {
            "scripts_sent": int(getattr(self._connection, "scripts_sent", 0)),
            "script_digest": getattr(
                self._connection, "last_script_digest", None
            ),
        }

    def connected_port(self) -> Optional[str]:
        """The connected port name, or None when not connected."""
        connection = self._connection
        if not getattr(connection, "is_connected", False):
            return None
        return getattr(connection, "port", None)

    def consume_agent_run(self) -> bool:
        """Return whether the finishing run was agent-started, and clear.

        Call from the GUI thread (the engine's finished/error handler).
        Consume-once semantics: every engine termination emits exactly
        one of finished/error, so whichever handler runs takes the flag
        and the next user-started run can never be misattributed.
        """
        was = self._agent_run_active
        self._agent_run_active = False
        return was

    def _start_agent_run(self, config: TechniqueConfig) -> None:
        """GUI-thread helper: mark the run agent-initiated, then start.

        Marking and starting happen in one GUI-thread closure so the
        flag is always set before any engine signal can be delivered to
        the GUI handlers; a start failure unwinds the mark.
        """
        self._agent_run_active = True
        try:
            self._engine.start_measurement(self._connection, config)
        except Exception:
            self._agent_run_active = False
            raise

    def _open_circuit_watch(
        self, config: TechniqueConfig
    ) -> Optional[OpenCircuitWatch]:
        """Attach an early-stop watch to a single-channel pinned-range sweep.

        Only potentiostatic EIS on one channel with a pinned range: with
        several channels one open channel must not stop the others, and in
        autoranging the instrument finds the current itself.
        """
        if config.technique.lower() != "eis" or len(config.channels) != 1:
            return None
        params = config.params
        if str(params.get("eis_range_mode", "static")) != "static":
            return None
        cr_a = _cr_amperes(str(params.get("cr", "")))
        e_ac = params.get("e_ac")
        signal = getattr(self._engine, "data_point_ready", None)
        if not cr_a or not e_ac or signal is None:
            return None
        # The watch runs on the engine thread (direct connection: the agent
        # thread has no Qt event loop to queue to). It must not write to the
        # serial port from there, so the abort is handed to the agent's
        # asyncio loop, the same thread the abort_measurement tool uses.
        loop = asyncio.get_running_loop()
        watch = OpenCircuitWatch(
            float(e_ac),
            cr_a,
            lambda: loop.call_soon_threadsafe(self._engine.abort),
        )
        signal.connect(watch.feed, Qt.ConnectionType.DirectConnection)
        return watch

    def _detach_watch(self, watch: Optional[OpenCircuitWatch]) -> None:
        if watch is None:
            return
        try:
            self._engine.data_point_ready.disconnect(watch.feed)
        except (TypeError, RuntimeError):
            pass  # already disconnected

    async def _wait_idle(self, seconds: float = 5.0) -> None:
        """Give an aborted engine a moment to stop before the next call."""
        waited = 0.0
        while self._engine.isRunning() and waited < seconds:
            await asyncio.sleep(0.1)
            waited += 0.1

    # ---- Measurement tools -------------------------------------------------

    async def run_technique(
        self,
        technique: str,
        params: Optional[dict[str, Any]] = None,
        *,
        channels: Optional[list[int]] = None,
        electrode_config_mode: Optional[str] = None,
        re_ce_channels: Optional[list[int]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Run one measurement to completion and return a summary.

        Sequence (architecture.md mandated): reject if busy; build the
        config; connect the finished/error race future BEFORE starting;
        marshal ``start_measurement`` onto the GUI thread; await the
        future via ``asyncio.wrap_future``.

        Args:
            technique: Technique identifier (e.g. ``"cv"``).
            params: Technique parameter overrides (may also carry the
                config keys; explicit keyword arguments below win).
            channels: 1-indexed MUX channels (default ``[1]``).
            electrode_config_mode: ``"external"`` / ``"on_board"`` /
                ``"manual"``.
            re_ce_channels: Per-channel RE/CE positions (manual mode).
            timeout: Optional seconds to wait for completion before
                failing with a timeout error result.

        Returns:
            On success a compact summary dict (``ok=True``, technique,
            point counts, channels, variable names, params echo).  On
            any failure a structured error dict (``ok=False``,
            ``error`` message).  Raw data arrays are never included.
        """
        if self._engine.isRunning():
            return self._error(
                technique,
                "Engine is busy: a measurement is already running. "
                "Abort it or wait for it to finish before starting "
                "another.",
            )

        merged = dict(params or {})
        if channels is not None:
            merged["channels"] = channels
        if electrode_config_mode is not None:
            merged["electrode_config_mode"] = electrode_config_mode
        if re_ce_channels is not None:
            merged["re_ce_channels"] = re_ce_channels
        try:
            config = build_technique_config(technique, merged)
        except ValueError as exc:
            return self._error(technique, str(exc))

        # Connect the one-shot finished/error race BEFORE starting so a
        # fast (or synchronous) completion can never be missed.
        future = await_signal(
            self._engine.measurement_finished,
            self._engine.measurement_error,
            timeout=timeout,
        )
        watch = self._open_circuit_watch(config)
        try:
            await run_on_gui(self._start_agent_run, config)
        except asyncio.CancelledError:
            # Stop pressed in the narrow window before the measurement
            # starts.  CancelledError is a BaseException, so the handler
            # below does NOT catch it -- without this the one-shot slots
            # stay connected until the measurement completes on its own.
            future.cancel()
            self._detach_watch(watch)
            raise
        except Exception as exc:
            # E.g. RuntimeError from a busy engine that won the race
            # against our isRunning() pre-check. Cancelling detaches
            # the one-shot slots immediately (done-callback cleanup).
            future.cancel()
            self._detach_watch(watch)
            logger.error("start_measurement failed: %s", exc)
            return self._error(
                technique, f"Failed to start measurement: {exc}"
            )

        try:
            result = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # The agent turn was cancelled mid-measurement (user pressed
            # Stop). CancelledError is a BaseException, so it is NOT caught
            # by the `except Exception` below -- without this the LLM turn
            # unwinds but the engine keeps sweeping the remaining channels
            # (and, for banded EIS, the remaining bands). Abort the in-flight
            # run so the device actually stops, then re-raise so the turn
            # unwinds normally. engine.abort() is thread-safe (same call the
            # abort_measurement tool uses).
            try:
                self._engine.abort()
                logger.info(
                    "Measurement aborted: agent turn cancelled (Stop)."
                )
            except Exception as abort_exc:  # noqa: BLE001 - best-effort stop
                logger.warning("Abort on turn-cancel failed: %s", abort_exc)
            raise
        except Exception as exc:
            if watch is not None and watch.tripped:
                # The watch aborted a sweep on a channel with no current.
                channel = int(config.channels[0])
                used_cr = str(config.params.get("cr", ""))
                logger.info(
                    "EIS stopped early: no current on channel %d.", channel
                )
                err = self._error(
                    technique, early_stop_message(channel, used_cr)
                )
                err["stopped_early"] = True
                err["verdict"] = "no_current"
                await self._wait_idle()
                return err
            # measurement_error payload, or SignalTimeoutError.
            is_timeout = isinstance(exc, SignalTimeoutError)
            if is_timeout:
                # A timed-out await must not leave the cell energized
                # and the engine busy: best-effort abort, mirroring the
                # engine's own error-path cell-off policy.
                try:
                    self._engine.abort()
                except Exception as abort_exc:
                    logger.warning(
                        "Abort after await timeout failed: %s", abort_exc
                    )
            logger.error("Measurement failed: %s", exc)
            err = self._error(technique, str(exc))
            # A too-small pinned EIS range can make the device REJECT the sweep
            # (e!xxxx) rather than complete with railed data, so the success-
            # path quality block never runs. Carry a re-range hint on the error
            # so the agent can still step the range up. Skipped for timeouts
            # (a slow low-frequency point, not a range fault).
            if technique.lower() in _EIS_TECHNIQUES and not is_timeout:
                used_cr = str(config.params.get("cr", _EIS_FALLBACK_CR))
                suggested = next_larger_eis_range(used_cr)
                if suggested is None and used_cr != EIS_CURRENT_RANGES[-1]:
                    suggested = _EIS_FALLBACK_CR
                if suggested is not None:
                    err["suggested_cr"] = suggested
                    err["hint"] = (
                        f"The device errored during EIS at current range "
                        f"{used_cr}. If you chose a small range it may be far "
                        f"too small — re-run at a larger range (e.g. "
                        f"{suggested}) before giving up."
                    )
            return err

        finally:
            self._detach_watch(watch)

        summary = self._summarize(result, config)
        logger.info(
            "Measurement summary: %s, %d points on channels %s.",
            summary["technique"],
            summary["num_points"],
            summary["measured_channels"],
        )
        return summary

    async def run_cv(
        self, params: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Run a cyclic voltammetry measurement (see ``run_technique``)."""
        return await self.run_technique("cv", params, **kwargs)

    async def run_ca(
        self, params: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Run a chronoamperometry measurement (see ``run_technique``)."""
        return await self.run_technique("ca", params, **kwargs)

    async def run_swv(
        self, params: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Run a square-wave voltammetry measurement (see ``run_technique``).

        A thin delegation like every other ``run_*``: SWV is already a
        hardware-verified technique in the script generator, the GUI and
        the .pssession exporter, so it drives the SAME ``MeasurementEngine``
        and the SAME live plots -- there is no second render path.  The
        summary carries an ``swv_output_vars`` block (see
        :func:`swv_output_vars`) so the four declared output variables are
        accounted for by role instead of being collapsed to net current.
        """
        return await self.run_technique("swv", params, **kwargs)

    async def run_cp(
        self, params: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Run a chronopotentiometry measurement (see ``run_technique``).

        RETIRED 2026-07-28 from the agent tool surface: the EmStat Pico
        has no galvanostat and answers ``e!4001 unknown command``.  Kept for
        programmatic callers and for a future EmStat4/Nexus.  See
        ``tools.MEASUREMENT_TECHNIQUES`` to restore the tool.
        """
        return await self.run_technique("cp", params, **kwargs)

    async def run_eis(
        self, params: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Run a potentiostatic EIS measurement (see ``run_technique``)."""
        return await self.run_technique("eis", params, **kwargs)

    async def run_geis(
        self, params: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Run a galvanostatic EIS measurement (see ``run_technique``).

        RETIRED 2026-07-28 from the agent tool surface (GEIS is not a
        measurement this lab runs); kept for programmatic callers.  See
        ``tools.MEASUREMENT_TECHNIQUES`` to restore the tool.
        """
        return await self.run_technique("geis", params, **kwargs)

    def abort_measurement(self) -> dict[str, Any]:
        """Request abort of the running measurement.

        ``engine.abort()`` is documented thread-safe on both the real
        and mock engines, so no GUI-thread marshaling is required.

        Returns:
            ``{"ok": True, ...}`` when an abort was requested,
            ``{"ok": False, ...}`` when nothing was running.
        """
        if not self._engine.isRunning():
            return {"ok": False, "error": "No measurement is running."}
        self._engine.abort()
        logger.info("Abort requested via agent adapter.")
        return {
            "ok": True,
            "message": (
                "Abort requested. The run will end with the error "
                "'Measurement aborted by user.'"
            ),
        }

    def export_session(
        self, path: Optional[str] = None
    ) -> dict[str, Any]:
        """Export the last finished measurement to a .pssession file.

        Bridges the run -> characterize gap: the analysis tools only
        read .pssession files, so the agent chains run_* ->
        export_session -> analyze_*. Uses the app's existing
        ``PsSessionExporter`` (PSTrace-fidelity validated), reading
        ``engine.result`` after the engine is idle -- pure data-to-file
        work, so no GUI-thread marshaling is required.

        Args:
            path: Optional output file or directory. A directory (or a
                trailing slash) gets an auto-generated
                ``<technique>_<timestamp>.pssession`` name; a file path
                without the .pssession suffix has it appended; omitted
                entirely, the file goes to ``agent_exports/``.

        Returns:
            ``{"ok": True, "path": ..., "technique": ...,
            "num_points": ..., "channels": [...]}`` on success,
            ``{"ok": False, "error": ...}`` otherwise.
        """
        if self._engine.isRunning():
            return {
                "ok": False,
                "error": (
                    "A measurement is still running; wait for it to "
                    "finish (or abort_measurement) before exporting."
                ),
            }
        result = getattr(self._engine, "result", None)
        if result is None or result.num_points == 0:
            return {
                "ok": False,
                "error": (
                    "No finished measurement to export. Run a "
                    "measurement first."
                ),
            }

        if path:
            out = str(path)
            if out.endswith(("/", "\\")) or os.path.isdir(out):
                out = os.path.join(out, self._export_name(result))
            elif not out.lower().endswith(".pssession"):
                out = out + ".pssession"
        else:
            out = os.path.join(
                _DEFAULT_EXPORT_DIR, self._export_name(result)
            )

        try:
            abs_path = PsSessionExporter().export_pssession(result, out)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            logger.exception("export_session failed")
            return {
                "ok": False,
                "error": (
                    f"Export failed: {type(exc).__name__}: {exc}"
                ),
            }
        logger.info("Agent exported session: %s", abs_path)
        return {
            "ok": True,
            "path": abs_path,
            "technique": result.technique,
            "num_points": result.num_points,
            "channels": result.measured_channels,
        }

    @staticmethod
    def _export_name(result: Any) -> str:
        """Build ``<technique>_<timestamp>.pssession`` for *result*."""
        started = getattr(result, "start_time", None) or datetime.now()
        stamp = started.strftime("%Y%m%d_%H%M%S")
        technique = getattr(result, "technique", "") or "session"
        return f"{technique}_{stamp}.pssession"

    # ---- Device tools --------------------------------------------------------

    def list_ports(self) -> dict[str, Any]:
        """Enumerate available serial ports.

        Returns:
            ``{"ok": True, "ports": [{"device", "description",
            "hwid"}, ...]}``.
        """
        ports = [
            {
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
            }
            for p in _serial_list_ports.comports()
        ]
        return {"ok": True, "ports": ports}

    async def connect_device(self, port: str) -> dict[str, Any]:
        """Connect the injected connection to *port* on the GUI thread.

        Args:
            port: Serial port name (e.g. ``"COM6"``); any string is
                accepted by ``MockConnection``.

        Returns:
            Status dict with ``ok`` flag; on success it includes the
            full ``device_status()`` payload.
        """
        if self._engine.isRunning():
            return {
                "ok": False,
                "error": (
                    "Cannot (re)connect while a measurement is "
                    "running. Abort it first."
                ),
            }
        try:
            await run_on_gui(self._connection.connect, port)
        except Exception as exc:
            logger.error("connect_device(%s) failed: %s", port, exc)
            return {
                "ok": False,
                "error": f"Failed to connect to {port}: {exc}",
            }
        status = self.device_status()
        status["ok"] = True
        return status

    async def disconnect_device(self) -> dict[str, Any]:
        """Disconnect the injected connection on the GUI thread.

        Returns:
            Status dict with ``ok`` flag.
        """
        if self._engine.isRunning():
            return {
                "ok": False,
                "error": (
                    "Cannot disconnect while a measurement is "
                    "running. Abort it first."
                ),
            }
        try:
            await run_on_gui(self._connection.disconnect)
        except Exception as exc:
            logger.error("disconnect_device failed: %s", exc)
            return {"ok": False, "error": f"Failed to disconnect: {exc}"}
        status = self.device_status()
        status["ok"] = True
        return status

    def device_status(self) -> dict[str, Any]:
        """Return connection and engine state.

        Returns:
            Dict with ``ok``, ``connected``, ``port``, ``firmware``,
            ``serial`` and ``engine_running``.
        """
        connection = self._connection
        return {
            "ok": True,
            "connected": bool(connection.is_connected),
            "port": getattr(connection, "port", None),
            "firmware": getattr(connection, "firmware_version", None),
            "serial": getattr(connection, "serial_number", None),
            "engine_running": bool(self._engine.isRunning()),
        }

    # ---- Internal helpers ------------------------------------------------------

    @staticmethod
    def _error(technique: str, message: str) -> dict[str, Any]:
        """Build a structured error result."""
        return {"ok": False, "technique": technique.lower(),
                "error": message}

    @staticmethod
    def _summarize(result: Any, config: TechniqueConfig) -> dict[str, Any]:
        """Build the compact success summary from a MeasurementResult.

        Never includes raw data arrays -- only counts, channel lists,
        variable names, and the parameter echo.
        """
        points_per_channel: dict[str, int] = {}
        variables: set[str] = set()
        overload_points = 0
        for dp in result.data_points:
            key = str(dp.channel)
            points_per_channel[key] = points_per_channel.get(key, 0) + 1
            variables.update(dp.variables)
            if getattr(dp, "overload", False):
                overload_points += 1
        summary: dict[str, Any] = {
            "ok": True,
            "technique": result.technique,
            "num_points": result.num_points,
            "measured_channels": result.measured_channels,
            "points_per_channel": points_per_channel,
            "overload_points": overload_points,
            "variables": sorted(variables),
            "channels_requested": list(config.channels),
            "re_ce_channels": list(config.re_ce_channels),
            "electrode_config_mode": config.electrode_config_mode,
            "params": dict(result.params),
            "start_time": (
                result.start_time.isoformat()
                if result.start_time is not None
                else None
            ),
        }
        # EIS/GEIS: attach a per-channel data-quality block so the agent can
        # detect an under-ranged sweep (current railed the range → overload /
        # NaN / negative-Z') and re-range up the mode-3 ladder before moving
        # on. Other techniques carry only the generic ``overload_points``.
        if result.technique in _EIS_TECHNIQUES:
            used_cr = str(result.params.get("cr", _EIS_FALLBACK_CR))
            q = eis_quality(result, list(config.channels), used_cr)
            summary["quality_ok"] = q["quality_ok"]
            summary["quality"] = q["per_channel"]
            summary["quality_note"] = q["note"]
            # Always present so the model has an unambiguous stop signal: True
            # means the largest range is still bad (fault is the cell/wiring,
            # not the range) — do not re-range further.
            summary["rerange_exhausted"] = q["rerange_exhausted"]
            if q["suggested_cr"] is not None:
                summary["suggested_cr"] = q["suggested_cr"]
        # SWV: the same auto-range contract as EIS but on the mode-2 ladder
        # (a railed peak is the failure that matters), plus an accounting of
        # the four declared output variables by role -- so the model reports
        # net current as net current and the deferred E-AB layer (Req 15b)
        # sees an explicit contract rather than a guess.
        elif result.technique in _SWV_TECHNIQUES:
            used_cr = str(result.params.get("cr", _SWV_FALLBACK_CR))
            q = swv_quality(result, list(config.channels), used_cr)
            summary["quality_ok"] = q["quality_ok"]
            summary["quality"] = q["per_channel"]
            summary["quality_note"] = q["note"]
            summary["rerange_exhausted"] = q["rerange_exhausted"]
            if q["suggested_cr"] is not None:
                summary["suggested_cr"] = q["suggested_cr"]
            summary["swv_output_vars"] = swv_output_vars(result)
        # Current-vs-time techniques (CV, CA, …): attach a per-channel ripple
        # block so the agent can scope settings against mains/bandwidth noise
        # (run a quick windowed CV, read ripple_ratio, lower bw_hz, re-run).
        elif any("current" in dp.variables for dp in result.data_points):
            nz = cv_noise(result, list(config.channels))
            summary["noise_ok"] = nz["noise_ok"]
            summary["noise"] = nz["per_channel"]
            summary["noise_note"] = nz["note"]
        # CA: the settled current of every channel, so the agent can read a
        # whole multichannel hold from this one result.
        if result.technique == "ca":
            ss = ca_steady_state(result, list(config.channels))
            summary["steady_state"] = ss["per_channel"]
            summary["steady_state_note"] = ss["note"]
        return summary

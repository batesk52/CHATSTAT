"""Operator limits on what the agent may ask the instrument to do.

Every ``run_*`` request from the agent is checked here, after the full
parameter set is known (defaults plus the agent's values) and before any
script is built or sent. A request is checked against two limits:

* **Potential.** No applied potential may exceed a maximum absolute value,
  counting the pulse or AC swing that rides on top of it (SWV amplitude,
  EIS ``e_ac``). Step sizes are held to the same maximum.
* **Duration.** The estimated total run time of the request, over all
  channels and including equilibration, may not exceed a maximum.

A request over either limit is refused whole. Nothing is clamped, and
nothing is sent to the instrument. The refusal names each value, the limit
and what to do, and the session log records it (``limit_refused``).

The limits belong to the person at the keyboard. They live in
``app_settings.json`` (keys ``agent_max_abs_potential_v`` and
``agent_max_duration_s``) and are read on every request, so an edit takes
effect on the next request. The agent has no tool that writes that file.

Defaults:

* ``DEFAULT_MAX_ABS_POTENTIAL_V = 1.0``. The scripts for CV, CA, SWV and
  the multiplexed CA set the instrument's potential range with
  ``set_pot_range -1 1`` (``techniques/scripts.py``), so +/-1 V is the
  range the code already works in. A test keeps the two in step.
* ``DEFAULT_MAX_DURATION_S = 14400.0`` (4 hours). This is not an
  instrument limit; the code and docs have none. It is a ceiling chosen so
  that a run of more than two hours goes through, and it is about 16 times
  the longest single call in the bench goal-suite logs (a one-channel EIS
  sweep from 100 kHz to 0.1 Hz, 912 s). The default sweep on 13 channels
  fits under it; on 14 or more it does not.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

from src.data import app_settings

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_ABS_POTENTIAL_V",
    "DEFAULT_MAX_DURATION_S",
    "REFUSED_BY",
    "RequestLimits",
    "Violation",
    "check_request",
    "estimate_duration_s",
    "load_request_limits",
    "refusal_result",
]

DEFAULT_MAX_ABS_POTENTIAL_V = 1.0
DEFAULT_MAX_DURATION_S = 14400.0

#: Marker on a refusal result; the session log keys on it.
REFUSED_BY = "request_limits"

#: Slack for float rounding (0.975 V + 0.025 V must pass a 1.0 V limit).
_EPS = 1e-9

#: Parameters that are a potential on the cell, checked against the maximum.
_ABSOLUTE_POTENTIALS = (
    "e_begin", "e_end", "e_vertex1", "e_vertex2", "e_dc",
    "e_cond", "e_dep", "e_eq",
)
#: Parameters that are a size (step, pulse, swing), held to the same maximum.
_RELATIVE_POTENTIALS = ("e_step", "e_pulse", "amplitude", "e_ac")
#: The swing that rides on the absolute potentials of each technique.
_SWING_PARAMETER = {
    "swv": "amplitude", "acv": "amplitude", "eis": "e_ac",
    "dpv": "e_pulse", "npv": "e_pulse", "pad": "e_pulse",
}

#: EIS time model, fitted to six one-channel sweeps in the bench goal-suite
#: logs (100 kHz or 10 kHz down to 10, 1 and 0.1 Hz; 20 to 50 points; 16.6 s
#: to 912.5 s). Each point costs a fixed 0.7 s plus 25 periods of its own
#: frequency, points log-spaced. Against those sweeps (start-up time
#: removed) it reads 16% high on the 912 s one, 33% to 46% low on the three
#: sweeps to 1 Hz, and 52% to 55% high on the two short ones. It is a guard
#: against runaway requests, not a clock.
_EIS_POINT_S = 0.7
_EIS_PERIODS = 25.0


@dataclass(frozen=True)
class RequestLimits:
    """The two limits on an agent request."""

    max_abs_potential_v: float = DEFAULT_MAX_ABS_POTENTIAL_V
    max_duration_s: float = DEFAULT_MAX_DURATION_S

    def as_dict(self) -> dict[str, float]:
        """The limits as the refusal result and the log show them."""
        return {
            "max_abs_potential_v": self.max_abs_potential_v,
            "max_duration_s": self.max_duration_s,
        }


@dataclass(frozen=True)
class Violation:
    """One value that breaks a limit (or cannot be checked)."""

    kind: str  # "potential", "duration" or "invalid"
    parameter: str
    message: str
    requested: Optional[float] = None
    limit: Optional[float] = None
    unit: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON form for the refusal result and the session log."""
        return {
            "kind": self.kind,
            "parameter": self.parameter,
            "requested": self.requested,
            "limit": self.limit,
            "unit": self.unit,
            "message": self.message,
        }


def load_request_limits(settings_path: Optional[str] = None) -> RequestLimits:
    """Read the limits from the app settings, falling back to defaults.

    A stored value that is not a positive, finite number is ignored (with a
    warning in the log), so a typo can never switch a limit off.

    Args:
        settings_path: Optional settings-file override (for tests).
    """
    potential = app_settings.get_agent_max_abs_potential_v(settings_path)
    duration = app_settings.get_agent_max_duration_s(settings_path)
    return RequestLimits(
        max_abs_potential_v=(
            DEFAULT_MAX_ABS_POTENTIAL_V if potential is None else potential
        ),
        max_duration_s=(
            DEFAULT_MAX_DURATION_S if duration is None else duration
        ),
    )


def _number(value: Any) -> Optional[float]:
    """A finite number as float, or None (bool, text and NaN are not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _invalid(name: str, value: Any, need: str = "a finite number") -> Violation:
    return Violation(
        kind="invalid",
        parameter=name,
        message=(
            f"{name} = {value!r} is not {need}, so it cannot be checked "
            "against the limits."
        ),
    )


def _check_potentials(
    technique: str, params: dict[str, Any], limit: float
) -> list[Violation]:
    found: list[Violation] = []
    swing_name = _SWING_PARAMETER.get(technique)
    swing = 0.0
    if swing_name is not None and swing_name in params:
        swing = abs(_number(params[swing_name]) or 0.0)

    for name in _RELATIVE_POTENTIALS:
        if name not in params:
            continue
        value = _number(params[name])
        if value is None:
            found.append(_invalid(name, params[name]))
        elif abs(value) > limit + _EPS:
            found.append(Violation(
                kind="potential", parameter=name, requested=abs(value),
                limit=limit, unit="V",
                message=(
                    f"{name} = {value:g} V is larger than the potential "
                    f"limit of {limit:g} V."
                ),
            ))

    for name in _ABSOLUTE_POTENTIALS:
        if name not in params:
            continue
        value = _number(params[name])
        if value is None:
            found.append(_invalid(name, params[name]))
            continue
        peak = abs(value) + swing
        if peak > limit + _EPS:
            extra = (
                f" ({swing_name} = {swing:g} V rides on it)"
                if swing > 0.0 and swing_name is not None
                else ""
            )
            found.append(Violation(
                kind="potential", parameter=name, requested=peak,
                limit=limit, unit="V",
                message=(
                    f"{name} = {value:g} V reaches {peak:g} V{extra}, "
                    f"which is beyond the potential limit of +/-{limit:g} V."
                ),
            ))
    return found


def _positive(params: dict[str, Any], name: str, default: float) -> float:
    """A parameter that must be a finite number above zero."""
    raw = params.get(name, default)
    value = _number(raw)
    if value is None or value <= 0.0:
        raise ValueError(f"{name} = {raw!r} must be a number above zero")
    return value


def _non_negative(params: dict[str, Any], name: str, default: float) -> float:
    """A time that must be a finite number, zero or more."""
    raw = params.get(name, default)
    value = _number(raw)
    if value is None or value < 0.0:
        raise ValueError(f"{name} = {raw!r} must be a number, zero or more")
    return value


def _eis_sweep_s(params: dict[str, Any]) -> float:
    """Estimated time of one EIS sweep on one channel (see the model note)."""
    f_start = _positive(params, "freq_start", 100000.0)
    f_end = _positive(params, "freq_end", 0.1)
    n_raw = _number(params.get("n_freq", 50))
    if n_raw is None or n_raw < 1:
        raise ValueError(
            f"n_freq = {params.get('n_freq')!r} must be a whole number of 1 or more"
        )
    n = int(n_raw)
    ratio = (f_end / f_start) ** (1.0 / (n - 1)) if n > 1 else 1.0
    try:
        if abs(ratio - 1.0) < 1e-12:
            period_sum = n / f_start
        else:
            # Sum of 1/f over log-spaced points f_i = f_start * ratio**i.
            period_sum = (1.0 / f_start) * (
                (1.0 - (1.0 / ratio) ** n) / (1.0 - 1.0 / ratio)
            )
    except OverflowError:
        return math.inf
    return _EIS_POINT_S * n + _EIS_PERIODS * period_sum


def estimate_duration_s(
    technique: str, params: dict[str, Any], n_channels: int
) -> Optional[tuple[float, str]]:
    """Estimate the total run time of a request.

    Args:
        technique: Lowercase technique name.
        params: The full parameter set (defaults plus the request).
        n_channels: Number of working electrodes in the request.

    Returns:
        ``(seconds, breakdown)``, or ``None`` for a technique with no time
        model (none of those has an agent tool).

    Raises:
        ValueError: A parameter the estimate needs is not usable.
    """
    n = max(1, int(n_channels))
    t_eq = _non_negative(params, "t_eq", 0.0)
    if technique == "ca_alt_mux":
        t_run = _non_negative(params, "t_run", 300.0)
        return t_eq + t_run, (
            f"{t_eq:g} s equilibration + {t_run:g} s run, all channels in "
            "rounds"
        )
    if technique == "cv":
        begin = _number(params.get("e_begin", -0.5))
        v1 = _number(params.get("e_vertex1", 0.5))
        v2 = _number(params.get("e_vertex2", -0.5))
        if begin is None or v1 is None or v2 is None:
            raise ValueError("e_begin, e_vertex1 and e_vertex2 must be numbers")
        rate = _positive(params, "scan_rate", 0.1)
        scans = max(1, int(_non_negative(params, "n_scans", 1)))
        path = abs(v1 - begin) + abs(v2 - v1) + abs(begin - v2)
        per = t_eq + scans * path / rate
        what = f"{t_eq:g} s equilibration + {scans} scan(s) of {path / rate:g} s"
    elif technique == "swv":
        begin = _number(params.get("e_begin", -0.5))
        end = _number(params.get("e_end", 0.5))
        if begin is None or end is None:
            raise ValueError("e_begin and e_end must be numbers")
        step = _positive(params, "e_step", 0.005)
        frequency = _positive(params, "frequency", 25.0)
        steps = int(abs(end - begin) / step + _EPS) + 1
        per = t_eq + steps / frequency
        what = f"{t_eq:g} s equilibration + {steps} steps at {frequency:g} Hz"
    elif technique == "eis":
        sweep = _eis_sweep_s(params)
        per = t_eq + sweep
        what = f"{t_eq:g} s equilibration + a sweep of about {sweep:.0f} s"
    elif "t_run" in params:
        t_run = _non_negative(params, "t_run", 0.0)
        per = t_eq + t_run
        what = f"{t_eq:g} s equilibration + {t_run:g} s run"
    else:
        return None
    return n * per, f"{n} channel(s) one after another x ({what})"


def _check_duration(
    technique: str,
    params: dict[str, Any],
    n_channels: int,
    limit: float,
) -> list[Violation]:
    try:
        estimate = estimate_duration_s(technique, params, n_channels)
    except ValueError as exc:
        return [Violation(
            kind="invalid", parameter="duration",
            message=f"The run time cannot be checked: {exc}.",
        )]
    if estimate is None:
        return []
    total, breakdown = estimate
    if total > limit + _EPS:
        shown = "more than a day" if total > 86400 else f"{total:.0f} s"
        return [Violation(
            kind="duration", parameter="duration",
            requested=None if math.isinf(total) else total,
            limit=limit, unit="s",
            message=(
                f"The estimated run time is {shown} ({breakdown}); the "
                f"duration limit is {limit:g} s."
            ),
        )]
    return []


def check_request(
    technique: str,
    params: dict[str, Any],
    channels: list[int],
    limits: RequestLimits,
) -> list[Violation]:
    """Check one request against the limits.

    Args:
        technique: Technique name (any case).
        params: The full parameter set, defaults plus the request.
        channels: The working-electrode channels of the request.
        limits: The limits to hold it to.

    Returns:
        Every violation found; an empty list means the request may run.
    """
    name = technique.lower()
    return _check_potentials(name, params, limits.max_abs_potential_v) + (
        _check_duration(name, params, len(channels), limits.max_duration_s)
    )


def refusal_result(
    technique: str, violations: list[Violation], limits: RequestLimits
) -> dict[str, Any]:
    """Build the error result the model sees when a request is refused."""
    lines = "\n".join(f"- {v.message}" for v in violations)
    error = (
        "Request refused: it is outside the limits the operator set for "
        "agent requests, and it was NOT sent to the instrument. Nothing was "
        f"changed or clamped.\n{lines}\n"
        "Choose smaller values and call the tool again; do not repeat the "
        "same values. You cannot change these limits. If the task really "
        "needs more, tell the user which limit stopped it: only the person "
        "can raise it, with the keys agent_max_abs_potential_v and "
        "agent_max_duration_s in app_settings.json."
    )
    return {
        "ok": False,
        "technique": technique.lower(),
        "error": error,
        "refused_by": REFUSED_BY,
        "sent_to_instrument": False,
        "violations": [v.as_dict() for v in violations],
        "limits": limits.as_dict(),
    }

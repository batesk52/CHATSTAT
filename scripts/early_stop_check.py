"""Bench check of the impedance early stop on the MUX16 resistor test cell.

No model and no API key: the script calls the same adapter the agent's tools
call, on the real instrument, and compares each result with what the vendor
resistor test cell must give. The cell has one resistor per channel, each
between its working electrode and the RE/CE pins at the SAME position
(channel 1 is 1 kOhm, channel 16 is 8.66 kOhm).

Cases:

  open          channel 1 in the default 'external' wiring (open on this
                cell): the sweep must stop early as ``no_current``. The
                readings are stray capacitance of a few picofarads.
  too_large     channel 16 in 'on_board' wiring on the 5 mA range (a real
                resistor, AC current about 0.02% of the range): the sweep
                must stop early as ``range_too_large`` and name a smaller
                range.
  suggested     the same channel at the range the previous case named: a
                full sweep with a good verdict.
  too_small     channel 1 with its own RE/CE on the 1 uA range (10 mV on
                1 kOhm is 10 uA): must stop early as ``underranged``.
  good          channel 1 with its own RE/CE on the 25 uA range: a full
                sweep with a good verdict.
  flags_1u      channels 1 and 2 together on the 1 uA range. Two channels
  flags_100n    are not watched, so the sweep runs to the end; the script
                prints how many points carry the overload flag and how many
                are NaN. No pass or fail: it records how this instrument
                reports a range that is too small.

Run from the repository root:

    python scripts/early_stop_check.py --port COM6
    python scripts/early_stop_check.py --mock          (no instrument)

Exit code 0 when every checked case passes. Results are also written to
``early_stop_check.json`` in the current folder (``--out`` to change).
"""

from __future__ import annotations

import numpy as np  # noqa: F401  eager native import before any loop starts

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from typing import Any, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt  # noqa: E402

from scripts.agent_goal_suite import build_stack  # noqa: E402

SWEEP = {"freq_start": 100000.0, "freq_end": 10.0, "n_freq": 30, "e_ac": 0.01}
OWN_1 = {"electrode_config_mode": "manual", "re_ce_channels": [1]}
OWN_12 = {"electrode_config_mode": "manual", "re_ce_channels": [1, 2]}


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    keys = ("ok", "stopped_early", "verdict", "suggested_cr",
            "median_impedance_ohm", "stray_capacitance_pf", "num_points",
            "quality_ok", "quality", "error")
    out = {k: result[k] for k in keys if k in result}
    if isinstance(out.get("error"), str):
        out["error"] = out["error"][:160]
    return out


async def _cases(adapter: Any, mock: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def run(name: str, channels: list[int], params: dict[str, Any],
                  expect: Optional[dict[str, Any]]) -> dict[str, Any]:
        t0 = time.monotonic()
        result = await adapter.run_technique("eis", dict(params), channels=channels)
        seconds = round(time.monotonic() - t0, 1)
        passed: Optional[bool] = None
        if expect is not None:
            passed = all(result.get(k) == v for k, v in expect.items())
        rows.append({"case": name, "channels": channels, "params": params,
                     "seconds": seconds, "passed": passed, "result": _brief(result)})
        mark = "INFO" if passed is None else ("PASS" if passed else "FAIL")
        print(f"[{mark}] {name:11s} {seconds:6.1f} s  {_brief(result)}", flush=True)
        return result

    await run("open", [1], {**SWEEP, "cr": "100u"},
              {"ok": False, "stopped_early": True, "verdict": "no_current"})
    large = await run("too_large", [16],
                      {**SWEEP, "cr": "5m", "electrode_config_mode": "on_board"},
                      {"ok": False, "stopped_early": True,
                       "verdict": "range_too_large"})
    suggested = large.get("suggested_cr")
    if suggested:
        await run("suggested", [16],
                  {**SWEEP, "cr": suggested, "electrode_config_mode": "on_board"},
                  {"ok": True, "quality_ok": True})
    else:
        rows.append({"case": "suggested", "passed": False,
                     "result": {"error": "the too_large case named no range"}})
        print("[FAIL] suggested   the too_large case named no range", flush=True)
    await run("too_small", [1], {**SWEEP, "cr": "1u", **OWN_1},
              {"ok": False, "stopped_early": True, "verdict": "underranged"})
    await run("good", [1], {**SWEEP, "cr": "25u", **OWN_1},
              {"ok": True, "quality_ok": True})
    fast = {**SWEEP, "freq_end": 1000.0, "n_freq": 20}
    await run("flags_1u", [1, 2], {**fast, "cr": "1u", **OWN_12}, None)
    await run("flags_100n", [1, 2], {**fast, "cr": "100n", **OWN_12}, None)
    if mock:
        print("Mock engine: the expected values describe the resistor cell, "
              "so FAIL lines are normal here.", flush=True)
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", help="serial port of the instrument, e.g. COM6")
    parser.add_argument("--mock", action="store_true", help="use the mock engine")
    parser.add_argument("--out", default="early_stop_check.json")
    args = parser.parse_args(argv)
    if not args.mock and not args.port:
        parser.error("give --port, or --mock")

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    engine, connection, adapter = build_stack(args.mock, args.port)
    rows: list[dict[str, Any]] = []

    def worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            rows.extend(loop.run_until_complete(_cases(adapter, args.mock)))
        finally:
            loop.close()
            QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    app.exec()
    thread.join(10.0)
    try:
        connection.disconnect()
    except Exception:  # noqa: BLE001 - best effort on the way out
        pass

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1, default=str)
    checked = [r for r in rows if r.get("passed") is not None]
    failed = [r["case"] for r in checked if not r["passed"]]
    print(f"{len(checked) - len(failed)} of {len(checked)} checked cases passed"
          + (f"; failed: {failed}" if failed else "") + f". Written to {args.out}")
    return 1 if failed or not checked else 0


if __name__ == "__main__":
    sys.exit(main())

"""Smoke test: vendored-analysis tools, fully headless.

No GUI, no QApplication, no hardware, no API key.  Builds the analysis
tools from src.agent.vendor_analysis with a recording figure_sink (a
plain list), registers them into a standalone ToolRegistry, and
dispatches load_session / analyze_cv / analyze_eis / analyze_ca against
the demo session that PSTrace installs (see SAMPLE below) via the
REAL src.agent.tools.dispatch_tool path.

Asserts:
* load_session lists the three known techniques with plausible row
  counts and columns;
* analyze_cv returns ok with plausible metrics (n points, peak
  currents) and emits a PNG figure;
* analyze_eis returns ok with Rs/Rct fit info and emits PNG figures;
* every sunk figure is real PNG bytes (b'\\x89PNG' magic);
* analyze_ca returns a clean structured error (ok=false,
  is_error=True) listing the techniques that ARE available, because
  the sample session contains no CA data;
* a non-existent path produces a helpful structured error.

A hard watchdog force-exits with code 2 after 60 s.  Prints
"SMOKE PASS" and exits 0 on success, or "SMOKE SKIP" and exits 0
when the sample session is missing.

Run from the repo root:
    python examples/smoke/smoke_analysis_tools.py
"""

from __future__ import annotations

# Eager-import native deps at module top, before any asyncio loop is
# created (avoids the Windows DLL-load deadlock).
import numpy  # noqa: F401  - eager native import

import asyncio
import json
import logging
import os
import sys
import threading

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.agent.tools import ToolRegistry, dispatch_tool  # noqa: E402
from src.agent.vendor_analysis import build_analysis_tools  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_analysis_tools")

WATCHDOG_SECONDS = 60.0
# The sample is the demo session that PSTrace installs with its example data
# (Documents/PSData/Demo CV DPV EIS IS-C electrode.pssession). It is a
# PalmSens file, so this repository does not include it: copy it to
# examples/smoke/data/demo_cv_dpv_eis.pssession, or let the second path below
# find PSTrace's own copy. Without it the script prints SMOKE SKIP.
_SAMPLE_PATHS = (
    os.path.join(
        _REPO_ROOT, "examples", "smoke", "data", "demo_cv_dpv_eis.pssession"
    ),
    os.path.join(
        os.path.expanduser("~"), "Documents", "PSData",
        "Demo CV DPV EIS IS-C electrode.pssession",
    ),
)
SAMPLE = next(
    (path for path in _SAMPLE_PATHS if os.path.isfile(path)), _SAMPLE_PATHS[0]
)
EXPECTED_TOOLS = {
    "load_session", "analyze_cv", "analyze_eis", "analyze_ca",
    "analyze_cp", "analyze_ecsa", "analyze_cic",
}


def _watchdog_fire() -> None:
    """Force-exit: the smoke gate must never hang."""
    print(
        "SMOKE FAIL: watchdog fired after %.0f s" % WATCHDOG_SECONDS,
        flush=True,
    )
    os._exit(2)


def _dispatch(registry: ToolRegistry, name: str, args: dict):
    """Run one tool through the real dispatch path; return (dict, err)."""
    result_json, is_error = asyncio.run(
        dispatch_tool(registry, name, args)
    )
    return json.loads(result_json), is_error


def main() -> int:
    """Entry point. Returns the process exit code."""
    if not os.path.isfile(SAMPLE):
        print("SMOKE SKIP: sample session not found:", SAMPLE)
        return 0
    failures: list[str] = []
    figures: list[dict] = []  # recording figure_sink

    registry = ToolRegistry()
    for tool_def, handler in build_analysis_tools(
        figure_sink=figures.append
    ):
        registry.register(tool_def, handler)

    if set(registry.names()) != EXPECTED_TOOLS:
        failures.append(
            f"tool names wrong: {sorted(registry.names())!r}"
        )
    for tool_def in registry.tool_defs:
        schema = tool_def["input_schema"]
        if schema.get("additionalProperties") is not False:
            failures.append(
                f"{tool_def['name']}: additionalProperties not false"
            )

    # ---- load_session ----------------------------------------------------
    result, is_error = _dispatch(
        registry, "load_session", {"path": SAMPLE}
    )
    if is_error or result.get("ok") is not True:
        failures.append(f"load_session failed: {result!r}")
    else:
        names = {s["name"] for s in result["scans"]}
        expected = {
            "Cyclic Voltammetry",
            "Differential Pulse Voltammetry",
            "Impedance Spectroscopy [2]",
        }
        if names != expected:
            failures.append(f"load_session scan names wrong: {names!r}")
        by_name = {s["name"]: s for s in result["scans"]}
        cv = by_name.get("Cyclic Voltammetry", {})
        if cv.get("rows", 0) < 100 or "Potential (V)" not in cv.get(
            "columns", []
        ):
            failures.append(f"load_session CV entry wrong: {cv!r}")
        techs = result.get("techniques", {})
        if "EIS" not in techs or "CV" not in techs:
            failures.append(f"load_session techniques wrong: {techs!r}")
        if "CA" in techs or "CP" in techs:
            failures.append(
                f"sample should expose no CA/CP: {techs!r}"
            )

    # ---- analyze_cv ----------------------------------------------------------
    result, is_error = _dispatch(
        registry,
        "analyze_cv",
        {"path": SAMPLE, "scan_rate": 0.1, "electrode_area_cm2": 0.01},
    )
    if is_error or result.get("ok") is not True:
        failures.append(f"analyze_cv failed: {result!r}")
    else:
        if result.get("scan_name") != "Cyclic Voltammetry":
            failures.append(
                f"analyze_cv picked wrong scan: {result.get('scan_name')!r}"
            )
        metrics = result.get("metrics", {})
        if metrics.get("n_data_points", 0) < 100:
            failures.append(f"analyze_cv n_data_points wrong: {metrics!r}")
        anodic = metrics.get("peak_anodic_current_ua")
        cathodic = metrics.get("peak_cathodic_current_ua")
        if anodic is None or cathodic is None or not (
            anodic > cathodic
        ):
            failures.append(
                f"analyze_cv peak currents implausible: {metrics!r}"
            )
        if metrics.get("csc_mc_per_cm2") is None or (
            metrics["csc_mc_per_cm2"] <= 0
        ):
            failures.append(f"analyze_cv CSC missing: {metrics!r}")
        if result.get("figures_emitted") != 1:
            failures.append(f"analyze_cv figure count wrong: {result!r}")

    # ---- analyze_eis -----------------------------------------------------------
    result, is_error = _dispatch(
        registry, "analyze_eis", {"path": SAMPLE}
    )
    if is_error or result.get("ok") is not True:
        failures.append(f"analyze_eis failed: {result!r}")
    else:
        if result.get("scan_name") != "Impedance Spectroscopy [2]":
            failures.append(
                f"analyze_eis picked wrong scan: {result.get('scan_name')!r}"
            )
        metrics = result.get("metrics", {})
        if metrics.get("n_data_points", 0) < 10:
            failures.append(f"analyze_eis n_data_points wrong: {metrics!r}")
        if "rs_ohm" not in metrics or "rct_ohm" not in metrics:
            failures.append(f"analyze_eis fit info missing: {metrics!r}")
        z_1khz = metrics.get("impedance_1khz_ohm")
        if not isinstance(z_1khz, (int, float)) or z_1khz <= 0:
            failures.append(f"analyze_eis |Z|@1kHz implausible: {metrics!r}")
        if result.get("figures_emitted") != 2:
            failures.append(f"analyze_eis figure count wrong: {result!r}")

    # ---- figures are real PNGs -------------------------------------------------
    if len(figures) < 1:
        failures.append("no figures reached the figure_sink")
    for i, payload in enumerate(figures):
        png = payload.get("png")
        if not (isinstance(png, bytes) and png.startswith(b"\x89PNG")):
            failures.append(f"figure {i} is not PNG bytes: {payload!r}")
        if not payload.get("title") or not payload.get("tool"):
            failures.append(f"figure {i} payload incomplete")

    # ---- analyze_ca: clean structured error (no CA in the sample) ---------------
    result, is_error = _dispatch(registry, "analyze_ca", {"path": SAMPLE})
    if not is_error or result.get("ok") is not False:
        failures.append(
            f"analyze_ca should fail cleanly on this sample: {result!r}"
        )
    else:
        if "no CA data" not in result.get("error", ""):
            failures.append(f"analyze_ca error unhelpful: {result!r}")
        available = result.get("available_techniques", {})
        if "EIS" not in available or "CV" not in available:
            failures.append(
                f"analyze_ca error lacks available techniques: {result!r}"
            )

    # ---- bad path: helpful structured error -----------------------------------
    result, is_error = _dispatch(
        registry, "load_session", {"path": "no_such_file.pssession"}
    )
    if not is_error or "not found" not in result.get("error", "").lower():
        failures.append(f"bad-path error unhelpful: {result!r}")

    if failures:
        for failure in failures:
            print("SMOKE FAIL:", failure)
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    watchdog = threading.Timer(WATCHDOG_SECONDS, _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()
    code = main()
    watchdog.cancel()
    sys.exit(code)

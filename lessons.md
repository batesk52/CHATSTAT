# Lessons Learned

Patterns captured from mistakes, corrections, and failed validations. Agents review this at session start.

## Format

Each lesson follows this pattern:
- **Date:** When it happened
- **Mistake:** What went wrong
- **Root Cause:** Why it happened
- **Rule:** How to prevent it (imperative, actionable)

## Lessons

- **Date:** 2026-07-28
  **Mistake:** A clean install from `requirements.txt` pulled PyQt6 6.11.1, and every Qt import failed with `DLL load failed ... The specified procedure could not be found`. It looked like a broken application, not a broken wheel. `Qt6Core.dll` in that release imports `icuuc.dll`, but the `PyQt6-Qt6` wheel ships no ICU; with Anaconda on `PATH` the loader found conda's `icuuc.dll`, whose symbols are version-suffixed (for example `ucnv_open_77`) and do not satisfy Qt's imports.
  **Root Cause:** The dependency had a lower bound only (`PyQt6>=6.5`). A long-lived environment kept working on older versions, so the breakage stayed invisible until someone provisioned from scratch. The test suite could not catch it, because a failed Qt import stops collection before any test runs.
  **Rule:** Cap a dependency at the last version known to work whenever a newer release is known to break, and write the failure mode in a comment beside the pin (`PyQt6<6.11` in `requirements.txt`). Before calling the tree healthy, build a fresh environment from `requirements.txt` or `requirements-lock.txt` and run both the test suite and the smoke scripts.

- **Date:** 2026-06-09 (rule adopted when the agent was built)
  **Mistake:** Loading a native extension (numpy, scipy, pandas, matplotlib) for the first time from inside a running asyncio loop in a worker thread can deadlock on Windows while the extension's DLLs load.
  **Root Cause:** A lazy import moves the first DLL load into the agent thread's running event loop, which exposes it to a known Windows DLL-load deadlock with asyncio threads. The exact lock involved was not recorded.
  **Rule:** Import numpy, scipy, pandas, matplotlib (with the Agg backend forced before any pyplot import) and anthropic at module top, before any asyncio loop starts. Never add a lazy import of a native package inside an agent tool handler.

- **Date:** 2026-10-08
  **Mistake:** The current-range parameter `cr` is the one text-valued run parameter, and the script generator pastes it verbatim into MethodSCRIPT lines. A value that carried a line break could inject a second script line, and the agent's tool arguments, preset files and sequence files could all supply such a value.
  **Root Cause:** Nothing between the sources of `cr` (the GUI, preset and sequence files, the agent's tools) and the generator checked its form. The GUI dropdown offers only valid values, which hid the gap.
  **Rule:** Validate every value that is interpolated into MethodSCRIPT at the single point where each run is built: `TechniqueConfig.__post_init__` accepts `cr` only if it matches `_CR_TOKEN_RE` (`^[0-9]+[pnum]?$`, for example `100n`, `2u`, `1m`). Keep the second guard in the serial layer: `send_script` refuses any line that contains a line break. Give each new free-text parameter a test like `tests/data/test_cr_token_validation.py`.

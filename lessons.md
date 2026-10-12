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

- **Date:** 2026-10-11
  **Mistake:** Asked for the resistance of all sixteen channels of the vendor resistor test cell, the agent measured in the default wiring, saw current on one channel only, and stopped to ask the user how the cell was wired. It never tried the manual mode, and it wrote that a channel cannot be both the working electrode and the RE/CE.
  **Root Cause:** The system prompt named the three RE/CE modes without saying what each one routes, and the tool schema told the agent to leave the mode alone unless the user named one. The agent had no way to know that the board has two independent multiplexers, or that one conducting channel at position 15 or 16 is the signature of a per-channel RE/CE cell.
  **Rule:** Describe hardware routing to the agent in physical terms (which position each mode holds), and pair every default with the observable pattern that shows the default is wrong and the next step to take. Never write "omit unless the user specifies" for a setting the agent can verify by measurement.

- **Date:** 2026-10-11
  **Mistake:** With the wiring guidance fixed, five headless goal runs still failed: each stopped at the 40 tool-call limit before answering. A second problem hid behind it: the scorer gave zero to a correct answer, because the agent reported sixteen values in a table with the unit in the header and the scorer wanted a unit beside every number.
  **Root Cause:** A multichannel CA result carried point counts only, so reading a plain steady current cost one export plus one analysis call per channel. The cost stayed invisible in the window, which has no call limit, and showed only under the suite's cap. The scorer had been written against hand-made example sentences, not against what the agent really writes.
  **Rule:** Put the scalar a user would read off the plot into the run result itself, per channel, so a multichannel answer costs one call. Run a new goal once in the headless runner before trusting a pass seen in the window. Build scorer tests from real agent answers.

- **Date:** 2026-10-11
  **Mistake:** Asked for an impedance spectrum on one channel of a cell with per-channel RE/CE, the agent swept it in the default wiring, where that channel is open. The sweep took 15 minutes. The result's own verdict then said the sweep looked under-ranged and told the agent to re-run at a LARGER current range, and the agent did.
  **Root Cause:** `eis_quality` judged a sweep only by overload and NaN points. An open channel returns noise with NaN and negative Z' but no overload bit, so it matched the under-range signature and got advice in the wrong direction. The wiring check in the prompt also described a multichannel pattern only, which a one-channel run never shows.
  **Rule:** A verdict that proposes a corrective action must first rule out the case where no signal arrived at all: compare the measured current with the range before reading NaN as overload (`no_current`, AC current under 0.5% of the range and no overload bit). Tell the agent to confirm that a channel conducts with a few seconds of CA before any long measurement.

- **Date:** 2026-10-11
  **Mistake:** A goal that named a current range ("start at 1 uA") made the agent skip the short wiring check and start the sweep at once. The sweep ran 15 minutes on an open channel. With the wiring found, the next sweep ran 15 minutes on a range that overloaded on every point. Two such sweeps cannot fit in a 30-minute session, so the goal could not pass even when every decision after the first was right.
  **Root Cause:** The only guard against a sweep that cannot give usable data was a sentence in the system prompt, and a specific instruction from the user outweighed it. The engine ran the whole sweep and judged it only at the end.
  **Rule:** When the first seconds of a long measurement can show that it will fail, check there, in code, and stop. Do not leave it to guidance text. Return the reason and the next step in the tool result. Keep the guard narrow (one channel, pinned range) so it cannot stop a run where only part of the data is bad.

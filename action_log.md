# Action Log

Task tracking for this repository.

---

## In Progress

---

## Completed

- [x | Session | 2026-10-27] Agent models updated to the current generation (branch update-models, pull request into main)
  - Picker and default: claude-fable-5-1 (default), claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5. ADAPTIVE_THINKING_MODELS adds the four new ids and keeps claude-fable-5, claude-opus-4-8 and claude-sonnet-4-6, so a model saved in app_settings.json keeps adaptive thinking. README, smoke_agent.py and three test files follow.
  - Checked: 443 passed, 1 skipped, 1 xfailed; smoke_agent.py prints SMOKE PASS. Not checked against the live API.
  - Merged branch agent-wiring-check deleted on GitHub and locally.

- [x | Session | 2026-10-16] Goal-suite rerun on the head of the branch read; pull request merged 2026-10-16 (merge commit a8006e7)
  - Rerun: code as of 6f58bf2 (the later commits change documents only); six goals x three models x five sessions on the resistor test cell, limits of 40 tool calls and 30 min. Result: 75 scored sessions correct (25 per model) plus 15 open_or_miswired sessions judged correct by reading (5 per model). The suite file sets no expected value for open_or_miswired, so the scorer leaves those rows blank.
  - The pull request from agent-wiring-check was merged 2026-10-16 (merge commit a8006e7). This entry supersedes the "Left off" line of the 2026-10-11 23:05 entry (pull request open and not merged; rerun results not read). The open review items in that entry stand.

- [x | Session | 2026-10-11 23:05] Agent goal tests on the resistor test cell; wiring check, impedance verdicts and early stop; one pull request with two reviews
  - Completed: six goals x three models x five sessions on hardware; a bench check with no model (scripts/early_stop_check.py), 5 of 5; all blocking review findings fixed; 447 tests.
  - Left off: the pull request from agent-wiring-check is open and not merged. A full rerun of the goal suite on the head of the branch was started on the bench; its results are not read yet.
  - Next: read the rerun, then merge or revise. Open items from the reviews: the 20 pF stray-capacitance line comes from one board; no run yet on an electrode in electrolyte; the DC bias is not counted when a smaller range is sized; _wait_idle gives up silently.

- [x | agent-review-round | 2026-10-11] src/agent/open_circuit_watch.py, src/agent/engine_adapter.py, src/agent/tools.py, src/agent/agent_worker.py, scripts/goal_scoring.py, scripts/agent_goal_suite.py, scripts/early_stop_check.py - Complete. Changes from two reviews of the pull request and from a bench check on the instrument. Impedance: a third verdict, range_too_large (steady readings, largest AC current under 0.5% of a pinned range), with a smaller suggested range; a sweep is judged by its largest current, so a capacitive cell is not stopped for the small currents at low frequency; an unconnected channel is recognised as a steady capacitance under 20 pF (about 6 pF measured on the board); a NaN point decides nothing; only the exact abort message counts as an early stop; only high-to-low sweeps are watched. Hold results: near_zero marks a settled current under 0.5% of the range, and the note no longer offers a resistance formula for it. The adapter waits for the engine thread to end before it returns. Prompt: the wiring check re-measures only the channels asked for; a current range is a measuring setting the agent may change and must report; no value is better than a wrong value. Scorer: bounds, questions and nominal figures are not read as the answer. scripts/early_stop_check.py runs the early-stop cases on the resistor test cell with no model: 5 of 5 on the instrument. Tests 400 -> 447.

- [x | agent-early-stop | 2026-10-11] src/agent/open_circuit_watch.py, src/agent/engine_adapter.py, scripts/goal_scoring.py - Complete. A single-channel impedance sweep on a pinned range is stopped early when five points in a row, within the first 25, pass no current (AC current under 0.5% of the range) or carry the overload flag. The tool result says which case it was and what to do next (check the wiring, or use a larger range). A full sweep to 0.1 Hz takes about 15 minutes on the instrument, so a wrong wiring or range now costs seconds. Multichannel sweeps and autoranging sweeps are not watched. goal_scoring also reads a value written before its channel, a single-channel answer that names no channel, wide table rows cell by cell, and a number with a note in brackets. The six-goal suite then ran on three models, five sessions each: all 90 sessions answered inside the limits. Tests 378 -> 400.

- [x | agent-open-channel | 2026-10-11] src/agent/engine_adapter.py, src/agent/agent_worker.py, src/agent/tools.py, examples/goal_suites/resistor_test_cell.yaml - Complete. `eis_quality` gives a `no_current` verdict when the AC current is under 0.5% of the range with no overload bit, and its note points at the wiring instead of a larger range. The wiring check now covers a single channel and says to make it with a short CA before a long sweep. The suite scores all sixteen channels against nominal values (two from the vendor guide, fourteen inferred and labelled so) and has a range fault goal. Tests 371 -> 378.

- [x | agent-wiring | 2026-10-11] src/agent/agent_worker.py, src/agent/tools.py, src/agent/engine_adapter.py, scripts/goal_scoring.py - Complete. The system prompt says which multiplexer position each RE/CE mode holds and has a wiring check: one conducting channel at position 15 or 16 means a per-channel RE/CE cell, so the agent re-measures channels 1-14 in manual mode with same-position pairing. The CA result carries a steady_state block (settled current per channel), so a sixteen-channel answer no longer costs sixteen analysis calls. goal_scoring reads a table whose header names the unit. The manual mode ran on hardware for the first time (vendor resistor test cell, fourteen channels, by hand and through the agent). Tests 356 -> 368.

- [x | agent-log | 2026-10-09] src/agent/session_log.py, scripts/agent_goal_suite.py - Complete. Every agent conversation writes a JSON Lines session log (start, messages, tool calls and results with durations, script digests, export paths, errors, end; key scrubbed). The goal-suite runner sends YAML goals to fresh headless sessions (mock or hardware) and writes summary.csv; example suite for the resistor test cell. Tests 346 -> 356; smoke scripts pass.
- [x | release-repo | 2026-10-08] Migrated from the private development repository as ChatStat 0.1.0; history before this date lives in the archived repository.

---

## Blocked/Notes

---

## Task Format Guide

**In Progress:**
`- [WIP:Agent-#### | timestamp] component - description`

**Completed:**
`- [x | Agent-#### | timestamp] component - Complete. Notes`

**Blocked:**
`- [BLOCKED] component - Reason for blockage`

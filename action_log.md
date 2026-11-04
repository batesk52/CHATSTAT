# Action Log

Task tracking for this repository.

---

## In Progress

---

## Completed

- [x | Session | 2026-11-03] src/vendor/electrochem_analysis/analysis/cv.py, src/agent/vendor_analysis.py, src/agent/request_limits.py, src/agent/cell_switch.py, src/gui/cell_switch_banner.py, src/gui/agent_dock.py, src/gui/main_window.py, src/agent/agent_worker.py, tests/ - total charge storage capacity, no default duration limit, and request_cell_switch (branch total-csc-and-cell-switch; not pushed, not merged)
  - **CSC.** cv.py re-copied from the analysis library's csc-total-charge branch (main 20aa168 plus `calculate_csc(..., kind='cathodic'|'anodic'|'total')`), with the same two mechanical edits as before (the `utils.grouping` import and the sample names in one docstring example); the package docstring records it. The re-copy also brings the library's fixes since the first copy: input checks, peak prominence, and the sweep segmentation that had undercounted a CSC cycle starting mid-range. `analyze_cv` now reports `csc_total_mc_per_cm2`, `csc_cathodic_mc_per_cm2` and `csc_anodic_mc_per_cm2` with a note, and no unlabelled CSC; an open loop adds a note.
  - **Duration limit off by default.** `DEFAULT_MAX_DURATION_S` is None: no limit unless `agent_max_duration_s` is set; the 1 V potential limit is unchanged. The time estimate is only made when a limit is set. The default impedance sweep on 16 channels now runs.
  - **request_cell_switch** (the 23rd tool). The agent asks the person to change the solution in the cell and waits. A non-modal "Waiting for cell switch" banner in the agent dock shows the medium and the reason, with Continue and Cancel (Stop still works). Continue gives `performed` true; Cancel gives an error result telling the agent the cell was NOT switched; Stop takes the banner down and logs `stopped`. The tool refuses while a measurement runs. The system prompt gets a seventh rule only when the tool is registered (so the headless goal suite's prompt is unchanged). The request and answer go to the session log as `cell_switch_requested` and `cell_switch_resolved`, and the tool result carries one generic record: requested (medium, reason, time), performed (done, time, operator, actuator), confirmation, waited_s. The actuator interface is two calls (`present`, `withdraw`), so a robot can take over later.
  - Checked: 657 passed, 1 xfailed (53 new: 23 cell-switch logic, 13 banner and bridge, 9 CSC report, 8 duration-limit changes); all smoke scripts print SMOKE PASS. Not run on the instrument. The analysis library's own suite: 201 passed, 1 skipped.

- [x | Session | 2026-11-03] src/data/electrode_presets.py, src/data/app_settings.py, src/gui/main_window.py, tests/data/test_electrode_presets_path.py, tests/gui/test_electrode_setup_presets.py, tests/conftest.py, README.md - Electrode setups file: location is a setting, plain file documented for hand editing (branch electrode-preset-file; not pushed, not merged)
  - The setups were already a plain indented JSON file of their own (`electrode_presets.mux16el`); its location was fixed. Now `electrode_presets_path` in app_settings.json (also set from File > Electrode Setups File...) moves it. Default is the old file, so nothing changes until it is set. `~` is the home folder; a relative path is read from the settings folder, not the working folder; an explicit `path` argument still wins; a stored value that is not a file path or names a folder is ignored with a warning.
  - `ElectrodePresetManager` gained `load_problems`, `reload()` and `use_file(path)`. A setup that does not validate is skipped, the others load, the status bar or a dialog says what was skipped, and the file is copied to `.bak` before the next save.
  - README documents the file: where it is, how to point at another one, a JSON example with all three modes (a test loads that example, so it cannot go stale), every field and the rules. The on-chip die setup now says it uses the on-board wiring, which shorts RE and CE, so pads 9 and 10 are shorted outside the die (Mode B confirmed).
  - tests/conftest.py (moved from tests/agent) now points the default settings file at a temp file for every test, because the new menu action writes it.
  - Checked: 604 passed, 1 xfailed; smoke scripts print SMOKE PASS. No EIS work; the 4 h limit and the EIS estimate are unchanged.

- [x | Session | 2026-11-03] src/data/electrode_presets.py, src/gui/controls.py, src/gui/main_window.py, tests/data/test_electrode_presets.py, tests/gui/test_electrode_setup_presets.py, tests/agent/test_electrode_presets_mock_run.py - Electrode setups as named presets (branch agent-request-limits; not pushed, not merged)
  - A setup is the electrode half of a run: mode (external, on-board or manual per-WE pairing), working-electrode channels, and in manual mode each RE/CE position. It holds no technique. Every mode saves and loads; the wiring rules are `TechniqueConfig`'s (a setup is checked by building one).
  - Stored in its own file, `~/.emstat_pico_mux16/electrode_presets.mux16el`, not in the measurement preset file: the agent's save_preset reads a preset file and writes it back, which would drop a second section. The two readers refuse each other's `format`. A file or entry that does not load is skipped and the file kept as `.bak` before the next save.
  - Built-ins: separate RE/CE (external, CH1-CH16), RE/CE on the chip (on-board, CH1-CH16), one RE/CE per working electrode (manual, CH1-CH14, same-position pairing) and two 16-site die setups read from 18 pads with the middle two (pads 9, 10) not measured (pads 1-8 -> CH1-CH8, pads 11-18 -> CH9-CH16): one with a separate external RE and CE (external wiring) and one with the on-chip RE and CE on pads 9 and 10 (on-board wiring).
  - GUI: a Setup row in the Electrode Configuration panel (pick, save, delete); applying a setup sets the radios and channel panels; a hand edit shows "(Custom)"; saving over an existing name asks first.
  - Not done: the agent's run tools still take channels and wiring per request (no setup name); measurement presets and sequence steps still embed their own wiring; not run on the instrument.
  - Checked: 574 passed, 1 xfailed (77 new setup tests across the data, mock-run and GUI files, 2 duration tests and 3 boundary tests: a CV from -1 V to +1 V passes, a square wave to 1 V is refused by its amplitude); smoke scripts print SMOKE PASS; the real settings folder gained no file during the tests.

- [x | Session | 2026-11-03] src/agent/request_limits.py, src/agent/engine_adapter.py, src/agent/session_log.py, src/data/app_settings.py, tests/agent/ - Potential and duration limits on agent requests (branch agent-request-limits; not pushed, not merged)
  - Every run_* request is checked, defaults included, before a script is built. Over the limit means refused whole: no clamping, nothing sent. The refusal names each value, the limit and what to do, and the session log gets a `limit_refused` event.
  - Defaults: 1 V largest potential (swing counted, steps held to the same value), taken from the `set_pot_range -1 1` that the technique scripts already set; 14400 s (4 h) largest estimated run time over all channels (first set to 1800 s, raised the same day so runs of more than two hours pass); the code and docs hold no instrument figure, so this is a ceiling, about 16 times the longest bench call (a 912 s one-channel sweep to 0.1 Hz). The default sweep fits under it on 13 channels, not on 14 or more. Both are read from app_settings.json on every request (`agent_max_abs_potential_v`, `agent_max_duration_s`); no agent tool writes them.
  - The EIS time estimate is a fit to six bench sweeps from the goal-suite logs (46% under to 55% over); SWV was not timed on the bench. Limits apply per request and to the agent only: the Start button and the sequencer are not checked.
  - Checked: 47 tests in tests/agent/test_request_limits.py; all smoke scripts print SMOKE PASS. tests/agent/conftest.py now points the default settings file at an empty temp file, so no agent test reads the person's settings. Not run on the instrument.

- [x | Session | 2026-10-27] Agent models updated to the current generation (branch update-models, pull request into main)
  - Picker and default: claude-sonnet-5-5 (default), claude-fable-5-1, claude-opus-5-5, claude-haiku-5-5. ADAPTIVE_THINKING_MODELS adds the four new ids and keeps claude-fable-5, claude-opus-4-8 and claude-sonnet-4-6, so a model saved in app_settings.json keeps adaptive thinking. README, smoke_agent.py and three test files follow.
  - Checked: 447 passed, 1 xfailed (laptop); smoke_agent.py prints SMOKE PASS. Not checked against the live API.
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

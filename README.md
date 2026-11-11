# ChatStat

ChatStat is an open-source desktop application for the PalmSens EmStat Pico potentiostat on its sixteen-channel MUX16 multiplexer board. It runs five techniques that have been verified on the instrument: cyclic and square-wave voltammetry, chronoamperometry, multiplexed chronoamperometry and impedance spectroscopy. It draws one live curve per channel and chains saved presets into sequences. Each run exports as one CSV file per channel, with a header that records how the data were made, and as a `.pssession` file that the vendor's PSTrace program can open. An optional language-model agent in the same window drives the same measurement engine as the buttons.

## Requirements

- Windows 11. Linux and macOS are untested.
- Python 3.10 or later (tested with 3.12.7).
- PyQt6 below 6.11, because the 6.11.1 wheel ships no ICU library and would not import on Windows 11.
- An EmStat Pico MUX16 board on USB, tested with module firmware 1.6 (espico1601).
- For the agent: network access and an Anthropic API key (a paid service).

## Install

From the repository folder:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m src.gui.main_window
```

`requirements.txt` gives minimum versions. `requirements-lock.txt` pins the exact versions that the test suite passed with (Python 3.12.7 on Windows 11); install it instead to reproduce that environment. Its pins need Python 3.12 or later.

## First run without hardware

Without an instrument you can still open the window and build presets and sequences. To try the agent dock, set `EMSTAT_AGENT_MOCK=1` before you start the window (PowerShell: `$env:EMSTAT_AGENT_MOCK = "1"`). The agent's tools then use a built-in mock engine with synthetic data, and its runs animate the live plots. You still need an API key, and the manual controls still drive the real instrument. The tests and the smoke scripts in `examples/smoke/` need neither an instrument nor a key. `examples/data/` holds the paper's example recordings (see its README).

## Connecting the instrument

Connect the board by USB, pick its COM port in the **Port** box and click **Connect**. The Connection panel then shows the module's firmware version.

## Setting up a run

- **Technique.** The list offers the five verified techniques; CA (MUX-Alternating) is the multiplexed chronoamperometry. Eleven more templates are hidden: untested, impossible on this instrument, or switched off.
- **Current range.** Outside EIS, your choice is a ceiling: the run starts on it, and the instrument autoranges between 100 nA and that ceiling. It uses the lowest of its own ranges that covers your choice (100 uA selects its 125 uA range). EIS uses the ten high-speed ranges, 100 nA to 5 mA, pinned for the whole sweep by default, because switching inside the sweep corrupted some spectra on the tested firmware. EIS can instead autorange up to a ceiling, which suits high-impedance two-electrode cells.
- **Max Bandwidth** is the measurement filter, chosen per run from 0.4 Hz upward. The default is 400 Hz, and EIS fixes it at 200 kHz.
- **Electrode Configuration.** **Separate CE + RE (external)** routes the reference and counter electrodes through multiplexer position 15. **On-board RE/CE (combined)** uses position 16. **Manual (per-WE pairing)** pairs each working electrode with its own reference/counter channel among positions 1 to 14. The first two modes have been used with sixteen working electrodes, but no validation dataset was recorded; the manual mode has not been run on hardware. See `docs/enclosure_design.md` and `docs/pre_enclosure_smoke.md`. The **Setup** row above the three choices picks a saved electrode setup by name; see Electrode setups below.
- **Channels.** Tick any of the sixteen working electrodes.
- **Settle time** (100 ms by default) is the wait after each switch before sampling. In CA (MUX-Alternating), **Samples per channel visit** sets how many samples of nominally 10 ms are averaged into one point, and **t interval** is the requested round time.

**Start** begins the run. **Halt**, **Resume** and **Stop** act on the instrument directly. Every run opens a new plot tab.

## Presets and sequences

A preset is a named configuration: technique, parameters, channels and wiring. Presets live in one file, by default `~/.emstat_pico_mux16/presets.mux16`. The first launch creates it with three built-in presets (CV, EIS and CA). To move presets to another computer, copy the file and use **Import preset file...** at the end of the preset list. The settings folder and the `.mux16` and `.mux16seq` formats keep the names of earlier versions, so existing presets and sequences keep working.

The **Sequence** dock runs presets back to back, each step with a repeat count and an idle delay. Each step stores its full configuration, so a saved `*.mux16seq` file works on any computer and survives later edits to its source preset. The whole queue is checked before the first step starts.

## Electrode setups

An electrode setup is the electrode half of a measurement: which working electrodes are measured and how the reference and counter electrodes are wired. It has no technique and no parameters, so one setup serves any technique, and you pick it by name instead of choosing between the three wiring modes. Pick one in the **Setup** row of the Electrode Configuration panel; the wiring choice and the channel boxes (or, for per-electrode wiring, the pairing table) are set to match. If you then change the wiring or a channel by hand, the row goes back to **(Custom)**. The save button stores the current wiring and channels under a name you give, and asks before it replaces a setup of the same name. The trash button deletes a setup you saved. Every wiring mode saves and loads, including per-electrode pairing.

Five setups are built in:

- **Separate reference + counter electrodes.** The external wiring (position 15), CH1 to CH16.
- **Reference + counter on the chip (shorted).** The on-board wiring (position 16), CH1 to CH16.
- **One RE/CE per working electrode (CH1-CH14).** Per-electrode pairing, each CH n with RE/CE n, as on the vendor resistor test cell.
- **16-site die, external RE + CE.** For a die with 18 pads in a row. Pads 1 to 8 are CH1 to CH8 and pads 11 to 18 are CH9 to CH16; the middle two (pads 9 and 10) are not measured. A separate reference electrode and counter electrode are wired in from outside, so this uses the external wiring (position 15).
- **16-site die, on-chip RE + CE (pads 9, 10).** The same die and the same channels, but the reference and counter are the ones on the die: pad 9 is the reference and pad 10 the counter. It uses the on-board wiring (position 16), which shorts the reference and counter together, so short pads 9 and 10 together outside the die. Pick the one that matches how the die is connected; the two differ only in the reference and counter.

### The setups file

Your setups are kept in one plain text file, by default `~/.emstat_pico_mux16/electrode_presets.mux16el`. It is separate from the measurement presets, so saving a measurement preset can never remove a setup. The built-ins are in the program; the file holds only the setups you added or changed. Deleting a setup that replaced a built-in brings the built-in back.

**Where the file lives is a setting.** Choose it with **File > Electrode Setups File...** (pick an existing file, or type a new name to start one), or set `electrode_presets_path` in `~/.emstat_pico_mux16/app_settings.json`:

```json
{ "electrode_presets_path": "D:/lab/shared/electrode_setups.mux16el" }
```

Use an absolute path, or `~` for your home folder. A relative path is read relative to `~/.emstat_pico_mux16/`, never the folder you started ChatStat from. Delete the key to go back to the default file. The file is created, with its folders, the first time you save a setup; nothing is written before that. A value that is not a file path, or that names a folder, is ignored with a warning in the log and the default file is used.

**Editing the file by hand.** Open it in any text editor. Each setup is one entry under `presets`:

```json
{
  "format": "mux16-electrode-presets",
  "version": 1,
  "presets": {
    "bench_external": {
      "name": "Bench, external RE + CE",
      "electrode_config_mode": "external",
      "channels": [1, 2, 3, 4],
      "description": "CH1 to CH4 against the external reference and counter."
    },
    "bench_on_board": {
      "name": "Bench, on-board RE/CE",
      "electrode_config_mode": "on_board",
      "channels": [5, 6]
    },
    "bench_pairs": {
      "name": "Bench, paired",
      "electrode_config_mode": "manual",
      "channels": [2, 3],
      "re_ce_channels": [5, 6],
      "description": "WE CH2 with RE/CE CH5, WE CH3 with RE/CE CH6."
    }
  }
}
```

- The entry name (`bench_external`) is the key. Use letters, digits and underscores. A key that matches a built-in (`separate_re_ce`, `on_chip_re_ce`, `per_electrode_re_ce`, `die_16_external`, `die_16_on_chip`) replaces that built-in.
- `name` is what the Setup row shows.
- `electrode_config_mode` is `external` (Mode A, RE/CE at position 15), `on_board` (Mode B, position 16) or `manual` (Mode C, a pairing of your own).
- `channels` are the working electrodes, in run order, without repeats: CH1 to CH16 in the first two modes, CH1 to CH14 in `manual`.
- `re_ce_channels` is only for `manual`: one RE/CE position (1 to 14) for each entry of `channels`, in the same order. Leave it out, or empty, in the other modes.
- `description` is optional text; `site_labels` is optional too (`{"9": "pad 11"}` names what a channel is on your device).

ChatStat reads the file when it starts and when you choose it in the menu, so restart it after editing by hand. A setup that does not follow these rules is skipped, the others still load, and the status bar says what was skipped. Before your next save in the app, the whole file is copied to `.bak`, so a hand edit is never lost. A file that is not valid JSON is treated the same way: the built-in setups still work, and the file is kept as `.bak` before a save writes a new one.

Measurement presets and sequence steps still carry their own wiring and are unchanged. The agent's run tools are unchanged: they take the channels and wiring in each request.

## Exports

Each run exports in two formats:

1. One CSV file per channel (`ch01.csv`, `ch02.csv`, ...). Lines starting with `#` record the technique, channel, timestamp, device serial number, firmware version, all parameters, the electrode configuration and the reference/counter channel. A `timestamp` column follows, then the data in an order that suits the technique: potential and current for voltammetry; current and applied potential for amperometry; frequency, impedance magnitude, real and imaginary parts, and phase for EIS (magnitude and phase are computed from the real and imaginary parts).
2. A `.pssession` file in PSTrace's format. Its technique codes and method keys were checked against files that PSTrace wrote (see `docs/references/pstrace_method_keys.md`).

Export folders, named `YYYYMMDD_HHMMSS_<technique>`, go under the folder set in **File > Set Export Folder...** (default `exports/`).

**Auto-save.** With **Auto-save CSV during measurement** ticked, the CSV files are written during the run and flushed to disk at every multiplexer loop boundary. In CA (MUX-Alternating) a boundary follows every channel visit, so a crash or a power loss keeps everything up to the last visit. Other techniques reach a boundary only when a channel's measurement ends, so a single-channel run reaches the disk only when it finishes. In auto-saved EIS files the magnitude and phase columns are blank. **File > Export Results...** writes a complete second copy, `.pssession` included, into a new folder.

Every auto-saved run also keeps the exact script sent to the instrument, as `_script.mscr`, and the log shows the first 16 hexadecimal characters of its SHA-256 digest. Exports without auto-save keep no script.

## The agent dock

The **Agent** dock is a chat panel. Set the API key and the model in **File > Agent Settings...**; the `ANTHROPIC_API_KEY` environment variable is the fallback. The key is stored in plain text in `~/.emstat_pico_mux16/app_settings.json`. The default model identifier is `claude-sonnet-5-5`.

There are 26 tools in six groups:

- Device (6): `list_ports`, `connect_device`, `disconnect_device`, `device_status`, `abort_measurement`, `export_session`.
- Runs (4): `run_cv`, `run_swv`, `run_ca`, `run_eis`.
- Analysis (8): `load_session`, `analyze_cv`, `analyze_swv`, `analyze_eis`, `analyze_ca`, `analyze_ecsa`, and, for data recorded on other instruments, `analyze_cp` and `analyze_cic`.
- Presets and sequences (4): `save_preset`, `load_preset`, `save_sequence`, `load_sequence`.
- Cell (1): `request_cell_switch`.
- Memory (3): `save_memory`, `list_memories`, `read_memory`.

The run tools use the same engine as the **Start** button, and the agent never sees raw MethodSCRIPT or serial commands. Every run result reports how many points the instrument flagged as overloaded. EIS and SWV results add a verdict for each channel, such as "underranged", and suggest a current range one step up; when even the largest range fails, the result says the fault is in the cell or the wiring. CV and CA results add a ripple index. `analyze_eis` withholds the charge-transfer resistance, and gives a lower bound, when the sweep did not reach the top of the semicircle. `analyze_swv` withholds a peak on a sweep edge or below its noise threshold. The dock's **Stop** button cancels the model's turn and aborts the measurement that turn started.

**Session logs.** Every agent conversation writes a log, one JSON object per line, to `~/.emstat_pico_mux16/agent_sessions/YYYYMMDD_HHMMSS_<id>.jsonl`. The log pane shows its path when the conversation starts. The log records the start of the session (application version, model, tool names, mock or hardware, port), each message from you and from the agent, each tool call with its arguments, and each result exactly as the model saw it, with its duration. It also records errors and the end of the session, a `limit_refused` line for each request that the limits below turned away, a `cell_switch_requested` and a `cell_switch_resolved` line for each solution change the agent asked for, and a `memory_saved` line for each memory note. Each line has a time stamp and is written to disk at once, so a crash leaves a readable file. When a tool call sends a script to the instrument, its result line carries the script digest that the log pane shows (`Script digest=...`); an `export_session` result line carries the exported file path. The API key and request headers are never written.

**Charge storage capacity.** Given a scan rate and an electrode area, `analyze_cv` reports the charge storage capacity (CSC, in mC/cm2) as three labelled numbers. `csc_total_mc_per_cm2` is the usual one: the integral of the absolute current over one full cycle, both sweeps, divided by scan rate and area. `csc_cathodic_mc_per_cm2` and `csc_anodic_mc_per_cm2` are the area between the two sweeps where the current is negative or positive. For a capacitive loop the total is about the sum of the other two; with current of one sign on both sweeps (for example oxygen reduction) the two area numbers can be near zero while the total is not. The result carries a note with these definitions, and the agent is told to say which one it quotes. A CV that does not close (it ends well short of where it started) is flagged, and its total covers only the part that was recorded.

**Changing the solution.** The agent cannot change the solution in the cell, and it does not guess. Before any step that needs a different solution than the one now in the cell (for example PBS, then a plating bath, then PBS again), its instructions tell it to call `request_cell_switch` with the new medium and the reason, and to wait. The dock then shows a **Waiting for cell switch** banner: "Switch the cell to the new medium, then press Continue", with **Continue** and **Cancel**. The agent's turn is paused until you press one; the rest of the window, and the **Stop** button, keep working. Continue tells the agent the cell is switched (`performed` true) and it goes on. Cancel tells it the cell was not switched; it runs nothing for that step and asks what to do. Stop ends the wait and takes the banner down. The tool refuses while a measurement is running.

Each request is logged twice, `cell_switch_requested` when it is made and `cell_switch_resolved` when it ends, and the tool result carries the same record, whoever does the switching: what was requested (medium, reason, time), whether it was performed (done, time, who, and by what: `human` here), and how it ended (`confirmed`, `cancelled` or `stopped`) and how long it took. The "who" is the login name of the computer. The record format does not depend on a person doing the switch, so a robot that implements the same two calls (show a request, take it down) can answer in its place.

**Memory notes.** A chat can be saved as a memory note, so that a later session, with you or with someone else, can find out how the work was done. Press **Save chat to memory** under the chat, or ask for it in words. The agent then calls `save_memory` and writes one text file with these headings: Goal, Electrode setup, Electrochemical setup, What was done, What was found, Problems and fixes, Data files, Open questions. A note is for the context that the data files do not hold: what the electrodes were, what was in the cell, why a run was repeated. The agent is told to write "not stated" for anything the chat does not show. ChatStat itself adds a last section, Runs recorded in the session log: every run, export and solution change since the last saved note, with the arguments as sent and whether it ran, failed, was refused or was stopped. That list is copied from the session log; the agent does not write it.

In a later session, ask in plain words, for example "how did we test the platinum sites?". The agent's instructions tell it to call `list_memories` and then `read_memory` before it answers, and to name the note it used. Both calls show in the chat and in the session log.

Notes live in `~/.emstat_pico_mux16/memories/`, one Markdown file each, named `YYYYMMDD_HHMMSS_<title>.md`. To use another folder, for example one on a shared drive, choose it with **File > Agent Memory Folder...** or set `agent_memory_dir` in `~/.emstat_pico_mux16/app_settings.json` (`~` is the home folder; a relative path is read from the settings folder). The setting is read on every call. You can read, edit, delete or write a note by hand in any text editor: every `.md` file in the folder is listed.

A note is a summary, and nothing checks it against the data. The agent is told to treat a note as context, never to report a number from a note as measured, and to measure again when a value matters. Keep the exported files as the record. The headless goal suite does not use the memory tools.

Limits: agent runs do not auto-save; `export_session` writes the `.pssession` file, and **File > Export Results...** writes the CSV files. The agent's chronoamperometry measures channels one after another, not in multiplexed rounds. A loaded sequence returns to the model as steps to run one by one, not to the sequencer. Potentials and durations in a request are checked against limits (next section). The agent's reliability has not been measured.

### Request limits

Before an agent run tool (`run_cv`, `run_ca`, `run_swv` or `run_eis`) builds a script, ChatStat checks the whole request, defaults included, against a potential limit and, if you set one, a duration limit:

- **Potential.** No potential may be larger than 1 V in size, positive or negative; 1 V itself is allowed, so a CV from -1 V to +1 V runs. The square-wave or AC amplitude that rides on a potential counts too, and a step size is held to the same value. The default is the +/-1 V range that the technique scripts set on the instrument (`set_pot_range -1 1`).
- **Duration.** There is no duration limit by default: the instrument has none and long runs are wanted. To set one, add `agent_max_duration_s` (seconds) to the settings file; then the estimated total run time, over all selected channels and including equilibration, may not pass it. Remove the key to turn it off again. The estimate is only made when a limit is set. Chronoamperometry counts hold time per channel and cyclic voltammetry counts path length over scan rate; both track the bench times to within the few seconds of start-up. Square-wave voltammetry counts steps over frequency and was not timed on the bench. For impedance the estimate is a fit to six bench sweeps, between 46% under and 55% over their times. So the estimate is a guard against runaway requests and not a clock; for scale, the default impedance sweep is about 15 minutes on one channel (912 s on the bench), so on 16 channels one after another it is about 4 hours.

A request over a limit that is set is refused whole. Nothing is clamped, and nothing is sent to the instrument. The agent gets an error that names each value, the limit and what to change, and the session log gets a `limit_refused` line. A value that cannot be checked (not a number, not finite, or a scan rate or frequency of zero) is refused too.

You set the limits in `~/.emstat_pico_mux16/app_settings.json`: `agent_max_abs_potential_v` in volts and, if you want one, `agent_max_duration_s` in seconds. They are read again at every request, so an edit applies at once. A stored value that is not a positive number is ignored with a warning, and the default applies (1 V, and no duration limit). The agent has no tool that can change them. The limits apply to the agent only: the **Start** button and the **Sequence** dock are not checked. They apply to each request alone; they do not cap the total over several requests.

## Goal suites

`scripts/agent_goal_suite.py` sends written goals to the agent without the window and records how each attempt went. A YAML file lists the goals (an id, the request in plain words, a repeat count and, optionally, expected values). Each repeat is a fresh agent session with its own session log. A session stops when the agent ends its turn, or after a maximum number of tool calls or minutes. The script writes `summary.csv` with one row per session: goal, run, whether it completed, why it stopped, tool calls, wall time, score, token counts, the agent's final message and the log path. The suite uses the device, run and analysis tools; it leaves out the preset and sequence tools, which open file dialogs.

```
python scripts/agent_goal_suite.py examples/goal_suites/resistor_test_cell.yaml --port COM5
python scripts/agent_goal_suite.py examples/goal_suites/resistor_test_cell.yaml --mock --repeats 1
```

`examples/goal_suites/resistor_test_cell.yaml` holds six goals for the resistor test cell that ships with the MUX16, one of them a fault goal that names a current range that is too small. The script needs `pyyaml` (`pip install pyyaml`) and an API key, also with `--mock`. Output goes to `agent_goal_runs/<stamp>_<suite>/` unless `--out` is given.

## Running the tests

```
pip install pytest
set QT_QPA_PLATFORM=offscreen
python -m pytest -q
```

(PowerShell: `$env:QT_QPA_PLATFORM = "offscreen"`.) The 356 tests need no instrument or API key and passed with the versions in `requirements-lock.txt`. Each smoke script in `examples/smoke/` prints `SMOKE PASS`. `smoke_analysis_tools.py` reads the demo session that PSTrace installs, which is not included here; without it the script prints `SMOKE SKIP`.

## Known limitations

- **One instrument family:** the EmStat Pico on its MUX16 board.
- **Windows.** The serial connection calls pyserial's `set_buffer_size`, which exists only on Windows.
- **Multiplexer cadence.** Each channel gets its settle wait and at least one 10 ms sample per round, so a sixteen-channel round takes at least about 1.8 s at the default settle. The requested round time is a target; each exported point carries its own time stamp, so the real cadence is recorded. A sixteen-channel multiplexed run cannot resolve sub-second events. Each electrode floats at open circuit between visits, so multiplexed amperometry is expected to be noisier than single-channel amperometry; the two have not been compared on this instrument. `docs/multiplexer_limitations_and_lessons.md` gives advice for nanoampere-level work.
- **Current ranges.** The low-speed list includes 10 mA and 100 mA, which lie above the instrument's largest range, 5 mA.
- **Bandwidth.** The documentation recommends 0.4 Hz for multiplexed amperometry, based on one sixteen-channel session in ferricyanide (detrended noise of 5.4 nA per channel at 0.4 Hz against 88 nA at 4 Hz) and on routine use since. That session was not a controlled comparison: the runs were made one after another, on electrodes whose drift dominated the raw signal, with no 400 Hz control run. A single-pole filter at 0.4 Hz would also have a time constant (about 0.4 s) longer than one channel visit in that session, so part of each reading could come from the previous channel. The default therefore stays at 400 Hz, the value in the vendor's multiplexer example.

## Adding a technique

1. Write the script template and its default parameters in `src/techniques/scripts.py`.
2. Add plot axes in `src/gui/plot_widget.py`, a display name in `src/gui/controls.py` and a session-file layout in `src/data/pssession_exporter.py`.
3. Run it on the instrument and check its session-file keys against a file that PSTrace wrote. Then add its name to `_VERIFIED_TECHNIQUES` in `src/techniques/scripts.py` to show it in the technique list.

## License

MIT license (see `LICENSE`). The code depends on PyQt6, which Riverbank Computing licenses under the GPL v3 or a commercial license. A bundled executable that includes PyQt6 would therefore ship under GPL v3 terms, unless you hold a commercial PyQt license.

## Citation

Bates, K. <paper title>, 2026, in preparation.

## Reporting issues

Open an issue in this repository with your Windows and Python versions, the firmware version, the technique and its parameters, and the log output (set the log to DEBUG). For agent problems, add the model identifier and your prompt.

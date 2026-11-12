# Product Requirements Document

## Project Overview

**Project Name:** ChatStat
**Version:** 0.1.0
**License:** MIT

## Problem Statement

The PalmSens EmStat Pico MUX16 board lets one potentiostat read sixteen working electrodes in turn, but it needs MethodSCRIPT or custom software to run, and the vendor's application does not switch the board's channels automatically or show each channel as its own curve. ChatStat is a desktop application that runs the board directly over serial, with live per-channel display, chained methods and analysis-ready exports.

## Goals

### Primary Goals
1. Operate the EmStat Pico MUX16 from a Python desktop application with live plot updates
2. Run the techniques this instrument supports, with the hardware-verified ones in the interface
3. Run sixteen-channel multiplexed measurements with per-channel visualization and export

### Success Criteria
- [x] Connect to the EmStat Pico MUX16 over USB serial and confirm the firmware version
- [x] Run any verified technique on any combination of the sixteen channels
- [x] Display live measurement data during acquisition
- [x] Export results as CSV and .pssession files

All four are met and were checked on hardware with module firmware 1.6 (espico1601).

Requirement numbers are kept stable because code comments cite them; numbers 14, 16 and 17 are not used.

## Functional Requirements

### Req 1: Serial Communication
**Description:** Establish and manage the serial connection to the EmStat Pico MUX16
**Acceptance Criteria:**
- Must connect at 230400 baud, 8N1, with XON/XOFF flow control
- Must detect the device by querying its firmware version (`t` command)
- Must handle the transitions between idle mode and script execution mode
- Must handle disconnect and reconnect gracefully

### Req 2: MethodSCRIPT Protocol
**Description:** Generate MethodSCRIPT and parse its data packets
**Acceptance Criteria:**
- Must decode hex-encoded data packets (28-bit values with an SI prefix)
- Must map variable type codes to measurement quantities (potential, current, impedance and others)
- Must generate valid MethodSCRIPT for all supported techniques
- Must handle measurement loop markers (M, *, L, +)
- Must refuse any script line that contains a line break, so a parameter value cannot inject a command

### Req 3: MUX16 Channel Control
**Description:** Switch the sixteen multiplexer channels through the GPIO lines
**Acceptance Criteria:**
- Must configure the GPIO pins for MUX16 addressing (10 bits: 2 enable + 4 RE/CE + 4 WE)
- Must calculate correct addresses for channels 1-16, with WE and RE/CE addressed independently
- Must support sequential and selective channel scanning
- Must support the multiplexed chronoamperometry that revisits every channel in rounds

### Req 4: Technique Support
**Description:** Generate MethodSCRIPT for the electrochemical techniques this instrument can run

The technique layer holds templates for sixteen techniques in four tiers. `_VERIFIED_TECHNIQUES` in `src/techniques/scripts.py` is the single source of truth for what the GUI exposes; `supported_techniques()` returns it.

**Acceptance Criteria:**

*Tier 1 - in the GUI and verified on hardware (the product surface):*
- Must support CV, SWV, CA, CA (MUX-Alternating) and EIS in the technique list
- Must expose technique-specific parameters (potential range, scan rate, frequency and others)
- Must include `cell_on` / `cell_off` and `on_finished` safety handling
- A cyclic voltammogram must end where it began

*Tier 2 - template exists, hidden, untested:*
- LSV, NPV, PAD (preconcentration followed by differential-pulse stripping) and OCP use commands that the MethodSCRIPT manual lists for the EmStat Pico but have not been run on it. Adding a name to `_VERIFIED_TECHNIQUES` exposes it

*Tier 3 - not possible on this hardware:*
- **CP** and **LSP** need a galvanostat, which the EmStat Pico does not have (asked to run CP, the device answers `e!4001 unknown command`). The CP template is kept for other instruments, but there is no `run_cp` agent tool, because a tool that can only fail makes the agent spend a turn discovering that. `analyze_cp` still analyzes CP data recorded on other instruments
- **ACV**, **FCV** and **FCA** are available only on the EmStat4 and Nexus; the FCA template raises a clear `ValueError` instead of emitting an invalid script

*Tier 4 - switched off:*
- **DPV** passed a bench check, but its PSTrace method keys (`e_pulse` / `t_pulse`) cannot be verified without a native PSTrace DPV file, and unverified keys make PSTrace silently fall back to its own defaults. It stays out of `_VERIFIED_TECHNIQUES`; `_gen_dpv` remains
- **GEIS** (galvanostatic impedance) is withdrawn: never in the GUI, never validated on hardware, and not an agent tool. `_gen_geis` and `EngineAdapter.run_geis` remain for programmatic callers
- Neither is a code deletion, so both can return once a reference file (DPV) or a need (GEIS) exists

### Req 5: Live Plotting GUI
**Description:** Real-time visualization during measurements
**Acceptance Criteria:**
- Must update plots during acquisition, not only after completion
- Must color-code channels in multiplexed runs
- Must display technique-appropriate axes (current against potential for voltammetry, current against time for amperometry, Nyquist with a Bode view for EIS)
- Must support zoom, pan and auto-scale

### Req 6: Main Application GUI
**Description:** Full application window for instrument control
**Acceptance Criteria:**
- Must provide a connection panel (COM port selection, connect/disconnect)
- Must provide a technique selector with parameter fields
- Must provide a channel selector (checkboxes for channels 1-16)
- Must provide measurement controls (start, stop/abort, halt/resume)
- Must show device status and measurement progress

### Req 7: Data Export
**Description:** Save measurement results in two formats
**Acceptance Criteria:**
- Must export per-channel CSV files with a header (technique, channel, timestamp, device serial, firmware, parameters, derived wiring label, RE/CE channel) and technique-ordered columns
- Must export a .pssession file in PSTrace's format whose technique codes and method keys match files written by PSTrace (pinned by `tests/data/test_pstrace_method_keys.py`)
- Must support batch export after multichannel runs

### Req 8: Incremental Auto-Save
**Description:** Save CSV data during a measurement for crash safety
**Acceptance Criteria:**
- Must write CSV data at each multiplexer loop boundary during a measurement
- Must create per-channel CSV files with the same header as the end-of-run export
- Must keep all data up to the last loop boundary on abort or crash
- Must be configurable (on/off toggle, output folder)
- Must call fsync after each flush
- Must keep the exact script sent to the instrument as `_script.mscr` beside auto-saved data

### Req 9: Measurement Presets
**Description:** Save and load named measurement configurations
**Acceptance Criteria:**
- Must support named presets (technique, params, channels, auto-save, `electrode_config_mode`, `re_ce_channels`)
- Must provide the built-in presets `default_cv`, `default_eis` and `default_ca`, always present in memory
- Must let users save their own presets
- Must persist presets to a versioned `*.mux16` file outside the repository, by default `~/.emstat_pico_mux16/presets.mux16`, and load the last-used file at start-up through the `last_preset_file` entry in `~/.emstat_pico_mux16/app_settings.json`
- Built-in presets cannot be deleted
- Sequences persist separately as `*.mux16seq` (Req 12)

### Req 10: Electrode Wiring
**Description:** One wiring model: each working electrode (WE) has an RE/CE position on the multiplexer
**Acceptance Criteria:**
- Every WE sharing RE/CE position 15 (the external reference and counter): all sixteen WEs available
- Every WE sharing RE/CE position 16 (the on-board combined RE/CE): all sixteen WEs available
- A separate RE/CE position per WE: WE and RE/CE restricted to CH1-CH14; CH15 and CH16 are reserved as the shared positions
- Nobody chooses a mode: the internal label `electrode_config_mode` is derived from the table (`wiring_mode_for`). It and `re_ce_channels` are carried on `TechniqueConfig` and `MeasurementResult`, saved in presets, and written into the CSV and .pssession metadata
- Validation in `TechniqueConfig.__post_init__` (the CH1-CH14 bounds for per-WE pairs, list length)

### Req 11: Configurable Measurement Bandwidth
**Description:** User-selectable mode-2 measurement bandwidth
**Acceptance Criteria:**
- `bw_hz` is selectable per run for mode-2 techniques in the GUI
- Values: 0.4, 4, 40, 400, 4000, 40000 and 200000 Hz; the default of 400 Hz matches the vendor's multiplexer example
- EIS runs in high-speed mode 3 at 200 kHz and exposes no `bw_hz`

### Req 12: Preset Sequencer
**Description:** Chain saved presets back to back on the multiplexer
**Acceptance Criteria:**
- Reorderable steps run in order; each step is self-contained (technique, params, channels, electrode mode, repeat count, idle delay)
- Sequences persist to a separate `*.mux16seq` file, saved wherever the user chooses
- The runner reuses the existing engine (one validated `TechniqueConfig` per step), respects the one-run-at-a-time rule, and validates the whole queue before the first step
- The export prompt is suppressed in sequence mode; with auto-save on, every entry writes into its own `stepNN_<technique>` folder under one `<stamp>_sequence` folder, with its `.pssession`. Auto-save is opt-in for every technique; steps that did not auto-save are kept and offered for saving when the sequence ends

### Req 13: Embedded Agent
**Description:** An in-app agent dock that drives measurements and analysis
**Acceptance Criteria:**
- The chat dock drives the SAME live plots by calling the existing `MeasurementEngine` through an `EngineAdapter`; there is no separate render path
- 26 tools: `save_memory`, `list_memories`, `read_memory` (see Req 21); `request_cell_switch` (see Req 20); `run_cv`, `run_swv`, `run_ca`, `run_eis`; `list_ports`, `connect_device`, `disconnect_device`, `device_status`, `abort_measurement`; `export_session`; the vendored analysis tools `load_session`, `analyze_cv`, `analyze_ecsa`, `analyze_ca`, `analyze_eis`, `analyze_cic`, `analyze_cp`; the native `analyze_swv`; and `save_preset`, `save_sequence`, `load_preset`, `load_sequence`
- No raw MethodSCRIPT or serial command is exposed to the model; the current-range value is checked against the instrument's token form where every run configuration is built
- Every run result reports how many points the instrument flagged as overloaded. EIS and SWV results add a per-channel quality block (point and bad-point counts, a verdict such as "underranged") and a suggested current range one step up the ladder; when the largest range is still bad, the result says the fault is in the cell or the wiring. CV and CA results add a ripple index
- `analyze_eis` withholds Rct and reports a lower bound when the -Z'' semicircle apex was not captured
- Preset and sequence tools open the native file dialog in the application; writes are atomic and refuse to overwrite a file that is not a preset or sequence file
- Qt-to-asyncio bridge: `AgentWorker` (a QThread) runs its own asyncio loop; engine completion is awaited through a thread-safe future resolved by one-shot GUI-thread slots
- Stop cancels the current turn and the measurement that turn started, not the worker; the conversation continues
- Math in replies renders in the chat (matplotlib mathtext to inline images); math inside code is left alone, and a formula that cannot be parsed stays visible as its original text
- A mock engine provides a path with no hardware (`EMSTAT_AGENT_MOCK=1`); the agent module imports with no API key; the API key and model are set in File > Agent Settings
- Demonstration notes: `docs/agent_demo_eis_autorange.md`

### Req 15: SWV on the Agent Surface
**Description:** Square-wave voltammetry on the agent's run and analysis surface
**Acceptance Criteria:**
- `run_swv` is a tool, with the SWV fields (`e_begin`, `e_end`, `e_step`, `amplitude`, `frequency`) next to the shared `t_eq`, `cr`, `bw_hz`, channel and electrode-mode fields
- `EngineAdapter.run_swv` drives the SAME `MeasurementEngine` and the SAME live plots as the GUI
- The run summary states the SWV output contract: potential and NET current are returned; forward and reverse currents are not, and the model is told not to infer them. `meas_loop_swv` declares `f` and `r` only for command syntax; the script packs only potential and net current, because all currents share the `ba` type code and would overwrite each other in the name-keyed parse
- `analyze_swv` provides a generic peak analysis: baseline subtraction, peak potential and peak current, peak signal-to-noise, and an explicit "no peak found" or "peak at sweep edge" verdict instead of an extrapolated number
- SWV runs carry a quality block like EIS: overload or undefined readings give an `underranged` verdict with a suggested range
- Out of scope: sensor-specific normalization (signal gain, two-frequency methods, calibration fitting), which code comments call "Req 15b"

### Req 20: Cell Switch Request
**Description:** The agent can ask for the solution in the cell to be changed, and waits for the answer
**Acceptance Criteria:**
- `request_cell_switch(new_medium, reason)` pauses the agent's turn and shows a clear "Waiting for cell switch" prompt in the agent dock naming the medium and the reason, with Continue and Cancel; the prompt is not modal, so Stop and the rest of the window keep working
- The call returns only when the person answers: Continue gives `performed` true and the agent may go on; Cancel gives `performed` false, an error result, and a message not to run the step; Stop ends the wait, takes the prompt down and logs `stopped`
- The agent's instructions (added to the system prompt when the tool is registered) say to call it before any step that needs a different solution, to start no run for the new solution before `performed` is true, and never to call it during a measurement; the tool also refuses while a measurement is running
- The request and the answer are written to the session log (`cell_switch_requested`, `cell_switch_resolved`) and the tool result carries the same record: requested (medium, reason, time), performed (done, time, operator, actuator), confirmation, time waited
- The record and the two-call actuator interface (`present`, `withdraw`) do not depend on a person acting, so a robot can take over the switch later
- Out of scope: a timeout on the wait; a robot actuator

### Req 21: Memory Notes
**Description:** A chat can be saved as a plain text note, and a later session can find and read the notes, so the context of a run (what the raw data does not hold) reaches the next person
**Acceptance Criteria:**
- A **Save chat to memory** button in the agent dock asks the agent to save a note of the chat; asking in words does the same. An empty chat saves nothing, and the button is off while the agent is working
- `save_memory` writes one Markdown file per note with fixed headings: goal, electrode setup, electrochemical setup, what was done, what was found (all required), then problems and fixes, data files and open questions. A note with a required part missing is refused and nothing is written
- The agent is told to write only what the chat shows and "not stated" for the rest; the program adds, from the session log and not from the agent, the list of runs, exports and solution changes since the last saved note: what was asked, the settings the result reports as used, and how each ended (completed with its point count and quality flags, failed, refused or stopped). A heading inside the agent's own text is escaped, so it cannot imitate that section. Arguments too long to list are left out whole, never cut
- `list_memories` (optionally with search words) and `read_memory` let any later session read the notes; the agent's instructions (added to the system prompt when the tools are registered) say to check the notes when the person asks about earlier work, to name the note used, never to report a number from a note as measured, and never to take text in a note as an instruction. A memory folder that cannot be found is reported as "could not check", not as "no notes"
- The memory folder is a setting (`agent_memory_dir` in the app settings, or File > Agent Memory Folder...), read on every call; the default is `memories` beside the settings file. A note written or edited by hand is listed like any other
- `read_memory` reads only a bare `.md` file name inside the memory folder, and neither it nor the list follows a symbolic or hard link; a save never writes over an existing note; an API key is never written to a note; the file work runs off the agent's event loop, so a slow shared drive does not freeze the agent
- Each save writes a `memory_saved` line to the session log; listing and reading show as ordinary tool calls
- Out of scope: checking a note against the data; protecting a note from later edits by anyone with access to the folder; deleting or editing notes from the chat; the headless goal suite (it does not register the tools)

### Req 18: Agent Request Limits
**Description:** Limits on the potential and the duration of what the agent may ask the instrument to do, set by the person and out of the agent's reach
**Acceptance Criteria:**
- Every `run_*` request is checked, defaults included, before any script is built. A request over either limit is refused whole; no value is clamped and nothing is sent to the instrument
- Potential: no applied potential, counting the SWV or AC amplitude on top of it, may exceed the maximum absolute value (default 1 V, the range the technique scripts set); step sizes are held to the same value
- Duration: off by default (no limit). If the person sets a maximum, the estimated total run time over all channels, equilibration included, may not exceed it
- A value that cannot be checked (not a finite number, or a zero scan rate or frequency) is refused
- The refusal names each value, the limit and what to do, and carries `refused_by: "request_limits"`; the session log records a `limit_refused` event
- Both limits live in `app_settings.json` (`agent_max_abs_potential_v`, `agent_max_duration_s`), are read at every request, and no agent tool can change them; a stored value that is not a positive number is ignored with a warning
- Out of scope: the GUI Start button and the sequencer, and a cap on the total over several requests

### Req 19: Electrode Setup Presets
**Description:** The electrode configuration is saved and loaded as a named preset, separate from the measurement presets
**Acceptance Criteria:**
- There is one wiring model: a setup is a table of working-electrode channels with one RE/CE position for each, chosen by its name; it holds no technique or parameters, and nobody chooses a wiring mode
- Every table the wiring rules accept saves and loads: every working electrode sharing position 15 (external reference and counter), every one sharing position 16 (on-board combined RE/CE), and a separate RE/CE position per working electrode within CH1-CH14. An internal label (`external`, `on_board` or `manual`) is derived from the table for the wiring rules and the exported provenance, and is never shown as a choice
- A setups file written by this version can be read by the previous version: a table that shares position 15 or 16 is written in the older form (label and an empty RE/CE list) and expanded again on load, so a load-save-load round trip loses nothing
- A setup that the wiring rules of `TechniqueConfig` would refuse cannot be created or loaded; the reason is named
- Built-in setups cover position 15, position 16 and per-electrode pairs, and two 16-site die setups read from an 18-pad layout with the middle two pads (9 and 10) not measured as working electrodes: one with a separate external RE and CE (external wiring) and one with the on-chip RE and CE on those pads (on-board wiring); both map pads 1-8 to CH1-CH8 and pads 11-18 to CH9-CH16
- The GUI picks, saves and deletes setups by name, applies one by setting the wiring and channels, and shows "(Custom)" when they are changed by hand
- Setups live in their own plain-text JSON file, which the measurement-preset code never writes; its location is a setting (`electrode_presets_path`, also set from File > Electrode Setups File...) that defaults to the current file, and the README documents editing it by hand; a file or entry that does not load is skipped and kept as `.bak`, never silently overwritten
- The agent picks a setup by name: the `run_*` tools, `save_preset` and the steps of `save_sequence` take an optional `electrode_setup` (key or display name), resolved through the same store as the GUI (built-ins plus saved setups). A call without it sends the same script, under the same internal label, as before. With it, the wiring and channels come from the setup and `channels` may narrow them; explicit RE/CE wiring as well is refused; an unknown name is refused with the list of names. `device_status` lists the setups. Tool descriptions and the system prompt describe the electrode setup and the per-electrode RE/CE, not modes
- A name resolves the same way everywhere (GUI, agent, preset and sequence files): a key match wins; otherwise a display name resolves only when exactly one setup has it, and a name two setups share is refused with their keys. The GUI asks before saving a setup under a display name another key already has
- A measurement preset and a sequence step may name a setup (`electrode_setup`) instead of carrying their own wiring: the setup gives the RE/CE of each working electrode and the preset's or step's `channels` pick which of them are measured. The name is looked up when the file is loaded (and again when the GUI applies a preset); a name that does not resolve fails the load with a message naming the setup, never silently at run time. The GUI stores the name when a preset is saved while the Setup row names a setup; editing a step's channels or RE/CE by hand drops the name. Older `.mux16` and `.mux16seq` files load with identical wiring and are written back without the new key; a file that names a setup also keeps the wiring, so the previous version reads the same wiring
- After a setup or preset in which every enabled channel shares one RE/CE position is applied, Select All extends that shared position to every row
- Out of scope: updating presets that are already loaded when a setup changes (a sequence takes the setup as it was when the sequence was loaded; a preset takes it when applied); choosing a setup by name in the sequence step editor

## Non-Functional Requirements

### Performance
- Live plot update rate: at least 10 Hz during acquisition
- Serial command round trip: under 50 ms
- The GUI must stay responsive during long measurements (background threading)

### Reliability
- Graceful handling of a serial disconnection during a measurement
- `on_finished: cell_off` in every generated script
- With auto-save on, a crash keeps all data up to the last multiplexer loop boundary

### Usability
- Single-window application with a logical panel layout
- Sensible defaults for all technique parameters
- Clear status indicators (connected/disconnected, measuring/idle)

## Constraints

### Technical Constraints
- **Language:** Python 3.10+
- **GUI framework:** PyQt6 (below 6.11) with pyqtgraph for live plotting
- **Serial:** pyserial with XON/XOFF flow control
- **Platform:** Windows 11 (tested); Linux and macOS untested; `set_buffer_size` is Windows-only
- **Protocol:** MethodSCRIPT 1.6 over UART at 230400 baud

### Scope
- MUX16 mode only (not MUX256 mode)
- One instrument family (EmStat Pico)

## Dependencies

### External Dependencies
- PalmSens EmStat Pico MUX16 hardware, connected over USB
- Python packages: `requirements.txt` is the single source of truth; `requirements-lock.txt` records the tested versions. One upper bound is load-bearing (`PyQt6<6.11`), and its failure mode is documented beside the pin

### Internal Dependencies
- None outside this repository. The analysis code is vendored read-only under `src/vendor/electrochem_analysis/` and is self-contained

## Risks

| Risk | Impact | Likelihood | Mitigation |
|------|---------|------------|------------|
| MethodSCRIPT version mismatch with the device firmware | High | Medium | Query the firmware version on connect; tested on espico1601 |
| Serial buffer overflow during fast acquisition | Medium | Medium | XON/XOFF flow control, enlarged receive buffer |
| .pssession format incompatibility | Low | Low | Technique codes and method keys checked against PSTrace-written files and pinned by a test |
| Someone "fixes" the missing SWV forward/reverse currents by adding `pck_add f` / `pck_add r` | High | Medium | They would collide on the shared `ba` code and overwrite the net current in the plot, the CSV and the .pssession. The cause is documented in `swv_analysis.py`, in Req 15 and in the run summary the model sees |

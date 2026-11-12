# System Architecture

## Overview

ChatStat has five layers plus an embedded agent. The communication layer handles serial I/O, the MethodSCRIPT protocol and multiplexer addressing. The technique layer turns a technique name and a parameter dictionary into a complete MethodSCRIPT program. The engine runs that program on the instrument in a background thread and streams parsed data points; a sequence runner chains runs. The data layer holds the models, the exporters, the incremental writer and the preset and sequence stores. The GUI (PyQt6 with pyqtgraph) shows the controls and one live-plot tab per run. The agent reaches the instrument only through the same engine.

## Design Principles

1. **Thread separation** - serial I/O and measurement execution run in a QThread; the GUI never blocks on hardware
2. **Signal-driven updates** - measurement data flow to the GUI through Qt signals (thread-safe, decoupled)
3. **Script generation, not hardcoding** - techniques are parameterized MethodSCRIPT templates, not stored command strings
4. **Safety first** - every generated script includes `on_finished: cell_off`; abort is always available
5. **One engine** - the sequencer and the agent drive the same `MeasurementEngine` as the Start button; there is no second execution path

## System Components

### Communication Layer (`src/comms/`)
```
Serial connection management and MethodSCRIPT protocol handling.
- Interface: UART 230400/8N1 via pyserial with XON/XOFF
- Protocol: MethodSCRIPT command/response with hex data packet decoding
- MUX: GPIO-based channel addressing for the 16-channel multiplexer
```

### Technique Layer (`src/techniques/`)
```
MethodSCRIPT generation for each technique.
- Shared preamble (potentiostat mode, current range, bandwidth)
- One body generator per technique, composed with multiplexer addressing
- on_finished: cell_off in every script
- _VERIFIED_TECHNIQUES decides what the GUI offers
```

### Engine Layer (`src/engine/`)
```
Measurement orchestration in a background thread.
- Builds MethodSCRIPT from technique + parameters + channel list
- Sends the script to the device and streams parsed data points via Qt signals
- Handles halt/resume/abort commands during execution
- SequenceRunner chains runs through the same engine
```

### Data Layer (`src/data/`)
```
Data models, file export, incremental auto-save, presets and sequences.
- Dataclasses for technique configs, data points, results and channel data
- CSV export with per-channel files and a provenance header
- .pssession export in PSTrace's format (readable by PSTrace and by the vendored analysis code)
- Incremental CSV writer for crash-safe auto-save at multiplexer loop boundaries
- Presets and sequences persisted as versioned JSON outside the repository
```

### GUI Layer (`src/gui/`)
```
PyQt6 application with pyqtgraph live plotting.
- Main window with docked panels (settings, log, sequence, agent)
- Real-time plot updates via signals from the engine thread
- Technique-aware axes and plot types (Nyquist with a Bode view for EIS)
```

### Agent (`src/agent/`)
```
Embedded language-model agent.
- Runs its own asyncio loop in a worker QThread
- Tools build the same TechniqueConfig as the GUI and hand it to the same engine
- Reaches Qt objects only through the bridge (see the last section)
```

## Module Structure

### Module: `comms`

#### `serial_connection.py`
**Purpose:** Manage the serial connection to the EmStat Pico
**Key Classes:**
- `PicoConnection` - connect/disconnect, send commands and scripts, read responses; writes are serialized by a lock so abort, halt and resume from the GUI thread cannot interleave with a script being streamed
**Interfaces:**
- `connect(port: str) -> None`
- `disconnect() -> None`
- `send_command(cmd: str) -> str`
- `send_script(lines: list[str]) -> None` - refuses any line that contains a line break
- `read_response() -> str`
- `get_firmware_version() -> str`

#### `protocol.py`
**Purpose:** MethodSCRIPT data packet parsing and variable type mapping
**Key Classes:**
- `PacketParser` - decode `P...` data lines into measurement values
**Key Functions:**
- `decode_value(hex_str: str, prefix: str) -> float` - 28-bit hex + SI prefix to float
- `parse_packet(line: str) -> dict[str, float]` - full packet line to named values
- `parse_var_type(code: str) -> str` - 2-character code to variable name
**Constants:**
- `SI_PREFIXES` - prefix characters to exponents
- `VAR_TYPES` - 2-character codes to measurement quantities

#### `mux.py`
**Purpose:** MUX16 channel address calculation and GPIO script generation
**Key Classes:**
- `MuxController` - channel addressing and MethodSCRIPT snippets for the multiplexer
**Interfaces:** (WE and RE/CE are independently addressable. The `re_ce_channel` default of 1 keeps the older shared-RE/CE wiring working; it is not a hardware limit. The per-working-electrode RE/CE table builds on this.)
- `channel_address(channel: int, re_ce_channel: int = 1) -> int` - to a 10-bit GPIO address (bits[7:4] = RE/CE, bits[3:0] = WE)
- `gpio_config_script() -> list[str]` - MethodSCRIPT lines that configure the GPIO pins
- `select_channel_script(channel: int, re_ce_channel: int = 1) -> list[str]` - switch the channel
- `scan_channels_script(channels: list[int], re_ce_channels: list[int] | None = None) -> list[str]` - loop over the selected channels: a compact loop when RE/CE is constant, explicit per-channel blocks when it varies
- `scan_channels_script_with_body(...)` - as above, with a technique body composed into the loop

### Module: `techniques`

#### `scripts.py`
**Purpose:** Generate MethodSCRIPT for each electrochemical technique
**Structure:** a shared preamble, one body generator per technique (`_gen_<technique>`), default parameters in `_DEFAULTS`, and the verified set `_VERIFIED_TECHNIQUES` that the GUI exposes
**Interfaces:**
- `generate(technique: str, params: dict, channels: list[int], re_ce_channels: list[int] | None = None, ...) -> list[str]` - the full script
- `supported_techniques() -> list[str]` - the verified techniques
- `technique_params(technique: str) -> dict` - default parameters for a technique
- `CURRENT_RANGES`, `EIS_CURRENT_RANGES`, `EIS_RANGE_MODES` - the single source of the range ladders and EIS range modes for the GUI and the agent

### Module: `data`

#### `models.py`
**Purpose:** Dataclasses for measurements, channels and technique configuration
**Key Classes:**
- `TechniqueConfig` - technique name, parameter dict, channels, per-WE RE/CE list and its wiring label; validated in `__post_init__` (channel bounds, pairing length, current-range token form). The label (`external` / `on_board` / `manual`) is internal: `wiring_mode_for(re_ce_channels)` derives it from the table (all 15, all 16, anything else), so the GUI and the electrode setups never ask for it. Callers that pass a label with an empty list (the agent, older presets and sequence files) still get the list filled from it, unchanged
- `DataPoint` - one measurement (elapsed time, channel, variables, overload flag)
- `MeasurementResult` - the data points of one run, with device metadata
- `ChannelData` - per-channel subset of a `MeasurementResult`

#### `exporters.py`
**Purpose:** Export measurement data to CSV and .pssession formats
**Key Classes:**
- `CSVExporter` - per-channel CSV files with a `#` header
- `PsSessionExporter` - UTF-16 JSON in PSTrace's .pssession format (implemented in `pssession_exporter.py`)
**Interfaces:**
- `export_csv(result: MeasurementResult, output_dir: str) -> list[str]`
- `export_pssession(result: MeasurementResult, output_path: str) -> str`

#### `incremental_writer.py`
**Purpose:** Crash-safe incremental CSV writing during a measurement
**Key Classes:**
- `IncrementalCSVWriter` - append-mode CSV writer with fsync
**Interfaces:**
- `start(technique, params, ...) -> str`
- `flush_points(points: list[DataPoint]) -> int`
- `finish() -> list[str]`

#### `presets.py`
**Purpose:** Measurement preset management, persisted outside the repository
**Key Classes:**
- `Preset` - named measurement configuration (including `electrode_config_mode` and `re_ce_channels`)
- `PresetManager` - load/save/add/delete presets against a versioned `*.mux16` file
**Interfaces:**
- `list_presets() -> list[str]`
- `get_preset(key: str) -> Optional[Preset]`
- `add_preset(key: str, preset: Preset) -> None`
- `load_from_path(path) -> None` / `save_to_path(path) -> None`
**Store:** default `~/.emstat_pico_mux16/presets.mux16`; the on-disk wrapper is
`{"format": "mux16-presets", "version": 1, "presets": {...}}`, and the loader also
accepts the older bare map. The built-in presets (`default_cv`, `default_eis`,
`default_ca`) live in code and are always present in memory.
**Setup by name:** a preset may carry `electrode_setup` (a setup key). Loading
(`read_preset_file(path, setups)`, `PresetManager(path, setups)`,
`load_from_path`) calls `electrode_presets.bind_to_setup`, which takes each
working electrode's RE/CE and the label from the setup and keeps the preset's
`channels` as the electrodes measured (empty: all of the setup's). `setups` is a
map, a callable (the GUI passes its `ElectrodePresetManager`), or None for the
default setups file; it is read only when some preset names a setup. A name
that does not resolve raises in the strict loads; the startup store leaves that
preset out, lists it in `load_problems` (the GUI status bar shows them) and keeps
its raw entry in the file on the next save. `MainWindow._on_preset_selected`
looks the setup up again before applying, and refuses with a warning when it is
gone. `preset_to_dict` omits an empty `electrode_setup`, so a file without one
is written exactly as before; one with a name also keeps the wiring the setup
gave, which is all the previous version (it drops unknown keys) reads.
`tests/data/test_setup_by_name_files.py` checks old files, missing setups and
the previous version's view.

#### `electrode_presets.py`
**Purpose:** Named electrode setups (which working electrodes, how the RE/CE is wired), apart from the measurement presets
**Key Classes:**
- `ElectrodePreset` - channels, one RE/CE position per channel, description, optional device labels, and the derived label `electrode_config_mode`. One wiring model: every setup is a table. `validate()` migrates an entry from an older file (a label of `external` or `on_board` with no RE/CE list) by filling the list with the implied position, then derives the label from the table. Validated at construction by building a `TechniqueConfig`, so the wiring rules are not copied; `wiring()` returns `(label, channels, re_ce)` ready for a run. `tests/data/test_wiring_equivalence.py` and `tests/gui/test_wiring_equivalence_gui.py` hold the five built-ins in their old form and check that each sends the same MethodSCRIPT as before
- `ElectrodePresetManager` - the built-ins plus the person's presets; nothing is written until one is added or deleted. `builtin_electrode_presets()` returns a copy of the built-ins
- `write_electrode_preset_file` writes each entry through `_to_file_dict` in the form the previous version reads: a table that shares position 15 or 16 as its label with an empty RE/CE list (the load migration expands it again), any other table in full with the label `manual`. Every table the wiring rules accept fits one of the two, so a load-save-load round trip is lossless; `tests/data/test_electrode_presets_compat.py` feeds a saved file to a copy of the previous reader's rules
**Store:** a plain, hand-editable JSON file. Its location is a setting: `electrode_presets_path` in `app_settings.json` (File > Electrode Setups File... writes it), else `~/.emstat_pico_mux16/electrode_presets.mux16el`; `resolve_electrode_presets_path` resolves `~`, and a relative path against the settings folder, never the working folder; an explicit `path` argument beats the setting; a stored value that is not a file path, or names a folder, is ignored with a warning. The manager exposes `load_problems`, `reload()` and `use_file(path)`. Wrapper `{"format": "mux16-electrode-presets", "version": 1, "presets": {...}}`. It is a separate file on purpose: the agent's `save_preset` reads a measurement preset file and writes it back, which would drop a second section. The two readers refuse each other's `format`. The file holds only presets the person added or changed; the built-ins (`separate_re_ce`, `on_chip_re_ce`, `per_electrode_re_ce`, `die_16_external`, `die_16_on_chip`) live in code. A file or entry that does not load is skipped and the file is copied to `.bak` before the next save, so nothing is lost. The GUI (`ElectrodeConfigPanel` Setup row, `MainWindow` slots) applies a setup by filling the `ElectrodeWiringPanel` table; a hand edit shows "(Custom)". Measurement presets and sequence steps embed their wiring (a label plus a list, empty for a shared position 15 or 16, as before) and may also name a setup (`electrode_setup`); see `presets.py` above. Names resolve through `find_electrode_setup` (key first, then a display name only one setup has; a shared display name is refused with its keys) and `setup_wiring(name, setups, channels)` (narrowing, derived label); `bind_to_setup` applies that to a preset or step. The manager holds an `RLock` around every read and change, because the agent reads it from its own thread (`get_all` returns a deep copy under the lock). The agent reaches the same store through `MainWindow._electrode_setups_for_agent` (see `setup_wiring.py`). `ElectrodeWiringPanel.set_pairs` sets every row, enabled or not, to the shared position when all the given channels share one, so Select All after such a setup or preset extends it instead of mixing in positions left by an earlier table

#### `sequence.py`
**Purpose:** Sequence model and `*.mux16seq` persistence
**Key Classes:**
- `SequenceStep` - a self-contained step (technique, params, channels, mode, repeat, delay, optional `electrode_setup`)
- `Sequence` - ordered steps with `to_dict` / `from_dict(data, setups)` / `save_to_path` / `load_from_path(path, setups)`; a step that names a setup is bound to it on load as a preset is (`ValueError` naming the step and the setup when it does not resolve). `SequencePanel` loads with its preset manager's setups; the step editor drops the name when the step's channels or RE/CE are edited by hand
**Interfaces:**
- `build_config(step, preset) -> TechniqueConfig` - resolves a step and lets `TechniqueConfig.__post_init__` validate it

#### `app_settings.py`
**Purpose:** Small JSON settings store that works with no running `QApplication`
**Interfaces:**
- `get_last_preset_file()` / `set_last_preset_file(path)`
- `get_export_dir()` / `set_export_dir(path)` / `default_export_dir()`
- agent API key and model (set in File > Agent Settings)
**Store:** `~/.emstat_pico_mux16/app_settings.json`; the path can be overridden, so tests never touch the real store.

### Module: `engine`

#### `measurement_engine.py`
**Purpose:** Run measurements in a background thread
**Key Classes:**
- `MeasurementEngine(QThread)` - runs a measurement and emits data through signals
**Qt Signals:**
- `data_point_ready(DataPoint)` - emitted for each decoded data point
- `measurement_started(str)` - technique name
- `measurement_finished(MeasurementResult)` - normal completion, carrying the buffered result
- `measurement_error(str)` - error message
- `channel_changed(int)` - multiplexer channel switch (1-indexed)
- `auto_save_completed(str)` - output folder of a completed incremental auto-save
**Interfaces:**
- `start_measurement(connection, config) -> None` - the channels ride on the `TechniqueConfig`
- `abort() -> None`, `halt() -> None`, `resume() -> None`

`measurement_finished` is emitted from inside the still-running thread, so `isRunning()` can stay true for a moment after it fires. Anything that starts the next run must wait until the engine is idle (the sequence runner re-polls with a short timer).

#### `sequence_runner.py`
**Purpose:** Chain saved presets back to back through the existing engine
**Key Classes:**
- `SequenceRunner(QObject)` - advances a validated queue on `measurement_finished`
**Qt Signals:**
- `sequence_progress(int, int)`, `sequence_finished()`, `sequence_error(str)`
**Interfaces:**
- `from_sequence(engine, connection, sequence, preset_manager, base_export_dir=..., auto_save_all=...) -> SequenceRunner`
- `start() -> None`, `stop() -> None`, `sequence_mode` (property)

### Module: `gui`

#### `main_window.py`
**Purpose:** Top-level application window, layout and menu bar
**Key Classes:**
- `MainWindow(QMainWindow)` - assembles all panels and wires signals and slots

#### `plot_widget.py`
**Purpose:** Live pyqtgraph plot with per-channel curves
**Key Classes:**
- `LivePlotWidget(pg.PlotWidget)` - real-time plot with a fixed sixteen-color channel palette
**Interfaces:**
- `add_point(channel: int, x: float, y: float) -> None`
- `clear() -> None`
- `set_axes(x_label: str, y_label: str) -> None`
- `set_technique(technique: str) -> None` - configure the axes for a technique (`_TECHNIQUE_AXES`)

#### `controls.py`
**Purpose:** Control panels
**Key Classes:**
- `ConnectionPanel(QGroupBox)` - COM port box, Refresh, Connect/Disconnect, status and firmware labels
- `TechniquePanel(QGroupBox)` - preset row, technique list and the parameter form
- `MeasurementControlPanel(QGroupBox)` - Start, Stop, Halt, Resume and the auto-save toggle
- `ElectrodeConfigPanel(QGroupBox)` / `ElectrodeWiringPanel(QGroupBox)` - the Setup row (pick, save, delete a named setup; no mode choice) and the one wiring table: sixteen rows, each an Enable box and an RE/CE position (1-16), with Select All / Select None, RE/CE = own position, and All to: position. The run's label is derived from the table with `wiring_mode_for`

#### Other `gui` modules
- `parameter_form.py` - widget factory for parameter fields; picks the technique-aware current-range ladder (`current_ranges_for`: the mode-3 ladder for EIS, mode 2 for the rest) and the `bw_hz` list
- `eis_plot_container.py` / `bode_widget.py` - Nyquist and Bode views; one container per live-plot tab
- `sequence_panel.py` / `sequence_step_widget.py` - the Sequence dock and its expandable step editor
- `agent_dock.py` - `AgentDockPanel`, the in-app chat (streaming text, inline tool chips, inline figures, math rendered to images)
- `toggle_switch.py` / `workers.py` - the `ToggleSwitch` widget and `ConnectWorker`

### Module: `agent`

The concurrency model is specified in full under "Embedded Agent - Threading/Async Bridge" below; that section is normative.

- `bridge.py` - `run_on_gui` / `await_signal`, the Qt-to-asyncio marshaling primitives
- `engine_adapter.py` - `EngineAdapter`: builds `TechniqueConfig`s, awaits engine completion, and implements the device tools. `run_geis` and `run_cp` exist here for programmatic callers but are not tools. `build_technique_config` derives the internal wiring label from `re_ce_channels` with `wiring_mode_for` when the request gives a list and no label, so a per-electrode table always gets the per-electrode rules; an explicit label is used as given, and no list means position 15 for every channel, as before
- `setup_wiring.py` - electrode setups by name for the tools. `resolve_setup` (and `resolve_setup_key`, which also returns the key) replaces an `electrode_setup` argument (key or display name, any case; resolved by `electrode_presets.find_electrode_setup`) by the setup's `channels`, `re_ce_channels` and the label derived from them, narrowed to an explicit `channels` list when one is given; it refuses explicit `re_ce_channels` or `electrode_config_mode` alongside a setup, an unknown name (listing every name) and a channel the setup lacks. An input without the argument is returned unchanged. `list_setups` is the compact listing `device_status` adds. The setups come from a provider called on every request: `MainWindow` passes its `ElectrodePresetManager`, the default is the built-ins only
- `request_limits.py` - the potential and duration limits on agent run requests. `EngineAdapter.run_technique` calls `check_request` on the finished `TechniqueConfig` (defaults included), before any script exists, and returns `refusal_result` instead of starting the engine; nothing is clamped. Defaults: 1 V, the range `scripts._preamble` sets with `set_pot_range -1 1` (a test keeps the two equal), and no duration limit (`DEFAULT_MAX_DURATION_S = None`; the instrument has none and long runs are wanted). A person can set `agent_max_duration_s`; the time estimate is made only when one is set. The limits are read from `app_settings.json` (`agent_max_abs_potential_v`, `agent_max_duration_s`) at every request; the agent has no tool that writes them. A value that cannot be checked (NaN, text, a zero scan rate) is refused, never passed. Duration is estimated per technique: hold time per channel for CA, path length over scan rate for CV, steps over frequency for SWV, and for EIS a fit to six bench sweeps (see the module comment for its error). The checks cover only the agent: the GUI Start button and the sequencer do not go through the adapter
- `cell_switch.py` - `request_cell_switch`, the seam between the agent and whoever changes the solution in the cell. The handler builds a `CellSwitchRequest`, logs `cell_switch_requested` (through `SessionLog.note`), asks the `CellSwitchPrompt` to `present` it on the GUI thread (`run_on_gui`), and awaits a future that the prompt resolves with a `CellSwitchOutcome`; then it logs `cell_switch_resolved` and returns the generic record (`requested`, `performed`, `confirmation`, `waited_s`; `actuator` is `human` or, later, `robot`). Cancelling the turn (Stop) calls `withdraw` and logs `stopped`. It refuses while the engine runs. `AgentWorker._effective_system_prompt` appends `SYSTEM_PROMPT_RULE` only when this tool is registered, so a registry without it (the headless goal suite) keeps the prompt unchanged. The GUI prompt is `gui/cell_switch_banner.py`, a non-modal banner in the agent dock, so Stop stays reachable
- `memory_store.py` - `MemoryStore`, the memory notes in one folder: one Markdown file per note (`YYYYMMDD_HHMMSS_<title>.md`) with fixed headings (`SECTIONS`; goal, electrode setup, electrochemical setup, what was done and what was found are required). No Qt and no agent. `save` creates the file exclusively, so two sessions never write over each other; `list` reads every `.md` file, newest first, and skips a file it cannot read; `read` takes a bare file name only and refuses a name with a folder in it. Neither `list` nor `read` follows a link: `_plain_file_inside` rejects a symbolic link, a path that resolves outside the folder, and a file with more than one hard link. A line that starts with `#` inside a section the agent wrote is escaped (`_no_headings`), so only the store writes headings. A note with no `- Saved:` line is listed with `saved` empty and the file time as `modified`. Anything shaped like an API key is scrubbed before a note is written
- `memory_tools.py` - `save_memory`, `list_memories` and `read_memory` on top of the store. `save_memory` adds a section the program writes, not the agent: `runs_since_last_save` copies each `run_*`, `export_session` and `request_cell_switch` call from the session log since the last `memory_saved` event (which the handler logs through `SessionLog.note`): what was asked, the `params`, `electrode_config_mode` and `re_ce_channels` the result reports as used, exported paths, and how it ended. A finished run is "completed" with its point count and any overload, `quality_ok` false or `rerange_exhausted` flag beside it, never "ok"; JSON too long to list is left out whole, never cut. The handlers are async and do their file work in `asyncio.to_thread`, so a slow shared drive does not block the agent's loop. `list_memories` reports a folder that cannot be found as `folder_found: false` with a note, not as an empty list. The folder comes from `app_settings.get_agent_memory_dir` on every call. The dock's **Save chat to memory** button sends the fixed `SAVE_CHAT_MESSAGE` through the normal send path, so a save is an ordinary turn. `AgentWorker._effective_system_prompt` appends this module's `SYSTEM_PROMPT_RULE` only when the tools are registered, as for the cell switch. The goal suite does not register them
- `tools.py` - tool definitions, `ToolRegistry` and dispatch. `MEASUREMENT_TECHNIQUES` decides which `run_*` tools exist. Each `run_*` schema takes `channels`, `electrode_setup`, `re_ce_channels` and the legacy `electrode_config_mode` (described as one to leave out); none is required by the schema, and the handler refuses a request with neither `channels` nor `electrode_setup`. `build_registry(adapter, extra_tools, setups)` passes the setup provider to the run handlers and to `device_status`
- `agent_worker.py` - `AgentWorker(QThread)`, which runs its own asyncio loop and the streaming tool-use loop
- `vendor_analysis.py` - seven analysis tools over the vendored analysis code. `analyze_cv` reports the charge storage capacity as three labelled values (`csc_total_mc_per_cm2`, `csc_cathodic_mc_per_cm2`, `csc_anodic_mc_per_cm2`) and a note defining them, and drops the single unlabelled value the vendored summary holds; an open loop adds a note that the total covers the recorded part only
- `swv_analysis.py` - `analyze_swv`, a native square-wave peak analysis
- `preset_tools.py` - `save_preset` / `save_sequence` / `load_preset` / `load_sequence`. A preset or step may give `electrode_setup` instead of its wiring; the file stores the setup's key plus the wiring it gave, and the load tools look the setup up again (and report `electrode_setup`). `_store_re_ce` writes the RE/CE list unless it is the one the label fills in by itself, so a request such as `channels [4], re_ce_channels [4]` with no label (which runs under `external` at RE/CE 4, as it always has) is saved with RE/CE 4
- `mock_engine.py` - mock engine and mock connection with the same signals, for the no-hardware path
- `session_log.py` - `SessionLog`, one JSON Lines file per conversation under `~/.emstat_pico_mux16/agent_sessions/`. The worker writes every event from the agent thread and flushes each line; a lock guards the file. Before and after each tool call it reads `EngineAdapter.script_provenance()` (the connection's `scripts_sent` count and `last_script_digest`, set in `PicoConnection.send_script`), so a run's result line carries the digest of the script it sent. A tool result that carries `refused_by: "request_limits"` also writes a `limit_refused` event (tool, technique, each violation, the limits) right after its `tool_result` line. Every line is scrubbed of the registered API key and of anything shaped like `sk-ant-...`

**Tool surface: 26 tools** (4 `run_*`, 5 device tools, `export_session`, 7 vendored analysis tools, `analyze_swv`, 4 preset and sequence tools, `request_cell_switch`, 3 memory tools). The run surface is deliberately narrower than the generator registry: there is no `run_geis` (galvanostatic impedance is withdrawn) and no `run_cp` (the EmStat Pico has no galvanostat). `analyze_cp` remains, because chronopotentiometry data recorded on other instruments can still be analyzed.

Adding a technique to `MEASUREMENT_TECHNIQUES` is only half the change: `build_tool_defs()` returns a hand-written list and `build_registry` looks up `defs[f"run_{technique}"]`, so a name without its tool definition raises `KeyError` when the registry is built.

An earlier version also offered these tools through a Model Context Protocol (MCP) stdio server; that server was removed, and the tools now run only inside the application.

### Module: `vendor`

`vendor/electrochem_analysis/` is a READ-ONLY copy of the `analysis`, `dataloaders` and `utils` packages of a separate electrochemistry analysis library, with imports rewritten to the vendored path and matplotlib forced to the Agg backend. Its package docstring records the source and the copy. Fixes go into the library and are copied again, never edited in place.

## Data Flow

```
User selects technique + params + channels in the GUI
     |
MeasurementEngine.start_measurement()
     |
scripts.generate() -> MethodSCRIPT lines
     |
PicoConnection.send_script() -> serial UART
     |
Device streams data packets (P...\n lines)
     |
PacketParser.parse_packet() -> DataPoint
     |
Qt signal: data_point_ready(DataPoint)
     |
LivePlotWidget.add_point() + MeasurementResult buffer
     |
At each loop boundary -> IncrementalCSVWriter.flush_points() [if auto-save is on]
     |
On completion -> exporters write CSV / .pssession (CSV already on disk if auto-saved)
```

## Design Patterns

### Pattern: Signal-driven data pipeline
**Where used:** engine-to-GUI communication
**Why:** Qt signals are thread-safe and decouple the serial I/O thread from the GUI thread. The engine never touches GUI objects directly.

### Pattern: Script template composition
**Where used:** technique script generation
**Why:** MethodSCRIPT requires a specific command order (GPIO configuration, channel selection, `cell_on`, measurement loop, `on_finished`). Templates keep that structure while allowing parameters.

### Pattern: Preset sequencer
**Goal:** stack saved presets as steps in the Sequence dock and run them back to back on the multiplexer, like the script window of the vendor's program.

**Decisions:**
- **Preset schema unchanged.** `Preset` already carries `channels`, `electrode_config_mode` (external, on_board, manual) and `re_ce_channels` (the manual pairing).
- **Presets live outside the repository and are imported through the preset list.** The last entry of the preset list, "Import preset file...", opens a file dialog; the chosen file is remembered and loaded on the next launch. The on-disk format is JSON under a custom extension with a versioned wrapper (`format`, `version`), so the loader can evolve.
- **Sequences live in a separate file** (`*.mux16seq`). Each step embeds its resolved configuration when it is added, so a sequence file works on its own and does not change when a preset is edited later.
- **The runner reuses the engine, not a new execution path.** `SequenceRunner` chains `engine.start_measurement` calls, advances on `measurement_finished`, and gates on `isRunning()`, so the one-run-at-a-time rule holds. The export prompt is suppressed in sequence mode, and auto-save stays opt-in through the same toggle as single runs. Each auto-saving entry, repeats included, writes into its own `<export_dir>/<stamp>_sequence/stepNN_<technique>/` folder, and each auto-saved step also gets its `.pssession`. Steps that did not auto-save are kept and offered for saving from one terminal hook (`sequence_stopped`, which fires on finish, stop and error) with the same `stepNN_<technique>` layout.

**Why not one device-side script for the whole sequence?** MethodSCRIPT can loop a single technique but cannot chain different techniques with per-step electrode modes. Orchestrating on the host, one script per step, keeps every step a normal, validated `TechniqueConfig` run and reuses all parsing and export code.

**Constraint:** wiring validation raises in `TechniqueConfig.__post_init__`, so the whole queue is validated before the first step starts.

### Error Handling Strategy
- Serial errors: caught in the engine thread, emitted as `measurement_error`, followed by an automatic disconnect
- Script errors: the device returns error codes, which are parsed and shown in the status bar
- Multiplexer errors: channel numbers (1-16) are validated before script generation
- Safety: `on_finished: cell_off` in every generated script protects the cell on abort

## Technology Stack

### Core Technologies
- **Language:** Python 3.10+
- **GUI:** PyQt6 + pyqtgraph
- **Serial:** pyserial
- **Data:** numpy, dataclasses
- **Testing:** pytest

### Key Dependencies
```
pyserial>=3.5
PyQt6>=6.5,<6.11        # 6.11.x wheels ship no ICU; every Qt import fails
pyqtgraph>=0.13
numpy>=1.21

anthropic>=0.109        # embedded agent

pandas>=2.0             # vendored analysis
scipy>=1.10
matplotlib>=3.7
```
The PyQt6 upper bound is load-bearing; `requirements.txt` documents the failure mode it prevents, and `requirements-lock.txt` records the tested set.

---

## Embedded Agent - Threading/Async Bridge

Three independent execution contexts interact through exactly one discipline.

**Contexts**
- GUI thread - owns the Qt widgets, the `MeasurementEngine` object, the `PicoConnection` and all pyqtgraph plots. NEVER block it. NEVER touch serial here.
- Engine thread - the `MeasurementEngine(QThread)`. Owns serial I/O. Communicates outward ONLY through its Qt signals.
- Agent thread - `AgentWorker(QThread)`, which in `run()` creates and runs its OWN asyncio event loop (`asyncio.new_event_loop()` / `loop.run_until_complete(...)`). The streaming tool-use loop lives here. It NEVER touches a Qt widget, the engine or the connection directly.

**The single rule:** the agent thread touches the engine and the GUI only by marshaling onto the GUI thread, and it awaits engine completion through a thread-safe future resolved by a one-shot connection on the GUI thread.

Every tool that drives hardware follows this sequence (implemented once in `bridge.py` and `engine_adapter.py`, reused by all tools):
1. The tool handler runs INSIDE the agent thread's asyncio loop (it is an `async def`).
2. It builds a `TechniqueConfig` (pure data, safe on any thread) and checks it against the request limits; an over-limit request is refused here and goes no further.
3. To start the measurement it calls `await run_on_gui(fn)`, where `fn` is a zero-argument callable executed on the GUI thread, marshaled through a GUI-thread QObject and `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`. On the GUI thread, `fn`:
   a. checks `engine.isRunning()`; if the engine is busy, it reports "engine busy" back (the tool returns an error result and does not raise);
   b. connects ONE-SHOT slots to `measurement_finished` and `measurement_error` (and optionally `channel_changed` / `data_point_ready` for progress) that resolve a `concurrent.futures.Future`;
   c. calls `engine.start_measurement(connection, config)` on the SAME engine instance the plots are wired to, so the plots animate with no separate render path;
   d. returns immediately.
4. Back in the asyncio loop, the tool runs `result = await asyncio.wrap_future(future)`. The GUI is not blocked; the agent loop only suspends this coroutine until the signal fires.
5. The one-shot slots (GUI thread) set the future's result (`MeasurementResult` or an error string) and disconnect themselves, so they never fire for the next run.
6. The tool returns a compact text/JSON summary (channels, point counts, key metrics) to the model; large arrays are NOT put into the model context.

**Streaming out:** the agent loop emits Qt signals (`agent_text_delta(str)`, `tool_call_started(dict)`, `tool_call_finished(dict)`, `tool_call_error(dict)`, `agent_turn_done()`) through a QObject bridge; the dock's slots run on the GUI thread and update the chat, the tool chips and the figures. The agent thread NEVER calls a widget method.

**Stop:** the dock's Stop button cancels the current turn (not the worker thread) and aborts the measurement that turn started; the conversation can continue. Closing the application stops the thread. Once a stop is requested (`request_stop`), the worker starts no further tool and makes no follow-up request; `scripts/agent_goal_suite.py` uses this, through a directly connected `tool_call_started` slot, to end a session at its tool-call limit.

**Why this design:** running the async client in the GUI thread's event loop would need an extra integration library and risks blocking on serial-bound awaits; running tools that wait on the engine thread would deadlock signal delivery. One asyncio loop in a worker QThread, with completion bridged through futures, keeps the GUI responsive and reuses the existing engine and plot wiring untouched.

**Mock parity:** `MockMeasurementEngine` exposes the same signal set and `start_measurement(conn, cfg)`; it emits a short synthetic `DataPoint` stream and then `measurement_finished` on a QTimer, so the bridge, the tools, the plots and the smoke scripts exercise the real code path with no hardware.

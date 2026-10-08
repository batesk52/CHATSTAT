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
**Interfaces:** (WE and RE/CE are independently addressable. The `re_ce_channel` default of 1 keeps the older shared-RE/CE wiring working; it is not a hardware limit. The electrode configuration modes build on this.)
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
- `TechniqueConfig` - technique name, parameter dict, channels and wiring mode; validated in `__post_init__` (channel bounds, manual pairing length, current-range token form)
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

#### `sequence.py`
**Purpose:** Sequence model and `*.mux16seq` persistence
**Key Classes:**
- `SequenceStep` - a self-contained step (technique, params, channels, mode, repeat, delay)
- `Sequence` - ordered steps with `to_dict` / `from_dict` / `save_to_path` / `load_from_path`
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
- `ChannelPanel(QGroupBox)` - sixteen channel checkboxes with Select All / Select None
- `MeasurementControlPanel(QGroupBox)` - Start, Stop, Halt, Resume and the auto-save toggle
- `ElectrodeConfigPanel(QGroupBox)` / `ManualChannelPanel(QGroupBox)` - wiring mode selector and the manual per-WE RE/CE pairing table

#### Other `gui` modules
- `parameter_form.py` - widget factory for parameter fields; picks the technique-aware current-range ladder (`current_ranges_for`: the mode-3 ladder for EIS, mode 2 for the rest) and the `bw_hz` list
- `eis_plot_container.py` / `bode_widget.py` - Nyquist and Bode views; one container per live-plot tab
- `sequence_panel.py` / `sequence_step_widget.py` - the Sequence dock and its expandable step editor
- `agent_dock.py` - `AgentDockPanel`, the in-app chat (streaming text, inline tool chips, inline figures, math rendered to images)
- `toggle_switch.py` / `workers.py` - the `ToggleSwitch` widget and `ConnectWorker`

### Module: `agent`

The concurrency model is specified in full under "Embedded Agent - Threading/Async Bridge" below; that section is normative.

- `bridge.py` - `run_on_gui` / `await_signal`, the Qt-to-asyncio marshaling primitives
- `engine_adapter.py` - `EngineAdapter`: builds `TechniqueConfig`s, awaits engine completion, and implements the device tools. `run_geis` and `run_cp` exist here for programmatic callers but are not tools
- `tools.py` - tool definitions, `ToolRegistry` and dispatch. `MEASUREMENT_TECHNIQUES` decides which `run_*` tools exist
- `agent_worker.py` - `AgentWorker(QThread)`, which runs its own asyncio loop and the streaming tool-use loop
- `vendor_analysis.py` - seven analysis tools over the vendored analysis code
- `swv_analysis.py` - `analyze_swv`, a native square-wave peak analysis
- `preset_tools.py` - `save_preset` / `save_sequence` / `load_preset` / `load_sequence`
- `mock_engine.py` - mock engine and mock connection with the same signals, for the no-hardware path

**Tool surface: 22 tools** (4 `run_*`, 5 device tools, `export_session`, 7 vendored analysis tools, `analyze_swv`, 4 preset and sequence tools). The run surface is deliberately narrower than the generator registry: there is no `run_geis` (galvanostatic impedance is withdrawn) and no `run_cp` (the EmStat Pico has no galvanostat). `analyze_cp` remains, because chronopotentiometry data recorded on other instruments can still be analyzed.

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
2. It builds a `TechniqueConfig` (pure data, safe on any thread).
3. To start the measurement it calls `await run_on_gui(fn)`, where `fn` is a zero-argument callable executed on the GUI thread, marshaled through a GUI-thread QObject and `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`. On the GUI thread, `fn`:
   a. checks `engine.isRunning()`; if the engine is busy, it reports "engine busy" back (the tool returns an error result and does not raise);
   b. connects ONE-SHOT slots to `measurement_finished` and `measurement_error` (and optionally `channel_changed` / `data_point_ready` for progress) that resolve a `concurrent.futures.Future`;
   c. calls `engine.start_measurement(connection, config)` on the SAME engine instance the plots are wired to, so the plots animate with no separate render path;
   d. returns immediately.
4. Back in the asyncio loop, the tool runs `result = await asyncio.wrap_future(future)`. The GUI is not blocked; the agent loop only suspends this coroutine until the signal fires.
5. The one-shot slots (GUI thread) set the future's result (`MeasurementResult` or an error string) and disconnect themselves, so they never fire for the next run.
6. The tool returns a compact text/JSON summary (channels, point counts, key metrics) to the model; large arrays are NOT put into the model context.

**Streaming out:** the agent loop emits Qt signals (`agent_text_delta(str)`, `tool_call_started(dict)`, `tool_call_finished(dict)`, `tool_call_error(dict)`, `agent_turn_done()`) through a QObject bridge; the dock's slots run on the GUI thread and update the chat, the tool chips and the figures. The agent thread NEVER calls a widget method.

**Stop:** the dock's Stop button cancels the current turn (not the worker thread) and aborts the measurement that turn started; the conversation can continue. Closing the application stops the thread.

**Why this design:** running the async client in the GUI thread's event loop would need an extra integration library and risks blocking on serial-bound awaits; running tools that wait on the engine thread would deadlock signal delivery. One asyncio loop in a worker QThread, with completion bridged through futures, keeps the GUI responsive and reuses the existing engine and plot wiring untouched.

**Mock parity:** `MockMeasurementEngine` exposes the same signal set and `start_measurement(conn, cfg)`; it emits a short synthetic `DataPoint` stream and then `measurement_finished` on a QTimer, so the bridge, the tools, the plots and the smoke scripts exercise the real code path with no hardware.

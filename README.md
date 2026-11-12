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
- **Electrode Configuration.** The **Setup** row picks a saved electrode setup by name (see Electrode setups below). Below it, one table lists the sixteen working electrodes: tick **Enable** for each one to measure, and pick its **RE/CE** position (CH1 to CH16). There is no wiring mode to choose. Position 15 is the external reference and counter, and position 16 the on-board combined (shorted) reference and counter; any working electrode can use either. When the working electrodes use different RE/CE positions, both the working electrodes and the RE/CE positions must be within CH1 to CH14, because 15 and 16 are the shared positions. **Select All** ticks every row the current table can extend to (all 16 when the ticked rows share position 15 or 16, CH1 to CH14 otherwise; see Electrode setups below), **Select None** clears every row, **RE/CE = own position** pairs each ticked electrode in CH1 to CH14 with the RE/CE at its own number, and **All to:** with **Apply** sets one RE/CE position for every ticked electrode. Sixteen working electrodes sharing position 15 or 16 have been used, but no validation dataset was recorded; a separate RE/CE per working electrode has not been run on hardware. See `docs/enclosure_design.md` and `docs/pre_enclosure_smoke.md`.
- **Settle time** (100 ms by default) is the wait after each switch before sampling. In CA (MUX-Alternating), **Samples per channel visit** sets how many samples of nominally 10 ms are averaged into one point, and **t interval** is the requested round time.

**Start** begins the run. **Halt**, **Resume** and **Stop** act on the instrument directly. Every run opens a new plot tab.

## Presets and sequences

A preset is a named configuration: technique, parameters, channels and wiring. Presets live in one file, by default `~/.emstat_pico_mux16/presets.mux16`. The first launch creates it with three built-in presets (CV, EIS and CA). To move presets to another computer, copy the file and use **Import preset file...** at the end of the preset list. The settings folder and the `.mux16` and `.mux16seq` formats keep the names of earlier versions, so existing presets and sequences keep working.

The **Sequence** dock runs presets back to back, each step with a repeat count and an idle delay. Each step stores its full configuration, so a saved `*.mux16seq` file works on any computer and survives later edits to its source preset. The whole queue is checked before the first step starts.

## Electrode setups

An electrode setup is the electrode half of a measurement: which working electrodes are measured and which reference/counter (RE/CE) position each one uses. It has no technique and no parameters, so one setup serves any technique, and you pick it by name. Every setup is the same kind of thing, a table with one RE/CE position per working electrode: an "external reference" setup is simply one whose table points every electrode at position 15, and an "on-chip reference" setup one that points them all at 16. Pick one in the **Setup** row of the Electrode Configuration panel and the table is filled to match. If you then change a row by hand, the Setup row goes back to **(Custom)**. The save button stores the current table under a name you give, and asks before it replaces a setup of the same name. The trash button deletes a setup you saved.

Five setups are built in:

- **Separate reference + counter electrodes.** CH1 to CH16, every one with RE/CE position 15 (the external reference and counter).
- **Reference + counter on the chip (shorted).** CH1 to CH16, every one with RE/CE position 16 (the on-board combined reference and counter).
- **One RE/CE per working electrode (CH1-CH14).** Each CH n with RE/CE n, as on the vendor resistor test cell.
- **16-site die, external RE + CE.** For a die with 18 pads in a row. Pads 1 to 8 are CH1 to CH8 and pads 11 to 18 are CH9 to CH16; the middle two (pads 9 and 10) are not measured. A separate reference electrode and counter electrode are wired in from outside, so every channel uses RE/CE position 15.
- **16-site die, on-chip RE + CE (pads 9, 10).** The same die and the same channels, but the reference and counter are the ones on the die: pad 9 is the reference and pad 10 the counter. Every channel uses RE/CE position 16, which shorts the reference and counter together, so short pads 9 and 10 together outside the die. Pick the one that matches how the die is connected; the two differ only in the RE/CE position.

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
      "channels": [1, 2, 3, 4],
      "re_ce_channels": [15, 15, 15, 15],
      "description": "CH1 to CH4 against the external reference and counter."
    },
    "bench_on_board": {
      "name": "Bench, on-board RE/CE",
      "channels": [5, 6],
      "re_ce_channels": [16, 16]
    },
    "bench_pairs": {
      "name": "Bench, paired",
      "channels": [2, 3],
      "re_ce_channels": [5, 6],
      "description": "WE CH2 with RE/CE CH5, WE CH3 with RE/CE CH6."
    }
  }
}
```

- The entry name (`bench_external`) is the key. Use letters, digits and underscores. A key that matches a built-in (`separate_re_ce`, `on_chip_re_ce`, `per_electrode_re_ce`, `die_16_external`, `die_16_on_chip`) replaces that built-in.
- `name` is what the Setup row shows.
- `channels` are the working electrodes, in run order, without repeats (CH1 to CH16).
- `re_ce_channels` gives one RE/CE position for each entry of `channels`, in the same order. All 15 or all 16 shares that position across every working electrode; otherwise every working electrode and every RE/CE position must be within 1 to 14.
- The app also writes `electrode_config_mode` (`external`, `on_board` or `manual`), an internal label worked out from `re_ce_channels`; you do not need to write it. An entry with `electrode_config_mode` set to `external` or `on_board` and no `re_ce_channels` means every working electrode at position 15 or 16. The app saves a setup whose working electrodes all share position 15 or 16 in that short form, so the previous version of ChatStat can still read the file; it expands to the full list on load, and nothing is lost. Every other table is saved in full, which the previous version also reads. Every table the rules above accept fits one of the two forms. A hand-written entry with a full list of 15s or 16s (as in the example) loads here but not in the previous version.
- `description` is optional text; `site_labels` is optional too (`{"9": "pad 11"}` names what a channel is on your device).

ChatStat reads the file when it starts and when you choose it in the menu, so restart it after editing by hand. A setup that does not follow these rules is skipped, the others still load, and the status bar says what was skipped. Before your next save in the app, the whole file is copied to `.bak`, so a hand edit is never lost. A file that is not valid JSON is treated the same way: the built-in setups still work, and the file is kept as `.bak` before a save writes a new one.

Measurement presets carry their own channels and RE/CE list and load into the same table, or name an electrode setup. A preset you save while the Setup row names a setup keeps that setup's name (`electrode_setup` in the `.mux16` file); its `channels` say which of the setup's working electrodes it measures, and the setup gives each one's RE/CE. The setup is looked up when the file is loaded and again when you apply the preset, so changing the setup changes the preset's wiring. The preset keeps the channel list it was saved with: channels added to the setup later are not measured by it, and if the setup later loses one of those channels, applying the preset is refused with a message naming them. If the named setup no longer exists, importing the file fails with a message naming it, a preset in your own store is left out of the list (the status bar says which, and the file keeps it), and applying it shows a warning and changes nothing. A sequence step can name a setup the same way (loading the sequence fails if the setup is missing); editing the step's channels or RE/CE by hand drops the name. These files still store the wiring as well, so an earlier version of ChatStat reads the same wiring, and files without a setup name are written exactly as before. When every channel of a preset or setup shares RE/CE position 15 or 16, applying it sets that position on every row. **Select All** follows the enabled rows: if they all share position 15 or 16 it enables all 16 rows at that position; if they form any other (per-electrode) table it enables CH1-CH14 only, since CH15 and CH16 are then the shared RE/CE positions, and pairs the new rows with their own position when every enabled row already is; with no row enabled it enables all 16 as they stand. A table Start still refuses is refused with a message naming the position. A sequence step shows one **RE/CE per WE** field (one position for each channel, or one position for all of them) and no mode choice. The agent picks a setup by name too (see the agent dock below).

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

The run tools use the same engine as the **Start** button, and the agent never sees raw MethodSCRIPT or serial commands. **Electrode setups by name.** `device_status` lists the electrode setups you can pick in the Setup row (the built-ins and your saved ones, with what each does), and the run tools, `save_preset` and each step of `save_sequence` take an optional `electrode_setup` with a setup's key or name. The setup then gives the working electrodes and their RE/CE positions, run in ascending channel order as the **Start** button runs them; `channels` can name some of its working electrodes to measure only those. An `electrode_setup` of null or an empty string counts as not given. A request that names a setup and also gives `re_ce_channels` is refused, and so is an unknown name (the error lists the setups). Without a setup the agent gives `channels` and, if needed, one RE/CE position per channel in `re_ce_channels`; with neither wiring, every channel uses position 15, as before. A preset or sequence saved this way keeps the setup's key and is wired from the setup whenever it is loaded. A name resolves by key first; a display name that two setups share is refused with their keys. Every run result reports how many points the instrument flagged as overloaded. EIS and SWV results add a verdict for each channel, such as "underranged", and suggest a current range one step up; when even the largest range fails, the result says the fault is in the cell or the wiring. CV and CA results add a ripple index. `analyze_eis` withholds the charge-transfer resistance, and gives a lower bound, when the sweep did not reach the top of the semicircle. `analyze_swv` withholds a peak on a sweep edge or below its noise threshold. The dock's **Stop** button cancels the model's turn and aborts the measurement that turn started.

**Session logs.** Every agent conversation writes a log, one JSON object per line, to `~/.emstat_pico_mux16/agent_sessions/YYYYMMDD_HHMMSS_<id>.jsonl`. The log pane shows its path when the conversation starts. The log records the start of the session (application version, model, tool names, mock or hardware, port), each message from you and from the agent, each tool call with its arguments, and each result exactly as the model saw it, with its duration. It also records errors and the end of the session, a `limit_refused` line for each request that the limits below turned away, a `cell_switch_requested` and a `cell_switch_resolved` line for each solution change the agent asked for, and a `memory_saved` line for each memory note. Each line has a time stamp and is written to disk at once, so a crash leaves a readable file. When a tool call sends a script to the instrument, its result line carries the script digest that the log pane shows (`Script digest=...`); an `export_session` result line carries the exported file path. The API key and request headers are never written.

**Charge storage capacity.** Given a scan rate and an electrode area, `analyze_cv` reports the charge storage capacity (CSC, in mC/cm2) as three labelled numbers. `csc_total_mc_per_cm2` is the usual one: the integral of the absolute current over one full cycle, both sweeps, divided by scan rate and area. `csc_cathodic_mc_per_cm2` and `csc_anodic_mc_per_cm2` are the area between the two sweeps where the current is negative or positive. For a capacitive loop the total is about the sum of the other two; with current of one sign on both sweeps (for example oxygen reduction) the two area numbers can be near zero while the total is not. The result carries a note with these definitions, and the agent is told to say which one it quotes. A CV that does not close (it ends well short of where it started) is flagged, and its total covers only the part that was recorded.

**Changing the solution.** The agent cannot change the solution in the cell, and it does not guess. Before any step that needs a different solution than the one now in the cell (for example PBS, then a plating bath, then PBS again), its instructions tell it to call `request_cell_switch` with the new medium and the reason, and to wait. The dock then shows a **Waiting for cell switch** banner: "Switch the cell to the new medium, then press Continue", with **Continue** and **Cancel**. The agent's turn is paused until you press one; the rest of the window, and the **Stop** button, keep working. Continue tells the agent the cell is switched (`performed` true) and it goes on. Cancel tells it the cell was not switched; it runs nothing for that step and asks what to do. Stop ends the wait and takes the banner down. The tool refuses while a measurement is running.

Each request is logged twice, `cell_switch_requested` when it is made and `cell_switch_resolved` when it ends, and the tool result carries the same record, whoever does the switching: what was requested (medium, reason, time), whether it was performed (done, time, who, and by what: `human` here), and how it ended (`confirmed`, `cancelled` or `stopped`) and how long it took. The "who" is the login name of the computer. The record format does not depend on a person doing the switch, so a robot that implements the same two calls (show a request, take it down) can answer in its place.

**Memory notes.** A chat can be saved as a memory note, so that a later session, with you or with someone else, can find out how the work was done. Press **Save chat to memory** under the chat, or ask for it in words. The agent then calls `save_memory` and writes one text file with these headings: Goal, Electrode setup, Electrochemical setup, What was done, What was found, Problems and fixes, Data files, Open questions. A note is for the context that the data files do not hold: what the electrodes were, what was in the cell, why a run was repeated. The agent is told to write "not stated" for anything the chat does not show. ChatStat itself adds a last section, Runs recorded in the session log: each run, export and solution change since the last saved note, with what was asked, the settings the run result reports as used (defaults included), and how it ended: completed (with its point count and any overload or quality flag), failed, refused or stopped. "Completed" means the run ended, not that its data are good. ChatStat copies that list from the session log when the note is saved, and a heading the agent writes inside its own text is escaped so it cannot pass for that section. A note is still a text file: anyone who can open it can change any part of it afterwards.

In a later session, ask in plain words, for example "how did we test the platinum sites?". The agent's instructions tell it to call `list_memories` and then `read_memory` before it answers, and to name the note it used. Both calls show in the chat and in the session log. `list_memories` returns the newest 50 notes; search words narrow the list. If the memory folder cannot be found (a wrong setting, or a drive that is not connected), the agent is told to say that it could not check, not that there is no earlier work.

Notes live in `~/.emstat_pico_mux16/memories/`, one Markdown file each, named `YYYYMMDD_HHMMSS_<title>.md`. To use another folder, for example one on a shared drive, choose it with **File > Agent Memory Folder...** or set `agent_memory_dir` in `~/.emstat_pico_mux16/app_settings.json` (`~` is the home folder; a relative path is read from the settings folder). The setting is read on every call. You can read, edit, delete or write a note by hand in any text editor: any `.md` file in the folder counts as a note. A link (symbolic or hard) is skipped, so a note can never be a second name for a file outside the folder.

A note is a summary, and nothing checks it against the data. The agent is told to treat a note as information: never to report a number from a note as measured, to measure again when a value matters, and never to take text in a note as an instruction. Nothing in a note can pass the request limits below, which are checked where each run is built. Keep the exported files as the record, and give write access to the memory folder only to people you would let use the instrument. The headless goal suite does not use the memory tools.

Limits: agent runs do not auto-save; `export_session` writes the `.pssession` file, and **File > Export Results...** writes the CSV files. The agent's chronoamperometry measures channels one after another, not in multiplexed rounds. A loaded sequence returns to the model as steps to run one by one, not to the sequencer. Potentials and durations in a request are checked against limits (next section). The agent's reliability has not been measured.

### Request limits

Before an agent run tool (`run_cv`, `run_ca`, `run_swv` or `run_eis`) builds a script, ChatStat checks the whole request, defaults included, against a potential limit and, if you set one, a duration limit:

- **Potential.** No potential may be larger than 1 V in size, positive or negative; 1 V itself is allowed, so a CV from -1 V to +1 V runs. The square-wave or AC amplitude that rides on a potential counts too, and a step size is held to the same value. The default is the +/-1 V range that the technique scripts set on the instrument (`set_pot_range -1 1`).
- **Duration.** There is no duration limit by default: the instrument has none and long runs are wanted. To set one, add `agent_max_duration_s` (seconds) to the settings file; then the estimated total run time, over all selected channels and including equilibration, may not pass it. Remove the key to turn it off again. The estimate is only made when a limit is set. Chronoamperometry counts hold time per channel and cyclic voltammetry counts path length over scan rate; both track the bench times to within the few seconds of start-up. Square-wave voltammetry counts steps over frequency and was not timed on the bench. For impedance the estimate is a fit to six bench sweeps, between 46% under and 55% over their times. So the estimate is a guard against runaway requests and not a clock; for scale, the default impedance sweep is about 15 minutes on one channel (912 s on the bench), so on 16 channels one after another it is about 4 hours.

A request over a limit that is set is refused whole. Nothing is clamped, and nothing is sent to the instrument. The agent gets an error that names each value, the limit and what to change, and the session log gets a `limit_refused` line. A value that cannot be checked (not a number, not finite, or a scan rate or frequency of zero) is refused too.

You set the limits in `~/.emstat_pico_mux16/app_settings.json`: `agent_max_abs_potential_v` in volts and, if you want one, `agent_max_duration_s` in seconds. They are read again at every request, so an edit applies at once. A stored value that is not a positive number is ignored with a warning, and the default applies (1 V, and no duration limit). The agent has no tool that can change them. The limits apply to the agent only: the **Start** button and the **Sequence** dock are not checked. They apply to each request alone; they do not cap the total over several requests.

## Use ChatStat from Claude Code or Claude Desktop

ChatStat can serve the agent's 26 tools over MCP (the Model Context Protocol), so an MCP client on the same computer, such as Claude Code, can drive the instrument with the model and subscription it already uses. The client calls the same tools as the dock, through the same code: the same request limits, electrode setups by name, refusal messages and live plots. The server runs inside the ChatStat window, so the window must be open.

**Turn it on.** **File > Serve Agent Tools over MCP**. It is off until you turn it on; the choice is saved (`mcp_server_enabled` in `app_settings.json`), so it starts with the window next time. The status bar shows `MCP: off` or `MCP: http://127.0.0.1:47615/mcp`. **File > MCP Server Details...** shows the address and the access token (hidden until you press **Show**), copies a ready Claude Code command, and makes a **New Token** (clients set up with the old one stop working). The port is the `mcp_server_port` setting (default 47615). If the port is taken, for example by a second ChatStat window, the status bar says `MCP: could not start`.

**Claude Code.** Copy the command from **MCP Server Details...**, or type it with your token:

```
claude mcp add --transport http chatstat http://127.0.0.1:47615/mcp --header "Authorization: Bearer <token>"
```

This adds the server for the current project folder (`--scope user` adds it everywhere; `--scope project` writes it to the project's `.mcp.json`, where `"Authorization": "Bearer ${CHATSTAT_TOKEN}"` keeps the token itself out of the file, by Claude Code's documentation). In Claude Code, `/mcp` shows whether it connected. The tools appear as `mcp__chatstat__run_ca` and so on.

Checked with Claude Code 2.1.322 on the mock engine, using the same server entry in a config file passed with `--mcp-config` (`{"mcpServers": {"chatstat": {"type": "http", "url": "...", "headers": {"Authorization": "Bearer <token>"}}}}`): it listed the 26 tools, ran a chronoamperometry hold, and got the limit refusal for a 2 V request word for word.

**Claude Desktop.** Claude Desktop's documented configuration file (`claude_desktop_config.json`) starts local servers as programs (`command` and `args`); an entry with a URL is not documented there. Its **Connectors** for remote servers connect from Anthropic's cloud and need a server reachable from the internet, so they cannot reach ChatStat on 127.0.0.1. To reach ChatStat from Claude Desktop, put a bridge between them that Claude Desktop starts as a program and that talks HTTP to ChatStat. One that is widely used is the community package [`mcp-remote`](https://github.com/geelen/mcp-remote) (it needs Node.js; it is not made by Anthropic or by this project). Following its README (version 0.16.6):

```json
{
  "mcpServers": {
    "chatstat": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:47615/mcp",
               "--allow-http", "--body-timeout", "0",
               "--header", "Authorization:${CHATSTAT_AUTH}"],
      "env": { "CHATSTAT_AUTH": "Bearer <token>" }
    }
  }
}
```

The header has no space after the colon and the token goes through `env`, as the `mcp-remote` README advises (spaces inside `args` can be split). `--allow-http` lets it use plain HTTP; `--body-timeout 0` stops it from closing a response stream that has been quiet for five minutes (its default), which a long sweep or a cell-switch wait can be. `mcp-remote` also offers `--header-file`, which keeps the token out of the process list. Restart Claude Desktop after editing the file. This bridge was not tried with ChatStat, and Claude Desktop's own time limit for a tool call is not documented, so a long run or a cell-switch wait may be cut off there.

**What the client gets.** The tool list, with the same descriptions and schemas as the dock. The server's instructions carry the same wiring check and operating rules as the dock's system prompt (the text is shared in the code), plus a note for outside clients; the same text is also the MCP prompt `chatstat_rules`, for clients that do not pass server instructions to the model. Results are the same compact JSON the dock's model reads. Plots and analysis figures appear in the ChatStat window, not in the client.

**What does not carry over.** The live-model goal-suite results were measured with the dock's prompt, its models and its loop. A different client, model or prompt has not been measured, so nothing is known about how well it does. The **Save chat to memory** button belongs to the dock; an MCP client can still call `save_memory` when you ask it to.

**Long calls.** A run blocks until it ends, and an impedance sweep can take 15 minutes per channel. `request_cell_switch` shows the **Waiting for cell switch** banner in the dock and returns only when you press **Continue** or **Cancel** in the window. While any call runs, the server sends an MCP progress notification every 10 s, if the client asked for progress. Whether a client extends its own time limit on progress is up to the client. In Claude Code, by its documentation, the default limit for a tool call is long (about 28 hours, set with the `MCP_TOOL_TIMEOUT` environment variable), but a per-server `timeout` in a configuration file is a hard limit on each call that progress notifications do not extend; leave it unset, or set it longer than your longest run plus any cell-switch wait. None of this was tested here with a call that long. A call the client cancels is stopped the way the dock's **Stop** button stops a turn: a running measurement is aborted and the banner is taken down.

**Safety.**

- Off by default. Listens on 127.0.0.1 only, so other computers cannot reach it.
- Every request needs the access token (`Authorization: Bearer <token>`); without it the answer is 401. The token is made once per install (`mcp_server_token` in `app_settings.json`, in plain text beside the API key) and is never written to the log pane or the session log. Requests whose Host or Origin header is not 127.0.0.1 or localhost are refused, so a web page cannot reach the server.
- The request limits apply to every run, as in the dock. No tool can change them.
- One driver at a time. A run while any measurement is running is refused ("Engine is busy"). While the dock's agent is in a turn, or a sequence runs, every MCP tool except `device_status`, `list_ports`, `list_memories` and `read_memory` is refused and nothing is run. The dock's agent is not held back by an MCP client in the same way: if both work at once, the engine's busy check is what keeps two measurements apart. `abort_measurement` from either one stops whatever run is going, as it does from the dock today.
- Anyone who has the token and can run programs on this computer can drive the instrument through it. Treat it like the API key.

**Session log.** MCP calls go to the same session log as the dock's (see Session logs above), marked `"source": "mcp"` with the client's name (`"client"`), and the run list in a saved memory note marks them `(via mcp client ...)`. If an MCP client is the first to use the agent tools, it opens the log.

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

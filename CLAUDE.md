# Project Conventions

Template: codebase

ChatStat is a PyQt6 desktop application for the PalmSens EmStat Pico on its MUX16 multiplexer board, with an embedded language-model agent. `README.md` is the public user guide, not a blueprint; track work in `action_log.md`.

## Ground Rules

- **This repository is public.** No internal project IDs, names of people, sample or chip IDs, or plans in any tracked file: code, comments, tests, docs and these core files.
- **No emojis** anywhere (source, logs, prints, Markdown). The Windows console uses cp1252.
- **Eager native imports.** Import numpy, scipy, pandas, matplotlib (Agg backend) and anthropic at module top, before any asyncio loop starts. A first import of a native extension inside a running loop can deadlock on Windows while its DLLs load.
- **Vendored analysis is read-only.** `src/vendor/electrochem_analysis/` is a copy of a separate analysis library; its package docstring records the source and the copy. Never edit it in place: fix the library, then copy it again.
- **Tests** live in `tests/`, mirror `src/`, and need no instrument and no API key: `QT_QPA_PLATFORM=offscreen python -m pytest -q`.
- **Agent scripts** (one-off checks, validation, debugging) go in `claude_test_files/`, which is gitignored. Move anything worth keeping into `tests/` or `examples/smoke/`. The smoke scripts in `examples/smoke/` must keep printing `SMOKE PASS`.
- **Settings folder:** `~/.emstat_pico_mux16/` holds `presets.mux16` and `app_settings.json` (which includes the agent API key in plain text). Keep this folder name and the `.mux16` and `.mux16seq` formats unchanged, so existing presets and sequences keep loading.
- **Mock mode:** `EMSTAT_AGENT_MOCK=1` points the agent's tools at the mock engine and mock connection (`src/agent/mock_engine.py`). The manual controls still drive the real instrument.
- **Example data** in `examples/data/` stay byte-identical to what the application exported. `.gitattributes` turns off line-ending conversion there.

## Code Style

Python 3.10+, PEP 8, 88 characters (Black formatter), Google-style docstrings.

## Project-Specific Patterns

### Serial Communication
- Always use XON/XOFF flow control (pyserial `xonxoff=True`), as the PalmSens communication protocol requires
- Terminate every command with `\n` (LF only, never CR)
- The device echoes the first character of a command; strip it before parsing the response
- Read timeouts: 5 s for commands, 60 s or more during measurements

### MethodSCRIPT
- Values use SI prefix notation: `500m` = 0.5, `100u` = 0.0001
- Integer values carry an `i` suffix: `0x3FFi`, `16i`
- No empty lines inside a script (the device reads one as the end of the script)
- Every script MUST include `on_finished:` followed by `cell_off`

### MethodSCRIPT Firmware
- Firmware 1.6 or later is required for the compact `loop i <= e` multiplexer pattern with `set_gpio i`
- `store_var` integer values MUST have the `i` suffix: `store_var i 0i aa` (not `0 aa`)
- `add_var i 0b01`: use binary format for the GPIO address increment
- `meas_loop_ca` argument order: `p c <e_dc> <t_interval> <t_run>` (interval before run)
- `meas_loop_eis`: the final argument is the DC potential (manual section 14.46), not a flag; pass `e_dc` or `i_dc`, not `0`
- SI values are an integer mantissa plus a prefix (`2565`, `100k`, `500m`). A decimal mantissa (`2.565k`) is rejected with `e!4004`. `_format_si` enforces this; never build values with decimals by hand
- **The EIS current range is selectable** (`eis_range_mode`: `"static"` or `"auto"`, static by default). `"static"` pins the range (`set_autoranging ba {cr} {cr}`), because range switching inside the sweep loop corrupts the spectrum of some cells on firmware 1.6.01 (the low-frequency Nyquist arc reverses or Z' goes negative). `"auto"` emits `set_autoranging ba 1n {cr}`, which high-impedance two-electrode cells need. In `"auto"`, `cr` is the ceiling; in `"static"`, it is the pinned range. The options live in `scripts.EIS_RANGE_MODES`, the single source for the GUI and the agent. Do not hard-pin EIS: the right choice depends on the electrode setup. Mode-2 techniques (CV, CA and the others) keep real autoranging. Mode 3 has its own range ladder (`100n, 1u, 6u, 13u, 25u, 50u, 100u, 200u, 1m, 5m`); the mode-2 values `2u` and `63u` return no data in mode 3 (see `parameter_form.current_ranges_for`)
- **EIS equilibrates at `e_dc` on the selected channel.** `_gen_eis` emits `set_e {e_dc}` and then `wait {t_eq}` inside the technique body, and `generate()` skips its generic wait for techniques in `_SELF_EQUILIBRATING`. A wait placed first would hold the cell at 0 V, and on multichannel runs the generic wait fires before the GPIO set-up, so it would bias whichever channel a previous script left selected. As a result `t_eq` is spent once per channel (`t_eq=60` on 16 channels takes 16 min). `tests/techniques/test_eis_equilibration.py` checks that `set_gpio` precedes `set_e` on all three script paths
- **EIS Rct is meaningful only if the -Z'' semicircle apex is captured.** When the maximum of |Z''| lies at the lowest swept frequency, `vendor_analysis._eis_apex_assessment` sets `rct_ohm`, `peak_frequency_hz` and `time_constant_s` to null and reports `rct_lower_bound_ohm` instead. Rs and |Z| at 1 kHz stay valid

### MUX16 Addressing
- Hardware labels are 1-indexed (CH1-CH16); GPIO addresses are 0-indexed
- **WE and RE/CE are independently addressable.** Two 16:1 multiplexers: bits[3:0] select the WE, bits[7:4] the RE/CE pair. `MuxController.channel_address(channel, re_ce_channel=1)` encodes both. The electrode configuration modes build on this: external RE/CE at CH15, on-board RE/CE at CH16, and manual pairing among CH1-CH14
- The `re_ce_channel=1` default keeps the older "shared RE/CE on position 1" wiring working; it is not a hardware limit
- Whether RE/CE is physically shared is a wiring choice, not a code one. See `docs/enclosure_design.md`, the single source of truth for enclosure wiring
- Enable bits (9:8) are inverted: 0 = enabled, 1 = disabled

### Data Packet Decoding
- Packet format: `Pvar1;var2;...varN\n`
- Variable: 2-character type code + 7-character hex value + 1-character SI prefix
- Decode: `(hex_to_uint(value) - 2^27) * 10^(SI_exponent)`
- Variable types: `da` = set_potential, `ab` = potential, `ba` = current, `cb` = impedance, `ca` = phase, `cc` = zreal, `cd` = zimag, `dc` = set_frequency
- All currents share the `ba` code, and the parser keys values by name, so a packet can carry only one current. SWV therefore packs only the net current; never add `pck_add f` or `pck_add r`

### Threading
- GUI thread: never perform serial I/O or blocking waits
- Engine thread (QThread): all serial communication happens here
- Data flow from the engine to the GUI goes through Qt signals only (no shared mutable state)
- Abort, halt and resume are sent from the GUI thread through `PicoConnection`'s locked write
- The agent runs its own asyncio loop in a worker QThread and reaches the GUI and the engine only through `src/agent/bridge.py` (see `architecture.md`)

### Error Handling
- Serial disconnection: emit an error signal and set the connection state to disconnected; never raise in the GUI thread
- MethodSCRIPT errors: parse the device error codes (Appendix A of the communication protocol) and show them in the status bar
- Always validate channel numbers (1-16) before generating multiplexer scripts

## Dependencies

`requirements.txt` is the single source of truth for versions; `requirements-lock.txt` records the exact set that the tests passed with. One upper bound is load-bearing: `PyQt6<6.11`. The 6.11.x wheels import `icuuc.dll` but ship no ICU, so every Qt import fails. Do not relax it without re-testing.

## Domain Terminology

| Term | Definition |
|------|------------|
| MethodSCRIPT | PalmSens scripting language that runs on the instrument |
| MUX16 | 16-channel multiplexer board; WE (bits[3:0]) and RE/CE (bits[7:4]) independently addressable |
| pck | Packet: data output block in MethodSCRIPT (`pck_start`, `pck_add`, `pck_end`) |
| SI prefix | One character for a scale factor (a, f, p, n, u, m, space, k, M, G, T, P, E) |

## File Organization

- Keep new files under 300 lines where practical
- Exports: one folder per run, `YYYYMMDD_HHMMSS_<technique>/`, with per-channel CSV files and one `.pssession`

## Reference Documentation

- [MethodSCRIPT v1.6 manual](https://assets.palmsens.com/app/uploads/2024/08/MethodSCRIPT-v1_6.pdf)
- [EmStat Pico communication protocol v1.5](https://www.palmsens.com/app/uploads/2025/03/Emstat-Pico-communication-protocol-V1.5.pdf)
- [Getting Started with the EmStat Pico MUX16](https://assets.palmsens.com/app/uploads/2021/06/Getting-Started-with-the-Emstat-Pico-MUX16.pdf)
- [PalmSens MethodSCRIPT examples (GitHub)](https://github.com/PalmSens/MethodSCRIPT_Examples)

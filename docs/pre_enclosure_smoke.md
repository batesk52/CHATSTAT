# Pre-enclosure smoke test — Electrode Configuration modes A/B/C

A ~5-minute bench protocol that validates the Mode A/B/C feature on real hardware **before the enclosure is built**, using only the EmStat Pico MUX16 + the test cell shipped in the kit. Three GUI-driven runs, each with a deterministic pass criterion read off the live plot.

## What this proves (and what it doesn't)

**Proves:**
- The Pico firmware accepts the new MUX GPIO addresses (`bits[7:4]=14` for Mode A, `bits[7:4]=15` for Mode B).
- The MUX silicon physically routes RE+CE to the position the GUI selects (not just symbolically).
- Mode C's sequential-fallback script (per-channel varying RE/CE) executes end-to-end on hardware.
- The GUI → `TechniqueConfig` → `MeasurementEngine` → script-on-wire plumbing is intact for all three modes.

**Does NOT prove:**
- The Mode A / Mode B 16-WE workflow with a shared external RE+CE. That requires either the enclosure (which hardwires external Ag/AgCl + Pt to position 15 / 16) or a real cell with externally-jumpered RE+CE. See [enclosure_design.md](enclosure_design.md) for the production wiring; see "Going beyond the test cell" below for a breadboard cheat.
- Anything about real electrochemistry — Rct accuracy, Nyquist correctness, cycling regime, etc. Those are bench-validation steps for whichever study is consuming the instrument, not properties of the MUX routing this protocol checks.

## Why the test cell works as a Mode A/B validator

Per the PalmSens *Getting Started with the EmStat Pico MUX16* guide (page 4), the test cell is **16 independent 3-electrode circuits**: each channel N has its own resistor R_N connecting WE_N to its matching CE_N + RE_N pins, with R_1 = 1 kΩ rising linearly to R_16 = 8.66 kΩ. The reference script (doc page 5) walks `bits[7:4]` and `bits[3:0]` in lockstep — confirming the test cell expects "WE position = RE/CE position" per channel.

That wiring is exactly what makes this test cell a clean Mode A/B validator:
- In **Mode A** (RE/CE locked at position 15), only WE = CH15 closes a circuit through R_15. Every other WE channel's pin connects to a resistor whose other end is at position N, not 15 → open circuit.
- In **Mode B** (RE/CE locked at position 16), only WE = CH16 closes through R_16.

So a single ohmic line at the expected channel — with the other 15 channels flatlining — is the smoking-gun proof that the new `bits[7:4]` encoding is electrically active on the silicon.

For **Mode C** with "Apply same-position," the GUI tells the MUX to walk (WE_N, RE/CE_N) together for each enabled row — which matches the test cell's wiring exactly, so every enabled channel closes through its own R_N. All 14 channels (CH1–CH14, the Mode C selectable range) show ohmic lines with varying slope. This matches Figure 5 of the PalmSens guide, minus the two reserved channels.

## Setup

1. EmStat Pico MUX16 powered, USB to laptop, COM port up.
2. Test cell connected to the MUX16 via the three supplied flat cables (see PalmSens guide Figure 4 for the photo).
3. No real cell, no enclosure, no extra wiring.
4. Launch the GUI: `python -m src.gui.main_window`. Click Connect; firmware version should appear.
5. (Optional but recommended) clear the Log tab so any `e!xxxx` line is obvious.

**Connector caveat.** The PalmSens guide labels the test cell's connectors `CON1 = CE`, `CON2 = RE`, `CON3 = WE`, while the enclosure wiring in `enclosure_design.md` uses `CON4 = WE`, `CON2 = RE`, `CON3 = CE`. Two different naming schemes float around the project. Wire by **the physical labels stamped on the MUX16 unit itself** — those are authoritative; the cable orientation needs to match what the test cell expects, not what either set of docs says. Also note the v1 PCB has a CON2/CON3 silkscreen swap (fixed in v2).

## The three runs

Use a LSV from −1.0 V to +1.0 V at 1 V/s, t_eq = 2 s, current range = 1 mA (matches the parameters from PalmSens guide Figure 5, gives unambiguous slopes against the kΩ-scale test cell). Any CV preset with similar range works too.

### Run 1 — Mode A regression

| Setting | Value |
|---------|-------|
| Electrode Configuration | **Separate CE + RE (external)** |
| Channels (WE) | CH1 through CH16 (Select All) |
| Technique | LSV per above |

**Expected:** Only **CH15** shows current. Linear I-V with slope ≈ 1/R_15. Linearly interpolating R between R_1 = 1 kΩ and R_16 = 8.66 kΩ puts R_15 ≈ 8.15 kΩ, so the slope ≈ 1/8.15 kΩ ≈ **123 μA/V**. Other 15 channels flatline at ≈ 0 (within noise).

**Proves:** Pico firmware accepts `bits[7:4]=14`; MUX silicon routes RE+CE to position 15; Mode A GUI → engine plumbing works end-to-end.

### Run 2 — Mode B regression

| Setting | Value |
|---------|-------|
| Electrode Configuration | **On-board RE/CE (combined)** |
| Channels (WE) | CH1 through CH16 (Select All) |
| Technique | LSV per above |

**Expected:** Only **CH16** shows current. Slope = 1/R_16 = 1/8.66 kΩ ≈ **115 μA/V**. Other 15 channels flatline.

**Proves:** Pico firmware accepts `bits[7:4]=15`; Mode B routes to position 16; Mode B plumbing works.

### Run 3 — Mode C end-to-end + sequential fallback

| Setting | Value |
|---------|-------|
| Electrode Configuration | **Manual (per-WE pairing)** |
| Manual table | Enable CH1–CH14; click **Apply same-position** |
| Technique | LSV per above |

**Expected:** All 14 channels show ohmic lines. Slopes range from ~1 mA/V (CH1, R_1 = 1 kΩ) to ~135 μA/V (CH14, interpolated R_14 ≈ 7.4 kΩ). The plot should match Figure 5 of the PalmSens guide, minus CH15 and CH16.

**Proves:** Mode C end-to-end: GUI 14-row table → per-WE varying RE/CE → sequential fallback script generator → MUX walks paired (WE_N, RE/CE_N) → 14 closed circuits with the expected ohmic slopes. This is the most complex new code path and the one most worth visual confirmation.

## Pass criteria

All three pass conditions must hold. Failure of any one points at a specific code path.

| Run | PASS | Failure interpretation |
|-----|------|------------------------|
| 1 | CH15 ohmic at ~123 μA/V; others < 1 μA over the sweep | No CH15 response: `bits[7:4]=14` rejected by firmware OR Mode A routing constants wrong. Check log for `e!xxxx`. Inspect generated MethodSCRIPT for `set_gpio` with `0x0E0` family addresses. |
| 2 | CH16 ohmic at ~115 μA/V; others < 1 μA | No CH16 response: same diagnosis with `bits[7:4]=15`. |
| 3 | All 14 channels ohmic, slopes monotonically decreasing CH1→CH14 | One channel flatlines: GUI row mapping bug — check `ManualChannelPanel.selected_pairs()` returns the right `(WE, RE/CE)` tuples. All flatline: sequential fallback malformed — check `_sequential_script` in `mux.py` and per-step `set_gpio` emissions. |

Also check the Log tab — any line starting with `e!` is a firmware error and disqualifies the run regardless of plot appearance.

## Going beyond the test cell (optional)

If you want to validate the 16-WE shared-RE+CE workflow that Modes A and B are designed for, before the enclosure is built, the breadboard cheat is ~10 minutes:

- One ~100 kΩ resistor + two jumper wires.
- Wire one end of the resistor to CON4 pin 1 (WE1).
- Wire the other end to a jumpered pair joining CON2 pin 15 + CON3 pin 15 (Mode A RE/CE position).
- Run Mode A with WE = CH1. Expect ~5 μA at 0.5 V (V/R = 0.5/100 kΩ).
- Repeat with WE = CH2, 3, ..., 16 — all should read the same ~5 μA because they all close through that one resistor against the shared RE/CE at position 15.
- Move the jumpers to position 16 to repeat for Mode B.

This is the standard dummy-load check, run at the Mode A/B RE/CE position instead of the historical CH1 default.

## After the smoke passes

The implementation is ready for a full bench-validation pass (1 MΩ resistor → CV slope = 1/R per mode → IDE cycling EIS in Mode C). The Mode C row needs real ferri/ferro electrolyte and electrodes rather than a dummy load.

**Status 2026-07-28.** Modes A and B are **closed as operator-confirmed** — the author has run the 16-WE shared-external-RE/CE workflow in routine research use and confirms both work. That is bench experience rather than a captured dataset, so it should not be cited as a formal validation; the 1 MΩ regression above remains the test if the behaviour is ever questioned. **Mode C (2-electrode IDE cycling EIS) has no validation run on record.**

## Related documentation

- [enclosure_design.md](enclosure_design.md) — production wiring contract for Modes A/B/C inside the enclosure.
- [multiplexer_limitations_and_lessons.md](multiplexer_limitations_and_lessons.md) — operational noise context.
- [Getting Started with the EmStat Pico MUX16](https://assets.palmsens.com/app/uploads/2021/06/Getting-Started-with-the-Emstat-Pico-MUX16.pdf) — PalmSens guide; pages 4–6 describe the test cell and the reference Mux16-mode script.

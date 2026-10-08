# Agent demo — EIS current-range auto-selection

A self-contained segment for the agent video demo: the agent runs an EIS
sweep, **detects that the current range is too small** (the current rails the
pinned range → overload / NaN / negative real-Z), **steps the range up**, and
re-runs until the spectrum is clean — all from one natural-language prompt.

This is the "it adapts on its own" beat. It works because every `run_eis`
result carries a per-channel quality block:

```jsonc
{
  "ok": true, "technique": "eis",
  "overload_points": 14,
  "quality_ok": false,
  "quality": { "1": { "points": 50, "overload_points": 14, "nan_points": 6,
                      "neg_zreal_points": 8, "bad_fraction": 0.4,
                      "verdict": "underranged" } },
  "suggested_cr": "6u",
  "rerange_exhausted": false,
  "quality_note": "Channel(s) ['1'] look under-ranged at 1u ... Re-run those
                   channel(s) at a LARGER current range, e.g. 6u, then continue."
}
```

The agent reads `quality_ok` / `suggested_cr` and decides to re-range. The
verdict is driven by the **device-authoritative** signals — the overload status
bit and NaN readings (the points that railed the pinned range). Scattered
negative-Z′ points are reported (`neg_zreal_points`) but do **not** by themselves
trip a re-range: the project's own bench data attributes those to mains (50/60 Hz)
pickup on correctly-ranged sweeps, and stepping the range up can't fix mains — so
the agent won't chase a phantom range fault on a good electrode. (A wholly
inverted Nyquist arc — a large negative-Z′ fraction — still flags.)

The range ladder (high-speed mode 3) is:

```
100n · 1u · 6u · 13u · 25u · 50u · 100u · 200u · 1m · 5m
```

`suggested_cr` is **one rung up** (the agent may jump several rungs when most
points are bad). When the largest range (`5m`) is still bad, the result sets
**`rerange_exhausted: true`** and the note flips to "it's the cell/wiring, not
the range" — an explicit stop signal so the agent doesn't loop. A too-small
range that makes the device hard-error (`e!xxxx`) instead of returning data also
carries a `suggested_cr` on the error result, so the agent can still step up.

---

## The combined opening prompt (CV + EIS, with EIS range-scoping)

This is the single spoken/typed prompt for the demo. The EIS
range-scoping is **one phase inside it** — the agent scopes the current range on
channel 1, finds the clean range, then sweeps all four channels with it:

> "I just connected a 16-channel device — gold electrodes with CeOx
> electrodeposited. Can you run basic electrochemical characterization on
> channels 1 through 4 — a CV and an EIS — then tell me which channels are
> working? What CV range is appropriate? Also, for EIS let's use a minimum
> frequency of 10 Hz so we can run them more quickly. I'm not sure which current
> range is appropriate for EIS — can you run a couple of scoping experiments on
> **channel 1 first, starting at a low range and stepping it up until the
> spectrum is clean**, then use that range to test all four channels?"

**Expected on-camera tool-chip sequence**

1. `device_status` — confirms connected + idle.
2. `run_cv` (ch 1–4, agent-chosen window/range) → CV overlay streams in (**hero shot**).
3. `export_session` → `analyze_cv` / `analyze_ecsa` → per-channel CV read.
4. **EIS scoping (the auto-range beat, ch 1 only):**
   - `run_eis` (ch 1, `cr=1u`, 100 kHz→10 Hz) → result `quality_ok=false`,
     channel 1 `underranged`, `suggested_cr` set.
   - Agent narrates: *"Channel 1 over-ranged at 1 µA — the current railed the
     pinned range. Stepping it up…"*
   - `run_eis` chip fires again one (or several) rungs larger → repeat until
     `quality_ok=true`. Agent: *"Clean at <N> µA — using that for all four."*
5. `run_eis` (ch 1–4, `cr=<N>`, 100 kHz→10 Hz) → Nyquist/Bode populate.
6. `export_session` → `analyze_eis` → per-channel EIS read.
7. Combined working/dead verdict across CV + EIS (**the reveal**).

The phrase *"scoping experiments on channel 1 first, starting at a low range"*
is what makes the auto-range beat fire deterministically: it forces a small
start range (otherwise the agent may pick the `100u` default, which could be
clean on the first try and skip the beat) and confines the retries to one
channel so the segment stays tight.

## Isolated auto-range prompt (rehearsal / B-roll)

To film or rehearse just the auto-range beat on its own:

> "Run an EIS on channel 1, 100 kHz down to 10 Hz, and **start it at the 1 µA
> current range**. If the data comes back bad, figure out the right current
> range and re-run until it's clean — you can jump up more than one step if it's
> badly over-ranged. Then tell me which range worked."

## Deterministic fallback (if the agent doesn't auto-correct on camera)

> "That EIS over-ranged — the current is railing the range. Re-run channel 1 at
> the suggested larger current range and keep stepping up until it's clean."

---

## Pacing / rehearsal notes

- **Do the silent rehearsal first** (already in the demo checklist) to
  learn the cell's good range. `suggested_cr` steps **one rung at a time**, so
  starting far below the good range means several retries. For a tight cut,
  seed the take's start range **1–2 rungs below** the rehearsal's good range
  (e.g. good=100µ → start 25µ → arc is 25µ→50µ→100µ), or use the "jump up more
  than one step" phrasing so the agent skips ahead.
- Each re-run is a full sweep (the time sink). Plan the edit cut between the
  *bad* run finishing and the *good* run's Nyquist landing — the reveal is the
  clean curve, not the wait.
- The hero line is the agent's own diagnosis ("the current railed the range"),
  not the plot — give that narration room.
- One channel makes the cleanest auto-range beat; save the 4-channel sweep for
  the working/dead verdict section.
- **Make the scope sweeps fast.** Overload happens at the *highest* frequencies
  (largest AC current), so the scoping runs don't need the full low-frequency
  tail — you can tell the agent to "use a quick high-frequency scope (e.g.
  100 kHz→1 kHz, ~15 points)" to find the range in seconds, then run the full
  100 kHz→10 Hz sweep only once, at the discovered range, on all four channels.

## Why a too-small range produces bad data (one-liner for narration)

EIS pins the current range for the whole sweep (in-loop autoranging corrupts
mode-3 EIS on this firmware). If the pinned range is smaller than the actual AC
current — largest at the highest frequencies — those points clip: the device
reports overload, emits NaN, or returns a corrupt negative real-Z. Raising the
range restores headroom.

---

## EIS Rct honesty — "we didn't hit the semicircle peak" (a demo beat)

A great on-camera moment: the agent looks at the EIS data and *refuses* to quote
Rct when the data doesn't support it. `analyze_eis` now checks whether the −Z″
**semicircle apex was actually captured**. The vendored analyzer derives Rct from
the maximum of |Z″|; if that maximum lands on the **lowest measured frequency**
(−Z″ still rising at `freq_end`), the arc never turned over and the "Rct" is just
Z′ at the sweep floor minus Rs — a meaningless number. The guard catches that:

```jsonc
{ "metrics": {
    "rs_ohm": 350,            // still valid (HF intercept)
    "impedance_1khz_ohm": 620, // still valid (a measured point)
    "apex_reached": false,
    "rct_reliable": false,
    "rct_ohm": null,           // suppressed — not quoted
    "rct_lower_bound_ohm": 35260,
    "rct_note": "-Z'' is still rising at the lowest measured frequency (10 Hz)
                 -- the semicircle apex was NOT reached, so Rct cannot be
                 reliably determined ... Lower freq_end (e.g. to 0.1 Hz) ..." } }
```

The agent then says, on camera: *"On these channels the −Z″ is still climbing at
10 Hz — we never hit the semicircle peak, so I can't give you a reliable Rct;
it's at least ~35 kΩ. Rs (~350 Ω) and |Z| at 1 kHz are solid. To actually
measure Rct we'd extend the sweep down to ~0.1 Hz."* That's the honest-instrument
beat: it knows the difference between a measurement and an extrapolation. (Run to
10 Hz for the quick alive/dead pass; only drop `freq_end` when you actually want
Rct — low-frequency EIS is the slow part.)

Verified on the real `testdata.csv`: all four CeOx-on-Au channels flag
`apex_reached: false` with lower bounds 21–36 kΩ — 2–4× the "5–10 kΩ" a naive
read-off reported.

## CV noise scope (bandwidth tuning) — the sibling beat

Same idea as EIS auto-range, for CV ripple. The agent can't *see* the plot (it
only gets metrics), so every `run_cv`/`run_ca` result now carries a per-channel
**`noise`** block — `ripple_ratio` (robust high-frequency ripple ÷ current span)
and `noise_ok`. The agent runs a quick **small-window** CV, reads `ripple_ratio`,
lowers the measurement bandwidth, and re-runs until the ripple drops:

> "Before the full CV, run a quick scoping CV on channel 1 over a small window
> (−0.1 V to +0.1 V) and check the noise. If it's rippling, lower the max
> bandwidth and re-run until it's clean, then use that bandwidth for the full
> scan."

**On-camera beats**

1. `run_cv` (ch 1, ±0.1 V window, `bw_hz=400`) → `noise_ok=false`, elevated
   `ripple_ratio`. Agent: *"The trace is rippling — that's mains pickup at 400 Hz
   bandwidth. Dropping the bandwidth…"*
2. `run_cv` again at `bw_hz=40` → `ripple_ratio` drops, `noise_ok=true`. Agent:
   *"Clean at 40 Hz — using that."*
3. Full CV on ch 1–4 at the clean bandwidth.

The mains ripple is mostly cosmetic, so this beat is optional — but it's a second
"the agent diagnoses and self-corrects" moment if you want it in the cut. The
`ripple_ratio` flag threshold (default 2%) should be calibrated on a clean-vs-
noisy pair from the rig during the rehearsal, exactly like the EIS range.

## Saving / loading presets & sequences (agent-driven)

The agent can persist what it dialed in, using the app's **normal file
dialogs** — it does not pick a path, it asks you. Four tools:

| Tool | What it does |
|------|--------------|
| `save_preset` | One measurement config → opens a native **Save** dialog → `*.mux16` |
| `save_sequence` | A multi-step routine (e.g. CV→EIS) → **Save** dialog → `*.mux16seq` |
| `load_preset` | Opens a native **Open** dialog → returns the config to run |
| `load_sequence` | Opens **Open** dialog → returns the ordered steps to run |

Demo flow — after the characterization the user says *"great work, save this as
a preset so I can use it later."* The agent calls `save_preset` with the exact
settings it used (the auto-ranged EIS current range, the noise-scoped CV
bandwidth); a native **Save As** dialog pops, the operator chooses the location,
done. For the whole CV+EIS routine, the agent uses `save_sequence` instead (one
`*.mux16seq` loadable in the sequencer panel). Later: *"load my CeOx preset"* →
the agent calls `load_preset`, the **Open** dialog appears, the operator finds
the file, and the agent re-runs it.

Notes:
- The agent supplies the **exact good parameters** (it has them in context), and
  every config is validated before writing — a saved preset/sequence is always
  runnable.
- Saving asks for a location every time (no hidden path); the dialog remembers
  the last-used directory within a session.
- Agent-saved presets are **files** you load via `load_preset` (or the GUI's
  preset import) — distinct from the built-in dropdown store.

## Inline figures (CSC and the rest)

The analysis figures — including the **CSC cathodic-area plot** — render **inline
in the app's agent dock**. The pipeline is wired in `main_window` (the analysis
tools are built with the dock's `figure_sink`), so `analyze_cv` / `analyze_eis` /
`analyze_ecsa` figures appear in the chat as they're produced. One condition:

- The **cathodic-area CSC plot** specifically appears only when `analyze_cv` is
  called with **both `scan_rate` and `electrode_area_cm2`** (that's what triggers
  the CSC calc and the shaded-area figure); without the area you get the plain CV
  plot. So give the agent the electrode area, or it will ask.

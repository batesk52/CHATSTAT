# Enclosure Design — RE/CE Channel Assignments and Wiring

Single source of truth for the enclosure's internal wiring. The GUI's three Electrode Configuration modes (A, B, C) map to physical wiring choices documented here. Update this file whenever the enclosure mechanical design changes — the constants `EXTERNAL_RE_CE_CHANNEL = 15`, `ON_BOARD_RE_CE_CHANNEL = 16`, and `MODE_C_MAX_CHANNEL = 14` in `src/data/models.py` must match.

## Channel Assignment

| Position | Role | Physical access |
|----------|------|-----------------|
| CH15 (RE/CE) | Mode A — Separate external Ag/AgCl + Pt | Internally wired; cables exit to bench-side terminals labeled "Ag/AgCl (RE)" and "Pt (CE)" |
| CH16 (RE/CE) | Mode B — On-board combined RE+CE | CON2 pin 16 jumpered to CON3 pin 16 internally; routed to "On-board ref" socket on enclosure face |
| CH1–CH14 (RE/CE) | Mode C — Manual per-WE pairing | CON2 pin N + CON3 pin N exposed via labeled DuPont connector (28 pins: 14 RE + 14 CE) |
| CH1–CH16 (WE) | All modes | CON4 pins 1–16 exposed on the chip-mount face (16 working electrodes) |

CH15 and CH16 reservation applies to **RE/CE pins (CON2 + CON3) only**. The working electrode pins at those positions (CON4 pin 15, CON4 pin 16) remain exposed on the chip-mount face — Modes A and B use all 16 WE channels with RE/CE locked at 15 or 16.

## RE/CE Pin Semantics

Each MUX RE/CE position has TWO pins: CON2 pin N (the RE pin) and CON3 pin N (the CE pin). Whether they are kept separate (true 3-electrode) or shorted externally (combined RE+CE for 2-electrode cycling) is determined by the wiring at that position:

- **CH15 (Mode A):** internal wiring keeps RE and CE separate. External Ag/AgCl and Pt cables exit independently.
- **CH16 (Mode B):** internal wiring shorts CON2 pin 16 to CON3 pin 16 (combined RE+CE node on a single socket).
- **CH1–CH14 (Mode C):** both pins exposed at the DuPont connector. User decides per-experiment.
   - 3-electrode wiring: plug separate Ag/AgCl into RE row N, separate Pt into CE row N.
   - 2-electrode wiring: jumper RE row N to CE row N at the DuPont level; plug the combined node into the jumper.

## Wiring Rule: Do NOT Y-split RE/CE wires

Each MUX RE/CE position should have at most ONE electrode set wired in at a time. Do not Y-split external Ag/AgCl across multiple positions (e.g. plug into both CH3 RE and CH7 RE simultaneously). When the MUX is electronically routed away from your "active" position, the other end of the Y still loads the cable against whatever's at the other position — parasitic capacitance/leakage degrades EIS at best, short-circuits a real voltage source at worst.

The whole point of the multiplexer is electronic switching. Each position stays statically wired; the GUI mode selection routes which one is electrically active per scan.

## Interdigitated Electrode (IDE) Wiring Recipes

These apply to any two-comb IDE chip; the electrical arrangement is what
matters, not the specific part.

### 3-electrode (CV scan-rate work, and a first EIS sweep against a real reference)
- Both combs → CON4 pin N (jumpered together as a single WE)
- External Ag/AgCl wire stays at the Mode A "Ag/AgCl" terminal (internally CH15 RE)
- External Pt wire stays at the Mode A "Pt" terminal (internally CH15 CE)
- GUI: Mode A; WE selection = CH N for the chip in slot N

### 2-electrode IDE cycling (comb-against-comb)
- Comb A → CON4 pin N (this is the WE; current measured here)
- Comb B → DuPont RE row N + DuPont CE row N, externally jumpered at the DuPont
- No external Ag/AgCl or Pt cables connected (Mode A's cables stay plugged but are not electrically active in Mode C)
- GUI: Mode C; enable row N with RE/CE = CH N

For multi-chip 2-electrode in a single sweep, repeat the above for chips in positions 2..14. The "Apply same-position" button in the Mode C panel sets every enabled row's RE/CE to its own WE channel in one click.

## Related Documentation

- [docs/multiplexer_limitations_and_lessons.md](multiplexer_limitations_and_lessons.md) — operational noise context and per-bandwidth recommendations

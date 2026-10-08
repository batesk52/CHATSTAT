# PSTrace `.pssession` method-string key mapping

Verified against native PSTrace-exported `.pssession` files (CA, CV, SWV,
EIS). PSTrace's method parser reads specific `KEY=value` tokens; our
generic uppercased param dump used different names, so PSTrace ignored
them and showed template defaults. `build_method_string` in
`src/data/pssession_exporter.py` maps our param names to these keys.

## `TECHNIQUE=` enum (MethodSCRIPT v1.6 Table 5; PSTrace uses the same)
| Technique | TECHNIQUE= |
|---|---|
| LSV | 0 |
| DPV | 1 |
| SWV | 2 |
| NPV | 3 |
| ACV | 4 |
| CV / FCV | 5 |
| CA | 7 |
| PAD | 8 |
| EIS | 14 *(PSTrace-specific, not in Table 5)* |

The old code had lsv=2, dpv=3, swv=4 — wrong (shifted), so PSTrace
mislabeled the technique.

## Parameter key mapping (our param → PSTrace key)
Common to all techniques: `t_eq → T_EQUIL`.

| Technique | Mapping |
|---|---|
| CA / ca_alt_mux | `e_dc → E` |
| CV / FCV | `e_vertex1 → E_VTX1`, `e_vertex2 → E_VTX2` |
| SWV / ACV | `amplitude → E_AMP`, `frequency → FREQ` |
| EIS | `freq_start → MAX_FREQ`, `freq_end → MIN_FREQ`, `e_dc → E`, `e_ac → AMPLITUDE` |

Already matching (no override): `e_begin → E_BEGIN`, `e_end → E_END`,
`e_step → E_STEP`, `scan_rate → SCAN_RATE`, `n_scans → N_SCANS`,
`n_freq → N_FREQ`, `t_run → T_RUN`, `t_interval → T_INTERVAL`.

## Not yet verified (no reference file)
- **DPV** pulse keys (`e_pulse`, `t_pulse`) — couldn't be confirmed
  without a native PSTrace DPV file, so DPV has been **deactivated**
  (removed from `_VERIFIED_TECHNIQUES`, hidden from the GUI). Re-add it
  there + verify the pulse keys once a reference is available.
- **GEIS** `i_dc`/`i_ac` — only the frequency keys are mapped.

## Provenance: how the CA mapping was found

The `e_dc → E` mapping came first, from decoding a genuine PSTrace-native
amperometry `.pssession` (device PS4A22Z003341, PSTrace 5.12, fw 1.7). The DC potential is emitted as **`E=`** inside the
`#Time method parameters` block, NOT `E_DC=` — which is why PSTrace had been
showing its own 0.5 V default for our exports rather than the requested
potential. `T_RUN` / `T_INTERVAL` were already correct; equilibration is
`T_EQUIL`, not `T_EQ`. Amperometry is `METHOD_ID=ad`, `TECHNIQUE=7`.

That finding generalized into the per-technique table above when CV/SWV/EIS
were reverse-engineered.

## Gotchas when reading PSTrace files
- Some PSTrace versions serialize top-level JSON keys **lowercase**
  (`methodformeasurement`, `measurements`) — read case-insensitively.
- The **authoritative** method is `Measurements[0].Method`, not the
  top-level `MethodForMeasurement` (which can be a stale/default method
  from a prior technique in the same session).

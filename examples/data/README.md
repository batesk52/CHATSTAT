# Example data

These folders hold the example recordings described in the accompanying paper. An earlier version of ChatStat exported them, and the CSV and `.pssession` files are byte-identical to what it wrote.

All runs used the multiplexed chronoamperometry, CA (MUX-Alternating), on an EmStat Pico module (firmware 1.6, espico1601) on the EmStat Pico MUX16 board. All were at +0.7 V with the 2 uA current range on all sixteen channels. Each folder holds one CSV file per channel (`ch01.csv` to `ch16.csv`) and one session file (`ca_alt_mux.pssession`), and each CSV header records the module's serial number. The `t_run` and `_n_rounds` entries in each header are the planned run length; every run here was stopped before that.

## ca_mux16_h2o2_step_2026-05-18

Sixteen platinum-black working electrodes on one chip, recorded on 2026-05-18 in stirred phosphate-buffered saline at 37 C, with one addition of hydrogen peroxide to 19.6 uM part-way through.

- Settings: 0.4 Hz bandwidth, 200 ms settle, eleven samples at 10 ms intervals averaged per visit, and a requested 10 s round.
- The measured round took 15.4 s. The run was stopped after 199 of the 500 planned rounds (about 51 min).
- A commercial Ag/AgCl reference and a platinum counter electrode were immersed in the same bath. This export predates the wiring lines in the CSV header, so the files do not record the reference/counter wiring.
- The application writes no event marker, so the time of the addition is not in the files. The paper places it at 2810 s, the median of the per-channel step times detected in these files.

## bandwidth_sweep_2026-05-11

Three runs made one after another in one session on 2026-05-11, in 1 mM potassium ferricyanide in 0.1 M KCl, with a 50 ms settle and 2 s rounds requested:

| Folder | Bandwidth | Rounds | Measured round |
|---|---|---|---|
| `4Hz` | 4 Hz | 172 | 2.08 s |
| `0p4Hz` | 0.4 Hz | 50 | 2.56 s |
| `40Hz` | 40 Hz | 16 | 2.01 s |

No 400 Hz run was made. The 40 Hz run was stopped after sixteen rounds with one channel above the 2 uA range in every round. The electrodes' drift dominated the raw signal, so the paper compares noise after detrending: for each channel, the standard deviation of the residuals of a line fitted after dropping the first 20 % of the run. The median was 5.4 nA at 0.4 Hz against 88 nA at 4 Hz. The runs were not a controlled comparison (see the bandwidth note in the main README).

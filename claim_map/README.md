# Paper claim map

Machine-readable provenance for every numeric claim in the paper.

## `claim_provenance.csv`

Columns:

| column | meaning |
|---|---|
| `paper_location` | Section / figure / table locator |
| `claim` | Human-readable name |
| `value` | Exact value as it appears in the paper |
| `tolerance` | Absolute tolerance for numeric comparison (0 = exact match, used for integer counts; 0.01 = ±0.01 absolute for percentages and pp deltas) |
| `source` | Relative path to the CSV + locator within it (`file.csv:row_key:column`) |
| `status` | `VERIFIED`, `TODO_CAMERA_READY`, or `NOTE` |

## Status semantics

- **VERIFIED** — `verify_numbers.py` checks the claim against the
  `source` cell and the check passes within the stated tolerance.
- **TODO_CAMERA_READY** — a discrepancy or open editorial decision
  that must be resolved before camera-ready. Currently one entry:
  Table III UTRef class regressions (raw = 8 source-level flags,
  runtime = 0; paper uses 0 under runtime semantics — see detailed
  note in `03_baselines_3way/aggregate/class_regressions_detail.csv`
  and `utref_compile_errors.csv`). A suggested one-sentence caption
  addition is provided in
  `03_baselines_3way/aggregate/utref_compile_errors.csv`.
- **NOTE** — descriptive pointer, not a check (none currently).

## Adding a new claim

If the paper gains a new headline number at camera-ready:

1. Add a row to `claim_provenance.csv`.
2. If the value is not already in an aggregate CSV, add it there and
   update `_build/build_aggregates.py` to regenerate it deterministically.
3. Teach `00_code/scripts/verify_numbers.py` to read it and compare
   against the `value`/`tolerance` cells.

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
- **NOT_MEASURABLE** — the quantity could not be observed for that
  condition, so the paper prints `—` rather than a number.
  UTRefactor emits both a JUnit 4 and a JUnit 5 `Test` import, which
  `javac` rejects as ambiguous; 12 of 12 completed projects therefore
  fail to compile and 0 tests ever run
  (`03_baselines_3way/aggregate/utref_compile_errors.csv`:
  `compiled=0`, `pit_success=0/15`). Class regressions, coverage delta
  and mutation score are all unmeasurable for UTRefactor as a result.
  The 8 source-level flags in the raw `per_project.json` (6 in
  `7_sfmis`, 2 in `41_follow`) are pre-existing failures in the
  unrepaired workdir, not repair-induced regressions.
  `verify_numbers.py` reports these two claims as SKIP.
  Also used for ARPM in Figure 3, which is 0 before and 0 after on
  every project, so no percentage is defined.
- **TODO_CAMERA_READY** — none remaining.
- **NOTE** — descriptive pointer, not a check (none currently).

## Adding a new claim

If the paper gains a new headline number at camera-ready:

1. Add a row to `claim_provenance.csv`.
2. If the value is not already in an aggregate CSV, add it there and
   update `_build/build_aggregates.py` to regenerate it deterministically.
3. Teach `00_code/scripts/verify_numbers.py` to read it and compare
   against the `value`/`tolerance` cells.

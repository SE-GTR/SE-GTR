# Naive LLM baseline (re-implementation)

This is our in-house re-implementation of the "naive LLM" baseline
referenced in the paper's RQ3 comparison (Phase B, §6).

## What it does

For each EvoSuite-generated test method:

1. Read the entire method body plus the enclosing class's field/method
   signatures as context.
2. Send a single prompt asking the LLM to "rewrite this test to remove
   the following smell: `<smell_name>`", with no structured operator
   catalogue, no tier routing, and no smell-specific templates.
3. Parse the returned Java source; compile-check it; run-check it.
4. If both checks pass, accept the rewrite in place of the original
   method. Otherwise reject (no retry beyond `max_llm_attempts`).

## What it does NOT do

- No atomic-operator decomposition
- No tier routing (every smell is treated the same)
- No plan-group grouping (each method is rewritten independently)
- No evidence-guided smell-substitution check (gate 6)
- No assert-loss check (gate 7)
- No dynamic-capture remediation (tier 4)

## Relationship to SE-GTR

The naive baseline is a strict subset of SE-GTR Full: if you disable
all tier routing, all templates, and all gates except compile and
test-pass, you get the naive baseline. See
`../../configs/naive_llm.yaml` for the exact setting.

## Where the results live

- `03_baselines_3way/naive_llm/per_project/<N>_<project>/` — per-project
  artefacts (per_project.json, smelly_before/after, jacoco_before/after,
  evosuite-tests/, plan_log.jsonl, pit_before/after scores, summary.json)
- `03_baselines_3way/aggregate/baseline_smell_comparison.csv` — Figure 4
- `03_baselines_3way/aggregate/baseline_preservation.csv` — Table III
- `05_phase_e_pit/naive_llm/per_project/<N>/score.json` — 3-way PIT

## Reproducing

```sh
bash 00_code/scripts/run_phaseB_naive.sh
```

Runs the baseline on the 15-project cohort in `01_cohort/selected_15.csv`.
Expected wall-clock: ~6 hours on 1 modest CPU + 1 LLM endpoint.

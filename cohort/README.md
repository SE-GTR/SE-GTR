# Cohorts

Three cohort definitions are used in the paper:

## 1. Phase-4 "held-out 81" — RQ1 and RQ2 (§5)

- File: `heldout_81.txt` (one project per line, SF110 ID)
- Size: 81 projects
- Purpose: headline per-smell reduction numbers (Figure 3), coverage
  preservation (n=79 with measurable JaCoCo), mutation preservation
  (n=58 with valid PIT on both pristine + repaired runs).

The held-out 81 is derived from the 94 SF110 pristine projects attempted
in Phase 4 by:
1. Excluding 8 that timed out at the 90-minute or 180-minute wall-clock
   caps: `13_jdbacl, 18_jsecurity, 21_geo-google, 35_corina,
   81_javathena, 86_at-robots2-j, 92_jcvi-javacommon, 101_netweaver`.
2. Excluding 5 held out as the **dev cohort** used during handler
   tuning: `1_tullibee, 29_apbsmem, 31_xisemele, 71_ext4j,
   88_jopenchart`.

## 2. Phase-4 dev 5 — handler tuning

These 5 projects were used during development of the Tier-3 evidence
templates and Tier-4 dynamic-capture heuristics. They are not included
in the Figure-3 / §5 aggregates. A parallel 86-project
"all-completed" aggregate is available at
`02_phase4_segtr_full/aggregate/smell_reduction_86_all_completed.csv`.

## 3. RQ3 "selected 15" — baseline comparison and ablation (§6)

- File: `selected_15.csv` (full stratification metadata)
- Companion: `pool_32_healthy.csv` (parent pool of 32 from which 15 were
  drawn) and `selection_log.md` (how 15 were stratified).
- Stratification: 5 small / 5 medium / 5 large by `tests_total`.
- Purpose: Figure 4 (3-way smell improvement), Table III
  (preservation), Figure 5 (ablation), Table IV (per-smell per-tier).

The 15 projects are a subset of the held-out 81, so the Full-baseline
comparison in Figure 4 and Table III re-uses the Phase-4 Full results.
A symlink tree at
`03_baselines_3way/segtr_full_15proj_subset/` points to those same
15 projects inside `02_phase4_segtr_full/per_project/`.

## Identity and seed

- SF110 IDs are upstream from Fraser & Arcuri's benchmark; IDs 1–117
  map 1:1 to the SF110 project numbering. Never re-numbered.
- The draw for `selected_15.csv` is stratified into `tests_total` bins and
  then sampled with a fixed seed: `random.Random(42).sample(..., 5)` per bin,
  over a bin order sorted by numeric project id so the draw is
  platform-independent (`phaseB_select_15.py`, `SEED = 42`; see
  `selection_log.md` §2.2). The selection is therefore deterministic given
  the pool content plus the seed.
- EvoSuite test generation used seed `1`; this is in the project-level
  `evosuite-files/evosuite.properties` for each project.

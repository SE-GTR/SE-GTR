# Phase B — RQ3 cohort selection log

Fifteen SF110 projects, stratified by test-suite size, drawn from the
Phase-4 healthy cohort used for RQ1/RQ2 apples-to-apples mutation
measurement. This cohort is shared across all RQ3 conditions (SE-GTR
Full, Naive LLM, UTRefactor, Tier-ablations) so that each approach is
evaluated on the same test inventories.

All numbers below are derived from the existing Phase-4 artefacts — no
extra runs were required for selection itself.

---

## 1. Pool definition

```
Pool =  Phase-4 pipeline-completed projects   (n = 86)
     ∩  Phase-4.5 apples-to-apples PIT cohort (both pristine PIT and
        v2-after PIT produced valid mutations.xml; n = 58)
     ∩  Phase-4.5 v1-PIT category == "healthy" (n = 36)
     –  dev set used for handler tuning:
        {1_tullibee, 29_apbsmem, 31_xisemele, 71_ext4j, 88_jopenchart}  (5 removed)
```

Result: **n = 32 held-out healthy projects**, listed in
`pool_32_healthy.csv`. Each row carries `tests_total`,
`line_coverage_pristine`, `branch_coverage_pristine`,
`instruction_coverage_pristine`, `mutation_score_pristine_pct`,
`pristine_mutants`, `pristine_killed`, `v2_after_score_pct`,
`category_v1`, and a free-text `phase4_coverage_outlier_note`.

### 1.1 Why this pool shape

- **Phase-4 pipeline-completed** guarantees every project has a build
  that reaches the 90-minute cap of the SE-GTR Full run without
  infrastructure failure. RQ3 alternatives that hit the same cap on
  these projects can therefore be attributed to the alternative, not to
  the benchmark plumbing.
- **PIT-58 apples-to-apples cohort** guarantees that, when RQ3 conditions
  are eventually re-measured with PIT, a pristine-vs-repaired comparison
  under the same mutator set (PIT 1.17.4) is feasible. The 10 projects
  whose PIT already fails on pristine or on v2-after workdirs are
  excluded up-front so no RQ3 condition inherits an un-measurable
  baseline.
- **`category == healthy`** (v1 PIT labels) concentrates the cohort on
  the projects for which pre-existing tests already have meaningful
  oracle strength (pristine mutation score on the order of 15–40 %).
  The weak-oracle and low-coverage subgroups remain available for
  follow-up analyses but are excluded from the core RQ3 measurement to
  avoid confounding repair-quality signals with pre-existing oracle
  weakness.
- **Dev set removed** because Tier 1–4 handler thresholds, prompt
  templates, and dynamic-capture heuristics were iterated against these
  five projects; keeping them in RQ3 would inflate SE-GTR numbers relative
  to the naive baseline and would not let us claim held-out evaluation.

### 1.2 Which projects are excluded and why

Starting from the 94 SF110 projects we exclude the following *before*
applying the healthy-category filter:

| Bucket | Count | Rationale |
|---|---:|---|
| Phase-4 pipeline timeout (90 min) | 3 | `13_jdbacl`, `18_jsecurity`, `21_geo-google` — build + EvoSuite class load exceeds the time budget regardless of the RQ3 condition |
| Phase-4 pipeline timeout (180-min retry) | 5 | `35_corina`, `81_javathena`, `86_at-robots2-j`, `92_jcvi-javacommon`, `101_netweaver` — still exceed the retry cap |
| Dev set (handler tuning projects) | 5 | `1_tullibee`, `29_apbsmem`, `31_xisemele`, `71_ext4j`, `88_jopenchart` |
| v1-PIT ineligible (Phase 4.5 exclusions) | 12 | `19_jmca`, `22_byuic`, `28_greencow`, `36_schemaspy`, `51_jiprof`, `56_jhandballmoves`, `72_battlecry`, `93_quickserver`, `94_jclo`, and the PIT-ineligible subset of the above |
| PIT cohort (pristine or v2-after failed) | 10 | projects the Phase-4.5 pristine-v2 PIT or v2-after PIT could not complete |
| Non-healthy v1 category | 22 | low-coverage (n=15) and weak-oracle (n=7) subgroups, reserved for follow-up analysis |

The counts above overlap slightly because some projects fall into more
than one bucket (e.g. `93_quickserver` is both PIT-ineligible and falls
outside the healthy category). Net effect is a 32-project held-out
healthy pool.

### 1.3 Phase 4.3 coverage outlier annotation

Two projects in the pool (`3_gaj`, `4_rif`) carried an initial Phase-4.3
coverage-regression flag (Δline=-5.81 pp and -14.98 pp respectively).
Both were traced to a JaCoCo drain-partial write (snapshot taken
mid-flush by the parallel orchestrator) rather than a real repair
regression. After re-verification the actual deltas are Δline=+0.07 pp
and +0.05 pp; the Phase-4.3 aggregation rows were replaced accordingly.
These annotations are preserved in `pool_32_healthy.csv` and
`selected_15.csv` under `phase4_coverage_outlier_note` so that any RQ3
table referring to them carries the context.

Both projects are retained in the pool and remain eligible for random
sampling — the outliers are instrumentation artefacts, not behavioural
signals, and excluding them would weaken the small-bin stratum without
serving a scientific purpose.

---

## 2. Stratification

### 2.1 Bin definition (data-driven tertile of the 32-project pool)

Test-method totals come from the `jacoco_before.tests_total` field of
the Phase-4 per-project JSON (i.e. `junit` `@Test` methods after
`prepare_workdir` + `ant compile compile-evosuite`).

Distribution across the 32 held-out healthy projects:

```
  min = 15    (53_shp2kml)
  p33 ≈ 51    (bin boundary small / medium)
  p67 ≈ 181   (bin boundary medium / large)
  max = 843   (54_db-everywhere)
```

Bin cut-points were set to simple round numbers just inside those
percentile boundaries so the partition is easy to reproduce:

```
  small  : tests_total <   60       n = 12
  medium : 60  ≤ tests <  220       n = 13
  large  : tests_total ≥  220       n =  7
```

(Dollar counts are the actual partition sizes — the heuristic target of
"11 / 11 / 10" cited in the planning memo was approximate.)

### 2.2 Random sampling

- `random.seed(42)` once, globally.
- For each bin in the fixed order `[small, medium, large]`, sort the
  bin's projects by numeric project id to give the RNG a platform-
  independent draw order, then call `rng.sample(..., 5)`.
- The resulting selection is deterministic given the pool content plus
  the seed.

### 2.3 Selected cohort

| bin | n | projects |
|---|---:|---|
| small | 5 | `3_gaj`, `4_rif`, `11_imsmart`, `14_omjstate`, `90_dcparseargs` |
| medium | 5 | `7_sfmis`, `8_gfarcegestionfa`, `12_dsachat`, `42_asphodel`, `60_sugar` |
| large | 5 | `2_a4j`, `41_follow`, `54_db-everywhere`, `63_objectexplorer`, `68_biblestudy` |

Full per-project descriptors, including Phase-4-Full baseline metrics
(plans submitted / accepted, LLM call count, cost, smell-reduction %,
JaCoCo line coverage before / after), live in `selected_15.csv`.

Summary of Phase-4 Full on this 15-project cohort (carried for
reference; not a claim — full aggregation happens in Phase C–E):

```
  mean tests_total                  : 210  (median 154)
  mean line_coverage_pristine       : 0.312
  mean mutation_score_pristine      : 26.4 %
  mean full_smell_reduction_pct     : 14.4 %
  mean full_plans_accept_rate       : 54.6 %
  total full_cost_usd (15 projects) : $0.92
```

`11_imsmart` is the one cohort project for which Phase-4 Full showed a
*negative* smell reduction (−7.1 %, +2 net smells); this is preserved in
the cohort rather than replaced because it represents the small-project
worst case the RQ3 baselines must also handle.

---

## 3. Files

| File | Contents |
|---|---|
| `pool_32_healthy.csv` | All 32 held-out healthy projects; 11 columns per row |
| `selected_15.csv` | The 15 sampled projects + Phase-4 Full baseline metrics |
| `selection_log.md` | This document |

---

## 4. Reproducibility

```bash
cd llm_smelly_repair_impl
python3 scripts/phaseB_build_pool.py     # -> pool_32_healthy.csv
python3 scripts/phaseB_select_15.py      # -> selected_15.csv (seed=42)
```

Any change to the pool definition, the bin boundaries, or the random
seed requires updating the script constants (documented as module-level
globals) and re-running both scripts.

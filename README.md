# SE-GTR

**SE-GTR: Smell Evidence-Guided Repair of Automatically Generated Unit Tests**

Seungho Kim (ohgnues@hanyang.ac.kr) · Scott Uk-Jin Lee (scottlee@hanyang.ac.kr)
Department of Computer Science and Engineering, Hanyang University, Ansan, Republic of Korea

Accepted at the IEEE International Symposium on Software Reliability
Engineering (ISSRE) 2026.

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19743168.svg)](https://doi.org/10.5281/zenodo.19743168)

---

## 1. Scope

**This repository is code-only.** It contains the SE-GTR pipeline
implementation, the experiment configurations and drivers, the cohort
definitions, and the environment specification.

All *data* — per-project run artefacts, repaired test suites, JaCoCo
coverage reports, PIT mutation scores, Smelly-E warning files, and the
aggregate CSVs behind every figure and table — lives in the companion
Zenodo archive (a single `replication_package.zip`, ≈109 MB):

**https://doi.org/10.5281/zenodo.19743168** — concept DOI; always
resolves to the latest version.

> **Path convention.** Throughout this repository's documentation, any path
> beginning `00_code/`, `01_cohort/`, `02_phase4_segtr_full/`,
> `03_baselines_3way/`, `04_ablation/`, `05_phase_e_pit/`,
> `06_paper_claim_map/`, `07_environment/`, or naming `CHANGELOG_v2.md`
> refers to the **extracted Zenodo archive**, not to this repository. Several
> documentation files are kept byte-identical between the two distributions,
> so they use the archive's layout.

### Layout

| Path | Contents |
|---|---|
| `src/segtr/` | the pipeline package (imported as `smell_repair_v2`; see §6) |
| `src/operators/`, `src/validator/`, `src/prompts/` | duplicate convenience copies of files already under `src/segtr/`, mirroring the Zenodo archive's `00_code/segtr/{operators,validator,prompts}/` layout |
| `src/baselines/naive_llm/` | description of the in-house Naive LLM baseline |
| `configs/` | SE-GTR Full, Naive LLM, and three tier-ablation configurations |
| `scripts/` | Python phase drivers, aggregation utilities, `verify_numbers.py`, and shell wrappers (see §6) |
| `cohort/` | the 81-project held-out set, the 15-project RQ3 cohort, and the 32-project pool it was drawn from |
| `claim_map/` | machine-readable map from each numeric claim in the paper to the artefact that produced it |
| `environment/` | Dockerfile, conda environment, tool version pins, LLM configuration notes |

## 2. What is reproducible from this repository alone

| Task | This repo alone | Also required |
|---|:---:|---|
| Read the pipeline, operators, and 7-gate validator | ✅ | — |
| Read the cohort definitions and the claim map | ✅ | — |
| Run the bundled unit tests | ⚠️ | package must be installed first (§6); 4 of 312 tests fail — see §7 |
| Verify the paper's numbers (`verify_numbers.py`) | ❌ | the Zenodo archive, passed via `--package-root` |
| Re-run the repair pipeline | ❌ | SF110 corpus · EvoSuite 1.2.0 generated tests · Smelly-E shaded jar · JUnit 4.11 + Hamcrest · an OpenAI-compatible LLM endpoint · local edits to the path constants (§7) |
| Inspect JaCoCo / PIT outputs | ❌ | the Zenodo archive |
| Re-run the UTRefactor baseline | ❌ | an upstream UTRefactor checkout |

## 3. Environment

Values below are taken from the code and the pinned version files in this
repository, not from the paper text.

| Component | Value | Source |
|---|---|---|
| Python | 3.11 (package metadata allows ≥3.10) | `environment/environment.yml`, `pyproject.toml` |
| Java | 8 (JDK 1.8) | `environment/Dockerfile` |
| Ant | 1.10+ | `environment/ant_build_requirements.md` |
| EvoSuite | 1.2.0 | `environment/evosuite_version.txt` |
| JUnit | 4.11 + hamcrest-core 1.3 | `environment/ant_build_requirements.md` |
| JaCoCo | 0.8.14 | `environment/jacoco_version.txt` |
| PIT | 1.17.4 | `environment/pit_version.txt` |
| LLM | `openai/gpt-oss-20b`, served over OpenRouter | `src/segtr/config/llm_config.yaml` |

### Decoding settings (effective values)

These come from `src/segtr/config/llm_config.yaml` by way of
`config/loader.py` and `llm/multi_client.py`, which is the configuration the
pipeline actually reads:

| Parameter | Value | Note |
|---|---|---|
| temperature | 0.2 | flat; there is no per-tier schedule |
| top_p | 0.9 | hard-coded in `llm/multi_client.py`; not configurable |
| max_output_tokens | 2000 | |
| request_timeout_sec | 120 | |
| attempts per plan | 3 | `cli_v2 --max-attempts`, default 3 |

Tier 1 is LLM-free, so only Tiers 2–4 issue requests. See §7 for the
`llm:` blocks in `configs/*.yaml`, which are **not** read at runtime.

### Budgets and wall-clock caps

| Setting | Value | Source |
|---|---|---|
| SE-GTR per-project wall clock | 90 min, extended to 180 min for stragglers | `scripts/run_phase4_parallel.py`, `src/segtr/config/main_experiment.yaml` |
| RQ3 baseline / ablation wall clock | 60 min | `scripts/run_rq3_parallel.py`, `scripts/phaseC_run_utrefactor_parallel.py` |
| PIT wall clock | 30 min per project | `scripts/run_phase4_pit.py`, `scripts/phaseE_run_pit.py` |
| Cost cap | overridable via `SE_GTR_COST_BUDGET_USD` | `scripts/run_phase4_parallel.py` |

### Seeds

There is **no global pipeline seed**, and LLM sampling is not seeded.

- `seed = 42` applies to **one thing only**: the RQ3 15-project cohort draw
  (`scripts/phaseB_select_15.py`, `random.Random(42).sample(...)` per
  `tests_total` bin).
- EvoSuite test generation used `seed = 1`, recorded per project in
  `evosuite-files/evosuite.properties` (not bundled here).

## 4. Architecture

```
Smelly-E warnings
      │
      ▼   tier routing (src/segtr/tiers/router.py)
  Tier 1  deterministic operators, LLM-free   NNA · DS · AC · TSES
  Tier 2  template-guided LLM plans           ARPM · NARV · NASE · TSVM · OIMT · ENET
  Tier 3  evidence-guided LLM plans           AOIMT · EDED · EDIS · TOFA
  Tier 4  dynamic-capture LLM plans           (fallback for unrouted smells)
      │
      ▼   7-gate validator, applied per plan group
  G1 banned import    G2 brace/paren balance   G3 ant compile
  G4 JUnitCore run    G5 coverage proxy        G6 smell substitution
  G7 assertion preservation
      │
      ▼   after the per-class loop
  project-level `ant clean compile compile-evosuite`, then a class-pass
  re-measurement that REPORTS regressed classes (see §7)
```

Gate 5 is enforced by a regex proxy (`_gate5_coverage_proxy`): no test method
may be lost, no test body may become empty, and the executable-statement
count may not drop by more than 30%.

## 5. Verifying the paper's numbers

Download and extract the Zenodo archive, then:

```bash
python3 scripts/verify_numbers.py --package-root /path/to/extracted/replication_package/
```

`--package-root` is required from this repository: the script's default root
assumes the archive's directory layout, not this one.

### Claim → artefact pointers

Paths are relative to the extracted Zenodo archive.

| Paper location | Artefact |
|---|---|
| Figure 3 — per-smell reduction (n = 79) | `02_phase4_segtr_full/aggregate/smell_reduction_79.csv` |
| Figure 3 — earlier n = 81 aggregate (legacy, superseded) | `02_phase4_segtr_full/aggregate/smell_reduction_81.csv` |
| §5 RQ2 — coverage preservation (n = 79) | `02_phase4_segtr_full/aggregate/preservation_79coverage.csv` |
| §5 RQ2 — mutation preservation (reported value: eval-only, n = 54) | `02_phase4_segtr_full/aggregate/preservation_54mutation_evalonly.csv` |
| §5 RQ2 — mutation, full n = 58 aggregate | `02_phase4_segtr_full/aggregate/preservation_58mutation.csv` |
| §5 RQ2 — mutation by band | `02_phase4_segtr_full/aggregate/mutation_by_band.csv` |
| Table III — 3-way preservation | `03_baselines_3way/aggregate/baseline_preservation.csv`, `class_regressions_detail.csv`, `utref_compile_errors.csv` |
| Table III — mutation Δ | `05_phase_e_pit/aggregate/pit_3way.csv` |
| Figure 4 — 3-way smell improvement | `03_baselines_3way/aggregate/baseline_smell_comparison.csv` |
| Figure 5 — tier ablation | `04_ablation/aggregate/tier_cumulative.csv`, `regressions_by_condition.csv` |
| Table IV — per-smell per-tier | `04_ablation/aggregate/per_smell_by_tier.csv` |

The UTRefactor coverage and class-regression cells in Table III are reported
as not measurable rather than as zero, because the baseline's output does not
compile in this environment (§7).

The full machine-readable map is
[`claim_map/claim_provenance.csv`](claim_map/claim_provenance.csv).

## 6. Running the pipeline

### Install

The sources import the package as `smell_repair_v2` (the development tree's
name) while this repository ships it under `src/segtr/`. `pyproject.toml`
maps the two, so an editable install makes the package importable without
moving or renaming any source file:

```bash
pip install -e .
python -c "import smell_repair_v2.cli_v2"
```

### Configure the endpoint

```bash
export OPENROUTER_API_KEY=...     # the ONLY key environment variable the code reads
```

> **Never put a real key in `src/segtr/config/llm_config.yaml`.** That file is
> tracked in git and ships with `api_key: "${LLM_API_KEY}"`, which is a
> literal string, not an environment reference — the pipeline only reads
> `OPENROUTER_API_KEY` from the environment.

### Per-project driver

```bash
python -m smell_repair_v2.cli_v2 \
    --config src/segtr/config/llm_config.yaml \
    --projects <N_projectname> \
    --out <output_dir> \
    --enable-tier1 --enable-tier2 --enable-tier3 --enable-tier4 \
    --enable-project-jacoco \
    --model-key gpt_oss_20b \
    --max-attempts 3
```

RQ3 and ablation conditions are selected with `--condition`
(`full`, `naive_llm`, `utrefactor`, `t1_only`, `t1_t2`, `t1_t2_t3`), which
overrides the individual `--enable-tier*` flags.

### Fleet drivers

```bash
python3 scripts/run_phase4_parallel.py \
    --config src/segtr/config/main_experiment.yaml \
    --llm-config src/segtr/config/llm_config.yaml \
    --project-csv <classification.csv> --out <output_dir>

python3 scripts/run_rq3_parallel.py --condition <cond> \
    --projects-csv cohort/selected_15.csv --out <output_dir>
```

Note that `run_phase4_parallel.py` takes **two** config flags: `--config` is
the experiment YAML (parallelism, budgets, tier flags) and `--llm-config` is
the file that supplies the model and decoding settings.

The shell wrappers in `scripts/*.sh` are **not** usable as entry points; see
§7.

## 7. Known limitations and reproducibility gaps

This section is deliberately complete. Every item below was verified against
the code in this repository.

1. **Run logs do not record model identity.** No record written by
   `pipeline_v2.py` — `llm_result`, `tier4_result`, `naive_result`,
   validator records, or `per_project.json` — carries a model field, even
   though the model key is in scope. The model therefore cannot be
   established from the logs after the fact. It is established instead from
   the code defaults: `cli_v2 --model-key` defaults to `gpt_oss_20b`, which
   `src/segtr/config/llm_config.yaml` maps to `openai/gpt-oss-20b`. No
   stamping was added for this release.

2. **Only Tier 1 is deterministic.** Tier 1 issues no LLM request. Tiers 2–4
   run at `temperature = 0.2` and are therefore non-deterministic; repeated
   runs will not reproduce identical repairs.

3. **Regression handling detects and reports; it does not prevent or roll
   back.** After the per-class loop, the pipeline runs one project-level
   `ant clean compile compile-evosuite`, re-measures which test classes pass,
   and records the regressed classes in `per_project.json`. Accepted edits
   are not reverted at that point. The only rollback in the system is
   per-plan: a plan rejected by a gate restores the original method text.

4. **The UTRefactor baseline could not be measured for coverage, class
   regressions, or mutation.** Its rewrites mix JUnit 4 and JUnit 5 imports
   (`org.junit.jupiter.api.Test` alongside `org.junit.Test`, plus
   `assertDoesNotThrow` and the JUnit-5 form of `assertThrows`), which
   `javac` rejects as ambiguous on the SF110 JUnit 4.11 classpath. No
   UTRefactor-rewritten class file was produced for any completed project,
   and PIT produced no successful run. This is a tool-compatibility finding;
   the affected Table III cells are reported as not measurable.

5. **The decoding values are undocumented choices.** Nothing in the code,
   comments, or configuration records why `temperature = 0.2`,
   `max_output_tokens = 2000`, and `request_timeout_sec = 120` were selected.

6. **The published tree needs `pip install -e .` to be importable.** 50 files
   import `smell_repair_v2.*`, but the package ships as `src/segtr/`. The
   mapping in `pyproject.toml` was added for this release and was verified by
   installing into a clean virtual environment and importing
   `smell_repair_v2.cli_v2` and `smell_repair_v2.operators.validator`. The
   Zenodo archive has the same layout and the same requirement. The
   *legacy v1* modules (`src/segtr/cli.py`, `src/segtr/pipeline.py`,
   `scripts/baseline_classify.py`) import a bare `smell_repair` package that
   is not mapped and remains unimportable; the paper's results come from the
   v2 path (`cli_v2` / `pipeline_v2`).

7. **Anonymised absolute paths remain.** 51 lines across 29 files hold
   module-level constants of the form
   `Path('<ANON_ROOT>/segtr_replication/...')`, written during double-blind
   anonymisation. They were not restored, because the originals are the
   authors' personal home paths. These constants must be edited locally
   before any script under `scripts/` will run.

8. **`LLM_API_KEY` and `LLM_BASE_URL` are inert.** They appear in the
   Dockerfile and in shell-wrapper comments, but no code reads them, and the
   `"${LLM_API_KEY}"` string in the YAML files is never expanded. The only
   key variable the code reads is `OPENROUTER_API_KEY`.

9. **The shell wrappers do not run.** All five wrappers in `scripts/*.sh`
   pass flags their Python drivers do not accept — `--projects_file`,
   `--cohort`, `--utrefactor_root`, `--full_root`, `--naive_root`,
   `--utref_root` — so each exits on an argument-parsing error. They also
   assume the Zenodo archive's directory layout (`00_code/`, `01_cohort/`).
   They are retained unchanged as a record of the intended invocation; the
   Python drivers in §6 are the real entry points.

10. **Dependency manifests are inaccurate in both directions.** The code
    imports `requests`, `PyYAML`, `matplotlib`, and `psutil`.
    `requirements.txt` and `pyproject.toml` list only the first two;
    `environment/environment.yml` additionally lists `openai`, `pydantic`,
    `javalang`, `lxml`, `tqdm`, `numpy`, and `pandas`, none of which is
    imported anywhere.

11. **Smelly-E has no distribution pointer.** The detector is required to run
    the pipeline, but no download or citation reference is included in this
    release. `configs/segtr_full.yaml` tells the reader to "point these at
    your Smelly-E fork (link in top-level README)", and no such link exists.
    A pointer is pending; the configuration comment is left unedited so that
    the file stays byte-identical to the archived copy (§8).

12. **Commit authorship.** The first four commits were made during the
    double-blind review period under an anonymous identity. The history is
    preserved unrewritten so that the published tree remains verifiable
    against the archived artefacts, so those commits still carry that
    authorship.

13. **The bundled unit tests run 308 of 312 passing.** The four failures are
    in `test_tier3` and `test_tier4`, which assert that the rendered prompt
    contains a `## Smell guide` heading while the prompt builder emits
    `## Smell evidence`. This is a pre-existing string mismatch between the
    tests and the prompt templates. It is unrelated to the packaging fix of
    item 6 — before that fix the test modules could not be imported as
    `smell_repair_v2.*` at all — and it does not affect the repair path. It
    was not fixed for this release.

### Vestigial and non-executed configuration

The following are present in the code but are not read, not reached, or
superseded. **They are intentionally left unmodified** so that every
source-code file stays byte-identical to its counterpart in the Zenodo
archive; this list is the documentation that would otherwise be in comments.

| Location | Status |
|---|---|
| `operators/validator.py` — `coverage_delta_floor = -0.02` | Never consulted. `_measure_coverage_delta` is a stub that always returns `None`, so the guarded branch cannot be taken. The effective Gate 5 threshold is `_STATEMENT_LOSS_THRESHOLD = -0.30` in the regex proxy. |
| `configs/*.yaml` — the whole `llm:` block (`max_tokens: 2048`, `request_timeout_sec: 300`, `model`, `base_url`) | Not read at runtime. Effective values come from `src/segtr/config/llm_config.yaml` (§3). |
| `configs/*.yaml` — `pipeline:` keys `tiers_enabled`, `validator_gates`, `max_llm_attempts_per_plan`, `enable_deterministic_rules`, `gate5_coverage_max_pct_drop` | Not read by any code. The drivers map a different key set (`enable_tier1..4`, `max_attempts`, `model_key`, …). |
| `configs/segtr_full.yaml` — the comment "0 indicates deactivated statement-loss check" above `gate5_coverage_max_pct_drop: 30` | Describes a mechanism that does not exist; the key is unread and the real threshold is hard-coded. |
| `configs/*.yaml` — `base_url: "<OPENAI_COMPATIBLE_ENDPOINT>"`, and the header comment in `configs/segtr_full.yaml` attributing the placeholder to "double-blind review" | The endpoint placeholders are an artefact of review-time anonymisation, and the comment still describes them that way. The wording is left unedited so the file stays byte-identical to the archived copy (§8). The endpoint actually used is recorded in `environment/llm_config.md` and `src/segtr/config/llm_config.yaml`. |
| `llm/client.py` — dataclass defaults `max_tokens = 2048`, `request_timeout_sec = 180` | Dead on the paper's execution path: `MultiModelClient` always supplies both explicitly. |
| `src/segtr/config/llm_config.yaml` — the model entries `qwen35_9b`, `qwen35_27b`, `qwen_coder_next`, `gemma4_31b` | Candidates from an earlier model-selection phase. Only `gpt_oss_20b` was used for the paper. |
| `scripts/phaseC_aggregate_utrefactor.py` — the "model downgrade (120b → 20b)" paragraph | Narrative from an exploratory smoke run on a project outside the RQ3 cohort. The reported model is `openai/gpt-oss-20b` throughout. Retained verbatim because it is part of the report text that produced the archived artefacts. |
| `scripts/phaseB_aggregate_naive.py` — `total_method_time_min = ... * 0  # placeholder` and the adjacent `serial_eq  # not used` | Dead computations; neither value reaches the output. |
| `src/segtr/pipeline.py`, `src/segtr/cli.py` | The v1 pipeline, superseded by `pipeline_v2.py` / `cli_v2.py`. These are the only consumers of the `llm:` blocks in `configs/*.yaml`. |
| `src/operators/`, `src/validator/`, `src/prompts/` | Byte-identical duplicates of files under `src/segtr/`, mirroring the Zenodo archive layout. The importable copies are the ones under `src/segtr/`. |

## 8. Provenance

The pipeline sources in `src/` and `scripts/` are the same code that produced
the numbers reported in the paper; they were not edited for release. A
file-by-file MD5 comparison against the published archive is given in the
release notes for this version.

Four files differ from their archive counterparts by design: `README.md`,
`LICENSE`, `CITATION.cff`, and `pyproject.toml` — the two distributions nest
the sources differently, so each carries the `package-dir` mapping correct for
its own layout.

## 9. Reproducibility tier

This repository targets the ISSRE artefact **Available** badge. The Zenodo
archive is the artefact to start from for any numeric checking, since it
contains the per-project outputs that `verify_numbers.py` reads.

## 10. Third-party components

Referenced but not redistributed here; install from upstream:

- EvoSuite 1.2.0 — https://www.evosuite.org/ (LGPL-3.0)
- PIT 1.17.4 — https://pitest.org/ (Apache-2.0)
- JaCoCo 0.8.14 — https://www.jacoco.org/jacoco/ (EPL-2.0)
- JUnit 4.11 (EPL-1.0)
- Smelly-E, the 13-issue test-smell detector — see §7.11
- UTRefactor, the LLM-based smell-refactoring baseline — install per its
  authors' instructions

## 11. Citation

```bibtex
@inproceedings{kim2026segtr,
  title     = {SE-GTR: Smell Evidence-Guided Repair of Automatically
               Generated Unit Tests},
  author    = {Kim, Seungho and Lee, Scott Uk-Jin},
  booktitle = {Proc. IEEE Int. Symp. on Software Reliability
               Engineering (ISSRE)},
  year      = {2026},
  note      = {Page numbers and publisher DOI will be available with the
               proceedings.}
}

@misc{kim2026segtr_artifact,
  title     = {SE-GTR: Smell Evidence-Guided Repair of Automatically
               Generated Unit Tests --- replication package},
  author    = {Kim, Seungho and Lee, Scott Uk-Jin},
  year      = {2026},
  publisher = {Zenodo},
  version   = {2.0.0},
  doi       = {10.5281/zenodo.19743168}
}
```

The dataset DOI above is the **concept DOI**: it always resolves to the
newest version of the record. To cite the exact version this release
corresponds to, use the version DOI on the Zenodo landing page.

## 12. License and contact

Code in this repository is released under the MIT License (see `LICENSE`).
Data in the Zenodo archive is released under CC BY 4.0.

Contact: Seungho Kim <ohgnues@hanyang.ac.kr>

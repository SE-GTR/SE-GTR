# SE-GTR

Replication package for the ISSRE 2026 submission:
**"SE-GTR: Smell Evidence-Guided Repair of Automatically Generated Unit Tests."**

> **Anonymity note.** This repository is hosted under the system
> name `SE-GTR` for double-blind review. Author and institutional
> information will be added at camera-ready.

---

## 1. What this repository contains

- `src/` — the SE-GTR pipeline implementation and the Naive LLM
  baseline reimplementation.
- `configs/` — YAML configurations for SE-GTR Full, the Naive LLM
  baseline, and the four tier-ablation conditions.
- `scripts/` — driver shell scripts for each experimental phase,
  aggregation utilities, and `verify_numbers.py` which cross-checks
  paper numbers against the aggregate CSVs in the Zenodo archive.
- `cohort/` — definition of the 15-project RQ3 cohort
  (`selected_15.csv`) and the 32-project healthy pool it was drawn
  from.
- `claim_map/` — machine-readable map from every numeric claim in
  the paper to the source file that produced it.
- `environment/` — environment specification: Dockerfile, conda
  env, tool versions, LLM configuration template.

## 2. What this repository does NOT contain

Per-project workdirs, JaCoCo coverage XMLs, PIT mutation score
outputs, smell detector (`Smelly-E`) warning files for all
conditions, and the aggregate CSVs for every paper figure and
table all live in the companion Zenodo archive:

> DOI: **`10.5281/zenodo.XXXXXXX`** (anonymous; will be updated at
> camera-ready)

Downloading the Zenodo archive is required to run `verify_numbers.py`
end-to-end; this repository alone is sufficient to inspect and re-run
the pipeline on new projects.

## 3. Quick start

Path A — rerun SE-GTR on one SF110 project:

```bash
cp configs/config.example.yaml configs/config.yaml
# edit configs/config.yaml to point at an OpenAI-compatible LLM endpoint

bash scripts/run_phase4.sh <project_name>
```

Path B — verify the paper's numeric claims against the Zenodo
archive:

```bash
# after downloading and extracting the Zenodo archive
python scripts/verify_numbers.py --package-root /path/to/extracted/zenodo/
```

## 4. Environment

- Python 3.11
- Java 8 (EvoSuite 1.2.0 and SF110 Ant targets require this)
- Ant 1.10+
- PIT 1.17.4 (for mutation testing; optional)
- JaCoCo 0.8+ (for coverage measurement)
- An OpenAI-compatible LLM endpoint for `gpt-oss-20b` (the model
  used in the paper)

See `environment/` for a Dockerfile, a conda `environment.yml`, and
per-tool version files.

## 5. Reproducibility tier

This repository is positioned for the **ISSRE artefact "available"**
badge. For full numeric reproduction ("reproduced" badge), start
from the Zenodo archive, which includes the pre-computed per-project
workdirs that `verify_numbers.py` reads.

## 6. Third-party components

The following tools are referenced but **not** redistributed in this
repository. Install them from their upstream distributions:

- EvoSuite 1.2.0 (https://www.evosuite.org/)
- PIT 1.17.4 (https://pitest.org/)
- JaCoCo (https://www.jacoco.org/jacoco/)
- JUnit 4.11 (standard Maven/Ivy dependency)
- Smelly-E, the 13-issue detector (upstream release URL TBD at
  camera-ready)
- UTRefactor (the LLM-based smell refactoring baseline; install per
  upstream authors' instructions)

## 7. License

Code in this repository: MIT (see `LICENSE`).
Data in the Zenodo archive: CC-BY-4.0.

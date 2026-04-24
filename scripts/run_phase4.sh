#!/usr/bin/env bash
# Reproduce RQ1/RQ2 — SE-GTR Full across 81 held-out SF110 projects.
# Thin wrapper over run_phase4_parallel.py (the Python driver).
#
# Prerequisites:
#   - Java 8 and Ant 1.10+ on PATH
#   - EvoSuite 1.2.0, JUnit 4.11, Smelly-E shaded jar (see ../../07_environment/)
#   - An OpenAI-compatible LLM endpoint that serves openai/gpt-oss-20b,
#     with $LLM_API_KEY and $LLM_BASE_URL in the environment.
#   - SF110 pristine projects staged at $SF110_ROOT/<N>_<name>/
#
# Wall clock: ~24 h on 1 modest CPU + 1 LLM endpoint (90 min cap/project,
# some retried at 180 min). See final_summary.md for the timing log.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."

python3 "$HERE/run_phase4_parallel.py" \
    --config 00_code/configs/segtr_full.yaml \
    --projects_file 01_cohort/heldout_81.txt \
    --out output/phase4_main \
    "$@"

#!/usr/bin/env bash
# Reproduce RQ3 Phase E — 3-way mutation testing on the 15-project cohort.
# Runs PIT 1.17.4 against (Full, Naive, UTRefactor) outputs from Phases A–C.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."

python3 "$HERE/phaseE_run_pit.py" \
    --cohort 01_cohort/selected_15.csv \
    --full_root   output/phase4_main \
    --naive_root  output/rq3/naive_llm \
    --utref_root  output/rq3/utrefactor \
    --out         output/rq3/phase_e_pit \
    "$@"

#!/usr/bin/env bash
# Reproduce RQ3 Phase B — Naive LLM baseline across the 15-project cohort.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."

python3 "$HERE/run_rq3_parallel.py" \
    --condition naive_llm \
    --config 00_code/configs/naive_llm.yaml \
    --cohort 01_cohort/selected_15.csv \
    --out output/rq3/naive_llm \
    "$@"

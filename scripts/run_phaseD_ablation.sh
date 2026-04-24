#!/usr/bin/env bash
# Reproduce RQ3 Phase D — 3 ablation conditions × 15 projects = 45 runs.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."

for cond in t1_only t1_t2 t1_t2_t3; do
    echo "[ablation] starting $cond"
    python3 "$HERE/run_rq3_parallel.py" \
        --condition "$cond" \
        --config "00_code/configs/ablation_${cond}.yaml" \
        --cohort 01_cohort/selected_15.csv \
        --out "output/rq3/ablation/${cond}"
done

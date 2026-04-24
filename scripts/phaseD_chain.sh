#!/usr/bin/env bash
# Phase D — ablation chain. Runs 3 conditions sequentially, each with
# N=4 internal parallelism. Serial between conditions keeps the
# concurrency ceiling at 4 cli_v2 subprocesses (avoids the N>4 memory
# pressure we saw during PIT).
#
# Usage:
#   scripts/phaseD_chain.sh
#   → output/runs/rq3_experiments/ablation/{t1_only,t1_t2,t1_t2_t3}/
set -euo pipefail

REPO="<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl"
cd "$REPO"

PROJECTS="$REPO/output/runs/rq3_experiments/selection/selected_15.csv"
OUT="$REPO/output/runs/rq3_experiments/ablation"
mkdir -p "$OUT"

CONDITIONS=(t1_only t1_t2 t1_t2_t3)

for cond in "${CONDITIONS[@]}"; do
  cond_out="$OUT/$cond"
  mkdir -p "$cond_out"
  echo "=== [$(date '+%H:%M:%S')] starting condition: $cond  out=$cond_out ===" \
      | tee -a "$OUT/chain.log"
  python3 scripts/run_rq3_parallel.py \
      --projects-csv "$PROJECTS" \
      --out "$cond_out" \
      --condition "$cond" \
      --parallel 4 \
      --timeout-min 60 \
      --cost-budget 20.0 \
      --interim-interval-min 30 \
      >> "$cond_out/runner_top.log" 2>&1
  rc=$?
  echo "=== [$(date '+%H:%M:%S')] condition $cond done (rc=$rc) ===" \
      | tee -a "$OUT/chain.log"
done

echo "=== [$(date '+%H:%M:%S')] all conditions complete ===" \
    | tee -a "$OUT/chain.log"

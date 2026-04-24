#!/usr/bin/env python3
"""Phase B Step 1b — tertile stratified sampling of 15 projects from the
32-project healthy pool.

Bins (data-driven natural tertiles of the 32-project healthy pool):
  small  : tests_total <   60      (n=11)
  medium : 60 ≤ tests <  220       (n=11)
  large  : tests_total ≥  220      (n=10, top 54_db-everywhere 843)

Sampling:
  random.seed(42); 5 projects per bin without replacement.

Also enriches each selected row with Phase 4 Full data (reusing that
cohort's raw_results.jsonl + per_project.json, no re-execution):
  - full_llm_calls               (Tier 2/3/4 LLM request count)
  - full_plans_submitted         (total plan groups sent to validator)
  - full_plans_accepted          (final_accepted=True count)
  - full_smells_before / after
  - full_smell_reduction_pct     ((before - after) / before * 100)
  - full_cost_usd                (sum of d_cost_usd across events)

Outputs:
  output/runs/rq3_experiments/selection/selected_15.csv
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
SELECTION_DIR = REPO / 'output/runs/rq3_experiments/selection'
POOL_CSV = SELECTION_DIR / 'pool_32_healthy.csv'
OUT_CSV = SELECTION_DIR / 'selected_15.csv'

SEED = 42

# Tertile boundaries — fixed by Phase B selection decision (Option A,
# natural tertile of the 32-project healthy pool).
SMALL_MAX = 60      # strict <
MEDIUM_MAX = 220    # strict <


def bin_of(n: int) -> str:
    if n < SMALL_MAX:
        return 'small'
    if n < MEDIUM_MAX:
        return 'medium'
    return 'large'


def _load_full_metrics(project: str) -> Dict[str, Any]:
    """Compute Phase 4 Full accept/cost/smell stats from existing artefacts.

    Never re-runs anything — reads `per_project.json` for smell totals and
    `raw_results.jsonl` for per-plan accept counts and LLM cost.
    """
    out: Dict[str, Any] = {
        'full_llm_calls': None,
        'full_plans_submitted': None,
        'full_plans_accepted': None,
        'full_smells_before': None,
        'full_smells_after': None,
        'full_smell_reduction_pct': None,
        'full_cost_usd': None,
        'full_jacoco_line_before': None,
        'full_jacoco_line_after': None,
    }
    proj_dir = REPO / 'output/runs/phase4_main' / f'project_{project}'
    pp = proj_dir / 'per_project.json'
    rr = proj_dir / 'raw_results.jsonl'
    if not pp.exists():
        return out
    try:
        d: Any = json.load(open(pp))
        if isinstance(d, list):
            d = d[0] if d else {}
    except Exception:
        return out
    sb = d.get('smell_totals_before') or {}
    sa = d.get('smell_totals_after') or {}
    total_before = sum(sb.values())
    total_after = sum(sa.values())
    out['full_smells_before'] = total_before
    out['full_smells_after'] = total_after
    out['full_smell_reduction_pct'] = (
        round((total_before - total_after) / total_before * 100.0, 2)
        if total_before else 0.0
    )
    out['full_jacoco_line_before'] = round(
        float((d.get('jacoco_before') or {}).get('line_coverage') or 0.0), 4)
    out['full_jacoco_line_after'] = round(
        float((d.get('jacoco_after') or {}).get('line_coverage') or 0.0), 4)

    if rr.exists():
        calls = 0
        cost = 0.0
        sub = 0
        acc = 0
        for ln in rr.open('r', encoding='utf-8'):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            ev = r.get('event')
            if ev in ('llm_result', 'tier4_result'):
                calls += 1
                cost += float(r.get('d_cost_usd') or 0.0)
            elif r.get('stage') == 'validator':
                sub += 1
                if r.get('final_accepted'):
                    acc += 1
        out['full_llm_calls'] = calls
        out['full_plans_submitted'] = sub
        out['full_plans_accepted'] = acc
        out['full_cost_usd'] = round(cost, 6)
    return out


def main() -> int:
    rows = list(csv.DictReader(POOL_CSV.open()))
    assert len(rows) == 32, f'expected 32 healthy projects, got {len(rows)}'

    # Classify into bins
    bins: Dict[str, List[Dict[str, Any]]] = {'small': [], 'medium': [], 'large': []}
    for r in rows:
        n = int(r['tests_total'])
        r['_bin'] = bin_of(n)
        bins[r['_bin']].append(r)

    sizes = {k: len(v) for k, v in bins.items()}
    print(f'bin sizes (from 32-project healthy pool): {sizes}')
    assert all(len(v) >= 5 for v in bins.values()), f'tertile bin too small: {sizes}'

    # Deterministic sampling
    rng = random.Random(SEED)
    selected: List[Dict[str, Any]] = []
    for b in ('small', 'medium', 'large'):
        # sort by project id so the RNG draws are deterministic across platforms
        lst = sorted(bins[b], key=lambda r: int(r['project'].split('_')[0]))
        picks = rng.sample(lst, 5)
        picks.sort(key=lambda r: int(r['project'].split('_')[0]))
        selected.extend(picks)

    # Enrich with Phase 4 Full stats
    print('enriching selected 15 with Phase 4 Full metrics...')
    for r in selected:
        r.update(_load_full_metrics(r['project']))

    out_headers = [
        'project', 'bin', 'tests_total',
        'line_coverage_pristine', 'branch_coverage_pristine',
        'mutation_score_pristine_pct',
        'v2_after_score_pct',
        'full_plans_submitted', 'full_plans_accepted',
        'full_llm_calls', 'full_cost_usd',
        'full_smells_before', 'full_smells_after',
        'full_smell_reduction_pct',
        'full_jacoco_line_before', 'full_jacoco_line_after',
        'phase4_coverage_outlier_note',
    ]
    with OUT_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=out_headers, extrasaction='ignore')
        w.writeheader()
        for r in selected:
            w.writerow({
                'project': r['project'],
                'bin': r['_bin'],
                'tests_total': r['tests_total'],
                'line_coverage_pristine': r['line_coverage_pristine'],
                'branch_coverage_pristine': r['branch_coverage_pristine'],
                'mutation_score_pristine_pct': r['mutation_score_pristine_pct'],
                'v2_after_score_pct': r['v2_after_score_pct'],
                'full_plans_submitted': r.get('full_plans_submitted'),
                'full_plans_accepted': r.get('full_plans_accepted'),
                'full_llm_calls': r.get('full_llm_calls'),
                'full_cost_usd': r.get('full_cost_usd'),
                'full_smells_before': r.get('full_smells_before'),
                'full_smells_after': r.get('full_smells_after'),
                'full_smell_reduction_pct': r.get('full_smell_reduction_pct'),
                'full_jacoco_line_before': r.get('full_jacoco_line_before'),
                'full_jacoco_line_after': r.get('full_jacoco_line_after'),
                'phase4_coverage_outlier_note': r.get('phase4_coverage_outlier_note', ''),
            })
    print(f'wrote: {OUT_CSV}')
    print('selected:')
    for r in selected:
        print(f'  [{r["_bin"]:6}] {r["project"]:25}  '
              f'tests={r["tests_total"]:>3}  '
              f'line_cov={r["line_coverage_pristine"]}  '
              f'mut={r["mutation_score_pristine_pct"]:>5}%')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

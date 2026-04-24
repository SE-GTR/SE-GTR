#!/usr/bin/env python3
"""Phase B Step 1a — build the 32-project healthy pool.

Writes `output/runs/rq3_experiments/selection/pool_32_healthy.csv` with
per-project descriptors required for reproducible stratified sampling:

  project, bin (after thresholds applied),
  test_methods (jacoco_before.tests_total from Phase 4),
  line_coverage_pristine (jacoco_before.line_coverage),
  mutation_score_pristine (from phase 4.5 pristine_v2pit),
  category (v1 PIT label — should be 'healthy' for every row),
  outlier_flag (Phase 4.3 coverage-outlier annotation, informational).

Pool definition:
  Phase 4 completed (checkpoint.json ['completed'])
  ∩ Phase 4.5 PIT-58 apples-to-apples cohort (both pristine & v2-after scores)
  ∩ v1 PIT category == 'healthy'
  − dev set {1_tullibee, 29_apbsmem, 31_xisemele, 71_ext4j, 88_jopenchart}
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
OUT = REPO / 'output/runs/rq3_experiments/selection'
OUT.mkdir(parents=True, exist_ok=True)

DEV = {'1_tullibee', '29_apbsmem', '31_xisemele', '71_ext4j', '88_jopenchart'}

# Phase 4.3 coverage-outlier annotations — the four projects flagged during
# Phase 4 aggregation, all of which were re-verified to be drain-partial
# artifacts and replaced with positive deltas. Informational only.
PHASE4_COVERAGE_OUTLIER_NOTES = {
    '4_rif': ('initial Δline=-14.98pp classified as drain artifact; '
              're-verified Δline=+0.05pp; row replaced in Phase 4.3 aggregation'),
    '3_gaj': ('initial Δline=-5.81pp classified as drain artifact; '
              're-verified Δline=+0.07pp; row replaced in Phase 4.3 aggregation'),
}


def main() -> int:
    ck = json.load(open(REPO / 'output/runs/phase4_main/checkpoint.json'))
    completed = set(ck['completed'])

    v1_cat = {
        r['project']: (r.get('category') or '').strip()
        for r in csv.DictReader(open(REPO / 'output/analysis_pit/rq3_final/before_vs_after.csv'))
    }

    pit_rows = list(csv.DictReader(
        open(REPO / 'output/runs/phase4_main/pit/per_project_pit_final.csv')
    ))
    pit_by_proj: Dict[str, Dict[str, str]] = {r['project']: r for r in pit_rows}
    valid_pit = {
        r['project'] for r in pit_rows
        if r.get('pristine_score_pct') and r.get('v2_after_score_pct')
    }

    pool = sorted(
        (completed & valid_pit)
        - DEV
        - {p for p in completed if v1_cat.get(p) != 'healthy'},
        key=lambda p: int(p.split('_')[0]),
    )

    rows = []
    missing: list[str] = []
    for proj in pool:
        jf = REPO / 'output/runs/phase4_main' / f'project_{proj}' / 'per_project.json'
        if not jf.exists():
            missing.append(proj)
            continue
        try:
            d: Any = json.load(open(jf))
            if isinstance(d, list):
                d = d[0] if d else {}
        except Exception:
            missing.append(proj)
            continue
        jb = d.get('jacoco_before') or {}
        pit = pit_by_proj.get(proj, {})
        rows.append({
            'project': proj,
            'tests_total': int(jb.get('tests_total') or 0),
            'line_coverage_pristine': round(float(jb.get('line_coverage') or 0.0), 4),
            'branch_coverage_pristine': round(float(jb.get('branch_coverage') or 0.0), 4),
            'instruction_coverage_pristine': round(float(jb.get('instruction_coverage') or 0.0), 4),
            'mutation_score_pristine_pct': round(float(pit.get('pristine_score_pct') or 0.0), 4),
            'pristine_mutants': int(pit.get('pristine_mutants') or 0),
            'pristine_killed': int(pit.get('pristine_killed') or 0),
            'v2_after_score_pct': round(float(pit.get('v2_after_score_pct') or 0.0), 4),
            'category_v1': v1_cat.get(proj, ''),
            'phase4_coverage_outlier_note': PHASE4_COVERAGE_OUTLIER_NOTES.get(proj, ''),
        })

    # Sort rows: by project number for stable output
    rows.sort(key=lambda r: int(r['project'].split('_')[0]))

    # Emit pool CSV
    out_csv = OUT / 'pool_32_healthy.csv'
    headers = [
        'project', 'tests_total',
        'line_coverage_pristine', 'branch_coverage_pristine',
        'instruction_coverage_pristine',
        'mutation_score_pristine_pct',
        'pristine_mutants', 'pristine_killed', 'v2_after_score_pct',
        'category_v1', 'phase4_coverage_outlier_note',
    ]
    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f'wrote: {out_csv}  (n={len(rows)})')
    if missing:
        print(f'WARNING: {len(missing)} projects missing per_project.json: {missing}')

    # Stats
    tc_values = [r['tests_total'] for r in rows]
    tc_values.sort()
    n = len(tc_values)
    p33 = tc_values[int(n * 1/3)]
    p67 = tc_values[int(n * 2/3)]
    print(f'tests_total distribution: min={tc_values[0]} p33={p33} p67={p67} max={tc_values[-1]}')
    print(f'Phase 4 coverage-outlier rows in pool: '
          f'{[r["project"] for r in rows if r["phase4_coverage_outlier_note"]]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

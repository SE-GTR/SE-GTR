#!/usr/bin/env python3
"""Phase 4.5 final Table 7 — v2 pristine (PIT 1.17.4) vs v2 after (PIT 1.17.4).

This is the apples-to-apples comparison spec-A requested. Uses:
  - output/runs/phase4_main/pit/pristine_v2pit/<proj>/score.json (new baseline)
  - output/runs/phase4_main/pit/per_project/<proj>/score.json (v2 after)
  - v1 PIT-cohort healthy/weak_oracle/low_coverage labels (for subgroups)

Produces:
  output/runs/phase4_main/pit/table7_final.{csv,md}
  output/runs/phase4_main/pit/per_project_pit_final.csv
  output/runs/phase4_main/pit/outlier_analysis_final.md
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
PHASE4 = REPO / 'output' / 'runs' / 'phase4_main'
V1_CSV = REPO / 'output' / 'analysis_pit' / 'rq3_final' / 'before_vs_after.csv'
PRISTINE_DIR = PHASE4 / 'pit' / 'pristine_v2pit'
V2_AFTER_DIR = PHASE4 / 'pit' / 'per_project'
OUT = PHASE4 / 'pit'


def load_score(path: Path) -> Dict[str, Any]:
    try:
        d = json.loads(path.read_text())
    except Exception:
        return {'ok': False}
    ok = d.get('pit_ok') and (d.get('pit_total_mutants') or 0) > 0
    return {
        'ok': bool(ok),
        'mutants': d.get('pit_total_mutants'),
        'killed': d.get('pit_killed'),
        'score_pct': d.get('pit_score_pct'),
        'timed_out': bool(d.get('timed_out')),
        'rc': d.get('rc'),
    }


def load_category() -> Dict[str, str]:
    """v1 PIT healthy / weak_oracle / low_coverage labels."""
    out: Dict[str, str] = {}
    with V1_CSV.open() as f:
        for row in csv.DictReader(f):
            out[row['project']] = row.get('category') or 'unknown'
    return out


def main() -> int:
    cat = load_category()

    projects_all = sorted(cat.keys(), key=lambda p: int(p.split('_')[0]))
    rows: List[List[Any]] = []
    by_cat: Dict[str, List[Tuple[str, float, float]]] = {}
    for proj in projects_all:
        p_score = load_score(PRISTINE_DIR / proj / 'score.json')
        a_score = load_score(V2_AFTER_DIR / proj / 'score.json')
        p_ok = p_score.get('ok')
        a_ok = a_score.get('ok')
        row = [
            proj, cat.get(proj, 'unknown'),
            p_score.get('mutants') if p_ok else None,
            p_score.get('killed') if p_ok else None,
            p_score.get('score_pct') if p_ok else None,
            a_score.get('mutants') if a_ok else None,
            a_score.get('killed') if a_ok else None,
            a_score.get('score_pct') if a_ok else None,
            (a_score['score_pct'] - p_score['score_pct']) if (p_ok and a_ok) else None,
            'pristine_timeout' if (not p_ok and p_score.get('timed_out')) else
            ('v2_after_timeout' if (not a_ok and a_score.get('timed_out')) else
             ('pristine_fail' if not p_ok else
              ('v2_after_fail' if not a_ok else ''))),
        ]
        rows.append(row)
        if p_ok and a_ok:
            by_cat.setdefault(cat.get(proj, 'unknown'), []).append(
                (proj, p_score['score_pct'], a_score['score_pct'])
            )

    # Per-project CSV
    headers = ['project', 'category',
               'pristine_mutants', 'pristine_killed', 'pristine_score_pct',
               'v2_after_mutants', 'v2_after_killed', 'v2_after_score_pct',
               'delta_pp', 'status']
    (OUT / 'per_project_pit_final.csv').open('w', newline='',
                                             encoding='utf-8').write('')
    with (OUT / 'per_project_pit_final.csv').open(
            'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(r)

    # Table 7: per-category aggregate
    cat_order = ['healthy', 'weak_oracle', 'low_coverage']
    t7_rows: List[List[Any]] = []
    for c in cat_order:
        entries = by_cat.get(c, [])
        if not entries:
            continue
        n = len(entries)
        mean_p = statistics.mean(e[1] for e in entries)
        mean_a = statistics.mean(e[2] for e in entries)
        delta = mean_a - mean_p
        t7_rows.append([
            c, n,
            f'{mean_p:.2f}',
            f'{mean_a:.2f}',
            f'{delta:+.2f}pp',
        ])
    all_entries = [e for lst in by_cat.values() for e in lst]
    if all_entries:
        n = len(all_entries)
        mean_p = statistics.mean(e[1] for e in all_entries)
        mean_a = statistics.mean(e[2] for e in all_entries)
        delta = mean_a - mean_p
        t7_rows.append([
            'OVERALL', n,
            f'{mean_p:.2f}',
            f'{mean_a:.2f}',
            f'{delta:+.2f}pp',
        ])

    t7_headers = ['category', 'n', 'v2_pristine_score', 'v2_after_score',
                  'delta (v2 after − v2 pristine)']
    with (OUT / 'table7_final.csv').open('w', newline='',
                                         encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(t7_headers)
        for r in t7_rows:
            w.writerow(r)

    lines = [
        f'# Table 7 (final) — Mutation score preservation under identical PIT config',
        '',
        f'Cohort: n={len(all_entries)} projects where both **v2 pristine** '
        f'(our new baseline, PIT 1.17.4 on SF110 pristine workdirs) and '
        f'**v2 after** (Phase 4 repaired workdirs) produced valid '
        f'`mutations.xml`. This is the apples-to-apples comparison '
        f'(same tool version, same mutator set, same targetClasses).',
        '',
        '| ' + ' | '.join(t7_headers) + ' |',
        '|' + '|'.join(['---'] * len(t7_headers)) + '|',
    ]
    for r in t7_rows:
        lines.append('| ' + ' | '.join(str(c) for c in r) + ' |')
    (OUT / 'table7_final.md').write_text('\n'.join(lines) + '\n',
                                         encoding='utf-8')

    # Outliers under fair baseline
    outliers = []
    for proj, p, a in all_entries:
        d = a - p
        if d < -5.0:
            outliers.append((proj, cat.get(proj, 'unknown'), p, a, d))
    outliers.sort(key=lambda x: x[4])

    olines = [
        '# Outlier analysis (final) — projects with v2_after − v2_pristine < −5pp',
        '',
        f'Cohort: n={len(all_entries)} apples-to-apples comparisons.',
        f'Outlier count: **{len(outliers)}**',
        '',
        '| project | category | pristine | v2_after | Δpp |',
        '|---|---|---:|---:|---:|',
    ]
    for proj, c, p, a, d in outliers:
        olines.append(f'| {proj} | {c} | {p:.2f} | {a:.2f} | {d:+.2f} |')
    (OUT / 'outlier_analysis_final.md').write_text(
        '\n'.join(olines) + '\n', encoding='utf-8'
    )

    print(f'Cohort size: {len(all_entries)}')
    print(f'Category sizes: ' +
          ', '.join(f'{c}={len(by_cat.get(c,[]))}' for c in cat_order))
    print(f'Outliers (<-5pp): {len(outliers)}')
    print(f'wrote: {OUT / "table7_final.md"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

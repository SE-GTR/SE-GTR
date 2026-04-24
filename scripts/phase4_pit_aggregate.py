#!/usr/bin/env python3
"""Phase 4.4 aggregation — Table 7 mutation score preservation.

Joins v1 PIT baseline (`output/analysis_pit/rq3_final/before_vs_after.csv`)
with the v2 SE-GTR after scores measured in this phase, producing:

    output/runs/phase4_main/pit/
      table7_mutation_score.{csv,md}
      per_project_pit.csv             # scatter data (b/a_v1/a_v2)
      outlier_analysis.md             # Δ_v2 < -5pp deep dives
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
PHASE4_DIR = REPO / 'output' / 'runs' / 'phase4_main'
PIT_PER_PROJ = PHASE4_DIR / 'pit' / 'per_project'
PIT_OUT = PHASE4_DIR / 'pit'
V1_CSV = REPO / 'output' / 'analysis_pit' / 'rq3_final' / 'before_vs_after.csv'


def load_v1() -> Dict[str, Dict[str, Any]]:
    """project → v1 category + before/after score."""
    out: Dict[str, Dict[str, Any]] = {}
    with V1_CSV.open() as f:
        for row in csv.DictReader(f):
            out[row['project']] = {
                'category': row.get('category') or 'unknown',
                'direction': row.get('direction') or '',
                'v1_before_score_pct': float(row.get('b_score_pct') or 0.0),
                'v1_after_score_pct': float(row.get('a_score_pct') or 0.0),
                'v1_before_mutants': int(row.get('b_mutants') or 0),
                'v1_after_mutants': int(row.get('a_mutants') or 0),
            }
    return out


def load_v2() -> Dict[str, Dict[str, Any]]:
    """Every score.json in per_project/. Keeps failed/timeout as null-score."""
    out: Dict[str, Dict[str, Any]] = {}
    for proj_dir in sorted(PIT_PER_PROJ.iterdir()):
        if not proj_dir.is_dir():
            continue
        score_path = proj_dir / 'score.json'
        if not score_path.exists():
            continue
        try:
            d = json.loads(score_path.read_text())
        except Exception:
            continue
        name = proj_dir.name
        ok = bool(d.get('pit_ok')) and (d.get('pit_total_mutants') or 0) > 0
        out[name] = {
            'ok': ok,
            'v2_after_score_pct': float(d.get('pit_score_pct') or 0.0) if ok else None,
            'v2_after_mutants': d.get('pit_total_mutants'),
            'v2_after_killed': d.get('pit_killed'),
            'elapsed_sec': d.get('elapsed_sec'),
            'timed_out': bool(d.get('timed_out')),
            'rc': d.get('rc'),
        }
    return out


def build_table7(v1: Dict[str, Dict[str, Any]],
                 v2: Dict[str, Dict[str, Any]]) -> None:
    """Compute by-category stats (healthy/weak_oracle/low_coverage/OVERALL)
    over projects where BOTH v1 and v2 have a score."""
    cats: Dict[str, List[Tuple[str, float, float, float]]] = {}
    per_proj_rows: List[List[Any]] = []
    for proj, v1e in v1.items():
        v2e = v2.get(proj, {})
        if not v2e.get('ok'):
            # skip from table7, but still record for per-project CSV
            per_proj_rows.append([
                proj, v1e['category'], v1e['direction'],
                v1e['v1_before_score_pct'],
                v1e['v1_after_score_pct'],
                None,
                None,
                None,
                v2e.get('timed_out'), v2e.get('rc'),
            ])
            continue
        b = v1e['v1_before_score_pct']
        a_v1 = v1e['v1_after_score_pct']
        a_v2 = v2e['v2_after_score_pct']
        cats.setdefault(v1e['category'], []).append(
            (proj, b, a_v1, a_v2)
        )
        per_proj_rows.append([
            proj, v1e['category'], v1e['direction'],
            b, a_v1, a_v2,
            a_v1 - b, a_v2 - b, v2e.get('timed_out'), v2e.get('rc'),
        ])

    # Sort categories
    cat_order = ['healthy', 'weak_oracle', 'low_coverage']
    table7_rows: List[List[Any]] = []
    for cat in cat_order:
        entries = cats.get(cat, [])
        if not entries:
            continue
        n = len(entries)
        mean_b = statistics.mean(e[1] for e in entries)
        mean_a_v1 = statistics.mean(e[2] for e in entries)
        mean_a_v2 = statistics.mean(e[3] for e in entries)
        delta_v1 = mean_a_v1 - mean_b
        delta_v2 = mean_a_v2 - mean_b
        improvement = delta_v2 - delta_v1
        table7_rows.append([
            cat, n,
            f'{mean_b:.2f}',
            f'{mean_a_v1:.2f} ({delta_v1:+.2f}pp)',
            f'{mean_a_v2:.2f} ({delta_v2:+.2f}pp)',
            f'{improvement:+.2f}pp',
        ])

    # OVERALL
    all_entries = [e for entries in cats.values() for e in entries]
    if all_entries:
        n = len(all_entries)
        mean_b = statistics.mean(e[1] for e in all_entries)
        mean_a_v1 = statistics.mean(e[2] for e in all_entries)
        mean_a_v2 = statistics.mean(e[3] for e in all_entries)
        delta_v1 = mean_a_v1 - mean_b
        delta_v2 = mean_a_v2 - mean_b
        improvement = delta_v2 - delta_v1
        table7_rows.append([
            'OVERALL', n,
            f'{mean_b:.2f}',
            f'{mean_a_v1:.2f} ({delta_v1:+.2f}pp)',
            f'{mean_a_v2:.2f} ({delta_v2:+.2f}pp)',
            f'{improvement:+.2f}pp',
        ])

    # Write Table 7
    headers = ['category', 'n', 'pristine_score_pct',
               'v1_after_score (Δ)', 'v2_after_score (Δ)',
               'v2 − v1 (pp)']
    with (PIT_OUT / 'table7_mutation_score.csv').open('w', newline='',
                                                      encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in table7_rows:
            w.writerow(r)

    lines = [
        f'# Table 7 — Mutation score preservation (n={len(all_entries)})',
        '',
        'Baseline reused from v1 (`output/analysis_pit/rq3_final/`); v2 '
        'scores measured in Phase 4.4 on SE-GTR v2 workdirs with identical '
        'PIT 1.17.4 + Phase-6-validated config. `v2 − v1` = v2 delta − v1 '
        'delta, positive = v2 regresses **less** than v1.',
        '',
        '| ' + ' | '.join(headers) + ' |',
        '|' + '|'.join(['---'] * len(headers)) + '|',
    ]
    for r in table7_rows:
        lines.append('| ' + ' | '.join(str(c) for c in r) + ' |')
    (PIT_OUT / 'table7_mutation_score.md').write_text(
        '\n'.join(lines) + '\n', encoding='utf-8'
    )
    # Per-project CSV
    pp_headers = ['project', 'category', 'direction_v1',
                  'pristine_score', 'v1_after_score', 'v2_after_score',
                  'delta_v1_pp', 'delta_v2_pp', 'v2_timed_out', 'v2_rc']
    with (PIT_OUT / 'per_project_pit.csv').open('w', newline='',
                                                encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(pp_headers)
        for r in per_proj_rows:
            w.writerow(r)
    return all_entries


def outlier_report(v1: Dict[str, Dict[str, Any]],
                   v2: Dict[str, Dict[str, Any]]) -> None:
    """Δ_v2 < -5pp projects — dig into per_project artefacts."""
    outliers: List[Tuple[str, float, float, float, float]] = []
    missing: List[Tuple[str, str]] = []
    for proj, v1e in v1.items():
        v2e = v2.get(proj) or {}
        b = v1e['v1_before_score_pct']
        a_v1 = v1e['v1_after_score_pct']
        if not v2e.get('ok'):
            reason = ('timeout' if v2e.get('timed_out')
                      else ('no_score' if not v2e
                            else f'rc={v2e.get("rc")}'))
            missing.append((proj, reason))
            continue
        a_v2 = v2e['v2_after_score_pct']
        d_v2 = a_v2 - b
        if d_v2 < -5.0:
            outliers.append((proj, b, a_v1, a_v2, d_v2))

    lines = [
        '# Phase 4.4 — PIT outlier analysis',
        '',
        'Projects where v2 after PIT score regressed by more than 5pp from '
        'the pristine baseline. For each outlier we record v1 vs v2 deltas '
        'to see if v2 made a pre-existing regression worse.',
        '',
    ]
    if outliers:
        lines.extend([
            f'## {len(outliers)} outliers (Δ_v2 < −5pp)',
            '',
            '| project | category | pristine | v1_after (Δ) | v2_after (Δ) | v2 − v1 |',
            '|---|---|---:|---:|---:|---:|',
        ])
        for p, b, a_v1, a_v2, _ in outliers:
            cat = v1[p]['category']
            d_v1 = a_v1 - b
            d_v2 = a_v2 - b
            lines.append(
                f'| {p} | {cat} | {b:.2f} | {a_v1:.2f} ({d_v1:+.2f}pp) '
                f'| {a_v2:.2f} ({d_v2:+.2f}pp) | {d_v2 - d_v1:+.2f}pp |'
            )
    else:
        lines.append('**No projects regressed more than 5pp in v2.**')

    lines.extend([
        '',
        f'## Projects without a v2 PIT score ({len(missing)})',
        '',
        '| project | v2 reason |',
        '|---|---|',
    ])
    for p, reason in missing:
        lines.append(f'| {p} | {reason} |')
    lines.append('')
    (PIT_OUT / 'outlier_analysis.md').write_text(
        '\n'.join(lines) + '\n', encoding='utf-8'
    )


def main() -> int:
    v1 = load_v1()
    v2 = load_v2()
    print(f'v1 projects: {len(v1)}')
    print(f'v2 results:  {len(v2)} '
          f'(ok={sum(1 for v in v2.values() if v["ok"])})')
    build_table7(v1, v2)
    outlier_report(v1, v2)
    print(f'wrote: {PIT_OUT / "table7_mutation_score.md"}')
    print(f'wrote: {PIT_OUT / "per_project_pit.csv"}')
    print(f'wrote: {PIT_OUT / "outlier_analysis.md"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

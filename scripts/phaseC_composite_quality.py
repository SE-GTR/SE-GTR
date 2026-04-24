#!/usr/bin/env python3
"""Phase C follow-up — composite quality metric across 3 conditions.

Per-project composite quality score::

    Q = (smells_reduced       * 1)
      - (smells_introduced    * 2)
      + (coverage_gain_pp     * 5)
      - (coverage_loss_pp     * 10)
      - (regressions          * 5)

Where:
  - `smells_reduced`  = sum over smells s of max(0, before[s] - after[s])
  - `smells_introduced` = sum over smells s of max(0, after[s] - before[s])
    Note: this is strictly positive when the *kind* of smell is
    introduced, so a repair that eliminates 10 DS but creates 2 OIMT
    scores smells_reduced=10, smells_introduced=2 (net +6).
  - `coverage_gain_pp` = max(0,  Δline) in percentage points
  - `coverage_loss_pp` = max(0, -Δline) in percentage points
  - `regressions`     = class-level regressions (Full / Naive: true
    transitions; UTRefactor: post-state only — we annotate this)

Weights come from the task spec and are intentionally asymmetric
(introduction 2×, coverage loss 2× penalties, 5× per pp coverage-or-
regression).

Outputs:
  output/runs/rq3_experiments/utrefactor/run15/composite_quality.csv
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
RUN_DIR = REPO / 'output/runs/rq3_experiments/utrefactor/run15'
NAIVE_DIR = REPO / 'output/runs/rq3_experiments/naive_llm'
FULL_DIR = REPO / 'output/runs/phase4_main'
SELECTION_CSV = REPO / 'output/runs/rq3_experiments/selection/selected_15.csv'

W_REDUCE = 1.0
W_INTRODUCE = 2.0
W_COV_GAIN = 5.0
W_COV_LOSS = 10.0
W_REGRESSION = 5.0


def _smell_totals_from(d: Any) -> Dict[str, int]:
    if not d:
        return {}
    if isinstance(d, list):
        d = d[0] if d else {}
    return dict(d.get('smell_totals_after') or {}) if False else d.get('smell_totals_before') or {}


def _load_phase4(project: str) -> Dict[str, Any]:
    pp = FULL_DIR / f'project_{project}' / 'per_project.json'
    if not pp.exists():
        return {}
    try:
        d: Any = json.load(pp.open())
        if isinstance(d, list):
            d = d[0] if d else {}
        return d
    except Exception:
        return {}


def _load_naive(project: str) -> Dict[str, Any]:
    pp = NAIVE_DIR / f'project_{project}' / 'per_project.json'
    if not pp.exists():
        return {}
    try:
        d: Any = json.load(pp.open())
        if isinstance(d, list):
            d = d[0] if d else {}
        return d
    except Exception:
        return {}


def _load_utref(project: str) -> Dict[str, Any]:
    pp = RUN_DIR / f'project_{project}' / 'per_project.json'
    if not pp.exists():
        return {}
    try:
        d: Any = json.load(pp.open())
        if isinstance(d, list):
            d = d[0] if d else {}
        return d
    except Exception:
        return {}


def _per_smell_delta(d: Dict[str, Any]) -> tuple[int, int]:
    """Return (reduced, introduced) counts summed across all smell types."""
    before = d.get('smell_totals_before') or {}
    after = d.get('smell_totals_after') or {}
    reduced = introduced = 0
    for k in set(before) | set(after):
        b = before.get(k, 0); a = after.get(k, 0)
        diff = a - b
        if diff < 0:
            reduced += -diff
        elif diff > 0:
            introduced += diff
    return reduced, introduced


def _delta_line_pp(d: Dict[str, Any]) -> Optional[float]:
    jb = (d.get('jacoco_before') or {}).get('line_coverage')
    ja = (d.get('jacoco_after') or {}).get('line_coverage')
    if jb is None or ja is None:
        return None
    return (ja - jb) * 100.0


def _utref_delta_line_pp(project: str) -> Optional[float]:
    """Use the sequential JaCoCo remeasurement for UTRefactor."""
    csv_path = RUN_DIR / 'jacoco_remeasure.csv'
    if not csv_path.exists():
        return None
    for r in csv.DictReader(csv_path.open()):
        if r['project'] == project and r['status'] == 'ok':
            try:
                return float(r['delta_line_pp'])
            except (ValueError, TypeError, KeyError):
                return None
    return None


def _regressions(d: Dict[str, Any]) -> int:
    return len(d.get('regressed_classes') or [])


def _score(reduced: int, introduced: int, cov_pp: Optional[float],
           regressions: int) -> Dict[str, float]:
    cov_gain = max(0.0, cov_pp) if cov_pp is not None else 0.0
    cov_loss = max(0.0, -cov_pp) if cov_pp is not None else 0.0
    q = (reduced * W_REDUCE
         - introduced * W_INTRODUCE
         + cov_gain * W_COV_GAIN
         - cov_loss * W_COV_LOSS
         - regressions * W_REGRESSION)
    return {
        'reduced': reduced,
        'introduced': introduced,
        'cov_gain_pp': round(cov_gain, 3),
        'cov_loss_pp': round(cov_loss, 3),
        'regressions': regressions,
        'quality_score': round(q, 3),
    }


def main() -> int:
    sel = {r['project']: r for r in csv.DictReader(SELECTION_CSV.open())}
    projects = list(sel.keys())

    rows: List[Dict[str, Any]] = []
    totals = {'full': [], 'naive': [], 'utref': []}
    for p in projects:
        full = _load_phase4(p)
        naive = _load_naive(p)
        utref = _load_utref(p)

        full_r, full_i = _per_smell_delta(full)
        nv_r, nv_i = _per_smell_delta(naive)
        ut_r, ut_i = _per_smell_delta(utref)

        full_dl = _delta_line_pp(full)
        nv_dl = _delta_line_pp(naive)
        ut_dl = _utref_delta_line_pp(p)

        f_s = _score(full_r, full_i, full_dl, _regressions(full))
        n_s = _score(nv_r, nv_i, nv_dl, _regressions(naive))
        u_s = _score(ut_r, ut_i, ut_dl, _regressions(utref)) if utref else None

        row: Dict[str, Any] = {
            'project': p, 'bin': sel[p]['bin'],
            'tests_pristine': sel[p]['tests_total'],
            'full_reduced': f_s['reduced'], 'full_introduced': f_s['introduced'],
            'full_cov_gain_pp': f_s['cov_gain_pp'],
            'full_cov_loss_pp': f_s['cov_loss_pp'],
            'full_regressions': f_s['regressions'],
            'full_quality': f_s['quality_score'],
            'naive_reduced': n_s['reduced'], 'naive_introduced': n_s['introduced'],
            'naive_cov_gain_pp': n_s['cov_gain_pp'],
            'naive_cov_loss_pp': n_s['cov_loss_pp'],
            'naive_regressions': n_s['regressions'],
            'naive_quality': n_s['quality_score'],
        }
        if u_s is not None:
            row.update({
                'utref_reduced': u_s['reduced'],
                'utref_introduced': u_s['introduced'],
                'utref_cov_gain_pp': u_s['cov_gain_pp'],
                'utref_cov_loss_pp': u_s['cov_loss_pp'],
                'utref_regressions': u_s['regressions'],
                'utref_quality': u_s['quality_score'],
            })
            totals['utref'].append(u_s['quality_score'])
        else:
            row.update({
                'utref_reduced': None, 'utref_introduced': None,
                'utref_cov_gain_pp': None, 'utref_cov_loss_pp': None,
                'utref_regressions': None, 'utref_quality': None,
            })
        totals['full'].append(f_s['quality_score'])
        totals['naive'].append(n_s['quality_score'])
        rows.append(row)

    headers = list(rows[0].keys())
    out_csv = RUN_DIR / 'composite_quality.csv'
    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)

    def _mean(xs: List[float]) -> float:
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else 0.0

    print(f'wrote: {out_csv}')
    print()
    print(f'Composite quality (higher is better, weights '
          f'reduce=+1, introduce=-2, cov_gain=+5pp, cov_loss=-10pp, '
          f'regression=-5):')
    print(f'  Full    (n=15): mean={_mean(totals["full"]):+.2f}  '
          f'projects with Q>0: {sum(1 for x in totals["full"] if x > 0)}/15')
    n_naive = sum(1 for r in rows if r["naive_quality"] is not None)
    print(f'  Naive   (n={n_naive}): mean={_mean(totals["naive"]):+.2f}  '
          f'projects with Q>0: {sum(1 for x in totals["naive"] if x > 0)}/{n_naive}')
    n_utref = sum(1 for r in rows if r["utref_quality"] is not None)
    print(f'  UTRef   (n={n_utref}): mean={_mean(totals["utref"]):+.2f}  '
          f'projects with Q>0: {sum(1 for x in totals["utref"] if x > 0)}/{n_utref}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

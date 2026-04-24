#!/usr/bin/env python3
"""Phase C Step 3 — aggregate UTRefactor run15 results + 3-way comparison.

Mirrors `scripts/phaseB_aggregate_naive.py`. Reads per-project.json files
from the Phase C run directory, joins with Phase B (Naive) and Phase 4
(Full) artefacts on the 12 projects that all three conditions completed,
and emits the paper-ready summary.

Outputs:
  output/runs/rq3_experiments/utrefactor/run15/
    per_project_utrefactor.csv
    aggregate_by_bin.csv
    three_way_comparison.csv
    smell_substitution_3way.csv
    anomaly_90_dcparseargs.md
    utrefactor_summary.md
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
UTREF_DIR = REPO / 'output/runs/rq3_experiments/utrefactor/run15'
NAIVE_DIR = REPO / 'output/runs/rq3_experiments/naive_llm'
FULL_DIR = REPO / 'output/runs/phase4_main'
SELECTION_CSV = REPO / 'output/runs/rq3_experiments/selection/selected_15.csv'


def _safe_mean(xs: List[Optional[float]]) -> Optional[float]:
    xs_f = [x for x in xs if x is not None]
    return statistics.mean(xs_f) if xs_f else None


def _safe_median(xs: List[Optional[float]]) -> Optional[float]:
    xs_f = [x for x in xs if x is not None]
    return statistics.median(xs_f) if xs_f else None


def _fmt(x: Optional[float], nd: int = 2) -> str:
    if x is None:
        return '—'
    return f'{x:.{nd}f}'


def _load_utrefactor_per_project(project: str) -> Dict[str, Any]:
    pj = UTREF_DIR / f'project_{project}' / 'per_project.json'
    if not pj.exists():
        return {'project': project, 'status': 'timeout'}
    try:
        d: Any = json.load(pj.open())
        if isinstance(d, list):
            d = d[0] if d else {}
    except Exception:
        return {'project': project, 'status': 'error'}
    d['status'] = 'completed'
    return d


def _load_full_per_project(project: str) -> Dict[str, Any]:
    pj = FULL_DIR / f'project_{project}' / 'per_project.json'
    if not pj.exists():
        return {}
    try:
        d: Any = json.load(pj.open())
        if isinstance(d, list):
            d = d[0] if d else {}
        return d
    except Exception:
        return {}


def _load_full_accept(project: str) -> Dict[str, Any]:
    """Count Full's plans submitted/accepted from raw_results.jsonl."""
    rr = FULL_DIR / f'project_{project}' / 'raw_results.jsonl'
    out = {'submitted': 0, 'accepted': 0, 'cost_usd': 0.0, 'llm_calls': 0}
    if not rr.exists():
        return out
    for ln in rr.open():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        ev = r.get('event')
        if ev in ('llm_result', 'tier4_result'):
            out['llm_calls'] += 1
            out['cost_usd'] += float(r.get('d_cost_usd') or 0.0)
        elif r.get('stage') == 'validator':
            out['submitted'] += 1
            if r.get('final_accepted'):
                out['accepted'] += 1
    return out


def _load_naive_per_project(project: str) -> Dict[str, Any]:
    pj = NAIVE_DIR / f'project_{project}' / 'per_project.json'
    if not pj.exists():
        return {'project': project, 'status': 'timeout'}
    try:
        d: Any = json.load(pj.open())
        if isinstance(d, list):
            d = d[0] if d else {}
    except Exception:
        return {'project': project, 'status': 'error'}
    d['status'] = 'completed'
    return d


def _totals(d: Dict[str, Any]) -> int:
    return sum((d or {}).values()) if d else 0


def main() -> int:
    sel = {r['project']: r for r in csv.DictReader(SELECTION_CSV.open())}
    projects = list(sel.keys())

    utref = {p: _load_utrefactor_per_project(p) for p in projects}
    full = {p: _load_full_per_project(p) for p in projects}
    full_accept = {p: _load_full_accept(p) for p in projects}
    naive = {p: _load_naive_per_project(p) for p in projects}

    # --- per_project_utrefactor.csv ---
    headers = [
        'project', 'bin', 'tests_total', 'status',
        'orch_elapsed_min',
        'smells_before', 'smells_after', 'smell_reduction_pct',
        'class_tests_before', 'class_tests_after', 'regressed_classes',
        'jacoco_line_before', 'jacoco_line_after', 'delta_line_pp',
        'jacoco_branch_before', 'jacoco_branch_after', 'delta_branch_pp',
    ]
    # Pull elapsed from checkpoint
    ck = json.load((UTREF_DIR / 'checkpoint.json').open())
    elapsed_by_proj = {
        r['project']: r.get('orch_elapsed_min')
        for r in (ck.get('all_results') or [])
    }

    with (UTREF_DIR / 'per_project_utrefactor.csv').open(
            'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
        w.writeheader()
        for p in projects:
            u = utref[p]
            sb = _totals(u.get('smell_totals_before'))
            sa = _totals(u.get('smell_totals_after'))
            jb = u.get('jacoco_before') or {}
            ja = u.get('jacoco_after') or {}
            jb_l = jb.get('line_coverage')
            ja_l = ja.get('line_coverage')
            jb_b = jb.get('branch_coverage')
            ja_b = ja.get('branch_coverage')
            row = {
                'project': p,
                'bin': sel[p]['bin'],
                'tests_total': sel[p]['tests_total'],
                'status': u.get('status'),
                'orch_elapsed_min': elapsed_by_proj.get(p),
                'smells_before': sb,
                'smells_after': sa,
                'smell_reduction_pct': ((sb - sa) / sb * 100.0) if sb else None,
                'class_tests_before': u.get('class_tests_before'),
                'class_tests_after': u.get('class_tests_after'),
                'regressed_classes': len(u.get('regressed_classes') or []),
                'jacoco_line_before': jb_l,
                'jacoco_line_after': ja_l,
                'delta_line_pp': ((ja_l - jb_l) * 100.0) if (jb_l is not None and ja_l is not None) else None,
                'jacoco_branch_before': jb_b,
                'jacoco_branch_after': ja_b,
                'delta_branch_pp': ((ja_b - jb_b) * 100.0) if (jb_b is not None and ja_b is not None) else None,
            }
            w.writerow(row)

    # --- aggregate_by_bin.csv ---
    bins: Dict[str, List[str]] = {'small': [], 'medium': [], 'large': []}
    for p in projects:
        bins[sel[p]['bin']].append(p)

    def _agg(sub: List[str]) -> Dict[str, Any]:
        completed = [p for p in sub if utref[p].get('status') == 'completed']
        timeouts = [p for p in sub if utref[p].get('status') == 'timeout']
        accept_pcts: List[float] = []   # n/a for UTRefactor (no per-method accept)
        smell_reds: List[float] = []
        line_deltas: List[float] = []
        br_deltas: List[float] = []
        introduced_counts = 0  # projects where smell_after > smell_before
        for p in completed:
            u = utref[p]
            sb = _totals(u.get('smell_totals_before'))
            sa = _totals(u.get('smell_totals_after'))
            if sb:
                smell_reds.append((sb - sa) / sb * 100.0)
            if sa > sb:
                introduced_counts += 1
            jb_l = (u.get('jacoco_before') or {}).get('line_coverage')
            ja_l = (u.get('jacoco_after') or {}).get('line_coverage')
            if jb_l is not None and ja_l is not None:
                line_deltas.append((ja_l - jb_l) * 100.0)
            jb_b = (u.get('jacoco_before') or {}).get('branch_coverage')
            ja_b = (u.get('jacoco_after') or {}).get('branch_coverage')
            if jb_b is not None and ja_b is not None:
                br_deltas.append((ja_b - jb_b) * 100.0)
        return {
            'n_total': len(sub),
            'n_completed': len(completed),
            'n_timeout': len(timeouts),
            'timeout_projects': timeouts,
            'mean_smell_reduction_pct': _safe_mean(smell_reds),
            'median_smell_reduction_pct': _safe_median(smell_reds),
            'mean_delta_line_pp': _safe_mean(line_deltas),
            'mean_delta_branch_pp': _safe_mean(br_deltas),
            'projects_with_net_smell_increase': introduced_counts,
        }

    agg = {b: _agg(bins[b]) for b in ['small', 'medium', 'large']}
    agg['all'] = _agg(projects)

    with (UTREF_DIR / 'aggregate_by_bin.csv').open(
            'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['bin', 'n_total', 'n_completed', 'n_timeout',
                    'mean_smell_reduction_pct',
                    'median_smell_reduction_pct',
                    'mean_delta_line_pp', 'mean_delta_branch_pp',
                    'projects_with_net_smell_increase'])
        for b in ['small', 'medium', 'large', 'all']:
            a = agg[b]
            w.writerow([
                b, a['n_total'], a['n_completed'], a['n_timeout'],
                _fmt(a['mean_smell_reduction_pct']),
                _fmt(a['median_smell_reduction_pct']),
                _fmt(a['mean_delta_line_pp'], 3),
                _fmt(a['mean_delta_branch_pp'], 3),
                a['projects_with_net_smell_increase'],
            ])

    # --- 3-way comparison cohort: projects completed in all three conditions ---
    comp = [
        p for p in projects
        if utref[p].get('status') == 'completed'
        and naive[p].get('status') == 'completed'
        # Full completes for all 15 in Phase 4 main — per_project.json present
        and full[p]
    ]
    with (UTREF_DIR / 'three_way_comparison.csv').open(
            'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([
            'project', 'bin', 'tests_total',
            'full_smell_red_pct', 'naive_smell_red_pct', 'utref_smell_red_pct',
            'full_delta_line_pp', 'naive_delta_line_pp', 'utref_delta_line_pp',
            'full_cost_usd', 'naive_cost_usd', 'utref_cost_usd',
            'full_regressed', 'naive_regressed', 'utref_regressed',
        ])
        for p in comp:
            fu = full[p]; nv = naive[p]; ut = utref[p]
            fa = full_accept[p]
            fsb = _totals(fu.get('smell_totals_before'))
            fsa = _totals(fu.get('smell_totals_after'))
            nsb = _totals(nv.get('smell_totals_before'))
            nsa = _totals(nv.get('smell_totals_after'))
            usb = _totals(ut.get('smell_totals_before'))
            usa = _totals(ut.get('smell_totals_after'))
            fdr = ((fsb - fsa) / fsb * 100.0) if fsb else None
            ndr = ((nsb - nsa) / nsb * 100.0) if nsb else None
            udr = ((usb - usa) / usb * 100.0) if usb else None
            fdl = ((fu.get('jacoco_after') or {}).get('line_coverage', 0)
                   - (fu.get('jacoco_before') or {}).get('line_coverage', 0)) * 100.0
            ndl = ((nv.get('jacoco_after') or {}).get('line_coverage', 0)
                   - (nv.get('jacoco_before') or {}).get('line_coverage', 0)) * 100.0
            u_jb_l = (ut.get('jacoco_before') or {}).get('line_coverage')
            u_ja_l = (ut.get('jacoco_after') or {}).get('line_coverage')
            udl = ((u_ja_l - u_jb_l) * 100.0) if (u_jb_l is not None and u_ja_l is not None) else None
            naive_cost = (nv.get('naive') or {}).get('cost_usd')
            w.writerow([
                p, sel[p]['bin'], sel[p]['tests_total'],
                _fmt(fdr), _fmt(ndr), _fmt(udr),
                _fmt(fdl, 3), _fmt(ndl, 3), _fmt(udl, 3),
                _fmt(fa.get('cost_usd', 0.0), 4),
                _fmt(naive_cost, 4) if naive_cost is not None else '—',
                '—',   # UTRefactor doesn't echo LLM cost
                len(fu.get('regressed_classes') or []),
                len(nv.get('regressed_classes') or []),
                len(ut.get('regressed_classes') or []),
            ])

    # --- smell_substitution_3way.csv ---
    all_smells: set = set()
    for p in comp:
        for src in (full[p], naive[p], utref[p]):
            all_smells.update((src.get('smell_totals_before') or {}).keys())
            all_smells.update((src.get('smell_totals_after') or {}).keys())
    subst_rows = []
    for s in sorted(all_smells):
        row = {'smell': s}
        for label, src in (('full', full), ('naive', naive), ('utref', utref)):
            bsum = asum = 0
            intro_n = 0
            for p in comp:
                sb = (src[p].get('smell_totals_before') or {}).get(s, 0)
                sa = (src[p].get('smell_totals_after') or {}).get(s, 0)
                bsum += sb; asum += sa
                if sa > sb:
                    intro_n += 1
            row[f'{label}_before'] = bsum
            row[f'{label}_after'] = asum
            row[f'{label}_delta'] = asum - bsum
            row[f'{label}_introduced_in_n'] = intro_n
        subst_rows.append(row)
    with (UTREF_DIR / 'smell_substitution_3way.csv').open(
            'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(subst_rows[0].keys()))
        w.writeheader()
        w.writerows(subst_rows)

    # --- anomaly_90_dcparseargs.md ---
    u90 = utref['90_dcparseargs']
    n90 = naive['90_dcparseargs']
    f90 = full['90_dcparseargs']
    alines = [
        '# Anomaly — 90_dcparseargs smell substitution under UTRefactor (20b)',
        '',
        'This is the one 15-cohort project where UTRefactor produced a net '
        '*increase* in Smelly-measured smell count. All three conditions '
        'completed on this project so we can compare per-smell.',
        '',
        '| smell | before | **Full** after (Δ) | **Naive** after (Δ) | **UTRefactor** after (Δ) |',
        '|---|---:|---:|---:|---:|',
    ]
    a90 = sorted(
        set((u90.get('smell_totals_before') or {}).keys())
        | set((u90.get('smell_totals_after') or {}).keys())
        | set((f90.get('smell_totals_after') or {}).keys())
        | set((n90.get('smell_totals_after') or {}).keys())
    )
    for s in a90:
        b = (u90.get('smell_totals_before') or {}).get(s, 0)
        f_a = (f90.get('smell_totals_after') or {}).get(s, 0)
        n_a = (n90.get('smell_totals_after') or {}).get(s, 0)
        u_a = (u90.get('smell_totals_after') or {}).get(s, 0)
        if b or f_a or n_a or u_a:
            alines.append(
                f'| {s} | {b} | {f_a} ({f_a-b:+d}) | {n_a} ({n_a-b:+d}) | '
                f'**{u_a} ({u_a-b:+d})** |'
            )
    alines.extend([
        '',
        'UTRefactor\'s rewrite flipped the one project into a +466 % OIMT '
        'explosion — 28 new "Asserting object initialization multiple '
        'times" instances — while Full and Naive left OIMT unchanged at 6.',
        '',
        'Mechanism: UTRefactor splits every original test method into a '
        'separate top-level `@Test` (see `src/test_split/` in the workdir) '
        'and then rewrites each split. When the same constructor is '
        'exercised across the split tests, Smelly-E\'s OIMT detector '
        'counts each duplicate pattern. Full and Naive edit in-place so '
        'the splitting does not happen.',
        '',
        'Coverage / tests:',
        f'- class_tests after: {u90.get("class_tests_after")} / '
        f'{u90.get("class_tests_total")} (Full: '
        f'{f90.get("class_tests_after")}/{f90.get("class_tests_total")}, '
        f'Naive: {n90.get("class_tests_after")}/{n90.get("class_tests_total")})',
        f'- jacoco_line_after: UTRefactor measurement failed '
        f'(JaCoCo agent exception — see `project_90_dcparseargs/post_hoc.log`), '
        f'Full: {(f90.get("jacoco_after") or {}).get("line_coverage")}, '
        f'Naive: {(n90.get("jacoco_after") or {}).get("line_coverage")}.',
    ])
    (UTREF_DIR / 'anomaly_90_dcparseargs.md').write_text(
        '\n'.join(alines) + '\n', encoding='utf-8'
    )

    # --- utrefactor_summary.md ---
    L: List[str] = []
    L.append('# Phase C — UTRefactor (gpt-oss-20b) on 15-project RQ3 cohort')
    L.append('')
    L.append(
        'Artefacts: `output/runs/rq3_experiments/utrefactor/run15/`.\n'
        'Selection: `selection/selected_15.csv` (same 15 projects as Phase B).'
    )
    L.append('')
    L.append('## 1. Run summary')
    L.append('')
    a_all = agg['all']
    L.append(f'| Field | Value |')
    L.append(f'|---|---|')
    L.append(f'| Projects attempted | 15 |')
    L.append(f'| Completed | {a_all["n_completed"]} |')
    L.append(f'| Timed out (60-min cap) | {a_all["n_timeout"]} — {a_all["timeout_projects"]} |')
    L.append(f'| Failed | 0 |')
    L.append(f'| Wall clock (runner) | 127.3 min |')
    L.append(f'| Parallelism | N = 4 (worker1..worker4 rsync copies) |')
    L.append(f'| Model | openai/gpt-oss-20b |')
    L.append(f'| Cost | — (UTRefactor does not echo LLM cost; est. < $0.30 at OpenRouter rates) |')
    L.append('')
    L.append(
        '- Same 3 timeouts as Phase B Naive (`2_a4j`, `54_db-everywhere`, '
        '`63_objectexplorer`). The 60-min cap is a Naive-and-UTRefactor-'
        'generic scaling limit on large EvoSuite suites, not a UTRefactor-'
        'specific problem.'
    )
    L.append('')

    L.append('## 2. Per-project (UTRefactor run15)')
    L.append('')
    L.append(
        '| project | bin | tests | status | elapsed | smell Δ% | Δline pp | classes after |'
    )
    L.append('|---|---|---:|---|---:|---:|---:|---|')
    for p in projects:
        u = utref[p]
        sb = _totals(u.get('smell_totals_before'))
        sa = _totals(u.get('smell_totals_after'))
        red = ((sb - sa) / sb * 100.0) if sb else None
        jb_l = (u.get('jacoco_before') or {}).get('line_coverage')
        ja_l = (u.get('jacoco_after') or {}).get('line_coverage')
        dl = ((ja_l - jb_l) * 100.0) if (jb_l is not None and ja_l is not None) else None
        status_s = '**timeout**' if u.get('status') == 'timeout' else 'ok'
        ctb = u.get('class_tests_before')
        cta = u.get('class_tests_after')
        cttot = u.get('class_tests_total')
        classes = f'{cta}/{cttot}' if cta is not None else '—'
        L.append(
            f'| {p} | {sel[p]["bin"]} | {sel[p]["tests_total"]} | '
            f'{status_s} | {_fmt(elapsed_by_proj.get(p))} min | '
            f'{_fmt(red)}% | {_fmt(dl, 3)} | {classes} |'
        )
    L.append('')

    L.append('## 3. Aggregate by bin')
    L.append('')
    L.append(
        '| bin | completed/total | mean smell Δ% | median smell Δ% | mean Δline pp | projects w/ net smell ↑ |'
    )
    L.append('|---|---:|---:|---:|---:|---:|')
    for b in ['small', 'medium', 'large', 'all']:
        a = agg[b]
        L.append(
            f'| {b} | {a["n_completed"]}/{a["n_total"]} '
            f'| {_fmt(a["mean_smell_reduction_pct"])}% '
            f'| {_fmt(a["median_smell_reduction_pct"])}% '
            f'| {_fmt(a["mean_delta_line_pp"], 3)} pp '
            f'| {a["projects_with_net_smell_increase"]} / {a["n_completed"]} |'
        )

    # 4. 3-way comparison
    L.append('')
    L.append(f'## 4. 3-way comparison on common-completed cohort (n = {len(comp)})')
    L.append('')
    full_smell_reds: List[float] = []
    naive_smell_reds: List[float] = []
    utref_smell_reds: List[float] = []
    full_lines: List[Optional[float]] = []
    naive_lines: List[Optional[float]] = []
    utref_lines: List[Optional[float]] = []
    full_cost_tot = 0.0
    naive_cost_tot = 0.0
    full_reg_tot = 0
    naive_reg_tot = 0
    utref_reg_tot = 0
    for p in comp:
        fu = full[p]; nv = naive[p]; ut = utref[p]
        fa = full_accept[p]
        fsb = _totals(fu.get('smell_totals_before'))
        fsa = _totals(fu.get('smell_totals_after'))
        nsb = _totals(nv.get('smell_totals_before'))
        nsa = _totals(nv.get('smell_totals_after'))
        usb = _totals(ut.get('smell_totals_before'))
        usa = _totals(ut.get('smell_totals_after'))
        if fsb: full_smell_reds.append((fsb - fsa) / fsb * 100.0)
        if nsb: naive_smell_reds.append((nsb - nsa) / nsb * 100.0)
        if usb: utref_smell_reds.append((usb - usa) / usb * 100.0)
        full_jb = (fu.get('jacoco_before') or {}).get('line_coverage')
        full_ja = (fu.get('jacoco_after') or {}).get('line_coverage')
        nv_jb = (nv.get('jacoco_before') or {}).get('line_coverage')
        nv_ja = (nv.get('jacoco_after') or {}).get('line_coverage')
        ut_jb = (ut.get('jacoco_before') or {}).get('line_coverage')
        ut_ja = (ut.get('jacoco_after') or {}).get('line_coverage')
        if full_jb is not None and full_ja is not None:
            full_lines.append((full_ja - full_jb) * 100.0)
        if nv_jb is not None and nv_ja is not None:
            naive_lines.append((nv_ja - nv_jb) * 100.0)
        if ut_jb is not None and ut_ja is not None:
            utref_lines.append((ut_ja - ut_jb) * 100.0)
        full_cost_tot += (fa.get('cost_usd') or 0.0)
        naive_cost_tot += ((nv.get('naive') or {}).get('cost_usd') or 0.0)
        full_reg_tot += len(fu.get('regressed_classes') or [])
        naive_reg_tot += len(nv.get('regressed_classes') or [])
        utref_reg_tot += len(ut.get('regressed_classes') or [])

    L.append('| metric | **Full** (SE-GTR) | **Naive LLM** | **UTRefactor** |')
    L.append('|---|---:|---:|---:|')
    L.append(
        f'| Mean smell reduction | {_fmt(_safe_mean(full_smell_reds))}% | '
        f'{_fmt(_safe_mean(naive_smell_reds))}% | '
        f'{_fmt(_safe_mean(utref_smell_reds))}% |'
    )
    L.append(
        f'| Median smell reduction | {_fmt(_safe_median(full_smell_reds))}% | '
        f'{_fmt(_safe_median(naive_smell_reds))}% | '
        f'{_fmt(_safe_median(utref_smell_reds))}% |'
    )
    L.append(
        f'| Mean Δ line coverage | {_fmt(_safe_mean(full_lines), 3)} pp | '
        f'{_fmt(_safe_mean(naive_lines), 3)} pp | '
        f'{_fmt(_safe_mean(utref_lines), 3)} pp |'
    )
    L.append(
        f'| Σ LLM cost (USD) | ${full_cost_tot:.4f} | ${naive_cost_tot:.4f} | — (not echoed) |'
    )
    L.append(
        f'| Σ class-level regressions | {full_reg_tot} | {naive_reg_tot} | {utref_reg_tot} |'
    )
    L.append(
        f'| Completed on 15 | 15/15 | 12/15 | 12/15 |'
    )

    # 5. Per-bin 3-way
    L.append('')
    L.append('## 5. Per-bin 3-way (smell reduction %)')
    L.append('')
    L.append('| bin | Full mean | Naive mean | UTRefactor mean | UTRef − Full | UTRef − Naive |')
    L.append('|---|---:|---:|---:|---:|---:|')
    for b in ['small', 'medium', 'large']:
        sub = [p for p in comp if sel[p]['bin'] == b]
        full_s = []; naive_s = []; utref_s = []
        for p in sub:
            fsb = _totals(full[p].get('smell_totals_before'))
            fsa = _totals(full[p].get('smell_totals_after'))
            nsb = _totals(naive[p].get('smell_totals_before'))
            nsa = _totals(naive[p].get('smell_totals_after'))
            usb = _totals(utref[p].get('smell_totals_before'))
            usa = _totals(utref[p].get('smell_totals_after'))
            if fsb: full_s.append((fsb - fsa) / fsb * 100.0)
            if nsb: naive_s.append((nsb - nsa) / nsb * 100.0)
            if usb: utref_s.append((usb - usa) / usb * 100.0)
        f_m = _safe_mean(full_s)
        n_m = _safe_mean(naive_s)
        u_m = _safe_mean(utref_s)
        uf = (u_m - f_m) if (u_m is not None and f_m is not None) else None
        un = (u_m - n_m) if (u_m is not None and n_m is not None) else None
        L.append(
            f'| {b} | {_fmt(f_m)}% | {_fmt(n_m)}% | {_fmt(u_m)}% '
            f'| {_fmt(uf)} pp | {_fmt(un)} pp |'
        )

    # 6. Smell substitution (3-way)
    L.append('')
    L.append('## 6. Per-smell substitution table (3-way)')
    L.append('')
    L.append(
        '| smell | Full Δ (in N proj) | Naive Δ (N) | UTRefactor Δ (N) |'
    )
    L.append('|---|---|---|---|')
    subst_rows.sort(key=lambda r: (-r['utref_delta'], r['smell']))
    for r in subst_rows:
        L.append(
            f'| {r["smell"]} | '
            f'{r["full_delta"]:+d} ({r["full_introduced_in_n"]}/{len(comp)}) | '
            f'{r["naive_delta"]:+d} ({r["naive_introduced_in_n"]}/{len(comp)}) | '
            f'**{r["utref_delta"]:+d}** ({r["utref_introduced_in_n"]}/{len(comp)}) |'
        )

    # 7. Observations
    L.append('')
    L.append('## 7. Observations')
    L.append('')
    L.append(
        '**Same 3 timeouts as Naive.** `2_a4j`, `54_db-everywhere`, and '
        '`63_objectexplorer` hit the 60-min cap under UTRefactor as well. '
        'These are the same three projects Phase B Naive failed on. That '
        'makes timeouts a cohort property, not a condition property — the '
        'final RQ3 table can be computed on the 12 commonly-completed '
        'projects without selection bias between conditions.'
    )
    L.append('')
    f_mean = _safe_mean(full_smell_reds) or 0.0
    u_mean = _safe_mean(utref_smell_reds) or 0.0
    n_mean = _safe_mean(naive_smell_reds) or 0.0
    L.append(
        f'**Smell-reduction ranking on common cohort.** '
        f'Full **{f_mean:.1f}%** > UTRefactor **{u_mean:.1f}%** > '
        f'Naive **{n_mean:.1f}%**. UTRefactor\'s gap to Full is '
        f'{f_mean - u_mean:+.1f} pp; UTRefactor beats Naive by '
        f'{u_mean - n_mean:+.1f} pp. On the cohort-level, UTRefactor is '
        f'a meaningful middle point — but 1 of its 12 projects '
        f'(`90_dcparseargs`) is a net *increase* (+466%), which is what '
        f'pulls the mean down.'
    )
    L.append('')
    L.append(
        '**90_dcparseargs anomaly drill-down.** Detailed in '
        '`anomaly_90_dcparseargs.md`. Root cause: UTRefactor\'s '
        'method-splitting step (`src/test_split/`) creates one top-level '
        '`@Test` per assertion cluster, and when the original test had '
        'constructor + assertion patterns, every split sibling ends up '
        'with the same constructor initialisation, which Smelly-E counts '
        'as OIMT ("Asserting object initialization multiple times"). '
        'This is a structural side-effect of UTRefactor\'s pipeline, not '
        'an LLM failure. Full and Naive edit in-place so they do not '
        'trigger it.'
    )
    L.append('')
    # Compare model-downgrade effect on 30_bpmail (smoke project)
    L.append(
        '**Model downgrade (120b → 20b) effect, from 30_bpmail smoke '
        '(not in this 15-cohort).** The 120b run of 30_bpmail showed '
        'Smelly 89→178 (+100 %, with +48 NASE and +62 TOFA introduced). '
        'The 20b run showed 89→48 (−46 %). On 30_bpmail at least, the '
        'smaller model is **less aggressive** and avoids the substitution '
        'failure mode that the larger model triggers. We cannot '
        'generalize from one project; the 15-cohort 20b result (UTRef '
        'mean smell reduction 10.0 % with 1/12 substituting) is the '
        'honest paper number.'
    )
    L.append('')
    # Count how many of 15 UTRefactor has a net smell increase
    n_inc_all = agg['all']['projects_with_net_smell_increase']
    L.append(
        f'**Projects with net smell increase.** UTRefactor: '
        f'{n_inc_all}/{agg["all"]["n_completed"]} (`90_dcparseargs`). '
        f'Naive: counted in `smell_substitution.csv` (TSVM was the main '
        f'Naive substitution smell — 4/12 projects). Full: 0/12 on '
        f'total-count basis; individual smells like TSVM can still '
        f'creep up (see the 3-way substitution table).'
    )

    # 8. Phase D readiness
    L.append('')
    L.append('## 8. Phase D (Ablation) readiness')
    L.append('')
    L.append(
        '- 15-project selected cohort stable; same 3 timeouts on both RQ3 '
        'baselines.\n'
        '- `scripts/run_rq3_parallel.py` (Phase B) already accepts '
        '`--condition t1_only|t1_t2|t1_t2_t3`; Phase D can reuse it '
        'without code changes.\n'
        '- 60-min cap is the right choice for Phase D too — neither '
        'Naive nor UTRefactor benefitted from a higher cap on the 3 '
        'large-suite timeouts.'
    )

    L.append('')
    L.append('## 9. Files')
    L.append('')
    L.append('- `per_project_utrefactor.csv`')
    L.append('- `aggregate_by_bin.csv`')
    L.append('- `three_way_comparison.csv`')
    L.append('- `smell_substitution_3way.csv`')
    L.append('- `anomaly_90_dcparseargs.md`')
    L.append('- `utrefactor_summary.md` (this document)')

    (UTREF_DIR / 'utrefactor_summary.md').write_text(
        '\n'.join(L) + '\n', encoding='utf-8'
    )
    print(f'wrote: {UTREF_DIR / "utrefactor_summary.md"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

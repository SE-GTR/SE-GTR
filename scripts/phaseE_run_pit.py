#!/usr/bin/env python3
"""Phase E — PIT mutation testing for Naive LLM / UTRefactor workdirs.

Full condition's PIT is already measured (Phase 4.5, reused from
`output/runs/phase4_main/pit/per_project/<proj>/score.json`). This
script handles the two remaining conditions only.

For each completed project, runs:
  python scripts/metrics/measure_pit.py
      --project <cond_workdir>
      --out output/runs/rq3_experiments/pit/<cond>/<project>/
      --pitest-home tools/pit_1_17_4
      --threads 4
      --pit-extra-args "--excludedClasses '*_ESTest*,*_ESTest_scaffolding*'"
      --filter-cut-jars --green-tests-only

Outputs:
  output/runs/rq3_experiments/pit/<cond>/<project>/score.json
  output/runs/rq3_experiments/pit/<cond>/summary.csv

Parallel N=2 (memory-safe; N=4 triggered server reboot during Phase 4.5
on large projects). Timeout is 30 min by default, bumpable per condition
via --timeout-min.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
PIT_HOME = REPO / 'tools' / 'pit_1_17_4'
MEASURE_PIT = REPO / 'scripts' / 'metrics' / 'measure_pit.py'
SELECTION_CSV = REPO / 'output/runs/rq3_experiments/selection/selected_15.csv'


def _naive_workdir(project: str) -> Optional[Path]:
    wd = REPO / 'output/runs/rq3_experiments/naive_llm' / f'project_{project}' / project
    return wd if (wd / 'build.xml').exists() else None


def _utref_workdir(project: str) -> Optional[Path]:
    proj_artefacts = (
        REPO / 'output/runs/rq3_experiments/utrefactor/run15'
        / 'by_project_tree' / 'by_project' / project
    )
    if not proj_artefacts.exists():
        return None
    runs = sorted(proj_artefacts.glob('run_*'))
    if not runs:
        return None
    wd = runs[-1] / 'workdir' / project
    return wd if (wd / 'build.xml').exists() else None


def _resolve_workdir(condition: str, project: str) -> Optional[Path]:
    if condition == 'naive_llm':
        return _naive_workdir(project)
    if condition == 'utrefactor':
        return _utref_workdir(project)
    raise ValueError(f'unknown condition {condition!r}')


def _parse_score(xml_path: Path) -> Dict[str, Any]:
    if not xml_path.exists():
        return {'ok': False, 'reason': 'no_mutations_xml'}
    try:
        root = ET.parse(xml_path).getroot()
    except Exception as e:
        return {'ok': False, 'reason': f'parse:{e}'}
    total = killed = 0
    counts: Dict[str, int] = {}
    for m in root.iter('mutation'):
        st = (m.get('status') or 'UNKNOWN').upper()
        counts[st] = counts.get(st, 0) + 1
        total += 1
        if st == 'KILLED':
            killed += 1
    return {
        'ok': True,
        'total_mutants': total,
        'killed': killed,
        'score_pct': round(killed / total * 100.0, 4) if total else 0.0,
        'status_counts': counts,
    }


def _run_one(project: str, condition: str, out_root: str,
             timeout_sec: int) -> Dict[str, Any]:
    wd = _resolve_workdir(condition, project)
    out_dir = Path(out_root) / condition / project
    out_dir.mkdir(parents=True, exist_ok=True)
    if wd is None:
        return {
            'project': project, 'condition': condition,
            'status': 'no_workdir', 'elapsed_sec': 0,
            'out_dir': str(out_dir),
        }

    cmd = [
        sys.executable, str(MEASURE_PIT),
        '--project', str(wd),
        '--out', str(out_dir),
        '--pitest-home', str(PIT_HOME),
        '--threads', '4',
        '--pit-extra-args',
        "--excludedClasses '*_ESTest*,*_ESTest_scaffolding*'",
        '--filter-cut-jars',
        '--green-tests-only',
    ]

    t0 = time.monotonic()
    rc: int
    stdout_tail = ''
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout_sec, check=False,
        )
        elapsed = time.monotonic() - t0
        rc = proc.returncode
        stdout_tail = (proc.stdout or '')[-2000:]
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - t0
        rc = -1
        stdout_tail = ((e.output or '') if isinstance(e.output, str) else '')[-2000:]
        (out_dir / 'pit_run.log').write_text(
            f'[runner] PIT wall timeout after {timeout_sec}s\n{stdout_tail}',
            encoding='utf-8',
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        rc = -2
        stdout_tail = f'{type(exc).__name__}: {exc}'

    score = _parse_score(out_dir / 'mutations.xml')
    out = {
        'project': project,
        'condition': condition,
        'rc': rc,
        'elapsed_sec': round(elapsed, 2),
        'timed_out': rc == -1,
        'status': ('success' if (rc == 0 and score.get('ok')
                                 and (score.get('total_mutants') or 0) > 0)
                   else ('timeout' if rc == -1 else 'error')),
        'workdir': str(wd),
        'stdout_tail': stdout_tail[-1200:],
        **{f'pit_{k}': v for k, v in score.items()},
    }
    (out_dir / 'score.json').write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--condition', required=True,
                    choices=['naive_llm', 'utrefactor'])
    ap.add_argument('--out-root', type=Path, required=True)
    ap.add_argument('--parallel', type=int, default=2)
    ap.add_argument('--timeout-min', type=int, default=30)
    ap.add_argument('--projects-csv', type=Path, default=SELECTION_CSV)
    args = ap.parse_args()

    projects = [r['project'] for r in csv.DictReader(args.projects_csv.open())]
    args.out_root.mkdir(parents=True, exist_ok=True)

    print(f'[pit] condition={args.condition}  n_projects={len(projects)}  '
          f'parallel={args.parallel}  timeout={args.timeout_min}min')
    timeout_sec = args.timeout_min * 60

    results: List[Dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.parallel) as pool:
        futs: Dict[Future, str] = {}
        it = iter(projects)
        for _ in range(min(args.parallel, len(projects))):
            try:
                p = next(it)
            except StopIteration:
                break
            futs[pool.submit(_run_one, p, args.condition,
                             str(args.out_root), timeout_sec)] = p

        while futs:
            done = [f for f in list(futs) if f.done()]
            if not done:
                time.sleep(5.0)
                continue
            for f in done:
                proj = futs.pop(f)
                try:
                    r = f.result()
                except Exception as exc:
                    r = {'project': proj, 'condition': args.condition,
                         'status': 'error',
                         'error': f'{type(exc).__name__}: {exc}'}
                results.append(r)
                s = r.get('status')
                sym = ('✓' if s == 'success'
                       else ('TIMEOUT' if s == 'timeout'
                             else ('no_wd' if s == 'no_workdir' else 'FAIL')))
                print(
                    f'[{sym}] {proj}  elapsed={r.get("elapsed_sec",0)/60:.1f}min  '
                    f'mutants={r.get("pit_total_mutants")}  '
                    f'score={r.get("pit_score_pct")}', flush=True
                )
                try:
                    p = next(it)
                    futs[pool.submit(_run_one, p, args.condition,
                                     str(args.out_root), timeout_sec)] = p
                except StopIteration:
                    pass

    # Summary CSV
    csv_path = args.out_root / args.condition / 'summary.csv'
    headers = ['project', 'status', 'elapsed_sec',
               'pit_total_mutants', 'pit_killed', 'pit_score_pct',
               'rc', 'timed_out']
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f'\n[pit] wrote {csv_path}')
    ok = sum(1 for r in results if r.get('status') == 'success')
    to = sum(1 for r in results if r.get('status') == 'timeout')
    fa = sum(1 for r in results if r.get('status') in ('error', 'no_workdir'))
    print(f'[pit] done: {ok} success / {to} timeout / {fa} error-or-missing')
    return 0


if __name__ == '__main__':
    sys.exit(main())

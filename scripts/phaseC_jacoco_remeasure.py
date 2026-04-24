#!/usr/bin/env python3
"""Phase C follow-up — sequential JaCoCo re-measurement for the 12
UTRefactor-completed projects.

The original run15 ran JaCoCo in-band with N=4 parallel workers and hit a
race / `FailOnTimeout`-vs-agent-shutdown failure; ``jacoco.exec`` was not
produced on any project. This script replays JaCoCo strictly
sequentially against the existing UTRefactor workdirs so we can fill in
the coverage-delta column of the 3-way comparison.

Outputs:
  output/runs/rq3_experiments/utrefactor/run15/jacoco_remeasure.csv
    project, bin, status, before_line, before_branch, before_inst,
    after_line, after_branch, after_inst,
    delta_line_pp, delta_branch_pp, delta_inst_pp, error
  output/runs/rq3_experiments/utrefactor/run15/jacoco_remeasure.log

Skips the three timeout projects (no workdir produced).
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
RUN_DIR = REPO / 'output/runs/rq3_experiments/utrefactor/run15'
SELECTION_CSV = REPO / 'output/runs/rq3_experiments/selection/selected_15.csv'

sys.path.insert(0, str(REPO))


def _baseline_line_coverage(project: str) -> Optional[Dict[str, Any]]:
    """Read Phase-4's `jacoco_before` for `project` (pristine workdir).
    Used as the "before" comparison point because the UTRefactor rsync
    preserves the same pristine classes.
    """
    pp = REPO / 'output/runs/phase4_main' / f'project_{project}' / 'per_project.json'
    if not pp.exists():
        return None
    try:
        data: Any = json.load(pp.open())
        if isinstance(data, list):
            data = data[0] if data else {}
        jb = data.get('jacoco_before') or {}
        if jb.get('line_coverage') is not None:
            return jb
    except Exception:
        pass
    return None


def _find_workdir(project: str) -> Optional[Path]:
    proj_artefacts = RUN_DIR / 'by_project_tree' / 'by_project' / project
    if not proj_artefacts.exists():
        return None
    runs = sorted(proj_artefacts.glob('run_*'))
    if not runs:
        return None
    latest = runs[-1]
    work = latest / 'workdir' / project
    return work if work.exists() else None


def _ensure_shared_lib(work_project: Path) -> None:
    import shutil
    workdir_root = work_project.parent
    shared_lib = workdir_root / 'lib'
    shared_lib.mkdir(exist_ok=True)
    need = [
        ('<ANON_ROOT>/segtr_replication/evosuite-1.2.0/evosuite-standalone-runtime-1.2.0.jar',
         'evosuite-standalone-runtime-1.2.0.jar'),
        ('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl/tools/backup-smelly-evidence/junit-4.11.jar',
         'junit-4.11.jar'),
        ('<ANON_ROOT>/segtr_replication/sf110_projects_pilot/lib/hamcrest-core-1.3.jar',
         'hamcrest-core-1.3.jar'),
    ]
    for src_s, name in need:
        src = Path(src_s); dst = shared_lib / name
        if src.exists() and not dst.exists():
            shutil.copyfile(src, dst)
    alias = shared_lib / 'evosuite.jar'
    primary = shared_lib / 'evosuite-standalone-runtime-1.2.0.jar'
    if primary.exists() and not alias.exists():
        shutil.copyfile(primary, alias)


def _run_jacoco_once(project: str, work_project: Path, log_fh) -> Dict[str, Any]:
    from smell_repair_v2.coverage.jacoco import run_jacoco
    _ensure_shared_lib(work_project)
    print(f'[jacoco] start {project}  work={work_project}', file=log_fh, flush=True)
    t0 = time.monotonic()
    try:
        res = run_jacoco(work_project, project_name=project).to_dict()
    except Exception as exc:
        res = {'error': f'{type(exc).__name__}: {exc}'}
    elapsed = time.monotonic() - t0
    res['_elapsed_sec'] = round(elapsed, 2)
    print(f'[jacoco] done  {project}  {elapsed:.1f}s  '
          f'line={res.get("line_coverage")}  err={res.get("error")}',
          file=log_fh, flush=True)
    return res


def main() -> int:
    sel = {r['project']: r for r in csv.DictReader(SELECTION_CSV.open())}
    projects = list(sel.keys())

    log_path = RUN_DIR / 'jacoco_remeasure.log'
    out_csv = RUN_DIR / 'jacoco_remeasure.csv'
    headers = [
        'project', 'bin', 'status',
        'before_line', 'before_branch', 'before_inst',
        'after_line', 'after_branch', 'after_inst',
        'delta_line_pp', 'delta_branch_pp', 'delta_inst_pp',
        'jacoco_elapsed_sec', 'error',
    ]
    rows = []
    with log_path.open('w', encoding='utf-8') as log_fh:
        for proj in projects:
            work = _find_workdir(proj)
            before = _baseline_line_coverage(proj)
            row: Dict[str, Any] = {
                'project': proj,
                'bin': sel[proj]['bin'],
                'status': 'ok',
                'before_line': (before or {}).get('line_coverage'),
                'before_branch': (before or {}).get('branch_coverage'),
                'before_inst': (before or {}).get('instruction_coverage'),
            }
            if work is None:
                row['status'] = 'no_workdir'
                row['error'] = 'UTRefactor workdir missing (likely timeout)'
                rows.append(row)
                continue

            after = _run_jacoco_once(proj, work, log_fh)
            err = after.get('error')
            if err:
                row['status'] = 'jacoco_error'
                row['error'] = str(err)[:200]
                rows.append(row)
                continue
            row['after_line'] = after.get('line_coverage')
            row['after_branch'] = after.get('branch_coverage')
            row['after_inst'] = after.get('instruction_coverage')
            row['jacoco_elapsed_sec'] = after.get('_elapsed_sec')
            bl = row['before_line']; al = row['after_line']
            bb = row['before_branch']; ab = row['after_branch']
            bi = row['before_inst']; ai = row['after_inst']
            row['delta_line_pp'] = round((al - bl) * 100.0, 3) \
                if (bl is not None and al is not None) else None
            row['delta_branch_pp'] = round((ab - bb) * 100.0, 3) \
                if (bb is not None and ab is not None) else None
            row['delta_inst_pp'] = round((ai - bi) * 100.0, 3) \
                if (bi is not None and ai is not None) else None
            rows.append(row)

    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)

    ok = [r for r in rows if r['status'] == 'ok']
    err = [r for r in rows if r['status'] not in ('ok', 'no_workdir')]
    skip = [r for r in rows if r['status'] == 'no_workdir']
    print(f'[jacoco] wrote {out_csv}  ok={len(ok)}  err={len(err)}  skipped={len(skip)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

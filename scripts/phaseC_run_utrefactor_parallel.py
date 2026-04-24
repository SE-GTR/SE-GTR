#!/usr/bin/env python3
"""Phase C — parallel UTRefactor runner for the 15-project RQ3 cohort.

UTRefactor's orchestrator (`scripts/rebuttal/run_utrefactor_selected.py`)
is single-threaded and keeps state in the tool's own checkout
(`testsmellrefactoring/temp/json/<project>`). To run N projects
concurrently we pre-rsync'd N isolated worker checkouts at
`rebuttal_experiments/exp3_utrefactor/workers/worker{1..N}/testsmellrefactoring`.

This runner:

  1. Dispatches projects onto worker threads (one thread per worker
     checkout; the worker lock ensures no two projects share a checkout).
  2. For each project, invokes the orchestrator with a per-project
     one-line projects file and a per-project experiment-root.
  3. After the orchestrator returns, runs the SE-GTR measurement layer on
     the UTRefactor workdir — JaCoCo (after), class-test pass (after),
     Smelly-E (already measured by the orchestrator; we just reload).
  4. Emits a Phase-B–compatible `per_project.json` per project inside the
     run directory, so downstream aggregation shares the same schema.

Outputs (self-contained inside llm_smelly_repair_impl, per the scope rule):

  <out>/
    checkpoint.json
    alerts.jsonl
    interim_summary_XX.md
    by_project/<proj>/                 # UTRefactor native artefacts
      smelly_<proj>.json
      run_<TIMESTAMP>/...
    project_<proj>/                    # Phase-B schema per-project
      per_project.json
      runner.log
    final_summary.md
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue as _queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
PY = sys.executable

# --------------------------------------------------------------------------
# configuration-like constants. Workers live in the sibling rebuttal tree
# because UTRefactor is itself a sibling tool — keeping its scratch state
# alongside the other UTRefactor artefacts avoids a 550 MB duplication in
# llm_smelly_repair_impl/. Outputs (per-project JSON, summaries) still go
# inside llm_smelly_repair_impl/output per the scope rule.
# --------------------------------------------------------------------------

WORKSPACE_ROOT = Path('<ANON_ROOT>/segtr_replication')
DEFAULT_WORKERS_ROOT = (
    WORKSPACE_ROOT / 'rebuttal_experiments' / 'exp3_utrefactor' / 'workers'
)
SF110_ROOT = WORKSPACE_ROOT / 'sf110_projects'


# --------------------------------------------------------------------------
# post-hoc measurement
# --------------------------------------------------------------------------


def _load_baseline_coverage(project: str) -> Optional[Dict[str, Any]]:
    """Return the pristine JaCoCo-before dict for ``project``.

    Looks first at `smell_repair_v2/data/baseline_coverage.json` (dev
    projects only), then falls back to the Phase-4 per-project.json which
    measures the same pristine workdir.
    """
    bc = REPO / 'smell_repair_v2' / 'data' / 'baseline_coverage.json'
    if bc.exists():
        try:
            d = json.load(bc.open())
            if project in d:
                return d[project]
        except Exception:
            pass
    pp = REPO / 'output' / 'runs' / 'phase4_main' / f'project_{project}' / 'per_project.json'
    if pp.exists():
        try:
            data: Any = json.load(pp.open())
            if isinstance(data, list):
                data = data[0] if data else {}
            return data.get('jacoco_before')
        except Exception:
            pass
    return None


def _load_smelly_data(path: Path) -> Dict[str, Any]:
    from smell_repair_v2.analysis.smelly import load_smelly_json
    return load_smelly_json(path)


def _ensure_shared_lib(work_project: Path, log_fh) -> None:
    """Create ``workdir_root/lib/`` with the junit/evosuite/hamcrest jars.

    UTRefactor's orchestrator does ``shutil.copytree(src, dest)`` but does
    not reproduce the parent-level shared-lib layout that Phase 4's
    ``prepare_workdir()`` sets up. SF110 ``build.xml`` resolves
    ``${lib.dir}`` to ``../lib/``, so without this step ``ant
    compile-evosuite`` and ``JUnitCore`` cannot find junit-4.11.jar or
    evosuite-standalone-runtime-1.2.0.jar.
    """
    workdir_root = work_project.parent
    shared_lib = workdir_root / 'lib'
    shared_lib.mkdir(exist_ok=True)
    need = [
        (Path('<ANON_ROOT>/segtr_replication/evosuite-1.2.0/'
              'evosuite-standalone-runtime-1.2.0.jar'),
         'evosuite-standalone-runtime-1.2.0.jar'),
        (Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl/'
              'tools/backup-smelly-evidence/junit-4.11.jar'),
         'junit-4.11.jar'),
        (Path('<ANON_ROOT>/segtr_replication/sf110_projects_pilot/lib/'
              'hamcrest-core-1.3.jar'),
         'hamcrest-core-1.3.jar'),
    ]
    for src, name in need:
        dst = shared_lib / name
        if src.exists() and not dst.exists():
            shutil.copyfile(src, dst)
            print(f'[shared-lib] copied {name}', file=log_fh)
    alias = shared_lib / 'evosuite.jar'
    primary = shared_lib / 'evosuite-standalone-runtime-1.2.0.jar'
    if primary.exists() and not alias.exists():
        shutil.copyfile(primary, alias)


def _run_post_hoc(
    project: str,
    work_project: Path,
    original_smelly: Dict[str, Any],
    after_smelly: Dict[str, Any],
    log_fh,
) -> Dict[str, Any]:
    """Run JaCoCo + class-test-pass on the UTRefactor workdir.

    Returns the Phase-B schema dict (a single element of the
    `per_project.json` list).
    """
    from smell_repair_v2.coverage.jacoco import run_jacoco
    from smell_repair_v2.pipeline_v2 import _measure_class_test_pass

    # Ensure junit/evosuite/hamcrest jars are in place so ant + JUnitCore
    # can find them when invoked against UTRefactor's workdir.
    _ensure_shared_lib(work_project, log_fh)

    out: Dict[str, Any] = {
        'project': project,
        'condition': 'utrefactor',
    }

    # Class-test-pass after (before was measured on pristine SF110 — reuse
    # Phase 4's number since UTRefactor's workdir derives from the same
    # pristine tree via rsync + overlay).
    before_pp = REPO / 'output/runs/phase4_main' / f'project_{project}' / 'per_project.json'
    class_tests_before_n = 0
    class_tests_total = 0
    regressed_before_ref: List[str] = []
    if before_pp.exists():
        try:
            data: Any = json.load(before_pp.open())
            if isinstance(data, list):
                data = data[0] if data else {}
            class_tests_before_n = int(data.get('class_tests_before') or 0)
            class_tests_total = int(data.get('class_tests_total') or 0)
        except Exception:
            pass

    class_tests_after: Dict[str, bool] = {}
    try:
        _measure_class_test_pass(work_project, original_smelly, class_tests_after)
    except Exception as exc:
        print(f'[post-hoc] class-test-pass FAILED for {project}: {exc}', file=log_fh)
        log_fh.flush()

    pass_after = sum(1 for v in class_tests_after.values() if v)
    out['class_tests_before'] = class_tests_before_n
    out['class_tests_after'] = pass_after
    out['class_tests_total'] = class_tests_total or len(class_tests_after)
    # "regressed" = previously passing class now failing. Reference is the
    # Phase 4 before-map (same pristine workdir), so a class present in
    # class_tests_after and false is a regression iff Phase 4 had it true.
    # We don't re-measure before here — class_tests_after set vs total
    # suffices for a coarse count.
    out['regressed_classes'] = sorted([
        c for c, v in class_tests_after.items() if not v
    ])

    # JaCoCo after
    try:
        jacoco_after = run_jacoco(work_project, project_name=project).to_dict()
    except Exception as exc:
        print(f'[post-hoc] jacoco_after FAILED for {project}: {exc}', file=log_fh)
        log_fh.flush()
        jacoco_after = {'error': str(exc)}
    out['jacoco_before'] = _load_baseline_coverage(project)
    out['jacoco_after'] = jacoco_after

    # Smell totals before/after — from the orchestrator's JSON files
    out['smell_totals_before'] = _aggregate_smell_totals(original_smelly)
    out['smell_totals_after'] = _aggregate_smell_totals(after_smelly)
    return out


def _aggregate_smell_totals(smelly_data: Dict[str, Any]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for class_smells in smelly_data.values():
        if not isinstance(class_smells, dict):
            continue
        for smell_name, items in class_smells.items():
            if isinstance(items, list):
                totals[smell_name] = totals.get(smell_name, 0) + len(items)
    return totals


# --------------------------------------------------------------------------
# per-project worker
# --------------------------------------------------------------------------


def _process_one(
    project: str,
    worker_root: Path,
    out_root: Path,
    base_config: Path,
    timeout_sec: int,
) -> Dict[str, Any]:
    """Run UTRefactor + post-hoc for one project. Returns summary dict."""
    sys.path.insert(0, str(REPO))

    exp_root = out_root / 'by_project_tree'
    exp_root.mkdir(parents=True, exist_ok=True)
    proj_out = out_root / f'project_{project}'
    proj_out.mkdir(parents=True, exist_ok=True)
    log_path = proj_out / 'runner.log'

    # Per-project single-line projects file for the orchestrator.
    tf = tempfile.NamedTemporaryFile(
        mode='w', prefix=f'utref_pc_{project}_', suffix='.txt',
        dir=str(proj_out), delete=False,
    )
    tf.write(project + '\n')
    tf.close()

    cmd = [
        PY, str(REPO / 'scripts/rebuttal/run_utrefactor_selected.py'),
        '--base-config', str(base_config),
        '--projects-root', str(SF110_ROOT),
        '--selected-projects-file', tf.name,
        '--experiment-root', str(exp_root),
        '--utrefactor-root', str(worker_root),
    ]

    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'

    t0 = time.monotonic()
    rc: int = 0
    timed_out = False
    orch_reason: str = ''
    try:
        with log_path.open('w', encoding='utf-8') as lf:
            print(f'[cmd] {" ".join(cmd)}', file=lf)
            lf.flush()
            proc = subprocess.run(
                cmd, stdout=lf, stderr=subprocess.STDOUT,
                timeout=timeout_sec, env=env, check=False,
            )
            rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
        timed_out = True
        orch_reason = 'timeout'
    except Exception as exc:
        rc = -2
        orch_reason = f'{type(exc).__name__}: {exc}'

    orch_elapsed = time.monotonic() - t0

    summary: Dict[str, Any] = {
        'project': project,
        'rc': rc,
        'timed_out': timed_out,
        'reason': orch_reason,
        'orch_elapsed_min': round(orch_elapsed / 60.0, 2),
        'out_dir': str(proj_out),
        'worker_root': str(worker_root),
    }

    # Locate the orchestrator's produced artefacts.
    proj_artefacts = exp_root / 'by_project' / project
    before_json = proj_artefacts / f'smelly_{project}.json'
    after_jsons = sorted(proj_artefacts.glob('run_*/reports/smelly_after_*.json'))
    latest_run_dir: Optional[Path] = None
    workdir_path: Optional[Path] = None
    if after_jsons:
        latest_run_dir = after_jsons[-1].parent.parent
        workdir_path = latest_run_dir / 'workdir' / project

    summary['utrefactor_run_dir'] = str(latest_run_dir) if latest_run_dir else None
    summary['has_before'] = before_json.exists()
    summary['has_after'] = bool(after_jsons)
    summary['has_workdir'] = bool(workdir_path and workdir_path.exists())

    # Post-hoc measurement — only when the orchestrator produced a workdir.
    if before_json.exists() and after_jsons and workdir_path and workdir_path.exists():
        post_log = proj_out / 'post_hoc.log'
        try:
            original_smelly = _load_smelly_data(before_json)
            after_smelly = _load_smelly_data(after_jsons[-1])
            with post_log.open('w', encoding='utf-8') as lf:
                print(f'[post-hoc] project={project} workdir={workdir_path}', file=lf)
                phase_b_row = _run_post_hoc(
                    project, workdir_path, original_smelly, after_smelly, lf,
                )
        except Exception as exc:
            phase_b_row = {
                'project': project,
                'condition': 'utrefactor',
                'post_hoc_error': f'{type(exc).__name__}: {exc}',
            }
        (proj_out / 'per_project.json').write_text(
            json.dumps([phase_b_row], indent=2, ensure_ascii=False),
            encoding='utf-8',
        )
        # Hoist key stats to the summary
        pb = phase_b_row
        summary['smells_before'] = sum((pb.get('smell_totals_before') or {}).values())
        summary['smells_after'] = sum((pb.get('smell_totals_after') or {}).values())
        summary['class_tests_before'] = pb.get('class_tests_before')
        summary['class_tests_after'] = pb.get('class_tests_after')
        summary['regressed_classes_n'] = len(pb.get('regressed_classes') or [])
        jb = pb.get('jacoco_before') or {}
        ja = pb.get('jacoco_after') or {}
        summary['jacoco_line_before'] = jb.get('line_coverage')
        summary['jacoco_line_after'] = ja.get('line_coverage')
    elif timed_out:
        summary['note'] = 'orchestrator_timed_out_before_measurement'
    else:
        summary['note'] = 'orchestrator_did_not_produce_artefacts'

    # Cleanup temp file
    try:
        os.unlink(tf.name)
    except Exception:
        pass

    return summary


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


@dataclass
class RunnerState:
    results: List[Dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: bool = False


def _check_alerts(state: RunnerState, alert_log: Path,
                  fired: Set[str], n_total: int) -> None:
    with state.lock:
        timeouts = [r for r in state.results if r.get('timed_out')]
        failed = [r for r in state.results
                  if (not r.get('timed_out')) and r.get('rc') not in (0, None)]
        fires: List[tuple[str, str]] = []
        if len(timeouts) >= 4 and 'timeout>=4' not in fired:
            fires.append(('timeout>=4',
                          f'{len(timeouts)} timeouts: '
                          f'{[r["project"] for r in timeouts]}'))
        if len(failed) >= 3 and 'failed>=3' not in fired:
            fires.append(('failed>=3',
                          f'{len(failed)} failed: '
                          f'{[(r["project"], r.get("rc")) for r in failed]}'))
        for f in fires:
            fired.add(f[0])
        results_snapshot = list(state.results)

    if fires:
        with alert_log.open('a', encoding='utf-8') as fh:
            for key, msg in fires:
                print(f'  [ALERT {key}] {msg}', flush=True)
                fh.write(json.dumps({
                    'ts': datetime.now().isoformat(timespec='seconds'),
                    'key': key, 'msg': msg,
                }) + '\n')


def _interim_summary(out_dir: Path, state: RunnerState,
                     in_flight: Set[str], t_start: float,
                     n_total: int, interval_idx: int) -> None:
    with state.lock:
        results = list(state.results)
    completed = [r for r in results if not r.get('timed_out') and r.get('rc') == 0]
    timed_out = [r for r in results if r.get('timed_out')]
    failed = [r for r in results
              if (not r.get('timed_out')) and r.get('rc') not in (0, None)]
    elapsed_min = (time.time() - t_start) / 60.0
    lines = [
        f'# Interim summary #{interval_idx}  (phaseC utrefactor)',
        '',
        f'- elapsed: {elapsed_min:.1f} min',
        f'- completed: {len(completed)}/{n_total}  timed_out: {len(timed_out)}  '
        f'failed: {len(failed)}  in_flight: {len(in_flight)}',
        '',
        '## per-project status',
        '',
        '| project | status | orch elapsed | smells before → after | classes before → after |',
        '|---|---|---:|---|---|',
    ]
    for r in sorted(results, key=lambda x: int(x['project'].split('_')[0])):
        st = 'timeout' if r.get('timed_out') else (
            'ok' if r.get('rc') == 0 else f'fail(rc={r.get("rc")})')
        sb, sa = r.get('smells_before'), r.get('smells_after')
        cb, ca = r.get('class_tests_before'), r.get('class_tests_after')
        lines.append(
            f'| {r["project"]} | {st} | {r.get("orch_elapsed_min","—")} min '
            f'| {sb if sb is not None else "—"} → {sa if sa is not None else "—"} '
            f'| {cb if cb is not None else "—"} → {ca if ca is not None else "—"} |'
        )
    if in_flight:
        lines.append('')
        lines.append('## in-flight')
        for p in sorted(in_flight, key=lambda x: int(x.split('_')[0])):
            lines.append(f'- {p}')
    (out_dir / f'interim_summary_{interval_idx:02d}.md').write_text(
        '\n'.join(lines) + '\n', encoding='utf-8')


def _worker_loop(
    worker_root: Path,
    project_queue: '_queue.Queue[Optional[str]]',
    state: RunnerState,
    in_flight: Set[str],
    in_flight_lock: threading.Lock,
    out_root: Path,
    base_config: Path,
    timeout_sec: int,
) -> None:
    while True:
        try:
            project = project_queue.get(timeout=1.0)
        except _queue.Empty:
            if state.stop:
                return
            continue
        if project is None:
            return
        with in_flight_lock:
            in_flight.add(project)
        print(f'[worker:{worker_root.parent.name}] start: {project}', flush=True)
        try:
            r = _process_one(
                project=project, worker_root=worker_root,
                out_root=out_root, base_config=base_config,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            r = {
                'project': project, 'rc': -3,
                'reason': f'{type(exc).__name__}: {exc}',
                'timed_out': False, 'orch_elapsed_min': 0,
            }
        with in_flight_lock:
            in_flight.discard(project)
        with state.lock:
            state.results.append(r)
        status = 'TIMEOUT' if r.get('timed_out') else (
            '✓' if r.get('rc') == 0 else f'FAIL(rc={r.get("rc")})')
        print(
            f'[{status}] {project}  orch_elapsed='
            f'{r.get("orch_elapsed_min",0)}min  '
            f'smells={r.get("smells_before")}→{r.get("smells_after")}',
            flush=True,
        )


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def _read_projects(csv_path: Path) -> List[str]:
    rows = list(csv.DictReader(csv_path.open()))
    if 'project' not in (rows[0].keys() if rows else []):
        # fallback: single-column or header-less file
        return [ln.strip() for ln in csv_path.open()
                if ln.strip() and not ln.strip().startswith('#')]
    return [r['project'] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--projects-csv', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--base-config', type=Path, required=True,
                    help='UTRefactor YAML config (e.g. config_openrouter_20b.yaml)')
    ap.add_argument('--parallel', type=int, default=4)
    ap.add_argument('--timeout-min', type=int, default=60)
    ap.add_argument('--workers-root', type=Path, default=DEFAULT_WORKERS_ROOT)
    ap.add_argument('--interim-interval-min', type=int, default=30)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    projects_all = _read_projects(args.projects_csv)
    # Resume detection — per-project per_project.json
    to_run: List[str] = []
    already: List[str] = []
    for p in projects_all:
        pj = args.out / f'project_{p}' / 'per_project.json'
        if pj.exists() and not args.force:
            already.append(p)
        else:
            to_run.append(p)
    print(f'[runner] total={len(projects_all)}  resume(skip)={len(already)}  '
          f'to_run={len(to_run)}  parallel={args.parallel}  '
          f'timeout={args.timeout_min}min')

    worker_roots = sorted([
        d / 'testsmellrefactoring'
        for d in args.workers_root.iterdir()
        if d.is_dir() and (d / 'testsmellrefactoring').exists()
    ])
    if not worker_roots:
        print(f'[runner] FATAL: no worker checkouts found under '
              f'{args.workers_root}. Run scripts/rebuttal/prepare_utrefactor_workers.sh first.',
              file=sys.stderr)
        return 2
    if len(worker_roots) < args.parallel:
        print(f'[runner] WARNING: only {len(worker_roots)} workers, '
              f'requested parallel={args.parallel}; capping to '
              f'{len(worker_roots)}')
        args.parallel = len(worker_roots)
    worker_roots = worker_roots[:args.parallel]
    print(f'[runner] workers: {[str(w) for w in worker_roots]}')

    state = RunnerState()

    # Seed already-done into results for interim/final reporting.
    for p in already:
        pj = args.out / f'project_{p}' / 'per_project.json'
        r: Dict[str, Any] = {'project': p, 'rc': 0, 'timed_out': False,
                             'orch_elapsed_min': None}
        try:
            data: Any = json.load(pj.open())
            if isinstance(data, list):
                data = data[0] if data else {}
            r['smells_before'] = sum(
                (data.get('smell_totals_before') or {}).values()
            )
            r['smells_after'] = sum(
                (data.get('smell_totals_after') or {}).values()
            )
            r['class_tests_before'] = data.get('class_tests_before')
            r['class_tests_after'] = data.get('class_tests_after')
        except Exception:
            pass
        state.results.append(r)

    def _sig(*_):
        state.stop = True
        print('[runner] signal — stopping admissions', flush=True)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    project_queue: _queue.Queue = _queue.Queue()
    for p in to_run:
        project_queue.put(p)

    alert_log = args.out / 'alerts.jsonl'
    alert_log.touch()
    fired_alerts: Set[str] = set()
    in_flight: Set[str] = set()
    in_flight_lock = threading.Lock()

    threads: List[threading.Thread] = []
    for wr in worker_roots:
        th = threading.Thread(
            target=_worker_loop,
            args=(wr, project_queue, state, in_flight, in_flight_lock,
                  args.out, args.base_config, args.timeout_min * 60),
            daemon=True,
        )
        th.start()
        threads.append(th)

    t_start = time.time()
    interim_marker = t_start
    interim_idx = 0

    while any(t.is_alive() for t in threads):
        time.sleep(5.0)
        # Flush checkpoint periodically
        with state.lock:
            results_snap = list(state.results)
        _write_checkpoint(args.out, results_snap)
        _check_alerts(state, alert_log, fired_alerts, len(projects_all))
        if time.time() - interim_marker >= args.interim_interval_min * 60:
            interim_idx += 1
            _interim_summary(args.out, state, in_flight,
                             t_start, len(projects_all), interim_idx)
            interim_marker = time.time()
        if project_queue.empty() and not in_flight:
            break

    # Signal workers to exit
    for _ in threads:
        project_queue.put(None)
    for t in threads:
        t.join(timeout=30)

    interim_idx += 1
    _interim_summary(args.out, state, set(), t_start, len(projects_all), interim_idx)
    with state.lock:
        _write_checkpoint(args.out, list(state.results))

    wall_min = (time.time() - t_start) / 60.0
    completed = [r for r in state.results if r.get('rc') == 0 and not r.get('timed_out')]
    timed_out = [r for r in state.results if r.get('timed_out')]
    failed = [r for r in state.results
              if (not r.get('timed_out')) and r.get('rc') not in (0, None)]
    print(f'\n[runner] done in {wall_min:.1f} min '
          f'(completed={len(completed)}  timed_out={len(timed_out)}  failed={len(failed)})')
    return 0


def _write_checkpoint(out_dir: Path, results: List[Dict[str, Any]]) -> None:
    ck = {
        'completed': sorted([r['project'] for r in results
                             if r.get('rc') == 0 and not r.get('timed_out')]),
        'timed_out': sorted([r['project'] for r in results if r.get('timed_out')]),
        'failed': sorted([r['project'] for r in results
                          if (not r.get('timed_out')) and r.get('rc') not in (0, None)]),
        'all_results': results,
        'ts': datetime.now().isoformat(timespec='seconds'),
    }
    (out_dir / 'checkpoint.json').write_text(
        json.dumps(ck, indent=2, ensure_ascii=False), encoding='utf-8',
    )


if __name__ == '__main__':
    sys.exit(main())

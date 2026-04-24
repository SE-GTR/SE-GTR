#!/usr/bin/env python3
"""Phase B–E parallel runner for RQ3 conditions.

Spawns one ``cli_v2`` subprocess per project, pooled at ``N`` workers,
for a fixed ``--condition``. Keeps the Phase-4 infrastructure untouched
— this is a dedicated runner for RQ3 experiments on the selected 15
projects.

Invocation::

  python3 scripts/run_rq3_parallel.py \\
      --condition naive_llm \\
      --projects-csv output/runs/rq3_experiments/selection/selected_15.csv \\
      --out output/runs/rq3_experiments/naive_llm \\
      --parallel 4 \\
      --timeout-min 60 \\
      --cost-budget 15.0

Outputs:

  <out>/
    checkpoint.json            # {completed, timed_out, failed}
    alerts.jsonl               # one line per alert condition trip
    interim_summary_XX.md      # every 30 min
    project_<name>/            # one per project (cli_v2 --out target)
      per_project.json
      raw_results.jsonl
      naive_rewrites/<proj>/   # before/after method sources
    final_summary.md           # once all projects terminal

Resume-friendly: a project whose ``per_project.json`` already exists is
skipped unless ``--force`` is passed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
PY = sys.executable


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------


def _run_one_project(
    project: str,
    condition: str,
    out_dir: str,
    config_path: Optional[str],
    cost_budget: float,
    timeout_sec: int,
) -> Dict[str, Any]:
    """Run one ``cli_v2`` invocation, enforcing the timeout.

    Returns the summary dict the parent writes into the checkpoint. Only
    primitive JSON-safe types (subprocesses can't share state across
    workers).
    """
    proj_out = Path(out_dir) / f'project_{project}'
    proj_out.mkdir(parents=True, exist_ok=True)
    log_path = proj_out / 'runner.log'

    cmd = [
        PY, '-m', 'smell_repair_v2.cli_v2',
        '--projects', project,
        '--condition', condition,
        '--enable-project-jacoco',
        '--out', str(proj_out),
        '--cost-budget', str(cost_budget),
    ]
    if config_path:
        cmd += ['--config', config_path]

    # Python subprocess buffering: force unbuffered so per-line output
    # lands in the log while the subprocess is running.
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'

    t0 = time.monotonic()
    rc: int
    reason: str = ''
    timed_out = False
    with log_path.open('w', encoding='utf-8') as lf:
        try:
            proc = subprocess.run(
                cmd, stdout=lf, stderr=subprocess.STDOUT,
                timeout=timeout_sec, env=env, check=False,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -1
            timed_out = True
            reason = 'timeout'
        except Exception as exc:
            rc = -2
            reason = f'{type(exc).__name__}: {exc}'

    elapsed = time.monotonic() - t0

    # Pull cost + accept info out of the run's per_project.json if present.
    out: Dict[str, Any] = {
        'project': project,
        'rc': rc,
        'timed_out': timed_out,
        'reason': reason,
        'elapsed_min': round(elapsed / 60.0, 2),
        'out_dir': str(proj_out),
    }
    pj = proj_out / 'per_project.json'
    if pj.exists():
        try:
            d: Any = json.load(pj.open())
            if isinstance(d, list):
                d = d[0] if d else {}
            out['class_tests_before'] = d.get('class_tests_before')
            out['class_tests_after'] = d.get('class_tests_after')
            out['regressed_classes'] = d.get('regressed_classes') or []
            if condition == 'naive_llm' and 'naive' in d:
                naive = d['naive']
                out['methods_attempted'] = naive.get('total_methods_attempted')
                out['plans_submitted'] = naive.get('submitted_to_validator')
                out['accepted'] = naive.get('accepted')
                out['cost_usd'] = naive.get('cost_usd')
                out['llm_calls'] = naive.get('llm_calls')
                out['rejected'] = naive.get('rejected')
            jb = d.get('jacoco_before') or {}
            ja = d.get('jacoco_after') or {}
            out['jacoco_line_before'] = jb.get('line_coverage')
            out['jacoco_line_after'] = ja.get('line_coverage')
            sb = d.get('smell_totals_before') or {}
            sa = d.get('smell_totals_after') or {}
            out['smells_before_total'] = sum(sb.values())
            out['smells_after_total'] = sum(sa.values())
        except Exception as e:
            out['parse_error'] = f'{type(e).__name__}: {e}'
    return out


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------


def _check_alerts(
    results: List[Dict[str, Any]],
    n_total: int,
    alert_log: Path,
    already_fired: Set[str],
) -> None:
    fired: List[tuple[str, str]] = []
    timeouts = [r for r in results if r.get('timed_out')]
    failed = [
        r for r in results
        if (not r.get('timed_out')) and (r.get('rc') not in (0, None))
    ]

    if len(timeouts) >= 2 and 'timeout>=2' not in already_fired:
        fired.append((
            'timeout>=2',
            f"{len(timeouts)} projects timed out so far: "
            f"{[r['project'] for r in timeouts]}",
        ))

    if len(failed) >= 3 and 'failed>=3' not in already_fired:
        fired.append((
            'failed>=3',
            f"{len(failed)} projects failed (rc != 0, not timeout): "
            f"{[(r['project'], r.get('rc')) for r in failed]}",
        ))

    # Cost projection — naive smoke was $0.03 / project. Alert if any
    # *completed* project exceeded $2 (way over budget).
    for r in results:
        cost = r.get('cost_usd')
        if cost and cost > 2.0 and f'cost_high:{r["project"]}' not in already_fired:
            fired.append((
                f'cost_high:{r["project"]}',
                f'{r["project"]} spent ${cost:.2f} (budget $2/project)',
            ))

    if fired:
        with alert_log.open('a', encoding='utf-8') as f:
            for key, msg in fired:
                print(f'  [ALERT {key}] {msg}', flush=True)
                f.write(json.dumps({
                    'ts': datetime.now().isoformat(timespec='seconds'),
                    'key': key, 'msg': msg,
                }) + '\n')
                already_fired.add(key)


# --------------------------------------------------------------------------
# interim summary
# --------------------------------------------------------------------------


def _interim_summary(
    run_dir: Path,
    results: List[Dict[str, Any]],
    in_flight: Set[str],
    t_start: float,
    n_total: int,
    interval_idx: int,
) -> None:
    completed = [r for r in results if not r.get('timed_out') and r.get('rc') == 0]
    timed_out = [r for r in results if r.get('timed_out')]
    failed = [r for r in results
              if (not r.get('timed_out')) and r.get('rc') not in (0, None)]

    total_cost = sum((r.get('cost_usd') or 0.0) for r in results)
    total_calls = sum((r.get('llm_calls') or 0) for r in results)
    accept_rates = []
    for r in completed:
        s = r.get('plans_submitted')
        a = r.get('accepted')
        if s and a is not None:
            accept_rates.append(a / s * 100.0 if s else 0.0)
    mean_accept = sum(accept_rates) / len(accept_rates) if accept_rates else 0.0

    elapsed_min = (time.time() - t_start) / 60.0
    lines = [
        f'# Interim summary #{interval_idx} — condition={results[0]["_condition"] if results else "?"}',
        '',
        f'- elapsed: {elapsed_min:.1f} min',
        f'- completed: {len(completed)}/{n_total}  timed_out: {len(timed_out)}  '
        f'failed: {len(failed)}  in_flight: {len(in_flight)}',
        f'- cost so far: ${total_cost:.4f}  LLM calls: {total_calls}',
        f'- mean accept rate (completed): {mean_accept:.1f}%',
        '',
        '## per-project status',
        '',
        '| project | status | elapsed | cost | accepted/submitted | accept% |',
        '|---|---|---:|---:|---:|---:|',
    ]
    for r in sorted(results, key=lambda x: int(x['project'].split('_')[0])):
        st = 'timeout' if r.get('timed_out') else (
            'ok' if r.get('rc') == 0 else f'fail(rc={r.get("rc")})'
        )
        sub = r.get('plans_submitted') or 0
        acc = r.get('accepted') or 0
        ar = (acc / sub * 100.0) if sub else 0.0
        lines.append(
            f'| {r["project"]} | {st} | {r.get("elapsed_min","—")} min '
            f'| ${(r.get("cost_usd") or 0):.4f} | {acc}/{sub} | {ar:.1f}% |'
        )
    if in_flight:
        lines.append('')
        lines.append('## in-flight')
        for p in sorted(in_flight, key=lambda x: int(x.split('_')[0])):
            lines.append(f'- {p}')
    path = run_dir / f'interim_summary_{interval_idx:02d}.md'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


STOP = False


def _sigterm(*_):
    global STOP
    STOP = True
    print('[runner] signal received — stopping admissions', flush=True)


def main() -> int:
    global STOP
    ap = argparse.ArgumentParser()
    ap.add_argument('--condition', required=True,
                    choices=['naive_llm', 'utrefactor', 't1_only', 't1_t2',
                             't1_t2_t3', 'full'])
    ap.add_argument('--projects-csv', required=True, type=Path,
                    help='CSV with a "project" column')
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--parallel', type=int, default=4)
    ap.add_argument('--timeout-min', type=int, default=60)
    ap.add_argument('--cost-budget', type=float, default=15.0,
                    help='per-project cost cap forwarded to cli_v2')
    ap.add_argument('--config', type=str, default=None)
    ap.add_argument('--force', action='store_true',
                    help='ignore existing per_project.json and re-run')
    ap.add_argument('--interim-interval-min', type=int, default=30)
    args = ap.parse_args()

    run_dir = args.out.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    projects = [r['project'] for r in csv.DictReader(args.projects_csv.open())]
    if not projects:
        print('no projects', file=sys.stderr); return 2

    # Resume detection
    to_run: List[str] = []
    already_done: List[str] = []
    for p in projects:
        pj = run_dir / f'project_{p}' / 'per_project.json'
        if pj.exists() and not args.force:
            already_done.append(p)
        else:
            to_run.append(p)
    print(f'[runner] total={len(projects)}  resume(skip)={len(already_done)}  '
          f'to_run={len(to_run)}  parallel={args.parallel}  '
          f'timeout={args.timeout_min}min  condition={args.condition}')
    if already_done:
        print(f'[runner] resuming — already done: {already_done}')

    signal.signal(signal.SIGINT, _sigterm)
    signal.signal(signal.SIGTERM, _sigterm)

    alerts_log = run_dir / 'alerts.jsonl'
    alerts_log.touch()
    fired_alerts: Set[str] = set()

    # Seed results with already-done projects (reload from disk)
    results: List[Dict[str, Any]] = []
    for p in already_done:
        out = {'project': p, '_condition': args.condition}
        pj = run_dir / f'project_{p}' / 'per_project.json'
        try:
            d = json.load(pj.open())
            if isinstance(d, list): d = d[0]
            out['rc'] = 0
            out['timed_out'] = False
            out['elapsed_min'] = None
            if args.condition == 'naive_llm' and 'naive' in d:
                out['cost_usd'] = d['naive'].get('cost_usd')
                out['llm_calls'] = d['naive'].get('llm_calls')
                out['plans_submitted'] = d['naive'].get('submitted_to_validator')
                out['accepted'] = d['naive'].get('accepted')
                out['rejected'] = d['naive'].get('rejected')
                out['methods_attempted'] = d['naive'].get('total_methods_attempted')
        except Exception:
            pass
        results.append(out)

    t_start = time.time()
    interim_marker = t_start
    interim_idx = 0

    timeout_sec = args.timeout_min * 60

    with ProcessPoolExecutor(max_workers=args.parallel) as pool:
        futs: Dict[Future, str] = {}
        it = iter(to_run)

        for _ in range(min(args.parallel, len(to_run))):
            try:
                p = next(it)
            except StopIteration:
                break
            futs[pool.submit(
                _run_one_project, p, args.condition, str(run_dir),
                args.config, args.cost_budget, timeout_sec,
            )] = p
            print(f'[runner] launched: {p}')

        in_flight: Set[str] = set(futs.values())

        while futs:
            done = [f for f in list(futs) if f.done()]
            if not done:
                time.sleep(5.0)
                # Interim summary
                if time.time() - interim_marker >= args.interim_interval_min * 60:
                    interim_idx += 1
                    _interim_summary(run_dir, results, in_flight,
                                     t_start, len(projects), interim_idx)
                    interim_marker = time.time()
                if STOP:
                    break
                continue

            for f in done:
                proj = futs.pop(f)
                in_flight.discard(proj)
                try:
                    r = f.result()
                except Exception as exc:
                    r = {'project': proj, 'rc': -3,
                         'reason': f'{type(exc).__name__}: {exc}',
                         'timed_out': False, 'elapsed_min': 0}
                r['_condition'] = args.condition
                results.append(r)
                status = 'TIMEOUT' if r.get('timed_out') else (
                    '✓' if r.get('rc') == 0 else f'FAIL(rc={r.get("rc")})'
                )
                print(f'[{status}] {proj}  elapsed={r.get("elapsed_min")}min  '
                      f'cost=${(r.get("cost_usd") or 0):.4f}  '
                      f'accept={r.get("accepted","—")}/{r.get("plans_submitted","—")}',
                      flush=True)

                # Persist checkpoint after every completion
                ck = {
                    'completed': [x['project'] for x in results if x.get('rc') == 0],
                    'timed_out': [x['project'] for x in results if x.get('timed_out')],
                    'failed': [x['project'] for x in results
                               if (not x.get('timed_out')) and x.get('rc') not in (0, None)],
                    'all_results': results,
                    'ts': datetime.now().isoformat(timespec='seconds'),
                }
                (run_dir / 'checkpoint.json').write_text(
                    json.dumps(ck, indent=2, ensure_ascii=False),
                    encoding='utf-8',
                )

                _check_alerts(results, len(projects), alerts_log, fired_alerts)

                if not STOP:
                    try:
                        p = next(it)
                        futs[pool.submit(
                            _run_one_project, p, args.condition, str(run_dir),
                            args.config, args.cost_budget, timeout_sec,
                        )] = p
                        in_flight.add(p)
                        print(f'[runner] launched: {p}')
                    except StopIteration:
                        pass

    # Final summary
    interim_idx += 1
    _interim_summary(run_dir, results, set(), t_start, len(projects), interim_idx)
    wall_min = (time.time() - t_start) / 60.0
    print(f'\n[runner] done in {wall_min:.1f} min '
          f'(completed={len([r for r in results if r.get("rc")==0])}, '
          f'timed_out={len([r for r in results if r.get("timed_out")])}, '
          f'failed={len([r for r in results if (not r.get("timed_out")) and r.get("rc") not in (0, None)])})')
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Phase 4.4 — PIT mutation testing on the Phase-4 SE-GTR v2 workdirs.

Target cohort: v1 PIT-82 ∩ Phase-4 completed = 76 projects (see
``project_classification.csv``). For each, invokes ``measure_pit.py``
against the Phase-4 workdir (``output/runs/phase4_main/project_<p>/<p>``)
with the v1 Phase-6-validated config:

    PIT 1.17.4
    --excludedClasses '*_ESTest*,*_ESTest_scaffolding*'
    --threads 4
    timeout 30 min (wall)

Parallel: N=4 projects × 4 PIT threads each = 16 active threads,
comfortable on a 20-core box.

Outputs per project:

    output/runs/phase4_main/pit/per_project/<p>/
        mutations.xml     # from PIT
        index.html        # PIT html report
        pit_run.log       # PIT stdout/stderr
        score.json        # our parsed summary
"""
from __future__ import annotations

import argparse
import csv
import json
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
PHASE4_DIR = REPO_ROOT / 'output' / 'runs' / 'phase4_main'
CLASSIFICATION_CSV = PHASE4_DIR / 'project_classification.csv'
V1_PIT_CSV = REPO_ROOT / 'output' / 'analysis_pit' / 'rq3_final' / 'before_vs_after.csv'
PIT_HOME = REPO_ROOT / 'tools' / 'pit_1_17_4'
PIT_OUT_DIR = PHASE4_DIR / 'pit' / 'per_project'
MEASURE_PIT = REPO_ROOT / 'scripts' / 'metrics' / 'measure_pit.py'

TIMEOUT_SEC = 30 * 60  # spec: 30 min per project
PARALLEL = 4


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------


def load_pit_targets() -> List[str]:
    """v1 PIT 82 ∩ Phase-4 pipeline completed."""
    v1_set: Set[str] = set()
    with V1_PIT_CSV.open() as f:
        for row in csv.DictReader(f):
            v1_set.add(row['project'])

    checkpoint = json.loads((PHASE4_DIR / 'checkpoint.json').read_text())
    completed = set(checkpoint['completed'])
    return sorted(v1_set & completed,
                  key=lambda p: int(p.split('_')[0]))


# ---------------------------------------------------------------------------
# Score parsing
# ---------------------------------------------------------------------------


def parse_mutation_score(mutations_xml: Path) -> Dict[str, Any]:
    """Compute overall + per-status counts from PIT's mutations.xml."""
    if not mutations_xml.exists():
        return {'ok': False, 'reason': 'no_mutations_xml'}
    try:
        tree = ET.parse(mutations_xml)
    except Exception as e:
        return {'ok': False, 'reason': f'parse_error:{e}'}
    root = tree.getroot()
    counts: Dict[str, int] = {}
    total = 0
    killed = 0
    for m in root.iter('mutation'):
        status = (m.get('status') or 'UNKNOWN').upper()
        counts[status] = counts.get(status, 0) + 1
        total += 1
        # PIT statuses: KILLED, SURVIVED, TIMED_OUT, MEMORY_ERROR, NO_COVERAGE,
        #               NON_VIABLE, RUN_ERROR. v1 paper counts KILLED as the
        #               mutation score numerator (as does PIT html report).
        if status == 'KILLED':
            killed += 1
    score_pct = (killed / total * 100.0) if total else 0.0
    return {
        'ok': True,
        'total_mutants': total,
        'killed': killed,
        'score_pct': round(score_pct, 4),
        'status_counts': counts,
    }


# ---------------------------------------------------------------------------
# Worker (runs in child process)
# ---------------------------------------------------------------------------


def _run_pit_one(project: str, timeout_sec: int) -> Dict[str, Any]:
    """Invoke measure_pit.py for one project; return a summary dict.

    Dev-reuse projects (1_tullibee, 29_apbsmem) only had their aggregated
    artefacts copied into Phase 4's layout — the actual Ant workdir lives
    in the Phase 2.4c run, which is the same SE-GTR v2 output we want PIT
    to measure. Fall back to that path when build.xml is missing here.
    """
    primary = PHASE4_DIR / f'project_{project}' / project
    fallback = REPO_ROOT / 'output' / 'runs' / 'phase2_4c_initial' / project
    wd = primary if (primary / 'build.xml').exists() else fallback

    out_dir = PIT_OUT_DIR / project
    out_dir.mkdir(parents=True, exist_ok=True)

    if not (wd / 'build.xml').exists():
        return {
            'project': project, 'status': 'no_workdir',
            'elapsed_sec': 0, 'out_dir': str(out_dir),
            'stdout_tail': f'no build.xml under {primary} or {fallback}',
        }

    cmd = [
        sys.executable, str(MEASURE_PIT),
        '--project', str(wd),
        '--out', str(out_dir),
        '--pitest-home', str(PIT_HOME),
        '--threads', '4',
        # v1 Phase 6 config — mutators default, excludedClasses via
        # explicit PIT CLI arg below
        '--pit-extra-args',
        "--excludedClasses '*_ESTest*,*_ESTest_scaffolding*'",
        '--filter-cut-jars',    # align with v1
        # Phase 4 repair can leave a few ESTests failing pre-mutation
        # (rare but happens e.g. under strict NARV-guard cascades); PIT
        # refuses to run when the suite isn't green. `--green-tests-only`
        # triggers `filter_passing_tests` → PIT only receives the green
        # subset, same strategy v1 used.
        '--green-tests-only',
    ]

    t0 = time.monotonic()
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

    score_info = parse_mutation_score(out_dir / 'mutations.xml')
    out = {
        'project': project,
        'rc': rc,
        'elapsed_sec': round(elapsed, 2),
        'timed_out': (rc == -1),
        'out_dir': str(out_dir),
        'stdout_tail': stdout_tail[-1200:],
        **{f'pit_{k}': v for k, v in score_info.items()},
    }
    (out_dir / 'score.json').write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8'
    )
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--parallel', type=int, default=PARALLEL)
    ap.add_argument('--timeout-sec', type=int, default=TIMEOUT_SEC)
    ap.add_argument('--only', nargs='*', default=None,
                    help='Optionally restrict to these projects (smoke test).')
    ap.add_argument('--resume', action='store_true',
                    help='Skip projects that already have score.json.')
    args = ap.parse_args()

    PIT_OUT_DIR.mkdir(parents=True, exist_ok=True)

    targets = load_pit_targets()
    if args.only:
        targets = [p for p in targets if p in set(args.only)]
    if args.resume:
        # skip those that already produced a score
        targets = [p for p in targets
                   if not (PIT_OUT_DIR / p / 'score.json').exists()]

    print(f'[pit] targets: {len(targets)} projects')
    print(f'[pit] parallel N={args.parallel}, per-project wall timeout={args.timeout_sec}s')
    print(f'[pit] output: {PIT_OUT_DIR}')

    summary_path = PHASE4_DIR / 'pit' / 'phase4_pit_summary.jsonl'
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    stop_flag = False
    def _sig(signum, frame):
        nonlocal stop_flag
        print(f'\n[pit] signal {signum} — draining', flush=True)
        stop_flag = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    t_start = time.time()
    results: List[Dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.parallel) as pool, \
            summary_path.open('w', encoding='utf-8') as sf:
        futures: Dict[Future, str] = {}
        iter_pending = iter(targets)
        # Prime
        for _ in range(min(args.parallel, len(targets))):
            try:
                p = next(iter_pending)
            except StopIteration:
                break
            futures[pool.submit(_run_pit_one, p, args.timeout_sec)] = p

        while futures:
            done = [f for f in list(futures) if f.done()]
            if not done:
                time.sleep(5.0)
                if stop_flag:
                    break
                continue
            for f in done:
                proj = futures.pop(f)
                try:
                    res = f.result()
                except Exception as exc:
                    res = {'project': proj, 'rc': -3,
                           'stdout_tail': f'{type(exc).__name__}: {exc}'}
                results.append(res)
                sf.write(json.dumps(res, ensure_ascii=False) + '\n')
                sf.flush()
                ok = (res.get('rc') == 0
                      and res.get('pit_ok')
                      and res.get('pit_total_mutants', 0) > 0)
                marker = '✓' if ok else ('TIMEOUT' if res.get('timed_out') else 'FAIL')
                score = res.get('pit_score_pct')
                print(
                    f'[{marker}] {proj}  '
                    f'elapsed={res.get("elapsed_sec",0)/60:.1f}min  '
                    f'mutants={res.get("pit_total_mutants","—")}  '
                    f'score={score if score is not None else "—"}',
                    flush=True,
                )
                if not stop_flag:
                    try:
                        p = next(iter_pending)
                        futures[pool.submit(_run_pit_one, p, args.timeout_sec)] = p
                    except StopIteration:
                        pass

    elapsed_h = (time.time() - t_start) / 3600.0
    ok_count = sum(1 for r in results
                   if r.get('rc') == 0 and r.get('pit_ok')
                   and r.get('pit_total_mutants', 0) > 0)
    fail_count = len(results) - ok_count
    print(f'\n[pit] done in {elapsed_h:.2f}h  ok={ok_count}  fail={fail_count}')
    print(f'[pit] summary jsonl: {summary_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Phase 4.5 — pristine-workdir PIT measurement under v2 (1.17.4) config.

Runs the same `measure_pit.py` invocation as the Phase-4 v2-after PIT, but
against the **pristine SF110 workdirs** (`<ANON_ROOT>/segtr_replication/sf110_projects/<p>`).
This yields an apples-to-apples baseline: both sides share PIT 1.17.4 and
the identical default-mutator set. The paper's Table 7 then compares
v2 pristine (this run) vs v2 after (Phase 4.4).

Outputs:
    output/runs/phase4_main/pit/pristine_v2pit/<project>/
        mutations.xml
        pit_run.log
        score.json
    output/runs/phase4_main/pit/pristine_v2pit_summary.jsonl

Parallelism: default N=2 while the retry run (bwtna9j96) is still using
N=4. When retry finishes, bump --parallel 4.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
SF110 = Path('<ANON_ROOT>/segtr_replication/sf110_projects')
PIT_HOME = REPO / 'tools' / 'pit_1_17_4'
PHASE4_DIR = REPO / 'output' / 'runs' / 'phase4_main'
MEASURE_PIT = REPO / 'scripts' / 'metrics' / 'measure_pit.py'
OUT_DIR = PHASE4_DIR / 'pit' / 'pristine_v2pit'

V1_CSV = REPO / 'output' / 'analysis_pit' / 'rq3_final' / 'before_vs_after.csv'


def load_targets() -> List[str]:
    """v1 PIT-82 ∩ Phase-4 pipeline completed. Same cohort as v2 after."""
    v1 = set()
    with V1_CSV.open() as f:
        for row in csv.DictReader(f):
            v1.add(row['project'])
    ck = json.loads((PHASE4_DIR / 'checkpoint.json').read_text())
    completed = set(ck['completed'])
    return sorted(v1 & completed, key=lambda p: int(p.split('_')[0]))


def parse_score(xml_path: Path) -> Dict[str, Any]:
    if not xml_path.exists():
        return {'ok': False, 'reason': 'no_mutations_xml'}
    try:
        root = ET.parse(xml_path).getroot()
    except Exception as e:
        return {'ok': False, 'reason': f'parse:{e}'}
    counts: Dict[str, int] = {}
    total = killed = 0
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


def _run_one(project: str, timeout_sec: int) -> Dict[str, Any]:
    wd = SF110 / project
    out_dir = OUT_DIR / project
    out_dir.mkdir(parents=True, exist_ok=True)

    if not (wd / 'build.xml').exists():
        return {'project': project, 'status': 'no_workdir',
                'elapsed_sec': 0, 'out_dir': str(out_dir)}

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
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout_sec, check=False,
        )
        elapsed = time.monotonic() - t0
        rc = proc.returncode
        tail = (proc.stdout or '')[-2000:]
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - t0
        rc = -1
        tail = ((e.output or '') if isinstance(e.output, str) else '')[-2000:]
        (out_dir / 'pit_run.log').write_text(
            f'[runner] pristine PIT wall timeout after {timeout_sec}s\n{tail}',
            encoding='utf-8',
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        rc = -2
        tail = f'{type(exc).__name__}: {exc}'

    score = parse_score(out_dir / 'mutations.xml')
    out = {
        'project': project,
        'rc': rc,
        'elapsed_sec': round(elapsed, 2),
        'timed_out': rc == -1,
        'out_dir': str(out_dir),
        'stdout_tail': tail[-1200:],
        **{f'pit_{k}': v for k, v in score.items()},
    }
    (out_dir / 'score.json').write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    return out


STOP = False


def main() -> int:
    global STOP
    ap = argparse.ArgumentParser()
    ap.add_argument('--parallel', type=int, default=2)
    ap.add_argument('--timeout-sec', type=int, default=90 * 60)
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--resume', action='store_true', default=True)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = load_targets()
    if args.only:
        targets = [p for p in targets if p in set(args.only)]
    if args.resume:
        targets = [p for p in targets
                   if not (OUT_DIR / p / 'score.json').exists()]

    print(f'[pristine-pit] targets: {len(targets)}  parallel=N={args.parallel}  '
          f'timeout={args.timeout_sec}s')

    def _sig(*_):
        global STOP
        STOP = True
        print('[pristine-pit] signal — stopping admissions', flush=True)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    summary = PHASE4_DIR / 'pit' / 'pristine_v2pit_summary.jsonl'
    summary.parent.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    results: List[Dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.parallel) as pool, \
            summary.open('w', encoding='utf-8') as sf:
        futs: Dict[Future, str] = {}
        it = iter(targets)
        for _ in range(min(args.parallel, len(targets))):
            try:
                p = next(it)
            except StopIteration:
                break
            futs[pool.submit(_run_one, p, args.timeout_sec)] = p

        while futs:
            done = [f for f in list(futs) if f.done()]
            if not done:
                time.sleep(5.0)
                if STOP:
                    break
                continue
            for f in done:
                proj = futs.pop(f)
                try:
                    r = f.result()
                except Exception as exc:
                    r = {'project': proj, 'rc': -3,
                         'stdout_tail': f'{type(exc).__name__}: {exc}'}
                results.append(r)
                sf.write(json.dumps(r, ensure_ascii=False) + '\n')
                sf.flush()
                ok = r.get('rc') == 0 and r.get('pit_ok') \
                    and (r.get('pit_total_mutants') or 0) > 0
                m = ('✓' if ok else
                     ('TIMEOUT' if r.get('timed_out') else 'FAIL'))
                print(f'[{m}] {proj}  elapsed={r.get("elapsed_sec",0)/60:.1f}min  '
                      f'mutants={r.get("pit_total_mutants","—")}  '
                      f'score={r.get("pit_score_pct","—")}', flush=True)
                if not STOP:
                    try:
                        p = next(it)
                        futs[pool.submit(_run_one, p, args.timeout_sec)] = p
                    except StopIteration:
                        pass
    print(f'\n[pristine-pit] done in {(time.time()-t_start)/3600.0:.2f}h')
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Run EvoSuite tests for all projects and summarize execution pass rate.

This uses the latest run_*/workdir/<project> as the patched snapshot.

Usage:
  python3 scripts/analysis/test_execution_rate_all.py \
    --root /PATH/TO/REPO/output/by_project \
    --out /PATH/TO/REPO/output/analysis/exec/test_exec_all.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


RE_SLF4J = re.compile(r"SLF4J|StaticLoggerBinder", re.IGNORECASE)
RE_CLASSPATH = re.compile(r"NoClassDefFoundError|ClassNotFoundException", re.IGNORECASE)
RE_ASSERT = re.compile(r"AssertionError|ComparisonFailure|FAILURES!!!", re.IGNORECASE)
RE_TIMEOUT = re.compile(r"timeout|timed out|Time out|TimeoutExpired|FailOnTimeout", re.IGNORECASE)
RE_NATIVE = re.compile(r"UnsatisfiedLinkError", re.IGNORECASE)
RE_INIT = re.compile(r"ExceptionInInitializerError", re.IGNORECASE)


def _iter_project_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        if re.match(r"^\d+_", d.name):
            yield d


def _proj_sort_key(p: Path) -> Tuple[int, str]:
    m = re.match(r"^(\d+)_", p.name)
    if m:
        return (int(m.group(1)), p.name)
    return (10**9, p.name)


def _latest_run_dir(proj_out: Path) -> Optional[Path]:
    runs = sorted([p for p in proj_out.glob("run_*") if p.is_dir()])
    if not runs:
        return None
    return runs[-1]


def _fqcn_from_java(test_file: Path, base: Path) -> str:
    rel = test_file.relative_to(base).as_posix()
    return rel.replace("/", ".").replace(".java", "")


def _classify_failure(output: str) -> str:
    if RE_SLF4J.search(output):
        return "slf4j"
    if RE_CLASSPATH.search(output):
        return "classpath"
    if RE_TIMEOUT.search(output):
        return "timeout"
    if RE_NATIVE.search(output):
        return "native"
    if RE_INIT.search(output):
        return "init_error"
    if RE_ASSERT.search(output):
        return "assertion"
    return "other"


def _run_project(
    proj_out: Path,
    timeout: int,
    sample: int,
    max_tests: int,
) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
    proj_name = proj_out.name
    run_dir = _latest_run_dir(proj_out)
    if not run_dir:
        row = {
            "project": proj_name,
            "run_dir": "",
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "pass_rate": "",
            "slf4j": 0,
            "classpath": 0,
            "timeout": 0,
            "native": 0,
            "init_error": 0,
            "assertion": 0,
            "other": 0,
            "status": "no_run_dir",
        }
        return row, None

    run_dir = run_dir.resolve()
    workdir = (run_dir / "workdir").resolve()
    proj_root = (workdir / proj_name).resolve()
    if not proj_root.exists():
        row = {
            "project": proj_name,
            "run_dir": str(run_dir),
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "pass_rate": "",
            "slf4j": 0,
            "classpath": 0,
            "timeout": 0,
            "native": 0,
            "init_error": 0,
            "assertion": 0,
            "other": 0,
            "status": "no_project_root",
        }
        return row, None

    # compile
    try:
        subprocess.run(
            ["ant", "clean", "compile", "compile-evosuite"],
            cwd=str(proj_root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError:
        row = {
            "project": proj_name,
            "run_dir": str(run_dir),
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "pass_rate": "",
            "slf4j": 0,
            "classpath": 0,
            "timeout": 0,
            "native": 0,
            "init_error": 0,
            "assertion": 0,
            "other": 0,
            "status": "compile_failed",
        }
        return row, None

    test_dir = proj_root / "evosuite-tests"
    if not test_dir.exists():
        row = {
            "project": proj_name,
            "run_dir": str(run_dir),
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "pass_rate": "",
            "slf4j": 0,
            "classpath": 0,
            "timeout": 0,
            "native": 0,
            "init_error": 0,
            "assertion": 0,
            "other": 0,
            "status": "no_tests",
        }
        return row, None

    shared_lib = (workdir / "lib").resolve()
    cp = ":".join(
        [
            str(proj_root / "build/classes"),
            str(proj_root / "build/evosuite"),
            str(proj_root / "lib/*"),
            str(proj_root / "test-lib/*"),
            str(shared_lib / "*"),
        ]
    )

    tests = sorted(test_dir.rglob("*_ESTest.java"))
    if max_tests > 0:
        tests = tests[:max_tests]

    total = 0
    ok = 0
    fail = 0
    reason_counts: Dict[str, int] = {
        "slf4j": 0,
        "classpath": 0,
        "timeout": 0,
        "native": 0,
        "init_error": 0,
        "assertion": 0,
        "other": 0,
    }
    samples: List[Tuple[str, str]] = []

    for test_file in tests:
        total += 1
        fqcn = _fqcn_from_java(test_file, test_dir)
        try:
            p = subprocess.run(
                ["java", "-cp", cp, "org.junit.runner.JUnitCore", fqcn],
                cwd=str(proj_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            fail += 1
            reason_counts["timeout"] += 1
            if sample > 0 and len(samples) < sample:
                samples.append((fqcn, "TIMEOUT"))
            continue

        if p.returncode == 0:
            ok += 1
            continue

        fail += 1
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        reason = _classify_failure(out)
        reason_counts[reason] += 1
        if sample > 0 and len(samples) < sample:
            samples.append((fqcn, out.strip()[:4000]))

    rate = ok / total if total else 0.0
    row = {
        "project": proj_name,
        "run_dir": str(run_dir),
        "total_tests": total,
        "passed_tests": ok,
        "failed_tests": fail,
        "pass_rate": f"{rate:.6f}" if total else "",
        "slf4j": reason_counts["slf4j"],
        "classpath": reason_counts["classpath"],
        "timeout": reason_counts["timeout"],
        "native": reason_counts["native"],
        "init_error": reason_counts["init_error"],
        "assertion": reason_counts["assertion"],
        "other": reason_counts["other"],
        "status": "ok",
    }

    detail = {
        "project": proj_name,
        "run_dir": str(run_dir),
        "total_tests": total,
        "passed_tests": ok,
        "failed_tests": fail,
        "pass_rate": round(rate, 6),
        "failure_reasons": reason_counts,
        "sample_failures": [{"class": c, "log": l} for c, l in samples],
    }
    return row, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        required=True,
        help="Root directory that contains project output folders (e.g., .../output/by_project)",
    )
    ap.add_argument("--out", default="", help="CSV output path (default: stdout)")
    ap.add_argument("--json-dir", default="", help="Optional per-project JSON output directory")
    ap.add_argument("--timeout", type=int, default=600, help="Timeout seconds per test class")
    ap.add_argument("--sample", type=int, default=0, help="Number of failure samples to save per project")
    ap.add_argument("--max-tests", type=int, default=0, help="Max tests per project (0 = all)")
    ap.add_argument("--workers", type=int, default=1, help="Parallel workers (default: 1)")
    args = ap.parse_args()

    root = Path(args.root)
    rows = []
    json_dir = Path(args.json_dir) if args.json_dir else None
    if json_dir:
        json_dir.mkdir(parents=True, exist_ok=True)

    proj_dirs = sorted(_iter_project_dirs(root), key=_proj_sort_key)

    if args.workers > 1:
        results = {}
        with ProcessPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
            futures = {
                ex.submit(
                    _run_project,
                    proj_dir,
                    timeout=args.timeout,
                    sample=args.sample,
                    max_tests=args.max_tests,
                ): proj_dir
                for proj_dir in proj_dirs
            }
            for fut in as_completed(futures):
                proj_dir = futures[fut]
                row, detail = fut.result()
                results[proj_dir.name] = (row, detail)
        for proj_dir in proj_dirs:
            row, detail = results[proj_dir.name]
            rows.append(row)
            if json_dir and detail is not None:
                out_path = json_dir / f"test_exec_{proj_dir.name}.json"
                out_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2))
    else:
        for proj_dir in proj_dirs:
            row, detail = _run_project(
                proj_dir,
                timeout=args.timeout,
                sample=args.sample,
                max_tests=args.max_tests,
            )
            rows.append(row)
            if json_dir and detail is not None:
                out_path = json_dir / f"test_exec_{proj_dir.name}.json"
                out_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2))

    fieldnames = [
        "project",
        "run_dir",
        "total_tests",
        "passed_tests",
        "failed_tests",
        "pass_rate",
        "slf4j",
        "classpath",
        "timeout",
        "native",
        "init_error",
        "assertion",
        "other",
        "status",
    ]
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"csv={out_path}")
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

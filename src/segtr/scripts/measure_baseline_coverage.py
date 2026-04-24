#!/usr/bin/env python3
"""Measure pristine-workdir JaCoCo baseline for the 5 dev projects.

Writes ``smell_repair_v2/data/baseline_coverage.json`` — Phase 2.4a.2
needs these numbers so later per-project runs can compute delta.

Usage:
  python -m smell_repair_v2.scripts.measure_baseline_coverage
  python -m smell_repair_v2.scripts.measure_baseline_coverage --projects 1_tullibee

The pristine sf110 workdir is rebuilt ``ant clean compile compile-evosuite``
before measuring (so jacoco sees a consistent classes/ directory).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.coverage.jacoco import JacocoError, run_jacoco  # noqa: E402


DEFAULT_PROJECTS = [
    "1_tullibee", "29_apbsmem", "71_ext4j", "88_jopenchart", "31_xisemele",
]
DEFAULT_SF110 = Path("<ANON_ROOT>/segtr_replication/sf110_projects")
DEFAULT_OUT = REPO_ROOT / "smell_repair_v2" / "data" / "baseline_coverage.json"


def _ensure_compiled(project_root: Path) -> bool:
    try:
        subprocess.run(
            ["ant", "-q", "clean", "compile", "compile-evosuite"],
            cwd=str(project_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=600, check=True,
        )
        return True
    except Exception as e:
        print(f"  [{project_root.name}] compile failed: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", nargs="*", default=DEFAULT_PROJECTS)
    ap.add_argument("--sf110-root", type=Path, default=DEFAULT_SF110)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if args.out.exists():
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    rows = []
    for proj in args.projects:
        root = args.sf110_root / proj
        print(f"\n=== {proj} ===")
        if not root.exists():
            print(f"  skip: {root} missing")
            continue
        if not _ensure_compiled(root):
            continue
        t0 = time.monotonic()
        try:
            cov = run_jacoco(root, project_name=proj)
        except JacocoError as e:
            print(f"  jacoco FAIL: {e}")
            continue
        elapsed = time.monotonic() - t0
        d = cov.to_dict()
        existing[proj] = d
        rows.append((proj, d))
        print(
            f"  OK  line={cov.line_coverage:.4f}  branch={cov.branch_coverage:.4f}  "
            f"inst={cov.instruction_coverage:.4f}  "
            f"tests={cov.tests_passed}/{cov.tests_total}  ({elapsed:.1f}s)"
        )

    args.out.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nwrote: {args.out}")

    # brief table
    if rows:
        print(f"\n{'project':<18}  {'line':>6}  {'branch':>6}  {'inst':>6}  tests")
        for proj, d in rows:
            print(
                f"{proj:<18}  {d['line_coverage']:>6.4f}  "
                f"{d['branch_coverage']:>6.4f}  "
                f"{d['instruction_coverage']:>6.4f}  "
                f"{d['tests_passed']}/{d['tests_total']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

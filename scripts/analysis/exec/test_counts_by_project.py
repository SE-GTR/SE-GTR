#!/usr/bin/env python3
"""Count EvoSuite test classes and test methods per project in output/by_project."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Iterable, Tuple


TEST_ANN = re.compile(r"^\s*@Test\b")


def latest_run_dir(project_dir: Path) -> Path | None:
    runs = sorted(project_dir.glob("run_*"), key=lambda p: p.name)
    return runs[-1] if runs else None


def project_root(run_dir: Path, project_name: str) -> Path | None:
    cand = run_dir / "workdir" / project_name
    if cand.exists():
        return cand
    cand2 = run_dir / "workdir"
    if cand2.exists():
        return cand2
    return None


def iter_projects(root: Path) -> Iterable[Tuple[str, Path]]:
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if p.is_dir():
            yield p.name, p


def count_tests(tests_root: Path) -> Tuple[int, int]:
    test_classes = 0
    test_methods = 0
    for java_file in tests_root.rglob("*_ESTest.java"):
        test_classes += 1
        try:
            with java_file.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if TEST_ANN.search(line):
                        test_methods += 1
        except OSError:
            continue
    return test_classes, test_methods


def main() -> int:
    ap = argparse.ArgumentParser(description="Count EvoSuite test classes/methods per project.")
    ap.add_argument(
        "--projects-root",
        type=Path,
        default=Path("/PATH/TO/REPO/output/by_project"),
        help="Root directory containing per-project run_* folders.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/exec/test_counts_by_project.csv"),
        help="Output CSV path",
    )
    args = ap.parse_args()

    rows = []
    for project_name, project_dir in iter_projects(args.projects_root):
        run_dir = latest_run_dir(project_dir)
        if run_dir is None:
            rows.append([project_name, "", "", 0, 0, "no_run"])
            continue

        proj_root = project_root(run_dir, project_name)
        if proj_root is None:
            rows.append([project_name, str(run_dir), "", 0, 0, "missing_workdir"])
            continue

        tests_root = proj_root / "evosuite-tests"
        if not tests_root.exists():
            rows.append([project_name, str(run_dir), str(proj_root), 0, 0, "no_tests"])
            continue

        test_classes, test_methods = count_tests(tests_root)
        rows.append([project_name, str(run_dir), str(proj_root), test_classes, test_methods, "ok"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["project", "run_dir", "project_root", "test_classes", "test_methods", "status"])
        w.writerows(rows)

    print(f"[OK] wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

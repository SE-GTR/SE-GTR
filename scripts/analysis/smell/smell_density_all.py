#!/usr/bin/env python3
"""Compute per-issue normalized density (per project, per smell).

Density definitions:
  - per-test: count(s) / #test_methods
  - per-loc : count(s) / LOC_tests (optional)

Counts are per smell type using unique (class, test_method) pairs.
Test method count is derived from JUnit @Test annotations in evosuite-tests.
Latest run workdir is used for tests/LOC when available.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple

TEST_ANN_RE = re.compile(r"^\s*@Test\b")


def _extract_method(inst: Dict) -> Optional[str]:
    return inst.get("test_method") or inst.get("testMethod") or inst.get("method")


def _load_smelly(path: Path) -> Dict[str, Dict[str, list]]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data  # type: ignore[return-value]


def _count_by_smell(data: Dict[str, Dict[str, list]]) -> Dict[str, int]:
    buckets: Dict[str, Set[Tuple[str, str]]] = {}
    for key, smells in data.items():
        if not isinstance(smells, dict):
            continue
        for smell_type, instances in smells.items():
            if not instances:
                continue
            bucket = buckets.setdefault(smell_type, set())
            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                m = _extract_method(inst)
                if m:
                    bucket.add((key, m))
    return {k: len(v) for k, v in buckets.items()}


def _iter_project_dirs(root: Path, project: Optional[str]) -> Iterable[Path]:
    if project:
        p = root / project
        if p.is_dir():
            yield p
        return
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


def _find_before(proj_dir: Path) -> Optional[Path]:
    p = proj_dir / f"smelly_{proj_dir.name}.json"
    return p if p.exists() else None


def _iter_after_candidates(proj_dir: Path) -> Iterable[Path]:
    yield from proj_dir.glob("smelly_after_*.json")
    yield from proj_dir.glob("run_*/reports/smelly_after_*.json")


def _find_after(proj_dir: Path) -> Optional[Path]:
    merged = list(proj_dir.glob("smelly_after_*merged*.json"))
    if merged:
        return max(merged, key=lambda p: p.stat().st_mtime)
    candidates = list(_iter_after_candidates(proj_dir))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _latest_run_dir(proj_dir: Path) -> Optional[Path]:
    runs = sorted(proj_dir.glob("run_*"), key=lambda p: p.name)
    return runs[-1] if runs else None


def _find_tests_root(proj_dir: Path, projects_root: Optional[Path]) -> Optional[Path]:
    run_dir = _latest_run_dir(proj_dir)
    if run_dir:
        cand = run_dir / "workdir" / proj_dir.name / "evosuite-tests"
        if cand.exists():
            return cand
    if projects_root:
        cand = projects_root / proj_dir.name / "evosuite-tests"
        if cand.exists():
            return cand
    return None


def _count_tests_and_loc(tests_root: Path) -> Tuple[int, int]:
    test_methods = 0
    loc = 0
    for p in tests_root.rglob("*_ESTest.java"):
        try:
            lines = p.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            if line.strip():
                loc += 1
            if TEST_ANN_RE.match(line):
                test_methods += 1
    return test_methods, loc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        required=True,
        help="Root directory that contains project output folders (e.g., .../output/by_project)",
    )
    ap.add_argument("--project", default="", help="Optional single project name")
    ap.add_argument(
        "--projects-root",
        default="",
        help="Optional root of sf110_projects for fallback tests/LOC",
    )
    ap.add_argument(
        "--out",
        default="/PATH/TO/REPO/output/analysis/smell/smell_density.csv",
        help="CSV output path",
    )
    args = ap.parse_args()

    root = Path(args.root)
    proj_filter = args.project or None
    projects_root = Path(args.projects_root) if args.projects_root else None

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []

    for proj_dir in sorted(_iter_project_dirs(root, proj_filter), key=_proj_sort_key):
        proj = proj_dir.name
        before_path = _find_before(proj_dir)
        after_path = _find_after(proj_dir)

        if not before_path:
            rows.append(
                {
                    "project": proj,
                    "smell_type": "",
                    "count_before": 0,
                    "count_after": 0,
                    "delta": 0,
                    "test_methods": 0,
                    "loc_tests": 0,
                    "density_before_tests": "",
                    "density_after_tests": "",
                    "delta_density_tests": "",
                    "density_before_loc": "",
                    "density_after_loc": "",
                    "delta_density_loc": "",
                    "status": "no_smelly_before",
                }
            )
            continue
        if not after_path:
            rows.append(
                {
                    "project": proj,
                    "smell_type": "",
                    "count_before": 0,
                    "count_after": 0,
                    "delta": 0,
                    "test_methods": 0,
                    "loc_tests": 0,
                    "density_before_tests": "",
                    "density_after_tests": "",
                    "delta_density_tests": "",
                    "density_before_loc": "",
                    "density_after_loc": "",
                    "delta_density_loc": "",
                    "status": "no_smelly_after",
                }
            )
            continue

        tests_root = _find_tests_root(proj_dir, projects_root)
        test_methods = 0
        loc_tests = 0
        if tests_root:
            test_methods, loc_tests = _count_tests_and_loc(tests_root)

        before_counts = _count_by_smell(_load_smelly(before_path))
        after_counts = _count_by_smell(_load_smelly(after_path))
        smell_types = set(before_counts) | set(after_counts)

        for s in sorted(smell_types):
            b = before_counts.get(s, 0)
            a = after_counts.get(s, 0)
            d = a - b
            dbt = (b / test_methods) if test_methods else ""
            dat = (a / test_methods) if test_methods else ""
            ddt = (d / test_methods) if test_methods else ""
            dbl = (b / loc_tests) if loc_tests else ""
            dal = (a / loc_tests) if loc_tests else ""
            ddl = (d / loc_tests) if loc_tests else ""
            rows.append(
                {
                    "project": proj,
                    "smell_type": s,
                    "count_before": b,
                    "count_after": a,
                    "delta": d,
                    "test_methods": test_methods,
                    "loc_tests": loc_tests,
                    "density_before_tests": f"{dbt:.8f}" if dbt != "" else "",
                    "density_after_tests": f"{dat:.8f}" if dat != "" else "",
                    "delta_density_tests": f"{ddt:.8f}" if ddt != "" else "",
                    "density_before_loc": f"{dbl:.8f}" if dbl != "" else "",
                    "density_after_loc": f"{dal:.8f}" if dal != "" else "",
                    "delta_density_loc": f"{ddl:.8f}" if ddl != "" else "",
                    "status": "ok",
                }
            )

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "project",
                "smell_type",
                "count_before",
                "count_after",
                "delta",
                "test_methods",
                "loc_tests",
                "density_before_tests",
                "density_after_tests",
                "delta_density_tests",
                "density_before_loc",
                "density_after_loc",
                "delta_density_loc",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"csv={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

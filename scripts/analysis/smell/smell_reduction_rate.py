#!/usr/bin/env python3
"""Compute project-level smell reduction rate.

Definition:
  reduction_rate = (count_before_total - count_after_total) / count_before_total

Counts are totals across smell types, where each smell type count is
the number of unique (class, test_method) pairs with that smell.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        required=True,
        help="Root directory that contains project output folders (e.g., .../output/by_project)",
    )
    ap.add_argument("--project", default="", help="Optional single project name")
    ap.add_argument(
        "--out",
        default="/PATH/TO/REPO/output/analysis/smell/smell_reduction_rate.csv",
        help="CSV output path",
    )
    args = ap.parse_args()

    root = Path(args.root)
    proj_filter = args.project or None
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
                    "count_before_total": 0,
                    "count_after_total": 0,
                    "delta_total": 0,
                    "reduction_rate": "",
                    "status": "no_smelly_before",
                }
            )
            continue
        if not after_path:
            rows.append(
                {
                    "project": proj,
                    "count_before_total": 0,
                    "count_after_total": 0,
                    "delta_total": 0,
                    "reduction_rate": "",
                    "status": "no_smelly_after",
                }
            )
            continue

        before_counts = _count_by_smell(_load_smelly(before_path))
        after_counts = _count_by_smell(_load_smelly(after_path))
        before_total = sum(before_counts.values())
        after_total = sum(after_counts.values())
        delta = after_total - before_total

        if before_total > 0:
            reduction_rate = (before_total - after_total) / before_total
            rate_str = f"{reduction_rate:.6f}"
        else:
            rate_str = ""

        rows.append(
            {
                "project": proj,
                "count_before_total": before_total,
                "count_after_total": after_total,
                "delta_total": delta,
                "reduction_rate": rate_str,
                "status": "ok" if before_total > 0 else "zero_before",
            }
        )

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "project",
                "count_before_total",
                "count_after_total",
                "delta_total",
                "reduction_rate",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"csv={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compute smell counts before/after for all projects under a root directory.

Outputs:
  - per-project per-smell CSV (project, smell_type, count_before, count_after, delta, status)
  - optional summary CSV aggregated by smell_type across all projects
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
    ap.add_argument(
        "--out",
        default="/PATH/TO/REPO/output/analysis/smell/smell_counts.csv",
        help="CSV output path (per-project per-smell)",
    )
    ap.add_argument(
        "--summary-out",
        default="/PATH/TO/REPO/output/analysis/smell/smell_counts_summary.csv",
        help="CSV output path for aggregated summary by smell_type",
    )
    args = ap.parse_args()

    root = Path(args.root)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    summary_totals: Dict[str, Dict[str, int]] = {}

    for proj_dir in sorted(_iter_project_dirs(root), key=_proj_sort_key):
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
                    "status": "no_smelly_after",
                }
            )
            continue

        before_counts = _count_by_smell(_load_smelly(before_path))
        after_counts = _count_by_smell(_load_smelly(after_path))
        smell_types = set(before_counts) | set(after_counts)

        for s in sorted(smell_types):
            b = before_counts.get(s, 0)
            a = after_counts.get(s, 0)
            rows.append(
                {
                    "project": proj,
                    "smell_type": s,
                    "count_before": b,
                    "count_after": a,
                    "delta": a - b,
                    "status": "ok",
                }
            )
            t = summary_totals.setdefault(s, {"count_before": 0, "count_after": 0})
            t["count_before"] += b
            t["count_after"] += a

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["project", "smell_type", "count_before", "count_after", "delta", "status"],
        )
        writer.writeheader()
        writer.writerows(rows)

    summary_rows = []
    for s, t in summary_totals.items():
        b = t["count_before"]
        a = t["count_after"]
        summary_rows.append({"smell_type": s, "count_before": b, "count_after": a, "delta": a - b})
    summary_rows.sort(key=lambda r: r["smell_type"])

    with summary_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["smell_type", "count_before", "count_after", "delta"])
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"csv={out_path}")
    print(f"summary_csv={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

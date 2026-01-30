#!/usr/bin/env python3
"""Compute method-level success rate: success(m) = validity_ok AND Δcount_m < 0.

Per method, count_before/after is the number of smell types detected
in that (class, test_method) pair (unique smell types).
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


def _smell_sets_by_method(data: Dict[str, Dict[str, list]]) -> Dict[Tuple[str, str], Set[str]]:
    out: Dict[Tuple[str, str], Set[str]] = {}
    for key, smells in data.items():
        if not isinstance(smells, dict):
            continue
        for smell_type, instances in smells.items():
            if not instances:
                continue
            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                m = _extract_method(inst)
                if not m:
                    continue
                out.setdefault((key, m), set()).add(smell_type)
    return out


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


def _iter_logs(proj_dir: Path) -> Iterable[Path]:
    return proj_dir.glob("run_*/logs/pipeline.jsonl")


def _collect_validity_ok(proj_dir: Path) -> Set[Tuple[str, str]]:
    ok: Set[Tuple[str, str]] = set()
    for log in _iter_logs(proj_dir):
        try:
            text = log.read_text(errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("event") != "validity_gate_ok":
                continue
            key = d.get("key")
            method = d.get("method")
            if key and method:
                ok.add((key, method))
    return ok


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
        default="/PATH/TO/REPO/output/analysis/smell/method_success_rate.csv",
        help="CSV output path (per project)",
    )
    ap.add_argument(
        "--detail-out",
        default="",
        help="Optional CSV output for per-method details",
    )
    args = ap.parse_args()

    root = Path(args.root)
    proj_filter = args.project or None
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    detail_rows = []
    rows = []

    for proj_dir in sorted(_iter_project_dirs(root, proj_filter), key=_proj_sort_key):
        proj = proj_dir.name
        before_path = _find_before(proj_dir)
        after_path = _find_after(proj_dir)

        if not before_path:
            rows.append(
                {
                    "project": proj,
                    "attempted_methods": 0,
                    "validity_ok_methods": 0,
                    "improved_methods": 0,
                    "success_methods": 0,
                    "success_rate": "",
                    "status": "no_smelly_before",
                }
            )
            continue
        if not after_path:
            rows.append(
                {
                    "project": proj,
                    "attempted_methods": 0,
                    "validity_ok_methods": 0,
                    "improved_methods": 0,
                    "success_methods": 0,
                    "success_rate": "",
                    "status": "no_smelly_after",
                }
            )
            continue

        before_sets = _smell_sets_by_method(_load_smelly(before_path))
        after_sets = _smell_sets_by_method(_load_smelly(after_path))
        attempted = set(before_sets.keys())
        ok_methods = _collect_validity_ok(proj_dir)

        attempted_n = len(attempted)
        validity_ok_n = 0
        improved_n = 0
        success_n = 0

        for m in attempted:
            b = len(before_sets.get(m, set()))
            a = len(after_sets.get(m, set()))
            delta = a - b
            improved = delta < 0
            validity_ok = m in ok_methods
            success = validity_ok and improved

            if validity_ok:
                validity_ok_n += 1
            if improved:
                improved_n += 1
            if success:
                success_n += 1

            if args.detail_out:
                detail_rows.append(
                    {
                        "project": proj,
                        "key": m[0],
                        "method": m[1],
                        "count_before": b,
                        "count_after": a,
                        "delta": delta,
                        "validity_ok": int(validity_ok),
                        "success": int(success),
                    }
                )

        success_rate = (success_n / attempted_n) if attempted_n else 0.0
        rows.append(
            {
                "project": proj,
                "attempted_methods": attempted_n,
                "validity_ok_methods": validity_ok_n,
                "improved_methods": improved_n,
                "success_methods": success_n,
                "success_rate": f"{success_rate:.6f}",
                "status": "ok",
            }
        )

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "project",
                "attempted_methods",
                "validity_ok_methods",
                "improved_methods",
                "success_methods",
                "success_rate",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    if args.detail_out:
        detail_path = Path(args.detail_out)
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        with detail_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "project",
                    "key",
                    "method",
                    "count_before",
                    "count_after",
                    "delta",
                    "validity_ok",
                    "success",
                ],
            )
            writer.writeheader()
            writer.writerows(detail_rows)

    print(f"csv={out_path}")
    if args.detail_out:
        print(f"detail_csv={args.detail_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

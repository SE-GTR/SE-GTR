#!/usr/bin/env python3
"""Compute compile_success_rate for all projects under a root directory.

Usage:
  python3 scripts/analysis/exec_compile_rate_all.py \
    --root /PATH/TO/REPO/output/by_project \
    --out /PATH/TO/REPO/output/analysis/exec/compile_success_rate.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple


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


def _find_smelly_before(proj_dir: Path) -> Optional[Path]:
    p = proj_dir / f"smelly_{proj_dir.name}.json"
    return p if p.exists() else None


def _extract_method(inst: Dict) -> Optional[str]:
    return inst.get("test_method") or inst.get("testMethod") or inst.get("method")


def _load_attempted_methods(smelly_path: Path) -> Set[Tuple[str, str]]:
    try:
        data = json.loads(smelly_path.read_text())
    except Exception:
        return set()
    methods: Set[Tuple[str, str]] = set()
    if not isinstance(data, dict):
        return methods
    for key, smells in data.items():
        if not isinstance(smells, dict):
            continue
        for instances in smells.values():
            if not instances:
                continue
            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                m = _extract_method(inst)
                if m:
                    methods.add((key, m))
    return methods


def _iter_logs(proj_dir: Path) -> Iterable[Path]:
    return proj_dir.glob("run_*/logs/pipeline.jsonl")


def _compile_ok_union(proj_dir: Path) -> Set[Tuple[str, str]]:
    compile_ok: Set[Tuple[str, str]] = set()
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
            event = d.get("event")
            key = d.get("key")
            method = d.get("method")
            if not key or not method:
                continue
            pair = (key, method)
            if event in ("validity_gate_ok", "validity_gate_failed"):
                compile_ok.add(pair)
            elif event == "method_done" and d.get("success") is True:
                # Fallback for runs with validity gate disabled.
                compile_ok.add(pair)
    return compile_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        required=True,
        help="Root directory that contains project output folders (e.g., .../output/by_project)",
    )
    ap.add_argument("--out", default="", help="CSV output path (default: stdout)")
    args = ap.parse_args()

    root = Path(args.root)
    rows = []

    for proj_dir in sorted(_iter_project_dirs(root), key=_proj_sort_key):
        proj = proj_dir.name
        smelly_path = _find_smelly_before(proj_dir)
        if not smelly_path:
            rows.append(
                {
                    "project": proj,
                    "attempted_methods": 0,
                    "compile_ok_methods": 0,
                    "compile_success_rate": "",
                    "status": "no_smelly_json",
                }
            )
            continue

        attempted = _load_attempted_methods(smelly_path)
        compile_ok = _compile_ok_union(proj_dir)

        attempted_n = len(attempted)
        compile_ok_n = len(compile_ok)
        if attempted_n == 0:
            rate = ""
        else:
            rate = f"{compile_ok_n / attempted_n:.6f}"

        status = "ok"
        if not list(_iter_logs(proj_dir)):
            status = "no_logs"

        rows.append(
            {
                "project": proj,
                "attempted_methods": attempted_n,
                "compile_ok_methods": compile_ok_n,
                "compile_success_rate": rate,
                "status": status,
            }
        )

    fieldnames = ["project", "attempted_methods", "compile_ok_methods", "compile_success_rate", "status"]
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

#!/usr/bin/env python3
"""Summarize method-level success rate across projects.

Reads method_success_rate.csv (per project) and computes:
  micro = sum(success_methods) / sum(attempted_methods)
  macro = mean(project success_rate) over projects with attempted_methods > 0
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import List, Dict


def _safe_int(v: str) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _safe_float(v: str) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in",
        dest="inp",
        default="/PATH/TO/REPO/output/analysis/smell/method_success_rate.csv",
        help="Input CSV from smell_method_success_rate.py",
    )
    ap.add_argument(
        "--out",
        default="/PATH/TO/REPO/output/analysis/smell/method_success_summary.csv",
        help="Output CSV path",
    )
    args = ap.parse_args()

    in_path = Path(args.inp)
    if not in_path.exists():
        print(f"[error] input not found: {in_path}")
        return 1

    rows: List[Dict[str, str]] = []
    with in_path.open() as f:
        rows = list(csv.DictReader(f))

    total_attempted = 0
    total_success = 0
    per_proj_rates = []
    n_projects = 0

    for r in rows:
        attempted = _safe_int(r.get("attempted_methods", "0"))
        success = _safe_int(r.get("success_methods", "0"))
        if attempted > 0:
            n_projects += 1
            per_proj_rates.append(success / attempted)
        total_attempted += attempted
        total_success += success

    micro = (total_success / total_attempted) if total_attempted else 0.0
    macro = (sum(per_proj_rates) / len(per_proj_rates)) if per_proj_rates else 0.0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "micro", "macro", "n_projects", "n_methods"])
        writer.writerow(
            [
                "method_success_rate",
                f"{micro:.6f}",
                f"{macro:.6f}",
                str(n_projects),
                str(total_attempted),
            ]
        )

    print(f"csv={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

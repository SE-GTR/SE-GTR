#!/usr/bin/env python3
"""Rank smell types by reduction to identify "easy" vs "hard" issues.

Uses smell_counts_summary.csv (smell_type, count_before, count_after, delta).
Reduction metrics:
  - delta = count_after - count_before (more negative => easier)
  - reduction_rate = (before - after) / before

Filters out smells with count_before < --min-before.
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in",
        dest="inp",
        default="/PATH/TO/REPO/output/analysis/smell/smell_counts_summary.csv",
        help="Input summary CSV from smell_reduction_all.py",
    )
    ap.add_argument(
        "--out",
        default="/PATH/TO/REPO/output/analysis/smell/smell_easy_hard_rank.csv",
        help="Output CSV path",
    )
    ap.add_argument("--min-before", type=int, default=20, help="Minimum count_before to include")
    ap.add_argument("--topk", type=int, default=10, help="Top K to mark as easy/hard")
    args = ap.parse_args()

    in_path = Path(args.inp)
    if not in_path.exists():
        print(f"[error] input not found: {in_path}")
        return 1

    rows: List[Dict[str, str]] = []
    with in_path.open() as f:
        rows = list(csv.DictReader(f))

    data = []
    for r in rows:
        b = _safe_int(r.get("count_before", "0"))
        a = _safe_int(r.get("count_after", "0"))
        if b < args.min_before:
            continue
        delta = a - b
        rate = (b - a) / b if b > 0 else 0.0
        data.append(
            {
                "smell_type": r.get("smell_type", ""),
                "count_before": b,
                "count_after": a,
                "delta": delta,
                "reduction_rate": rate,
            }
        )

    # Rank by delta (ascending: most negative first)
    by_delta = sorted(data, key=lambda x: x["delta"])
    easy_set = {d["smell_type"] for d in by_delta[: args.topk]}
    hard_set = {d["smell_type"] for d in by_delta[-args.topk :]} if by_delta else set()

    out_rows = []
    for d in sorted(data, key=lambda x: x["reduction_rate"], reverse=True):
        tag = ""
        if d["smell_type"] in easy_set:
            tag = "easy"
        elif d["smell_type"] in hard_set:
            tag = "hard"
        out_rows.append(
            {
                "smell_type": d["smell_type"],
                "count_before": d["count_before"],
                "count_after": d["count_after"],
                "delta": d["delta"],
                "reduction_rate": f"{d['reduction_rate']:.6f}",
                "tag": tag,
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["smell_type", "count_before", "count_after", "delta", "reduction_rate", "tag"],
        )
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"csv={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

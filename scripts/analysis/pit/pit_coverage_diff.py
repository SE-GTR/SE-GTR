#!/usr/bin/env python3
"""Compute PIT mutation score deltas from before/after mutations.xml."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import Dict, Tuple
from xml.etree import ElementTree as ET


def _parse_pit(xml_path: Path) -> Dict[str, float]:
    if not xml_path.exists():
        return {}
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return {}
    root = tree.getroot()
    counts = Counter()
    for mut in root.findall("mutation"):
        status = (mut.get("status") or "").upper()
        if not status:
            status = "UNKNOWN"
        counts[status] += 1
    total = sum(counts.values())
    killed = counts.get("KILLED", 0)
    survived = counts.get("SURVIVED", 0)
    timed_out = counts.get("TIMED_OUT", 0)
    no_cov = counts.get("NO_COVERAGE", 0)
    non_viable = counts.get("NON_VIABLE", 0)
    other = total - (killed + survived + timed_out + no_cov + non_viable)
    score_all = (killed / total) if total else 0.0
    denom_killed_survived = killed + survived
    score_killed_survived = (killed / denom_killed_survived) if denom_killed_survived else 0.0
    return {
        "total": float(total),
        "killed": float(killed),
        "survived": float(survived),
        "timed_out": float(timed_out),
        "no_coverage": float(no_cov),
        "non_viable": float(non_viable),
        "other": float(other),
        "score_all": score_all,
        "score_killed_survived": score_killed_survived,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="PIT mutation score delta for before/after roots.")
    ap.add_argument(
        "--before-root",
        type=Path,
        required=True,
        help="Root dir containing before/<project>/mutations.xml",
    )
    ap.add_argument(
        "--after-root",
        type=Path,
        required=True,
        help="Root dir containing after/<project>/mutations.xml",
    )
    ap.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output CSV path",
    )
    args = ap.parse_args()

    before_root = args.before_root.resolve()
    after_root = args.after_root.resolve()
    projects = sorted({p.name for p in before_root.iterdir() if p.is_dir()} | {p.name for p in after_root.iterdir() if p.is_dir()})

    rows = []
    for proj in projects:
        b = _parse_pit(before_root / proj / "mutations.xml")
        a = _parse_pit(after_root / proj / "mutations.xml")
        status = "ok"
        if not b and not a:
            status = "missing_both"
        elif not b:
            status = "missing_before"
        elif not a:
            status = "missing_after"
        row = {
            "project": proj,
            "before_score_all": b.get("score_all", ""),
            "after_score_all": a.get("score_all", ""),
            "delta_score_all": (a.get("score_all", 0.0) - b.get("score_all", 0.0)) if b and a else "",
            "before_score_killed_survived": b.get("score_killed_survived", ""),
            "after_score_killed_survived": a.get("score_killed_survived", ""),
            "delta_score_killed_survived": (a.get("score_killed_survived", 0.0) - b.get("score_killed_survived", 0.0)) if b and a else "",
            "before_total": b.get("total", ""),
            "after_total": a.get("total", ""),
            "status": status,
        }
        rows.append(row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "project",
                "before_score_all",
                "after_score_all",
                "delta_score_all",
                "before_score_killed_survived",
                "after_score_killed_survived",
                "delta_score_killed_survived",
                "before_total",
                "after_total",
                "status",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    print(f"[OK] PIT delta CSV written to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

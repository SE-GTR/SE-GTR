#!/usr/bin/env python3
"""Filter PIT before/after summaries to OK intersection and compute delta CSV."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Set
from xml.etree import ElementTree as ET


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: Iterable[Dict[str, str]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


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
        status = (mut.get("status") or "").upper() or "UNKNOWN"
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


def _ok_projects(rows: List[Dict[str, str]], exclude: Set[str]) -> Set[str]:
    return {
        r["project"]
        for r in rows
        if r.get("project") and r.get("status") == "ok" and r["project"] not in exclude
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Filter PIT summaries to OK intersection and compute deltas.")
    ap.add_argument(
        "--before-summary",
        type=Path,
        default=Path("output/analysis/pit/pit_before_summary.csv"),
    )
    ap.add_argument(
        "--after-summary",
        type=Path,
        default=Path("output/analysis/pit/pit_after_summary.csv"),
    )
    ap.add_argument(
        "--before-root",
        type=Path,
        default=Path("output/analysis/pit/before"),
        help="Root dir containing before/<project>/mutations.xml",
    )
    ap.add_argument(
        "--after-root",
        type=Path,
        default=Path("output/analysis/pit/after"),
        help="Root dir containing after/<project>/mutations.xml",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("output/analysis/pit"),
    )
    ap.add_argument(
        "--exclude",
        type=str,
        default="lib",
        help="Comma-separated project names to exclude (default: lib).",
    )
    ap.add_argument(
        "--prefix",
        type=str,
        default="intersection",
        help="Output prefix (default: intersection).",
    )
    args = ap.parse_args()

    exclude = {p.strip() for p in args.exclude.split(",") if p.strip()}

    before_rows = _read_csv(args.before_summary)
    after_rows = _read_csv(args.after_summary)

    before_ok = _ok_projects(before_rows, exclude)
    after_ok = _ok_projects(after_rows, exclude)
    inter = sorted(before_ok & after_ok)

    # Filter summaries to intersection
    before_filtered = [r for r in before_rows if r.get("project") in inter]
    after_filtered = [r for r in after_rows if r.get("project") in inter]

    before_out = args.out_dir / f"pit_before_summary_{args.prefix}.csv"
    after_out = args.out_dir / f"pit_after_summary_{args.prefix}.csv"
    _write_csv(before_out, before_filtered, list(before_rows[0].keys()) if before_rows else ["project"])
    _write_csv(after_out, after_filtered, list(after_rows[0].keys()) if after_rows else ["project"])

    # Delta CSV for intersection only
    delta_rows: List[Dict[str, str]] = []
    for proj in inter:
        b = _parse_pit(args.before_root / proj / "mutations.xml")
        a = _parse_pit(args.after_root / proj / "mutations.xml")
        status = "ok"
        if not b or not a:
            status = "missing_xml"
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
        delta_rows.append(row)

    delta_out = args.out_dir / f"pit_coverage_delta_{args.prefix}.csv"
    _write_csv(
        delta_out,
        delta_rows,
        [
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

    print(f"[OK] intersection size: {len(inter)}")
    print(f"[OK] wrote: {before_out}")
    print(f"[OK] wrote: {after_out}")
    print(f"[OK] wrote: {delta_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

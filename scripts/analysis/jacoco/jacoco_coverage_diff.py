#!/usr/bin/env python3
"""Compute JaCoCo line/branch coverage before/after and delta per project.

Expected layout:
  before_root/<project>/jacoco.xml
  after_root/<project>/jacoco.xml

Output:
  CSV with line_coverage, branch_coverage (before/after) and deltas.
"""

from __future__ import annotations

import argparse
import csv
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, Tuple


def _proj_sort_key(name: str) -> Tuple[int, str]:
    m = re.match(r"^(\d+)_", name)
    if m:
        return (int(m.group(1)), name)
    return (10**9, name)


def _iter_project_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    for d in root.iterdir():
        if d.is_dir() and re.match(r"^\d+_", d.name):
            yield d


def _parse_jacoco(xml_path: Path) -> Dict[str, Tuple[int, int, float]]:
    """Return coverage stats for LINE and BRANCH.

    Values are (covered, missed, coverage_rate).
    """

    if not xml_path.exists():
        return {}
    try:
        tree = ET.parse(str(xml_path))
        root = tree.getroot()
    except Exception:
        return {}

    counters = []
    for child in list(root):
        if child.tag.endswith("counter"):
            counters.append(child)

    if not counters:
        # Fallback: pick the last counters found anywhere (risk of duplication)
        for node in root.iter():
            if node.tag.endswith("counter"):
                counters.append(node)

    stats: Dict[str, Tuple[int, int, float]] = {}
    for c in counters:
        ctype = c.attrib.get("type")
        if ctype not in {"LINE", "BRANCH"}:
            continue
        covered = int(c.attrib.get("covered", "0"))
        missed = int(c.attrib.get("missed", "0"))
        total = covered + missed
        rate = (covered / total) if total else 0.0
        stats[ctype] = (covered, missed, rate)

    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--before-root",
        required=True,
        help="Root dir containing before/<project>/jacoco.xml",
    )
    ap.add_argument(
        "--after-root",
        required=True,
        help="Root dir containing after/<project>/jacoco.xml",
    )
    ap.add_argument(
        "--out",
        default="/PATH/TO/REPO/output/analysis/jacoco/jacoco_coverage_delta.csv",
        help="Output CSV path",
    )
    args = ap.parse_args()

    before_root = Path(args.before_root)
    after_root = Path(args.after_root)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    projects = {d.name for d in _iter_project_dirs(before_root)} | {
        d.name for d in _iter_project_dirs(after_root)
    }

    rows = []
    for proj in sorted(projects, key=_proj_sort_key):
        before_xml = before_root / proj / "jacoco.xml"
        after_xml = after_root / proj / "jacoco.xml"
        b = _parse_jacoco(before_xml)
        a = _parse_jacoco(after_xml)
        b_line = b.get("LINE", (0, 0, 0.0))[2] if b else None
        b_branch = b.get("BRANCH", (0, 0, 0.0))[2] if b else None
        a_line = a.get("LINE", (0, 0, 0.0))[2] if a else None
        a_branch = a.get("BRANCH", (0, 0, 0.0))[2] if a else None

        if b_line is None and a_line is None:
            status = "missing_both"
        elif b_line is None:
            status = "missing_before"
        elif a_line is None:
            status = "missing_after"
        else:
            status = "ok"

        delta_line = (a_line - b_line) if (a_line is not None and b_line is not None) else None
        delta_branch = (
            (a_branch - b_branch) if (a_branch is not None and b_branch is not None) else None
        )

        rows.append(
            {
                "project": proj,
                "before_line_coverage": f"{b_line:.6f}" if b_line is not None else "",
                "before_branch_coverage": f"{b_branch:.6f}" if b_branch is not None else "",
                "after_line_coverage": f"{a_line:.6f}" if a_line is not None else "",
                "after_branch_coverage": f"{a_branch:.6f}" if a_branch is not None else "",
                "delta_line_coverage": f"{delta_line:.6f}" if delta_line is not None else "",
                "delta_branch_coverage": f"{delta_branch:.6f}" if delta_branch is not None else "",
                "status": status,
            }
        )

    with out_path.open("w", newline="") as f:
        fieldnames = [
            "project",
            "before_line_coverage",
            "before_branch_coverage",
            "after_line_coverage",
            "after_branch_coverage",
            "delta_line_coverage",
            "delta_branch_coverage",
            "status",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"[ok] wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

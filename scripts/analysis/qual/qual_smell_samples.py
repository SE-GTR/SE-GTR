#!/usr/bin/env python3
"""Generate qualitative sample CSV and example Markdown files.

Outputs (by default into output/analysis/qual):
  - qual_smell_samples.csv
  - qual_success_examples.md
  - qual_failure_examples.md
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _split_smells(s: str) -> List[str]:
    s = (s or "").strip()
    if not s:
        return []
    return [x for x in s.split("|") if x]


def _diff_snippet(path: Path, max_lines: int = 20) -> str:
    if not path.exists():
        return "(diff not found)"
    lines = path.read_text(errors="ignore").splitlines()
    if not lines:
        return "(diff empty)"
    return "\n".join(lines[:max_lines])


def _smells_reduced(before: List[str], after: List[str]) -> List[str]:
    return [s for s in before if s not in set(after)]


def _smells_added(before: List[str], after: List[str]) -> List[str]:
    return [s for s in after if s not in set(before)]


def _select_success_examples(rows: List[Dict[str, str]], k: int) -> List[Dict[str, str]]:
    removed = [r for r in rows if r.get("change") == "removed"]
    # Sort by most negative delta first
    def key(r: Dict[str, str]) -> Tuple[int, str]:
        try:
            d = int(r.get("delta", "0"))
        except Exception:
            d = 0
        return (d, r.get("smell_type", ""))
    removed.sort(key=key)  # most negative first
    return removed[:k]


def _select_failure_examples(rows: List[Dict[str, str]], k: int) -> List[Dict[str, str]]:
    # Prefer regressions, then unchanged
    reg = [r for r in rows if r.get("section") == "regression"]
    unch = [r for r in rows if r.get("section") == "unchanged"]
    out: List[Dict[str, str]] = []
    out.extend(reg[: max(1, k // 2)])
    out.extend(unch[: max(1, k - len(out))])
    return out[:k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out-dir",
        default="/PATH/TO/REPO/output/analysis/qual",
    )
    ap.add_argument(
        "--smell-rank",
        default="/PATH/TO/REPO/output/analysis/smell/smell_easy_hard_rank.csv",
    )
    ap.add_argument(
        "--method-cases",
        default="/PATH/TO/REPO/output/analysis/qual/qual_smell_method_cases.csv",
    )
    ap.add_argument(
        "--smelltype-cases",
        default="/PATH/TO/REPO/output/analysis/qual/qual_smell_smelltype_cases.csv",
    )
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--examples", type=int, default=4)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build qual_smell_samples.csv
    smell_rank = _read_csv(Path(args.smell_rank))
    method_cases = _read_csv(Path(args.method_cases))

    rows: List[Dict[str, str]] = []
    # easy/hard smells
    for r in smell_rank:
        tag = r.get("tag", "")
        if tag not in ("easy", "hard"):
            continue
        section = "easy_smells" if tag == "easy" else "hard_smells"
        rows.append(
            {
                "section": section,
                "project": "",
                "key": "",
                "method": "",
                "smell_type": r.get("smell_type", ""),
                "count_before": r.get("count_before", ""),
                "count_after": r.get("count_after", ""),
                "delta": r.get("delta", ""),
                "validity_ok": "",
                "smells_before": "",
                "smells_after": "",
            }
        )

    # method cases
    section_map = {
        "improved": "improved_methods",
        "unchanged": "unchanged_methods",
        "regression": "regression_methods",
    }
    for r in method_cases:
        section = section_map.get(r.get("section", ""), r.get("section", ""))
        rows.append(
            {
                "section": section,
                "project": r.get("project", ""),
                "key": r.get("key", ""),
                "method": r.get("method", ""),
                "smell_type": "",
                "count_before": r.get("count_before", ""),
                "count_after": r.get("count_after", ""),
                "delta": r.get("delta", ""),
                "validity_ok": r.get("validity_ok", ""),
                "smells_before": r.get("smells_before", ""),
                "smells_after": r.get("smells_after", ""),
            }
        )

    samples_csv = out_dir / "qual_smell_samples.csv"
    with samples_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "section",
                "project",
                "key",
                "method",
                "smell_type",
                "count_before",
                "count_after",
                "delta",
                "validity_ok",
                "smells_before",
                "smells_after",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    # Success / Failure examples
    smelltype_cases = _read_csv(Path(args.smelltype_cases))
    success = _select_success_examples(smelltype_cases, args.examples)
    failure = _select_failure_examples(method_cases, args.examples)

    # Success examples markdown
    success_md = out_dir / "qual_success_examples.md"
    with success_md.open("w", encoding="utf-8") as f:
        f.write("# Qualitative Examples — Successful Smell Reductions (Before/After)\n\n")
        f.write("This file lists representative success cases where smells were reduced.\n\n---\n\n")
        for i, r in enumerate(success, start=1):
            proj = r.get("project", "")
            key = r.get("key", "")
            method = r.get("method", "")
            smell = r.get("smell_type", "")
            before = _split_smells(r.get("smells_before", ""))
            after = _split_smells(r.get("smells_after", ""))
            reduced = _smells_reduced(before, after)
            diff_path = Path(r.get("diff_path", ""))
            f.write(f"## {i}) {smell} reduction\n")
            f.write(f"**Project / Method**: `{proj}` — `{key.split('.')[-1]}::{method}`  \n")
            if reduced:
                f.write(f"**Smells reduced**: {', '.join(reduced)}  \n")
            f.write(f"**Diff**: `{diff_path}`\n\n")
            f.write("```diff\n")
            f.write(_diff_snippet(diff_path))
            f.write("\n```\n\n")

    # Failure examples markdown
    failure_md = out_dir / "qual_failure_examples.md"
    with failure_md.open("w", encoding="utf-8") as f:
        f.write("# Qualitative Examples — Failed / Partial Smell Reductions\n\n")
        f.write("This file lists representative unchanged/regression cases.\n\n---\n\n")
        for i, r in enumerate(failure, start=1):
            proj = r.get("project", "")
            key = r.get("key", "")
            method = r.get("method", "")
            delta = r.get("delta", "")
            before = _split_smells(r.get("smells_before", ""))
            after = _split_smells(r.get("smells_after", ""))
            added = _smells_added(before, after)
            diff_path = Path(r.get("diff_path", ""))
            f.write(f"## {i}) {r.get('section','case').capitalize()} case (Δ {delta})\n")
            f.write(f"**Project / Method**: `{proj}` — `{key.split('.')[-1]}::{method}`  \n")
            if added:
                f.write(f"**Smells added**: {', '.join(added)}  \n")
            f.write(f"**Diff**: `{diff_path}`\n\n")
            f.write("```diff\n")
            f.write(_diff_snippet(diff_path))
            f.write("\n```\n\n")

    print(f"[ok] wrote: {samples_csv}")
    print(f"[ok] wrote: {success_md}")
    print(f"[ok] wrote: {failure_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

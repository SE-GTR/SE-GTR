#!/usr/bin/env python3
"""Create executability summary tables from existing CSV/JSON outputs.

Outputs:
  - exec_summary.csv
  - failure_mode_table.csv

By default, reads:
  output/analysis/exec/compile_success_rate.csv
  output/analysis/exec/validity_gate_success_rate.csv
  output/analysis/exec/failure_dist.csv
  output/analysis/exec/test_exec_json/*.json (optional)
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _safe_int(v: Optional[str]) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _safe_float(v: Optional[str]) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _macro_micro_rate(rows: Iterable[Dict[str, str]], num_key: str, den_key: str) -> Tuple[float, float, int, int]:
    per_proj = []
    total_num = 0
    total_den = 0
    for r in rows:
        den = _safe_int(r.get(den_key))
        num = _safe_int(r.get(num_key))
        if den > 0:
            per_proj.append(num / den)
        total_num += num
        total_den += den
    macro = sum(per_proj) / len(per_proj) if per_proj else 0.0
    micro = (total_num / total_den) if total_den else 0.0
    return micro, macro, len(per_proj), total_den


def _load_test_exec_jsons(test_json_dir: Path) -> List[Dict[str, object]]:
    rows = []
    if not test_json_dir.exists():
        return rows
    for p in sorted(test_json_dir.glob("test_exec_*.json")):
        try:
            rows.append(json.loads(p.read_text()))
        except Exception:
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--compile-csv",
        default="/PATH/TO/REPO/output/analysis/exec/compile_success_rate.csv",
    )
    ap.add_argument(
        "--validity-csv",
        default="/PATH/TO/REPO/output/analysis/exec/validity_gate_success_rate.csv",
    )
    ap.add_argument(
        "--failure-csv",
        default="/PATH/TO/REPO/output/analysis/exec/failure_dist.csv",
    )
    ap.add_argument(
        "--test-json-dir",
        default="/PATH/TO/REPO/output/analysis/exec/test_exec_json",
    )
    ap.add_argument(
        "--out-dir",
        default="/PATH/TO/REPO/output/analysis/exec",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Executability summary
    compile_rows = _read_csv(Path(args.compile_csv))
    validity_rows = _read_csv(Path(args.validity_csv))

    compile_micro, compile_macro, compile_nproj, compile_nmethods = _macro_micro_rate(
        compile_rows, "compile_ok_methods", "attempted_methods"
    )
    val_micro, val_macro, val_nproj, val_nmethods = _macro_micro_rate(
        validity_rows, "validity_gate_ok_methods", "validity_gate_attempted_methods"
    )

    test_rows = _load_test_exec_jsons(Path(args.test_json_dir))
    test_micro = test_macro = 0.0
    test_nproj = test_nmethods = 0
    if test_rows:
        per_proj = []
        total_pass = 0
        total_total = 0
        for r in test_rows:
            total = int(r.get("total_tests", 0) or 0)
            passed = int(r.get("passed_tests", 0) or 0)
            if total > 0:
                per_proj.append(passed / total)
            total_pass += passed
            total_total += total
        test_macro = sum(per_proj) / len(per_proj) if per_proj else 0.0
        test_micro = (total_pass / total_total) if total_total else 0.0
        test_nproj = len(per_proj)
        test_nmethods = total_total

    summary_rows = [
        ["compile_success_rate", f"{compile_micro:.6f}", f"{compile_macro:.6f}", str(compile_nproj), str(compile_nmethods)],
        ["validity_gate_success_rate", f"{val_micro:.6f}", f"{val_macro:.6f}", str(val_nproj), str(val_nmethods)],
    ]
    if test_rows:
        summary_rows.append(
            ["test_execution_rate", f"{test_micro:.6f}", f"{test_macro:.6f}", str(test_nproj), str(test_nmethods)]
        )

    summary_csv = out_dir / "exec_summary.csv"
    with summary_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "micro", "macro", "n_projects", "n_methods"])
        writer.writerows(summary_rows)

    # Failure mode table
    failure_rows = _read_csv(Path(args.failure_csv))
    failure_cols = [
        "compile_fail",
        "assertion_fail",
        "runtime_fail",
        "validity_fail",
        "timeout",
        "patch_fail",
        "llm_fail",
        "unknown",
    ]
    totals = {c: 0 for c in failure_cols}
    for r in failure_rows:
        for c in failure_cols:
            totals[c] += _safe_int(r.get(c))
    total_fail = sum(totals.values()) or 1

    failure_table = []
    for k, v in sorted(totals.items(), key=lambda x: x[1], reverse=True):
        if v == 0:
            continue
        failure_table.append([k, str(v), f"{(v / total_fail) * 100:.2f}"])

    failure_csv = out_dir / "failure_mode_table.csv"
    with failure_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["failure_type", "count", "percent"])
        writer.writerows(failure_table)

    print(f"summary_csv={summary_csv}")
    print(f"failure_csv={failure_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

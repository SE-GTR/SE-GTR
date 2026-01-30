#!/usr/bin/env python3
"""Compute failure type distribution for all projects under a root directory.

Failure types (heuristic, based on pipeline.jsonl):
  - assertion_fail: validity_gate_failed with AssertionError-like signal
  - runtime_fail: validity_gate_failed with non-assertion error
  - validity_fail: validity_gate_failed with unknown/empty error
  - timeout: validity_gate_failed with timeout-like error
  - compile_fail: method_done success=false with at least one llm_response_extracted
  - patch_fail: method_done success=false with no llm_response_extracted
  - llm_fail: saw llm_request but never saw llm_response
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple

TIMEOUT_RE = re.compile(r"timeout|timed out|Time out|TimeoutExpired", re.IGNORECASE)
ASSERT_RE = re.compile(r"AssertionError|AssertionFailedError|ComparisonFailure", re.IGNORECASE)


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


def _collect_events(proj_dir: Path) -> Dict[Tuple[str, str], Dict[str, object]]:
    state: Dict[Tuple[str, str], Dict[str, object]] = {}
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
            st = state.setdefault(
                pair,
                {
                    "llm_request": False,
                    "llm_response": False,
                    "llm_response_extracted": False,
                    "validity_ok": False,
                    "validity_failed": False,
                    "validity_timeout": False,
                    "validity_assertion": False,
                    "validity_runtime": False,
                    "method_done": False,
                    "method_success": False,
                },
            )
            if event == "llm_request":
                st["llm_request"] = True
            elif event == "llm_response":
                st["llm_response"] = True
            elif event == "llm_response_extracted":
                st["llm_response_extracted"] = True
            elif event == "validity_gate_ok":
                st["validity_ok"] = True
            elif event == "validity_gate_failed":
                st["validity_failed"] = True
                err = str(d.get("error") or "")
                if TIMEOUT_RE.search(err):
                    st["validity_timeout"] = True
                elif err:
                    if ASSERT_RE.search(err):
                        st["validity_assertion"] = True
                    else:
                        st["validity_runtime"] = True
            elif event == "method_done":
                st["method_done"] = True
                if d.get("success") is True:
                    st["method_success"] = True
    return state


def _classify(st: Dict[str, object]) -> str:
    if st.get("validity_ok"):
        return "success"
    if st.get("validity_timeout"):
        return "timeout"
    if st.get("validity_assertion"):
        return "assertion_fail"
    if st.get("validity_runtime"):
        return "runtime_fail"
    if st.get("validity_failed"):
        return "validity_fail"
    if st.get("method_done") and not st.get("method_success"):
        if st.get("llm_response_extracted"):
            return "compile_fail"
        return "patch_fail"
    if st.get("llm_request") and not st.get("llm_response"):
        return "llm_fail"
    return "unknown"


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
                    "success": 0,
                    "compile_fail": 0,
                    "assertion_fail": 0,
                    "runtime_fail": 0,
                    "validity_fail": 0,
                    "timeout": 0,
                    "patch_fail": 0,
                    "llm_fail": 0,
                    "unknown": 0,
                    "status": "no_smelly_json",
                }
            )
            continue

        attempted = _load_attempted_methods(smelly_path)
        state = _collect_events(proj_dir)

        counts = {
            "success": 0,
            "compile_fail": 0,
            "assertion_fail": 0,
            "runtime_fail": 0,
            "validity_fail": 0,
            "timeout": 0,
            "patch_fail": 0,
            "llm_fail": 0,
            "unknown": 0,
        }

        for pair in attempted:
            cls = _classify(state.get(pair, {}))
            counts[cls] += 1

        rows.append(
            {
                "project": proj,
                "attempted_methods": len(attempted),
                **counts,
                "status": "ok",
            }
        )

    fieldnames = [
        "project",
        "attempted_methods",
        "success",
        "compile_fail",
        "assertion_fail",
        "runtime_fail",
        "validity_fail",
        "timeout",
        "patch_fail",
        "llm_fail",
        "unknown",
        "status",
    ]
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

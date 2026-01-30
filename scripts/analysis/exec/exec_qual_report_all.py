#!/usr/bin/env python3
"""Generate per-project qualitative failure report from pipeline.jsonl logs.

Outputs:
  - CSV summary with counts and top failure type
  - JSON detail with example errors per failure type
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

TIMEOUT_RE = re.compile(r"timeout|timed out|Time out|TimeoutExpired", re.IGNORECASE)
ASSERT_RE = re.compile(r"AssertionError|AssertionFailedError|ComparisonFailure", re.IGNORECASE)

HINT_RULES = [
    ("slf4j", re.compile(r"SLF4J|StaticLoggerBinder", re.IGNORECASE)),
    ("classpath_missing", re.compile(r"NoClassDefFoundError|ClassNotFoundException", re.IGNORECASE)),
    ("native", re.compile(r"UnsatisfiedLinkError|java.library.path|JNI", re.IGNORECASE)),
    ("assertion", ASSERT_RE),
    ("timeout", TIMEOUT_RE),
    ("stack_overflow", re.compile(r"StackOverflowError", re.IGNORECASE)),
    ("oom", re.compile(r"OutOfMemoryError|Java heap space", re.IGNORECASE)),
    ("npe", re.compile(r"NullPointerException", re.IGNORECASE)),
    ("illegal_state", re.compile(r"IllegalStateException", re.IGNORECASE)),
    ("no_such_method", re.compile(r"NoSuchMethodError", re.IGNORECASE)),
    ("no_such_field", re.compile(r"NoSuchFieldError", re.IGNORECASE)),
    ("incompatible_types", re.compile(r"incompatible types", re.IGNORECASE)),
    ("cannot_find_symbol", re.compile(r"cannot find symbol", re.IGNORECASE)),
]


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
    return sorted(proj_dir.glob("run_*/logs/pipeline.jsonl"))


def _trim_error(err: str, max_lines: int, max_chars: int) -> str:
    if not err:
        return ""
    lines = err.splitlines()
    if max_lines > 0:
        lines = lines[:max_lines]
    s = "\n".join(lines).strip()
    if len(s) > max_chars:
        s = s[: max_chars - 3].rstrip() + "..."
    return s


def _error_hint(err: str) -> str:
    if not err:
        return "unknown"
    for name, pat in HINT_RULES:
        if pat.search(err):
            return name
    return "other"


def _collect_state(proj_dir: Path) -> Dict[Tuple[str, str], Dict[str, object]]:
    state: Dict[Tuple[str, str], Dict[str, object]] = {}
    for log in _iter_logs(proj_dir):
        run_id = log.parent.parent.name
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
                    "compile_failed": False,
                    "compile_error": None,
                    "compile_run": None,
                    "validity_error": None,
                    "validity_run": None,
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
                if err and st.get("validity_error") is None:
                    st["validity_error"] = err
                    st["validity_run"] = run_id
                if TIMEOUT_RE.search(err):
                    st["validity_timeout"] = True
                elif err:
                    if ASSERT_RE.search(err):
                        st["validity_assertion"] = True
                    else:
                        st["validity_runtime"] = True
            elif event and str(event).startswith("compile_failed"):
                st["compile_failed"] = True
                err = str(d.get("error") or "")
                if err and st.get("compile_error") is None:
                    st["compile_error"] = err
                    st["compile_run"] = run_id
            elif event == "method_done":
                st["method_done"] = True
                if d.get("success") is True:
                    st["method_success"] = True
    return state


def _classify(st: Dict[str, object]) -> str:
    if st.get("validity_ok") or st.get("method_success"):
        return "success"
    if st.get("validity_timeout"):
        return "timeout"
    if st.get("validity_assertion"):
        return "assertion_fail"
    if st.get("validity_runtime"):
        return "runtime_fail"
    if st.get("validity_failed"):
        return "validity_fail"
    if st.get("compile_failed"):
        return "compile_fail"
    if st.get("method_done") and not st.get("method_success"):
        if st.get("llm_response_extracted"):
            return "compile_fail"
        return "patch_fail"
    if st.get("llm_request") and not st.get("llm_response"):
        return "llm_fail"
    return "unknown"


def _example_for_type(
    pair: Tuple[str, str],
    st: Dict[str, object],
    cls: str,
    max_lines: int,
    max_chars: int,
) -> Dict[str, str]:
    key, method = pair
    err = ""
    run = ""
    if cls in {"timeout", "assertion_fail", "runtime_fail", "validity_fail"}:
        err = str(st.get("validity_error") or "")
        run = str(st.get("validity_run") or "")
    elif cls == "compile_fail":
        err = str(st.get("compile_error") or "")
        run = str(st.get("compile_run") or "")
    elif cls == "patch_fail":
        err = "Patch not applied or empty output (no llm_response_extracted)."
    elif cls == "llm_fail":
        err = "LLM request without response."
    if not err:
        err = "(no error text in log)"
    hint = _error_hint(err)
    return {
        "key": key,
        "method": method,
        "run": run,
        "hint": hint,
        "excerpt": _trim_error(err, max_lines=max_lines, max_chars=max_chars),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        required=True,
        help="Root directory that contains project output folders (e.g., .../output/by_project)",
    )
    ap.add_argument(
        "--out-csv",
        default="output/analysis/exec/qual_report.csv",
        help="CSV output path",
    )
    ap.add_argument(
        "--out-json",
        default="output/analysis/exec/qual_report.json",
        help="JSON output path",
    )
    ap.add_argument("--max-examples", type=int, default=3)
    ap.add_argument("--max-error-lines", type=int, default=8)
    ap.add_argument("--max-error-chars", type=int, default=600)
    args = ap.parse_args()

    root = Path(args.root)
    rows = []
    details: List[Dict[str, object]] = []

    for proj_dir in sorted(_iter_project_dirs(root), key=_proj_sort_key):
        proj = proj_dir.name
        smelly_path = _find_smelly_before(proj_dir)
        attempted: Set[Tuple[str, str]] = set()
        status = "ok"

        if smelly_path:
            attempted = _load_attempted_methods(smelly_path)
        else:
            status = "no_smelly_json"

        state = _collect_state(proj_dir)
        if not attempted:
            attempted = set(state.keys())
            if status == "ok":
                status = "no_smelly_or_empty"

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
        examples: Dict[str, List[Dict[str, str]]] = {k: [] for k in counts if k != "success"}
        hint_counts: Dict[str, int] = {}

        for pair in attempted:
            st = state.get(pair, {})
            cls = _classify(st)
            counts[cls] += 1
            if cls != "success":
                ex_list = examples[cls]
                if len(ex_list) < args.max_examples:
                    ex = _example_for_type(
                        pair, st, cls, args.max_error_lines, args.max_error_chars
                    )
                    ex_list.append(ex)
                hint = _error_hint(
                    str(
                        st.get("validity_error")
                        or st.get("compile_error")
                        or ""
                    )
                )
                hint_counts[hint] = hint_counts.get(hint, 0) + 1

        failure_counts = {k: v for k, v in counts.items() if k != "success"}
        top_failure_type = ""
        top_failure_count = 0
        if failure_counts:
            top_failure_type = max(failure_counts, key=failure_counts.get)
            top_failure_count = failure_counts[top_failure_type]
        top_failure_pct = (
            (top_failure_count / len(attempted)) if attempted else 0.0
        )
        top_hint = ""
        if hint_counts:
            top_hint = max(hint_counts, key=hint_counts.get)

        rows.append(
            {
                "project": proj,
                "attempted_methods": len(attempted),
                **counts,
                "top_failure_type": top_failure_type,
                "top_failure_count": top_failure_count,
                "top_failure_pct": f"{top_failure_pct:.6f}",
                "top_failure_hint": top_hint,
                "status": status,
            }
        )

        details.append(
            {
                "project": proj,
                "attempted_methods": len(attempted),
                "counts": counts,
                "top_failure_type": top_failure_type,
                "top_failure_count": top_failure_count,
                "top_failure_pct": top_failure_pct,
                "top_failure_hint": top_hint,
                "examples": examples,
                "hint_counts": hint_counts,
                "status": status,
            }
        )

    csv_path = Path(args.out_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
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
        "top_failure_type",
        "top_failure_count",
        "top_failure_pct",
        "top_failure_hint",
        "status",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    json_path = Path(args.out_json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(details, indent=2), encoding="utf-8")

    print(f"csv={csv_path}")
    print(f"json={json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

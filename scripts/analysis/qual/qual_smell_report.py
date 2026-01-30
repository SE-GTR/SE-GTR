#!/usr/bin/env python3
"""Qualitative report for Quality Issue Reduction (Smelly).

Generates:
  - CSV samples for smell-type cases, method cases, rule-vs-LLM cases
  - Patch pattern summary CSV
  - Domain/size summary CSV
  - Failure cluster CSV
  - JSON report (all sections)
  - Markdown summary
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


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


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _iter_project_dirs(root: Path) -> Iterable[Path]:
    for d in root.iterdir():
        if not d.is_dir():
            continue
        if re.match(r"^\d+_", d.name):
            yield d


def _proj_sort_key(name: str) -> Tuple[int, str]:
    m = re.match(r"^(\d+)_", name)
    if m:
        return (int(m.group(1)), name)
    return (10**9, name)


def _find_before(proj_dir: Path) -> Optional[Path]:
    p = proj_dir / f"smelly_{proj_dir.name}.json"
    return p if p.exists() else None


def _iter_after_candidates(proj_dir: Path) -> Iterable[Path]:
    yield from proj_dir.glob("smelly_after_*.json")
    yield from proj_dir.glob("run_*/reports/smelly_after_*.json")


def _find_after(proj_dir: Path) -> Optional[Path]:
    merged = list(proj_dir.glob("smelly_after_*merged*.json"))
    if merged:
        return max(merged, key=lambda p: p.stat().st_mtime)
    candidates = list(_iter_after_candidates(proj_dir))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _extract_method(inst: Dict) -> Optional[str]:
    return inst.get("test_method") or inst.get("testMethod") or inst.get("method")


def _load_smelly(path: Path) -> Dict[str, Dict[str, list]]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data  # type: ignore[return-value]


def _collect_smell_methods_for_project(
    data: Dict[str, Dict[str, list]], target_smells: Set[str]
) -> Dict[str, Set[Tuple[str, str]]]:
    out: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for key, smells in data.items():
        if not isinstance(smells, dict):
            continue
        for smell_type, instances in smells.items():
            if smell_type not in target_smells:
                continue
            if not instances:
                continue
            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                m = _extract_method(inst)
                if not m:
                    continue
                out[smell_type].add((key, m))
    return out


def _collect_smell_sets_for_methods(
    data: Dict[str, Dict[str, list]],
    needed: Set[Tuple[str, str]],
) -> Dict[Tuple[str, str], Set[str]]:
    out: Dict[Tuple[str, str], Set[str]] = {m: set() for m in needed}
    if not needed:
        return out
    for key, smells in data.items():
        if not isinstance(smells, dict):
            continue
        for smell_type, instances in smells.items():
            if not instances:
                continue
            for inst in instances:
                if not isinstance(inst, dict):
                    continue
                m = _extract_method(inst)
                if not m:
                    continue
                k = (key, m)
                if k in out:
                    out[k].add(smell_type)
    return out


def _methods_from_test_file(path: Path, cache: Dict[Path, Set[str]]) -> Set[str]:
    if path in cache:
        return cache[path]
    methods: Set[str] = set()
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        cache[path] = methods
        return methods
    for m in re.finditer(r"\bvoid\s+(test\w+)\s*\(", text):
        methods.add(m.group(1))
    cache[path] = methods
    return methods


def _collect_method_flags(proj_dir: Path) -> Dict[Tuple[str, str], Dict[str, bool]]:
    flags: Dict[Tuple[str, str], Dict[str, bool]] = {}
    file_cache: Dict[Path, Set[str]] = {}
    for log in proj_dir.glob("run_*/logs/pipeline.jsonl"):
        try:
            text = log.read_text(errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            ev = d.get("event")
            key = d.get("key")
            if not key:
                continue
            if ev == "deterministic_ds":
                methods = d.get("methods") or []
                if isinstance(methods, list):
                    for m in methods:
                        if not m:
                            continue
                        flags.setdefault((key, m), {}).update({"det": True})
            elif ev == "deterministic_nna":
                f = d.get("file")
                if f:
                    mset = _methods_from_test_file(Path(f), file_cache)
                    for m in mset:
                        flags.setdefault((key, m), {}).update({"det": True})
            elif ev == "llm_request":
                m = d.get("method")
                if m:
                    flags.setdefault((key, m), {}).update({"llm": True})
    return flags


def _find_diff(proj_dir: Path, key: str, method: str) -> Optional[Path]:
    class_name = key.split(".")[-1]
    candidates = list(proj_dir.glob(f"run_*/patches/*/{class_name}/{method}.diff"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_diff_snippet(path: Path, max_lines: int = 40, max_chars: int = 2000) -> str:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return ""
    out = []
    total = 0
    for line in lines[:max_lines]:
        total += len(line) + 1
        if total > max_chars:
            break
        out.append(line)
    return "\n".join(out)


def _diff_patterns(text: str) -> Set[str]:
    patterns: Set[str] = set()
    for line in text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            if "assert" in line:
                patterns.add("assertion_added")
            if "assertThrows" in line or ("@Test" in line and "expected" in line):
                patterns.add("exception_check_added")
            if "mock(" in line or "Mockito" in line or "when(" in line or "doReturn(" in line:
                patterns.add("mock_or_stub_added")
            if "assertNotNull" in line or "requireNonNull" in line:
                patterns.add("null_check_added")
            if "hashCode" in line or "equals(" in line:
                patterns.add("determinism_check_added")
        if line.startswith("-"):
            if "assert" in line:
                patterns.add("assertion_removed")
    return patterns


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denx == 0 or deny == 0:
        return None
    return num / (denx * deny)


def _project_domain(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["gui", "swing", "ui", "viewer", "chart", "frame", "window"]):
        return "ui"
    if any(k in n for k in ["web", "http", "server", "servlet", "rest", "jsp"]):
        return "web"
    if any(k in n for k in ["db", "jdbc", "sql", "hibernate", "orm"]):
        return "db"
    if any(k in n for k in ["ftp", "net", "socket", "tcp", "udp", "mail"]):
        return "network"
    if any(k in n for k in ["xml", "json", "parser", "csv"]):
        return "data"
    if any(k in n for k in ["game", "battle", "war", "player", "robot", "sim"]):
        return "game"
    return "util"


def _pick_methods(
    methods: Iterable[Tuple[str, str, str]],
    n: int,
    sort_key,
    reverse: bool = True,
) -> List[Tuple[str, str, str]]:
    uniq = list({m for m in methods})
    uniq.sort(key=sort_key, reverse=reverse)
    return uniq[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--by-project",
        default="/PATH/TO/REPO/output/by_project",
        help="Root directory that contains project outputs",
    )
    ap.add_argument(
        "--analysis-exec",
        default="/PATH/TO/REPO/output/analysis/exec",
        help="Directory with exec analysis outputs",
    )
    ap.add_argument(
        "--analysis-smell",
        default="/PATH/TO/REPO/output/analysis/smell",
        help="Directory with smell analysis outputs",
    )
    ap.add_argument(
        "--out-dir",
        default="/PATH/TO/REPO/output/analysis/qual",
        help="Output directory for qualitative report",
    )
    ap.add_argument("--topk", type=int, default=10, help="Top/bottom sample size")
    ap.add_argument("--rep-per-smell", type=int, default=2, help="Representative cases per smell type")
    args = ap.parse_args()

    by_project = Path(args.by_project)
    analysis/exec = Path(args.analysis/exec)
    analysis/smell = Path(args.analysis/smell)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    topk = args.topk
    rep_per_smell = args.rep_per_smell

    # 1) Smell-type success/failure cases
    easy_hard_path = analysis/smell / "smell_easy_hard_rank.csv"
    easy_hard_rows = _read_csv(easy_hard_path)
    easy_smells = [r for r in easy_hard_rows if r.get("tag") == "easy"][:topk]
    hard_smells = [r for r in easy_hard_rows if r.get("tag") == "hard"][:topk]
    target_smells = {r.get("smell_type", "") for r in easy_smells + hard_smells if r.get("smell_type")}

    detail_rows = _read_csv(analysis/smell / "method_success_detail.csv")
    method_stats: Dict[Tuple[str, str, str], Dict[str, int]] = {}
    for r in detail_rows:
        proj = r.get("project", "")
        key = r.get("key", "")
        method = r.get("method", "")
        if not proj or not key or not method:
            continue
        method_stats[(proj, key, method)] = {
            "count_before": _safe_int(r.get("count_before", "0")),
            "count_after": _safe_int(r.get("count_after", "0")),
            "delta": _safe_int(r.get("delta", "0")),
            "validity_ok": _safe_int(r.get("validity_ok", "0")),
            "success": _safe_int(r.get("success", "0")),
        }

    before_map: Dict[str, Set[Tuple[str, str, str]]] = defaultdict(set)
    after_map: Dict[str, Set[Tuple[str, str, str]]] = defaultdict(set)

    for proj_dir in sorted(_iter_project_dirs(by_project), key=lambda p: _proj_sort_key(p.name)):
        proj = proj_dir.name
        before = _find_before(proj_dir)
        after = _find_after(proj_dir)
        if not before or not after:
            continue
        before_data = _load_smelly(before)
        after_data = _load_smelly(after)
        bmap = _collect_smell_methods_for_project(before_data, target_smells)
        amap = _collect_smell_methods_for_project(after_data, target_smells)
        for smell, methods in bmap.items():
            for key, method in methods:
                before_map[smell].add((proj, key, method))
        for smell, methods in amap.items():
            for key, method in methods:
                after_map[smell].add((proj, key, method))

    smell_type_cases = []
    for smell in target_smells:
        bset = before_map.get(smell, set())
        aset = after_map.get(smell, set())
        removed = bset - aset
        persisted = bset & aset
        introduced = aset - bset

        def delta_of(m):
            return method_stats.get(m, {}).get("delta", 0)

        def count_before_of(m):
            return method_stats.get(m, {}).get("count_before", 0)

        # easy: show removed cases
        if any(r.get("smell_type") == smell for r in easy_smells):
            picks = _pick_methods(
                removed,
                rep_per_smell,
                sort_key=lambda m: (delta_of(m), -count_before_of(m)),
                reverse=False,
            )
            for m in picks:
                smell_type_cases.append(
                    {"smell_type": smell, "category": "easy", "change": "removed", "project": m[0], "key": m[1], "method": m[2]}
                )
        # hard: prefer introduced, else persisted
        if any(r.get("smell_type") == smell for r in hard_smells):
            if introduced:
                picks = _pick_methods(introduced, rep_per_smell, sort_key=lambda m: delta_of(m), reverse=True)
                change = "introduced"
            else:
                picks = _pick_methods(persisted, rep_per_smell, sort_key=lambda m: count_before_of(m), reverse=True)
                change = "persisted"
            for m in picks:
                smell_type_cases.append(
                    {"smell_type": smell, "category": "hard", "change": change, "project": m[0], "key": m[1], "method": m[2]}
                )

    # 4) Regression samples + 2) patch patterns use top method cases
    improved = [r for r in detail_rows if _safe_int(r.get("validity_ok", "0")) == 1 and _safe_int(r.get("delta", "0")) < 0]
    unchanged = [r for r in detail_rows if _safe_int(r.get("validity_ok", "0")) == 1 and _safe_int(r.get("delta", "0")) == 0]
    regression = [r for r in detail_rows if _safe_int(r.get("delta", "0")) > 0]

    improved = sorted(improved, key=lambda r: _safe_int(r.get("delta", "0")))[:topk]
    unchanged = sorted(unchanged, key=lambda r: _safe_int(r.get("count_before", "0")), reverse=True)[:topk]
    regression = sorted(regression, key=lambda r: _safe_int(r.get("delta", "0")), reverse=True)[:topk]

    method_cases = []
    for r in improved:
        method_cases.append({"section": "improved", **r})
    for r in unchanged:
        method_cases.append({"section": "unchanged", **r})
    for r in regression:
        method_cases.append({"section": "regression", **r})

    # 6) Rule vs LLM effect
    rule_llm_cases = []
    for proj_dir in sorted(_iter_project_dirs(by_project), key=lambda p: _proj_sort_key(p.name)):
        proj = proj_dir.name
        flags = _collect_method_flags(proj_dir)
        proj_rows = [r for r in detail_rows if r.get("project") == proj and _safe_int(r.get("success", "0")) == 1]
        for r in proj_rows:
            key = r.get("key", "")
            method = r.get("method", "")
            if not key or not method:
                continue
            f = flags.get((key, method), {})
            det = bool(f.get("det"))
            llm = bool(f.get("llm"))
            if det and not llm:
                cat = "deterministic_only"
            elif llm and not det:
                cat = "llm_only"
            elif det and llm:
                cat = "both"
            else:
                cat = "none"
            rule_llm_cases.append({"category": cat, **r})

    def _pick_rule_llm(cat: str) -> List[Dict[str, str]]:
        rows = [r for r in rule_llm_cases if r["category"] == cat]
        rows.sort(key=lambda r: _safe_int(r.get("delta", "0")))
        return rows[:topk]

    rule_llm_samples = _pick_rule_llm("deterministic_only") + _pick_rule_llm("llm_only") + _pick_rule_llm("both")

    # Collect all methods we need smells/diffs for
    needed_by_proj: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    def _add_needed(project: str, key: str, method: str) -> None:
        if project and key and method:
            needed_by_proj[project].add((key, method))

    for c in smell_type_cases:
        _add_needed(c["project"], c["key"], c["method"])
    for r in method_cases:
        _add_needed(r.get("project", ""), r.get("key", ""), r.get("method", ""))
    for r in rule_llm_samples:
        _add_needed(r.get("project", ""), r.get("key", ""), r.get("method", ""))

    smell_sets_before: Dict[Tuple[str, str, str], Set[str]] = {}
    smell_sets_after: Dict[Tuple[str, str, str], Set[str]] = {}
    for proj, methods in needed_by_proj.items():
        proj_dir = by_project / proj
        before = _find_before(proj_dir)
        after = _find_after(proj_dir)
        if not before or not after:
            continue
        before_data = _load_smelly(before)
        after_data = _load_smelly(after)
        bsets = _collect_smell_sets_for_methods(before_data, methods)
        asets = _collect_smell_sets_for_methods(after_data, methods)
        for key, method in methods:
            smell_sets_before[(proj, key, method)] = bsets.get((key, method), set())
            smell_sets_after[(proj, key, method)] = asets.get((key, method), set())

    # Attach smells and diffs
    def _attach_smells_and_diff(item: Dict) -> Dict:
        proj = item.get("project", "")
        key = item.get("key", "")
        method = item.get("method", "")
        before = sorted(smell_sets_before.get((proj, key, method), set()))
        after = sorted(smell_sets_after.get((proj, key, method), set()))
        diff_path = None
        diff_snippet = ""
        if proj and key and method:
            diff = _find_diff(by_project / proj, key, method)
            if diff:
                diff_path = str(diff)
                diff_snippet = _read_diff_snippet(diff)
        return {
            **item,
            "smells_before": before,
            "smells_after": after,
            "diff_path": diff_path or "",
            "diff_snippet": diff_snippet,
        }

    smell_type_cases = [_attach_smells_and_diff(c) for c in smell_type_cases]
    method_cases = [_attach_smells_and_diff(c) for c in method_cases]
    rule_llm_samples = [_attach_smells_and_diff(c) for c in rule_llm_samples]

    # 2) Patch pattern summary (from improved + smell_type_cases)
    pattern_counts: Dict[str, int] = defaultdict(int)
    pattern_by_case: List[Dict[str, object]] = []
    for c in method_cases:
        if c.get("section") != "improved":
            continue
        if not c.get("diff_snippet"):
            continue
        patterns = _diff_patterns(c["diff_snippet"])
        for p in patterns:
            pattern_counts[p] += 1
        pattern_by_case.append(
            {
                "project": c.get("project", ""),
                "key": c.get("key", ""),
                "method": c.get("method", ""),
                "patterns": "|".join(sorted(patterns)),
            }
        )

    # 3) Failure reasons summary
    qual_rows = _read_csv(analysis/exec / "qual_report.csv")
    failure_types = [
        "compile_fail",
        "assertion_fail",
        "runtime_fail",
        "validity_fail",
        "timeout",
        "patch_fail",
        "llm_fail",
        "unknown",
    ]
    failure_totals = {k: 0 for k in failure_types}
    for r in qual_rows:
        for k in failure_types:
            failure_totals[k] += _safe_int(r.get(k, "0"))
    total_failures = sum(failure_totals.values())
    failure_pct = {k: (failure_totals[k] / total_failures if total_failures else 0.0) for k in failure_types}

    # 5) Project size/domain differences
    msr_rows = _read_csv(analysis/smell / "method_success_rate.csv")
    rrate_rows = _read_csv(analysis/smell / "smell_reduction_rate.csv")
    rrate_map = {r.get("project", ""): r for r in rrate_rows}

    sizes = sorted([_safe_int(r.get("attempted_methods", "0")) for r in msr_rows])
    if sizes:
        q1 = sizes[len(sizes) // 3]
        q2 = sizes[(2 * len(sizes)) // 3]
    else:
        q1 = q2 = 0

    domain_summary: Dict[str, Dict[str, float]] = defaultdict(lambda: {"count": 0, "success_rate_sum": 0.0, "reduction_rate_sum": 0.0})
    size_summary: Dict[str, Dict[str, float]] = defaultdict(lambda: {"count": 0, "success_rate_sum": 0.0, "reduction_rate_sum": 0.0})

    size_vals = []
    success_vals = []
    reduction_vals = []

    project_summary_rows = []
    for r in msr_rows:
        project = r.get("project", "")
        attempted = _safe_int(r.get("attempted_methods", "0"))
        success_rate = _safe_float(r.get("success_rate", "0"))
        reduction_rate = _safe_float(rrate_map.get(project, {}).get("reduction_rate", "0"))
        domain = _project_domain(project)
        if attempted <= q1:
            size_bucket = "small"
        elif attempted <= q2:
            size_bucket = "medium"
        else:
            size_bucket = "large"
        domain_summary[domain]["count"] += 1
        domain_summary[domain]["success_rate_sum"] += success_rate
        domain_summary[domain]["reduction_rate_sum"] += reduction_rate
        size_summary[size_bucket]["count"] += 1
        size_summary[size_bucket]["success_rate_sum"] += success_rate
        size_summary[size_bucket]["reduction_rate_sum"] += reduction_rate

        size_vals.append(float(attempted))
        success_vals.append(success_rate)
        reduction_vals.append(reduction_rate)

        project_summary_rows.append(
            {
                "project": project,
                "domain": domain,
                "size_bucket": size_bucket,
                "attempted_methods": attempted,
                "success_rate": success_rate,
                "reduction_rate": reduction_rate,
            }
        )

    size_success_corr = _pearson(size_vals, success_vals)
    size_reduction_corr = _pearson(size_vals, reduction_vals)

    # Write CSV outputs
    smell_cases_csv = out_dir / "qual_smell_smelltype_cases.csv"
    with smell_cases_csv.open("w", newline="") as f:
        fieldnames = [
            "smell_type",
            "category",
            "change",
            "project",
            "key",
            "method",
            "count_before",
            "count_after",
            "delta",
            "smells_before",
            "smells_after",
            "diff_path",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in smell_type_cases:
            stats = method_stats.get((c["project"], c["key"], c["method"]), {})
            w.writerow(
                {
                    "smell_type": c["smell_type"],
                    "category": c["category"],
                    "change": c["change"],
                    "project": c["project"],
                    "key": c["key"],
                    "method": c["method"],
                    "count_before": stats.get("count_before", 0),
                    "count_after": stats.get("count_after", 0),
                    "delta": stats.get("delta", 0),
                    "smells_before": "|".join(c.get("smells_before", [])),
                    "smells_after": "|".join(c.get("smells_after", [])),
                    "diff_path": c.get("diff_path", ""),
                }
            )

    method_cases_csv = out_dir / "qual_smell_method_cases.csv"
    with method_cases_csv.open("w", newline="") as f:
        fieldnames = [
            "section",
            "project",
            "key",
            "method",
            "count_before",
            "count_after",
            "delta",
            "validity_ok",
            "smells_before",
            "smells_after",
            "diff_path",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in method_cases:
            w.writerow(
                {
                    "section": c.get("section", ""),
                    "project": c.get("project", ""),
                    "key": c.get("key", ""),
                    "method": c.get("method", ""),
                    "count_before": c.get("count_before", ""),
                    "count_after": c.get("count_after", ""),
                    "delta": c.get("delta", ""),
                    "validity_ok": c.get("validity_ok", ""),
                    "smells_before": "|".join(c.get("smells_before", [])),
                    "smells_after": "|".join(c.get("smells_after", [])),
                    "diff_path": c.get("diff_path", ""),
                }
            )

    rule_llm_csv = out_dir / "qual_smell_rule_llm_cases.csv"
    with rule_llm_csv.open("w", newline="") as f:
        fieldnames = [
            "category",
            "project",
            "key",
            "method",
            "count_before",
            "count_after",
            "delta",
            "validity_ok",
            "smells_before",
            "smells_after",
            "diff_path",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in rule_llm_samples:
            w.writerow(
                {
                    "category": c.get("category", ""),
                    "project": c.get("project", ""),
                    "key": c.get("key", ""),
                    "method": c.get("method", ""),
                    "count_before": c.get("count_before", ""),
                    "count_after": c.get("count_after", ""),
                    "delta": c.get("delta", ""),
                    "validity_ok": c.get("validity_ok", ""),
                    "smells_before": "|".join(c.get("smells_before", [])),
                    "smells_after": "|".join(c.get("smells_after", [])),
                    "diff_path": c.get("diff_path", ""),
                }
            )

    pattern_csv = out_dir / "qual_smell_patch_patterns.csv"
    with pattern_csv.open("w", newline="") as f:
        fieldnames = ["pattern", "count"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for p, c in sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True):
            w.writerow({"pattern": p, "count": c})

    failure_csv = out_dir / "qual_smell_failure_clusters.csv"
    with failure_csv.open("w", newline="") as f:
        fieldnames = ["failure_type", "count", "pct"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for k in failure_types:
            w.writerow({"failure_type": k, "count": failure_totals[k], "pct": failure_pct[k]})

    domain_csv = out_dir / "qual_smell_domain_summary.csv"
    with domain_csv.open("w", newline="") as f:
        fieldnames = ["group", "category", "count", "avg_success_rate", "avg_reduction_rate"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for domain, agg in sorted(domain_summary.items()):
            c = int(agg["count"])
            w.writerow(
                {
                    "group": "domain",
                    "category": domain,
                    "count": c,
                    "avg_success_rate": (agg["success_rate_sum"] / c if c else 0.0),
                    "avg_reduction_rate": (agg["reduction_rate_sum"] / c if c else 0.0),
                }
            )
        for bucket, agg in sorted(size_summary.items()):
            c = int(agg["count"])
            w.writerow(
                {
                    "group": "size",
                    "category": bucket,
                    "count": c,
                    "avg_success_rate": (agg["success_rate_sum"] / c if c else 0.0),
                    "avg_reduction_rate": (agg["reduction_rate_sum"] / c if c else 0.0),
                }
            )

    project_csv = out_dir / "qual_smell_project_summary.csv"
    project_summary_rows = sorted(project_summary_rows, key=lambda r: _proj_sort_key(r["project"]))
    with project_csv.open("w", newline="") as f:
        fieldnames = ["project", "domain", "size_bucket", "attempted_methods", "success_rate", "reduction_rate"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in project_summary_rows:
            w.writerow(r)

    # JSON report
    report = {
        "params": {"topk": topk, "rep_per_smell": rep_per_smell},
        "easy_smells": easy_smells,
        "hard_smells": hard_smells,
        "smell_type_cases": smell_type_cases,
        "method_cases": method_cases,
        "rule_llm_cases": rule_llm_samples,
        "patch_patterns": {
            "counts": pattern_counts,
            "cases": pattern_by_case,
        },
        "failure_clusters": {"totals": failure_totals, "pct": failure_pct},
        "domain_summary": domain_summary,
        "size_summary": size_summary,
        "size_outcome_corr": {"size_vs_success": size_success_corr, "size_vs_reduction": size_reduction_corr},
    }
    report_json = out_dir / "qual_smell_report.json"
    report_json.write_text(json.dumps(report, indent=2))

    # Markdown summary
    md = []
    md.append("# Qualitative Summary (Quality Issue Reduction)")
    md.append("")
    md.append("## 1) Smell-type success/failure cases")
    md.append("")
    md.append("Top (easy) smells:")
    for r in easy_smells:
        md.append(f"- {r.get('smell_type')}: delta={r.get('delta')}, before={r.get('count_before')}, after={r.get('count_after')}")
    md.append("")
    md.append("Bottom (hard) smells:")
    for r in hard_smells:
        md.append(f"- {r.get('smell_type')}: delta={r.get('delta')}, before={r.get('count_before')}, after={r.get('count_after')}")

    md.append("")
    md.append("## 2) Before/After patch patterns (from improved cases)")
    md.append("")
    for p, c in sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True):
        md.append(f"- {p}: {c}")

    md.append("")
    md.append("## 3) Failure reason clusters")
    md.append("")
    for k in failure_types:
        md.append(f"- {k}: {failure_totals[k]} ({failure_pct[k]:.2%})")

    md.append("")
    md.append("## 4) Regression cases (delta > 0)")
    md.append("")
    for c in [r for r in method_cases if r.get("section") == "regression"]:
        md.append(
            f"- {c.get('project')} {c.get('key')}.{c.get('method')}: "
            f"{c.get('count_before')} -> {c.get('count_after')} (Δ={c.get('delta')})"
        )

    md.append("")
    md.append("## 5) Project size/domain differences")
    md.append("")
    md.append(f"- size vs success_rate corr: {size_success_corr}")
    md.append(f"- size vs reduction_rate corr: {size_reduction_corr}")
    md.append("")
    md.append("Domain averages:")
    for domain, agg in sorted(domain_summary.items()):
        c = int(agg["count"])
        md.append(
            f"- {domain}: avg_success={(agg['success_rate_sum']/c if c else 0.0):.3f}, "
            f"avg_reduction={(agg['reduction_rate_sum']/c if c else 0.0):.3f}, n={c}"
        )

    md.append("")
    md.append("## 6) Rule vs LLM effect (success cases)")
    md.append("")
    for cat in ["deterministic_only", "llm_only", "both"]:
        md.append(f"- {cat}: {len([c for c in rule_llm_samples if c.get('category') == cat])} samples")

    md_path = out_dir / "qual_smell_summary.md"
    md_path.write_text("\n".join(md) + "\n")

    print(f"[ok] wrote: {smell_cases_csv}")
    print(f"[ok] wrote: {method_cases_csv}")
    print(f"[ok] wrote: {rule_llm_csv}")
    print(f"[ok] wrote: {pattern_csv}")
    print(f"[ok] wrote: {failure_csv}")
    print(f"[ok] wrote: {domain_csv}")
    print(f"[ok] wrote: {project_csv}")
    print(f"[ok] wrote: {report_json}")
    print(f"[ok] wrote: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

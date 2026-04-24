#!/usr/bin/env python3
"""Smoke test for Tier 1 deterministic handlers on the SE-GTR v2 dev set.

Loads Smelly-E JSONs from
  `output/by_project/<proj>/smelly_<proj>.json`
and for each (class, test method, smell) that routes to Tier 1, generates
a plan, runs the executor on either the method body or the full file (for
DS), then runs the validator with compile/tests *disabled* (this is an
in-process smoke test, not end-to-end).

Output: summary table of plans / accepted / rejected with rejection reasons.
Also writes JSONL log at `smoke_test_tier1.jsonl` in the CWD.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.operators.base import ExecutionContext, OperatorScope  # noqa: E402
from smell_repair_v2.operators.catalog import get_operator_scope  # noqa: E402
from smell_repair_v2.operators.executor import OperatorExecutor  # noqa: E402
from smell_repair_v2.operators.import_manager import ImportManager  # noqa: E402
from smell_repair_v2.operators.validator import MultiGateValidator, ValidatorConfig  # noqa: E402
from smell_repair_v2.project.discover import (  # noqa: E402
    find_evosuite_test_file,
    resolve_cut_fqcn_from_test,
    Project,
)
from smell_repair_v2.project.java_extract import (  # noqa: E402
    TEST_METHOD_START_RE,
    _scan_to_matching_brace,
)
from smell_repair_v2.tiers.router import get_tier_for_smell  # noqa: E402
from smell_repair_v2.tiers.tier1_deterministic import get_tier1_plan  # noqa: E402


SMELLY_NAME_TO_ID = {
    "Not null assertion": "NNA",
    "Duplicated Setup": "DS",
    "Testing the same exception scenario": "TSES",
    "Asserting Constants": "AC",
    # other smells ignored for Tier 1 smoke test
}


DEV_PROJECTS = [
    "1_tullibee",
    "88_jopenchart",
    "29_apbsmem",
    "71_ext4j",
    "31_xisemele",
]


@dataclass
class Stats:
    plans: int = 0
    accepted: int = 0
    rejected: int = 0
    rejection_reasons: Dict[str, int] = field(default_factory=dict)

    def record(self, accepted: bool, reason: Optional[str]) -> None:
        self.plans += 1
        if accepted:
            self.accepted += 1
        else:
            self.rejected += 1
            key = (reason or "unknown").split(":", 2)[0:2]
            bucket = ":".join(key)
            self.rejection_reasons[bucket] = self.rejection_reasons.get(bucket, 0) + 1


def _locate_class_file(
    project_root: Path, smelly_key: str
) -> Tuple[Optional[Path], Optional[str]]:
    """Smelly-E key format is `<realName>.<CUT_simple>` (e.g. tullibee.Execution).
    Returns (test_file_path, cut_fqcn). Uses rglob to find `<Class>_ESTest.java`.
    """
    if "." not in smelly_key:
        return None, None
    _, cut_simple = smelly_key.split(".", 1)
    proj = Project(folder_name=project_root.name, real_name=project_root.name, root=project_root)
    test_file = find_evosuite_test_file(proj, cut_simple)
    if test_file is None:
        return None, None
    cut_fqcn = resolve_cut_fqcn_from_test(test_file, cut_simple)
    return test_file, cut_fqcn


def _extract_method_with_range(
    file_text: str, method_name: str
) -> Optional[Tuple[str, int, int]]:
    """Return (method_text, start_line, end_line) or None."""
    for m in TEST_METHOD_START_RE.finditer(file_text):
        if m.group("name") != method_name:
            continue
        open_idx = m.end() - 1
        close_idx = _scan_to_matching_brace(file_text, open_idx)
        if close_idx < 0:
            return None
        start = m.start()
        block = file_text[start : close_idx + 1]
        # Compute starting line (1-indexed)
        start_line = file_text.count("\n", 0, start) + 1
        end_line = file_text.count("\n", 0, close_idx + 1) + 1
        return block, start_line, end_line
    return None


def _splice_method_back(file_text: str, method_text_old: str, method_text_new: str) -> str:
    if method_text_old not in file_text:
        return file_text
    return file_text.replace(method_text_old, method_text_new, 1)


def _smell_items_for(class_smells: Dict[str, List[Dict[str, Any]]], smelly_name: str):
    return class_smells.get(smelly_name, [])


def _run_one_smell(
    smell_id: str,
    items: List[Dict[str, Any]],
    class_fqcn: str,
    test_file: Path,
    project_root: Path,
    stats_by_smell: Dict[str, Stats],
    log_fh,
) -> None:
    """Process every evidence item for a given smell within one class file."""
    stats = stats_by_smell.setdefault(smell_id, Stats())

    # Group items by test_method
    by_method: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        tm = item.get("test_method")
        if not tm:
            continue
        by_method.setdefault(tm, []).append(item)

    original_file_text = test_file.read_text(encoding="utf-8", errors="ignore")
    current_file_text = original_file_text

    # For DS the plan is file-level — aggregate evidence across methods and emit once.
    if smell_id == "DS":
        merged_evidence = {"duplicated_setup_groups": []}
        seen_group_ids = set()
        for tm_items in by_method.values():
            for it in tm_items:
                for g in it.get("evidence", {}).get("duplicated_setup_groups", []):
                    gid = g.get("group_id")
                    if gid in seen_group_ids:
                        continue
                    seen_group_ids.add(gid)
                    merged_evidence["duplicated_setup_groups"].append(g)

        if not merged_evidence["duplicated_setup_groups"]:
            return

        ctx = ExecutionContext(
            method_name="__file__",
            method_line_range=(1, len(current_file_text.splitlines())),
            file_text=current_file_text,
            cut_fqcn=class_fqcn,
        )
        plans = get_tier1_plan(
            "DS",
            merged_evidence,
            method_text=current_file_text,
            file_text=current_file_text,
            ctx=ctx,
        ) or []
        _run_plans_and_validate(
            plans=plans,
            scope_text=current_file_text,
            ctx=ctx,
            test_file=test_file,
            project_root=project_root,
            original_file_text=original_file_text,
            original_imports=ImportManager().existing_imports(original_file_text),
            stats=stats,
            log_fh=log_fh,
            smell_id=smell_id,
            class_fqcn=class_fqcn,
            method_name="__file__",
        )
        return

    # NNA / TSES / AC — per-method
    for method_name, tm_items in by_method.items():
        # Merge per-method evidence
        merged: Dict[str, Any] = {}
        for it in tm_items:
            for k, v in (it.get("evidence") or {}).items():
                if isinstance(v, list):
                    merged.setdefault(k, []).extend(v)
                else:
                    merged[k] = v

        extract = _extract_method_with_range(current_file_text, method_name)
        if extract is None:
            _log(log_fh, {
                "status": "skipped", "reason": "method_not_found",
                "project": project_root.name, "class": class_fqcn,
                "method": method_name, "smell": smell_id,
            })
            continue
        method_text, start_line, end_line = extract

        ctx = ExecutionContext(
            method_name=method_name,
            method_line_range=(start_line, end_line),
            file_text=current_file_text,
            cut_fqcn=class_fqcn,
        )
        plans = get_tier1_plan(
            smell_id,
            merged,
            method_text=method_text,
            file_text=current_file_text,
            ctx=ctx,
        )
        if plans is None or not plans:
            _log(log_fh, {
                "status": "skipped", "reason": "no_plan_generated",
                "project": project_root.name, "class": class_fqcn,
                "method": method_name, "smell": smell_id,
            })
            continue

        _run_plans_and_validate(
            plans=plans,
            scope_text=method_text,
            ctx=ctx,
            test_file=test_file,
            project_root=project_root,
            original_file_text=original_file_text,
            original_imports=ImportManager().existing_imports(original_file_text),
            stats=stats,
            log_fh=log_fh,
            smell_id=smell_id,
            class_fqcn=class_fqcn,
            method_name=method_name,
            method_text_old=method_text,
        )


def _run_plans_and_validate(
    *,
    plans,
    scope_text: str,
    ctx: ExecutionContext,
    test_file: Path,
    project_root: Path,
    original_file_text: str,
    original_imports,
    stats: Stats,
    log_fh,
    smell_id: str,
    class_fqcn: str,
    method_name: str,
    method_text_old: Optional[str] = None,
) -> None:
    executor = OperatorExecutor(ImportManager())
    outcome = executor.execute_plan(scope_text, plans, ctx)
    any_success = any(r.success for r in outcome.results)
    if not any_success:
        reasons = [r.rejection_reason for r in outcome.results]
        stats.record(False, "executor:no_op_succeeded")
        _log(log_fh, {
            "status": "rejected", "stage": "executor",
            "project": project_root.name, "class": class_fqcn,
            "method": method_name, "smell": smell_id,
            "n_plans": len(plans), "reasons": reasons,
        })
        return

    # Compose modified file text
    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        modified_file_text = outcome.final_text
    else:
        assert method_text_old is not None
        modified_file_text = _splice_method_back(
            original_file_text, method_text_old, outcome.final_text
        )

    # Reconcile imports (additive-only, strips new JUnit 5 imports)
    mgr = ImportManager()
    modified_file_text, _ = mgr.reconcile(
        modified_file_text, outcome.used_asserts, original_imports=original_imports
    )

    cfg = ValidatorConfig(
        project_root=project_root,
        test_file=test_file,
        skip_compile=True,
        skip_tests=True,
        original_imports=original_imports,
    )
    validator = MultiGateValidator(cfg)
    accepted, reason = validator.validate(original_file_text, modified_file_text, ctx)
    # ensure the real file isn't mutated by the smoke test
    test_file.write_text(original_file_text, encoding="utf-8")

    stats.record(accepted, reason)
    _log(log_fh, {
        "status": "accepted" if accepted else "rejected",
        "stage": "validator",
        "reason": reason,
        "project": project_root.name, "class": class_fqcn,
        "method": method_name, "smell": smell_id,
        "n_plans": len(plans),
        "n_applied": sum(1 for r in outcome.results if r.success),
    })


def _log(fh, record: Dict[str, Any]) -> None:
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _run_project(
    project: str,
    sf110_root: Path,
    output_root: Path,
    log_fh,
) -> Dict[str, Stats]:
    project_root = sf110_root / project
    smelly_json = output_root / "by_project" / project / f"smelly_{project}.json"
    stats_by_smell: Dict[str, Stats] = {}

    if not smelly_json.exists():
        print(f"[skip] {project}: smelly json not found ({smelly_json})")
        return stats_by_smell
    if not project_root.exists():
        print(f"[skip] {project}: project root missing")
        return stats_by_smell

    with smelly_json.open("r", encoding="utf-8") as f:
        smells_by_class = json.load(f)

    for smelly_key, class_smells in smells_by_class.items():
        test_file, cut_fqcn = _locate_class_file(project_root, smelly_key)
        if test_file is None:
            _log(log_fh, {
                "status": "skipped", "reason": "test_file_not_found",
                "project": project, "class": smelly_key,
            })
            continue

        class_fqcn = cut_fqcn or smelly_key

        for smelly_name, items in class_smells.items():
            smell_id = SMELLY_NAME_TO_ID.get(smelly_name)
            if not smell_id:
                continue
            if get_tier_for_smell(smell_id) != 1:
                continue
            if not items:
                continue
            _run_one_smell(
                smell_id,
                items,
                class_fqcn,
                test_file,
                project_root,
                stats_by_smell,
                log_fh,
            )

    return stats_by_smell


def _format_summary(name: str, stats: Dict[str, Stats]) -> str:
    lines = [f"\nProject: {name}"]
    if not stats:
        lines.append("  (no Tier 1 plans generated)")
        return "\n".join(lines)
    for smell_id, s in sorted(stats.items()):
        reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(s.rejection_reasons.items())) or "-"
        lines.append(
            f"  {smell_id:<6} {s.plans:>4} plans  {s.accepted:>4} accepted  {s.rejected:>4} rejected  [{reasons_str}]"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", nargs="+", default=DEV_PROJECTS)
    ap.add_argument("--sf110-root", default=str(REPO_ROOT.parent / "sf110_projects"))
    ap.add_argument(
        "--output-root",
        default=str(REPO_ROOT / "output"),
        help="Directory with by_project/<proj>/smelly_<proj>.json",
    )
    ap.add_argument("--log", default="smoke_test_tier1.jsonl")
    args = ap.parse_args()

    sf110_root = Path(args.sf110_root)
    output_root = Path(args.output_root)
    log_path = Path(args.log)

    total: Dict[str, Stats] = {}
    with log_path.open("w", encoding="utf-8") as log_fh:
        for proj in args.projects:
            project_stats = _run_project(proj, sf110_root, output_root, log_fh)
            print(_format_summary(proj, project_stats))
            for k, v in project_stats.items():
                tot = total.setdefault(k, Stats())
                tot.plans += v.plans
                tot.accepted += v.accepted
                tot.rejected += v.rejected
                for rk, rv in v.rejection_reasons.items():
                    tot.rejection_reasons[rk] = tot.rejection_reasons.get(rk, 0) + rv

    print("\n=== Overall ===")
    total_plans = sum(s.plans for s in total.values())
    total_ok = sum(s.accepted for s in total.values())
    rate = (total_ok / total_plans * 100.0) if total_plans else 0.0
    print(f"Total plans: {total_plans}")
    print(f"Accepted: {total_ok} ({rate:.1f}%)")
    print("By smell:")
    for smell_id, s in sorted(total.items()):
        rate = (s.accepted / s.plans * 100.0) if s.plans else 0.0
        reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(s.rejection_reasons.items())) or "-"
        print(f"  {smell_id:<6} {s.plans:>5} {s.accepted:>5} ({rate:5.1f}%)  [{reasons_str}]")

    print(f"\nLog: {log_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Phase 2.3 Tier 3 dev-experiment runner (gpt_oss_20b only).

Unlike the Tier 2 runner, Tier 3 is evaluated **on top of Phase 2.2's
Tier 2 output**. The input workdir is the `full_run/gpt_oss_20b/<project>/`
produced by the Tier 2 experiment — that way the before→after diff
measures Tier 3's *additional* contribution rather than its absolute effect
on pristine code.

Tier 3 smells: NARV, OIMT, TOFA, ARPM.

Evidence source: the post-Tier-2 Smelly-E JSON
(`dev_tier2_runs/full_run/gpt_oss_20b/smelly_after/after_<proj>.json`).

Outputs mirror `dev_experiment_tier2.py`:
  raw_results.jsonl
  summary_per_cell.csv
  aggregate_by_model.csv      (single-model: gpt_oss_20b)
  model_comparison.md         (Tier 2 vs Tier 2+3 contribution)
  cost_report.md
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.analysis.smelly import load_smelly_json, run_smelly  # noqa: E402
from smell_repair_v2.config.loader import (  # noqa: E402
    BudgetExceededError,
    load_llm_config,
)
from smell_repair_v2.llm.multi_client import MultiModelClient, contains_thinking_artifact  # noqa: E402
from smell_repair_v2.llm.plan_runner import PlanRunner  # noqa: E402
from smell_repair_v2.operators.base import ExecutionContext, OperatorScope  # noqa: E402
from smell_repair_v2.operators.catalog import get_operator_scope  # noqa: E402
from smell_repair_v2.operators.executor import OperatorExecutor  # noqa: E402
from smell_repair_v2.operators.import_manager import ImportManager  # noqa: E402
from smell_repair_v2.operators.validator import MultiGateValidator, ValidatorConfig  # noqa: E402
from smell_repair_v2.project.discover import (  # noqa: E402
    Project,
    find_cut_source_file,
    find_evosuite_test_file,
    resolve_cut_fqcn_from_test,
)
from smell_repair_v2.scripts.e2e_test_tier1 import (  # noqa: E402
    SMELLY_CFG,
    extract_method_with_range,
    prepare_workdir,
    splice_method_back,
)
from smell_repair_v2.tiers.tier3_evidence import (  # noqa: E402
    TIER3_SMELLS,
    is_tier3_smell,
    plan_tier3,
)


TIER3_SMELLY_NAME_TO_ID = {
    "Not asserted return values": "NARV",
    "Asserting object initialization multiple times": "OIMT",
    "Testing only field accesors": "TOFA",
    "Assertion with not related parent class method": "ARPM",
}


DEFAULT_BASE_RUN_DIR = REPO_ROOT / "output" / "dev_tier2_runs" / "full_run"
MODEL_KEY = "gpt_oss_20b"


# ---------------------------------------------------------------------------
# PerCellMetrics — one per (project, smell). Model is fixed so no model key.
# ---------------------------------------------------------------------------


@dataclass
class PerCellMetrics:
    project: str
    smell_id: str
    model_key: str = MODEL_KEY

    total_methods_targeted: int = 0
    llm_calls: int = 0
    parse_success: int = 0
    parse_failures: int = 0
    empty_plans: int = 0
    thinking_artifacts: int = 0

    total_plans_generated: int = 0
    precondition_pass: int = 0
    precondition_fail: int = 0
    precondition_fail_reasons: Dict[str, int] = field(default_factory=dict)

    operator_apply_success: int = 0
    operator_apply_errors: int = 0

    plan_groups_submitted: int = 0
    gate_banned_reject: int = 0
    gate_syntax_reject: int = 0
    gate_compile_reject: int = 0
    gate_test_reject: int = 0
    gate_coverage_reject: int = 0
    gate_smell_substitution_reject: int = 0
    gate_assertion_loss_reject: int = 0
    other_reject: int = 0
    final_accepted: int = 0

    # Tier 3 is measured against post-Tier-2 counts.
    smell_before_count: int = 0   # from post-Tier-2 Smelly JSON
    smell_after_count: int = 0    # from post-Tier-3 Smelly JSON
    new_nna_introduced: int = 0
    new_tsvm_introduced: int = 0
    new_nase_introduced: int = 0
    new_narv_introduced: int = 0

    class_level_test_pass_before: int = 0
    class_level_test_pass_after: int = 0
    regressed_methods: List[str] = field(default_factory=list)

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0

    def smell_reduction_pct(self) -> float:
        if self.smell_before_count == 0:
            return 0.0
        return (self.smell_before_count - self.smell_after_count) / self.smell_before_count * 100.0

    def avg_latency_per_call_ms(self) -> float:
        return (self.total_latency_ms / self.llm_calls) if self.llm_calls else 0.0

    def record_precondition_reason(self, reason: str) -> None:
        bucket = ":".join((reason or "unknown").split(":", 2)[:2])
        self.precondition_fail_reasons[bucket] = self.precondition_fail_reasons.get(bucket, 0) + 1


CellKey = Tuple[str, str]   # (project, smell_id)


_GATE_TO_FIELD = {
    "gate1_banned": "gate_banned_reject",
    "gate2_syntax": "gate_syntax_reject",
    "gate3_compile": "gate_compile_reject",
    "gate4_test": "gate_test_reject",
    "gate5_coverage": "gate_coverage_reject",
    "gate6_smell_sub": "gate_smell_substitution_reject",
    "gate7_assert_loss": "gate_assertion_loss_reject",
    "gate7_no_assertions_left": "gate_assertion_loss_reject",
}


def _classify(reason: Optional[str]) -> str:
    return (reason or "other").split(":", 1)[0]


def _group_by_method(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        tm = it.get("test_method")
        if tm:
            out.setdefault(tm, []).append(it)
    return out


def _merge_evidence(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for it in items:
        for k, v in (it.get("evidence") or {}).items():
            if isinstance(v, list):
                merged.setdefault(k, []).extend(v)
            else:
                merged[k] = v
    return merged


def _measure_class_test_pass(
    work_project: Path, smelly_data: Dict[str, Any], out: Dict[str, bool]
) -> None:
    from smell_repair_v2.operators.validator import _build_sf110_classpath, _test_class_fqcn
    classes: List[Path] = []
    for smelly_key in smelly_data.keys():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj = Project(folder_name=work_project.name, real_name=work_project.name, root=work_project)
        tf = find_evosuite_test_file(proj, cut_simple)
        if tf is not None and tf not in classes:
            classes.append(tf)
    cp = _build_sf110_classpath(work_project)
    for tf in classes:
        fqcn = _test_class_fqcn(tf)
        try:
            proc = subprocess.run(
                ["java", "-cp", cp, "org.junit.runner.JUnitCore", fqcn],
                cwd=str(work_project), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=300, check=False,
            )
            out[fqcn] = (proc.returncode == 0)
        except Exception:
            out[fqcn] = False


def _rerun_smelly(work_project: Path, run_dir: Path, project: str) -> Optional[Dict[str, Any]]:
    smelly_out_dir = run_dir / "smelly_after"
    smelly_out_dir.mkdir(parents=True, exist_ok=True)
    tmp_parent = run_dir / f"tmp_smelly_{project}"
    if tmp_parent.exists():
        shutil.rmtree(tmp_parent)
    tmp_parent.mkdir(parents=True)
    tmp_root = tmp_parent / project
    shutil.copytree(work_project, tmp_root, symlinks=True)
    try:
        print(f"    [{project}] re-running Smelly-E...")
        out = run_smelly(
            smelly_jar=SMELLY_CFG["jar"],
            evosuite_runtime_jar=SMELLY_CFG["evosuite_runtime_jar"],
            junit_jar=SMELLY_CFG["junit_jar"],
            source_path=tmp_root.parent,
            test_path=tmp_root.parent,
            output_dir=smelly_out_dir,
            output_name=f"after_{project}",
            detectors=0, mode=0, sufix=" ", resume_analisis=False, timeout_sec=1800,
        )
        return load_smelly_json(out)
    except Exception as e:
        print(f"    [{project}] Smelly-E re-run failed: {e}")
        return None


# ---------------------------------------------------------------------------
# per-plan-group driver
# ---------------------------------------------------------------------------


def _process_plan_group(
    *,
    metrics: PerCellMetrics,
    project_root: Path,
    test_file: Path,
    ctx: ExecutionContext,
    method_text_old: str,
    plans,
    original_file_text: str,
    original_imports: Set[str],
    raw_log_fh,
    log_record_base: Dict[str, Any],
) -> None:
    metrics.plan_groups_submitted += 1
    metrics.total_plans_generated += len(plans)

    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        scope_text = original_file_text
    else:
        scope_text = method_text_old

    executor = OperatorExecutor(ImportManager())
    outcome = executor.execute_plan(scope_text, plans, ctx)
    for r in outcome.results:
        if r.success:
            metrics.operator_apply_success += 1
            metrics.precondition_pass += 1
        else:
            reason = r.rejection_reason or ""
            if reason.startswith("apply_error"):
                metrics.operator_apply_errors += 1
            else:
                metrics.precondition_fail += 1
                metrics.record_precondition_reason(reason)

    any_success = any(r.success for r in outcome.results)
    log_record = dict(log_record_base)
    log_record["stage"] = "executor"
    log_record["executor_reasons"] = [r.rejection_reason for r in outcome.results]
    log_record["any_success"] = any_success

    if not any_success:
        metrics.other_reject += 1
        log_record["final_accepted"] = False
        raw_log_fh.write(json.dumps(log_record, ensure_ascii=False) + "\n")
        raw_log_fh.flush()
        return

    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        modified_file_text = outcome.final_text
    else:
        modified_file_text = splice_method_back(
            original_file_text, method_text_old, outcome.final_text
        )
    mgr = ImportManager()
    modified_file_text, _ = mgr.reconcile(
        modified_file_text, set(outcome.used_asserts), original_imports=original_imports
    )

    cfg = ValidatorConfig(
        project_root=project_root,
        test_file=test_file,
        skip_compile=False,
        skip_tests=False,
        original_imports=original_imports,
    )
    accepted, reason = MultiGateValidator(cfg).validate(
        original_file_text, modified_file_text, ctx
    )
    log_record["stage"] = "validator"
    log_record["validator_reason"] = reason
    log_record["final_accepted"] = accepted
    raw_log_fh.write(json.dumps(log_record, ensure_ascii=False) + "\n")
    raw_log_fh.flush()

    if accepted:
        metrics.final_accepted += 1
    else:
        field_name = _GATE_TO_FIELD.get(_classify(reason), None)
        if field_name is None:
            metrics.other_reject += 1
        else:
            setattr(metrics, field_name, getattr(metrics, field_name) + 1)


# ---------------------------------------------------------------------------
# per-project driver
# ---------------------------------------------------------------------------


def _run_project(
    *,
    project: str,
    run_dir: Path,
    base_project_root: Path,
    post_tier2_smelly_path: Path,
    runner: PlanRunner,
    multi: MultiModelClient,
    cells: Dict[CellKey, PerCellMetrics],
    raw_log_fh,
    budget_exhausted: Set[str],
    only_smells: Optional[Set[str]] = None,
    only_methods: Optional[Set[str]] = None,
    max_methods_per_cell: Optional[int] = None,
) -> None:
    # Prepare workdir from the Tier 2 output (base_project_root is the
    # already-Tier-2-processed project tree, not pristine sf110).
    work_project = prepare_workdir(run_dir, base_project_root)
    print(f"  [{project}] workdir: {work_project}  (from Tier 2 output)")

    from smell_repair_v2.project.ant import run_ant
    try:
        run_ant(work_project, ["clean", "compile", "compile-evosuite"], timeout_sec=600)
    except Exception as e:
        print(f"  [{project}] initial compile failed: {e}")
        return

    with post_tier2_smelly_path.open() as f:
        post_tier2_smelly = json.load(f)

    class_tests_before: Dict[str, bool] = {}
    _measure_class_test_pass(work_project, post_tier2_smelly, class_tests_before)
    class_pass_before = sum(1 for v in class_tests_before.values() if v)

    # Seed cells with pre-Tier-3 smell counts
    for smelly_name, smell_id in TIER3_SMELLY_NAME_TO_ID.items():
        if only_smells and smell_id not in only_smells:
            continue
        total_before = sum(len(sm.get(smelly_name, []) or []) for sm in post_tier2_smelly.values())
        key = (project, smell_id)
        cell = cells.setdefault(key, PerCellMetrics(project=project, smell_id=smell_id))
        cell.smell_before_count = total_before
        cell.class_level_test_pass_before = class_pass_before

    # Iterate (class, smell, method)
    for smelly_key, class_smells in post_tier2_smelly.items():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj_obj = Project(folder_name=work_project.name, real_name=work_project.name, root=work_project)
        test_file = find_evosuite_test_file(proj_obj, cut_simple)
        if test_file is None:
            continue
        cut_fqcn = resolve_cut_fqcn_from_test(test_file, cut_simple) or smelly_key
        cut_src_text: Optional[str] = None
        cut_src_file = find_cut_source_file(proj_obj, cut_fqcn) if cut_fqcn else None
        if cut_src_file is not None and cut_src_file.exists():
            try:
                cut_src_text = cut_src_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                cut_src_text = None

        original_imports = ImportManager().existing_imports(
            test_file.read_text(encoding="utf-8", errors="ignore")
        )
        pristine_file_text = test_file.read_text(encoding="utf-8", errors="ignore")

        for smelly_name, smell_id in TIER3_SMELLY_NAME_TO_ID.items():
            if only_smells and smell_id not in only_smells:
                continue
            if MODEL_KEY in budget_exhausted:
                return
            items = class_smells.get(smelly_name, []) or []
            if not items:
                continue
            by_method = _group_by_method(items)
            cell = cells[(project, smell_id)]

            for method_name, tm_items in by_method.items():
                if only_methods and method_name not in only_methods:
                    continue
                if max_methods_per_cell is not None and cell.total_methods_targeted >= max_methods_per_cell:
                    break
                if MODEL_KEY in budget_exhausted:
                    return
                if multi.budget_remaining(MODEL_KEY) <= 0.001:
                    budget_exhausted.add(MODEL_KEY)
                    return

                extract = extract_method_with_range(pristine_file_text, method_name)
                if extract is None:
                    continue
                method_text, start_line, end_line = extract
                current_file_text = test_file.read_text(encoding="utf-8", errors="ignore")
                if method_text not in current_file_text:
                    continue

                cell.total_methods_targeted += 1
                ctx = ExecutionContext(
                    method_name=method_name,
                    method_line_range=(start_line, end_line),
                    file_text=pristine_file_text,
                    cut_fqcn=cut_fqcn,
                    cut_source=cut_src_text,
                )
                evidence = _merge_evidence(tm_items)

                before_stats = multi.get_usage(MODEL_KEY)
                b_reqs = before_stats.total_requests
                b_in = before_stats.total_input_tokens
                b_out = before_stats.total_output_tokens
                b_cost = before_stats.total_cost_usd
                b_lat = before_stats.total_latency_ms
                try:
                    t0 = time.monotonic()
                    result = plan_tier3(
                        smell_id=smell_id, evidence=evidence,
                        method_text=method_text, ctx=ctx, runner=runner,
                    )
                    walltime_ms = int((time.monotonic() - t0) * 1000)
                except BudgetExceededError as e:
                    budget_exhausted.add(MODEL_KEY)
                    print(f"  [{MODEL_KEY}] BudgetExceededError: {e}; stopping")
                    return

                after_stats = multi.get_usage(MODEL_KEY)
                cell.llm_calls += after_stats.total_requests - b_reqs
                cell.total_input_tokens += after_stats.total_input_tokens - b_in
                cell.total_output_tokens += after_stats.total_output_tokens - b_out
                cell.total_cost_usd += after_stats.total_cost_usd - b_cost
                cell.total_latency_ms += after_stats.total_latency_ms - b_lat

                if contains_thinking_artifact(result.final_raw_response or ""):
                    cell.thinking_artifacts += 1

                err = result.error or ""
                if "budget" in err.lower():
                    budget_exhausted.add(MODEL_KEY)
                    return

                raw_log_fh.write(json.dumps({
                    "event": "llm_result",
                    "project": project,
                    "class": smelly_key,
                    "method": method_name,
                    "smell_id": smell_id,
                    "attempts": result.attempts,
                    "success": result.success,
                    "error": result.error,
                    "n_plans": len(result.plans),
                    "wall_ms": walltime_ms,
                    "d_input_tokens": after_stats.total_input_tokens - b_in,
                    "d_output_tokens": after_stats.total_output_tokens - b_out,
                    "d_cost_usd": round(after_stats.total_cost_usd - b_cost, 6),
                    "raw_preview": (result.final_raw_response or "")[:300],
                }, ensure_ascii=False) + "\n")
                raw_log_fh.flush()

                if not result.success:
                    cell.parse_failures += 1
                    continue
                cell.parse_success += 1
                if not result.plans:
                    cell.empty_plans += 1
                    continue

                _process_plan_group(
                    metrics=cell,
                    project_root=work_project,
                    test_file=test_file,
                    ctx=ctx,
                    method_text_old=method_text,
                    plans=result.plans,
                    original_file_text=test_file.read_text(encoding="utf-8", errors="ignore"),
                    original_imports=original_imports,
                    raw_log_fh=raw_log_fh,
                    log_record_base={
                        "event": "plan_group",
                        "project": project,
                        "class": smelly_key,
                        "method": method_name,
                        "smell_id": smell_id,
                        "n_plans": len(result.plans),
                    },
                )

    # Rerun Smelly-E on the Tier 3 modified workdir
    after_smelly = _rerun_smelly(work_project, run_dir, project)
    if after_smelly is not None:
        for smelly_name, smell_id in TIER3_SMELLY_NAME_TO_ID.items():
            if only_smells and smell_id not in only_smells:
                continue
            key = (project, smell_id)
            if key in cells:
                cells[key].smell_after_count = sum(
                    len(sm.get(smelly_name, []) or []) for sm in after_smelly.values()
                )

        subst_map = {
            "Not null assertion": "new_nna_introduced",
            "Test without assertions": "new_tsvm_introduced",
            "Not asserted side effects": "new_nase_introduced",
            "Not asserted return values": "new_narv_introduced",
        }
        # "new introduced" means: present in after but not in post-Tier-2.
        before_sets: Dict[str, set] = {k: set() for k in subst_map}
        after_sets: Dict[str, set] = {k: set() for k in subst_map}
        for side, store in ((post_tier2_smelly, before_sets), (after_smelly, after_sets)):
            for cls_key, sm in side.items():
                simple = cls_key.split(".", 1)[-1]
                for sname in subst_map:
                    for it in sm.get(sname, []) or []:
                        store[sname].add((simple, it.get("test_method")))
        for sname, field_name in subst_map.items():
            new_instances = after_sets[sname] - before_sets[sname]
            # attribute once per project (arbitrary cell) to avoid double counting
            for smell_id in TIER3_SMELLY_NAME_TO_ID.values():
                key = (project, smell_id)
                if key in cells:
                    setattr(cells[key], field_name,
                            getattr(cells[key], field_name) + len(new_instances))
                    break

    # Option-3 stale-build guard: after hundreds of Tier-3 modifications the
    # incremental ant build can leave stale .class files that make unrelated
    # classes appear to fail. A single `ant clean compile compile-evosuite`
    # (~1s per project) forces a consistent final build before we measure the
    # post-Tier-3 class-level pass count.
    try:
        run_ant(work_project, ["clean", "compile", "compile-evosuite"], timeout_sec=600)
    except Exception as e:
        print(f"  [{project}] final clean-rebuild failed: {e}")

    class_tests_after: Dict[str, bool] = {}
    _measure_class_test_pass(work_project, post_tier2_smelly, class_tests_after)
    regressed = [
        c for c in class_tests_after
        if class_tests_before.get(c) and not class_tests_after[c]
    ]
    for smelly_name, smell_id in TIER3_SMELLY_NAME_TO_ID.items():
        key = (project, smell_id)
        if key in cells:
            cells[key].class_level_test_pass_after = sum(1 for v in class_tests_after.values() if v)
            cells[key].regressed_methods = list(regressed)


# ---------------------------------------------------------------------------
# output generators
# ---------------------------------------------------------------------------


def _cell_row(cell: PerCellMetrics) -> Dict[str, Any]:
    row = dataclasses.asdict(cell)
    row["smell_reduction_pct"] = round(cell.smell_reduction_pct(), 2)
    row["avg_latency_per_call_ms"] = round(cell.avg_latency_per_call_ms(), 1)
    row["precondition_fail_reasons"] = json.dumps(cell.precondition_fail_reasons, ensure_ascii=False)
    row["regressed_methods"] = ";".join(cell.regressed_methods)
    return row


def _write_summary_csv(run_dir: Path, cells: Dict[CellKey, PerCellMetrics]) -> Path:
    path = run_dir / "summary_per_cell.csv"
    if not cells:
        path.write_text("", encoding="utf-8")
        return path
    rows = [_cell_row(c) for c in cells.values()]
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def _write_summary_md(
    run_dir: Path, cells: Dict[CellKey, PerCellMetrics], multi: MultiModelClient
) -> Path:
    path = run_dir / "tier3_summary.md"
    lines = ["# Tier 3 Dev Evaluation (gpt_oss_20b)\n"]
    lines.append("Tier 3 runs on top of the Phase 2.2 Tier 2 output;")
    lines.append("before counts are POST-Tier-2 smell counts, after counts are POST-Tier-3.\n")
    lines.append("| Project | Smell | Plans | Accepted | Before→After | Red% | NARV+ | NASE+ | NNA+ | TSVM+ | $ |")
    lines.append("|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|")
    for key in sorted(cells.keys()):
        c = cells[key]
        lines.append(
            f"| {c.project} | {c.smell_id} | {c.total_plans_generated} | "
            f"{c.final_accepted}/{c.plan_groups_submitted} | "
            f"{c.smell_before_count}→{c.smell_after_count} | "
            f"{c.smell_reduction_pct():.1f}% | "
            f"{c.new_narv_introduced} | {c.new_nase_introduced} | "
            f"{c.new_nna_introduced} | {c.new_tsvm_introduced} | "
            f"${c.total_cost_usd:.4f} |"
        )

    # aggregate by smell
    lines.append("\n## Aggregated by smell\n")
    lines.append("| Smell | Plans | Accepted | Before→After | Red% |")
    lines.append("|---|---:|---:|---|---:|")
    by_smell: Dict[str, Dict[str, int]] = {}
    for c in cells.values():
        b = by_smell.setdefault(c.smell_id, {
            "plans": 0, "accept": 0, "before": 0, "after": 0
        })
        b["plans"] += c.total_plans_generated
        b["accept"] += c.final_accepted
        b["before"] += c.smell_before_count
        b["after"] += c.smell_after_count
    for sid, b in sorted(by_smell.items()):
        red = ((b["before"] - b["after"]) / b["before"] * 100.0) if b["before"] else 0.0
        lines.append(f"| {sid} | {b['plans']} | {b['accept']} | {b['before']}→{b['after']} | {red:.1f}% |")

    # cost totals
    u = multi.get_usage(MODEL_KEY)
    lines.append(f"\n## Cost\n")
    lines.append(f"Requests: {u.total_requests}, input {u.total_input_tokens}, output {u.total_output_tokens}")
    lines.append(f"Total: ${u.total_cost_usd:.4f}")
    lines.append(f"Avg latency: {u.avg_latency_ms():.0f}ms")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--base-run-dir", type=Path, default=DEFAULT_BASE_RUN_DIR,
                   help="Phase 2.2 full_run dir; Tier 3 reads {model}/{project}/ and smelly_after/.")
    p.add_argument("--output-root", type=Path,
                   default=REPO_ROOT / "output" / "dev_tier3_runs")
    p.add_argument("--run-name", type=str, default=None)
    p.add_argument("--only-projects", nargs="*", default=None)
    p.add_argument("--only-smells", nargs="*", default=None)
    p.add_argument("--only-methods", nargs="*", default=None)
    p.add_argument("--max-methods-per-cell", type=int, default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="Equivalent to --max-methods-per-cell 1 — single smell per project.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        cfg = load_llm_config(args.config)
        cfg.require()
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[setup] {e}", file=sys.stderr)
        return 1

    run_name = args.run_name or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = args.output_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run_dir: {run_dir}")

    multi = MultiModelClient(cfg)
    if MODEL_KEY not in multi.model_keys:
        print(f"[setup] {MODEL_KEY} not configured in llm_config.yaml")
        return 1
    runner = PlanRunner(multi.client_for(MODEL_KEY))

    projects = list(cfg.dev.projects)
    if args.only_projects:
        projects = [p for p in projects if p in set(args.only_projects)]
    smells = set(args.only_smells) if args.only_smells else None
    methods = set(args.only_methods) if args.only_methods else None
    max_per_cell = args.max_methods_per_cell
    if args.dry_run and max_per_cell is None:
        max_per_cell = 1

    print(f"projects: {projects}")
    print(f"smells: {smells or 'ALL Tier 3'}")
    if max_per_cell:
        print(f"max_methods_per_cell: {max_per_cell}")

    raw_log_path = run_dir / "raw_results.jsonl"
    cells: Dict[CellKey, PerCellMetrics] = {}
    budget_exhausted: Set[str] = set()
    t_start = time.time()

    with raw_log_path.open("w", encoding="utf-8") as raw_log_fh:
        for project in projects:
            if MODEL_KEY in budget_exhausted:
                break
            base_project_root = args.base_run_dir / MODEL_KEY / project
            post_tier2_smelly = args.base_run_dir / MODEL_KEY / "smelly_after" / f"after_{project}.json"
            if not base_project_root.exists():
                print(f"  [{project}] missing Tier 2 workdir: {base_project_root}")
                continue
            if not post_tier2_smelly.exists():
                print(f"  [{project}] missing post-Tier-2 smelly json: {post_tier2_smelly}")
                continue
            try:
                _run_project(
                    project=project,
                    run_dir=run_dir,
                    base_project_root=base_project_root,
                    post_tier2_smelly_path=post_tier2_smelly,
                    runner=runner, multi=multi, cells=cells,
                    raw_log_fh=raw_log_fh, budget_exhausted=budget_exhausted,
                    only_smells=smells, only_methods=methods,
                    max_methods_per_cell=max_per_cell,
                )
            except Exception as e:
                print(f"  [{project}] FATAL: {type(e).__name__}: {e}")
                traceback.print_exc()

        u = multi.get_usage(MODEL_KEY)
        print(f"\n[{MODEL_KEY}] usage: {u.total_requests} reqs, ${u.total_cost_usd:.4f}, "
              f"{u.errors} errors, avg {u.avg_latency_ms():.0f}ms")

    elapsed = time.time() - t_start
    print(f"\nElapsed: {elapsed:.1f}s   ({elapsed/60.0:.1f} min)")

    csv_path = _write_summary_csv(run_dir, cells)
    md_path = _write_summary_md(run_dir, cells, multi)

    print(f"\nsummary_per_cell.csv: {csv_path}")
    print(f"tier3_summary.md: {md_path}")
    print(f"raw_results.jsonl: {raw_log_path}")
    if budget_exhausted:
        print(f"\n⚠ budget exhausted: {sorted(budget_exhausted)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

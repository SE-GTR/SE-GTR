#!/usr/bin/env python3
"""Phase 2.2 multi-model Tier 2 dev-experiment runner.

Iterates (model, project, smell, method) and per each method:

  1. Builds prompt inputs + bound PlanRunner
  2. Calls Tier 2 handler → LLM plan
  3. Runs OperatorExecutor
  4. Runs MultiGateValidator (compile + test *active*, Gate 5 is still a stub)
  5. Accumulates per-cell metrics

After all methods in a (model, project) are processed, re-runs Smelly-E on
the model-isolated workdir and computes before/after smell counts.

Outputs
  raw_results.jsonl        — one line per LLM call / per plan-group attempt
  summary_per_cell.csv     — one row per (model, project, smell) cell
  aggregate_by_model.csv   — per-model totals
  model_comparison.md      — markdown summary with model-wise table
  cost_report.md           — $/request and $/accepted-plan per model

Budget safety
  Each model has an independent ``cost_budget_per_model_usd``. When the
  budget is exhausted on a model we mark it as ``budget_exhausted`` and skip
  the remaining cells for that model — other models keep running.
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
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.analysis.smelly import load_smelly_json, run_smelly  # noqa: E402
from smell_repair_v2.config.loader import (  # noqa: E402
    BudgetExceededError,
    LlmRuntimeConfig,
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
# Reuse Phase 1.5 helpers — their semantics are exactly what we need here and
# keeping them in one place avoids divergence.
from smell_repair_v2.scripts.e2e_test_tier1 import (  # noqa: E402
    SMELLY_CFG,
    extract_method_with_range,
    prepare_workdir,
    splice_method_back,
)
from smell_repair_v2.tiers.post_processing import apply_narv_guard  # noqa: E402
from smell_repair_v2.tiers.tier2_template import TIER2_SMELLS, is_tier2_smell, plan_tier2  # noqa: E402


# ---------------------------------------------------------------------------
# smelly-E name ↔ Tier 2 smell_id mapping
# ---------------------------------------------------------------------------

TIER2_SMELLY_NAME_TO_ID = {
    "Exceptions due to null arguments": "ENET",
    "Exceptions due to incomplete setup": "EDIS",
    "Exceptions due to external dependencies": "EDED",
    "Testing the same exception scenario": "TSES",
    "Asserting Constants": "AC",
}

DEFAULT_SMELLY_JSON_ROOT = REPO_ROOT / "output" / "by_project"


# ---------------------------------------------------------------------------
# PerCellMetrics — one per (model, project, smell)
# ---------------------------------------------------------------------------


@dataclass
class PerCellMetrics:
    model_key: str
    project: str
    smell_id: str

    # LLM response quality
    total_methods_targeted: int = 0
    llm_calls: int = 0
    parse_success: int = 0
    parse_failures: int = 0
    empty_plans: int = 0
    thinking_artifacts: int = 0

    # Plan validity
    total_plans_generated: int = 0
    precondition_pass: int = 0
    precondition_fail: int = 0
    precondition_fail_reasons: Dict[str, int] = field(default_factory=dict)

    # Execution
    operator_apply_success: int = 0
    operator_apply_errors: int = 0

    # Gate results (per plan-group)
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

    # Smell-level outcome (filled after Smelly-E re-run)
    smell_before_count: int = 0
    smell_after_count: int = 0
    new_nna_introduced: int = 0
    new_tsvm_introduced: int = 0
    new_nase_introduced: int = 0
    new_narv_introduced: int = 0

    # Class-level test pass (filled at project level but duplicated per cell
    # for CSV convenience — aggregate by taking any cell's value).
    class_level_test_pass_before: int = 0
    class_level_test_pass_after: int = 0
    regressed_methods: List[str] = field(default_factory=list)

    # Resource
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0

    def smell_reduction_pct(self) -> float:
        if self.smell_before_count == 0:
            return 0.0
        return (self.smell_before_count - self.smell_after_count) / self.smell_before_count * 100.0

    def avg_latency_per_call_ms(self) -> float:
        if self.llm_calls == 0:
            return 0.0
        return self.total_latency_ms / self.llm_calls

    def record_precondition_reason(self, reason: str) -> None:
        key = (reason or "unknown").split(":", 2)[:2]
        bucket = ":".join(key)
        self.precondition_fail_reasons[bucket] = (
            self.precondition_fail_reasons.get(bucket, 0) + 1
        )


CellKey = Tuple[str, str, str]   # (model_key, project, smell_id)


# ---------------------------------------------------------------------------
# processing loop
# ---------------------------------------------------------------------------


def _group_items_by_method(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
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
    classes_to_run: List[Path] = []
    for smelly_key in smelly_data.keys():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj = Project(folder_name=work_project.name, real_name=work_project.name, root=work_project)
        tf = find_evosuite_test_file(proj, cut_simple)
        if tf is not None and tf not in classes_to_run:
            classes_to_run.append(tf)
    cp = _build_sf110_classpath(work_project)
    for tf in classes_to_run:
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


def _rerun_smelly(
    work_project: Path, run_dir: Path, project_name: str, model_key: str
) -> Optional[Dict[str, Any]]:
    smelly_out_dir = run_dir / model_key / "smelly_after"
    smelly_out_dir.mkdir(parents=True, exist_ok=True)
    tmp_parent = run_dir / model_key / f"tmp_smelly_{project_name}"
    if tmp_parent.exists():
        shutil.rmtree(tmp_parent)
    tmp_parent.mkdir(parents=True)
    tmp_root = tmp_parent / project_name
    shutil.copytree(work_project, tmp_root, symlinks=True)
    try:
        print(f"    [{model_key}|{project_name}] re-running Smelly-E...")
        out_path = run_smelly(
            smelly_jar=SMELLY_CFG["jar"],
            evosuite_runtime_jar=SMELLY_CFG["evosuite_runtime_jar"],
            junit_jar=SMELLY_CFG["junit_jar"],
            source_path=tmp_root.parent,
            test_path=tmp_root.parent,
            output_dir=smelly_out_dir,
            output_name=f"after_{project_name}",
            detectors=0, mode=0, sufix=" ", resume_analisis=False, timeout_sec=1800,
        )
        return load_smelly_json(out_path)
    except Exception as e:
        print(f"    [{model_key}|{project_name}] Smelly-E re-run failed: {e}")
        return None


# ---------------------------------------------------------------------------
# per-plan-group pipeline
# ---------------------------------------------------------------------------


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


def _classify_validator_reason(reason: Optional[str]) -> str:
    if not reason:
        return "other"
    head = reason.split(":", 1)[0]
    return head


def _process_plan_group(
    *,
    metrics: PerCellMetrics,
    project_root: Path,
    test_file: Path,
    ctx: ExecutionContext,
    method_text_old: Optional[str],
    plans,
    original_file_text: str,
    original_imports: Set[str],
    used_asserts_hint: Set[str],
    raw_log_fh,
    log_record_base: Dict[str, Any],
) -> None:
    """Execute one plan group (list of OperatorPlans) end-to-end."""
    metrics.plan_groups_submitted += 1
    metrics.total_plans_generated += len(plans)

    executor = OperatorExecutor(ImportManager())
    # Per-plan precondition accounting — the executor reports rejection reasons
    # in OperatorResult.rejection_reason for each step.
    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        scope_text = original_file_text
    else:
        assert method_text_old is not None
        scope_text = method_text_old

    outcome = executor.execute_plan(scope_text, plans, ctx)
    for r in outcome.results:
        if r.success:
            metrics.operator_apply_success += 1
            metrics.precondition_pass += 1
        else:
            reason = r.rejection_reason or ""
            if reason.startswith("precondition"):
                metrics.precondition_fail += 1
                metrics.record_precondition_reason(reason)
            elif reason.startswith("apply_error"):
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

    # NARV guard: if a try-catch-removal operator fired, wrap naked non-void
    # calls in assignments so Smelly-E's NARV detector doesn't flag them.
    # This runs BEFORE the validator so the compile+test gates verify the
    # guarded text (user requirement: Gate 3-4 revalidation after guard).
    applied_op_ids = [p.op for p in plans]
    guarded_text = outcome.final_text
    narv_changes = []
    if ctx.cut_source:
        guarded_text, narv_changes = apply_narv_guard(
            outcome.final_text, applied_op_ids, ctx
        )
    if narv_changes:
        log_record["narv_guard"] = [
            {"line": c.line_num, "method": c.method_name, "type": c.return_type, "var": c.var_name}
            for c in narv_changes
        ]

    # compose modified file text
    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        modified_file_text = guarded_text
    else:
        assert method_text_old is not None
        modified_file_text = splice_method_back(
            original_file_text, method_text_old, guarded_text
        )
    mgr = ImportManager()
    modified_file_text, _ = mgr.reconcile(
        modified_file_text,
        set(outcome.used_asserts) | used_asserts_hint,
        original_imports=original_imports,
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
        head = _classify_validator_reason(reason)
        field_name = _GATE_TO_FIELD.get(head)
        if field_name is None:
            metrics.other_reject += 1
        else:
            setattr(metrics, field_name, getattr(metrics, field_name) + 1)


# ---------------------------------------------------------------------------
# project-level driver (per model)
# ---------------------------------------------------------------------------


def _run_model_project(
    *,
    model_key: str,
    project: str,
    run_dir: Path,
    project_root: Path,
    smelly_json_path: Path,
    runner: PlanRunner,
    multi: MultiModelClient,
    cells: Dict[CellKey, PerCellMetrics],
    raw_log_fh,
    budget_exhausted: Set[str],
    only_smells: Optional[Set[str]] = None,
    only_methods: Optional[Set[str]] = None,
    max_methods_per_cell: Optional[int] = None,
) -> None:
    """Run Tier 2 on one (model, project). Mutates `cells` + `raw_log_fh`.
    Sets ``budget_exhausted`` entry when MultiModelClient reports empty budget."""

    model_dir = run_dir / model_key
    model_dir.mkdir(parents=True, exist_ok=True)

    # Isolated workdir: run_dir/<model_key>/<project>/
    work_project = prepare_workdir(model_dir, project_root)
    print(f"  [{model_key}|{project}] workdir: {work_project}")

    from smell_repair_v2.project.ant import run_ant
    try:
        run_ant(work_project, ["clean", "compile", "compile-evosuite"], timeout_sec=600)
    except Exception as e:
        print(f"  [{model_key}|{project}] initial compile failed: {e}")
        return

    with smelly_json_path.open() as f:
        original_smelly = json.load(f)

    # baseline class-level test pass
    class_tests_before: Dict[str, bool] = {}
    _measure_class_test_pass(work_project, original_smelly, class_tests_before)

    # pre-seed cells with smell_before counts
    for smelly_name, smell_id in TIER2_SMELLY_NAME_TO_ID.items():
        if only_smells and smell_id not in only_smells:
            continue
        total_before = sum(len(sm.get(smelly_name, []) or []) for sm in original_smelly.values())
        key = (model_key, project, smell_id)
        cell = cells.setdefault(key, PerCellMetrics(model_key, project, smell_id))
        cell.smell_before_count = total_before
        cell.class_level_test_pass_before = sum(1 for v in class_tests_before.values() if v)

    # iterate classes
    for smelly_key, class_smells in original_smelly.items():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj_obj = Project(folder_name=work_project.name, real_name=work_project.name, root=work_project)
        test_file = find_evosuite_test_file(proj_obj, cut_simple)
        if test_file is None:
            continue
        cut_fqcn = resolve_cut_fqcn_from_test(test_file, cut_simple) or smelly_key
        cut_source_text: Optional[str] = None
        cut_src_file = find_cut_source_file(proj_obj, cut_fqcn) if cut_fqcn else None
        if cut_src_file is not None and cut_src_file.exists():
            try:
                cut_source_text = cut_src_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                cut_source_text = None

        original_imports = ImportManager().existing_imports(
            test_file.read_text(encoding="utf-8", errors="ignore")
        )
        pristine_file_text = test_file.read_text(encoding="utf-8", errors="ignore")

        for smelly_name, smell_id in TIER2_SMELLY_NAME_TO_ID.items():
            if only_smells and smell_id not in only_smells:
                continue
            if model_key in budget_exhausted:
                return
            items = class_smells.get(smelly_name, []) or []
            if not items:
                continue
            by_method = _group_items_by_method(items)

            cell = cells[(model_key, project, smell_id)]

            for method_name, tm_items in by_method.items():
                if only_methods and method_name not in only_methods:
                    continue
                # cell-level cap (cell.total_methods_targeted is incremented below)
                if max_methods_per_cell is not None and cell.total_methods_targeted >= max_methods_per_cell:
                    break
                if model_key in budget_exhausted:
                    return
                if multi.budget_remaining(model_key) <= 0.001:
                    budget_exhausted.add(model_key)
                    print(f"  [{model_key}] budget exhausted (pre-call check); skipping remaining work")
                    return

                extract = extract_method_with_range(pristine_file_text, method_name)
                if extract is None:
                    continue
                method_text, start_line, end_line = extract
                current_file_text = test_file.read_text(encoding="utf-8", errors="ignore")
                if method_text not in current_file_text:
                    # a prior accepted plan rewrote this method — skip to avoid a bad splice
                    continue

                cell.total_methods_targeted += 1

                ctx = ExecutionContext(
                    method_name=method_name,
                    method_line_range=(start_line, end_line),
                    file_text=pristine_file_text,
                    cut_fqcn=cut_fqcn,
                    cut_source=cut_source_text,
                )
                merged_evidence = _merge_evidence(tm_items)

                # Call Tier 2 handler (LLM)
                usage_before = multi.get_usage(model_key)
                before_requests = usage_before.total_requests
                before_in = usage_before.total_input_tokens
                before_out = usage_before.total_output_tokens
                before_cost = usage_before.total_cost_usd
                before_lat = usage_before.total_latency_ms
                try:
                    t0 = time.monotonic()
                    result = plan_tier2(
                        smell_id=smell_id,
                        evidence=merged_evidence,
                        method_text=method_text,
                        ctx=ctx,
                        runner=runner,
                    )
                    walltime_ms = int((time.monotonic() - t0) * 1000)
                except BudgetExceededError as e:
                    budget_exhausted.add(model_key)
                    print(f"  [{model_key}] BudgetExceededError: {e}; skipping remaining work")
                    return

                usage_after = multi.get_usage(model_key)
                d_reqs = usage_after.total_requests - before_requests
                d_in = usage_after.total_input_tokens - before_in
                d_out = usage_after.total_output_tokens - before_out
                d_cost = usage_after.total_cost_usd - before_cost
                d_lat = usage_after.total_latency_ms - before_lat

                cell.llm_calls += d_reqs
                cell.total_input_tokens += d_in
                cell.total_output_tokens += d_out
                cell.total_cost_usd += d_cost
                cell.total_latency_ms += d_lat

                # thinking artifact detection on final raw response
                if contains_thinking_artifact(result.final_raw_response or ""):
                    cell.thinking_artifacts += 1

                # detect budget error surfaced by runner
                err_str = result.error or ""
                if "BudgetExceededError" in err_str or "budget" in err_str.lower():
                    budget_exhausted.add(model_key)
                    print(f"  [{model_key}] budget surfaced through runner; stopping")
                    return

                # log LLM call attempts
                llm_log = {
                    "event": "llm_result",
                    "model_key": model_key,
                    "project": project,
                    "class": smelly_key,
                    "method": method_name,
                    "smell_id": smell_id,
                    "attempts": result.attempts,
                    "success": result.success,
                    "error": result.error,
                    "n_plans": len(result.plans),
                    "wall_ms": walltime_ms,
                    "d_input_tokens": d_in,
                    "d_output_tokens": d_out,
                    "d_cost_usd": round(d_cost, 6),
                    "raw_preview": (result.final_raw_response or "")[:300],
                }
                raw_log_fh.write(json.dumps(llm_log, ensure_ascii=False) + "\n")
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
                    used_asserts_hint=set(),
                    raw_log_fh=raw_log_fh,
                    log_record_base={
                        "event": "plan_group",
                        "model_key": model_key,
                        "project": project,
                        "class": smelly_key,
                        "method": method_name,
                        "smell_id": smell_id,
                        "n_plans": len(result.plans),
                    },
                )

    # Rerun Smelly-E on the modified workdir
    after_smells = _rerun_smelly(work_project, run_dir, project, model_key)

    if after_smells is not None:
        for smelly_name, smell_id in TIER2_SMELLY_NAME_TO_ID.items():
            if only_smells and smell_id not in only_smells:
                continue
            key = (model_key, project, smell_id)
            if key not in cells:
                continue
            cells[key].smell_after_count = sum(
                len(sm.get(smelly_name, []) or []) for sm in after_smells.values()
            )

        # Smell substitution — count *new* instances of substitution-prone smells
        # that weren't in the original JSON for this class+method.
        subst_map = {
            "Not null assertion": "new_nna_introduced",
            "Test without assertions": "new_tsvm_introduced",
            "Not asserted side effects": "new_nase_introduced",
            "Not asserted return values": "new_narv_introduced",
        }
        before_sets: Dict[str, set] = {k: set() for k in subst_map}
        after_sets: Dict[str, set] = {k: set() for k in subst_map}
        for side, store in ((original_smelly, before_sets), (after_smells, after_sets)):
            for cls_key, sm in side.items():
                simple = cls_key.split(".", 1)[-1]
                for sname in subst_map:
                    for it in sm.get(sname, []) or []:
                        store[sname].add((simple, it.get("test_method")))
        for sname, field_name in subst_map.items():
            new_instances = after_sets[sname] - before_sets[sname]
            # attribute substitution count to each Tier 2 cell for this project+model
            for smell_id in TIER2_SMELLY_NAME_TO_ID.values():
                key = (model_key, project, smell_id)
                if key in cells:
                    setattr(cells[key], field_name,
                            getattr(cells[key], field_name) + len(new_instances))
                    break  # only attribute once (arbitrary cell picks it up — we divide later)

    # Option-3 stale-build guard: incremental ant build during a run can
    # leave stale .class files that make unrelated classes appear to fail
    # when we re-measure. A single clean+compile before the final pass
    # count reconciles the build with the current source state.
    from smell_repair_v2.project.ant import run_ant as _run_ant_final
    try:
        _run_ant_final(work_project, ["clean", "compile", "compile-evosuite"], timeout_sec=600)
    except Exception as e:
        print(f"  [{model_key}|{project}] final clean-rebuild failed: {e}")

    # post-repair class-level test pass
    class_tests_after: Dict[str, bool] = {}
    _measure_class_test_pass(work_project, original_smelly, class_tests_after)
    regressed = [
        c for c in class_tests_after
        if class_tests_before.get(c) and not class_tests_after[c]
    ]
    for smelly_name, smell_id in TIER2_SMELLY_NAME_TO_ID.items():
        key = (model_key, project, smell_id)
        if key in cells:
            cells[key].class_level_test_pass_after = sum(
                1 for v in class_tests_after.values() if v
            )
            cells[key].regressed_methods = list(regressed)


# ---------------------------------------------------------------------------
# output generators
# ---------------------------------------------------------------------------


def _cell_row(cell: PerCellMetrics) -> Dict[str, Any]:
    row = dataclasses.asdict(cell)
    row["smell_reduction_pct"] = round(cell.smell_reduction_pct(), 2)
    row["avg_latency_per_call_ms"] = round(cell.avg_latency_per_call_ms(), 1)
    # flatten dict fields
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


def _aggregate_by_model(cells: Dict[CellKey, PerCellMetrics]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for cell in cells.values():
        m = out.setdefault(cell.model_key, {
            "total_methods_targeted": 0,
            "llm_calls": 0,
            "parse_success": 0,
            "parse_failures": 0,
            "empty_plans": 0,
            "thinking_artifacts": 0,
            "total_plans_generated": 0,
            "precondition_pass": 0,
            "precondition_fail": 0,
            "gate_compile_reject": 0,
            "gate_test_reject": 0,
            "gate_smell_substitution_reject": 0,
            "gate_assertion_loss_reject": 0,
            "final_accepted": 0,
            "smell_before_total": 0,
            "smell_after_total": 0,
            "new_narv_introduced": 0,
            "new_nase_introduced": 0,
            "new_nna_introduced": 0,
            "new_tsvm_introduced": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_usd": 0.0,
            "total_latency_ms": 0,
        })
        m["total_methods_targeted"] += cell.total_methods_targeted
        m["llm_calls"] += cell.llm_calls
        m["parse_success"] += cell.parse_success
        m["parse_failures"] += cell.parse_failures
        m["empty_plans"] += cell.empty_plans
        m["thinking_artifacts"] += cell.thinking_artifacts
        m["total_plans_generated"] += cell.total_plans_generated
        m["precondition_pass"] += cell.precondition_pass
        m["precondition_fail"] += cell.precondition_fail
        m["gate_compile_reject"] += cell.gate_compile_reject
        m["gate_test_reject"] += cell.gate_test_reject
        m["gate_smell_substitution_reject"] += cell.gate_smell_substitution_reject
        m["gate_assertion_loss_reject"] += cell.gate_assertion_loss_reject
        m["final_accepted"] += cell.final_accepted
        m["smell_before_total"] += cell.smell_before_count
        m["smell_after_total"] += cell.smell_after_count
        m["new_narv_introduced"] += cell.new_narv_introduced
        m["new_nase_introduced"] += cell.new_nase_introduced
        m["new_nna_introduced"] += cell.new_nna_introduced
        m["new_tsvm_introduced"] += cell.new_tsvm_introduced
        m["total_input_tokens"] += cell.total_input_tokens
        m["total_output_tokens"] += cell.total_output_tokens
        m["total_cost_usd"] += cell.total_cost_usd
        m["total_latency_ms"] += cell.total_latency_ms
    return out


def _write_aggregate_by_model_csv(run_dir: Path, agg: Dict[str, Dict[str, Any]]) -> Path:
    path = run_dir / "aggregate_by_model.csv"
    if not agg:
        path.write_text("", encoding="utf-8")
        return path
    keys = sorted(next(iter(agg.values())).keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model_key", *keys])
        for m, row in agg.items():
            w.writerow([m, *(row[k] for k in keys)])
    return path


def _write_model_comparison_md(
    run_dir: Path, agg: Dict[str, Dict[str, Any]], budget_exhausted: Set[str]
) -> Path:
    path = run_dir / "model_comparison.md"
    lines = ["# Tier 2 Dev Evaluation — Multi-Model Comparison\n"]
    if not agg:
        lines.append("_(no cells produced)_\n")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    lines.append(
        "| Model | Plans | Validity | Precond Pass | Accepted | Smell Red. | "
        "NARV+ | NASE+ | NNA+ | TSVM+ | $ total | $/accept | Avg lat. | Notes |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for m, r in sorted(agg.items()):
        plans = r["total_plans_generated"]
        parse_ok = r["parse_success"]
        attempts = parse_ok + r["parse_failures"]
        validity = f"{(parse_ok/attempts*100.0) if attempts else 0.0:.1f}%"
        precond = r["precondition_pass"]
        precond_total = precond + r["precondition_fail"]
        precond_pct = f"{(precond/precond_total*100.0) if precond_total else 0.0:.1f}%"
        accepted = r["final_accepted"]
        accept_pct = f"{(accepted/plans*100.0) if plans else 0.0:.1f}%"
        reduction = (
            (r['smell_before_total'] - r['smell_after_total'])
            / r['smell_before_total'] * 100.0
            if r['smell_before_total'] else 0.0
        )
        cost = r['total_cost_usd']
        cost_per_accept = cost / accepted if accepted else 0.0
        avg_lat = r['total_latency_ms'] / r['llm_calls'] if r['llm_calls'] else 0.0
        note = "budget_exhausted" if m in budget_exhausted else ""
        lines.append(
            f"| {m} | {plans} | {validity} | {precond_pct} | {accept_pct} | "
            f"-{reduction:.1f}% | {r['new_narv_introduced']} | {r['new_nase_introduced']} | "
            f"{r['new_nna_introduced']} | {r['new_tsvm_introduced']} | "
            f"${cost:.4f} | ${cost_per_accept:.4f} | {avg_lat:.0f}ms | {note} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_cost_report(
    run_dir: Path, multi: MultiModelClient, agg: Dict[str, Dict[str, Any]]
) -> Path:
    path = run_dir / "cost_report.md"
    lines = ["# Cost report\n"]
    lines.append("| Model | Requests | Input tok | Output tok | $ total | $/request | $/accepted plan |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for m_key, usage in multi.all_usage().items():
        agg_row = agg.get(m_key, {})
        accepted = agg_row.get("final_accepted", 0)
        reqs = usage.total_requests
        cost = usage.total_cost_usd
        cost_per_req = cost / reqs if reqs else 0.0
        cost_per_accept = cost / accepted if accepted else 0.0
        lines.append(
            f"| {m_key} | {reqs} | {usage.total_input_tokens} | {usage.total_output_tokens} "
            f"| ${cost:.4f} | ${cost_per_req:.6f} | ${cost_per_accept:.4f} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=None,
                   help="Path to llm_config.yaml (default: config/llm_config.yaml)")
    p.add_argument("--sf110-root", type=Path, default=Path("<ANON_ROOT>/segtr_replication/sf110_projects"))
    p.add_argument("--smelly-json-root", type=Path, default=DEFAULT_SMELLY_JSON_ROOT)
    p.add_argument("--output-root", type=Path,
                   default=REPO_ROOT / "output" / "dev_tier2_runs")
    p.add_argument("--run-name", type=str, default=None,
                   help="Subdir name under --output-root. Defaults to timestamp.")
    p.add_argument("--only-models", nargs="*", default=None)
    p.add_argument("--only-projects", nargs="*", default=None)
    p.add_argument("--only-smells", nargs="*", default=None)
    p.add_argument("--only-methods", nargs="*", default=None,
                   help="Restrict to these test method names (useful for dry-run).")
    p.add_argument("--max-methods-per-cell", type=int, default=None,
                   help="Cap methods per (model,project,smell) cell. Default: unlimited.")
    p.add_argument("--dry-run", action="store_true",
                   help="Equivalent to --max-methods-per-cell 1 and single model/project/smell.")
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
    model_keys = list(multi.model_keys)
    if args.only_models:
        model_keys = [m for m in model_keys if m in set(args.only_models)]
    projects = list(cfg.dev.projects)
    if args.only_projects:
        projects = [p for p in projects if p in set(args.only_projects)]
    smells = set(args.only_smells) if args.only_smells else None
    methods = set(args.only_methods) if args.only_methods else None
    max_per_cell = args.max_methods_per_cell
    if args.dry_run and max_per_cell is None:
        max_per_cell = 1

    print(f"models: {model_keys}")
    print(f"projects: {projects}")
    print(f"smells: {smells or 'ALL Tier 2'}")
    if max_per_cell:
        print(f"max_methods_per_cell: {max_per_cell}")

    raw_log_path = run_dir / "raw_results.jsonl"
    cells: Dict[CellKey, PerCellMetrics] = {}
    budget_exhausted: Set[str] = set()
    t_start = time.time()

    with raw_log_path.open("w", encoding="utf-8") as raw_log_fh:
        for model_key in model_keys:
            print(f"\n=== MODEL {model_key} ===")
            runner = PlanRunner(multi.client_for(model_key))
            for project in projects:
                if model_key in budget_exhausted:
                    break
                project_root = args.sf110_root / project
                smelly_json = args.smelly_json_root / project / f"smelly_{project}.json"
                if not project_root.exists():
                    print(f"  [{project}] missing project root, skipping")
                    continue
                if not smelly_json.exists():
                    print(f"  [{project}] missing smelly json, skipping")
                    continue
                try:
                    _run_model_project(
                        model_key=model_key,
                        project=project,
                        run_dir=run_dir,
                        project_root=project_root,
                        smelly_json_path=smelly_json,
                        runner=runner,
                        multi=multi,
                        cells=cells,
                        raw_log_fh=raw_log_fh,
                        budget_exhausted=budget_exhausted,
                        only_smells=smells,
                        only_methods=methods,
                        max_methods_per_cell=max_per_cell,
                    )
                except Exception as e:
                    print(f"  [{model_key}|{project}] FATAL: {type(e).__name__}: {e}")
                    traceback.print_exc()
            # after each model, emit interim usage snapshot to stdout
            u = multi.get_usage(model_key)
            print(f"  [{model_key}] usage: {u.total_requests} reqs, "
                  f"${u.total_cost_usd:.4f} cost, {u.errors} errors, "
                  f"avg {u.avg_latency_ms():.0f}ms")

    elapsed = time.time() - t_start
    print(f"\nElapsed: {elapsed:.1f}s   ({elapsed/60.0:.1f} min)")

    # outputs
    agg = _aggregate_by_model(cells)
    csv_path = _write_summary_csv(run_dir, cells)
    agg_path = _write_aggregate_by_model_csv(run_dir, agg)
    md_path = _write_model_comparison_md(run_dir, agg, budget_exhausted)
    cost_path = _write_cost_report(run_dir, multi, agg)

    print(f"\nsummary_per_cell.csv: {csv_path}")
    print(f"aggregate_by_model.csv: {agg_path}")
    print(f"model_comparison.md: {md_path}")
    print(f"cost_report.md: {cost_path}")
    print(f"raw_results.jsonl: {raw_log_path}")
    if budget_exhausted:
        print(f"\n⚠ budget exhausted on: {sorted(budget_exhausted)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

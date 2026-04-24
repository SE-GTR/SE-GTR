"""SE-GTR v2 integrated pipeline.

Runs Tier 1 (deterministic) + Tier 2 (template) + Tier 3 (evidence-guided)
handlers on a per-(project, class, method) basis, with Tier 4 stubbed until
Phase 2.4b.

Keeps all Phase 1–2.3 infrastructure (operators, validator, LLM, fewshot,
handlers) immutable. This module is a thin orchestrator that wires them
together and produces the aggregate metrics + artefacts equivalent to what
the individual ``dev_experiment_tier{2,3}.py`` scripts emit today.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.analysis.smelly import load_smelly_json, run_smelly
from smell_repair_v2.config.loader import (
    BudgetExceededError,
    LlmRuntimeConfig,
    load_llm_config,
)
from smell_repair_v2.llm.multi_client import (
    MultiModelClient,
    contains_thinking_artifact,
)
from smell_repair_v2.llm.plan_runner import PlanRunner
from smell_repair_v2.operators.base import (
    ExecutionContext,
    OperatorId,
    OperatorScope,
)
from smell_repair_v2.operators.catalog import get_operator_scope
from smell_repair_v2.operators.executor import OperatorExecutor
from smell_repair_v2.operators.import_manager import ImportManager
from smell_repair_v2.operators.validator import (
    MultiGateValidator,
    ValidatorConfig,
    _build_sf110_classpath,
    _test_class_fqcn,
)
from smell_repair_v2.project.ant import run_ant
from smell_repair_v2.project.discover import (
    Project,
    find_cut_source_file,
    find_evosuite_test_file,
    resolve_cut_fqcn_from_test,
)
from smell_repair_v2.scripts.e2e_test_tier1 import (  # reuse shared helpers
    SMELLY_CFG,
    extract_method_with_range,
    prepare_workdir,
    splice_method_back,
)
from smell_repair_v2.tiers.post_processing import apply_narv_guard
from smell_repair_v2.tiers.tier1_deterministic import (
    get_tier1_plan,
    is_simple_try_catch_pattern,
)
from smell_repair_v2.dynamic.collector import DynamicContextCollector
from smell_repair_v2.tiers.tier2_template import is_tier2_smell, plan_tier2
from smell_repair_v2.tiers.tier3_evidence import is_tier3_smell, plan_tier3
from smell_repair_v2.tiers.tier4_dynamic import (
    CaptureRequest,
    Tier4Result,
    is_tier4_smell,
    plan_tier4,
)
from smell_repair_v2.tiers.naive_llm import (
    NaiveResult,
    repair_test_naive,
)


# --------------------------------------------------------------------------
# Smelly-name ↔ smell_id mappings for each tier
# --------------------------------------------------------------------------

# Tier 1 set covers four smells whose simple patterns are handled
# deterministically; the Tier 1 handler itself falls through to None for
# complex patterns, letting Tier 2 pick them up.
TIER1_SMELLY_NAME_TO_ID = {
    "Not null assertion": "NNA",
    "Duplicated Setup": "DS",
    "Testing the same exception scenario": "TSES",
    "Asserting Constants": "AC",
}

TIER2_SMELLY_NAME_TO_ID = {
    "Exceptions due to null arguments": "ENET",
    "Exceptions due to incomplete setup": "EDIS",
    "Exceptions due to external dependencies": "EDED",
    "Testing the same exception scenario": "TSES",
    "Asserting Constants": "AC",
}

TIER3_SMELLY_NAME_TO_ID = {
    "Not asserted return values": "NARV",
    "Asserting object initialization multiple times": "OIMT",
    "Testing only field accesors": "TOFA",
    "Assertion with not related parent class method": "ARPM",
}

# Tier 4: dynamic-context handler (Phase 2.4b.2).
# TSVM in Smelly-E is named "Multiple calls to the same void method" —
# not "Test without assertions" (that's a different smell Smelly doesn't
# emit here). TSVM's evidence is cross-test (groups of tests calling the
# same void method); per-test repair reuses the corresponding NASE entry.
TIER4_SMELLY_NAME_TO_ID = {
    "Not asserted side effects": "NASE",
    "Multiple calls to the same void method": "TSVM",
}


# Naive LLM baseline (RQ3) — union of the 13 smells addressed across
# Tier 1–4. The baseline feeds all smells for a given method to the LLM
# as a bullet list; it does not route per tier. Ordering is preserved so
# the prompt lists smells in a stable order.
ALL_SMELLY_NAME_TO_ID: Dict[str, str] = {}
for _mapping in (
    TIER1_SMELLY_NAME_TO_ID,
    TIER2_SMELLY_NAME_TO_ID,
    TIER3_SMELLY_NAME_TO_ID,
    TIER4_SMELLY_NAME_TO_ID,
):
    for _sname, _sid in _mapping.items():
        ALL_SMELLY_NAME_TO_ID.setdefault(_sname, _sid)


CONDITION_CHOICES = (
    "full",
    "naive_llm",
    "utrefactor",
    "t1_only",
    "t1_t2",
    "t1_t2_t3",
)


def _enables_for_condition(condition: str) -> Dict[str, bool]:
    """Map a --condition value onto the enable_tier{1..4} flags.

    The naive_llm and utrefactor conditions disable all SE-GTR tiers —
    they have their own code paths in `_process_project`.
    """
    if condition == "full":
        return {"tier1": True, "tier2": True, "tier3": True, "tier4": True}
    if condition == "t1_only":
        return {"tier1": True, "tier2": False, "tier3": False, "tier4": False}
    if condition == "t1_t2":
        return {"tier1": True, "tier2": True, "tier3": False, "tier4": False}
    if condition == "t1_t2_t3":
        return {"tier1": True, "tier2": True, "tier3": True, "tier4": False}
    if condition in ("naive_llm", "utrefactor"):
        return {"tier1": False, "tier2": False, "tier3": False, "tier4": False}
    raise ValueError(f"unknown condition {condition!r}")


# --------------------------------------------------------------------------
# Config / outcome dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineV2Config:
    projects_root: Path
    smelly_json_root: Path
    out_root: Path
    projects: List[str]
    model_key: str = "gpt_oss_20b"
    enable_tier1: bool = True
    enable_tier2: bool = True
    enable_tier3: bool = True
    enable_tier4: bool = False
    # When True, run project-level JaCoCo before and after the tier pipeline
    # so the pipeline summary can report a coverage delta. Phase 2.4c turns
    # this on to satisfy the paper's coverage-preservation claim.
    enable_project_jacoco: bool = False
    # Per-call reasoning-effort override for Tier 4 (gpt-oss style). When
    # set (e.g. "low"/"medium"), it is passed through as a ``chat(...)``
    # kwarg on every Tier 4 LLM call. ``None`` means "use the model's default".
    tier4_reasoning_effort: Optional[str] = None
    run_smelly_after: bool = True
    max_attempts: int = 3
    limit_methods_per_cell: Optional[int] = None
    llm_config_path: Optional[Path] = None
    run_name: Optional[str] = None
    # RQ3 alternative-approach switch. "full" is the SE-GTR pipeline (all
    # tiers). "naive_llm" routes every method through the one-shot LLM
    # rewrite baseline. Tier flags are derived from this field in
    # `run_pipeline_v2`, so callers shouldn't set both.
    condition: str = "full"


@dataclass
class NaiveCellMetrics:
    """Single-cell metrics for one project under the naive_llm baseline.

    Keeps the same field names as ``TierCellMetrics`` where sensible so the
    aggregation / summary code can treat naive as a pseudo-tier (tier=0,
    smell_id='ALL').
    """

    project: str
    smell_id: str = "ALL"
    tier: int = 0

    total_methods_targeted: int = 0
    llm_calls: int = 0
    plan_groups_submitted: int = 0           # methods where a rewrite reached the validator
    plan_groups_accepted: int = 0

    parse_failures: int = 0                  # LLM returned no parseable java block
    llm_errors: int = 0                      # LLM call raised

    gate_banned_reject: int = 0
    gate_syntax_reject: int = 0
    gate_compile_reject: int = 0
    gate_test_reject: int = 0
    gate_coverage_reject: int = 0
    other_reject: int = 0

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    thinking_artifacts: int = 0

    total_retries: int = 0                   # summed over all methods


@dataclass
class TierCellMetrics:
    project: str
    smell_id: str
    tier: int

    total_methods_targeted: int = 0
    llm_calls: int = 0
    plan_groups_submitted: int = 0
    plan_groups_accepted: int = 0
    plans_generated: int = 0
    empty_plans: int = 0
    parse_failures: int = 0

    gate_banned_reject: int = 0
    gate_syntax_reject: int = 0
    gate_compile_reject: int = 0
    gate_test_reject: int = 0
    gate_coverage_reject: int = 0
    gate_smell_sub_reject: int = 0
    gate_assert_loss_reject: int = 0
    other_reject: int = 0

    smell_before_count: int = 0
    smell_after_count: int = 0

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    thinking_artifacts: int = 0

    # Tier 4-specific — zero for other tiers.
    dynamic_capture_attempts: int = 0
    dynamic_capture_success: int = 0
    dynamic_capture_elapsed_ms: int = 0
    dynamic_fail_no_getters: int = 0
    dynamic_fail_compile: int = 0
    dynamic_fail_scope: int = 0
    dynamic_fail_act_line: int = 0
    dynamic_fail_no_markers: int = 0
    dynamic_fail_timeout: int = 0
    dynamic_fail_other: int = 0
    # Accept breakdown by mode (dynamic vs static fallback).
    mode_dynamic_submitted: int = 0
    mode_dynamic_accepted: int = 0
    mode_static_submitted: int = 0
    mode_static_accepted: int = 0
    # Static-fallback warning signal: did the LLM emit assertNotNull?
    assert_notnull_accepted: int = 0


CellKey = Tuple[str, str, int]   # (project, smell_id, tier)


_GATE_TO_FIELD = {
    "gate1_banned": "gate_banned_reject",
    "gate2_syntax": "gate_syntax_reject",
    "gate3_compile": "gate_compile_reject",
    "gate4_test": "gate_test_reject",
    "gate5_coverage": "gate_coverage_reject",
    "gate6_smell_sub": "gate_smell_sub_reject",
    "gate7_assert_loss": "gate_assert_loss_reject",
    "gate7_no_assertions_left": "gate_assert_loss_reject",
}


def _classify_gate(reason: Optional[str]) -> str:
    return (reason or "other").split(":", 1)[0]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


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
    work_project: Path,
    smelly_data: Dict[str, Any],
    out: Dict[str, bool],
) -> None:
    classes: List[Path] = []
    for smelly_key in smelly_data.keys():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj = Project(folder_name=work_project.name, real_name=work_project.name,
                       root=work_project)
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
        out = run_smelly(
            smelly_jar=SMELLY_CFG["jar"],
            evosuite_runtime_jar=SMELLY_CFG["evosuite_runtime_jar"],
            junit_jar=SMELLY_CFG["junit_jar"],
            source_path=tmp_root.parent,
            test_path=tmp_root.parent,
            output_dir=smelly_out_dir,
            output_name=f"after_{project}",
            detectors=0, mode=0, sufix=" ",
            resume_analisis=False, timeout_sec=1800,
        )
        return load_smelly_json(out)
    except Exception as e:
        print(f"  [{project}] Smelly-E re-run failed: {e}")
        return None


# --------------------------------------------------------------------------
# plan-group execution (shared by all tiers)
# --------------------------------------------------------------------------


def _apply_plan_group(
    *,
    metrics: TierCellMetrics,
    work_project: Path,
    test_file: Path,
    ctx: ExecutionContext,
    plans,
    method_text_old: Optional[str],
    original_imports: Set[str],
    raw_log_fh,
    log_base: Dict[str, Any],
    enable_narv_guard: bool,
    tier4_mode: Optional[str] = None,   # "dynamic" | "static_fallback" for Tier 4
) -> bool:
    """Execute one plan group end-to-end. Returns True iff validator accepted.

    ``tier4_mode`` is passed only by the Tier 4 driver. When set, we also
    increment mode-specific accept/submit counters and detect the
    ``assertNotNull`` anti-pattern (a weak oracle that typically indicates
    the static fallback resorted to guessing).
    """
    metrics.plan_groups_submitted += 1
    metrics.plans_generated += len(plans)
    if tier4_mode == "dynamic":
        metrics.mode_dynamic_submitted += 1
    elif tier4_mode == "static_fallback":
        metrics.mode_static_submitted += 1

    original_file_text = test_file.read_text(encoding="utf-8", errors="ignore")

    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        scope_text = original_file_text
    else:
        if method_text_old is None:
            return False
        scope_text = method_text_old

    executor = OperatorExecutor(ImportManager())
    outcome = executor.execute_plan(scope_text, plans, ctx)

    any_success = any(r.success for r in outcome.results)
    log_record = dict(log_base)
    log_record["stage"] = "executor"
    log_record["executor_reasons"] = [r.rejection_reason for r in outcome.results]
    if not any_success:
        metrics.other_reject += 1
        log_record["final_accepted"] = False
        raw_log_fh.write(json.dumps(log_record, ensure_ascii=False) + "\n")
        raw_log_fh.flush()
        return False

    guarded_text = outcome.final_text
    if enable_narv_guard and ctx.cut_source:
        applied_ids = [p.op for p in plans]
        guarded_text, guard_changes = apply_narv_guard(
            outcome.final_text, applied_ids, ctx
        )
        if guard_changes:
            log_record["narv_guard"] = [
                {"line": c.line_num, "method": c.method_name,
                 "type": c.return_type, "var": c.var_name}
                for c in guard_changes
            ]

    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        modified_file_text = guarded_text
    else:
        modified_file_text = splice_method_back(
            original_file_text, method_text_old, guarded_text
        )
    mgr = ImportManager()
    modified_file_text, _ = mgr.reconcile(
        modified_file_text, set(outcome.used_asserts),
        original_imports=original_imports,
    )

    cfg = ValidatorConfig(
        project_root=work_project,
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
        metrics.plan_groups_accepted += 1
        if tier4_mode == "dynamic":
            metrics.mode_dynamic_accepted += 1
        elif tier4_mode == "static_fallback":
            metrics.mode_static_accepted += 1
        # Detect the static-fallback "assertNotNull crutch" anti-pattern so
        # we can flag it in the summary. We only count this when it is a
        # Tier 4 plan (since other tiers legitimately emit assertNotNull).
        if tier4_mode is not None:
            for p in plans:
                atype = (p.params or {}).get("assert_type") \
                        or (p.params or {}).get("new_assert_type")
                if atype == "assertNotNull":
                    metrics.assert_notnull_accepted += 1
                    break
        return True
    else:
        field_name = _GATE_TO_FIELD.get(_classify_gate(reason))
        if field_name is None:
            metrics.other_reject += 1
        else:
            setattr(metrics, field_name, getattr(metrics, field_name) + 1)
        return False


# --------------------------------------------------------------------------
# per-tier processing
# --------------------------------------------------------------------------


def _process_tier1_for_class(
    *,
    smelly_key: str,
    class_smells: Dict[str, List[Dict[str, Any]]],
    work_project: Path,
    test_file: Path,
    cut_fqcn: Optional[str],
    cut_source: Optional[str],
    cells: Dict[CellKey, TierCellMetrics],
    project: str,
    raw_log_fh,
) -> None:
    """Tier 1: NNA / TSES-simple / AC-simple (method-level), then DS (file-level).
    Reuses the Phase 1.5 ordering: method-level first, DS last."""
    original_imports = ImportManager().existing_imports(
        test_file.read_text(encoding="utf-8", errors="ignore")
    )
    pristine_file_text = test_file.read_text(encoding="utf-8", errors="ignore")

    # method-level
    for smelly_name, smell_id in TIER1_SMELLY_NAME_TO_ID.items():
        if smell_id == "DS":
            continue
        items = class_smells.get(smelly_name, []) or []
        if not items:
            continue
        by_method = _group_by_method(items)
        cell = cells.setdefault(
            (project, smell_id, 1),
            TierCellMetrics(project=project, smell_id=smell_id, tier=1),
        )
        for method_name, tm_items in by_method.items():
            evidence = _merge_evidence(tm_items)
            extract = extract_method_with_range(pristine_file_text, method_name)
            if extract is None:
                continue
            method_text, start_line, end_line = extract
            current_file = test_file.read_text(encoding="utf-8", errors="ignore")
            if method_text not in current_file:
                continue
            cell.total_methods_targeted += 1
            ctx = ExecutionContext(
                method_name=method_name,
                method_line_range=(start_line, end_line),
                file_text=pristine_file_text,
                cut_fqcn=cut_fqcn,
                cut_source=cut_source,
            )
            plans = get_tier1_plan(
                smell_id, evidence, method_text=method_text,
                file_text=pristine_file_text, ctx=ctx,
            )
            if not plans:
                continue
            _apply_plan_group(
                metrics=cell, work_project=work_project, test_file=test_file,
                ctx=ctx, plans=plans, method_text_old=method_text,
                original_imports=original_imports, raw_log_fh=raw_log_fh,
                log_base={"tier": 1, "project": project, "class": smelly_key,
                          "method": method_name, "smell_id": smell_id,
                          "n_plans": len(plans)},
                enable_narv_guard=False,
            )

    # DS (file-level) — merge groups then fire once
    ds_items = class_smells.get("Duplicated Setup", []) or []
    if ds_items:
        merged = {"duplicated_setup_groups": []}
        seen = set()
        for it in ds_items:
            for g in it.get("evidence", {}).get("duplicated_setup_groups", []):
                gid = g.get("group_id")
                if gid in seen:
                    continue
                seen.add(gid)
                merged["duplicated_setup_groups"].append(g)
        if merged["duplicated_setup_groups"]:
            file_text_now = test_file.read_text(encoding="utf-8", errors="ignore")
            ctx = ExecutionContext(
                method_name="__file__",
                method_line_range=(1, len(file_text_now.splitlines())),
                file_text=file_text_now, cut_fqcn=cut_fqcn,
                cut_source=cut_source,
            )
            plans = get_tier1_plan(
                "DS", merged, method_text=file_text_now,
                file_text=file_text_now, ctx=ctx,
            ) or []
            if plans:
                cell = cells.setdefault(
                    (project, "DS", 1),
                    TierCellMetrics(project=project, smell_id="DS", tier=1),
                )
                cell.total_methods_targeted += 1
                _apply_plan_group(
                    metrics=cell, work_project=work_project, test_file=test_file,
                    ctx=ctx, plans=plans, method_text_old=None,
                    original_imports=original_imports, raw_log_fh=raw_log_fh,
                    log_base={"tier": 1, "project": project, "class": smelly_key,
                              "method": "__file__", "smell_id": "DS",
                              "n_plans": len(plans)},
                    enable_narv_guard=False,
                )


def _process_llm_tier_for_class(
    *,
    tier: int,
    smelly_key: str,
    class_smells: Dict[str, List[Dict[str, Any]]],
    work_project: Path,
    test_file: Path,
    cut_fqcn: Optional[str],
    cut_source: Optional[str],
    cells: Dict[CellKey, TierCellMetrics],
    project: str,
    runner: PlanRunner,
    multi: MultiModelClient,
    model_key: str,
    raw_log_fh,
    budget_exhausted: Set[str],
    limit_methods_per_cell: Optional[int],
) -> None:
    """Tier 2 / 3 shared driver. Method-level smells only."""
    smells_map = (
        TIER2_SMELLY_NAME_TO_ID if tier == 2 else TIER3_SMELLY_NAME_TO_ID
    )
    plan_func = plan_tier2 if tier == 2 else plan_tier3

    original_imports = ImportManager().existing_imports(
        test_file.read_text(encoding="utf-8", errors="ignore")
    )

    for smelly_name, smell_id in smells_map.items():
        if model_key in budget_exhausted:
            return
        items = class_smells.get(smelly_name, []) or []
        if not items:
            continue
        by_method = _group_by_method(items)
        cell = cells.setdefault(
            (project, smell_id, tier),
            TierCellMetrics(project=project, smell_id=smell_id, tier=tier),
        )

        for method_name, tm_items in by_method.items():
            if limit_methods_per_cell is not None \
               and cell.total_methods_targeted >= limit_methods_per_cell:
                break
            if model_key in budget_exhausted:
                return
            if multi.budget_remaining(model_key) <= 0.001:
                budget_exhausted.add(model_key)
                return

            evidence = _merge_evidence(tm_items)
            current_file = test_file.read_text(encoding="utf-8", errors="ignore")
            extract = extract_method_with_range(current_file, method_name)
            if extract is None:
                continue
            method_text, start_line, end_line = extract
            cell.total_methods_targeted += 1

            ctx = ExecutionContext(
                method_name=method_name,
                method_line_range=(start_line, end_line),
                file_text=current_file, cut_fqcn=cut_fqcn,
                cut_source=cut_source,
            )

            before = multi.get_usage(model_key)
            b_reqs, b_in, b_out, b_cost, b_lat = (
                before.total_requests, before.total_input_tokens,
                before.total_output_tokens, before.total_cost_usd,
                before.total_latency_ms,
            )
            try:
                t0 = time.monotonic()
                result = plan_func(
                    smell_id=smell_id, evidence=evidence,
                    method_text=method_text, ctx=ctx, runner=runner,
                )
                wall_ms = int((time.monotonic() - t0) * 1000)
            except BudgetExceededError as e:
                budget_exhausted.add(model_key)
                print(f"  [{model_key}] BudgetExceededError: {e}; stopping")
                return

            after = multi.get_usage(model_key)
            cell.llm_calls += after.total_requests - b_reqs
            cell.total_input_tokens += after.total_input_tokens - b_in
            cell.total_output_tokens += after.total_output_tokens - b_out
            cell.total_cost_usd += after.total_cost_usd - b_cost
            cell.total_latency_ms += after.total_latency_ms - b_lat

            if contains_thinking_artifact(result.final_raw_response or ""):
                cell.thinking_artifacts += 1

            err = result.error or ""
            if "budget" in err.lower():
                budget_exhausted.add(model_key)
                return

            raw_log_fh.write(json.dumps({
                "event": "llm_result",
                "tier": tier, "project": project, "class": smelly_key,
                "method": method_name, "smell_id": smell_id,
                "attempts": result.attempts, "success": result.success,
                "error": result.error, "n_plans": len(result.plans),
                "wall_ms": wall_ms,
                "d_cost_usd": round(after.total_cost_usd - b_cost, 6),
                "raw_preview": (result.final_raw_response or "")[:300],
            }, ensure_ascii=False) + "\n")
            raw_log_fh.flush()

            if not result.success:
                cell.parse_failures += 1
                continue
            if not result.plans:
                cell.empty_plans += 1
                continue

            _apply_plan_group(
                metrics=cell, work_project=work_project, test_file=test_file,
                ctx=ctx, plans=result.plans, method_text_old=method_text,
                original_imports=original_imports, raw_log_fh=raw_log_fh,
                log_base={"tier": tier, "project": project, "class": smelly_key,
                          "method": method_name, "smell_id": smell_id,
                          "n_plans": len(result.plans)},
                enable_narv_guard=True,
            )


# One DynamicContextCollector per work_project is enough — it's stateless
# apart from the project root it was constructed with, and reusing it across
# classes avoids needless object churn on large projects (29_apbsmem has
# hundreds of NASE items).
_DYNAMIC_COLLECTOR_CACHE: Dict[Path, DynamicContextCollector] = {}


def _dynamic_collector_for(
    work_project: Path,
    config: PipelineV2Config,
) -> DynamicContextCollector:
    key = Path(work_project)
    if key not in _DYNAMIC_COLLECTOR_CACHE:
        _DYNAMIC_COLLECTOR_CACHE[key] = DynamicContextCollector(key)
    return _DYNAMIC_COLLECTOR_CACHE[key]


def _classify_dynamic_failure(err: Optional[str]) -> str:
    """Map a DynamicEvidence.error string onto one of our bucketed counters."""
    if not err:
        return "other"
    e = err.lower()
    if "no_observable_getters" in e:
        return "no_getters"
    if "cut_source" in e or "scope" in e:
        return "scope"
    if "act_call_not_located" in e:
        return "act_line"
    if "compile_error" in e:
        return "compile"
    if "no_markers" in e:
        return "no_markers"
    if "timeout" in e:
        return "timeout"
    return "other"


def _derive_tier4_act_call(
    tm_items: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Extract the first act-call info out of merged NASE evidence entries."""
    for it in tm_items:
        calls = ((it.get("evidence") or {}).get("unverified_side_effect_calls")
                 or [])
        for c in calls:
            ac = c.get("act_call") or {}
            if ac.get("scope") and ac.get("begin_line"):
                return ac
    return None


def _process_tier4_for_class(
    *,
    smelly_key: str,
    class_smells: Dict[str, List[Dict[str, Any]]],
    work_project: Path,
    test_file: Path,
    cut_fqcn: Optional[str],
    cut_source: Optional[str],
    cells: Dict[CellKey, TierCellMetrics],
    project: str,
    runner: PlanRunner,
    multi: MultiModelClient,
    model_key: str,
    raw_log_fh,
    budget_exhausted: Set[str],
    limit_methods_per_cell: Optional[int],
    dynamic_collector: DynamicContextCollector,
    tier4_reasoning_effort: Optional[str],
) -> None:
    """Run NASE first (direct act-call evidence), then TSVM (cross-test
    groups that map back to the matching NASE test)."""
    original_imports = ImportManager().existing_imports(
        test_file.read_text(encoding="utf-8", errors="ignore")
    )

    nase_items_by_method = _group_by_method(
        class_smells.get("Not asserted side effects", []) or []
    )
    processed_pairs: Set[str] = set()

    def _runner_for_tier4() -> PlanRunner:
        """Return a runner that forwards a per-call reasoning-effort override
        for this Tier 4 invocation. We wrap the existing client lazily so
        the outer Tier 2/3 calls keep using the model default."""
        if not tier4_reasoning_effort:
            return runner
        base_client = runner.client

        class _ReasoningEffortClient:
            def chat(self, messages, **overrides):
                # The MultiModelClient forwards ``extra_body`` into the
                # HTTP body and merges it over the per-model thinking
                # config, so caller overrides win. We set both the unified
                # OpenRouter `reasoning.effort` shape and the gpt-oss
                # native `reasoning_effort` string, matching the patterns
                # already used in THINKING_DISABLE_CONFIG.
                caller_extra = dict(overrides.pop("extra_body", None) or {})
                caller_extra.setdefault(
                    "reasoning", {"effort": tier4_reasoning_effort}
                )
                caller_extra.setdefault(
                    "reasoning_effort", tier4_reasoning_effort
                )
                overrides["extra_body"] = caller_extra
                return base_client.chat(messages, **overrides)

        return PlanRunner(_ReasoningEffortClient(), max_attempts=runner.max_attempts)

    tier4_runner = _runner_for_tier4()

    # ---- NASE ----
    def _run_one(
        *, smell_id: str, method_name: str, tm_items, act_call: Dict[str, Any],
    ) -> None:
        if model_key in budget_exhausted:
            return
        cell = cells.setdefault(
            (project, smell_id, 4),
            TierCellMetrics(project=project, smell_id=smell_id, tier=4),
        )
        if limit_methods_per_cell is not None \
                and cell.total_methods_targeted >= limit_methods_per_cell:
            return
        if multi.budget_remaining(model_key) <= 0.001:
            budget_exhausted.add(model_key)
            return

        current_file = test_file.read_text(encoding="utf-8", errors="ignore")
        extract = extract_method_with_range(current_file, method_name)
        if extract is None:
            return
        method_text, start_line, end_line = extract
        cell.total_methods_targeted += 1

        ctx = ExecutionContext(
            method_name=method_name,
            method_line_range=(start_line, end_line),
            file_text=current_file, cut_fqcn=cut_fqcn,
            cut_source=cut_source,
        )
        capture_req = CaptureRequest(
            test_file=test_file,
            test_method_name=method_name,
            act_call_info=act_call,
            cut_source=cut_source,
            cut_fqcn=cut_fqcn,
        )
        evidence_for_prompt = _merge_evidence(tm_items)

        before = multi.get_usage(model_key)
        b_reqs, b_in, b_out, b_cost, b_lat = (
            before.total_requests, before.total_input_tokens,
            before.total_output_tokens, before.total_cost_usd,
            before.total_latency_ms,
        )
        t0 = time.monotonic()
        try:
            result: Tier4Result = plan_tier4(
                smell_id=smell_id,
                evidence=evidence_for_prompt,
                method_text=method_text,
                ctx=ctx, runner=tier4_runner,
                capture_request=capture_req,
                dynamic_collector=dynamic_collector,
            )
        except BudgetExceededError as e:
            budget_exhausted.add(model_key)
            print(f"  [{model_key}] BudgetExceededError: {e}; stopping")
            return
        wall_ms = int((time.monotonic() - t0) * 1000)

        after = multi.get_usage(model_key)
        cell.llm_calls += after.total_requests - b_reqs
        cell.total_input_tokens += after.total_input_tokens - b_in
        cell.total_output_tokens += after.total_output_tokens - b_out
        cell.total_cost_usd += after.total_cost_usd - b_cost
        cell.total_latency_ms += after.total_latency_ms - b_lat

        # Capture-side metrics
        cell.dynamic_capture_attempts += 1
        cell.dynamic_capture_elapsed_ms += result.elapsed_capture_ms
        if result.mode == "dynamic":
            cell.dynamic_capture_success += 1
        elif result.capture_error:
            bucket = _classify_dynamic_failure(result.capture_error)
            attr = f"dynamic_fail_{bucket}"
            if hasattr(cell, attr):
                setattr(cell, attr, getattr(cell, attr) + 1)
            else:
                cell.dynamic_fail_other += 1

        if contains_thinking_artifact(result.plan_result.final_raw_response or ""):
            cell.thinking_artifacts += 1

        raw_log_fh.write(json.dumps({
            "event": "tier4_result",
            "project": project, "class": smelly_key,
            "method": method_name, "smell_id": smell_id,
            "mode": result.mode,
            "capture_error": result.capture_error,
            "elapsed_capture_ms": result.elapsed_capture_ms,
            "dynamic_changed_fields": list(
                (result.dynamic_evidence or {}).get("changed_fields") or {}
            ),
            "attempts": result.attempts,
            "success": result.success,
            "error": result.error,
            "n_plans": len(result.plans),
            "wall_ms": wall_ms,
            "d_cost_usd": round(after.total_cost_usd - b_cost, 6),
        }, ensure_ascii=False) + "\n")
        raw_log_fh.flush()

        if not result.success:
            cell.parse_failures += 1
            return
        if not result.plans:
            cell.empty_plans += 1
            return

        _apply_plan_group(
            metrics=cell, work_project=work_project, test_file=test_file,
            ctx=ctx, plans=result.plans, method_text_old=method_text,
            original_imports=original_imports, raw_log_fh=raw_log_fh,
            log_base={"tier": 4, "project": project, "class": smelly_key,
                      "method": method_name, "smell_id": smell_id,
                      "mode": result.mode,
                      "n_plans": len(result.plans)},
            enable_narv_guard=True,
            tier4_mode=result.mode,
        )
        processed_pairs.add(f"{smell_id}:{smelly_key}:{method_name}")

    # Pass 1 — NASE
    for method_name, tm_items in nase_items_by_method.items():
        act_call = _derive_tier4_act_call(tm_items)
        if act_call is None:
            # No usable act_call — Tier 4 can't instrument; skip cleanly.
            continue
        _run_one(
            smell_id="NASE", method_name=method_name,
            tm_items=tm_items, act_call=act_call,
        )

    # Pass 2 — TSVM (cross-test groups). Per-test repair reuses the matching
    # NASE entry's act_call. Skip tests that NASE already processed to avoid
    # redundant assertions piling up.
    tsvm_items = class_smells.get(
        "Multiple calls to the same void method", []
    ) or []
    tsvm_items_by_method = _group_by_method(tsvm_items)
    for method_name, tm_items in tsvm_items_by_method.items():
        # Was this (class, test) already repaired via NASE?
        if f"NASE:{smelly_key}:{method_name}" in processed_pairs:
            continue
        # Borrow act_call from this test's NASE evidence if present.
        matching_nase = nase_items_by_method.get(method_name) or []
        act_call = _derive_tier4_act_call(matching_nase)
        if act_call is None:
            # Fallback: synthesize from TSVM evidence (void_method_name only).
            # Without a concrete `scope` and `begin_line` the collector will
            # bail, but the LLM can still use static evidence — so we still
            # invoke the handler and let it run in static_fallback mode.
            void_name = None
            for it in tm_items:
                for g in ((it.get("evidence") or {})
                          .get("same_void_method_groups") or []):
                    void_name = g.get("void_method_name")
                    if void_name:
                        break
                if void_name:
                    break
            act_call = {"name": void_name, "scope": None}
        _run_one(
            smell_id="TSVM", method_name=method_name,
            tm_items=tm_items, act_call=act_call,
        )


_GATE_TO_NAIVE_FIELD = {
    "gate1_banned": "gate_banned_reject",
    "gate2_syntax": "gate_syntax_reject",
    "gate3_compile": "gate_compile_reject",
    "gate4_test": "gate_test_reject",
    "gate5_coverage": "gate_coverage_reject",
    "gate5_coverage_proxy": "gate_coverage_reject",
}


def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def _process_naive_for_class(
    *,
    smelly_key: str,
    class_smells: Dict[str, List[Dict[str, Any]]],
    work_project: Path,
    test_file: Path,
    cut_fqcn: Optional[str],
    cut_source: Optional[str],
    naive_cells: Dict[str, NaiveCellMetrics],
    project: str,
    multi: MultiModelClient,
    model_key: str,
    raw_log_fh,
    budget_exhausted: Set[str],
    limit_methods_per_cell: Optional[int],
    rewrites_dir: Optional[Path],
) -> None:
    """RQ3 naive-LLM baseline. One LLM call per smelly method: give it
    the method source + the list of smells on that method, ask for the
    whole rewritten method back. Gate 3/4/5 are enforced; Gate 6/7 are
    disabled so we can see the raw behaviour."""
    cell = naive_cells.setdefault(
        project, NaiveCellMetrics(project=project),
    )

    # Collect (smelly_name, smell_id, evidence) per test method, across
    # all 13 smell types we treat as naive-baseline targets.
    by_method: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = {}
    for smelly_name, smell_id in ALL_SMELLY_NAME_TO_ID.items():
        for item in class_smells.get(smelly_name, []) or []:
            tm = item.get("test_method")
            if not tm:
                continue
            by_method.setdefault(tm, []).append(
                (smelly_name, smell_id, item.get("evidence") or {})
            )

    if not by_method:
        return

    original_imports = ImportManager().existing_imports(
        test_file.read_text(encoding="utf-8", errors="ignore")
    )
    bound_client = multi.client_for(model_key)

    for method_name, smell_tuples in by_method.items():
        if model_key in budget_exhausted:
            return
        if limit_methods_per_cell is not None \
                and cell.total_methods_targeted >= limit_methods_per_cell:
            break
        if multi.budget_remaining(model_key) <= 0.001:
            budget_exhausted.add(model_key)
            return

        current_file = test_file.read_text(encoding="utf-8", errors="ignore")
        extract = extract_method_with_range(current_file, method_name)
        if extract is None:
            continue
        method_text, start_line, end_line = extract
        cell.total_methods_targeted += 1

        ctx = ExecutionContext(
            method_name=method_name,
            method_line_range=(start_line, end_line),
            file_text=current_file,
            cut_fqcn=cut_fqcn,
            cut_source=cut_source,
        )

        before = multi.get_usage(model_key)
        b_reqs = before.total_requests
        b_in = before.total_input_tokens
        b_out = before.total_output_tokens
        b_cost = before.total_cost_usd
        b_lat = before.total_latency_ms

        t0 = time.monotonic()
        try:
            result: NaiveResult = repair_test_naive(
                method_text=method_text,
                smells=smell_tuples,
                project_name=project,
                cut_fqcn=cut_fqcn,
                client=bound_client,
                max_attempts=2,
            )
        except BudgetExceededError as e:
            budget_exhausted.add(model_key)
            print(f"  [{model_key}] BudgetExceededError: {e}; stopping")
            return
        wall_ms = int((time.monotonic() - t0) * 1000)

        after = multi.get_usage(model_key)
        cell.llm_calls += after.total_requests - b_reqs
        cell.total_input_tokens += after.total_input_tokens - b_in
        cell.total_output_tokens += after.total_output_tokens - b_out
        cell.total_cost_usd += after.total_cost_usd - b_cost
        cell.total_latency_ms += after.total_latency_ms - b_lat
        cell.total_retries += result.retry_count

        if contains_thinking_artifact(result.raw_response_preview):
            cell.thinking_artifacts += 1

        raw_log_fh.write(json.dumps({
            "event": "naive_result",
            "project": project, "class": smelly_key,
            "method": method_name,
            "smells": [sid for _, sid, _ in smell_tuples],
            "mode": result.mode,
            "reject_reason": result.reject_reason,
            "retry_count": result.retry_count,
            "wall_ms": wall_ms,
            "d_cost_usd": round(after.total_cost_usd - b_cost, 6),
            "raw_preview": result.raw_response_preview,
        }, ensure_ascii=False) + "\n")
        raw_log_fh.flush()

        if result.mode == "error":
            cell.llm_errors += 1
            continue
        if result.mode != "accepted" or result.rewritten_method is None:
            cell.parse_failures += 1
            continue

        # Optional: persist before/after for inspection.
        if rewrites_dir is not None:
            try:
                rewrites_dir.mkdir(parents=True, exist_ok=True)
                stub = _safe_filename(f"{smelly_key}_{method_name}")
                (rewrites_dir / f"{stub}.before.java").write_text(
                    method_text, encoding="utf-8"
                )
                (rewrites_dir / f"{stub}.after.java").write_text(
                    result.rewritten_method, encoding="utf-8"
                )
            except Exception:
                pass

        original_file_text = current_file
        modified_file_text = splice_method_back(
            original_file_text, method_text, result.rewritten_method
        )
        if modified_file_text == original_file_text:
            # splice_method_back returns the unchanged file if method_text
            # was not found; treat as a parse/match failure so it gets
            # counted distinctly from gate rejections.
            cell.parse_failures += 1
            continue

        # Reconcile imports with a best-effort empty assert set — the
        # naive rewrite may introduce new assertion types but the helper
        # preserves any imports already present in the file.
        mgr = ImportManager()
        modified_file_text, _ = mgr.reconcile(
            modified_file_text, set(), original_imports=original_imports,
        )

        cfg = ValidatorConfig(
            project_root=work_project,
            test_file=test_file,
            skip_compile=False,
            skip_tests=False,
            original_imports=original_imports,
            skip_gate6_gate7=True,   # naive baseline — see ValidatorConfig
        )
        accepted, reason = MultiGateValidator(cfg).validate(
            original_file_text, modified_file_text, ctx
        )

        cell.plan_groups_submitted += 1
        raw_log_fh.write(json.dumps({
            "event": "naive_validator",
            "project": project, "class": smelly_key,
            "method": method_name,
            "accepted": accepted, "reason": reason,
        }, ensure_ascii=False) + "\n")
        raw_log_fh.flush()

        if accepted:
            cell.plan_groups_accepted += 1
        else:
            field_name = _GATE_TO_NAIVE_FIELD.get(_classify_gate(reason))
            if field_name is None:
                cell.other_reject += 1
            else:
                setattr(cell, field_name, getattr(cell, field_name) + 1)


# --------------------------------------------------------------------------
# project-level driver
# --------------------------------------------------------------------------


def _process_project(
    *,
    project: str,
    config: PipelineV2Config,
    run_dir: Path,
    original_smelly: Dict[str, Any],
    runner: PlanRunner,
    multi: MultiModelClient,
    cells: Dict[CellKey, TierCellMetrics],
    raw_log_fh,
    budget_exhausted: Set[str],
    naive_cells: Optional[Dict[str, NaiveCellMetrics]] = None,
) -> Dict[str, Any]:
    """Returns the per-project summary dict."""
    base_project_root = config.projects_root / project
    work_project = prepare_workdir(run_dir, base_project_root)
    print(f"  [{project}] workdir: {work_project}")

    try:
        run_ant(work_project, ["clean", "compile", "compile-evosuite"],
                timeout_sec=600)
    except Exception as e:
        print(f"  [{project}] initial compile failed: {e}")
        return {"project": project, "error": str(e)}

    class_tests_before: Dict[str, bool] = {}
    _measure_class_test_pass(work_project, original_smelly, class_tests_before)

    # Project-level JaCoCo: pristine-workdir baseline. We run this before
    # any tier touches the tree so the "after" measurement is comparable.
    jacoco_before: Optional[Dict[str, Any]] = None
    if config.enable_project_jacoco:
        try:
            from smell_repair_v2.coverage.jacoco import run_jacoco
            jacoco_before = run_jacoco(work_project, project_name=project).to_dict()
            print(f"  [{project}] jacoco before: "
                  f"line={jacoco_before['line_coverage']:.4f} "
                  f"branch={jacoco_before['branch_coverage']:.4f} "
                  f"inst={jacoco_before['instruction_coverage']:.4f}")
        except Exception as e:
            print(f"  [{project}] jacoco before FAIL: {e}")
            jacoco_before = {"error": str(e)}

    # per-class loop
    for smelly_key, class_smells in original_smelly.items():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj_obj = Project(folder_name=work_project.name,
                           real_name=work_project.name, root=work_project)
        test_file = find_evosuite_test_file(proj_obj, cut_simple)
        if test_file is None:
            continue
        cut_fqcn = resolve_cut_fqcn_from_test(test_file, cut_simple) or smelly_key
        cut_source: Optional[str] = None
        cut_src_file = find_cut_source_file(proj_obj, cut_fqcn) if cut_fqcn else None
        if cut_src_file is not None and cut_src_file.exists():
            try:
                cut_source = cut_src_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                cut_source = None

        # Tier 1
        if config.enable_tier1:
            _process_tier1_for_class(
                smelly_key=smelly_key, class_smells=class_smells,
                work_project=work_project, test_file=test_file,
                cut_fqcn=cut_fqcn, cut_source=cut_source,
                cells=cells, project=project, raw_log_fh=raw_log_fh,
            )

        # Tier 2
        if config.enable_tier2 and config.model_key not in budget_exhausted:
            _process_llm_tier_for_class(
                tier=2, smelly_key=smelly_key, class_smells=class_smells,
                work_project=work_project, test_file=test_file,
                cut_fqcn=cut_fqcn, cut_source=cut_source,
                cells=cells, project=project,
                runner=runner, multi=multi, model_key=config.model_key,
                raw_log_fh=raw_log_fh, budget_exhausted=budget_exhausted,
                limit_methods_per_cell=config.limit_methods_per_cell,
            )

        # Tier 3
        if config.enable_tier3 and config.model_key not in budget_exhausted:
            _process_llm_tier_for_class(
                tier=3, smelly_key=smelly_key, class_smells=class_smells,
                work_project=work_project, test_file=test_file,
                cut_fqcn=cut_fqcn, cut_source=cut_source,
                cells=cells, project=project,
                runner=runner, multi=multi, model_key=config.model_key,
                raw_log_fh=raw_log_fh, budget_exhausted=budget_exhausted,
                limit_methods_per_cell=config.limit_methods_per_cell,
            )

        # Tier 4 — dynamic-context repair (NASE / TSVM). The collector is
        # constructed once per project; it reuses the same workdir the
        # earlier tiers already rebuilt.
        if config.enable_tier4 and config.model_key not in budget_exhausted:
            _process_tier4_for_class(
                smelly_key=smelly_key, class_smells=class_smells,
                work_project=work_project, test_file=test_file,
                cut_fqcn=cut_fqcn, cut_source=cut_source,
                cells=cells, project=project,
                runner=runner, multi=multi, model_key=config.model_key,
                raw_log_fh=raw_log_fh, budget_exhausted=budget_exhausted,
                limit_methods_per_cell=config.limit_methods_per_cell,
                dynamic_collector=_dynamic_collector_for(work_project, config),
                tier4_reasoning_effort=config.tier4_reasoning_effort,
            )

        # RQ3 naive-LLM baseline. All smells for a method are handed to
        # the LLM in a single prompt; the rewritten method is spliced
        # back and runs through Gates 3/4/5 (Gate 6/7 disabled).
        if config.condition == "naive_llm" \
                and config.model_key not in budget_exhausted \
                and naive_cells is not None:
            _process_naive_for_class(
                smelly_key=smelly_key, class_smells=class_smells,
                work_project=work_project, test_file=test_file,
                cut_fqcn=cut_fqcn, cut_source=cut_source,
                naive_cells=naive_cells, project=project,
                multi=multi, model_key=config.model_key,
                raw_log_fh=raw_log_fh, budget_exhausted=budget_exhausted,
                limit_methods_per_cell=config.limit_methods_per_cell,
                rewrites_dir=run_dir / "naive_rewrites" / project,
            )

    # --- End of class loop: final hygiene + aggregate measurements ---
    try:
        run_ant(work_project, ["clean", "compile", "compile-evosuite"],
                timeout_sec=600)
    except Exception as e:
        print(f"  [{project}] final clean-rebuild failed: {e}")

    class_tests_after: Dict[str, bool] = {}
    _measure_class_test_pass(work_project, original_smelly, class_tests_after)
    pass_before = sum(1 for v in class_tests_before.values() if v)
    pass_after = sum(1 for v in class_tests_after.values() if v)
    regressed = sorted([
        c for c in class_tests_after
        if class_tests_before.get(c) and not class_tests_after[c]
    ])

    # Project-level JaCoCo: post-run. Run AFTER the final clean-rebuild so
    # the .class set matches what we just measured for test pass.
    jacoco_after: Optional[Dict[str, Any]] = None
    if config.enable_project_jacoco:
        try:
            from smell_repair_v2.coverage.jacoco import run_jacoco
            jacoco_after = run_jacoco(work_project, project_name=project).to_dict()
            print(f"  [{project}] jacoco after:  "
                  f"line={jacoco_after['line_coverage']:.4f} "
                  f"branch={jacoco_after['branch_coverage']:.4f} "
                  f"inst={jacoco_after['instruction_coverage']:.4f}")
        except Exception as e:
            print(f"  [{project}] jacoco after FAIL: {e}")
            jacoco_after = {"error": str(e)}

    after_smelly = None
    if config.run_smelly_after:
        after_smelly = _rerun_smelly(work_project, run_dir, project)

    # fill smell_before/after counts per cell
    for (proj, smell_id, tier), cell in cells.items():
        if proj != project:
            continue
        smelly_name = _smelly_name_for(smell_id)
        if smelly_name:
            cell.smell_before_count = sum(
                len(sm.get(smelly_name, []) or []) for sm in original_smelly.values()
            )
            if after_smelly is not None:
                cell.smell_after_count = sum(
                    len(sm.get(smelly_name, []) or []) for sm in after_smelly.values()
                )

    report: Dict[str, Any] = {
        "project": project,
        "condition": config.condition,
        "class_tests_before": pass_before,
        "class_tests_after": pass_after,
        "class_tests_total": len(class_tests_before),
        "regressed_classes": regressed,
    }
    if config.enable_project_jacoco:
        report["jacoco_before"] = jacoco_before
        report["jacoco_after"] = jacoco_after
    if after_smelly is not None:
        # Persist the per-class/per-smell before/after counts for all 13
        # smells so the summary can render a cumulative table.
        report["smell_totals_before"] = _aggregate_smell_totals(original_smelly)
        report["smell_totals_after"] = _aggregate_smell_totals(after_smelly)

    # Naive-baseline roll-up: the single NaiveCellMetrics cell for this
    # project carries accept / reject / cost data that mirrors what the
    # paper's RQ3 table needs.
    if config.condition == "naive_llm" and naive_cells is not None \
            and project in naive_cells:
        nc = naive_cells[project]
        report["naive"] = {
            "total_methods_attempted": nc.total_methods_targeted,
            "submitted_to_validator": nc.plan_groups_submitted,
            "accepted": nc.plan_groups_accepted,
            "rejected": {
                "parse_fail": nc.parse_failures,
                "llm_error": nc.llm_errors,
                "gate1_banned": nc.gate_banned_reject,
                "gate2_syntax": nc.gate_syntax_reject,
                "gate3_compile": nc.gate_compile_reject,
                "gate4_test": nc.gate_test_reject,
                "gate5_coverage": nc.gate_coverage_reject,
                "other": nc.other_reject,
            },
            "llm_calls": nc.llm_calls,
            "total_retries": nc.total_retries,
            "cost_usd": round(nc.total_cost_usd, 6),
            "input_tokens": nc.total_input_tokens,
            "output_tokens": nc.total_output_tokens,
            "thinking_artifacts": nc.thinking_artifacts,
        }
    return report


def _aggregate_smell_totals(smelly_data: Dict[str, Any]) -> Dict[str, int]:
    """Sum up per-smell counts across all classes so we can print the
    before/after table the paper needs."""
    totals: Dict[str, int] = {}
    for class_smells in smelly_data.values():
        if not isinstance(class_smells, dict):
            continue
        for smell_name, items in class_smells.items():
            if isinstance(items, list):
                totals[smell_name] = totals.get(smell_name, 0) + len(items)
    return totals


def _smelly_name_for(smell_id: str) -> Optional[str]:
    for mapping in (TIER1_SMELLY_NAME_TO_ID, TIER2_SMELLY_NAME_TO_ID,
                    TIER3_SMELLY_NAME_TO_ID, TIER4_SMELLY_NAME_TO_ID):
        for name, sid in mapping.items():
            if sid == smell_id:
                return name
    return None


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def run_pipeline_v2(config: PipelineV2Config) -> Path:
    # Condition → tier enable flags. The field-by-field override keeps
    # legacy callers that already set enable_tier{1..4} explicitly working
    # (condition defaults to "full", which matches the old behavior).
    if config.condition != "full":
        flags = _enables_for_condition(config.condition)
        config = dataclasses.replace(
            config,
            enable_tier1=flags["tier1"],
            enable_tier2=flags["tier2"],
            enable_tier3=flags["tier3"],
            enable_tier4=flags["tier4"],
        )
    if config.condition == "utrefactor":
        raise NotImplementedError(
            "condition='utrefactor' is declared for RQ3 but not implemented "
            "in this module; use the standalone UTRefactor baseline harness."
        )

    llm_cfg = load_llm_config(config.llm_config_path)
    llm_cfg.require()
    multi = MultiModelClient(llm_cfg)
    if config.model_key not in multi.model_keys:
        raise RuntimeError(
            f"model {config.model_key!r} not in llm_config.yaml; configured: "
            f"{sorted(multi.model_keys)}"
        )
    runner = PlanRunner(multi.client_for(config.model_key),
                        max_attempts=config.max_attempts)

    run_name = config.run_name or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = config.out_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run_dir: {run_dir}")
    print(f"condition: {config.condition}")
    print(f"tiers enabled: "
          f"T1={config.enable_tier1} T2={config.enable_tier2} "
          f"T3={config.enable_tier3} T4={config.enable_tier4}")
    print(f"projects: {config.projects}")

    raw_log_path = run_dir / "raw_results.jsonl"
    cells: Dict[CellKey, TierCellMetrics] = {}
    naive_cells: Dict[str, NaiveCellMetrics] = {}
    budget_exhausted: Set[str] = set()
    per_project: List[Dict[str, Any]] = []
    t_start = time.time()

    with raw_log_path.open("w", encoding="utf-8") as raw_log_fh:
        for project in config.projects:
            smelly_json = config.smelly_json_root / project / f"smelly_{project}.json"
            if not smelly_json.exists():
                print(f"  [{project}] missing smelly json at {smelly_json}")
                continue
            original_smelly = load_smelly_json(smelly_json)
            try:
                report = _process_project(
                    project=project, config=config, run_dir=run_dir,
                    original_smelly=original_smelly,
                    runner=runner, multi=multi, cells=cells,
                    raw_log_fh=raw_log_fh, budget_exhausted=budget_exhausted,
                    naive_cells=naive_cells,
                )
                per_project.append(report)
            except Exception as e:
                print(f"  [{project}] FATAL: {type(e).__name__}: {e}")
                traceback.print_exc()
                per_project.append({"project": project, "error": str(e)})

    elapsed = time.time() - t_start
    print(f"\nelapsed: {elapsed:.1f}s   ({elapsed/60.0:.1f} min)")
    u = multi.get_usage(config.model_key)
    print(f"[{config.model_key}] usage: {u.total_requests} reqs, "
          f"${u.total_cost_usd:.4f}, {u.errors} errors")

    _write_outputs(run_dir, cells, per_project, u, naive_cells=naive_cells)
    return run_dir


def _write_outputs(
    run_dir: Path,
    cells: Dict[CellKey, TierCellMetrics],
    per_project: List[Dict[str, Any]],
    usage,
    *,
    naive_cells: Optional[Dict[str, NaiveCellMetrics]] = None,
) -> None:
    # ---- summary_per_cell.csv ----
    if cells:
        rows = []
        for c in cells.values():
            r = dataclasses.asdict(c)
            r["accept_rate"] = (
                c.plan_groups_accepted / c.plan_groups_submitted * 100.0
                if c.plan_groups_submitted else 0.0
            )
            rows.append(r)
        path = run_dir / "summary_per_cell.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow(r)

    # ---- per_project.json ----
    (run_dir / "per_project.json").write_text(
        json.dumps(per_project, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ---- pipeline_summary.md ----
    lines: List[str] = ["# Pipeline v2 integrated run\n"]

    # 1. Per-project class-test table
    lines.append("## Class-test pass rate (before → after)\n")
    lines.append("| Project | Class tests before | after | Δ | Regressed |")
    lines.append("|---|---:|---:|---:|---|")
    for p in per_project:
        b, a, t = p.get("class_tests_before"), p.get("class_tests_after"), p.get("class_tests_total")
        reg = p.get("regressed_classes") or []
        if b is None:
            lines.append(f"| {p['project']} | error | error |  |  |")
        else:
            lines.append(f"| {p['project']} | {b}/{t} | {a}/{t} | {a-b:+d} | {len(reg)} |")

    # 2. Aggregated per (tier, smell) cell
    lines.append("\n## Per (tier, smell) cell\n")
    lines.append("| Tier | Smell | Methods | Plan grps | Accepted | "
                 "Accept % | Before → After |")
    lines.append("|---:|---|---:|---:|---:|---:|---|")
    for key in sorted(cells.keys(), key=lambda k: (k[2], k[1])):
        c = cells[key]
        ar = (c.plan_groups_accepted / c.plan_groups_submitted * 100.0
              if c.plan_groups_submitted else 0.0)
        lines.append(
            f"| {c.tier} | {c.smell_id} | {c.total_methods_targeted} "
            f"| {c.plan_groups_submitted} | {c.plan_groups_accepted} "
            f"| {ar:.1f}% "
            f"| {c.smell_before_count} → {c.smell_after_count} |"
        )

    # 3. Tier 4 — dynamic capture success rate
    tier4_cells = [c for c in cells.values() if c.tier == 4]
    if tier4_cells:
        lines.append("\n## Tier 4 — dynamic capture success rate\n")
        lines.append(
            "| Project | Smell | Attempts | Success | Fail: no_getters "
            "| compile | scope | act_line | no_markers | timeout | other "
            "| Avg elapsed (ms) |"
        )
        lines.append(
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )
        for c in sorted(tier4_cells, key=lambda x: (x.project, x.smell_id)):
            avg_el = (c.dynamic_capture_elapsed_ms // c.dynamic_capture_attempts
                      if c.dynamic_capture_attempts else 0)
            lines.append(
                f"| {c.project} | {c.smell_id} "
                f"| {c.dynamic_capture_attempts} | {c.dynamic_capture_success} "
                f"| {c.dynamic_fail_no_getters} | {c.dynamic_fail_compile} "
                f"| {c.dynamic_fail_scope} | {c.dynamic_fail_act_line} "
                f"| {c.dynamic_fail_no_markers} | {c.dynamic_fail_timeout} "
                f"| {c.dynamic_fail_other} | {avg_el} |"
            )

        # 4. Dynamic vs static-fallback mode comparison
        lines.append("\n## Tier 4 — dynamic vs static fallback\n")
        lines.append(
            "| Project | Smell | Dyn submitted | Dyn accepted | Dyn accept % "
            "| Static submitted | Static accepted | Static accept % "
            "| `assertNotNull` accepted (static-warning) |"
        )
        lines.append(
            "|---|---|---:|---:|---:|---:|---:|---:|---:|"
        )
        for c in sorted(tier4_cells, key=lambda x: (x.project, x.smell_id)):
            da = (c.mode_dynamic_accepted / c.mode_dynamic_submitted * 100.0
                  if c.mode_dynamic_submitted else 0.0)
            sa = (c.mode_static_accepted / c.mode_static_submitted * 100.0
                  if c.mode_static_submitted else 0.0)
            lines.append(
                f"| {c.project} | {c.smell_id} "
                f"| {c.mode_dynamic_submitted} | {c.mode_dynamic_accepted} "
                f"| {da:.1f}% "
                f"| {c.mode_static_submitted} | {c.mode_static_accepted} "
                f"| {sa:.1f}% | {c.assert_notnull_accepted} |"
            )

    # 5. Full 13-smell before/after table (per project)
    if any("smell_totals_before" in p for p in per_project):
        lines.append("\n## Smell totals (before → after) — all 13 smells\n")
        all_smells: Set[str] = set()
        for p in per_project:
            all_smells.update((p.get("smell_totals_before") or {}).keys())
            all_smells.update((p.get("smell_totals_after") or {}).keys())
        header_projects = [p.get("project") for p in per_project
                           if p.get("smell_totals_before") is not None]
        lines.append("| Smell | "
                     + " | ".join(f"{hp} before → after (Δ)" for hp in header_projects)
                     + " |")
        lines.append("|---|" + "|".join(["---" for _ in header_projects]) + "|")
        for s in sorted(all_smells):
            cells_str: List[str] = []
            for p in per_project:
                if p.get("smell_totals_before") is None:
                    continue
                b = p["smell_totals_before"].get(s, 0)
                a = p["smell_totals_after"].get(s, 0)
                cells_str.append(f"{b} → {a} ({a - b:+d})")
            lines.append(f"| {s} | " + " | ".join(cells_str) + " |")

    # 6. Coverage delta (JaCoCo)
    if any(p.get("jacoco_before") or p.get("jacoco_after") for p in per_project):
        lines.append("\n## Coverage delta (JaCoCo)\n")
        lines.append(
            "| Project | Line before | Line after | Δline | "
            "Branch before | Branch after | Δbranch | "
            "Inst before | Inst after | Δinst |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for p in per_project:
            jb = p.get("jacoco_before") or {}
            ja = p.get("jacoco_after") or {}
            if "error" in jb or "error" in ja:
                lines.append(f"| {p['project']} | (error) | | | | | | | | |")
                continue

            def _fmt(b, a):
                if b is None or a is None:
                    return "—", "—", "—"
                return f"{b:.4f}", f"{a:.4f}", f"{a - b:+.4f}"

            lb, la, dl = _fmt(jb.get("line_coverage"), ja.get("line_coverage"))
            bb, ba, db = _fmt(jb.get("branch_coverage"), ja.get("branch_coverage"))
            ib, ia, di = _fmt(jb.get("instruction_coverage"),
                              ja.get("instruction_coverage"))
            lines.append(
                f"| {p['project']} | {lb} | {la} | {dl} "
                f"| {bb} | {ba} | {db} | {ib} | {ia} | {di} |"
            )

    # 7. Gate 5 (coverage proxy) rejection breakdown by tier
    if cells:
        lines.append("\n## Gate-rejection breakdown by tier\n")
        lines.append(
            "| Tier | Banned | Syntax | Compile | Test | Coverage-proxy "
            "| Smell sub | Assert loss | Other |"
        )
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        agg: Dict[int, Dict[str, int]] = {}
        for c in cells.values():
            t = agg.setdefault(c.tier, {k: 0 for k in (
                "banned", "syntax", "compile", "test", "cov",
                "smell_sub", "assert_loss", "other",
            )})
            t["banned"] += c.gate_banned_reject
            t["syntax"] += c.gate_syntax_reject
            t["compile"] += c.gate_compile_reject
            t["test"] += c.gate_test_reject
            t["cov"] += c.gate_coverage_reject
            t["smell_sub"] += c.gate_smell_sub_reject
            t["assert_loss"] += c.gate_assert_loss_reject
            t["other"] += c.other_reject
        for tier in sorted(agg.keys()):
            t = agg[tier]
            lines.append(
                f"| {tier} | {t['banned']} | {t['syntax']} | {t['compile']} "
                f"| {t['test']} | {t['cov']} | {t['smell_sub']} "
                f"| {t['assert_loss']} | {t['other']} |"
            )

    # 8. LLM usage
    lines.append(
        f"\n## LLM usage\n\n"
        f"Requests: {usage.total_requests}, "
        f"cost ${usage.total_cost_usd:.4f}, "
        f"errors {usage.errors}, "
        f"avg latency {usage.avg_latency_ms():.0f}ms"
    )

    # 9. Naive-LLM baseline breakdown (only emitted when condition=naive_llm)
    if naive_cells:
        rows_csv = []
        for nc in naive_cells.values():
            rows_csv.append(dataclasses.asdict(nc))
        if rows_csv:
            path = run_dir / "summary_naive_per_project.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows_csv[0].keys()))
                w.writeheader()
                for r in rows_csv:
                    w.writerow(r)

        lines.append("\n## Naive-LLM baseline (RQ3)\n")
        lines.append(
            "| Project | Methods | LLM calls | Parse fail | Submitted "
            "| Accepted | Accept % | Compile fail | Test fail | Cov fail "
            "| Other | Cost $ |"
        )
        lines.append(
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )
        for nc in sorted(naive_cells.values(), key=lambda c: c.project):
            ar = (
                nc.plan_groups_accepted / nc.plan_groups_submitted * 100.0
                if nc.plan_groups_submitted else 0.0
            )
            lines.append(
                f"| {nc.project} | {nc.total_methods_targeted} "
                f"| {nc.llm_calls} | {nc.parse_failures} "
                f"| {nc.plan_groups_submitted} | {nc.plan_groups_accepted} "
                f"| {ar:.1f}% | {nc.gate_compile_reject} "
                f"| {nc.gate_test_reject} | {nc.gate_coverage_reject} "
                f"| {nc.other_reject} | {nc.total_cost_usd:.4f} |"
            )

    (run_dir / "pipeline_summary.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

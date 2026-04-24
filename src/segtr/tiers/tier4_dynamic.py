"""Tier 4 (dynamic-context) handler — NASE and TSVM.

Phase 2.4b.2 scope:

  NASE  — Not asserted side effect. A call in the Act step mutates state,
          but no assertion observes the effect. Tier 4's job is to add an
          INSERT_ASSERTION (or small sequence) that checks the post-state.

  TSVM  — Same as NASE but repeated across multiple tests for the same
          void method. The per-test action is identical to NASE; the
          pipeline handles the cross-test scope.

Two modes:

  dynamic           — a ``DynamicContextCollector`` capture succeeded, so
                       the prompt carries observed ``state_before`` /
                       ``state_after`` and the LLM is asked to emit
                       assertions using the OBSERVED values as literals.

  static_fallback   — capture failed (unsupported CUT shape, compile
                       error, etc.). The prompt still includes the
                       Smelly-E static evidence (modified_fields, act_call
                       info) and the smell guide; the LLM has to infer
                       assertions from structure alone. This is weaker
                       evidence — the handler records why capture failed
                       so we can triage later.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from smell_repair_v2.dynamic.collector import (
    DynamicContextCollector,
    DynamicEvidence,
)
from smell_repair_v2.llm.fewshot import get_examples
from smell_repair_v2.llm.plan_runner import AttemptRecord, PlanRunner, PlanRunResult
from smell_repair_v2.llm.prompts import (
    PlanPromptInputs,
    PlanPromptLimits,
    TIER_ALLOWED_OPERATORS,
)
from smell_repair_v2.operators.base import ExecutionContext, OperatorPlan


TIER4_SMELLS = frozenset({"NASE", "TSVM"})


_SMELLS_DIR = Path(__file__).resolve().parents[2] / "smells"


def is_tier4_smell(smell_id: str) -> bool:
    return smell_id in TIER4_SMELLS


def _load_smell_guide(smell_id: str) -> Optional[str]:
    p = _SMELLS_DIR / f"{smell_id}.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _format_cut_context(ctx: ExecutionContext) -> str:
    if ctx.cut_source:
        return ctx.cut_source
    if ctx.cut_public_methods:
        lines = [f"// CUT FQN: {ctx.cut_fqcn or 'UNKNOWN'}"]
        for m in ctx.cut_public_methods:
            params = m.get("params", "")
            rtype = m.get("return_type", "void")
            lines.append(f"public {rtype} {m.get('name')}({params});")
        return "\n".join(lines)
    return ""


# ---------------------------------------------------------------------------
# Tier4Result
# ---------------------------------------------------------------------------


@dataclass
class Tier4Result:
    """Wraps the LLM plan outcome with Tier 4 capture metadata."""

    plan_result: PlanRunResult
    mode: str                                # "dynamic" | "static_fallback" | "skipped"
    dynamic_evidence: Optional[Dict[str, Any]] = None
    capture_error: Optional[str] = None
    elapsed_capture_ms: int = 0
    # Diagnostic: the getter sources used (CUT vs parent). Useful when the
    # checkpoint needs to explain *why* an observed value was available.
    getter_sources: Dict[str, str] = field(default_factory=dict)

    # --- PlanRunResult delegation -----------------------------------------
    @property
    def success(self) -> bool:
        return self.plan_result.success

    @property
    def plans(self) -> List[OperatorPlan]:
        return self.plan_result.plans

    @property
    def attempts(self) -> int:
        return self.plan_result.attempts

    @property
    def error(self) -> Optional[str]:
        return self.plan_result.error


# ---------------------------------------------------------------------------
# Capture orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureRequest:
    """Inputs required to run ``DynamicContextCollector.collect()`` from within
    the Tier 4 handler. All fields come from Smelly-E evidence / disk
    discovery — the handler doesn't redo that work."""

    test_file: Path
    test_method_name: str
    act_call_info: Dict[str, Any]
    cut_source: Optional[str]
    cut_fqcn: Optional[str] = None


def _summarize_capture_failure(ev: DynamicEvidence) -> str:
    """Turn a failed DynamicEvidence into a one-line reason for logs/telemetry."""
    return ev.error or "capture_failed"


def _build_dynamic_evidence_block(ev: DynamicEvidence) -> Dict[str, Any]:
    """Reduce DynamicEvidence to the subset the LLM actually needs.

    We deliberately drop the raw stdout (noisy, up to ~8 kB) and keep only
    the observed key/value pairs plus the diff. The LLM's task is to
    generate assertions using these literals — more context would just
    burn tokens.
    """
    changed = {
        k: {"before": b, "after": a}
        for k, (b, a) in ev.changed_fields().items()
    }
    # Unchanged getters are a useful "do NOT add useless assertions" signal.
    unchanged = [
        k for k in ev.state_after
        if k in ev.state_before and ev.state_before[k] == ev.state_after[k]
    ]
    return {
        "state_before": dict(ev.state_before),
        "state_after": dict(ev.state_after),
        "changed_fields": changed,
        "unchanged_fields": unchanged,
        "act_call_line": ev.act_call_line,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def plan_tier4(
    smell_id: str,
    evidence: Dict[str, Any],
    method_text: str,
    ctx: ExecutionContext,
    runner: PlanRunner,
    *,
    capture_request: Optional[CaptureRequest] = None,
    dynamic_collector: Optional[DynamicContextCollector] = None,
    previous_feedback: Optional[str] = None,
    limits: Optional[PlanPromptLimits] = None,
) -> Tier4Result:
    """Run Tier 4 for one (smell, method) cell.

    Flow:
      1. Short-circuit if smell_id is not a Tier 4 smell.
      2. If both collector and capture_request are provided, attempt dynamic
         capture. On success → dynamic mode. On failure → static fallback
         (NOT an error — we still try to produce a plan).
      3. Build the prompt (with or without dynamic_evidence) and invoke
         runner.run().

    Returns a ``Tier4Result`` that carries both the LLM plan outcome and
    the capture diagnostics. Callers that only need plans can use
    ``result.plans`` / ``result.success`` directly.
    """
    if not is_tier4_smell(smell_id):
        return Tier4Result(
            plan_result=PlanRunResult(
                success=False,
                plans=[],
                attempts=0,
                final_raw_response="",
                error=f"{smell_id!r} is not in Tier 4 scope {sorted(TIER4_SMELLS)}",
                attempt_history=[],
            ),
            mode="skipped",
        )

    # -- 1. Attempt dynamic capture ---------------------------------------
    mode = "static_fallback"
    dyn_payload: Optional[Dict[str, Any]] = None
    capture_error: Optional[str] = None
    elapsed_ms = 0
    getter_sources: Dict[str, str] = {}

    if dynamic_collector is not None and capture_request is not None:
        t0 = time.monotonic()
        try:
            ev = dynamic_collector.collect(
                test_file=capture_request.test_file,
                test_method_name=capture_request.test_method_name,
                act_call_info=capture_request.act_call_info,
                cut_source=capture_request.cut_source,
                cut_fqcn=capture_request.cut_fqcn,
            )
            elapsed_ms = ev.elapsed_ms or int((time.monotonic() - t0) * 1000)
            getter_sources = dict(ev.getter_sources)
            if ev.capture_success:
                mode = "dynamic"
                dyn_payload = _build_dynamic_evidence_block(ev)
            else:
                capture_error = _summarize_capture_failure(ev)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            capture_error = f"{type(e).__name__}:{e}"
    elif dynamic_collector is None and capture_request is not None:
        capture_error = "collector_not_provided"
    elif dynamic_collector is not None and capture_request is None:
        capture_error = "capture_request_not_provided"
    else:
        capture_error = "dynamic_capture_not_configured"

    # -- 2. Build prompt + invoke runner ----------------------------------
    prompt = PlanPromptInputs(
        smell_id=smell_id,
        tier=4,
        evidence=evidence or {},
        test_method_code=method_text,
        cut_context=_format_cut_context(ctx),
        cut_fqcn=ctx.cut_fqcn,
        allowed_operators=list(TIER_ALLOWED_OPERATORS[4]),
        fewshot_examples=get_examples(4, smell_id),
        smell_guide=_load_smell_guide(smell_id),
        dynamic_evidence=dyn_payload,
        previous_attempt_feedback=previous_feedback,
        limits=limits or PlanPromptLimits(),
    )
    plan_result = runner.run(prompt)

    return Tier4Result(
        plan_result=plan_result,
        mode=mode,
        dynamic_evidence=dyn_payload,
        capture_error=capture_error,
        elapsed_capture_ms=elapsed_ms,
        getter_sources=getter_sources,
    )

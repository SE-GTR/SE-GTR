"""Tier 2 (template-guided) handler.

Tier 2 is entered when Tier 1's deterministic patterns don't apply. The
handler is a thin dispatcher: for each supported smell id it packages the
smell's evidence + CUT context + few-shot examples into a
``PlanPromptInputs`` and hands it to a ``PlanRunner`` that's already been
bound to a specific model (via ``MultiModelClient.client_for(model_key)``).

Design notes:

  - The runner-model binding is the caller's responsibility. This keeps the
    handler's surface matching Tier 1 (no multi-model awareness) and lets
    the dev-experiment script pick a ``model_key`` per cell.
  - We intentionally do NOT pre-filter which operator a given smell should
    use. The allowed-operator list (``TIER_ALLOWED_OPERATORS[2]``) plus
    the prompt's evidence section constrain the LLM; hard-coding a subset
    per smell would defeat the point of template-guided repair.
  - Tier 1 handlers for NNA / DS / TSES-simple / AC-simple have already run
    before this layer. Only the cases those couldn't handle reach Tier 2.

Per-smell repair intent (LLM reads these via the prompt header + examples):

  ENET (Exceptions due to null arguments)
    Primary operators: REPLACE_NULL_ARG, REMOVE_TRY_CATCH_KEEP_BODY,
                       ADD_TEST_EXPECTED
    Evidence: null_argument_sites, surrounding try/catch.
    LLM decides the replacement expression (type-appropriate).

  EDIS (Exceptions due to incomplete setup)
    Primary operators: ADD_SETUP_CALL, REMOVE_TRY_CATCH_KEEP_BODY,
                       INSERT_ASSERTION
    Evidence: trigger_call, unmodified_variable.
    LLM decides which setup call to insert.

  EDED (Exceptions due to external dependencies)
    Primary operators: REPLACE_EXPRESSION, REMOVE_STATEMENT,
                       ADD_TEST_EXPECTED
    Evidence: external_dependency_exceptions (matched_exception_type).
    LLM chooses the deterministic substitute or decides to accept the
    exception via @Test(expected=).

  TSES (complex) — Tier 1's void-only filter deferred these.
    Primary operators: CAPTURE_RETURN_VALUE + INSERT_ASSERTION +
                       REMOVE_TRY_CATCH_KEEP_BODY, or TRY_CATCH_TO_EXPECTED
                       if the return is truly unused.

  AC (complex) — CUT-related constant assertions Tier 1 can't safely drop.
    Primary operator: REPLACE_ASSERTION with a meaningful CUT assertion.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from smell_repair_v2.llm.fewshot import get_examples
from smell_repair_v2.llm.plan_runner import PlanRunner, PlanRunResult
from smell_repair_v2.llm.prompts import (
    PlanPromptInputs,
    PlanPromptLimits,
    TIER_ALLOWED_OPERATORS,
)
from smell_repair_v2.operators.base import ExecutionContext


TIER2_SMELLS = frozenset({"ENET", "EDIS", "EDED", "TSES", "AC"})


def is_tier2_smell(smell_id: str) -> bool:
    return smell_id in TIER2_SMELLS


def _format_cut_context(ctx: ExecutionContext) -> str:
    """Prefer the full CUT source when available; fall back to the signature
    list if we only have it in structured form; else empty string."""
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


def plan_tier2(
    smell_id: str,
    evidence: Dict[str, Any],
    method_text: str,
    ctx: ExecutionContext,
    runner: PlanRunner,
    *,
    previous_feedback: Optional[str] = None,
    limits: Optional[PlanPromptLimits] = None,
) -> PlanRunResult:
    """Dispatch the given Tier 2 smell to the bound ``runner``.

    The ``runner`` must already be constructed with a model-bound chat
    client; see ``MultiModelClient.client_for(model_key)``.

    Returns a ``PlanRunResult`` whose ``success``/``error`` fields the
    caller uses directly. A non-Tier-2 smell short-circuits with
    ``success=False`` and an explanatory ``error``.
    """
    if not is_tier2_smell(smell_id):
        return PlanRunResult(
            success=False,
            plans=[],
            attempts=0,
            final_raw_response="",
            error=f"{smell_id!r} is not in Tier 2 scope {sorted(TIER2_SMELLS)}",
            attempt_history=[],
        )

    prompt = PlanPromptInputs(
        smell_id=smell_id,
        tier=2,
        evidence=evidence or {},
        test_method_code=method_text,
        cut_context=_format_cut_context(ctx),
        cut_fqcn=ctx.cut_fqcn,
        allowed_operators=list(TIER_ALLOWED_OPERATORS[2]),
        fewshot_examples=get_examples(2, smell_id),
        dynamic_evidence=None,
        previous_attempt_feedback=previous_feedback,
        limits=limits or PlanPromptLimits(),
    )
    return runner.run(prompt)

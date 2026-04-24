"""Tier 3 (evidence-guided) handler.

Tier 3 picks up smells that require semantic judgment — the operator set is
larger than Tier 2 (14 vs 9) and the LLM is expected to read both Smelly-E
evidence and a long-form smell guide (``smells/<ID>.md``) before choosing a
plan.

Dispatcher surface mirrors ``tier2_template.plan_tier2`` so the dev
experiment script can swap handlers without structural changes.

Per-smell repair intent (rendered in prompt header):

  NARV  Non-void return not asserted → CAPTURE_RETURN_VALUE + INSERT_ASSERTION
  OIMT  Redundant init assertions → REMOVE + INSERT behaviour assertion
  TOFA  Only accessors exercised → add logic call, or return `[]` if CUT is
        a pure data holder
  ARPM  Inherited-method assertion unrelated to Act step → REPLACE with
        CUT-driven assertion
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from smell_repair_v2.llm.fewshot import get_examples
from smell_repair_v2.llm.plan_runner import PlanRunner, PlanRunResult
from smell_repair_v2.llm.prompts import (
    PlanPromptInputs,
    PlanPromptLimits,
    TIER_ALLOWED_OPERATORS,
)
from smell_repair_v2.operators.base import ExecutionContext


TIER3_SMELLS = frozenset({"NARV", "OIMT", "TOFA", "ARPM"})


# Pre-compiled path to smells/ at repo root. The guide files are identical to
# the v1 copy; keeping one canonical copy avoids divergence.
_SMELLS_DIR = Path(__file__).resolve().parents[2] / "smells"


def is_tier3_smell(smell_id: str) -> bool:
    return smell_id in TIER3_SMELLS


def _load_smell_guide(smell_id: str) -> Optional[str]:
    """Return the smell's long-form guide markdown, or None if missing."""
    p = _SMELLS_DIR / f"{smell_id}.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _format_cut_context(ctx: ExecutionContext) -> str:
    """Same contract as ``tier2_template._format_cut_context``."""
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


def plan_tier3(
    smell_id: str,
    evidence: Dict[str, Any],
    method_text: str,
    ctx: ExecutionContext,
    runner: PlanRunner,
    *,
    previous_feedback: Optional[str] = None,
    limits: Optional[PlanPromptLimits] = None,
) -> PlanRunResult:
    """Dispatch a Tier 3 smell to ``runner`` (which must already be bound to
    a specific model via ``MultiModelClient.client_for``). Non-Tier-3 smells
    short-circuit with an explanatory error and no LLM call.
    """
    if not is_tier3_smell(smell_id):
        return PlanRunResult(
            success=False,
            plans=[],
            attempts=0,
            final_raw_response="",
            error=f"{smell_id!r} is not in Tier 3 scope {sorted(TIER3_SMELLS)}",
            attempt_history=[],
        )

    prompt = PlanPromptInputs(
        smell_id=smell_id,
        tier=3,
        evidence=evidence or {},
        test_method_code=method_text,
        cut_context=_format_cut_context(ctx),
        cut_fqcn=ctx.cut_fqcn,
        allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
        fewshot_examples=get_examples(3, smell_id),
        smell_guide=_load_smell_guide(smell_id),
        dynamic_evidence=None,
        previous_attempt_feedback=previous_feedback,
        limits=limits or PlanPromptLimits(max_smell_guides_chars=8000),
    )
    return runner.run(prompt)

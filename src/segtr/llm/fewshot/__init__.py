"""Tier-indexed few-shot example collections for plan-mode LLM prompting.

Each example is a dict with keys:
  description       — human-readable label shown in the prompt header
  evidence_summary  — short evidence snippet (one-line preferred)
  test_method       — the target method with line-number prefixes
                      (same format the runtime prompt uses)
  cut_context       — optional CUT signature / snippet
  dynamic_evidence  — optional (Tier 4 only); observed runtime state
  expected_plan     — list[{op, params}] — MUST satisfy the operator
                      catalog's preconditions when applied to `test_method`
  notes             — short rationale (optional)

``test_plan_fewshot_valid`` validates every example's expected_plan by
running it through the real OperatorExecutor.
"""
from .tier2_examples import TIER2_EXAMPLES
from .tier3_examples import TIER3_EXAMPLES
from .tier4_examples import TIER4_EXAMPLES


def get_examples(tier: int, smell_id: str):
    store = {2: TIER2_EXAMPLES, 3: TIER3_EXAMPLES, 4: TIER4_EXAMPLES}.get(tier, {})
    return list(store.get(smell_id, []))


__all__ = ["TIER2_EXAMPLES", "TIER3_EXAMPLES", "TIER4_EXAMPLES", "get_examples"]

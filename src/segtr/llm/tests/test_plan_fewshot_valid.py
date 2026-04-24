"""Validate every few-shot example by applying its `expected_plan` to
`test_method` with the real OperatorExecutor. This is the "schema mismatch
between fewshot and catalog" guard that the task brief called for — if an
example's plan is incompatible with the current operator catalog, this test
fails immediately instead of confusing the LLM at runtime.

We also round-trip the expected_plan through plan_parser to make sure the
JSON shape we ship as examples is also a shape we accept on inbound.
"""
from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List

from smell_repair_v2.llm.fewshot import TIER2_EXAMPLES, TIER3_EXAMPLES, TIER4_EXAMPLES
from smell_repair_v2.llm.plan_parser import parse_plan_response
from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.operators.executor import OperatorExecutor


ALL_OPS = [op.value for op in OperatorId]


def _make_ctx(method_text: str, cut_source: str | None = None) -> ExecutionContext:
    return ExecutionContext(
        method_name="__example__",
        method_line_range=(1, len(method_text.splitlines())),
        file_text=method_text,
        cut_source=cut_source,
    )


def _validate_example(
    test: unittest.TestCase,
    *,
    tier: int,
    smell_id: str,
    example_index: int,
    example: Dict[str, Any],
) -> None:
    label = f"tier={tier} smell={smell_id} example={example_index} desc={example.get('description','')!r}"

    # 1. plan_parser must accept the expected_plan JSON shape.
    plan_json = json.dumps(example["expected_plan"])
    try:
        plans = parse_plan_response(plan_json, smell_id=smell_id, allowed_operators=ALL_OPS)
    except Exception as e:
        test.fail(f"{label}: expected_plan failed plan_parser: {e}")

    # 2. executor must apply every step successfully against `test_method`.
    method_text = example["test_method"]
    ctx = _make_ctx(method_text, cut_source=example.get("cut_context"))
    executor = OperatorExecutor()
    outcome = executor.execute_plan(method_text, plans, ctx)

    for step_idx, (plan, result) in enumerate(zip(plans, outcome.results)):
        test.assertTrue(
            result.success,
            f"{label}: step {step_idx} ({plan.op.value}) rejected: {result.rejection_reason}",
        )


class TestTier2FewshotPlansApply(unittest.TestCase):
    pass


class TestTier3FewshotPlansApply(unittest.TestCase):
    pass


class TestTier4FewshotPlansApply(unittest.TestCase):
    pass


def _generate_cases(cls, tier: int, store: Dict[str, List[Dict[str, Any]]]):
    for smell_id, examples in store.items():
        for idx, ex in enumerate(examples):
            def make(sid=smell_id, i=idx, example=ex):
                def _test(self):
                    _validate_example(self, tier=tier, smell_id=sid, example_index=i, example=example)
                _test.__doc__ = f"{sid}[{i}]: {example.get('description','')}"
                return _test

            name = f"test_{smell_id.lower()}_{idx}"
            setattr(cls, name, make())


_generate_cases(TestTier2FewshotPlansApply, 2, TIER2_EXAMPLES)
_generate_cases(TestTier3FewshotPlansApply, 3, TIER3_EXAMPLES)
_generate_cases(TestTier4FewshotPlansApply, 4, TIER4_EXAMPLES)


if __name__ == "__main__":
    unittest.main()

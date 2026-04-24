"""End-to-end mock simulation: prompt build → fake LLM → parse → execute.

This is the Phase 2.1 smoke test from the task brief. No real LLM; instead
a MockClient returns a fixed JSON array. We verify the entire pipeline
(build_plan_messages → PlanRunner → OperatorExecutor) composes cleanly and
produces a transformed method.
"""
from __future__ import annotations

import json
import textwrap
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List

from smell_repair_v2.llm.fewshot import TIER3_EXAMPLES
from smell_repair_v2.llm.plan_runner import PlanRunner
from smell_repair_v2.llm.prompts import (
    PlanPromptInputs,
    PlanPromptLimits,
    TIER_ALLOWED_OPERATORS,
)
from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.operators.executor import OperatorExecutor


@dataclass
class MockClient:
    responses: List[str]
    calls: List[List[Dict[str, str]]] = field(default_factory=list)
    idx: int = 0

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str:
        self.calls.append(messages)
        r = self.responses[self.idx]
        self.idx += 1
        return r


TEST_METHOD = textwrap.dedent("""\
    @Test
    public void test() {
        List<String> list = new ArrayList<String>();
        list.add("hello");
        list.add("world");
        assertEquals(2, list.size());
        list.contains("hello");
    }""")


# Planner output: capture return then assert on it — matches Tier 3 NARV fewshot.
PLANNER_OUTPUT = json.dumps([
    {
        "op": "CAPTURE_RETURN_VALUE",
        "params": {"target_line": 7, "var_name": "containsHello", "var_type": "boolean"},
    },
    {
        "op": "INSERT_ASSERTION",
        "params": {
            "after_line": 7,
            "assert_type": "assertTrue",
            "actual_expr": "containsHello",
        },
    },
])


class TestE2EMockPipeline(unittest.TestCase):
    def test_full_prompt_parse_execute_pipeline(self):
        client = MockClient(responses=[PLANNER_OUTPUT])
        runner = PlanRunner(client)

        inp = PlanPromptInputs(
            smell_id="NARV",
            tier=3,
            evidence={
                "unasserted_return_calls": [
                    {"expr": "list.contains(\"hello\")",
                     "return_type": "boolean", "begin_line": 7}
                ]
            },
            test_method_code=TEST_METHOD,
            cut_context="public class ArrayList<E> { public boolean contains(Object o); }",
            cut_fqcn="java.util.ArrayList",
            allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
            fewshot_examples=TIER3_EXAMPLES["NARV"],
        )

        # 1. PlanRunner produces plans
        result = runner.run(inp)
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.plans), 2)
        self.assertEqual(result.plans[0].op, OperatorId.CAPTURE_RETURN_VALUE)
        self.assertEqual(result.plans[1].op, OperatorId.INSERT_ASSERTION)

        # 2. OperatorExecutor applies them
        ctx = ExecutionContext(
            method_name="test",
            method_line_range=(1, len(TEST_METHOD.splitlines())),
            file_text=TEST_METHOD,
            cut_source=inp.cut_context,
        )
        exec_outcome = OperatorExecutor().execute_plan(
            TEST_METHOD, result.plans, ctx
        )

        for i, r in enumerate(exec_outcome.results):
            self.assertTrue(r.success, f"step {i}: {r.rejection_reason}")

        final = exec_outcome.final_text
        self.assertIn("boolean containsHello = list.contains(\"hello\");", final)
        self.assertIn("assertTrue(containsHello);", final)
        self.assertEqual(exec_outcome.used_asserts, {"assertTrue"})

    def test_retry_recovers_then_executes(self):
        """LLM first returns malformed JSON; retry returns valid; executor runs."""
        bad = "here is my plan:\n- capture the return\n- assert it"
        client = MockClient(responses=[bad, PLANNER_OUTPUT])
        runner = PlanRunner(client)
        inp = PlanPromptInputs(
            smell_id="NARV",
            tier=3,
            evidence={},
            test_method_code=TEST_METHOD,
            cut_context="",
            allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
            fewshot_examples=[],
        )
        result = runner.run(inp)
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

        ctx = ExecutionContext(
            method_name="test",
            method_line_range=(1, len(TEST_METHOD.splitlines())),
            file_text=TEST_METHOD,
        )
        outcome = OperatorExecutor().execute_plan(TEST_METHOD, result.plans, ctx)
        self.assertTrue(all(r.success for r in outcome.results))

    def test_fewshot_examples_appear_in_prompt(self):
        client = MockClient(responses=[PLANNER_OUTPUT])
        runner = PlanRunner(client)
        inp = PlanPromptInputs(
            smell_id="NARV",
            tier=3,
            evidence={},
            test_method_code=TEST_METHOD,
            cut_context="",
            allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
            fewshot_examples=TIER3_EXAMPLES["NARV"],
        )
        runner.run(inp)
        user = client.calls[0][1]["content"]
        self.assertIn("## Examples", user)
        # An example's expected_plan content must be visible to the LLM
        self.assertIn("containsHello", user)

    def test_empty_plan_is_success(self):
        """LLM can return [] to signal 'no safe fix'."""
        client = MockClient(responses=["[]"])
        runner = PlanRunner(client)
        inp = PlanPromptInputs(
            smell_id="NARV",
            tier=3,
            evidence={},
            test_method_code=TEST_METHOD,
            cut_context="",
            allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
            fewshot_examples=[],
        )
        result = runner.run(inp)
        self.assertTrue(result.success)
        self.assertEqual(result.plans, [])

        # Executor with empty plan list leaves text untouched
        ctx = ExecutionContext(
            method_name="test",
            method_line_range=(1, len(TEST_METHOD.splitlines())),
            file_text=TEST_METHOD,
        )
        outcome = OperatorExecutor().execute_plan(TEST_METHOD, result.plans, ctx)
        self.assertEqual(outcome.final_text, TEST_METHOD)
        self.assertEqual(outcome.results, [])


if __name__ == "__main__":
    unittest.main()

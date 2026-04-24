"""Unit tests for the Tier 3 dispatcher (``plan_tier3``)."""
from __future__ import annotations

import json
import textwrap
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List

from smell_repair_v2.llm.plan_runner import PlanRunner
from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.tiers.tier3_evidence import (
    TIER3_SMELLS,
    is_tier3_smell,
    plan_tier3,
)


@dataclass
class _Mock:
    responses: List[str]
    calls: List[List[Dict[str, str]]] = field(default_factory=list)
    idx: int = 0

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str:
        self.calls.append(messages)
        r = self.responses[self.idx]
        self.idx += 1
        return r


def _ctx(method_text: str, cut_src: str | None = None, cut_fqcn: str | None = None) -> ExecutionContext:
    return ExecutionContext(
        method_name="__test__",
        method_line_range=(1, len(method_text.splitlines())),
        file_text=method_text,
        cut_source=cut_src,
        cut_fqcn=cut_fqcn,
    )


NARV_METHOD = textwrap.dedent("""\
    @Test
    public void test() {
        ArrayList<String> list = new ArrayList<String>();
        list.add("hello");
        list.contains("hello");
    }""")


class TestTier3Scope(unittest.TestCase):
    def test_scope_membership(self):
        self.assertEqual(TIER3_SMELLS, frozenset({"NARV", "OIMT", "TOFA", "ARPM"}))

    def test_is_tier3_smell_helper(self):
        self.assertTrue(is_tier3_smell("NARV"))
        self.assertTrue(is_tier3_smell("ARPM"))
        self.assertFalse(is_tier3_smell("ENET"))   # Tier 2 smell
        self.assertFalse(is_tier3_smell("NNA"))    # Tier 1 smell

    def test_non_tier3_short_circuits(self):
        runner = PlanRunner(_Mock(responses=[]))
        result = plan_tier3(
            "ENET", {}, NARV_METHOD, _ctx(NARV_METHOD), runner=runner,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 0)
        self.assertIn("not in Tier 3 scope", result.error or "")


class TestTier3Dispatch(unittest.TestCase):
    def test_narv_happy_path(self):
        mock_response = json.dumps([
            {"op": "CAPTURE_RETURN_VALUE",
             "params": {"target_line": 5, "var_name": "r", "var_type": "boolean"}},
            {"op": "INSERT_ASSERTION",
             "params": {"after_line": 5, "assert_type": "assertTrue", "actual_expr": "r"}},
        ])
        client = _Mock(responses=[mock_response])
        runner = PlanRunner(client)
        result = plan_tier3(
            smell_id="NARV",
            evidence={"unasserted_return_calls": [
                {"expr": "list.contains(\"hello\")", "return_type": "boolean", "begin_line": 5}
            ]},
            method_text=NARV_METHOD,
            ctx=_ctx(NARV_METHOD, cut_src="class ArrayList { public boolean contains(Object o); }"),
            runner=runner,
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.plans), 2)
        self.assertEqual(result.plans[0].op, OperatorId.CAPTURE_RETURN_VALUE)
        self.assertEqual(result.plans[1].op, OperatorId.INSERT_ASSERTION)

    def test_empty_plan_is_success(self):
        """TOFA can legitimately return [] when CUT is a pure data holder."""
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        result = plan_tier3(
            smell_id="TOFA", evidence={}, method_text=NARV_METHOD,
            ctx=_ctx(NARV_METHOD), runner=runner,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.plans, [])

    def test_prompt_has_tier3_header_and_allowed_ops(self):
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        plan_tier3(
            smell_id="OIMT",
            evidence={"rules_triggered": ["init_value_repeated"]},
            method_text=NARV_METHOD,
            ctx=_ctx(NARV_METHOD, cut_fqcn="com.ex.Counter"),
            runner=runner,
        )
        user = client.calls[0][1]["content"]
        self.assertIn("tier=3", user)
        self.assertIn("Tier 3 — evidence-guided", user)
        # Tier 3 allowed ops include INSERT_STATEMENT (Tier 2 does NOT)
        self.assertIn("INSERT_STATEMENT", user)
        self.assertIn("CAPTURE_RETURN_VALUE", user)
        self.assertIn("REPLACE_ASSERTION", user)

    def test_smell_guide_rendered_in_prompt(self):
        """Tier 3 adds the long-form smell guide section; verify it appears."""
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        plan_tier3(
            smell_id="NARV", evidence={}, method_text=NARV_METHOD,
            ctx=_ctx(NARV_METHOD), runner=runner,
        )
        user = client.calls[0][1]["content"]
        self.assertIn("## Smell guide", user)
        # NARV.md's header phrase
        self.assertIn("Not Asserted Return Value", user)

    def test_fewshot_examples_rendered(self):
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        plan_tier3(
            smell_id="NARV", evidence={}, method_text=NARV_METHOD,
            ctx=_ctx(NARV_METHOD), runner=runner,
        )
        user = client.calls[0][1]["content"]
        self.assertIn("## Examples", user)
        self.assertIn("CAPTURE_RETURN_VALUE", user)

    def test_previous_feedback_propagated(self):
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        plan_tier3(
            smell_id="NARV", evidence={}, method_text=NARV_METHOD,
            ctx=_ctx(NARV_METHOD), runner=runner,
            previous_feedback="Earlier attempt used assertNotNull — use the real return value instead.",
        )
        user = client.calls[0][1]["content"]
        self.assertIn("Previous attempt failed", user)
        self.assertIn("assertNotNull", user)


class TestSmellGuideLoading(unittest.TestCase):
    def test_guide_loads_for_known_smell(self):
        from smell_repair_v2.tiers.tier3_evidence import _load_smell_guide
        for sid in ("NARV", "OIMT", "TOFA", "ARPM"):
            g = _load_smell_guide(sid)
            self.assertIsNotNone(g, f"missing guide for {sid}")
            self.assertIn(sid, g)

    def test_guide_missing_returns_none(self):
        from smell_repair_v2.tiers.tier3_evidence import _load_smell_guide
        self.assertIsNone(_load_smell_guide("NONEXISTENT"))


if __name__ == "__main__":
    unittest.main()

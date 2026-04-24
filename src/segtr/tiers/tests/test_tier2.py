"""Tests for the Tier 2 dispatcher. Uses a fake ``ChatClient`` that replays
a fixed response so the whole prompt → parse → plan pipeline can be
exercised end-to-end without touching a real LLM endpoint."""
from __future__ import annotations

import json
import textwrap
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List

from smell_repair_v2.llm.plan_runner import PlanRunner
from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.tiers.tier2_template import (
    TIER2_SMELLS,
    is_tier2_smell,
    plan_tier2,
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


ENET_METHOD = textwrap.dedent("""\
    @Test
    public void test05() throws Throwable {
        Parser parser = new Parser();
        try {
            parser.parse(null);
            fail("Expecting NullPointerException");
        } catch (NullPointerException e) {
            verifyException("Parser", e);
        }
    }""")


class TestTier2SmellScope(unittest.TestCase):
    def test_scope_membership(self):
        self.assertEqual(
            TIER2_SMELLS, frozenset({"ENET", "EDIS", "EDED", "TSES", "AC"})
        )

    def test_is_tier2_smell_helper(self):
        self.assertTrue(is_tier2_smell("ENET"))
        self.assertTrue(is_tier2_smell("EDED"))
        self.assertFalse(is_tier2_smell("NNA"))
        self.assertFalse(is_tier2_smell("UNKNOWN"))

    def test_non_tier2_short_circuits(self):
        runner = PlanRunner(_Mock(responses=[]))
        result = plan_tier2(
            smell_id="NNA",
            evidence={},
            method_text=ENET_METHOD,
            ctx=_ctx(ENET_METHOD),
            runner=runner,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.plans, [])
        self.assertEqual(result.attempts, 0)
        self.assertIn("not in Tier 2 scope", result.error or "")


class TestTier2Dispatch(unittest.TestCase):
    def test_enet_happy_path(self):
        mock_response = json.dumps([
            {
                "op": "REPLACE_NULL_ARG",
                "params": {
                    "target_line": 5, "call_expr": "parser.parse",
                    "arg_index": 0, "new_value": "\"\"",
                },
            },
            {
                "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                "params": {"try_begin_line": 4, "drop_fail_call": True},
            },
        ])
        client = _Mock(responses=[mock_response])
        runner = PlanRunner(client)

        result = plan_tier2(
            smell_id="ENET",
            evidence={"null_argument_sites": [
                {"arg_index": 0, "arg_expr": "null", "kind": "method_call"}
            ]},
            method_text=ENET_METHOD,
            ctx=_ctx(ENET_METHOD, cut_src="public class Parser { public Document parse(String html); }",
                    cut_fqcn="com.ex.Parser"),
            runner=runner,
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.plans), 2)
        self.assertEqual(result.plans[0].op, OperatorId.REPLACE_NULL_ARG)
        self.assertEqual(result.plans[1].op, OperatorId.REMOVE_TRY_CATCH_KEEP_BODY)
        self.assertEqual(result.plans[0].smell_id, "ENET")

    def test_prompt_has_tier2_header_and_allowed_ops(self):
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        plan_tier2(
            smell_id="EDIS",
            evidence={"trigger_call": "cfg.apply()"},
            method_text=ENET_METHOD,
            ctx=_ctx(ENET_METHOD, cut_fqcn="com.ex.Config"),
            runner=runner,
        )
        user = client.calls[0][1]["content"]
        # Tier-2 header
        self.assertIn("tier=2", user)
        self.assertIn("Tier 2 — template-guided repair", user)
        # At least one Tier 2 operator schema rendered
        self.assertIn("REPLACE_NULL_ARG", user)
        self.assertIn("ADD_SETUP_CALL", user)
        # INSERT_STATEMENT is Tier 3 only — must NOT appear in the allowed set
        # (the allowed-operators section comes before any fewshot block, so
        # limit the search to the header region).
        allowed_section = user.split("## Target test method", 1)[0]
        self.assertNotIn("INSERT_STATEMENT(", allowed_section)

    def test_empty_plan_is_success(self):
        """LLM can reject via []; handler surfaces as success with empty plans."""
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        result = plan_tier2(
            smell_id="EDED",
            evidence={},
            method_text=ENET_METHOD,
            ctx=_ctx(ENET_METHOD),
            runner=runner,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.plans, [])

    def test_previous_feedback_propagated(self):
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        plan_tier2(
            smell_id="ENET",
            evidence={},
            method_text=ENET_METHOD,
            ctx=_ctx(ENET_METHOD),
            runner=runner,
            previous_feedback="Earlier attempt returned operator INSERT_STATEMENT — it is not in the Tier 2 allowed list.",
        )
        user = client.calls[0][1]["content"]
        self.assertIn("Previous attempt failed", user)
        self.assertIn("INSERT_STATEMENT", user)

    def test_fewshot_examples_rendered(self):
        client = _Mock(responses=["[]"])
        runner = PlanRunner(client)
        plan_tier2(
            smell_id="ENET",
            evidence={},
            method_text=ENET_METHOD,
            ctx=_ctx(ENET_METHOD),
            runner=runner,
        )
        user = client.calls[0][1]["content"]
        self.assertIn("## Examples", user)
        # At least one ENET example's characteristic content appears
        self.assertIn("REPLACE_NULL_ARG", user)


if __name__ == "__main__":
    unittest.main()

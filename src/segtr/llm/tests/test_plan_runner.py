from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List

from smell_repair_v2.llm.plan_runner import PlanRunner, PlanRunResult
from smell_repair_v2.llm.prompts import (
    PlanPromptInputs,
    PlanPromptLimits,
    TIER_ALLOWED_OPERATORS,
)
from smell_repair_v2.operators.base import OperatorId


VALID_RESPONSE = (
    '[{"op": "INSERT_ASSERTION", "params": {'
    '"after_line": 5, "assert_type": "assertTrue", "actual_expr": "x > 0"}}]'
)
INVALID_JSON_RESPONSE = "sorry I cannot help"
BAD_SCHEMA_RESPONSE = (
    '[{"op": "INSERT_ASSERTION", "params": {"after_line": 5}}]'  # missing assert_type
)


@dataclass
class MockClient:
    """Replays a fixed sequence of responses and records call history."""
    responses: List[Any]
    calls: List[List[Dict[str, str]]] = field(default_factory=list)
    idx: int = 0

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str:
        self.calls.append(messages)
        if self.idx >= len(self.responses):
            raise RuntimeError("mock exhausted")
        r = self.responses[self.idx]
        self.idx += 1
        if isinstance(r, Exception):
            raise r
        return r


def _make_inputs(**overrides) -> PlanPromptInputs:
    base = dict(
        smell_id="NARV",
        tier=3,
        evidence={"unasserted_return_calls": [{"expr": "x.f()", "begin_line": 5}]},
        test_method_code="@Test\npublic void t() {\n    int x = 0;\n    assertEquals(0, x);\n}",
        cut_context="public class C {}",
        cut_fqcn="C",
        allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
    )
    base.update(overrides)
    return PlanPromptInputs(**base)


class TestFirstAttemptSuccess(unittest.TestCase):
    def test_parses_plan_on_first_call(self):
        client = MockClient(responses=[VALID_RESPONSE])
        runner = PlanRunner(client)
        result = runner.run(_make_inputs())
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(result.plans), 1)
        self.assertEqual(result.plans[0].op, OperatorId.INSERT_ASSERTION)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(result.attempt_history), 1)
        self.assertEqual(result.attempt_history[0].outcome, "parsed")

    def test_empty_array_counts_as_success(self):
        client = MockClient(responses=["[]"])
        result = PlanRunner(client).run(_make_inputs())
        self.assertTrue(result.success)
        self.assertEqual(result.plans, [])


class TestRetryOnParseError(unittest.TestCase):
    def test_retries_invalid_json_then_succeeds(self):
        client = MockClient(responses=[INVALID_JSON_RESPONSE, VALID_RESPONSE])
        result = PlanRunner(client).run(_make_inputs())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(client.calls), 2)
        # history: first parse_error, second parsed
        self.assertEqual([r.outcome for r in result.attempt_history],
                         ["parse_error", "parsed"])

    def test_retries_schema_error_then_succeeds(self):
        client = MockClient(responses=[BAD_SCHEMA_RESPONSE, VALID_RESPONSE])
        result = PlanRunner(client).run(_make_inputs())
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)

    def test_max_retries_all_fail(self):
        client = MockClient(responses=[INVALID_JSON_RESPONSE] * 3)
        result = PlanRunner(client, max_attempts=3).run(_make_inputs())
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(all(r.outcome == "parse_error" for r in result.attempt_history))

    def test_single_attempt_config(self):
        """max_attempts=1 means no retry at all."""
        client = MockClient(responses=[INVALID_JSON_RESPONSE, VALID_RESPONSE])
        result = PlanRunner(client, max_attempts=1).run(_make_inputs())
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(client.calls), 1)


class TestFeedbackInjection(unittest.TestCase):
    def test_retry_prompt_contains_previous_error(self):
        client = MockClient(responses=[INVALID_JSON_RESPONSE, VALID_RESPONSE])
        result = PlanRunner(client).run(_make_inputs())
        self.assertTrue(result.success)
        # 1st call: no feedback section in user message
        first_user = client.calls[0][1]["content"]
        self.assertNotIn("Previous attempt failed", first_user)
        # 2nd call: feedback section appears with parser's reason
        second_user = client.calls[1][1]["content"]
        self.assertIn("Previous attempt failed", second_user)
        self.assertIn("JSON parse failed", second_user)

    def test_retry_feedback_includes_schema_reason(self):
        client = MockClient(responses=[BAD_SCHEMA_RESPONSE, VALID_RESPONSE])
        PlanRunner(client).run(_make_inputs())
        self.assertIn("assert_type", client.calls[1][1]["content"])


class TestLlmError(unittest.TestCase):
    def test_llm_exception_is_fatal_no_retry(self):
        client = MockClient(responses=[RuntimeError("network down"), VALID_RESPONSE])
        result = PlanRunner(client).run(_make_inputs())
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("LLM call failed", result.error or "")


class TestLogger(unittest.TestCase):
    def test_logger_receives_per_attempt_events(self):
        events: List[Dict[str, Any]] = []
        client = MockClient(responses=[INVALID_JSON_RESPONSE, VALID_RESPONSE])
        runner = PlanRunner(client, logger=events.append)
        runner.run(_make_inputs())
        # At minimum: 2 llm_request + 1 parse_error + 1 parsed = 4 events
        event_names = [e["event"] for e in events]
        self.assertIn("llm_request", event_names)
        self.assertIn("parse_error", event_names)
        self.assertIn("parsed", event_names)

    def test_logger_exception_does_not_break_runner(self):
        def bad_logger(_: Dict[str, Any]) -> None:
            raise ValueError("logger crashed")

        client = MockClient(responses=[VALID_RESPONSE])
        runner = PlanRunner(client, logger=bad_logger)
        result = runner.run(_make_inputs())
        self.assertTrue(result.success)


class TestAttemptHistoryShape(unittest.TestCase):
    def test_history_captures_raw_preview_and_sizes(self):
        client = MockClient(responses=[VALID_RESPONSE])
        result = PlanRunner(client).run(_make_inputs())
        rec = result.attempt_history[0]
        self.assertEqual(rec.outcome, "parsed")
        self.assertEqual(rec.num_plans, 1)
        self.assertIn("sys_chars", rec.messages_preview or {})
        self.assertIn("user_chars", rec.messages_preview or {})
        self.assertTrue(rec.raw_preview.startswith("[{"))


class TestAllowedOperatorsResolution(unittest.TestCase):
    def test_falls_back_to_tier_allowed(self):
        """Empty allowed_operators should default to TIER_ALLOWED_OPERATORS[tier]."""
        client = MockClient(responses=[VALID_RESPONSE])
        inp = _make_inputs(allowed_operators=[])
        result = PlanRunner(client).run(inp)
        self.assertTrue(result.success)

    def test_restrictive_allowed_rejects_valid_op(self):
        """If the caller passes a very small allowed list, parse fails."""
        client = MockClient(responses=[VALID_RESPONSE] * 3)
        inp = _make_inputs(allowed_operators=["REMOVE_ASSERTION"])
        result = PlanRunner(client).run(inp)
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 3)


if __name__ == "__main__":
    unittest.main()

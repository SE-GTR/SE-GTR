"""Unit tests for the Tier 4 dispatcher (``plan_tier4``).

These tests exercise the dispatcher and prompt routing with mock LLM and
mock DynamicContextCollector — no Java toolchain invoked. End-to-end
capture-plus-LLM validation lives in
``scripts/tier4_checkpoint.py`` (requires real OpenRouter + ant + JUnit).
"""
from __future__ import annotations

import json
import textwrap
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from smell_repair_v2.dynamic.collector import DynamicEvidence
from smell_repair_v2.llm.plan_runner import PlanRunner
from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.tiers.tier4_dynamic import (
    CaptureRequest,
    TIER4_SMELLS,
    Tier4Result,
    is_tier4_smell,
    plan_tier4,
)


@dataclass
class _MockLLM:
    """Minimal chat client that replays a fixed response list and records
    every request so tests can assert on prompt contents."""

    responses: List[str]
    calls: List[List[Dict[str, str]]] = field(default_factory=list)
    idx: int = 0

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str:
        self.calls.append(messages)
        r = self.responses[self.idx]
        self.idx += 1
        return r


class _MockCollector:
    """Stand-in for DynamicContextCollector — returns a canned DynamicEvidence."""

    def __init__(self, evidence: DynamicEvidence):
        self.evidence = evidence
        self.calls: List[Dict[str, Any]] = []

    def collect(self, **kwargs) -> DynamicEvidence:
        self.calls.append(kwargs)
        return self.evidence


def _ctx(method_text: str, cut_src: str | None = None, cut_fqcn: str | None = None) -> ExecutionContext:
    return ExecutionContext(
        method_name="__test__",
        method_line_range=(1, len(method_text.splitlines())),
        file_text=method_text,
        cut_source=cut_src,
        cut_fqcn=cut_fqcn,
    )


NASE_METHOD = textwrap.dedent("""\
    @Test
    public void test() {
        PlotDatum plotDatum0 = new PlotDatum(-1145.56, -1145.56, true);
        plotDatum0.setYError(-1145.56);
    }""")

NASE_ACT_CALL = {
    "expr": "plotDatum0.setYError(-1145.56)",
    "scope": "plotDatum0",
    "name": "setYError",
    "begin_line": 4,
    "return_type": "void",
    "declaring_type": "jahuwaldt.plot.PlotDatum",
}

NASE_CAPTURE_REQ = CaptureRequest(
    test_file=Path("/tmp/_unused_in_mock.java"),
    test_method_name="test",
    act_call_info=NASE_ACT_CALL,
    cut_source="class PlotDatum {}",
    cut_fqcn="jahuwaldt.plot.PlotDatum",
)


def _success_evidence() -> DynamicEvidence:
    """A canned DynamicEvidence that mirrors the PlotDatum::test05 capture."""
    return DynamicEvidence(
        state_before={"getYError()": "0.0", "hasErrorBar()": "false", "getX()": "-1145.56"},
        state_after={"getYError()": "-1145.56", "hasErrorBar()": "true", "getX()": "-1145.56"},
        capture_success=True,
        error=None,
        act_call_line=4,
        captured_getters=["getYError", "hasErrorBar", "getX"],
        getter_sources={"getYError": "cut", "hasErrorBar": "cut", "getX": "cut"},
        stdout="(mock)",
        elapsed_ms=800,
    )


def _failure_evidence(reason: str = "no_observable_getters") -> DynamicEvidence:
    return DynamicEvidence(capture_success=False, error=reason, elapsed_ms=50)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestTier4Scope(unittest.TestCase):
    def test_scope_membership(self):
        self.assertEqual(TIER4_SMELLS, frozenset({"NASE", "TSVM"}))

    def test_is_tier4_smell_helper(self):
        self.assertTrue(is_tier4_smell("NASE"))
        self.assertTrue(is_tier4_smell("TSVM"))
        self.assertFalse(is_tier4_smell("NARV"))
        self.assertFalse(is_tier4_smell("NNA"))

    def test_non_tier4_short_circuits(self):
        runner = PlanRunner(_MockLLM(responses=[]))
        r = plan_tier4("NARV", {}, NASE_METHOD, _ctx(NASE_METHOD), runner=runner)
        self.assertFalse(r.success)
        self.assertEqual(r.mode, "skipped")
        self.assertIn("not in Tier 4 scope", r.error or "")


# ---------------------------------------------------------------------------
# Dynamic mode
# ---------------------------------------------------------------------------


class TestTier4Dynamic(unittest.TestCase):
    def test_dynamic_mode_uses_observed_values_in_prompt(self):
        """When capture succeeds, dynamic_evidence JSON MUST be rendered in
        the user prompt — that's the whole point of Tier 4."""
        llm = _MockLLM(responses=["[]"])
        collector = _MockCollector(_success_evidence())
        plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD, cut_src="class PlotDatum {}"),
            runner=PlanRunner(llm),
            capture_request=NASE_CAPTURE_REQ,
            dynamic_collector=collector,
        )
        user = llm.calls[0][1]["content"]
        self.assertIn("Dynamic evidence", user)
        self.assertIn("-1145.56", user)         # observed changed value
        self.assertIn("hasErrorBar()", user)    # observed getter key
        self.assertIn("changed_fields", user)
        self.assertIn("unchanged_fields", user)

    def test_dynamic_result_reports_mode_and_evidence(self):
        llm = _MockLLM(responses=['[{"op":"INSERT_ASSERTION","params":{"after_line":4,"assert_type":"assertEquals","actual_expr":"plotDatum0.getYError()","expected_expr":"-1145.56"}}]'])
        collector = _MockCollector(_success_evidence())
        r = plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD, cut_src="class PlotDatum {}"),
            runner=PlanRunner(llm),
            capture_request=NASE_CAPTURE_REQ,
            dynamic_collector=collector,
        )
        self.assertIsInstance(r, Tier4Result)
        self.assertEqual(r.mode, "dynamic")
        self.assertTrue(r.success)
        self.assertEqual(len(r.plans), 1)
        self.assertEqual(r.plans[0].op, OperatorId.INSERT_ASSERTION)
        self.assertIsNotNone(r.dynamic_evidence)
        self.assertIn("changed_fields", r.dynamic_evidence)
        self.assertEqual(r.capture_error, None)
        self.assertEqual(r.getter_sources["getYError"], "cut")
        self.assertGreater(r.elapsed_capture_ms, 0)

    def test_dynamic_evidence_filters_unchanged(self):
        """The rendered dynamic_evidence payload must expose both changed
        and unchanged field lists so the LLM can skip the unchanged ones."""
        llm = _MockLLM(responses=["[]"])
        plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD, cut_src="class PlotDatum {}"),
            runner=PlanRunner(llm),
            capture_request=NASE_CAPTURE_REQ,
            dynamic_collector=_MockCollector(_success_evidence()),
        )
        user = llm.calls[0][1]["content"]
        # The canned evidence has getX unchanged → must appear under unchanged_fields
        # but still present in state_before/state_after.
        self.assertIn('"unchanged_fields"', user)
        self.assertIn("getX()", user)


# ---------------------------------------------------------------------------
# Static-fallback mode
# ---------------------------------------------------------------------------


class TestTier4StaticFallback(unittest.TestCase):
    def test_capture_failure_falls_back_to_static(self):
        llm = _MockLLM(responses=["[]"])
        collector = _MockCollector(_failure_evidence("no_observable_getters"))
        r = plan_tier4(
            "NASE", {"unverified_side_effect_calls": [{"modified_fields": ["yErr"]}]},
            NASE_METHOD, _ctx(NASE_METHOD, cut_src="class PlotDatum {}"),
            runner=PlanRunner(llm),
            capture_request=NASE_CAPTURE_REQ,
            dynamic_collector=collector,
        )
        self.assertEqual(r.mode, "static_fallback")
        self.assertEqual(r.capture_error, "no_observable_getters")
        self.assertIsNone(r.dynamic_evidence)
        user = llm.calls[0][1]["content"]
        # No dynamic block — verify by absence.
        self.assertNotIn("Dynamic evidence (observed runtime state)", user)
        # But static smell evidence + guide should still be there.
        self.assertIn("Smell evidence", user)
        self.assertIn("Smell guide", user)

    def test_no_collector_provided_is_static(self):
        llm = _MockLLM(responses=["[]"])
        r = plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD, cut_src="class X {}"),
            runner=PlanRunner(llm),
            capture_request=NASE_CAPTURE_REQ,    # collector missing
        )
        self.assertEqual(r.mode, "static_fallback")
        self.assertEqual(r.capture_error, "collector_not_provided")

    def test_no_capture_request_is_static(self):
        llm = _MockLLM(responses=["[]"])
        collector = _MockCollector(_success_evidence())
        r = plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD, cut_src="class X {}"),
            runner=PlanRunner(llm),
            dynamic_collector=collector,          # request missing
        )
        self.assertEqual(r.mode, "static_fallback")
        self.assertEqual(r.capture_error, "capture_request_not_provided")
        self.assertEqual(collector.calls, [])   # never invoked

    def test_collector_exception_is_recorded(self):
        class _Boom:
            def collect(self, **kw):
                raise RuntimeError("ant died")

        llm = _MockLLM(responses=["[]"])
        r = plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD, cut_src="class X {}"),
            runner=PlanRunner(llm),
            capture_request=NASE_CAPTURE_REQ,
            dynamic_collector=_Boom(),
        )
        self.assertEqual(r.mode, "static_fallback")
        self.assertIn("RuntimeError", r.capture_error or "")
        self.assertIn("ant died", r.capture_error or "")


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


class TestTier4Prompt(unittest.TestCase):
    def test_prompt_carries_tier4_header_and_allowed_ops(self):
        llm = _MockLLM(responses=["[]"])
        plan_tier4(
            "TSVM", {}, NASE_METHOD, _ctx(NASE_METHOD),
            runner=PlanRunner(llm),
        )
        user = llm.calls[0][1]["content"]
        self.assertIn("tier=4", user)
        self.assertIn("Tier 4 — dynamic-context", user)
        self.assertIn("INSERT_ASSERTION", user)

    def test_fewshot_examples_rendered(self):
        llm = _MockLLM(responses=["[]"])
        plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD),
            runner=PlanRunner(llm),
        )
        user = llm.calls[0][1]["content"]
        self.assertIn("## Examples", user)
        # NASE[0] is the Consumer example
        self.assertIn("consumer.getProcessedCount", user)

    def test_smell_guide_rendered(self):
        llm = _MockLLM(responses=["[]"])
        plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD),
            runner=PlanRunner(llm),
        )
        user = llm.calls[0][1]["content"]
        self.assertIn("## Smell guide", user)
        # Guide is NASE.md
        self.assertIn("Not Asserted Side Effect", user)

    def test_previous_feedback_is_propagated(self):
        llm = _MockLLM(responses=["[]"])
        plan_tier4(
            "NASE", {}, NASE_METHOD, _ctx(NASE_METHOD),
            runner=PlanRunner(llm),
            previous_feedback="Earlier attempt used assertNotNull — use the real getter.",
        )
        user = llm.calls[0][1]["content"]
        self.assertIn("Previous attempt failed", user)
        self.assertIn("assertNotNull", user)


if __name__ == "__main__":
    unittest.main()

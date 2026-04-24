"""Tests for MultiModelClient — use a fake ``requests.Session`` so no real
HTTP is issued.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest import mock

from smell_repair_v2.config.loader import (
    BudgetExceededError,
    DevExperimentConfig,
    LlmRuntimeConfig,
    ModelSpec,
    OpenRouterConfig,
)
from smell_repair_v2.llm.multi_client import (
    MultiModelClient,
    THINKING_DISABLE_CONFIG,
    _BoundClient,
    contains_thinking_artifact,
)


def _mock_runtime(budget: float = 10.0) -> LlmRuntimeConfig:
    return LlmRuntimeConfig(
        openrouter=OpenRouterConfig(
            api_key="sk-test",
            base_url="https://example.invalid/api/v1",
            timeout_sec=30,
            default_headers={"HTTP-Referer": "https://ex.com", "X-Title": "t"},
        ),
        models={
            "cheap": ModelSpec(
                key="cheap", id="vendor/cheap", display_name="Cheap",
                input_price_per_m=1.0, output_price_per_m=2.0,
                context_window=131072, notes="",
            ),
            "pricey": ModelSpec(
                key="pricey", id="vendor/pricey", display_name="Pricey",
                input_price_per_m=10.0, output_price_per_m=20.0,
                context_window=131072, notes="",
            ),
        },
        dev=DevExperimentConfig(
            projects=["p"], tier2_smells=["ENET"],
            max_requests_per_method=3, cost_budget_per_model_usd=budget,
            temperature=0.2, max_output_tokens=100,
        ),
    )


@dataclass
class _FakeResponse:
    status_code: int
    _payload: Dict[str, Any] = field(default_factory=dict)
    text: str = ""

    def json(self) -> Dict[str, Any]:
        return self._payload


@dataclass
class _FakeSession:
    """Replaces requests.Session.post. Records calls so tests can assert on
    headers / body."""
    posts: List[Dict[str, Any]] = field(default_factory=list)
    response_for: Dict[str, _FakeResponse] = field(default_factory=dict)
    default_response: _FakeResponse = field(
        default_factory=lambda: _FakeResponse(
            200, {
                "choices": [{"message": {"content": "4"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )
    )

    def post(self, url, headers=None, data=None, timeout=None):
        body = json.loads(data)
        self.posts.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        model_id = body.get("model")
        return self.response_for.get(model_id, self.default_response)


class TestMultiModelClient(unittest.TestCase):
    def _patch_session(self, session: _FakeSession):
        return mock.patch("requests.Session", return_value=session)

    def test_rejects_when_api_key_placeholder(self):
        rt = _mock_runtime()
        # force placeholder
        rt = LlmRuntimeConfig(
            openrouter=OpenRouterConfig(
                api_key="PLACEHOLDER_SET_BY_USER", base_url=rt.openrouter.base_url,
                timeout_sec=rt.openrouter.timeout_sec,
                default_headers=rt.openrouter.default_headers,
            ),
            models=rt.models, dev=rt.dev,
        )
        with self.assertRaises(RuntimeError) as ctx:
            MultiModelClient(rt)
        self.assertIn("PLACEHOLDER_SET_BY_USER", str(ctx.exception))

    def test_chat_records_usage_and_cost(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(_mock_runtime())
            resp = mc.chat("cheap", [{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "4")
        self.assertEqual(resp.input_tokens, 20)
        self.assertEqual(resp.output_tokens, 5)
        # cheap: $1 input/M, $2 output/M → 20/1e6 + 5/1e6 * 2 = 30e-6
        self.assertAlmostEqual(resp.cost_usd, 20 / 1e6 * 1.0 + 5 / 1e6 * 2.0, places=9)
        stats = mc.get_usage("cheap")
        self.assertEqual(stats.total_requests, 1)
        self.assertEqual(stats.total_input_tokens, 20)
        self.assertEqual(stats.total_output_tokens, 5)

    def test_bound_client_satisfies_chat_client_protocol(self):
        """_BoundClient.chat(messages) returns str — PlanRunner-compatible."""
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(_mock_runtime())
            bound = mc.client_for("cheap")
            content = bound.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(content, "4")
        # routed through the right model
        self.assertEqual(session.posts[0]["body"]["model"], "vendor/cheap")

    def test_extra_headers_forwarded(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(_mock_runtime())
            mc.chat("cheap", [{"role": "user", "content": "hi"}])
        headers = session.posts[0]["headers"]
        self.assertEqual(headers["HTTP-Referer"], "https://ex.com")
        self.assertEqual(headers["X-Title"], "t")
        self.assertTrue(headers["Authorization"].startswith("Bearer sk-test"))

    def test_budget_exceeded_blocks_next_call(self):
        session = _FakeSession()
        # expensive response to burn through a tiny budget
        session.default_response = _FakeResponse(
            200, {
                "choices": [{"message": {"content": "x" * 100}}],
                "usage": {"prompt_tokens": 50_000, "completion_tokens": 50_000},
            },
        )
        with self._patch_session(session):
            mc = MultiModelClient(_mock_runtime(budget=0.10))
            # first call: cheap model, 50k in * $1/M + 50k out * $2/M = $0.15 → over budget
            resp = mc.chat("cheap", [{"role": "user", "content": "hi"}])
            self.assertGreater(resp.cost_usd, 0.10)
            # next call must refuse
            with self.assertRaises(BudgetExceededError) as ctx:
                mc.chat("cheap", [{"role": "user", "content": "hi again"}])
        self.assertIn("cheap", str(ctx.exception))

    def test_budget_per_model_isolation(self):
        """Burning 'cheap' must not stop 'pricey'."""
        session = _FakeSession()
        session.response_for["vendor/cheap"] = _FakeResponse(
            200, {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 200_000, "completion_tokens": 200_000},
            },
        )
        with self._patch_session(session):
            mc = MultiModelClient(_mock_runtime(budget=0.30))
            mc.chat("cheap", [{"role": "user", "content": "hi"}])
            with self.assertRaises(BudgetExceededError):
                mc.chat("cheap", [{"role": "user", "content": "hi"}])
            # pricey still has full budget
            resp = mc.chat("pricey", [{"role": "user", "content": "hi"}])
            self.assertEqual(resp.content, "4")

    def test_error_is_counted_even_when_raised(self):
        session = _FakeSession()
        session.default_response = _FakeResponse(500, text="server exploded")
        # patch time.sleep inside the client so bounded-backoff doesn't slow the test
        with self._patch_session(session), \
             mock.patch("smell_repair_v2.llm.client.time.sleep", return_value=None):
            mc = MultiModelClient(_mock_runtime())
            with self.assertRaises(RuntimeError):
                mc.chat("cheap", [{"role": "user", "content": "hi"}])
            self.assertEqual(mc.get_usage("cheap").errors, 1)
            self.assertEqual(mc.get_usage("cheap").total_requests, 1)
            self.assertEqual(mc.get_usage("cheap").total_cost_usd, 0.0)

    def test_unknown_model_key_raises(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(_mock_runtime())
            with self.assertRaises(KeyError):
                mc.chat("does-not-exist", [{"role": "user", "content": "hi"}])
            with self.assertRaises(KeyError):
                mc.client_for("does-not-exist")


class TestThinkingDisableRouting(unittest.TestCase):
    """Verifies the Phase-2.2 thinking/reasoning disable knobs actually land
    in the outgoing HTTP body."""

    def _patch_session(self, session: _FakeSession):
        return mock.patch("requests.Session", return_value=session)

    def _runtime(self, keys):
        base = _mock_runtime()
        # Use the real THINKING_DISABLE_CONFIG keys so the decoration
        # machinery fires; we re-stub the model list to avoid needing the
        # real 'qwen35_9b'-style specs.
        return LlmRuntimeConfig(
            openrouter=base.openrouter,
            models={
                k: ModelSpec(
                    key=k, id=f"vendor/{k}", display_name=k,
                    input_price_per_m=1.0, output_price_per_m=2.0,
                    context_window=131072, notes="",
                )
                for k in keys
            },
            dev=base.dev,
        )

    def test_qwen_extra_body_and_no_think_suffix(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(self._runtime(["qwen35_27b"]))
            mc.chat("qwen35_27b", [
                {"role": "system", "content": "You are a JSON emitter."},
                {"role": "user", "content": "ping"},
            ])
        body = session.posts[0]["body"]
        # top-level reasoning toggle
        self.assertEqual(body.get("reasoning"), {"enabled": False})
        # model-native Qwen knob
        self.assertIs(body.get("enable_thinking"), False)
        # chat_template_kwargs (vLLM path)
        self.assertEqual(body.get("chat_template_kwargs"), {"enable_thinking": False})
        # /no_think appended to the first system message
        sys_content = body["messages"][0]["content"]
        self.assertIn("/no_think", sys_content)
        self.assertTrue(sys_content.startswith("You are a JSON emitter."))

    def test_qwen_no_think_prepended_when_no_system_message(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(self._runtime(["qwen35_9b"]))
            mc.chat("qwen35_9b", [{"role": "user", "content": "ping"}])
        msgs = session.posts[0]["body"]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "/no_think")
        self.assertEqual(msgs[1]["role"], "user")

    def test_gemma_reasoning_disabled(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(self._runtime(["gemma4_31b"]))
            mc.chat("gemma4_31b", [
                {"role": "system", "content": "X"}, {"role": "user", "content": "Y"},
            ])
        body = session.posts[0]["body"]
        self.assertEqual(body.get("reasoning"), {"enabled": False})
        # Gemma has no /no_think suffix
        self.assertEqual(body["messages"][0]["content"], "X")
        self.assertNotIn("enable_thinking", body)

    def test_gpt_oss_reasoning_effort_low(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(self._runtime(["gpt_oss_20b"]))
            mc.chat("gpt_oss_20b", [{"role": "user", "content": "Y"}])
        body = session.posts[0]["body"]
        self.assertEqual(body.get("reasoning"), {"effort": "low"})
        self.assertEqual(body.get("reasoning_effort"), "low")

    def test_coder_next_has_no_extra_body(self):
        """qwen_coder_next is documented as non-thinking-only — we must not
        inject reasoning knobs that would cause the provider to reject the
        payload."""
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(self._runtime(["qwen_coder_next"]))
            mc.chat("qwen_coder_next", [{"role": "user", "content": "Y"}])
        body = session.posts[0]["body"]
        self.assertNotIn("reasoning", body)
        self.assertNotIn("enable_thinking", body)

    def test_caller_extra_body_overrides_recipe(self):
        session = _FakeSession()
        with self._patch_session(session):
            mc = MultiModelClient(self._runtime(["qwen35_9b"]))
            mc.chat(
                "qwen35_9b",
                [{"role": "user", "content": "x"}],
                extra_body={"reasoning": {"enabled": True}},
            )
        body = session.posts[0]["body"]
        # Caller's explicit override wins over the THINKING_DISABLE_CONFIG default.
        self.assertEqual(body.get("reasoning"), {"enabled": True})

    def test_thinking_artifact_detector(self):
        self.assertTrue(contains_thinking_artifact("prefix <think>...</think> suffix"))
        self.assertTrue(contains_thinking_artifact("<THINKING>..</THINKING>"))
        self.assertTrue(contains_thinking_artifact("<reasoning>..."))
        self.assertFalse(contains_thinking_artifact("{\"status\": \"ok\"}"))
        self.assertFalse(contains_thinking_artifact(""))


if __name__ == "__main__":
    unittest.main()

"""Multi-model wrapper for Phase 2.2 comparison.

Owns one ``OpenAICompatibleClient`` per model spec, meters usage + cost per
call, and enforces a per-model dollar budget. Bound adapters satisfy the
existing ``ChatClient`` protocol so ``PlanRunner`` can drive any model
without the runner needing a model_key parameter.

**Thinking-mode handling**. Several candidate models (Qwen 3.5, Gemma 4,
gpt-oss-20b) emit large ``<think>``/``<reasoning>`` blocks by default. Under
``max_tokens=2000`` those blocks exhaust the budget before any user-visible
content is produced — the initial smoke test on Qwen 3.5 9B/27B and
gpt-oss-20b returned empty ``content`` with ``output_tokens==max_tokens``.

To disable reasoning we pass two layers per call (best-effort — not all
providers honour every knob):

1. ``extra_body``: OpenRouter's standard ``reasoning: {enabled: false}`` /
   ``reasoning: {effort: ...}`` object AND the model-native ``enable_thinking``
   flag where applicable.
2. A system-message suffix such as ``/no_think`` (Qwen-specific control
   token) appended to the first system message.

After each call, ``_contains_thinking_artifact`` scans the response for
``<think>``/``<reasoning>`` literals so we can log when the knobs didn't
take effect and fall back gracefully.
"""
from __future__ import annotations

import copy
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from smell_repair_v2.config.loader import (
    BudgetExceededError,
    LlmRuntimeConfig,
    ModelSpec,
)
from smell_repair_v2.llm.client import ChatUsage, LlmConfig, OpenAICompatibleClient


# Per-model thinking-disable recipe. Each entry is applied on every call via
# ``MultiModelClient._decorate_request``. Missing keys = default (empty).
#
# OpenRouter's unified ``reasoning`` field is tried first for all models that
# support reasoning; model-native flags (Qwen's ``enable_thinking``,
# gpt-oss's ``reasoning_effort``) ride alongside as belt-and-suspenders.
THINKING_DISABLE_CONFIG: Dict[str, Dict[str, Any]] = {
    "qwen35_9b": {
        "extra_body": {
            "reasoning": {"enabled": False},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        "system_suffix": "/no_think",
    },
    "qwen35_27b": {
        "extra_body": {
            "reasoning": {"enabled": False},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        "system_suffix": "/no_think",
    },
    "qwen_coder_next": {
        # Documented as non-thinking-only; no flag needed.
    },
    "gemma4_31b": {
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    "gpt_oss_20b": {
        "extra_body": {
            "reasoning": {"effort": "low"},
            "reasoning_effort": "low",
        },
    },
}


_THINKING_ARTIFACT_RE = re.compile(
    r"(?is)<\s*(?:think(?:ing)?|reasoning)\b[^>]*>"
    r"|</\s*(?:think(?:ing)?|reasoning)\s*>"
)


def contains_thinking_artifact(content: str) -> bool:
    """True iff the assistant content contains a literal reasoning/thinking
    tag. Fallback after provider-side knobs fail; used by smoke test + runner
    logs."""
    if not content:
        return False
    return bool(_THINKING_ARTIFACT_RE.search(content))


@dataclass
class ChatResponse:
    """The user-visible result of one metered call."""
    content: str
    model_key: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float


@dataclass
class UsageStats:
    model: ModelSpec
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    errors: int = 0

    def record(self, resp: ChatResponse) -> None:
        self.total_requests += 1
        self.total_input_tokens += resp.input_tokens
        self.total_output_tokens += resp.output_tokens
        self.total_latency_ms += resp.latency_ms
        self.total_cost_usd += resp.cost_usd

    def record_error(self) -> None:
        self.errors += 1
        self.total_requests += 1

    def avg_latency_ms(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests


def _price_of(spec: ModelSpec, usage: ChatUsage) -> float:
    return (
        usage.input_tokens / 1_000_000.0 * spec.input_price_per_m
        + usage.output_tokens / 1_000_000.0 * spec.output_price_per_m
    )


class MultiModelClient:
    """Holds one client per model and centralises budget / usage accounting.

    - ``client_for(model_key)`` returns a ``_BoundClient`` that satisfies the
      ``ChatClient`` protocol; handlers pass it straight to ``PlanRunner``.
    - ``chat(model_key, messages)`` is the richer entry point that returns a
      ``ChatResponse`` (content + usage + cost).
    """

    def __init__(self, runtime: LlmRuntimeConfig) -> None:
        runtime.require()  # surfaces placeholder api_key as a friendly error
        self._runtime = runtime
        self._clients: Dict[str, OpenAICompatibleClient] = {}
        self._usage: Dict[str, UsageStats] = {}
        self._lock = threading.Lock()

        for key, spec in runtime.models.items():
            llm_cfg = LlmConfig(
                base_url=runtime.openrouter.base_url,
                api_key=runtime.openrouter.api_key,
                model=spec.id,
                temperature=runtime.dev.temperature,
                top_p=0.9,
                max_tokens=runtime.dev.max_output_tokens,
                request_timeout_sec=runtime.openrouter.timeout_sec,
                extra_headers=dict(runtime.openrouter.default_headers),
            )
            self._clients[key] = OpenAICompatibleClient(llm_cfg)
            self._usage[key] = UsageStats(spec)

    # ------------------------------------------------------------------ API

    @property
    def model_keys(self) -> List[str]:
        return list(self._clients.keys())

    def model_spec(self, model_key: str) -> ModelSpec:
        return self._runtime.models[model_key]

    def budget_remaining(self, model_key: str) -> float:
        cap = self._runtime.dev.cost_budget_per_model_usd
        return max(0.0, cap - self._usage[model_key].total_cost_usd)

    def get_usage(self, model_key: str) -> UsageStats:
        return self._usage[model_key]

    def all_usage(self) -> Dict[str, UsageStats]:
        return dict(self._usage)

    def client_for(self, model_key: str) -> "_BoundClient":
        if model_key not in self._clients:
            raise KeyError(
                f"unknown model_key {model_key!r}; configured: {sorted(self._clients)}"
            )
        return _BoundClient(self, model_key)

    def chat(
        self,
        model_key: str,
        messages: List[Dict[str, str]],
        **overrides: Any,
    ) -> ChatResponse:
        if model_key not in self._clients:
            raise KeyError(
                f"unknown model_key {model_key!r}; configured: {sorted(self._clients)}"
            )
        stats = self._usage[model_key]
        spec = self._runtime.models[model_key]

        # Budget check BEFORE the call so we don't exceed on the next request.
        cap = self._runtime.dev.cost_budget_per_model_usd
        if stats.total_cost_usd >= cap:
            raise BudgetExceededError(
                f"model {model_key!r} already at ${stats.total_cost_usd:.4f} / "
                f"${cap:.2f} budget"
            )

        decorated_messages, extra_body = self._decorate_request(model_key, messages)
        caller_extra = overrides.pop("extra_body", None)
        if caller_extra:
            extra_body = {**extra_body, **caller_extra}

        client = self._clients[model_key]
        try:
            content, usage = client.chat_with_usage(
                decorated_messages,
                extra_body=extra_body or None,
                **overrides,
            )
        except Exception:
            with self._lock:
                stats.record_error()
            raise

        cost = _price_of(spec, usage)
        resp = ChatResponse(
            content=content,
            model_key=model_key,
            model_id=spec.id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=usage.latency_ms,
            cost_usd=cost,
        )
        with self._lock:
            stats.record(resp)
        return resp

    # ------------------------------------------------------------- internals

    def _decorate_request(
        self,
        model_key: str,
        messages: List[Dict[str, str]],
    ) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
        """Apply the per-model thinking-disable recipe to outgoing messages.

        Returns (new_messages, extra_body) — neither mutates the caller's
        inputs.
        """
        recipe = THINKING_DISABLE_CONFIG.get(model_key, {})
        extra_body: Dict[str, Any] = copy.deepcopy(recipe.get("extra_body", {}) or {})

        suffix = recipe.get("system_suffix")
        if not suffix:
            return list(messages), extra_body

        new_messages = [dict(m) for m in messages]
        # find first system message; inject suffix on a new line.
        for m in new_messages:
            if m.get("role") == "system":
                existing = (m.get("content") or "").rstrip()
                m["content"] = f"{existing}\n{suffix}" if existing else suffix
                return new_messages, extra_body
        # no system message present — prepend one with just the suffix.
        new_messages.insert(0, {"role": "system", "content": suffix})
        return new_messages, extra_body


@dataclass
class _BoundClient:
    """``ChatClient``-compatible adapter bound to a single model_key."""
    multi: MultiModelClient
    model_key: str

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str:
        return self.multi.chat(self.model_key, messages, **overrides).content

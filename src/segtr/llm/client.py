from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 2048
    request_timeout_sec: int = 180
    # Extra HTTP headers to merge with Authorization/Content-Type on every
    # request. OpenRouter uses `HTTP-Referer` and `X-Title` for leaderboard
    # attribution; other providers usually ignore unknown headers.
    extra_headers: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatUsage:
    """Token/timing counters for one ``chat_with_usage`` call.

    Cost is not computed here — the caller (e.g. ``MultiModelClient``) knows
    the pricing for its bound model.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class OpenAICompatibleClient:
    """Chat Completions client for OpenAI-compatible endpoints (e.g., vLLM)."""

    def __init__(self, cfg: LlmConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str:
        """Return just the assistant content. Backward-compatible entry point."""
        content, _ = self.chat_with_usage(messages, **overrides)
        return content

    def chat_with_usage(
        self,
        messages: List[Dict[str, str]],
        *,
        extra_body: Optional[Dict[str, Any]] = None,
        **overrides: Any,
    ) -> Tuple[str, ChatUsage]:
        """Same as ``chat`` but also reports token counts + wall-clock latency
        so upstream cost/budget tracking can record per-call usage.

        ``extra_body`` is merged at the top level of the JSON payload. Used by
        ``MultiModelClient`` to pass provider-specific knobs such as
        OpenRouter's ``reasoning`` object or Qwen's ``enable_thinking`` flag.
        """
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": overrides.get("model", self.cfg.model),
            "messages": messages,
            "temperature": overrides.get("temperature", self.cfg.temperature),
            "top_p": overrides.get("top_p", self.cfg.top_p),
            "max_tokens": overrides.get("max_tokens", self.cfg.max_tokens),
        }
        if extra_body:
            for k, v in extra_body.items():
                # extra_body wins over defaults but must not clobber 'model'
                # or 'messages' (safety against accidental misuse).
                if k in ("model", "messages"):
                    continue
                payload[k] = v
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }
        if self.cfg.extra_headers:
            headers.update(self.cfg.extra_headers)
        # Retry transient failures (429/5xx/timeouts) with bounded exponential backoff.
        max_attempts = 4
        base_delay_sec = 1.5
        max_delay_sec = 20.0

        t0 = time.monotonic()
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.post(
                    url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=self.cfg.request_timeout_sec,
                )
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.RequestException,
            ) as e:
                if attempt >= max_attempts:
                    raise RuntimeError(f"LLM request failed after retries: {e}") from e
                delay = min(max_delay_sec, base_delay_sec * (2 ** (attempt - 1)))
                delay *= 1.0 + random.uniform(0.0, 0.25)
                time.sleep(delay)
                continue

            status = resp.status_code
            if status == 200:
                try:
                    data = resp.json()
                except ValueError as e:
                    if attempt < max_attempts:
                        delay = min(max_delay_sec, base_delay_sec * (2 ** (attempt - 1)))
                        delay *= 1.0 + random.uniform(0.0, 0.25)
                        time.sleep(delay)
                        continue
                    snippet = (resp.text or "")[:500]
                    raise RuntimeError(f"LLM HTTP 200 but invalid JSON: {snippet}") from e
                content = (data["choices"][0].get("message") or {}).get("content") or ""
                usage_raw = data.get("usage") or {}
                usage = ChatUsage(
                    input_tokens=int(usage_raw.get("prompt_tokens") or 0),
                    output_tokens=int(usage_raw.get("completion_tokens") or 0),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )
                return content, usage

            retryable = status == 429 or 500 <= status < 600
            if retryable and attempt < max_attempts:
                delay = min(max_delay_sec, base_delay_sec * (2 ** (attempt - 1)))
                delay *= 1.0 + random.uniform(0.0, 0.25)
                time.sleep(delay)
                continue

            raise RuntimeError(f"LLM HTTP {status}: {resp.text}")

        # This should be unreachable, but keeps the type checker happy.
        raise RuntimeError("LLM request failed without a response")

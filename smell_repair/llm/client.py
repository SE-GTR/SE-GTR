from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List

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


class OpenAICompatibleClient:
    """Chat Completions client for OpenAI-compatible endpoints (e.g., vLLM)."""

    def __init__(self, cfg: LlmConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": overrides.get("model", self.cfg.model),
            "messages": messages,
            "temperature": overrides.get("temperature", self.cfg.temperature),
            "top_p": overrides.get("top_p", self.cfg.top_p),
            "max_tokens": overrides.get("max_tokens", self.cfg.max_tokens),
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }
        # Retry transient failures (429/5xx/timeouts) with bounded exponential backoff.
        max_attempts = 4
        base_delay_sec = 1.5
        max_delay_sec = 20.0

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
                    # Some providers return truncated/HTML bodies with 200.
                    if attempt < max_attempts:
                        delay = min(max_delay_sec, base_delay_sec * (2 ** (attempt - 1)))
                        delay *= 1.0 + random.uniform(0.0, 0.25)
                        time.sleep(delay)
                        continue
                    snippet = (resp.text or "")[:500]
                    raise RuntimeError(f"LLM HTTP 200 but invalid JSON: {snippet}") from e
                return (data["choices"][0].get("message") or {}).get("content") or ""

            retryable = status == 429 or 500 <= status < 600
            if retryable and attempt < max_attempts:
                delay = min(max_delay_sec, base_delay_sec * (2 ** (attempt - 1)))
                delay *= 1.0 + random.uniform(0.0, 0.25)
                time.sleep(delay)
                continue

            raise RuntimeError(f"LLM HTTP {status}: {resp.text}")

        # This should be unreachable, but keeps the type checker happy.
        raise RuntimeError("LLM request failed without a response")

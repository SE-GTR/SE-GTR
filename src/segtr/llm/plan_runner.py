"""Drives an LLM through the plan-generation loop.

Responsibilities:
  - build prompt messages (delegated to ``prompts.build_plan_messages``)
  - call the LLM client
  - parse the response via ``plan_parser.parse_plan_response``
  - on ``PlanParseError`` (structural or schema failure), feed the error
    message back to the LLM and retry up to ``max_attempts`` times
  - surface a rich ``PlanRunResult`` (including a per-attempt trace) so Tier
    handlers and the E2E script can log whatever they need

Anything that needs method context (operator preconditions, compile/test
outcomes) lives downstream — this module only cares about producing a
well-formed plan.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Protocol

from smell_repair_v2.llm.plan_parser import PlanParseError, parse_plan_response
from smell_repair_v2.llm.prompts import (
    PlanPromptInputs,
    TIER_ALLOWED_OPERATORS,
    build_plan_messages,
)
from smell_repair_v2.operators.base import OperatorPlan


class ChatClient(Protocol):
    """The minimal surface ``PlanRunner`` needs — both the real
    ``OpenAICompatibleClient`` and the test mock satisfy this."""

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str: ...


@dataclass
class AttemptRecord:
    attempt: int
    outcome: str                # "parsed" | "parse_error" | "llm_error"
    raw_preview: str            # first 500 chars of LLM response
    reason: Optional[str] = None
    num_plans: Optional[int] = None
    messages_preview: Optional[Dict[str, int]] = None  # {sys_chars, user_chars}


@dataclass
class PlanRunResult:
    success: bool
    plans: List[OperatorPlan]
    attempts: int
    final_raw_response: str
    error: Optional[str]
    attempt_history: List[AttemptRecord] = field(default_factory=list)


def _retry_feedback(err: PlanParseError) -> str:
    """Format a PlanParseError for LLM consumption."""
    return (
        f"Previous attempt failed: {err.reason}\n\n"
        "Please correct your output and return ONLY a valid JSON array.\n"
        "Remember:\n"
        "- No markdown, no code fences, no explanations\n"
        "- Each operator must use the exact id from the allowed list\n"
        "- Required parameters must be present with correct types\n"
        "- Line numbers are integers, 1-indexed"
    )


class PlanRunner:
    """Runs the prompt → LLM → parse → retry loop.

    Retry happens on ``PlanParseError``. LLM-transport errors (network,
    HTTP 5xx/429) are handled by the underlying client; here they propagate
    out as non-retryable — returning failure immediately rather than wasting
    attempts on a dead endpoint.
    """

    def __init__(
        self,
        client: ChatClient,
        *,
        max_attempts: int = 3,
        logger: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.client = client
        self.max_attempts = max_attempts
        self.logger = logger

    def run(self, prompt_inputs: PlanPromptInputs) -> PlanRunResult:
        history: List[AttemptRecord] = []
        feedback = prompt_inputs.previous_attempt_feedback
        allowed = prompt_inputs.allowed_operators or TIER_ALLOWED_OPERATORS.get(
            prompt_inputs.tier, []
        )

        for attempt in range(1, self.max_attempts + 1):
            # assemble messages with the current retry-feedback
            attempt_inputs = replace(
                prompt_inputs,
                previous_attempt_feedback=feedback,
                allowed_operators=allowed,
            )
            messages = build_plan_messages(attempt_inputs)
            sizes = {
                "sys_chars": len(messages[0]["content"]),
                "user_chars": len(messages[1]["content"]),
            }
            self._log({
                "event": "llm_request",
                "attempt": attempt,
                "smell_id": prompt_inputs.smell_id,
                "tier": prompt_inputs.tier,
                "sizes": sizes,
            })

            # LLM call — transport errors are fatal for this run
            try:
                raw = self.client.chat(messages)
            except Exception as e:
                history.append(AttemptRecord(
                    attempt=attempt, outcome="llm_error",
                    raw_preview="", reason=f"{type(e).__name__}: {e}",
                    messages_preview=sizes,
                ))
                self._log({
                    "event": "llm_error", "attempt": attempt,
                    "error": repr(e),
                })
                return PlanRunResult(
                    success=False, plans=[], attempts=attempt,
                    final_raw_response="",
                    error=f"LLM call failed: {e}",
                    attempt_history=history,
                )

            # parse
            try:
                plans = parse_plan_response(
                    raw,
                    smell_id=prompt_inputs.smell_id,
                    allowed_operators=list(allowed),
                )
            except PlanParseError as e:
                history.append(AttemptRecord(
                    attempt=attempt, outcome="parse_error",
                    raw_preview=(raw or "")[:500],
                    reason=e.reason, messages_preview=sizes,
                ))
                self._log({
                    "event": "parse_error", "attempt": attempt,
                    "reason": e.reason,
                })
                if not e.recoverable or attempt == self.max_attempts:
                    return PlanRunResult(
                        success=False, plans=[], attempts=attempt,
                        final_raw_response=raw, error=e.reason,
                        attempt_history=history,
                    )
                feedback = _retry_feedback(e)
                continue

            # success
            history.append(AttemptRecord(
                attempt=attempt, outcome="parsed",
                raw_preview=(raw or "")[:500],
                num_plans=len(plans), messages_preview=sizes,
            ))
            self._log({
                "event": "parsed", "attempt": attempt,
                "num_plans": len(plans),
            })
            return PlanRunResult(
                success=True, plans=plans, attempts=attempt,
                final_raw_response=raw, error=None,
                attempt_history=history,
            )

        # unreachable
        return PlanRunResult(
            success=False, plans=[], attempts=self.max_attempts,
            final_raw_response="", error="max retries exhausted",
            attempt_history=history,
        )

    def _log(self, record: Dict[str, Any]) -> None:
        if self.logger is None:
            return
        try:
            self.logger(record)
        except Exception:
            # never let logger exceptions break the LLM loop
            pass

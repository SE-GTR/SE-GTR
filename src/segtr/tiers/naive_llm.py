"""Naive LLM baseline — single-shot whole-method rewrite.

RQ3 baseline for SE-GTR. Represents the simplest possible LLM approach to
test-smell repair: give the model a smelly test method + a bullet list of
smells, ask it to rewrite the method. No operator catalog, no few-shot
examples, no multi-step planning, no dynamic evidence.

Contrast with SE-GTR: this baseline has no structured output, no Tier
routing, no assertion-preservation guarantees at generation time. Any
correctness comes purely from gate validation (Gate 3 compile, Gate 4
test, Gate 5 coverage proxy). Gate 6 (smell substitution) and Gate 7
(assertion preservation) are intentionally **not** applied to this
baseline because the purpose of RQ3 is to show what happens when only
compile/test/coverage gates are enforced.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple


SmellTuple = Tuple[str, str, Dict[str, Any]]


class _ChatClient(Protocol):
    """Minimal LLM surface the naive handler needs."""

    def chat(self, messages: List[Dict[str, str]], **overrides: Any) -> str: ...


@dataclass
class NaiveResult:
    """Per-method outcome of the naive baseline."""

    mode: str                          # "accepted" | "rejected" | "error"
    reject_reason: Optional[str]       # parse_fail | gate3_* | gate4_* | gate5_* | error | ...
    original_method: str
    rewritten_method: Optional[str]
    llm_elapsed_s: float
    llm_cost_usd: float
    retry_count: int
    prompt_preview: str = ""
    raw_response_preview: str = ""
    attempts: List[Dict[str, Any]] = field(default_factory=list)


SYSTEM_PROMPT = (
    "You are an expert Java test engineer. You will be given a JUnit4 test "
    "method that has quality issues. Rewrite the test method to fix the "
    "identified issues while preserving its testing intent.\n"
    "\n"
    "Output ONLY the rewritten method code inside a ```java``` fenced block. "
    "Do not include the enclosing class, imports, or any explanatory text."
)


_SMELL_DESCRIPTIONS: Dict[str, str] = {
    "NNA": "assertNotNull-only assertions that do not verify the post-state",
    "DS": "duplicated setup across sibling tests that belongs in @Before",
    "TSES": "try/catch exception testing that should use @Test(expected=...) or assertThrows",
    "AC": "asserting constant literals instead of computed results",
    "ENET": "exception-raising method calls made with null arguments and no meaningful assertion",
    "EDIS": "exception-raising method calls reachable only because setup is incomplete",
    "EDED": "exception-raising method calls caused by missing external dependencies",
    "NARV": "the return value of the method under test is never asserted",
    "OIMT": "assertions that only check object initialization, repeated across tests",
    "TOFA": "assertions only exercising simple field accessors (getters)",
    "ARPM": "assertions on methods inherited from an unrelated parent class",
    "NASE": "a side-effecting call on the CUT whose post-state is never asserted",
    "TSVM": "multiple tests call the same void method without observing its effect",
}


def _format_smell_list(smells: List[SmellTuple]) -> str:
    """Render the per-method smell list for the prompt.

    Each bullet: ``- <smell_id> (<Smelly-E name>): <human description>``.
    Evidence dicts are deliberately omitted — the naive baseline shows what
    the LLM does with only the names."""
    lines: List[str] = []
    seen_ids: set = set()
    for smelly_name, smell_id, _evidence in smells:
        if smell_id in seen_ids:
            continue
        seen_ids.add(smell_id)
        desc = _SMELL_DESCRIPTIONS.get(smell_id, smelly_name)
        lines.append(f"- {smell_id} ({smelly_name}): {desc}")
    return "\n".join(lines)


_JAVA_BLOCK_RE = re.compile(
    r"```(?:java|Java|JAVA)?\s*\n(.*?)```", re.DOTALL
)
_METHOD_DECL_HINT = re.compile(
    r"(?:@[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?\s*)*"
    r"(?:public|private|protected|static|final|\s)+"
    r"\s*(?:<[^>]+>\s*)?"
    r"[A-Za-z_$][\w<>\[\],\s]*\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*"
    r"(?:throws\s+[^\{]+?)?\{",
    re.DOTALL,
)


def extract_method_from_llm_output(text: str) -> Optional[str]:
    """Extract a Java method declaration from LLM output.

    Strategy:
      1. Prefer the first ```java ... ``` fenced block; validate it parses
         as a method declaration.
      2. Fallback: if the output is raw Java (no fences), use it directly
         if it starts with an annotation/modifier and contains a method
         signature followed by a body.
      3. Otherwise return None — the caller will count a parse failure.
    """
    if not text:
        return None

    for m in _JAVA_BLOCK_RE.finditer(text):
        block = m.group(1).strip()
        if _METHOD_DECL_HINT.search(block) and block.rstrip().endswith("}"):
            return block

    stripped = text.strip()
    if _METHOD_DECL_HINT.search(stripped) and stripped.rstrip().endswith("}"):
        return stripped

    return None


def _build_messages(
    *,
    project_name: str,
    cut_fqcn: Optional[str],
    smells: List[SmellTuple],
    method_text: str,
    previous_feedback: Optional[str] = None,
) -> List[Dict[str, str]]:
    smell_block = _format_smell_list(smells) or "- (no smells listed)"
    user = (
        f"Project: {project_name}\n"
        f"Class under test: {cut_fqcn or 'UNKNOWN'}\n\n"
        f"This test method has the following quality issues:\n{smell_block}\n\n"
        f"Original test method:\n```java\n{method_text}\n```\n\n"
        "Rewrite this test method to fix the listed issues. Output ONLY the "
        "rewritten method code inside a ```java``` block. Do not include "
        "explanations."
    )
    if previous_feedback:
        user += (
            "\n\nYour previous response could not be parsed "
            f"({previous_feedback}). Respond again with ONLY a ```java``` "
            "block containing a single method declaration."
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def repair_test_naive(
    *,
    method_text: str,
    smells: List[SmellTuple],
    project_name: str,
    cut_fqcn: Optional[str],
    client: _ChatClient,
    max_attempts: int = 2,
) -> NaiveResult:
    """Run the naive LLM baseline for one test method.

    Returns a ``NaiveResult`` whose ``mode`` is ``"accepted"`` iff a method
    declaration was successfully extracted from the LLM output (within
    ``max_attempts`` retries). Gate validation is the caller's
    responsibility — this function is concerned only with generation.

    Parameters
    ----------
    client
        Object with a ``chat(messages, **overrides) -> str`` method. In the
        pipeline this is the bound adapter returned by
        ``MultiModelClient.client_for(model_key)`` (``_BoundClient``).
    max_attempts
        First call + up to ``max_attempts - 1`` retries on parse failure.
        Default 2 (one retry), intentionally minimal — the point of the
        naive baseline is that it does not have sophisticated retry logic.
    """
    t_start = time.monotonic()
    attempts: List[Dict[str, Any]] = []
    previous_feedback: Optional[str] = None
    last_raw = ""
    last_prompt_preview = ""

    for attempt_idx in range(max_attempts):
        messages = _build_messages(
            project_name=project_name,
            cut_fqcn=cut_fqcn,
            smells=smells,
            method_text=method_text,
            previous_feedback=previous_feedback,
        )
        if not last_prompt_preview:
            last_prompt_preview = messages[-1]["content"][:500]

        try:
            raw = client.chat(messages)
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            attempts.append({
                "idx": attempt_idx,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return NaiveResult(
                mode="error",
                reject_reason=f"llm_error:{type(exc).__name__}",
                original_method=method_text,
                rewritten_method=None,
                llm_elapsed_s=elapsed,
                llm_cost_usd=0.0,
                retry_count=attempt_idx,
                prompt_preview=last_prompt_preview,
                raw_response_preview=last_raw[:500],
                attempts=attempts,
            )

        last_raw = raw or ""
        rewritten = extract_method_from_llm_output(last_raw)
        attempts.append({
            "idx": attempt_idx,
            "parsed": rewritten is not None,
            "response_len": len(last_raw),
        })

        if rewritten is not None:
            elapsed = time.monotonic() - t_start
            return NaiveResult(
                mode="accepted",
                reject_reason=None,
                original_method=method_text,
                rewritten_method=rewritten,
                llm_elapsed_s=elapsed,
                llm_cost_usd=0.0,
                retry_count=attempt_idx,
                prompt_preview=last_prompt_preview,
                raw_response_preview=last_raw[:500],
                attempts=attempts,
            )

        previous_feedback = (
            "no ```java``` block found"
            if "```" not in last_raw
            else "extracted text did not look like a method declaration"
        )

    elapsed = time.monotonic() - t_start
    return NaiveResult(
        mode="rejected",
        reject_reason="parse_fail",
        original_method=method_text,
        rewritten_method=None,
        llm_elapsed_s=elapsed,
        llm_cost_usd=0.0,
        retry_count=max_attempts - 1,
        prompt_preview=last_prompt_preview,
        raw_response_preview=last_raw[:500],
        attempts=attempts,
    )

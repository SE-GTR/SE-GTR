"""Phase 2.1 prompt builder for operator-plan LLM handlers.

The LLM is asked to output a JSON array of operator invocations — it never
produces Java code directly. The system (OperatorExecutor) applies the plan
deterministically.

Public API:
  - SYSTEM_PROMPT_V2        — system message for plan-mode LLM calls
  - OPERATOR_SCHEMAS        — id → human-readable schema (kept consistent with
                              `operators/catalog.py`)
  - TIER_ALLOWED_OPERATORS  — tier → list of allowed operator ids
  - PlanPromptLimits        — char/count caps (extends v1's PromptLimits)
  - PlanPromptInputs        — inputs to build_plan_messages
  - build_plan_messages     — returns OpenAI-style messages list

Legacy v1 symbols (PromptInputs, PromptLimits, build_messages,
SYSTEM_PROMPT, load_smell_guides) are retained for pipeline.py compatibility
until Phase 2.4 migrates it to the new path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from smell_repair_v2.llm.evidence import (
    evidence_block_markdown,
    render_evidence_for_prompt,
)

# =============================================================================
# v2 SYSTEM PROMPT
# =============================================================================

SYSTEM_PROMPT_V2 = """You are a transformation planner for Java unit tests.

Your role is NOT to write code. Your role is to select transformation operators
and fill in their parameters. The system will generate the actual code changes.

You output a JSON array of operator invocations. Each operator has an id and a
parameters object. The system executes them in order.

Available operators will be listed in each task. Do NOT invent operators not
in the provided list.

Output format: A JSON array. Nothing else. No explanation, no markdown, no
code fences.

Example output:
[
  {"op": "CAPTURE_RETURN_VALUE", "params": {"target_line": 8, "var_name": "result", "var_type": "int"}},
  {"op": "INSERT_ASSERTION", "params": {"after_line": 8, "assert_type": "assertTrue", "actual_expr": "result >= 0"}}
]

Rules:
1. Output ONLY valid JSON. Parse failures cause retries.
2. Each operator must come from the provided allowed list.
3. Each operator's params must match its schema exactly.
4. Line numbers are 1-indexed, relative to the original test method shown.
5. Do NOT add operators for imports. Import management is automatic.
6. Do NOT use assertNotNull as a substitute when no meaningful assertion exists.
   Return an empty array [] if no safe transformation is possible.
7. Prefer fewer operators over more. Do not over-modify.
"""

# =============================================================================
# Allowed operators per tier
# =============================================================================

TIER_ALLOWED_OPERATORS: Dict[int, List[str]] = {
    2: [
        "REPLACE_NULL_ARG",
        "ADD_SETUP_CALL",
        "REPLACE_EXPRESSION",
        "REMOVE_TRY_CATCH_KEEP_BODY",
        "TRY_CATCH_TO_EXPECTED",
        "ADD_TEST_EXPECTED",
        "REPLACE_ASSERTION",
        "INSERT_ASSERTION",
        "REMOVE_ASSERTION",
    ],
    3: [
        # Tier 3 includes all Tier 2 operators plus structural ones.
        "REPLACE_NULL_ARG",
        "ADD_SETUP_CALL",
        "REPLACE_EXPRESSION",
        "REMOVE_TRY_CATCH_KEEP_BODY",
        "TRY_CATCH_TO_EXPECTED",
        "ADD_TEST_EXPECTED",
        "REPLACE_ASSERTION",
        "INSERT_ASSERTION",
        "REMOVE_ASSERTION",
        "CAPTURE_RETURN_VALUE",
        "INSERT_STATEMENT",
        "REMOVE_STATEMENT",
        "REMOVE_TEST_EXPECTED",
        "WRAP_WITH_ASSERT_THROWS",
    ],
    4: [
        # Tier 4: primarily observed-state assertion insertion.
        "INSERT_ASSERTION",
        "CAPTURE_RETURN_VALUE",
        "INSERT_STATEMENT",
        "REMOVE_ASSERTION",
    ],
}

# =============================================================================
# Operator schemas  (kept aligned with operators/catalog.py preconditions)
# =============================================================================

OPERATOR_SCHEMAS: Dict[str, str] = {
    "INSERT_ASSERTION": """INSERT_ASSERTION(after_line, assert_type, actual_expr, expected_expr?, message?)
  Inserts an assertion statement after the specified line.
  Params:
    after_line    : int    — 1-indexed line of the target method; must be a valid line
    assert_type   : string — one of [assertEquals, assertTrue, assertFalse, assertNotNull,
                               assertNull, assertSame, assertNotSame, assertArrayEquals, fail]
    actual_expr   : string — the expression to assert on; MUST reference a variable
                             already in scope (parameter/local/field). Empty only for fail.
    expected_expr : string — REQUIRED for assertEquals/assertSame/assertNotSame/assertArrayEquals
    message       : string — optional descriptive message (becomes the first arg)
  Example:
    {"op": "INSERT_ASSERTION",
     "params": {"after_line": 8, "assert_type": "assertEquals",
                "actual_expr": "obj.getCount()", "expected_expr": "1"}}""",

    "REMOVE_ASSERTION": """REMOVE_ASSERTION(target_line)
  Removes the assertion on the specified line.
  Precondition: that line is an assertion call AND the method has at least 2 assertions.
  Params:
    target_line : int — 1-indexed method line pointing at an assertion
  Example:
    {"op": "REMOVE_ASSERTION", "params": {"target_line": 5}}""",

    "REPLACE_ASSERTION": """REPLACE_ASSERTION(target_line, new_assert_type, new_actual_expr, new_expected_expr?, message?)
  Replaces one assertion with another on the same line (assertion count unchanged).
  Params:
    target_line      : int    — 1-indexed line that currently holds an assertion
    new_assert_type  : string — same enum as INSERT_ASSERTION
    new_actual_expr  : string — must reference a local variable
    new_expected_expr: string — required for *Equals/*Same variants
    message          : string — optional message
  Example:
    {"op": "REPLACE_ASSERTION",
     "params": {"target_line": 5, "new_assert_type": "assertEquals",
                "new_actual_expr": "foo.getValue()", "new_expected_expr": "42"}}""",

    "INSERT_STATEMENT": """INSERT_STATEMENT(after_line, statement)
  Inserts a raw statement after `after_line`.
  Precondition: `statement` is balanced (parens/braces) and ends with ';' or '}'.
                No JUnit 5 calls (assertThrows / assertDoesNotThrow / assertAll).
  Params:
    after_line : int    — 0 to N; 0 inserts at method top
    statement  : string — the full statement text, ending with ';'
  Example:
    {"op": "INSERT_STATEMENT",
     "params": {"after_line": 4, "statement": "foo.prepare();"}}""",

    "REMOVE_STATEMENT": """REMOVE_STATEMENT(target_line)
  Removes a single-line complete statement.
  Precondition: the line is NOT a brace, control-flow header (if/for/while/switch/try/
                catch/finally/else/do), or multi-line fragment. Parens must balance on-line.
  Params:
    target_line : int — 1-indexed line of the statement to delete
  Example:
    {"op": "REMOVE_STATEMENT", "params": {"target_line": 6}}""",

    "REPLACE_EXPRESSION": """REPLACE_EXPRESSION(target_line, old_expr, new_expr)
  Substitutes the first occurrence of `old_expr` with `new_expr` on `target_line`.
  Precondition: `old_expr` present verbatim on the line; new_expr != old_expr;
                new_expr must not introduce banned JUnit 5 calls.
  Params:
    target_line : int
    old_expr    : string — must appear on target_line
    new_expr    : string
  Example:
    {"op": "REPLACE_EXPRESSION",
     "params": {"target_line": 5, "old_expr": "foo.getValue()", "new_expr": "foo.getRealValue()"}}""",

    "CAPTURE_RETURN_VALUE": """CAPTURE_RETURN_VALUE(target_line, var_name, var_type)
  Converts `expr();` into `Type name = expr();` so the return value becomes asserted.
  Precondition: the line is a bare expression statement (no existing '='); var_type is
                NOT "void"; var_name is a fresh identifier.
  Params:
    target_line : int
    var_name    : string — must be a valid, unused Java identifier
    var_type    : string — a Java type (e.g., "int", "String", "List<Foo>"); NOT "void"
  Example:
    {"op": "CAPTURE_RETURN_VALUE",
     "params": {"target_line": 7, "var_name": "result", "var_type": "int"}}""",

    "REPLACE_NULL_ARG": """REPLACE_NULL_ARG(target_line, call_expr, arg_index, new_value)
  Replaces a `null` argument at `arg_index` in `call_expr(...)` with a concrete value.
  Precondition: `call_expr` appears on the line; the arg at `arg_index` is literally
                `null` (possibly with a cast like `(String) null`).
  Params:
    target_line : int
    call_expr   : string — e.g. "foo.setName"   (the callee, without trailing parens)
    arg_index   : int    — 0-based index of the null argument
    new_value   : string — the replacement expression (e.g. "\"abc\"", "0", "new Foo()")
  Example:
    {"op": "REPLACE_NULL_ARG",
     "params": {"target_line": 5, "call_expr": "parser.parse",
                "arg_index": 0, "new_value": "\\"<html/>\\""}}""",

    "ADD_SETUP_CALL": """ADD_SETUP_CALL(statement)
  Inserts a setup statement at the top of the method body (right after `{`).
  Precondition: statement is balanced and ends with ';' or '}'; no JUnit 5 calls.
  Params:
    statement : string — setup statement to prepend to the method body
  Example:
    {"op": "ADD_SETUP_CALL", "params": {"statement": "Foo.reset();"}}""",

    "TRY_CATCH_TO_EXPECTED": """TRY_CATCH_TO_EXPECTED(exception_type)
  Converts a simple try-catch pattern into @Test(expected=X.class).
  Precondition: method has exactly 1 try-catch, exactly 1 catch clause for a single type,
                try body contains fail(), catch body contains only verifyException or
                simple asserts; @Test annotation without existing `expected=`.
  Params:
    exception_type : string — simple class name (e.g. "NullPointerException", NOT fqn)
  Example:
    {"op": "TRY_CATCH_TO_EXPECTED", "params": {"exception_type": "IOException"}}""",

    "REMOVE_TRY_CATCH_KEEP_BODY": """REMOVE_TRY_CATCH_KEEP_BODY(try_begin_line, drop_fail_call?)
  Removes try/catch scaffolding, keeps the try body (optionally minus fail() calls).
  Precondition: a `try {` starts at `try_begin_line` (1-indexed).
  Params:
    try_begin_line  : int  — line where `try {` starts
    drop_fail_call  : bool — optional; default true. When true, removes fail(...) lines
                             from the kept body.
  Example:
    {"op": "REMOVE_TRY_CATCH_KEEP_BODY",
     "params": {"try_begin_line": 4, "drop_fail_call": true}}""",

    "WRAP_WITH_ASSERT_THROWS": """WRAP_WITH_ASSERT_THROWS(exception_type)
  JUnit 5 ONLY — DISABLED in this repository's JUnit 4 environment.
  This operator's precondition always fails; do not emit it except as a
  deliberately "no safe fix" signal (prefer returning [] instead).""",

    "ADD_TEST_EXPECTED": """ADD_TEST_EXPECTED(exception_type)
  Adds `expected = X.class` to the method's @Test annotation.
  Precondition: method has @Test annotation; `expected=` not already present.
  Params:
    exception_type : string — simple class name
  Example:
    {"op": "ADD_TEST_EXPECTED", "params": {"exception_type": "IllegalStateException"}}""",

    "REMOVE_TEST_EXPECTED": """REMOVE_TEST_EXPECTED()
  Removes the `expected = X.class` attribute from @Test, keeping other attrs.
  Precondition: @Test currently has `expected=`.
  Params: (none)
  Example:
    {"op": "REMOVE_TEST_EXPECTED", "params": {}}""",

    "EXTRACT_TO_BEFORE": """EXTRACT_TO_BEFORE(target_methods)
  FILE-scope: extracts a common prefix of `target_methods` into an @Before setUp().
  Precondition: at least 2 target methods, all share a line-identical prefix of >=2
                lines; no @Before already in the class.
  Params:
    target_methods : string[]
  Example:
    {"op": "EXTRACT_TO_BEFORE", "params": {"target_methods": ["test01","test02","test03"]}}""",
}


def operator_schemas_block(
    allowed: List[str],
    *,
    max_chars: int = 4000,
) -> str:
    parts: List[str] = []
    for op_id in allowed:
        s = OPERATOR_SCHEMAS.get(op_id)
        if not s:
            parts.append(f"- {op_id}: (no schema available)")
            continue
        parts.append(f"- {s}")
    block = "\n\n".join(parts)
    if max_chars and len(block) > max_chars:
        block = block[:max_chars].rstrip() + "\n... [truncated]"
    return block


# =============================================================================
# Limits & input dataclasses
# =============================================================================


@dataclass(frozen=True)
class PlanPromptLimits:
    # v1-era fields (still used by evidence.render_evidence_for_prompt)
    max_smell_guides_chars: int = 12000
    max_evidence_chars: int = 8000
    max_test_method_chars: int = 6000
    max_cut_context_chars: int = 8000
    max_compile_error_chars: int = 2000
    evidence_max_list_items: int = 6
    evidence_max_group_tests: int = 10
    evidence_max_prefix_stmts: int = 2
    evidence_max_str_len: int = 240

    # v2 additions
    max_fewshot_examples: int = 2
    # 8000 ≈ 2000 tokens; measured: Tier 2 ~5k, Tier 3 ~7k, Tier 4 ~2.4k chars.
    # 4000 (original suggestion) would truncate Tier 2 and Tier 3 mid-schema.
    max_operator_schemas_chars: int = 8000
    max_dynamic_evidence_chars: int = 3000
    max_previous_feedback_chars: int = 1500
    include_line_numbers: bool = True


@dataclass(frozen=True)
class PlanPromptInputs:
    """Everything build_plan_messages needs, packaged for easy override-in-retry
    via dataclasses.replace()."""

    smell_id: str
    tier: int                                   # 2, 3, or 4
    evidence: Dict[str, Any]                    # Smelly-E evidence (raw or compact)
    test_method_code: str                       # the method body verbatim
    cut_context: str                            # CUT signature / relevant source
    cut_fqcn: Optional[str] = None
    allowed_operators: List[str] = field(default_factory=list)
    operator_schemas: Dict[str, str] = field(default_factory=lambda: OPERATOR_SCHEMAS)
    fewshot_examples: List[Dict[str, Any]] = field(default_factory=list)
    smell_guide: Optional[str] = None           # optional long-form smell description
    dynamic_evidence: Optional[Dict[str, Any]] = None   # Tier 4 only
    previous_attempt_feedback: Optional[str] = None     # retry feedback
    limits: PlanPromptLimits = field(default_factory=PlanPromptLimits)


# =============================================================================
# build_plan_messages
# =============================================================================


def _truncate(text: str, max_chars: int) -> str:
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n... [truncated]"


def _number_lines(code: str) -> str:
    """Render code with line-number prefixes: ` 1 | ...`.

    Gutter width scales with line count; separator `|` stays stable for
    every line so the LLM can read line numbers robustly.
    """
    lines = code.splitlines() or [""]
    width = max(2, len(str(len(lines))))
    return "\n".join(f"{i:>{width}} | {ln}" for i, ln in enumerate(lines, start=1))


def _render_evidence(smell_id: str, evidence: Dict[str, Any], limits: PlanPromptLimits) -> str:
    if not evidence:
        return "(no evidence available)"
    er = render_evidence_for_prompt(
        smell_id,
        evidence,
        max_list_items=limits.evidence_max_list_items,
        max_group_tests=limits.evidence_max_group_tests,
        max_prefix_stmts=limits.evidence_max_prefix_stmts,
        max_str_len=limits.evidence_max_str_len,
    )
    return _truncate(evidence_block_markdown(er).strip(), limits.max_evidence_chars)


def _render_fewshot(
    examples: List[Dict[str, Any]],
    *,
    limit: int = 2,
    number_lines: bool = True,
) -> str:
    """Render few-shot examples. `test_method` is stored raw in the example
    dict and rendered with line-number prefixes here so the LLM sees the
    *same* format in the example and in the live task."""
    if not examples:
        return ""
    parts: List[str] = ["## Examples"]
    for idx, ex in enumerate(examples[: max(0, limit)], start=1):
        desc = ex.get("description", "").strip() or f"Example {idx}"
        parts.append(f"\n### Example {idx}: {desc}\n")
        if ex.get("evidence_summary"):
            parts.append(f"Evidence:\n{ex['evidence_summary']}\n")
        if ex.get("test_method"):
            body = ex["test_method"].rstrip()
            if number_lines:
                body = _number_lines(body)
            parts.append("Test method:\n```\n" + body + "\n```\n")
        if ex.get("cut_context"):
            parts.append("CUT context:\n```\n" + ex["cut_context"].rstrip() + "\n```\n")
        if ex.get("dynamic_evidence"):
            parts.append(
                "Dynamic evidence:\n```json\n"
                + json.dumps(ex["dynamic_evidence"], indent=2, ensure_ascii=False)
                + "\n```\n"
            )
        if "expected_plan" in ex:
            parts.append(
                "Expected plan:\n```json\n"
                + json.dumps(ex["expected_plan"], indent=2, ensure_ascii=False)
                + "\n```\n"
            )
        if ex.get("notes"):
            parts.append(f"Notes: {ex['notes']}\n")
        parts.append("---")
    return "\n".join(parts).rstrip("-\n ")


_TIER_HEADER = {
    2: "Tier 2 — template-guided repair. Fill in the parameters of well-known operator templates.",
    3: "Tier 3 — evidence-guided repair. Read the evidence and pick the minimal operator sequence.",
    4: "Tier 4 — dynamic-context repair. Use observed runtime state to produce concrete assertions.",
}


def build_plan_messages(inp: PlanPromptInputs) -> List[Dict[str, str]]:
    """Assemble system + user messages for the plan-mode LLM call."""
    limits = inp.limits

    # 1. Task header
    header = _TIER_HEADER.get(inp.tier, f"Tier {inp.tier} repair.")
    sections: List[str] = [
        f"# Repair task: smell={inp.smell_id}, tier={inp.tier}",
        header,
    ]

    # 2. Allowed operators + schemas
    allowed = inp.allowed_operators or TIER_ALLOWED_OPERATORS.get(inp.tier, [])
    sections.append("## Allowed operators for this task\n"
                    + operator_schemas_block(allowed, max_chars=limits.max_operator_schemas_chars))

    # 3. Test method (line-numbered)
    method_code = _truncate(inp.test_method_code, limits.max_test_method_chars)
    if limits.include_line_numbers:
        method_display = _number_lines(method_code)
    else:
        method_display = method_code
    sections.append(
        "## Target test method (line numbers shown on the left are what you use in params)\n"
        "```\n" + method_display.rstrip() + "\n```"
    )

    # 4. Smell evidence
    sections.append("## Smell evidence\n" + _render_evidence(inp.smell_id, inp.evidence, limits))

    # 5. Smell guide (optional long-form)
    if inp.smell_guide:
        sections.append("## Smell guide\n"
                        + _truncate(inp.smell_guide, limits.max_smell_guides_chars))

    # 6. CUT context
    cut_text = _truncate(inp.cut_context or "", limits.max_cut_context_chars)
    sections.append(
        f"## CUT context (class under test: {inp.cut_fqcn or 'UNKNOWN'})\n"
        "```java\n" + (cut_text or "(unavailable)") + "\n```"
    )

    # 7. Dynamic evidence (Tier 4)
    if inp.dynamic_evidence:
        dyn_json = json.dumps(inp.dynamic_evidence, indent=2, ensure_ascii=False)
        dyn_json = _truncate(dyn_json, limits.max_dynamic_evidence_chars)
        sections.append(
            "## Dynamic evidence (observed runtime state)\n"
            "This block shows the ACTUAL runtime state captured before and after "
            "the Act call. Use these values directly:\n"
            "- For each entry in `changed_fields`, emit one INSERT_ASSERTION that "
            "asserts the observed `after` value via the matching getter.\n"
            "- Use the observed value as the `expected_expr` literal verbatim — "
            "do NOT guess, compute, or paraphrase it.\n"
            "- Skip getters listed in `unchanged_fields` — those add no signal.\n"
            "- Skip observed values that are identity-hash-like "
            "(e.g. `Foo@1a2b3c`) because they are non-deterministic.\n"
            "```json\n" + dyn_json + "\n```"
        )

    # 8. Previous-attempt feedback
    if inp.previous_attempt_feedback:
        fb = _truncate(inp.previous_attempt_feedback, limits.max_previous_feedback_chars)
        sections.append("## Previous attempt failed — correct your output\n" + fb)

    # 9. Few-shot examples
    fewshot_block = _render_fewshot(inp.fewshot_examples, limit=limits.max_fewshot_examples)
    if fewshot_block:
        sections.append(fewshot_block)

    # 10. Output instruction (redundant with system, deliberately reinforced)
    sections.append(
        "## Output\nReturn ONLY a JSON array of operator invocations. "
        "No markdown, no commentary. If no safe transformation is possible, return []."
    )

    user = "\n\n".join(sections)
    return [
        {"role": "system", "content": SYSTEM_PROMPT_V2},
        {"role": "user", "content": user},
    ]


# =============================================================================
# LEGACY v1 API — DO NOT USE for new code.
# Retained for pipeline.py until Phase 2.4 integration.
# Scheduled for removal once pipeline.py migrates to build_plan_messages.
# =============================================================================

from smell_repair_v2.project.java_extract import ExtractedContext  # noqa: E402

_LEGACY_SYSTEM_PROMPT = """You are an expert Java developer.
You will be given a JUnit4 test method generated by EvoSuite and relevant production-code context.
Your task is to refactor/repair the test to reduce the provided Smelly test smells.

Hard rules:
- DO NOT delete the test method or test class.
- DO NOT add @Ignore or disable the test.
- Keep the test compiling.

Output format:
- Return ONLY the complete refactored test method code (including @Test annotation and method signature).
- Do NOT return a diff.
- Do NOT include markdown/code fences or any extra text.
"""

# Legacy alias — still the literal v1 prompt (whole-method-return style).
SYSTEM_PROMPT = _LEGACY_SYSTEM_PROMPT


@dataclass(frozen=True)
class PromptLimits:
    """Legacy v1 limits. New code should use PlanPromptLimits instead."""

    max_smell_guides_chars: int = 12000
    max_evidence_chars: int = 8000
    max_test_method_chars: int = 8000
    max_cut_context_chars: int = 12000
    max_compile_error_chars: int = 4000
    evidence_max_list_items: int = 6
    evidence_max_group_tests: int = 10
    evidence_max_prefix_stmts: int = 2
    evidence_max_str_len: int = 240


@dataclass(frozen=True)
class PromptInputs:
    """Legacy v1 prompt input — whole-method-rewrite mode."""

    smells: List[str]
    smell_guides: str
    smell_evidence: Dict[str, Any]
    allow_reflection_asserts: bool
    file_relpath: str
    ctx: ExtractedContext
    limits: Optional[PromptLimits] = None
    compile_error: Optional[str] = None
    prompt_variant: str = "full"


def load_smell_guides(smells_dir: Path, smell_ids: List[str]) -> str:
    """Legacy helper; unchanged from v1."""
    parts: List[str] = []
    for sid in smell_ids:
        p = smells_dir / f"{sid}.md"
        if p.exists():
            parts.append(p.read_text(encoding="utf-8", errors="ignore"))
    return "\n\n".join(parts)


def _legacy_truncate_section(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n... [truncated]"


def _build_full_user_prompt_legacy(inp: PromptInputs, limits: PromptLimits) -> str:
    limits = inp.limits or PromptLimits()
    allow_reflect = "ALLOWED" if inp.allow_reflection_asserts else "NOT ALLOWED"

    evidence_sections: List[str] = []
    for sid in inp.smells:
        ev = (inp.smell_evidence or {}).get(sid)
        if not ev:
            continue
        evidence_sections.append(
            evidence_block_markdown(
                render_evidence_for_prompt(
                    sid,
                    ev,
                    max_list_items=limits.evidence_max_list_items,
                    max_group_tests=limits.evidence_max_group_tests,
                    max_prefix_stmts=limits.evidence_max_prefix_stmts,
                    max_str_len=limits.evidence_max_str_len,
                )
            )
        )
    evidence_text = _legacy_truncate_section(
        "\n\n".join(evidence_sections).strip(), limits.max_evidence_chars
    )

    smell_guides = _legacy_truncate_section(inp.smell_guides, limits.max_smell_guides_chars)
    test_method_code = _legacy_truncate_section(inp.ctx.test_method_code, limits.max_test_method_chars)
    cut_context = _legacy_truncate_section(inp.ctx.cut_relevant_code, limits.max_cut_context_chars)

    user = f"""Smells to address: {', '.join(inp.smells)}
Reflection-based assertions: {allow_reflect}

Output requirement:
- Return ONLY the complete refactored method code for {inp.ctx.test_method_name}.
- Do NOT include any other text, diff, or code fences.

Smell guides:
{smell_guides}

=== Smelly evidence (extended JSON, compact & prioritized) ===
{evidence_text if evidence_text else '(no evidence provided)'}

=== Target Java file (relative path) ===
{inp.file_relpath}

=== Test class ===
{inp.ctx.test_class_name}

=== Test method (focus) ===
{inp.ctx.test_method_name}

=== Test method source ===
```java
{test_method_code}
```

=== Production code context (CUT + related methods, best-effort) ===
CUT: {inp.ctx.cut_fqcn or 'UNKNOWN'}
```java
{cut_context}
```
"""

    if inp.compile_error:
        compile_error = _legacy_truncate_section(inp.compile_error, limits.max_compile_error_chars)
        user += f"""

=== Previous attempt failed compilation/test ===
```
{compile_error}
```
Please fix the issues and re-output ONLY the complete refactored method code for {inp.ctx.test_method_name}.
"""
    return user


def _build_generic_user_prompt_legacy(inp: PromptInputs, limits: PromptLimits) -> str:
    test_method_code = _legacy_truncate_section(inp.ctx.test_method_code, limits.max_test_method_chars)
    cut_context = _legacy_truncate_section(inp.ctx.cut_relevant_code, limits.max_cut_context_chars)

    user = f"""Task:
Fix quality issues in this EvoSuite-generated JUnit4 test method.
Keep the method present and keep the file compiling.

Output requirement:
- Return ONLY the complete refactored method code for {inp.ctx.test_method_name}.
- Do NOT include any other text, diff, or code fences.

=== Target Java file (relative path) ===
{inp.file_relpath}

=== Test class ===
{inp.ctx.test_class_name}

=== Test method (focus) ===
{inp.ctx.test_method_name}

=== Test method source ===
```java
{test_method_code}
```

=== Production code context (CUT + related methods, best-effort) ===
CUT: {inp.ctx.cut_fqcn or 'UNKNOWN'}
```java
{cut_context}
```
"""

    if inp.compile_error:
        compile_error = _legacy_truncate_section(inp.compile_error, limits.max_compile_error_chars)
        user += f"""

=== Previous attempt failed compilation/test ===
```
{compile_error}
```
Please fix the issues and re-output ONLY the complete refactored method code for {inp.ctx.test_method_name}.
"""
    return user


# LEGACY v1 API — DO NOT USE for new code.
# Retained for pipeline.py until Phase 2.4 integration.
# Scheduled for removal once pipeline.py migrates to build_plan_messages.
def build_messages(inp: PromptInputs) -> List[Dict[str, str]]:
    """LEGACY v1 message builder (whole-method-return). Use build_plan_messages
    for new code. Scheduled for removal in Phase 2.4."""
    limits = inp.limits or PromptLimits()
    variant = (inp.prompt_variant or "full").strip().lower()
    if variant == "generic":
        user = _build_generic_user_prompt_legacy(inp, limits)
    else:
        user = _build_full_user_prompt_legacy(inp, limits)
    return [
        {"role": "system", "content": _LEGACY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]

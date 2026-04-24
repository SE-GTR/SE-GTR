"""Tier 1 deterministic handlers: create OperatorPlans straight from
Smelly-E evidence, without invoking the LLM.

Line numbers in evidence are **file-relative** (1-indexed). Handlers translate
them to **method-relative** line numbers using `ctx.method_line_range` before
emitting plans.

Simple-pattern gates let us fall through to Tier 2/3 when the method is not
a trivially deterministic case.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from smell_repair_v2.operators.base import (
    ExecutionContext,
    OperatorId,
    OperatorPlan,
)
from smell_repair_v2.operators.catalog import ASSERT_CALL_RE
from smell_repair_v2.project.java_extract import METHOD_START_RE


# ----------------------------------------------------------------------------
# NNA
# ----------------------------------------------------------------------------


def plan_nna(
    evidence: Dict[str, Any],
    method_text: str,
    ctx: ExecutionContext,
) -> List[OperatorPlan]:
    """Each redundant assertNotNull becomes a REMOVE_ASSERTION plan."""
    plans: List[OperatorPlan] = []
    method_start, _ = ctx.method_line_range
    for item in evidence.get("redundant_not_null_assertions", []):
        begin_line = item.get("begin_line")
        if not isinstance(begin_line, int):
            continue
        rel = begin_line - method_start + 1
        if rel < 1:
            continue
        plans.append(
            OperatorPlan(
                op=OperatorId.REMOVE_ASSERTION,
                params={"target_line": rel},
                smell_id="NNA",
            )
        )
    # Sort descending so that successive removals don't shift each other —
    # the executor's LineTracker would handle it, but descending ordering
    # keeps the plan robust even against a naive executor.
    plans.sort(key=lambda p: p.params["target_line"], reverse=True)
    return plans


# ----------------------------------------------------------------------------
# DS  (file scope)
# ----------------------------------------------------------------------------


_TEST_METHOD_BLOCK_RE = re.compile(
    r"(?ms)(?:@Test[^\n]*\n\s*)*(?:public\s+)?void\s+(?P<name>test\w+)\s*"
    r"\([^\)]*\)\s*(?:throws[^\{]+)?\{(?P<body>.*?)^\s*\}",
)


def _method_body_lines(file_text: str, method_name: str) -> Optional[List[str]]:
    for m in _TEST_METHOD_BLOCK_RE.finditer(file_text):
        if m.group("name") != method_name:
            continue
        return [ln for ln in m.group("body").splitlines() if ln.strip()]
    return None


def verify_common_prefix_in_file(
    file_text: str, methods: List[str], min_common_lines: int = 2
) -> bool:
    """Return True iff `methods` all share at least `min_common_lines` of
    verbatim leading non-empty lines in their bodies, with the same early-stop
    rules used by `EXTRACT_TO_BEFORE` (halt on assert / try / fail / closing
    brace). This must stay consistent with `apply_extract_to_before` in
    `operators/catalog.py`; otherwise a plan that passes precondition will
    fail postcondition.
    """
    bodies: List[List[str]] = []
    for nm in methods:
        lines = _method_body_lines(file_text, nm)
        if lines is None:
            return False
        bodies.append(lines)
    if len(bodies) < 2:
        return False
    min_len = min(len(b) for b in bodies)
    if min_len < min_common_lines:
        return False
    first = bodies[0]
    count = 0
    for i in range(min_len):
        line = first[i]
        if not all(b[i] == line for b in bodies[1:]):
            break
        stripped = line.strip()
        if (
            "assert" in stripped
            or stripped.startswith("try")
            or stripped.startswith("fail(")
            or stripped.startswith("}")
        ):
            break
        count += 1
    return count >= min_common_lines


def plan_ds(
    evidence: Dict[str, Any],
    file_text: str,
    ctx: ExecutionContext,
) -> List[OperatorPlan]:
    """DS Tier 1 handler — common-prefix-verified variant.

    Smelly-E groups tests whose setup is similar under some equivalence notion
    that is NOT the same as "identical leading lines". On the first E2E run
    (`1_tullibee`) four DS plans passed Tier 1 precondition but failed the
    operator's postcondition ("line count did not grow") because the grouped
    methods did not actually share a line-identical prefix. This variant
    verifies prefix sharing up-front so those cases are silently deferred
    (not counted as rejections) and leaves them for Tier 2 in Phase 2.
    """
    groups = evidence.get("duplicated_setup_groups", [])
    if not groups:
        return []
    largest = max(groups, key=lambda g: g.get("group_size", 0))
    tests = largest.get("group_tests", [])
    if len(tests) < 2:
        return []
    if "@Before" in file_text or "org.junit.Before" in file_text:
        return []
    if not verify_common_prefix_in_file(file_text, list(tests)):
        return []
    return [
        OperatorPlan(
            op=OperatorId.EXTRACT_TO_BEFORE,
            params={"target_methods": list(tests)},
            smell_id="DS",
        )
    ]


# ----------------------------------------------------------------------------
# TSES  (simple pattern → TRY_CATCH_TO_EXPECTED; else None)
# ----------------------------------------------------------------------------


_TRY_RE = re.compile(r"\btry\s*\{")
_CATCH_RE = re.compile(r"\bcatch\s*\(([^)]*)\)\s*\{")
_FAIL_RE = re.compile(r"\bfail\s*\(")


def _match_brace(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    in_s = False
    in_d = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_s:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == "'":
                in_s = False
        elif in_d:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == '"':
                in_d = False
        else:
            if ch == "/" and nxt == "/":
                while i < len(text) and text[i] != "\n":
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                i += 2
                while i < len(text) - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            if ch == "'":
                in_s = True
            elif ch == '"':
                in_d = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def is_simple_try_catch_pattern(method_text: str) -> bool:
    tries = list(_TRY_RE.finditer(method_text))
    catches = list(_CATCH_RE.finditer(method_text))
    if len(tries) != 1 or len(catches) != 1:
        return False
    decl = catches[0].group(1).strip()
    if "|" in decl:
        return False
    parts = decl.split()
    if len(parts) < 2:
        return False
    # try body must contain fail()
    try_open = method_text.find("{", tries[0].end() - 1)
    if try_open < 0:
        return False
    try_close = _match_brace(method_text, try_open)
    if try_close < 0:
        return False
    try_body = method_text[try_open + 1 : try_close]
    if not _FAIL_RE.search(try_body):
        return False
    # catch body: only verifyException / asserts / comments / blanks
    catch_open = method_text.find("{", catches[0].end() - 1)
    if catch_open < 0:
        return False
    catch_close = _match_brace(method_text, catch_open)
    if catch_close < 0:
        return False
    catch_body = method_text[catch_open + 1 : catch_close]
    for raw in catch_body.splitlines():
        s = raw.strip()
        if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        if s.startswith("verifyException") or s.startswith("assertEquals") or s.startswith("assertTrue"):
            continue
        return False
    return True


_JAVA_MODIFIERS = {
    "public", "protected", "private", "static", "final",
    "synchronized", "native", "abstract", "default",
}


def get_method_return_type(cut_src: str, method_name: str) -> Optional[str]:
    """Parse CUT source (regex-based) and return the declared return type of
    `method_name`, or None if unknown/ambiguous.

    Handles simple cases: `public void foo()`, `protected String bar()`,
    `static final Map<K,V> baz(...)`. Conservative: if multiple overloads
    exist with different return types, returns None.
    """
    found_types: set[str] = set()
    for m in METHOD_START_RE.finditer(cut_src):
        if m.group("name") != method_name:
            continue
        sig_text = cut_src[m.start() : m.end()]
        # locate method-name inside sig_text (rightmost occurrence before '(')
        paren_idx = sig_text.rfind("(")
        name_idx = sig_text.rfind(method_name, 0, paren_idx)
        if name_idx < 0:
            continue
        prefix = sig_text[:name_idx]
        # strip leading annotations
        prefix = re.sub(r"@[A-Za-z_][\w\.]*(?:\([^)]*\))?\s*", "", prefix)
        # tokenize, strip modifiers
        tokens = [t for t in re.split(r"\s+", prefix.strip()) if t]
        while tokens and tokens[0] in _JAVA_MODIFIERS:
            tokens.pop(0)
        # skip generics specifier like <T>
        if tokens and tokens[0].startswith("<"):
            tokens.pop(0)
        if not tokens:
            continue
        rtype = " ".join(tokens).strip()
        # strip any trailing whitespace
        if rtype:
            # remove generics nesting spaces: "Map<K, V>" → keep as-is
            found_types.add(rtype)
    if len(found_types) == 1:
        return next(iter(found_types))
    return None


_STMT_SPLIT_RE = re.compile(r";\s*")


def _split_statements(block: str) -> List[str]:
    """Split a brace-stripped block into trimmed statements (no trailing `;`)."""
    # strip line comments
    stripped = re.sub(r"//[^\n]*", "", block)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    out: List[str] = []
    for raw in _STMT_SPLIT_RE.split(stripped):
        s = raw.strip()
        if s:
            out.append(s)
    return out


_BARE_METHOD_CALL_RE = re.compile(
    r"^(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*)\s*\("
)
_BARE_FUNCTION_CALL_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(")
_NEW_CALL_RE = re.compile(r"^new\s+[A-Za-z_][\w\.]*\s*[\(<]")
# assignment detector: '=' that isn't '==', '!=', '<=', '>='
_ASSIGN_RE = re.compile(r"(?<![=!<>])=(?!=)")


def _last_effective_statement(try_body: str) -> Optional[str]:
    """Return the last statement in `try_body` that is not `fail(...)` or
    `verifyException(...)` (and not a blank line / comment-only)."""
    for s in reversed(_split_statements(try_body)):
        if re.match(r"^fail\s*\(", s):
            continue
        if re.match(r"^verifyException\s*\(", s):
            continue
        return s
    return None


def _extract_try_body(method_text: str) -> Optional[str]:
    """Returns the text between `try {` and its matching `}`, or None."""
    m = _TRY_RE.search(method_text)
    if not m:
        return None
    brace_idx = method_text.find("{", m.end() - 1)
    if brace_idx < 0:
        return None
    close_idx = _match_brace(method_text, brace_idx)
    if close_idx < 0:
        return None
    return method_text[brace_idx + 1 : close_idx]


def is_last_call_void(try_body: str, ctx: ExecutionContext) -> bool:
    """Return True iff the last effective statement in `try_body` is "safe to
    leave naked" after `TRY_CATCH_TO_EXPECTED` drops the surrounding
    try/catch. Safe means it would NOT trip Smelly-E's NARV detector:

      - an assignment (return value captured into a variable)
      - a constructor call `new X(...)` (no return-value NARV hazard)
      - a method call whose declared return type is `void`

    Conservative: if CUT source is unavailable or the return type cannot be
    resolved, returns False so the plan falls through to Tier 2.
    """
    last = _last_effective_statement(try_body)
    if not last:
        return False

    # case 1: assignment ("Type var = ..." or "var = ...") — captures the return
    if _ASSIGN_RE.search(last):
        return True

    # case 2: `new X(...)` — no return-value NARV concern
    if _NEW_CALL_RE.match(last):
        return True

    # case 3: `target.method(...)` — look up method return type from CUT source
    m = _BARE_METHOD_CALL_RE.match(last)
    if m:
        method_name = m.group(1)
        if not ctx.cut_source:
            return False
        rtype = get_method_return_type(ctx.cut_source, method_name)
        if rtype is None:
            return False
        return rtype == "void"

    # case 4: unqualified `method(...)` (inherited / same-class) — same lookup
    m = _BARE_FUNCTION_CALL_RE.match(last)
    if m:
        method_name = m.group(1)
        if not ctx.cut_source:
            return False
        rtype = get_method_return_type(ctx.cut_source, method_name)
        if rtype is None:
            return False
        return rtype == "void"

    return False


def plan_tses_simple(
    evidence: Dict[str, Any],
    method_text: str,
    ctx: ExecutionContext,
) -> Optional[List[OperatorPlan]]:
    """TSES Tier 1 handler — void-only variant.

    Accepts only cases satisfying all of:
      1. Method contains exactly one try-catch block.
      2. The catch clause declares a single exception type (no `|` unions).
      3. The try body contains a `fail(...)` call.
      4. The catch body contains only `verifyException(...)` or simple
         assertion calls (no branching, no extra logic).
      5. The last effective try-body statement is "NARV-safe" — either an
         assignment, a `new X(...)` expression, or a method call whose
         declared return type is `void`.

    Rule 5 exists because `TRY_CATCH_TO_EXPECTED` drops the try/catch and the
    `fail(...)` call, leaving the penultimate expression as a naked statement.
    If that expression is a non-void method call, Smelly-E flags it as
    NARV — the first E2E run on `1_tullibee` observed +14 NARV introductions
    from this exact pattern. Cases failing rule 5 are deferred to Tier 2
    (LLM-driven) in Phase 2.
    """
    groups = evidence.get("same_exception_scenario_groups", [])
    if not groups:
        return None
    if not is_simple_try_catch_pattern(method_text):
        return None

    try_body = _extract_try_body(method_text)
    if try_body is None:
        return None
    if not is_last_call_void(try_body, ctx):
        return None  # defer to Tier 2

    exc_type = groups[0].get("exception_type", "Exception")
    exc_short = exc_type.rsplit(".", 1)[-1]
    if not re.fullmatch(r"[A-Za-z_][\w]*", exc_short):
        return None
    return [
        OperatorPlan(
            op=OperatorId.TRY_CATCH_TO_EXPECTED,
            params={"exception_type": exc_short},
            smell_id="TSES",
        )
    ]


# ----------------------------------------------------------------------------
# AC  (simple pattern → REMOVE_ASSERTION; else None)
# ----------------------------------------------------------------------------


def _count_assertions_in_text(text: str) -> int:
    return len(ASSERT_CALL_RE.findall(text))


def _is_unrelated_constant(item: Dict[str, Any], ctx: ExecutionContext) -> bool:
    """Constant is considered CUT-unrelated if its FQN does not reference the
    CUT class. Conservative: if we can't decide, return False to stay safe."""
    const = item.get("constant", "")
    if not const:
        return False
    cut_fqcn = ctx.cut_fqcn
    if cut_fqcn and const.startswith(cut_fqcn + "."):
        return False
    if cut_fqcn:
        cut_simple = cut_fqcn.rsplit(".", 1)[-1]
        if f".{cut_simple}." in const or const.startswith(cut_simple + "."):
            return False
    return True


def plan_ac_simple(
    evidence: Dict[str, Any],
    method_text: str,
    ctx: ExecutionContext,
) -> Optional[List[OperatorPlan]]:
    items = evidence.get("constant_assertions", [])
    if not items:
        return None
    remaining = _count_assertions_in_text(method_text)
    method_start, _ = ctx.method_line_range
    plans: List[OperatorPlan] = []
    for item in items:
        if not _is_unrelated_constant(item, ctx):
            continue
        begin = item.get("begin_line")
        if not isinstance(begin, int):
            continue
        rel = begin - method_start + 1
        if rel < 1:
            continue
        if remaining <= 1:
            break  # can't remove the last assertion
        remaining -= 1
        plans.append(
            OperatorPlan(
                op=OperatorId.REMOVE_ASSERTION,
                params={"target_line": rel},
                smell_id="AC",
            )
        )
    if not plans:
        return None
    plans.sort(key=lambda p: p.params["target_line"], reverse=True)
    return plans


# ----------------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------------


def get_tier1_plan(
    smell_id: str,
    evidence: Dict[str, Any],
    *,
    method_text: str,
    file_text: str,
    ctx: ExecutionContext,
) -> Optional[List[OperatorPlan]]:
    """Return a plan list for Tier 1 smells, or None if the smell is not
    Tier 1 or does not match a simple pattern.

    Empty list means "Tier 1 matched but no actionable items" — handled
    separately from None by the pipeline.
    """
    if smell_id == "NNA":
        return plan_nna(evidence, method_text, ctx)
    if smell_id == "DS":
        return plan_ds(evidence, file_text, ctx)
    if smell_id == "TSES":
        return plan_tses_simple(evidence, method_text, ctx)
    if smell_id == "AC":
        return plan_ac_simple(evidence, method_text, ctx)
    return None

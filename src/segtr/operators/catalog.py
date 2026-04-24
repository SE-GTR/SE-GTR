"""Atomic operator catalog for SE-GTR v2.

Each operator has three phases: precondition check, apply, postcondition check.
Operators with scope=METHOD work on a method body text. EXTRACT_TO_BEFORE has
scope=FILE and works on the whole file text.

Line numbers in `params` are 1-indexed and relative to the text passed to the
operator (method_text for METHOD-scope, file_text for FILE-scope).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .base import ExecutionContext, OperatorId, OperatorPlan, OperatorScope

# ----------------------------------------------------------------------------
# Shared regex / helpers
# ----------------------------------------------------------------------------

VALID_ASSERT_TYPES: Set[str] = {
    "assertEquals",
    "assertTrue",
    "assertFalse",
    "assertNotNull",
    "assertNull",
    "assertSame",
    "assertNotSame",
    "assertArrayEquals",
    "fail",
}

ASSERT_CALL_RE = re.compile(
    r"\b(?:assertEquals|assertTrue|assertFalse|assertNotNull|assertNull"
    r"|assertSame|assertNotSame|assertArrayEquals|fail)\s*\("
)

ASSERT_LINE_RE = re.compile(
    r"^\s*(?:assertEquals|assertTrue|assertFalse|assertNotNull|assertNull"
    r"|assertSame|assertNotSame|assertArrayEquals|fail)\s*\("
)

IDENT_RE = re.compile(r"[A-Za-z_]\w*")

TEST_ANNOTATION_RE = re.compile(
    r"(?P<full>@Test\b(?:\s*\((?P<args>[^)]*)\))?)"
)


def count_assertions(text: str) -> int:
    return len(ASSERT_CALL_RE.findall(text))


def indent_of(line: str) -> str:
    m = re.match(r"(\s*)", line)
    return m.group(1) if m else "    "


def is_comment_or_blank(line: str) -> bool:
    s = line.strip()
    return (not s) or s.startswith("//") or s.startswith("/*") or s.startswith("*")


def method_local_identifiers(method_text: str) -> Set[str]:
    """Collect plausible variable/parameter names defined in a method body.

    Best-effort: picks up `T name = ...`, `T name;`, and single-param patterns.
    """
    names: Set[str] = set()
    decl_re = re.compile(
        r"^\s*(?:final\s+)?[A-Za-z_][\w\.<>,\[\]]*\s+(?P<var>[A-Za-z_]\w*)\s*(?:=|;)"
    )
    for line in method_text.splitlines():
        m = decl_re.match(line)
        if m:
            names.add(m.group("var"))
    # method parameters (first line of method_text commonly has signature)
    sig_m = re.search(r"\([^)]*\)", method_text)
    if sig_m:
        for p in sig_m.group(0).strip("()").split(","):
            p = p.strip()
            if not p:
                continue
            parts = p.split()
            if parts:
                names.add(parts[-1].strip("[]"))
    return names


def expression_references_locals(expr: str, locals_: Set[str]) -> bool:
    """Return True iff expr references at least one known identifier or a literal is ok.

    We accept expressions that are literals (e.g. "0", "\"abc\"", "true"),
    or that reference at least one known local, or that are clearly qualified
    class accesses (e.g. Foo.BAR, new Foo()).
    """
    s = expr.strip()
    if not s:
        return False
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?[dDfFlL]?", s):
        return True
    if s in {"true", "false", "null"}:
        return True
    if s.startswith('"') and s.endswith('"'):
        return True
    if s.startswith("'") and s.endswith("'"):
        return True
    if s.startswith("new "):
        return True
    for m in IDENT_RE.finditer(s):
        if m.group(0) in locals_:
            return True
    # qualified access: Foo.BAR or Foo.method(...)
    if re.match(r"^[A-Z]\w*(?:\.\w+)+", s):
        return True
    # cast / parenthesised expression
    if s.startswith("("):
        return True
    return False


# ----------------------------------------------------------------------------
# 1. INSERT_ASSERTION
# ----------------------------------------------------------------------------

_NEEDS_EXPECTED = {"assertEquals", "assertSame", "assertNotSame", "assertArrayEquals"}


def pre_insert_assertion(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    params = plan.params
    after_line = params.get("after_line")
    assert_type = params.get("assert_type")
    actual_expr = params.get("actual_expr", "")
    expected_expr = params.get("expected_expr")

    lines = text.splitlines()
    if not isinstance(after_line, int) or not (1 <= after_line <= len(lines)):
        return False, f"after_line {after_line} out of range [1, {len(lines)}]"

    if assert_type not in VALID_ASSERT_TYPES:
        return False, f"invalid assert_type: {assert_type}"

    if assert_type != "fail" and not str(actual_expr).strip():
        return False, "actual_expr is empty"

    if assert_type in _NEEDS_EXPECTED:
        if expected_expr is None or not str(expected_expr).strip():
            return False, f"{assert_type} requires expected_expr"

    locals_ = method_local_identifiers(text)
    if assert_type != "fail":
        if not expression_references_locals(str(actual_expr), locals_):
            return False, f"actual_expr '{actual_expr}' does not reference any local"

    return True, ""


def _render_assert(
    assert_type: str,
    actual_expr: str,
    expected_expr: Optional[str],
    message: Optional[str],
) -> str:
    msg = f'"{message}", ' if message else ""
    if assert_type == "assertEquals":
        return f"assertEquals({msg}{expected_expr}, {actual_expr});"
    if assert_type in {"assertTrue", "assertFalse", "assertNotNull", "assertNull"}:
        return f"{assert_type}({msg}{actual_expr});"
    if assert_type in {"assertSame", "assertNotSame"}:
        return f"{assert_type}({msg}{expected_expr}, {actual_expr});"
    if assert_type == "assertArrayEquals":
        return f"assertArrayEquals({msg}{expected_expr}, {actual_expr});"
    if assert_type == "fail":
        return f'fail({message!r});' if message else "fail();"
    raise ValueError(f"Unknown assert_type: {assert_type}")


def apply_insert_assertion(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    p = plan.params
    lines = text.splitlines()
    after = p["after_line"]
    indent = indent_of(lines[after - 1])
    if not indent:
        indent = "    "
    stmt_body = _render_assert(
        p["assert_type"], p.get("actual_expr", ""), p.get("expected_expr"), p.get("message")
    )
    stmt = f"{indent}{stmt_body}"
    new_lines = lines[:after] + [stmt] + lines[after:]
    return "\n".join(new_lines)


def post_insert_assertion(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    if count_assertions(new_text) != count_assertions(old_text) + 1:
        return False, "assertion count did not increase by 1"
    return True, ""


# ----------------------------------------------------------------------------
# 2. REMOVE_ASSERTION
# ----------------------------------------------------------------------------


def pre_remove_assertion(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    target = plan.params.get("target_line")
    if not isinstance(target, int) or not (1 <= target <= len(lines)):
        return False, f"target_line {target} out of range [1, {len(lines)}]"
    if not ASSERT_LINE_RE.match(lines[target - 1]):
        return False, f"line {target} is not an assertion"
    if count_assertions(text) <= 1:
        return False, "only 1 assertion left; refusing to leave method empty"
    return True, ""


def apply_remove_assertion(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    target = plan.params["target_line"]
    del lines[target - 1]
    return "\n".join(lines)


def post_remove_assertion(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    new_count = count_assertions(new_text)
    if new_count != count_assertions(old_text) - 1:
        return False, "assertion count did not decrease by 1"
    if new_count < 1:
        return False, "no assertion remains after removal"
    return True, ""


# ----------------------------------------------------------------------------
# 3. REPLACE_ASSERTION
# ----------------------------------------------------------------------------


def pre_replace_assertion(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    target = plan.params.get("target_line")
    if not isinstance(target, int) or not (1 <= target <= len(lines)):
        return False, f"target_line {target} out of range [1, {len(lines)}]"
    if not ASSERT_LINE_RE.match(lines[target - 1]):
        return False, f"line {target} is not an assertion"

    new_type = plan.params.get("new_assert_type") or plan.params.get("assert_type")
    if new_type not in VALID_ASSERT_TYPES:
        return False, f"invalid new_assert_type: {new_type}"
    new_actual = plan.params.get("new_actual_expr") or plan.params.get("actual_expr", "")
    new_expected = plan.params.get("new_expected_expr") or plan.params.get("expected_expr")
    if new_type != "fail" and not str(new_actual).strip():
        return False, "new_actual_expr is empty"
    if new_type in _NEEDS_EXPECTED:
        if new_expected is None or not str(new_expected).strip():
            return False, f"{new_type} requires new_expected_expr"

    locals_ = method_local_identifiers(text)
    if new_type != "fail" and not expression_references_locals(str(new_actual), locals_):
        return False, f"new_actual_expr '{new_actual}' does not reference any local"
    return True, ""


def apply_replace_assertion(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    target = plan.params["target_line"]
    indent = indent_of(lines[target - 1]) or "    "
    new_type = plan.params.get("new_assert_type") or plan.params.get("assert_type")
    new_actual = plan.params.get("new_actual_expr") or plan.params.get("actual_expr", "")
    new_expected = plan.params.get("new_expected_expr") or plan.params.get("expected_expr")
    message = plan.params.get("message")
    stmt = f"{indent}{_render_assert(new_type, str(new_actual), new_expected, message)}"
    lines[target - 1] = stmt
    return "\n".join(lines)


def post_replace_assertion(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    if count_assertions(new_text) != count_assertions(old_text):
        return False, "assertion count must be unchanged"
    return True, ""


# ----------------------------------------------------------------------------
# 4. INSERT_STATEMENT
# ----------------------------------------------------------------------------


def _looks_like_statement(stmt: str) -> bool:
    s = stmt.strip()
    if not s:
        return False
    if s.count("{") != s.count("}"):
        return False
    if s.count("(") != s.count(")"):
        return False
    return s.endswith(";") or s.endswith("}")


def pre_insert_statement(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    after = plan.params.get("after_line")
    if not isinstance(after, int) or not (0 <= after <= len(lines)):
        return False, f"after_line {after} out of range [0, {len(lines)}]"
    stmt = plan.params.get("statement")
    if not isinstance(stmt, str) or not _looks_like_statement(stmt):
        return False, "statement is missing or ill-formed"
    # Reject banned calls at the source
    for banned in ("assertThrows", "assertDoesNotThrow", "assertAll"):
        if re.search(rf"\b{banned}\s*\(", stmt):
            return False, f"statement contains banned JUnit5 call: {banned}"
    return True, ""


def apply_insert_statement(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    after = plan.params["after_line"]
    # Figure out indent from a nearby code line
    ref_line = ""
    if 1 <= after <= len(lines):
        ref_line = lines[after - 1]
    elif lines:
        ref_line = next((ln for ln in lines if ln.strip()), "")
    indent = indent_of(ref_line) or "    "
    stmt = plan.params["statement"].strip()
    rendered = "\n".join(indent + ln if ln.strip() else ln for ln in stmt.splitlines())
    new_lines = lines[:after] + rendered.splitlines() + lines[after:]
    return "\n".join(new_lines)


def post_insert_statement(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    stmt = plan.params["statement"].strip()
    if stmt not in new_text:
        return False, "statement text not found in result"
    return True, ""


# ----------------------------------------------------------------------------
# 5. REMOVE_STATEMENT
# ----------------------------------------------------------------------------


def pre_remove_statement(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    target = plan.params.get("target_line")
    if not isinstance(target, int) or not (1 <= target <= len(lines)):
        return False, f"target_line {target} out of range [1, {len(lines)}]"
    line = lines[target - 1]
    stripped = line.strip()
    if not stripped:
        return False, "line is blank"
    if stripped.startswith("//"):
        return False, "line is a comment"
    # Refuse to remove multi-line statements, control-flow heads, or braces
    if stripped in {"{", "}"}:
        return False, "line is a lone brace"
    if re.match(r"^\s*(if|for|while|switch|try|catch|finally|else|do|\})\b", line):
        return False, "line is a control-flow construct"
    if not (stripped.endswith(";") or stripped.endswith("}")):
        return False, "line is not a complete single-line statement"
    if stripped.count("(") != stripped.count(")"):
        return False, "unbalanced parentheses on line"
    return True, ""


def apply_remove_statement(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    target = plan.params["target_line"]
    del lines[target - 1]
    return "\n".join(lines)


def post_remove_statement(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    if len(new_text.splitlines()) != len(old_text.splitlines()) - 1:
        return False, "line count did not decrease by 1"
    return True, ""


# ----------------------------------------------------------------------------
# 6. REPLACE_EXPRESSION
# ----------------------------------------------------------------------------


def pre_replace_expression(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    target = plan.params.get("target_line")
    if not isinstance(target, int) or not (1 <= target <= len(lines)):
        return False, f"target_line {target} out of range [1, {len(lines)}]"
    old_expr = plan.params.get("old_expr")
    new_expr = plan.params.get("new_expr")
    if not isinstance(old_expr, str) or not old_expr:
        return False, "old_expr empty"
    if not isinstance(new_expr, str):
        return False, "new_expr missing"
    if old_expr not in lines[target - 1]:
        return False, f"old_expr not present on line {target}"
    if old_expr == new_expr:
        return False, "old_expr equals new_expr (no-op)"
    # reject replacements that bring in banned JUnit5 calls
    for banned in ("assertThrows", "assertDoesNotThrow", "assertAll"):
        if re.search(rf"\b{banned}\s*\(", new_expr):
            return False, f"new_expr introduces banned call: {banned}"
    return True, ""


def apply_replace_expression(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    target = plan.params["target_line"]
    old_expr = plan.params["old_expr"]
    new_expr = plan.params["new_expr"]
    lines[target - 1] = lines[target - 1].replace(old_expr, new_expr, 1)
    return "\n".join(lines)


def post_replace_expression(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    if old_text == new_text:
        return False, "no change"
    return True, ""


# ----------------------------------------------------------------------------
# 7. CAPTURE_RETURN_VALUE
# ----------------------------------------------------------------------------

# Matches a bare expression-statement: "<expr>;" (no assignment, no declaration).
_BARE_CALL_RE = re.compile(
    r"^(?P<indent>\s*)(?P<expr>[^=;\{\}][^;]*)\s*;\s*$"
)


def _line_has_assignment(line: str) -> bool:
    s = line.strip()
    # crude: '=' not part of ==, !=, >=, <=
    return bool(re.search(r"(?<![=!<>])=(?!=)", s))


def pre_capture_return_value(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    target = plan.params.get("target_line")
    if not isinstance(target, int) or not (1 <= target <= len(lines)):
        return False, f"target_line {target} out of range [1, {len(lines)}]"
    var_name = plan.params.get("var_name")
    var_type = plan.params.get("var_type")
    if not isinstance(var_name, str) or not re.fullmatch(r"[A-Za-z_]\w*", var_name):
        return False, f"invalid var_name: {var_name!r}"
    if not isinstance(var_type, str) or not var_type.strip():
        return False, "var_type missing"
    if var_type.strip() == "void":
        return False, "cannot capture void return"
    line = lines[target - 1]
    if _line_has_assignment(line):
        return False, "line already contains assignment"
    m = _BARE_CALL_RE.match(line)
    if not m:
        return False, "line is not a bare expression-statement"
    if var_name in method_local_identifiers(text):
        return False, f"variable {var_name} already declared"
    return True, ""


def apply_capture_return_value(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    target = plan.params["target_line"]
    var_name = plan.params["var_name"]
    var_type = plan.params["var_type"].strip()
    m = _BARE_CALL_RE.match(lines[target - 1])
    assert m is not None  # precondition ensures
    indent = m.group("indent")
    expr = m.group("expr").strip()
    lines[target - 1] = f"{indent}{var_type} {var_name} = {expr};"
    return "\n".join(lines)


def post_capture_return_value(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    var_name = plan.params["var_name"]
    if var_name not in new_text:
        return False, "variable not present in result"
    if new_text == old_text:
        return False, "no change"
    return True, ""


# ----------------------------------------------------------------------------
# 8. REPLACE_NULL_ARG
# ----------------------------------------------------------------------------


def _split_top_level_args(arglist: str) -> List[str]:
    """Split a call's argument list respecting nested parens/brackets/strings."""
    out: List[str] = []
    depth = 0
    buf: List[str] = []
    in_s = False
    in_d = False
    i = 0
    while i < len(arglist):
        ch = arglist[i]
        if in_s:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(arglist):
                buf.append(arglist[i + 1])
                i += 2
                continue
            if ch == "'":
                in_s = False
        elif in_d:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(arglist):
                buf.append(arglist[i + 1])
                i += 2
                continue
            if ch == '"':
                in_d = False
        else:
            if ch == "'":
                in_s = True
                buf.append(ch)
            elif ch == '"':
                in_d = True
                buf.append(ch)
            elif ch in "([{<":
                depth += 1
                buf.append(ch)
            elif ch in ")]}>":
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0:
                out.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return [a.strip() for a in out]


def _find_call_in_line(line: str, call_expr: str) -> Optional[Tuple[int, int, int]]:
    """Locate `call_expr(` in `line`. Returns (start, paren_open, paren_close)
    indices, where paren_close is matched to paren_open. None if not found."""
    # find name+'(' — use regex anchored to identifier boundary
    needle = re.escape(call_expr)
    m = re.search(needle + r"\s*\(", line)
    if not m:
        return None
    start = m.start()
    po = line.find("(", m.end() - 1)
    if po < 0:
        return None
    depth = 0
    in_s = False
    in_d = False
    i = po
    while i < len(line):
        ch = line[i]
        if in_s:
            if ch == "\\" and i + 1 < len(line):
                i += 2
                continue
            if ch == "'":
                in_s = False
        elif in_d:
            if ch == "\\" and i + 1 < len(line):
                i += 2
                continue
            if ch == '"':
                in_d = False
        else:
            if ch == "'":
                in_s = True
            elif ch == '"':
                in_d = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return start, po, i
        i += 1
    return None


def pre_replace_null_arg(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    target = plan.params.get("target_line")
    if not isinstance(target, int) or not (1 <= target <= len(lines)):
        return False, f"target_line {target} out of range [1, {len(lines)}]"
    call_expr = plan.params.get("call_expr")
    arg_index = plan.params.get("arg_index")
    new_value = plan.params.get("new_value")
    if not isinstance(call_expr, str) or not call_expr:
        return False, "call_expr missing"
    if not isinstance(arg_index, int) or arg_index < 0:
        return False, f"arg_index must be non-negative int, got {arg_index}"
    if not isinstance(new_value, str) or not new_value.strip():
        return False, "new_value missing"
    loc = _find_call_in_line(lines[target - 1], call_expr)
    if loc is None:
        return False, f"call_expr '{call_expr}' not found on line {target}"
    _, po, pc = loc
    args = _split_top_level_args(lines[target - 1][po + 1 : pc])
    if arg_index >= len(args):
        return False, f"arg_index {arg_index} >= arg count {len(args)}"
    target_arg = args[arg_index]
    # require current arg be null (possibly with (Type) cast)
    if not re.fullmatch(r"(?:\([^)]+\)\s*)?null", target_arg):
        return False, f"arg {arg_index} is not null (was {target_arg!r})"
    return True, ""


def apply_replace_null_arg(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    target = plan.params["target_line"]
    call_expr = plan.params["call_expr"]
    arg_index = plan.params["arg_index"]
    new_value = plan.params["new_value"].strip()

    loc = _find_call_in_line(lines[target - 1], call_expr)
    assert loc is not None
    _, po, pc = loc
    line = lines[target - 1]
    args = _split_top_level_args(line[po + 1 : pc])
    args[arg_index] = new_value
    lines[target - 1] = line[: po + 1] + ", ".join(args) + line[pc:]
    return "\n".join(lines)


def post_replace_null_arg(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    if old_text == new_text:
        return False, "no change"
    return True, ""


# ----------------------------------------------------------------------------
# 9. ADD_SETUP_CALL
# ----------------------------------------------------------------------------
# Inserts a single setup statement at the top of the method body (right after
# the method signature line containing '{').


def pre_add_setup_call(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    stmt = plan.params.get("statement")
    if not isinstance(stmt, str) or not _looks_like_statement(stmt):
        return False, "statement missing or ill-formed"
    for banned in ("assertThrows", "assertDoesNotThrow", "assertAll"):
        if re.search(rf"\b{banned}\s*\(", stmt):
            return False, f"statement contains banned call: {banned}"
    # must have an opening brace line
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.rstrip().endswith("{"):
            return True, ""
    return False, "no method opening brace found"


def apply_add_setup_call(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    stmt = plan.params["statement"].strip()
    # locate the first line that ends with '{' — that's the method opener
    open_idx = next(i for i, ln in enumerate(lines) if ln.rstrip().endswith("{"))
    # indent = indentation of the first non-blank body line, else opener indent + 4
    body_indent = None
    for ln in lines[open_idx + 1 :]:
        if ln.strip():
            body_indent = indent_of(ln)
            break
    if body_indent is None:
        body_indent = indent_of(lines[open_idx]) + "    "
    rendered = "\n".join(
        body_indent + ln if ln.strip() else ln for ln in stmt.splitlines()
    )
    new_lines = lines[: open_idx + 1] + rendered.splitlines() + lines[open_idx + 1 :]
    return "\n".join(new_lines)


def post_add_setup_call(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    stmt = plan.params["statement"].strip()
    if stmt not in new_text:
        return False, "statement not present"
    return True, ""


# ----------------------------------------------------------------------------
# 10. ADD_TEST_EXPECTED
# ----------------------------------------------------------------------------

_SIMPLE_TYPE_RE = re.compile(r"^[A-Za-z_][\w\.]*$")


def _find_test_annotation_line(lines: List[str]) -> int:
    """Return 1-indexed line that contains @Test, or -1."""
    for i, ln in enumerate(lines):
        if re.search(r"@Test\b", ln):
            return i + 1
    return -1


def pre_add_test_expected(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    exc = plan.params.get("exception_type")
    if not isinstance(exc, str) or not _SIMPLE_TYPE_RE.match(exc):
        return False, f"invalid exception_type: {exc!r}"
    lines = text.splitlines()
    ln = _find_test_annotation_line(lines)
    if ln == -1:
        return False, "no @Test annotation found"
    if re.search(r"@Test\s*\([^)]*\bexpected\s*=", lines[ln - 1]):
        return False, "@Test already has expected="
    return True, ""


def apply_add_test_expected(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    exc = plan.params["exception_type"]
    ln = _find_test_annotation_line(lines)
    line = lines[ln - 1]
    if re.search(r"@Test\s*\(", line):
        # merge arg into existing parens
        line = re.sub(
            r"@Test\s*\(\s*([^)]*)\s*\)",
            lambda m: f"@Test(expected = {exc}.class, {m.group(1).strip()})" if m.group(1).strip() else f"@Test(expected = {exc}.class)",
            line,
            count=1,
        )
    else:
        line = re.sub(r"@Test\b", f"@Test(expected = {exc}.class)", line, count=1)
    lines[ln - 1] = line
    return "\n".join(lines)


def post_add_test_expected(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    exc = plan.params["exception_type"]
    if f"expected = {exc}.class" not in new_text and f"expected={exc}.class" not in new_text:
        return False, "expected= attr not inserted"
    return True, ""


# ----------------------------------------------------------------------------
# 11. REMOVE_TEST_EXPECTED
# ----------------------------------------------------------------------------


def pre_remove_test_expected(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    lines = text.splitlines()
    ln = _find_test_annotation_line(lines)
    if ln == -1:
        return False, "no @Test annotation found"
    if not re.search(r"@Test\s*\([^)]*\bexpected\s*=", lines[ln - 1]):
        return False, "@Test does not have expected="
    return True, ""


def apply_remove_test_expected(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    lines = text.splitlines()
    ln = _find_test_annotation_line(lines)
    line = lines[ln - 1]

    def _rewrite(m: "re.Match[str]") -> str:
        inner = m.group(1)
        parts = [
            p.strip()
            for p in _split_top_level_args(inner)
            if not re.match(r"\s*expected\s*=", p)
        ]
        if not parts:
            return "@Test"
        return f"@Test({', '.join(parts)})"

    line = re.sub(r"@Test\s*\(([^)]*)\)", _rewrite, line, count=1)
    lines[ln - 1] = line
    return "\n".join(lines)


def post_remove_test_expected(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    if re.search(r"@Test\s*\([^)]*\bexpected\s*=", new_text):
        return False, "expected= still present"
    return True, ""


# ----------------------------------------------------------------------------
# 12. REMOVE_TRY_CATCH_KEEP_BODY
# ----------------------------------------------------------------------------

_TRY_KEYWORD_RE = re.compile(r"(^|[^\w.])try\s*\{")


def _find_matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    n = len(text)
    in_s = False
    in_d = False
    in_sl = False
    in_ml = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_sl:
            if ch == "\n":
                in_sl = False
            i += 1
            continue
        if in_ml:
            if ch == "*" and nxt == "/":
                in_ml = False
                i += 2
                continue
            i += 1
            continue
        if not (in_s or in_d):
            if ch == "/" and nxt == "/":
                in_sl = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_ml = True
                i += 2
                continue
        if ch == '"' and not in_s:
            in_d = not in_d
            i += 1
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
            i += 1
            continue
        if in_s or in_d:
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _locate_try_block(text: str, try_begin_line: int) -> Optional[Dict[str, int]]:
    """Return dict with offsets of try block:
    {try_kw_start, try_open, try_close, catches: [(kw_start, open, close), ...]}
    or None.
    """
    lines = text.splitlines(keepends=True)
    if not (1 <= try_begin_line <= len(lines)):
        return None
    # absolute offset of start of target line
    line_start = sum(len(ln) for ln in lines[: try_begin_line - 1])
    # search for 'try {' starting at line_start
    m = re.search(r"\btry\s*\{", text[line_start:])
    if not m:
        return None
    try_open = line_start + m.end() - 1  # index of '{'
    try_close = _find_matching_brace(text, try_open)
    if try_close < 0:
        return None

    info: Dict[str, Any] = {
        "try_kw_start": line_start + m.start(),
        "try_open": try_open,
        "try_close": try_close,
        "catches": [],
    }

    # walk catch/finally clauses immediately following
    cursor = try_close + 1
    while True:
        # skip whitespace/comments between clauses
        ws = re.match(r"\s*", text[cursor:])
        gap = ws.end() if ws else 0
        rem = text[cursor + gap :]
        cm = re.match(r"catch\s*\([^)]*\)\s*\{", rem)
        fm = re.match(r"finally\s*\{", rem)
        if cm:
            c_open = cursor + gap + cm.end() - 1
            c_close = _find_matching_brace(text, c_open)
            if c_close < 0:
                return None
            info["catches"].append(("catch", cursor + gap, c_open, c_close))
            cursor = c_close + 1
            continue
        if fm:
            f_open = cursor + gap + fm.end() - 1
            f_close = _find_matching_brace(text, f_open)
            if f_close < 0:
                return None
            info["catches"].append(("finally", cursor + gap, f_open, f_close))
            cursor = f_close + 1
            continue
        break
    return info


def _extract_try_body_lines(text: str, try_info: Dict[str, int]) -> List[str]:
    body = text[try_info["try_open"] + 1 : try_info["try_close"]]
    # strip one leading/trailing newline for cleanliness
    if body.startswith("\n"):
        body = body[1:]
    if body.endswith("\n"):
        body = body[:-1]
    return body.splitlines()


def _line_offsets(text: str) -> List[int]:
    offsets = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _line_of_offset(offsets: List[int], idx: int) -> int:
    import bisect
    return bisect.bisect_right(offsets, idx) - 1


def pre_remove_try_catch_keep_body(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    begin = plan.params.get("try_begin_line")
    if not isinstance(begin, int) or begin < 1:
        return False, "try_begin_line required"
    info = _locate_try_block(text, begin)
    if info is None:
        return False, f"no try-block starting at line {begin}"
    return True, ""


def apply_remove_try_catch_keep_body(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    info = _locate_try_block(text, plan.params["try_begin_line"])
    assert info is not None
    body_lines = _extract_try_body_lines(text, info)
    drop_fail = plan.params.get("drop_fail_call", True)
    if drop_fail:
        body_lines = [
            ln for ln in body_lines if not re.match(r"\s*fail\s*\(", ln)
        ]
    # dedent by one level (4 spaces or a tab) because try-body was nested
    dedented: List[str] = []
    for ln in body_lines:
        if ln.startswith("    "):
            dedented.append(ln[4:])
        elif ln.startswith("\t"):
            dedented.append(ln[1:])
        else:
            dedented.append(ln)
    body_block = "\n".join(dedented)
    # replacement region: from try_kw_start to end of last clause
    end_idx = info["catches"][-1][3] if info["catches"] else info["try_close"]
    # include trailing newline if present
    if end_idx + 1 < len(text) and text[end_idx + 1] == "\n":
        end_idx += 1
    new_text = text[: info["try_kw_start"]] + body_block + text[end_idx + 1 :]
    return new_text


def post_remove_try_catch_keep_body(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    # best-effort: old had try {, new should have at least one fewer try
    if old_text.count("try {") - new_text.count("try {") < 1 and \
       old_text.count("try{") - new_text.count("try{") < 1:
        return False, "try block not removed"
    return True, ""


# ----------------------------------------------------------------------------
# 13. TRY_CATCH_TO_EXPECTED
# ----------------------------------------------------------------------------
# Composite: locates a try-catch and rewrites method to use @Test(expected=X.class).
# Precondition demands simple single-try, single-catch, fail-inside-try pattern.


def _is_simple_try_catch_method(text: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    # count 'try {' occurrences
    tries = list(_TRY_KEYWORD_RE.finditer(text))
    if len(tries) != 1:
        return False, None, f"expected exactly 1 try, got {len(tries)}"
    # locate that try block
    offsets = _line_offsets(text)
    try_line = _line_of_offset(offsets, tries[0].start()) + 1
    info = _locate_try_block(text, try_line)
    if info is None:
        return False, None, "could not locate try block"
    catches = info["catches"]
    non_finally = [c for c in catches if c[0] == "catch"]
    if len(non_finally) != 1:
        return False, None, f"expected exactly 1 catch clause, got {len(non_finally)}"
    if any(c[0] == "finally" for c in catches):
        return False, None, "finally clause not supported"
    # try body must contain fail()
    try_body = text[info["try_open"] + 1 : info["try_close"]]
    if not re.search(r"\bfail\s*\(", try_body):
        return False, None, "no fail() in try body"
    # catch body must only contain verifyException/assertion-on-expected or be empty
    _, _, c_open, c_close = non_finally[0]
    catch_body = text[c_open + 1 : c_close]
    for raw in catch_body.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("//") or ln.startswith("/*") or ln.startswith("*"):
            continue
        if ln.startswith("verifyException"):
            continue
        if ln.startswith("assertEquals") or ln.startswith("assertTrue"):
            continue
        return False, None, f"catch body has unsupported statement: {ln!r}"
    return True, info, ""


def pre_try_catch_to_expected(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    exc = plan.params.get("exception_type")
    if not isinstance(exc, str) or not _SIMPLE_TYPE_RE.match(exc):
        return False, f"invalid exception_type: {exc!r}"
    lines = text.splitlines()
    if _find_test_annotation_line(lines) == -1:
        return False, "no @Test annotation on method"
    if re.search(r"@Test\s*\([^)]*\bexpected\s*=", text):
        return False, "@Test already has expected="
    ok, _, reason = _is_simple_try_catch_method(text)
    if not ok:
        return False, reason
    return True, ""


def apply_try_catch_to_expected(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    exc = plan.params["exception_type"]
    # 1. rewrite @Test annotation
    lines = text.splitlines()
    ln = _find_test_annotation_line(lines)
    line = lines[ln - 1]
    if re.search(r"@Test\s*\(", line):
        line = re.sub(
            r"@Test\s*\(\s*([^)]*)\s*\)",
            lambda m: f"@Test(expected = {exc}.class, {m.group(1).strip()})" if m.group(1).strip() else f"@Test(expected = {exc}.class)",
            line,
            count=1,
        )
    else:
        line = re.sub(r"@Test\b", f"@Test(expected = {exc}.class)", line, count=1)
    lines[ln - 1] = line
    text = "\n".join(lines)
    # 2. locate the try block and rewrite by discarding try-catch, keeping try body minus fail()
    ok, info, reason = _is_simple_try_catch_method(text)
    assert ok, reason
    # find line where try starts
    offsets = _line_offsets(text)
    try_line = _line_of_offset(offsets, info["try_kw_start"]) + 1
    return apply_remove_try_catch_keep_body(
        OperatorPlan(
            op=OperatorId.REMOVE_TRY_CATCH_KEEP_BODY,
            params={"try_begin_line": try_line, "drop_fail_call": True},
            smell_id=plan.smell_id,
        ),
        text,
        ctx,
    )


def post_try_catch_to_expected(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    exc = plan.params["exception_type"]
    if f"expected = {exc}.class" not in new_text and f"expected={exc}.class" not in new_text:
        return False, "expected= attr not inserted"
    if re.search(r"\bfail\s*\(", new_text) and not re.search(r"\bfail\s*\(", old_text.replace(new_text, "")):
        # fail call remains; weak heuristic
        pass
    if new_text.count("try {") >= old_text.count("try {") and old_text.count("try {") > 0:
        return False, "try block not removed"
    return True, ""


# ----------------------------------------------------------------------------
# 14. WRAP_WITH_ASSERT_THROWS
# ----------------------------------------------------------------------------
# JUnit 5 operator; banned in our JUnit 4 environment. Included for interface
# completeness. Precondition always fails unless explicitly opted-in.


def pre_wrap_with_assert_throws(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    return False, "assertThrows is JUnit 5 only; disabled in JUnit 4 environment"


def apply_wrap_with_assert_throws(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:  # pragma: no cover - unreachable
    raise RuntimeError("WRAP_WITH_ASSERT_THROWS is disabled")


def post_wrap_with_assert_throws(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:  # pragma: no cover
    return False, "disabled"


# ----------------------------------------------------------------------------
# 15. EXTRACT_TO_BEFORE  (FILE scope)
# ----------------------------------------------------------------------------
# Wraps v1's `extract_duplicated_setup_to_before` so it fits the operator
# interface. Operates on the full file text.


def pre_extract_to_before(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    targets = plan.params.get("target_methods")
    if not isinstance(targets, list) or len(targets) < 2:
        return False, "target_methods must be a list of >=2 method names"
    if not all(isinstance(n, str) and n for n in targets):
        return False, "target_methods contains non-string"
    if "@Before" in text or "org.junit.Before" in text:
        return False, "@Before already exists in file"
    # must contain all target methods
    for nm in targets:
        if not re.search(rf"\bvoid\s+{re.escape(nm)}\s*\(", text):
            return False, f"method {nm} not found in file"
    return True, ""


_METHOD_BLOCK_RE = re.compile(
    r"(?ms)^(?P<prefix>[ \t]*(?:@Test[^\n]*\n[ \t]*)*)"
    r"(?P<sig>(?:public\s+)?void\s+(?P<name>test\w+)\s*\([^)]*\)\s*(?:throws[^\{]+)?\{)"
    r"(?P<body>.*?)(?P<close>^[ \t]*\})",
)

_DECL_RE = re.compile(
    r"^(?P<indent>\s*)(?:final\s+)?(?P<type>[A-Za-z_][\w\.<>,\[\]]*)\s+"
    r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<rhs>.+);\s*$"
)


def apply_extract_to_before(
    plan: OperatorPlan, text: str, ctx: ExecutionContext
) -> str:
    targets: List[str] = list(plan.params["target_methods"])

    # 1. collect each target method's body lines (non-empty, in order)
    matches: Dict[str, "re.Match[str]"] = {}
    for m in _METHOD_BLOCK_RE.finditer(text):
        if m.group("name") in targets and m.group("name") not in matches:
            matches[m.group("name")] = m
    if len(matches) < 2:
        return text

    bodies: Dict[str, List[str]] = {}
    for nm, m in matches.items():
        body_lines = [ln for ln in m.group("body").splitlines() if ln.strip() != ""]
        bodies[nm] = body_lines

    # 2. compute common line prefix (stop at assertion / try / control flow)
    prefix: List[str] = []
    min_len = min(len(b) for b in bodies.values())
    first = next(iter(bodies.values()))
    for i in range(min_len):
        li = first[i]
        if not all(bodies[nm][i] == li for nm in bodies):
            break
        stripped = li.strip()
        if (
            "assert" in stripped
            or stripped.startswith("try")
            or stripped.startswith("fail(")
            or stripped.startswith("}")
        ):
            break
        prefix.append(li)
    if len(prefix) < 2:
        return text

    # 3. promote variable declarations in prefix to fields
    promoted: Dict[str, str] = {}
    field_decls: List[str] = []
    setup_lines: List[str] = []
    for ln in prefix:
        md = _DECL_RE.match(ln)
        if md:
            ty, var, rhs = md.group("type"), md.group("var"), md.group("rhs")
            if var not in promoted:
                promoted[var] = ty
                field_decls.append(f"  private {ty} {var};")
            setup_lines.append(f"    {var} = {rhs};")
        else:
            setup_lines.append(ln.lstrip())

    # 4. build the @Before block
    setup_block = (
        "\n"
        + "\n".join(field_decls)
        + "\n\n  @org.junit.Before\n  public void setUp() throws Exception {\n"
        + "\n".join("    " + l for l in setup_lines)
        + "\n  }\n"
    )

    # 5. remove the prefix lines from each target method's body.
    # Rebuild method body: drop the first `len(prefix)` non-empty lines that equal prefix.
    # For lines redeclaring promoted vars after removal, strip the type prefix.
    new_text_parts: List[str] = []
    cursor = 0
    sorted_matches = sorted(matches.values(), key=lambda m: m.start())
    for m in sorted_matches:
        new_text_parts.append(text[cursor : m.start("body")])
        body = m.group("body")
        # walk lines, drop exactly the prefix once (non-empty matches only)
        out_lines: List[str] = []
        to_drop = list(prefix)
        for raw in body.splitlines(keepends=True):
            line_no_nl = raw.rstrip("\n")
            if to_drop and line_no_nl == to_drop[0]:
                to_drop.pop(0)
                continue
            # strip type declaration for promoted vars appearing later
            stripped_ln = line_no_nl
            m_decl = _DECL_RE.match(line_no_nl)
            if m_decl and m_decl.group("var") in promoted:
                indent = m_decl.group("indent")
                var = m_decl.group("var")
                rhs = m_decl.group("rhs")
                stripped_ln = f"{indent}{var} = {rhs};"
            out_lines.append(stripped_ln + ("\n" if raw.endswith("\n") else ""))
        new_body = "".join(out_lines)
        new_text_parts.append(new_body)
        cursor = m.end("body")
    new_text_parts.append(text[cursor:])
    rewritten = "".join(new_text_parts)

    # 6. insert setup block right after the first '{' (class opener)
    class_open = rewritten.find("{")
    if class_open < 0:
        return text
    return rewritten[: class_open + 1] + setup_block + rewritten[class_open + 1 :]


def post_extract_to_before(
    plan: OperatorPlan, old_text: str, new_text: str, ctx: ExecutionContext
) -> Tuple[bool, str]:
    # Extraction of a K-line prefix from N tests removes K*N lines and adds
    # a fixed setUp() block (~6-10 lines). For large groups the net change
    # is negative — so we do NOT require line count to grow. Correctness is
    # witnessed by "text changed" + "@Before inserted".
    if new_text == old_text:
        return False, "no common prefix extracted"
    if "@org.junit.Before" not in new_text and "@Before" not in new_text:
        return False, "no @Before generated"
    return True, ""


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


OPERATOR_REGISTRY: Dict[OperatorId, Dict[str, object]] = {
    OperatorId.INSERT_ASSERTION: {
        "scope": OperatorScope.METHOD,
        "pre": pre_insert_assertion,
        "apply": apply_insert_assertion,
        "post": post_insert_assertion,
    },
    OperatorId.REMOVE_ASSERTION: {
        "scope": OperatorScope.METHOD,
        "pre": pre_remove_assertion,
        "apply": apply_remove_assertion,
        "post": post_remove_assertion,
    },
    OperatorId.REPLACE_ASSERTION: {
        "scope": OperatorScope.METHOD,
        "pre": pre_replace_assertion,
        "apply": apply_replace_assertion,
        "post": post_replace_assertion,
    },
    OperatorId.INSERT_STATEMENT: {
        "scope": OperatorScope.METHOD,
        "pre": pre_insert_statement,
        "apply": apply_insert_statement,
        "post": post_insert_statement,
    },
    OperatorId.REMOVE_STATEMENT: {
        "scope": OperatorScope.METHOD,
        "pre": pre_remove_statement,
        "apply": apply_remove_statement,
        "post": post_remove_statement,
    },
    OperatorId.REPLACE_EXPRESSION: {
        "scope": OperatorScope.METHOD,
        "pre": pre_replace_expression,
        "apply": apply_replace_expression,
        "post": post_replace_expression,
    },
    OperatorId.CAPTURE_RETURN_VALUE: {
        "scope": OperatorScope.METHOD,
        "pre": pre_capture_return_value,
        "apply": apply_capture_return_value,
        "post": post_capture_return_value,
    },
    OperatorId.REPLACE_NULL_ARG: {
        "scope": OperatorScope.METHOD,
        "pre": pre_replace_null_arg,
        "apply": apply_replace_null_arg,
        "post": post_replace_null_arg,
    },
    OperatorId.ADD_SETUP_CALL: {
        "scope": OperatorScope.METHOD,
        "pre": pre_add_setup_call,
        "apply": apply_add_setup_call,
        "post": post_add_setup_call,
    },
    OperatorId.ADD_TEST_EXPECTED: {
        "scope": OperatorScope.METHOD,
        "pre": pre_add_test_expected,
        "apply": apply_add_test_expected,
        "post": post_add_test_expected,
    },
    OperatorId.REMOVE_TEST_EXPECTED: {
        "scope": OperatorScope.METHOD,
        "pre": pre_remove_test_expected,
        "apply": apply_remove_test_expected,
        "post": post_remove_test_expected,
    },
    OperatorId.REMOVE_TRY_CATCH_KEEP_BODY: {
        "scope": OperatorScope.METHOD,
        "pre": pre_remove_try_catch_keep_body,
        "apply": apply_remove_try_catch_keep_body,
        "post": post_remove_try_catch_keep_body,
    },
    OperatorId.TRY_CATCH_TO_EXPECTED: {
        "scope": OperatorScope.METHOD,
        "pre": pre_try_catch_to_expected,
        "apply": apply_try_catch_to_expected,
        "post": post_try_catch_to_expected,
    },
    OperatorId.WRAP_WITH_ASSERT_THROWS: {
        "scope": OperatorScope.METHOD,
        "pre": pre_wrap_with_assert_throws,
        "apply": apply_wrap_with_assert_throws,
        "post": post_wrap_with_assert_throws,
    },
    OperatorId.EXTRACT_TO_BEFORE: {
        "scope": OperatorScope.FILE,
        "pre": pre_extract_to_before,
        "apply": apply_extract_to_before,
        "post": post_extract_to_before,
    },
}


def get_operator_funcs(op_id: OperatorId) -> Dict[str, Callable]:
    if op_id not in OPERATOR_REGISTRY:
        raise KeyError(f"Operator not registered: {op_id}")
    return OPERATOR_REGISTRY[op_id]  # type: ignore[return-value]


def get_operator_scope(op_id: OperatorId) -> OperatorScope:
    return OPERATOR_REGISTRY[op_id]["scope"]  # type: ignore[return-value]

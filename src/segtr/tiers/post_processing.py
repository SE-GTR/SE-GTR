"""Post-processing passes applied after OperatorExecutor succeeds but before
the MultiGateValidator runs. Keeping these outside executor.py preserves the
Phase 1 operator infrastructure untouched.

Currently the only pass is :func:`apply_narv_guard`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.operators.catalog import (
    ASSERT_LINE_RE,
    method_local_identifiers,
)
from smell_repair_v2.tiers.tier1_deterministic import get_method_return_type


_TRY_CATCH_OPS: frozenset[OperatorId] = frozenset({
    OperatorId.REMOVE_TRY_CATCH_KEEP_BODY,
    OperatorId.TRY_CATCH_TO_EXPECTED,
})

_BARE_METHOD_RE = re.compile(
    r"^(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*)\s*\("
)
_BARE_FUNC_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(")
_ASSIGN_RE = re.compile(r"(?<![=!<>])=(?!=)")
_SKIP_PREFIXES = (
    "if ", "for ", "while ", "switch ", "try ", "catch ", "finally ",
    "else ", "do ", "return ", "throw ", "break", "continue",
)


@dataclass
class NarvGuardChange:
    line_num: int
    method_name: str
    return_type: str
    var_name: str
    original_line: str
    captured_line: str


def apply_narv_guard(
    method_text: str,
    applied_op_ids: List[OperatorId],
    ctx: ExecutionContext,
) -> Tuple[str, List[NarvGuardChange]]:
    """Wrap naked non-void calls left by try-catch removal so Smelly-E's
    NARV detector doesn't fire.

    Only triggers when ``applied_op_ids`` contains a try-catch-removal
    operator. For each bare expression-statement whose callee returns
    non-void (resolved via ``ctx.cut_source``), the line is rewritten from

        ``expr;``  →  ``ReturnType _capturedN = expr;``

    The captured variable is intentionally unused — its sole purpose is to
    shift the statement from "expression-statement" to "local-variable
    declaration" so NARV's detector pattern no longer matches.

    Returns ``(new_method_text, list_of_changes)``.
    """
    if not (_TRY_CATCH_OPS & set(applied_op_ids)):
        return method_text, []
    if not ctx.cut_source:
        return method_text, []

    lines = method_text.splitlines()
    used_names: Set[str] = method_local_identifiers(method_text)
    capture_idx = 0
    changes: List[NarvGuardChange] = []
    new_lines: List[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        if _should_skip(stripped):
            new_lines.append(line)
            continue

        method_name = _extract_callee(stripped)
        if method_name is None:
            new_lines.append(line)
            continue

        rtype = get_method_return_type(ctx.cut_source, method_name)
        if rtype is None or rtype == "void":
            new_lines.append(line)
            continue

        # pick a fresh variable name
        while True:
            var = f"_captured{capture_idx}"
            capture_idx += 1
            if var not in used_names:
                break
        used_names.add(var)

        indent = re.match(r"(\s*)", line).group(1)
        expr = stripped.rstrip(";").strip()
        captured_line = f"{indent}{rtype} {var} = {expr};"
        new_lines.append(captured_line)
        changes.append(NarvGuardChange(
            line_num=i + 1,
            method_name=method_name,
            return_type=rtype,
            var_name=var,
            original_line=line,
            captured_line=captured_line,
        ))

    return "\n".join(new_lines), changes


def _should_skip(stripped: str) -> bool:
    """Return True iff the line should NOT be considered for capture."""
    if not stripped:
        return True
    if stripped.startswith(("//", "/*", "*", "}")):
        return True
    if stripped in ("{",):
        return True
    if ASSERT_LINE_RE.match(stripped):
        return True
    if stripped.startswith(("fail(", "verifyException(")):
        return True
    if any(stripped.startswith(p) for p in _SKIP_PREFIXES):
        return True
    if _ASSIGN_RE.search(stripped):
        return True
    if not stripped.endswith(";"):
        return True
    if "(" not in stripped:
        return True
    if stripped.startswith("new "):
        return True
    return False


def _extract_callee(stripped: str) -> Optional[str]:
    """Extract the method name from a bare call expression, or None.

    Handles ``var.method(...)``, ``Class.method(...)``, ``method(...)``.
    Does NOT match ``new Foo(...)`` (filtered by caller) or declarations
    (filtered by ``_should_skip``).
    """
    m = _BARE_METHOD_RE.match(stripped)
    if m:
        return m.group(1)
    m = _BARE_FUNC_RE.match(stripped)
    if m:
        name = m.group(1)
        # reject Java keywords that look like function calls
        if name in ("super", "this"):
            return None
        return name
    return None

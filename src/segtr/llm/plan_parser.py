"""Parses an LLM JSON response into ``List[OperatorPlan]``.

Responsibilities strictly *structural*:
  - extract a JSON candidate from the response (tolerate markdown / preamble)
  - parse JSON
  - validate array-of-objects shape
  - validate operator id is registered AND in the caller's allowed list
  - validate the *shape* of each operator's params (required keys, primitive
    types, enum membership) against OPERATOR_SCHEMAS

Everything that needs method context — line ranges, variable scope, balanced
braces, "only 1 assertion left" — is deliberately left to the operator
preconditions in ``operators/catalog.py``. Keeping the two layers disjoint
avoids the maintenance hazard of duplicated rules.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from smell_repair_v2.operators.base import OperatorId, OperatorPlan


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class PlanParseError(Exception):
    """Raised when an LLM response cannot be parsed into a plan.

    ``recoverable=True`` signals to ``PlanRunner`` that retrying with the
    error as feedback may succeed (e.g. malformed JSON, wrong operator id).
    ``recoverable=False`` is reserved for genuinely fatal conditions (the
    runner will not retry on those — currently only used if an opaque
    internal bug occurs).
    """

    def __init__(self, reason: str, llm_output: str, recoverable: bool = True):
        self.reason = reason
        self.llm_output = llm_output
        self.recoverable = recoverable
        super().__init__(reason)


# ---------------------------------------------------------------------------
# JSON extraction (tolerate markdown fences / preamble)
# ---------------------------------------------------------------------------

_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Return the best-guess JSON candidate from ``text``.

    Strategies, in order:
      1. First fenced block ``` ```json ... ``` ```
      2. First fenced block ``` ``` ... ``` ``` (no language hint)
      3. Substring from the first ``[`` to the matching closing ``]``
      4. Fall back to raw text
    """
    if text is None:
        return ""
    s = text.strip()
    if not s:
        return s

    m = _FENCE_JSON_RE.search(s)
    if m:
        return m.group(1).strip()

    first_bracket = s.find("[")
    if first_bracket >= 0:
        depth = 0
        in_str: Optional[str] = None
        i = first_bracket
        while i < len(s):
            ch = s[i]
            if in_str:
                if ch == "\\" and i + 1 < len(s):
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            else:
                if ch in ("'", '"'):
                    in_str = ch
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        return s[first_bracket : i + 1]
            i += 1

    return s


# ---------------------------------------------------------------------------
# structural schema validation
# ---------------------------------------------------------------------------

VALID_ASSERT_TYPES = {
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

# assert_types that require an `expected_expr` alongside `actual_expr`
ASSERT_TYPES_NEEDING_EXPECTED = {
    "assertEquals",
    "assertSame",
    "assertNotSame",
    "assertArrayEquals",
}

_IDENT_RE = re.compile(r"^[A-Za-z_][\w]*$")
_SIMPLE_TYPE_RE = re.compile(r"^[A-Za-z_][\w\.]*$")


def _require(params: Dict[str, Any], key: str, type_: type, label: str) -> Optional[str]:
    if key not in params:
        return f"missing required param '{key}'"
    if type_ is str and not isinstance(params[key], str):
        return f"{label}: '{key}' must be string, got {type(params[key]).__name__}"
    if type_ is int:
        # reject bool (bool is subclass of int in Python) since line numbers shouldn't be True/False
        if isinstance(params[key], bool) or not isinstance(params[key], int):
            return f"{label}: '{key}' must be int, got {type(params[key]).__name__}"
    if type_ is list and not isinstance(params[key], list):
        return f"{label}: '{key}' must be array, got {type(params[key]).__name__}"
    if type_ is dict and not isinstance(params[key], dict):
        return f"{label}: '{key}' must be object, got {type(params[key]).__name__}"
    return None


def _v_insert_assertion(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "after_line", int, "INSERT_ASSERTION")
    if err:
        return err
    err = _require(p, "assert_type", str, "INSERT_ASSERTION")
    if err:
        return err
    if p["assert_type"] not in VALID_ASSERT_TYPES:
        return (
            f"INSERT_ASSERTION: assert_type '{p['assert_type']}' not in "
            f"{sorted(VALID_ASSERT_TYPES)}"
        )
    err = _require(p, "actual_expr", str, "INSERT_ASSERTION")
    if err:
        return err
    # actual_expr may be empty only for fail()
    if p["assert_type"] != "fail" and not p["actual_expr"].strip():
        return "INSERT_ASSERTION: actual_expr must be non-empty (unless assert_type='fail')"
    if p["assert_type"] in ASSERT_TYPES_NEEDING_EXPECTED:
        if not isinstance(p.get("expected_expr"), str) or not p["expected_expr"].strip():
            return f"INSERT_ASSERTION: assert_type '{p['assert_type']}' requires non-empty expected_expr"
    if "message" in p and not isinstance(p["message"], str):
        return "INSERT_ASSERTION: message must be string"
    return None


def _v_remove_assertion(p: Dict[str, Any]) -> Optional[str]:
    return _require(p, "target_line", int, "REMOVE_ASSERTION")


def _v_replace_assertion(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "target_line", int, "REPLACE_ASSERTION")
    if err:
        return err
    at = p.get("new_assert_type") or p.get("assert_type")
    if not isinstance(at, str) or not at:
        return "REPLACE_ASSERTION: missing new_assert_type"
    if at not in VALID_ASSERT_TYPES:
        return f"REPLACE_ASSERTION: new_assert_type '{at}' not in {sorted(VALID_ASSERT_TYPES)}"
    actual = p.get("new_actual_expr") if "new_actual_expr" in p else p.get("actual_expr")
    if not isinstance(actual, str):
        return "REPLACE_ASSERTION: missing new_actual_expr"
    if at != "fail" and not actual.strip():
        return "REPLACE_ASSERTION: new_actual_expr must be non-empty (unless new_assert_type='fail')"
    if at in ASSERT_TYPES_NEEDING_EXPECTED:
        exp = p.get("new_expected_expr") if "new_expected_expr" in p else p.get("expected_expr")
        if not isinstance(exp, str) or not exp.strip():
            return (
                f"REPLACE_ASSERTION: new_assert_type '{at}' requires non-empty "
                f"new_expected_expr"
            )
    if "message" in p and not isinstance(p["message"], str):
        return "REPLACE_ASSERTION: message must be string"
    return None


def _v_insert_statement(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "after_line", int, "INSERT_STATEMENT")
    if err:
        return err
    if p["after_line"] < 0:
        return "INSERT_STATEMENT: after_line must be >= 0"
    err = _require(p, "statement", str, "INSERT_STATEMENT")
    if err:
        return err
    if not p["statement"].strip():
        return "INSERT_STATEMENT: statement must be non-empty"
    return None


def _v_remove_statement(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "target_line", int, "REMOVE_STATEMENT")
    if err:
        return err
    if p["target_line"] < 1:
        return "REMOVE_STATEMENT: target_line must be >= 1"
    return None


def _v_replace_expression(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "target_line", int, "REPLACE_EXPRESSION")
    if err:
        return err
    err = _require(p, "old_expr", str, "REPLACE_EXPRESSION")
    if err:
        return err
    if not p["old_expr"]:
        return "REPLACE_EXPRESSION: old_expr must be non-empty"
    err = _require(p, "new_expr", str, "REPLACE_EXPRESSION")
    if err:
        return err
    return None


def _v_capture_return_value(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "target_line", int, "CAPTURE_RETURN_VALUE")
    if err:
        return err
    err = _require(p, "var_name", str, "CAPTURE_RETURN_VALUE")
    if err:
        return err
    if not _IDENT_RE.match(p["var_name"]):
        return f"CAPTURE_RETURN_VALUE: var_name '{p['var_name']}' is not a valid Java identifier"
    err = _require(p, "var_type", str, "CAPTURE_RETURN_VALUE")
    if err:
        return err
    if not p["var_type"].strip():
        return "CAPTURE_RETURN_VALUE: var_type must be non-empty"
    if p["var_type"].strip() == "void":
        return "CAPTURE_RETURN_VALUE: var_type must not be 'void'"
    return None


def _v_replace_null_arg(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "target_line", int, "REPLACE_NULL_ARG")
    if err:
        return err
    err = _require(p, "call_expr", str, "REPLACE_NULL_ARG")
    if err:
        return err
    if not p["call_expr"].strip():
        return "REPLACE_NULL_ARG: call_expr must be non-empty"
    err = _require(p, "arg_index", int, "REPLACE_NULL_ARG")
    if err:
        return err
    if p["arg_index"] < 0:
        return "REPLACE_NULL_ARG: arg_index must be >= 0"
    err = _require(p, "new_value", str, "REPLACE_NULL_ARG")
    if err:
        return err
    if not p["new_value"].strip():
        return "REPLACE_NULL_ARG: new_value must be non-empty"
    return None


def _v_add_setup_call(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "statement", str, "ADD_SETUP_CALL")
    if err:
        return err
    if not p["statement"].strip():
        return "ADD_SETUP_CALL: statement must be non-empty"
    return None


def _v_try_catch_to_expected(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "exception_type", str, "TRY_CATCH_TO_EXPECTED")
    if err:
        return err
    if not _SIMPLE_TYPE_RE.match(p["exception_type"]):
        return f"TRY_CATCH_TO_EXPECTED: exception_type '{p['exception_type']}' is not a simple class name"
    return None


def _v_remove_try_catch_keep_body(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "try_begin_line", int, "REMOVE_TRY_CATCH_KEEP_BODY")
    if err:
        return err
    if p["try_begin_line"] < 1:
        return "REMOVE_TRY_CATCH_KEEP_BODY: try_begin_line must be >= 1"
    if "drop_fail_call" in p and not isinstance(p["drop_fail_call"], bool):
        return "REMOVE_TRY_CATCH_KEEP_BODY: drop_fail_call must be bool if present"
    return None


def _v_wrap_with_assert_throws(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "exception_type", str, "WRAP_WITH_ASSERT_THROWS")
    if err:
        return err
    if not _SIMPLE_TYPE_RE.match(p["exception_type"]):
        return f"WRAP_WITH_ASSERT_THROWS: exception_type '{p['exception_type']}' is not a simple class name"
    return None


def _v_add_test_expected(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "exception_type", str, "ADD_TEST_EXPECTED")
    if err:
        return err
    if not _SIMPLE_TYPE_RE.match(p["exception_type"]):
        return f"ADD_TEST_EXPECTED: exception_type '{p['exception_type']}' is not a simple class name"
    return None


def _v_remove_test_expected(p: Dict[str, Any]) -> Optional[str]:
    # no required params; params dict itself must be an object — already ensured by caller.
    return None


def _v_extract_to_before(p: Dict[str, Any]) -> Optional[str]:
    err = _require(p, "target_methods", list, "EXTRACT_TO_BEFORE")
    if err:
        return err
    methods = p["target_methods"]
    if len(methods) < 2:
        return "EXTRACT_TO_BEFORE: target_methods must contain at least 2 names"
    for i, nm in enumerate(methods):
        if not isinstance(nm, str) or not nm.strip():
            return f"EXTRACT_TO_BEFORE: target_methods[{i}] must be a non-empty string"
    return None


_VALIDATORS: Dict[OperatorId, Callable[[Dict[str, Any]], Optional[str]]] = {
    OperatorId.INSERT_ASSERTION: _v_insert_assertion,
    OperatorId.REMOVE_ASSERTION: _v_remove_assertion,
    OperatorId.REPLACE_ASSERTION: _v_replace_assertion,
    OperatorId.INSERT_STATEMENT: _v_insert_statement,
    OperatorId.REMOVE_STATEMENT: _v_remove_statement,
    OperatorId.REPLACE_EXPRESSION: _v_replace_expression,
    OperatorId.CAPTURE_RETURN_VALUE: _v_capture_return_value,
    OperatorId.REPLACE_NULL_ARG: _v_replace_null_arg,
    OperatorId.ADD_SETUP_CALL: _v_add_setup_call,
    OperatorId.TRY_CATCH_TO_EXPECTED: _v_try_catch_to_expected,
    OperatorId.REMOVE_TRY_CATCH_KEEP_BODY: _v_remove_try_catch_keep_body,
    OperatorId.WRAP_WITH_ASSERT_THROWS: _v_wrap_with_assert_throws,
    OperatorId.ADD_TEST_EXPECTED: _v_add_test_expected,
    OperatorId.REMOVE_TEST_EXPECTED: _v_remove_test_expected,
    OperatorId.EXTRACT_TO_BEFORE: _v_extract_to_before,
}


def _validate_params(op_id: OperatorId, params: Dict[str, Any]) -> Optional[str]:
    validator = _VALIDATORS.get(op_id)
    if validator is None:
        return f"no validator registered for operator {op_id.value}"
    return validator(params)


# ---------------------------------------------------------------------------
# top-level parser
# ---------------------------------------------------------------------------


def parse_plan_response(
    llm_output: str,
    smell_id: str,
    allowed_operators: List[str],
) -> List[OperatorPlan]:
    """Parse an LLM response into a list of ``OperatorPlan`` objects.

    See the module docstring for the split between structural checks (here)
    and semantic checks (``operators/catalog.py::pre_*``).
    """
    cleaned = _extract_json(llm_output or "").strip()
    if not cleaned:
        raise PlanParseError("empty response", llm_output or "")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise PlanParseError(f"JSON parse failed: {e.msg} at pos {e.pos}", llm_output or "")

    if not isinstance(data, list):
        raise PlanParseError(
            f"expected a JSON array at top level, got {type(data).__name__}",
            llm_output or "",
        )

    allowed_set = set(allowed_operators)
    plans: List[OperatorPlan] = []

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise PlanParseError(f"item {i}: expected object, got {type(item).__name__}", llm_output or "")
        op_str = item.get("op")
        if not isinstance(op_str, str) or not op_str:
            raise PlanParseError(f"item {i}: missing or non-string 'op'", llm_output or "")
        try:
            op_id = OperatorId(op_str)
        except ValueError:
            raise PlanParseError(
                f"item {i}: unknown operator id '{op_str}'", llm_output or ""
            )
        if op_str not in allowed_set:
            raise PlanParseError(
                f"item {i}: operator '{op_str}' is not in the allowed list for this task "
                f"({sorted(allowed_set)})",
                llm_output or "",
            )
        params = item.get("params", {})
        if not isinstance(params, dict):
            raise PlanParseError(
                f"item {i} ({op_str}): 'params' must be an object, got {type(params).__name__}",
                llm_output or "",
            )
        err = _validate_params(op_id, params)
        if err:
            raise PlanParseError(f"item {i}: {err}", llm_output or "")
        plans.append(OperatorPlan(op=op_id, params=dict(params), smell_id=smell_id))

    return plans

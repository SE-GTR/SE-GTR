from __future__ import annotations

import unittest

from smell_repair_v2.llm.plan_parser import (
    PlanParseError,
    _extract_json,
    parse_plan_response,
)
from smell_repair_v2.operators.base import OperatorId

ALL_OPS = [op.value for op in OperatorId]


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


class TestExtractJson(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(_extract_json("[1, 2]"), "[1, 2]")

    def test_fenced_json_block(self):
        out = _extract_json("```json\n[1, 2]\n```")
        self.assertEqual(out, "[1, 2]")

    def test_fenced_plain_block(self):
        out = _extract_json("```\n[1, 2]\n```")
        self.assertEqual(out, "[1, 2]")

    def test_preamble_before_array(self):
        out = _extract_json("Here is the plan:\n[1, 2]\nDone.")
        self.assertEqual(out, "[1, 2]")

    def test_nested_arrays(self):
        out = _extract_json("prefix [[1], [2, 3]] suffix")
        self.assertEqual(out, "[[1], [2, 3]]")

    def test_string_with_brackets(self):
        """Ensure bracket counter ignores ']' inside strings."""
        out = _extract_json('[{"op": "X", "params": {"s": "a]b"}}]')
        self.assertEqual(out, '[{"op": "X", "params": {"s": "a]b"}}]')

    def test_empty_input(self):
        self.assertEqual(_extract_json(""), "")


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


class TestParseHappyPaths(unittest.TestCase):
    def test_single_insert_assertion(self):
        out = (
            '[{"op": "INSERT_ASSERTION", "params": {'
            '"after_line": 8, "assert_type": "assertTrue", "actual_expr": "x > 0"}}]'
        )
        plans = parse_plan_response(out, "NARV", ALL_OPS)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].op, OperatorId.INSERT_ASSERTION)
        self.assertEqual(plans[0].smell_id, "NARV")
        self.assertEqual(plans[0].params["after_line"], 8)

    def test_empty_array(self):
        self.assertEqual(parse_plan_response("[]", "NARV", ALL_OPS), [])

    def test_markdown_fenced(self):
        out = '```json\n[{"op": "REMOVE_ASSERTION", "params": {"target_line": 3}}]\n```'
        plans = parse_plan_response(out, "NNA", ALL_OPS)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].op, OperatorId.REMOVE_ASSERTION)

    def test_preamble_before_array(self):
        out = 'Plan:\n[{"op": "REMOVE_ASSERTION", "params": {"target_line": 3}}]'
        plans = parse_plan_response(out, "NNA", ALL_OPS)
        self.assertEqual(len(plans), 1)

    def test_multi_operator_sequence(self):
        out = """[
            {"op": "CAPTURE_RETURN_VALUE",
             "params": {"target_line": 7, "var_name": "r", "var_type": "boolean"}},
            {"op": "INSERT_ASSERTION",
             "params": {"after_line": 7, "assert_type": "assertTrue", "actual_expr": "r"}}
        ]"""
        plans = parse_plan_response(out, "NARV", ALL_OPS)
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0].op, OperatorId.CAPTURE_RETURN_VALUE)
        self.assertEqual(plans[1].op, OperatorId.INSERT_ASSERTION)


# ---------------------------------------------------------------------------
# structural failures
# ---------------------------------------------------------------------------


class TestParseInvalidJson(unittest.TestCase):
    def test_not_json(self):
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response("this is not json", "NARV", ALL_OPS)
        self.assertTrue(ctx.exception.recoverable)
        self.assertIn("JSON parse failed", ctx.exception.reason)

    def test_empty_string(self):
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response("", "NARV", ALL_OPS)
        self.assertIn("empty", ctx.exception.reason)

    def test_object_not_array(self):
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response('{"op": "X"}', "NARV", ALL_OPS)
        self.assertIn("expected a JSON array", ctx.exception.reason)

    def test_non_dict_item(self):
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response('["string item"]', "NARV", ALL_OPS)
        self.assertIn("expected object", ctx.exception.reason)


class TestOperatorIdChecks(unittest.TestCase):
    def test_unknown_operator(self):
        out = '[{"op": "INVENTED_OP", "params": {}}]'
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response(out, "NARV", ALL_OPS)
        self.assertIn("unknown operator id", ctx.exception.reason)

    def test_op_not_in_allowed_list(self):
        out = '[{"op": "INSERT_ASSERTION", "params": {"after_line": 1, "assert_type": "assertTrue", "actual_expr": "x"}}]'
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response(out, "NARV", ["REMOVE_ASSERTION"])
        self.assertIn("not in the allowed list", ctx.exception.reason)

    def test_missing_op_field(self):
        out = '[{"params": {}}]'
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response(out, "NARV", ALL_OPS)
        self.assertIn("missing or non-string 'op'", ctx.exception.reason)

    def test_non_string_op_field(self):
        out = '[{"op": 42, "params": {}}]'
        with self.assertRaises(PlanParseError):
            parse_plan_response(out, "NARV", ALL_OPS)

    def test_params_not_object(self):
        out = '[{"op": "REMOVE_ASSERTION", "params": [1, 2]}]'
        with self.assertRaises(PlanParseError) as ctx:
            parse_plan_response(out, "NNA", ALL_OPS)
        self.assertIn("must be an object", ctx.exception.reason)


# ---------------------------------------------------------------------------
# per-operator structural validators
# ---------------------------------------------------------------------------


class TestInsertAssertionSchema(unittest.TestCase):
    def _call(self, params):
        out = f'[{{"op": "INSERT_ASSERTION", "params": {params}}}]'
        return parse_plan_response(out, "NARV", ALL_OPS)

    def test_missing_after_line(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"assert_type": "assertTrue", "actual_expr": "x"}')
        self.assertIn("after_line", ctx.exception.reason)

    def test_after_line_not_int(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"after_line": "5", "assert_type": "assertTrue", "actual_expr": "x"}')
        self.assertIn("must be int", ctx.exception.reason)

    def test_after_line_rejects_bool(self):
        with self.assertRaises(PlanParseError):
            self._call('{"after_line": true, "assert_type": "assertTrue", "actual_expr": "x"}')

    def test_invalid_assert_type(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"after_line": 5, "assert_type": "assertMaybeTrue", "actual_expr": "x"}')
        self.assertIn("not in", ctx.exception.reason)

    def test_assert_equals_requires_expected(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"after_line": 5, "assert_type": "assertEquals", "actual_expr": "x"}')
        self.assertIn("expected_expr", ctx.exception.reason)

    def test_assert_equals_with_expected_ok(self):
        plans = self._call(
            '{"after_line": 5, "assert_type": "assertEquals", "actual_expr": "x", "expected_expr": "1"}'
        )
        self.assertEqual(len(plans), 1)

    def test_fail_no_actual_ok(self):
        plans = self._call('{"after_line": 5, "assert_type": "fail", "actual_expr": ""}')
        self.assertEqual(len(plans), 1)

    def test_message_must_be_string(self):
        with self.assertRaises(PlanParseError):
            self._call(
                '{"after_line": 5, "assert_type": "assertTrue", "actual_expr": "x", "message": 42}'
            )


class TestReplaceAssertionSchema(unittest.TestCase):
    def _call(self, params):
        out = f'[{{"op": "REPLACE_ASSERTION", "params": {params}}}]'
        return parse_plan_response(out, "AC", ALL_OPS)

    def test_requires_new_expected_when_equals(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"target_line": 3, "new_assert_type": "assertEquals", "new_actual_expr": "x"}')
        self.assertIn("new_expected_expr", ctx.exception.reason)

    def test_accepts_legacy_actual_expr_key(self):
        # allow either new_actual_expr or actual_expr (same for new_expected_expr)
        plans = self._call(
            '{"target_line": 3, "new_assert_type": "assertEquals", "actual_expr": "x", "expected_expr": "1"}'
        )
        self.assertEqual(len(plans), 1)


class TestCaptureReturnSchema(unittest.TestCase):
    def _call(self, params):
        return parse_plan_response(
            f'[{{"op": "CAPTURE_RETURN_VALUE", "params": {params}}}]', "NARV", ALL_OPS
        )

    def test_ok(self):
        self._call('{"target_line": 7, "var_name": "r", "var_type": "int"}')

    def test_rejects_void_type(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"target_line": 7, "var_name": "r", "var_type": "void"}')
        self.assertIn("void", ctx.exception.reason)

    def test_invalid_var_name(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"target_line": 7, "var_name": "1bad", "var_type": "int"}')
        self.assertIn("not a valid Java identifier", ctx.exception.reason)


class TestReplaceNullArgSchema(unittest.TestCase):
    def _call(self, params):
        return parse_plan_response(
            f'[{{"op": "REPLACE_NULL_ARG", "params": {params}}}]', "ENET", ALL_OPS
        )

    def test_ok(self):
        self._call('{"target_line": 5, "call_expr": "foo.bar", "arg_index": 0, "new_value": "\\"abc\\""}')

    def test_arg_index_negative(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"target_line": 5, "call_expr": "foo.bar", "arg_index": -1, "new_value": "\\"x\\""}')
        self.assertIn(">= 0", ctx.exception.reason)

    def test_empty_call_expr(self):
        with self.assertRaises(PlanParseError):
            self._call('{"target_line": 5, "call_expr": "", "arg_index": 0, "new_value": "\\"x\\""}')


class TestExtractToBeforeSchema(unittest.TestCase):
    def _call(self, params):
        return parse_plan_response(
            f'[{{"op": "EXTRACT_TO_BEFORE", "params": {params}}}]', "DS", ALL_OPS
        )

    def test_ok(self):
        self._call('{"target_methods": ["test1", "test2"]}')

    def test_needs_two_methods(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"target_methods": ["testA"]}')
        self.assertIn("at least 2", ctx.exception.reason)

    def test_non_string_item(self):
        with self.assertRaises(PlanParseError) as ctx:
            self._call('{"target_methods": ["testA", 42]}')
        self.assertIn("non-empty string", ctx.exception.reason)


class TestTryCatchSchema(unittest.TestCase):
    def test_try_catch_to_expected_rejects_fqn(self):
        out = '[{"op": "TRY_CATCH_TO_EXPECTED", "params": {"exception_type": "java.lang.NullPointerException"}}]'
        # dots are allowed in _SIMPLE_TYPE_RE, so FQN passes structural check;
        # semantic "must be simple name" is a Tier 1 concern. This documents the split.
        plans = parse_plan_response(out, "TSES", ALL_OPS)
        self.assertEqual(len(plans), 1)

    def test_exception_type_must_be_identifier_shape(self):
        out = '[{"op": "TRY_CATCH_TO_EXPECTED", "params": {"exception_type": "42Bad"}}]'
        with self.assertRaises(PlanParseError):
            parse_plan_response(out, "TSES", ALL_OPS)

    def test_remove_try_catch_ok_with_optional_flag(self):
        out = '[{"op": "REMOVE_TRY_CATCH_KEEP_BODY", "params": {"try_begin_line": 4, "drop_fail_call": false}}]'
        plans = parse_plan_response(out, "ENET", ALL_OPS)
        self.assertEqual(len(plans), 1)

    def test_remove_try_catch_drop_fail_wrong_type(self):
        out = '[{"op": "REMOVE_TRY_CATCH_KEEP_BODY", "params": {"try_begin_line": 4, "drop_fail_call": "yes"}}]'
        with self.assertRaises(PlanParseError):
            parse_plan_response(out, "ENET", ALL_OPS)


class TestRemoveTestExpected(unittest.TestCase):
    def test_empty_params_ok(self):
        plans = parse_plan_response(
            '[{"op": "REMOVE_TEST_EXPECTED", "params": {}}]', "AC", ALL_OPS
        )
        self.assertEqual(len(plans), 1)


if __name__ == "__main__":
    unittest.main()

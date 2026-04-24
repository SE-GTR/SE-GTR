from __future__ import annotations

import textwrap
import unittest

from smell_repair_v2.operators.base import (
    ExecutionContext,
    OperatorId,
    OperatorPlan,
)
from smell_repair_v2.operators.catalog import (
    count_assertions,
    get_operator_funcs,
)

METHOD_SIMPLE = textwrap.dedent(
    """\
    public void test0() throws Throwable {
        Foo foo = new Foo();
        foo.setValue(42);
        assertEquals(42, foo.getValue());
    }"""
)


def make_ctx(text: str = "") -> ExecutionContext:
    return ExecutionContext(
        method_name="test0",
        method_line_range=(1, len(text.splitlines())),
        file_text=text,
    )


def run_op(op_id: OperatorId, params: dict, text: str):
    plan = OperatorPlan(op=op_id, params=params, smell_id="TEST")
    ctx = make_ctx(text)
    funcs = get_operator_funcs(op_id)
    pre_ok, pre_reason = funcs["pre"](plan, text, ctx)
    if not pre_ok:
        return False, text, f"pre:{pre_reason}"
    new_text = funcs["apply"](plan, text, ctx)
    post_ok, post_reason = funcs["post"](plan, text, new_text, ctx)
    if not post_ok:
        return False, new_text, f"post:{post_reason}"
    return True, new_text, None


class TestInsertAssertion(unittest.TestCase):
    def test_insert_assert_true_after_call(self):
        ok, new, reason = run_op(
            OperatorId.INSERT_ASSERTION,
            {"after_line": 3, "assert_type": "assertTrue", "actual_expr": "foo.getValue() > 0"},
            METHOD_SIMPLE,
        )
        self.assertTrue(ok, reason)
        self.assertIn("assertTrue(foo.getValue() > 0);", new)
        self.assertEqual(count_assertions(new), 2)

    def test_insert_assert_equals_requires_expected(self):
        ok, _, reason = run_op(
            OperatorId.INSERT_ASSERTION,
            {"after_line": 3, "assert_type": "assertEquals", "actual_expr": "foo.getValue()"},
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("requires expected_expr", reason)

    def test_insert_assert_equals_with_message(self):
        ok, new, reason = run_op(
            OperatorId.INSERT_ASSERTION,
            {
                "after_line": 3,
                "assert_type": "assertEquals",
                "actual_expr": "foo.getValue()",
                "expected_expr": "42",
                "message": "must be 42",
            },
            METHOD_SIMPLE,
        )
        self.assertTrue(ok, reason)
        self.assertIn('assertEquals("must be 42", 42, foo.getValue());', new)

    def test_insert_after_line_out_of_range(self):
        ok, _, reason = run_op(
            OperatorId.INSERT_ASSERTION,
            {"after_line": 99, "assert_type": "assertTrue", "actual_expr": "foo != null"},
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("out of range", reason)

    def test_insert_reject_unknown_local(self):
        ok, _, reason = run_op(
            OperatorId.INSERT_ASSERTION,
            {"after_line": 3, "assert_type": "assertTrue", "actual_expr": "bar.stale()"},
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("does not reference", reason)

    def test_insert_fail_no_actual_needed(self):
        ok, new, reason = run_op(
            OperatorId.INSERT_ASSERTION,
            {"after_line": 3, "assert_type": "fail", "actual_expr": ""},
            METHOD_SIMPLE,
        )
        self.assertTrue(ok, reason)
        self.assertIn("fail();", new)

    def test_insert_preserves_indent(self):
        ok, new, _ = run_op(
            OperatorId.INSERT_ASSERTION,
            {"after_line": 3, "assert_type": "assertTrue", "actual_expr": "foo != null"},
            METHOD_SIMPLE,
        )
        self.assertTrue(ok)
        new_line = [ln for ln in new.splitlines() if "assertTrue(foo != null)" in ln][0]
        self.assertTrue(new_line.startswith("    "))


class TestRemoveAssertion(unittest.TestCase):
    def setUp(self) -> None:
        self.method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                Foo foo = new Foo();
                assertNotNull(foo);
                assertEquals(0, foo.getValue());
            }"""
        )

    def test_remove_assertion_ok(self):
        ok, new, reason = run_op(
            OperatorId.REMOVE_ASSERTION, {"target_line": 3}, self.method
        )
        self.assertTrue(ok, reason)
        self.assertNotIn("assertNotNull(foo)", new)
        self.assertEqual(count_assertions(new), 1)

    def test_remove_non_assert_rejected(self):
        ok, _, reason = run_op(
            OperatorId.REMOVE_ASSERTION, {"target_line": 2}, self.method
        )
        self.assertFalse(ok)
        self.assertIn("not an assertion", reason)

    def test_remove_last_assertion_rejected(self):
        single = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                assertTrue(true);
            }"""
        )
        ok, _, reason = run_op(OperatorId.REMOVE_ASSERTION, {"target_line": 2}, single)
        self.assertFalse(ok)
        self.assertIn("only 1 assertion left", reason)


class TestReplaceAssertion(unittest.TestCase):
    def test_replace_assert_not_null_with_equals(self):
        method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                Foo foo = new Foo();
                assertNotNull(foo);
            }"""
        )
        ok, new, reason = run_op(
            OperatorId.REPLACE_ASSERTION,
            {
                "target_line": 3,
                "new_assert_type": "assertEquals",
                "new_actual_expr": "foo.getValue()",
                "new_expected_expr": "0",
            },
            method,
        )
        self.assertTrue(ok, reason)
        self.assertNotIn("assertNotNull(foo)", new)
        self.assertIn("assertEquals(0, foo.getValue());", new)
        self.assertEqual(count_assertions(new), 1)

    def test_replace_rejects_non_assert_line(self):
        ok, _, reason = run_op(
            OperatorId.REPLACE_ASSERTION,
            {
                "target_line": 2,
                "new_assert_type": "assertTrue",
                "new_actual_expr": "foo != null",
            },
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("not an assertion", reason)


class TestInsertStatement(unittest.TestCase):
    def test_insert_statement_ok(self):
        ok, new, reason = run_op(
            OperatorId.INSERT_STATEMENT,
            {"after_line": 2, "statement": "foo.prepare();"},
            METHOD_SIMPLE,
        )
        self.assertTrue(ok, reason)
        self.assertIn("foo.prepare();", new)

    def test_insert_statement_missing_semicolon(self):
        ok, _, reason = run_op(
            OperatorId.INSERT_STATEMENT,
            {"after_line": 2, "statement": "foo.prepare()"},
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)

    def test_insert_statement_rejects_banned_junit5(self):
        ok, _, reason = run_op(
            OperatorId.INSERT_STATEMENT,
            {"after_line": 2, "statement": "assertThrows(IOException.class, () -> foo.go());"},
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("banned", reason)


class TestRemoveStatement(unittest.TestCase):
    def test_remove_statement_ok(self):
        ok, new, reason = run_op(
            OperatorId.REMOVE_STATEMENT, {"target_line": 3}, METHOD_SIMPLE
        )
        self.assertTrue(ok, reason)
        self.assertNotIn("foo.setValue(42);", new)

    def test_remove_brace_rejected(self):
        ok, _, reason = run_op(
            OperatorId.REMOVE_STATEMENT, {"target_line": 5}, METHOD_SIMPLE
        )
        self.assertFalse(ok)

    def test_remove_control_flow_rejected(self):
        method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                for (int i = 0; i < 3; i++) {
                    assertTrue(i >= 0);
                }
            }"""
        )
        ok, _, reason = run_op(OperatorId.REMOVE_STATEMENT, {"target_line": 2}, method)
        self.assertFalse(ok)


class TestReplaceExpression(unittest.TestCase):
    def test_replace_ok(self):
        ok, new, reason = run_op(
            OperatorId.REPLACE_EXPRESSION,
            {"target_line": 4, "old_expr": "foo.getValue()", "new_expr": "foo.getRealValue()"},
            METHOD_SIMPLE,
        )
        self.assertTrue(ok, reason)
        self.assertIn("foo.getRealValue()", new)

    def test_replace_missing_old_expr(self):
        ok, _, reason = run_op(
            OperatorId.REPLACE_EXPRESSION,
            {"target_line": 4, "old_expr": "bar.baz()", "new_expr": "foo.baz()"},
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("not present", reason)

    def test_replace_rejects_banned_new_expr(self):
        ok, _, reason = run_op(
            OperatorId.REPLACE_EXPRESSION,
            {
                "target_line": 4,
                "old_expr": "foo.getValue()",
                "new_expr": "assertThrows(IOException.class, () -> foo.getValue())",
            },
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("banned", reason)


class TestCaptureReturnValue(unittest.TestCase):
    def setUp(self) -> None:
        self.method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.compute();
                assertEquals(42, foo.getValue());
            }"""
        )

    def test_capture_ok(self):
        ok, new, reason = run_op(
            OperatorId.CAPTURE_RETURN_VALUE,
            {"target_line": 3, "var_name": "r", "var_type": "int"},
            self.method,
        )
        self.assertTrue(ok, reason)
        self.assertIn("int r = foo.compute();", new)

    def test_capture_rejects_assignment_line(self):
        ok, _, reason = run_op(
            OperatorId.CAPTURE_RETURN_VALUE,
            {"target_line": 2, "var_name": "x", "var_type": "Foo"},
            self.method,
        )
        self.assertFalse(ok)

    def test_capture_rejects_void_type(self):
        ok, _, reason = run_op(
            OperatorId.CAPTURE_RETURN_VALUE,
            {"target_line": 3, "var_name": "r", "var_type": "void"},
            self.method,
        )
        self.assertFalse(ok)
        self.assertIn("void", reason)

    def test_capture_rejects_redeclaration(self):
        ok, _, reason = run_op(
            OperatorId.CAPTURE_RETURN_VALUE,
            {"target_line": 3, "var_name": "foo", "var_type": "Foo"},
            self.method,
        )
        self.assertFalse(ok)
        self.assertIn("already declared", reason)


class TestReplaceNullArg(unittest.TestCase):
    def setUp(self) -> None:
        self.method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.setName((String) null);
                assertTrue(foo != null);
            }"""
        )

    def test_replace_null_with_literal(self):
        ok, new, reason = run_op(
            OperatorId.REPLACE_NULL_ARG,
            {"target_line": 3, "call_expr": "foo.setName", "arg_index": 0, "new_value": '"alice"'},
            self.method,
        )
        self.assertTrue(ok, reason)
        self.assertIn('foo.setName("alice");', new)

    def test_replace_null_arg_out_of_range(self):
        ok, _, reason = run_op(
            OperatorId.REPLACE_NULL_ARG,
            {"target_line": 3, "call_expr": "foo.setName", "arg_index": 2, "new_value": '"x"'},
            self.method,
        )
        self.assertFalse(ok)
        self.assertIn("arg_index", reason)

    def test_replace_null_when_arg_not_null(self):
        m2 = self.method.replace("(String) null", '"old"')
        ok, _, reason = run_op(
            OperatorId.REPLACE_NULL_ARG,
            {"target_line": 3, "call_expr": "foo.setName", "arg_index": 0, "new_value": '"x"'},
            m2,
        )
        self.assertFalse(ok)
        self.assertIn("not null", reason)


class TestAddSetupCall(unittest.TestCase):
    def test_add_setup_ok(self):
        method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                Foo foo = new Foo();
                assertEquals(0, foo.getValue());
            }"""
        )
        ok, new, reason = run_op(
            OperatorId.ADD_SETUP_CALL,
            {"statement": "Foo.reset();"},
            method,
        )
        self.assertTrue(ok, reason)
        self.assertIn("Foo.reset();", new)
        # must precede the first existing statement
        lines = new.splitlines()
        reset_idx = next(i for i, l in enumerate(lines) if "Foo.reset();" in l)
        foo_decl_idx = next(i for i, l in enumerate(lines) if "Foo foo = new Foo();" in l)
        self.assertLess(reset_idx, foo_decl_idx)


class TestAddTestExpected(unittest.TestCase):
    def test_add_expected_to_bare_test(self):
        method = textwrap.dedent(
            """\
            @Test
            public void test0() throws Throwable {
                foo.fail();
            }"""
        )
        ok, new, reason = run_op(
            OperatorId.ADD_TEST_EXPECTED,
            {"exception_type": "IllegalStateException"},
            method,
        )
        self.assertTrue(ok, reason)
        self.assertIn("@Test(expected = IllegalStateException.class)", new)

    def test_add_expected_when_already_present(self):
        method = textwrap.dedent(
            """\
            @Test(expected = IOException.class)
            public void test0() throws Throwable {
                foo.fail();
            }"""
        )
        ok, _, reason = run_op(
            OperatorId.ADD_TEST_EXPECTED,
            {"exception_type": "IllegalStateException"},
            method,
        )
        self.assertFalse(ok)
        self.assertIn("already", reason)

    def test_add_expected_no_annotation(self):
        method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                foo.fail();
            }"""
        )
        ok, _, reason = run_op(
            OperatorId.ADD_TEST_EXPECTED,
            {"exception_type": "IOException"},
            method,
        )
        self.assertFalse(ok)
        self.assertIn("no @Test", reason)


class TestRemoveTestExpected(unittest.TestCase):
    def test_remove_expected_ok(self):
        method = textwrap.dedent(
            """\
            @Test(expected = IOException.class)
            public void test0() throws Throwable {
                foo.fail();
            }"""
        )
        ok, new, reason = run_op(
            OperatorId.REMOVE_TEST_EXPECTED, {}, method
        )
        self.assertTrue(ok, reason)
        self.assertNotIn("expected", new)
        self.assertIn("@Test", new)

    def test_remove_expected_with_other_attrs(self):
        method = textwrap.dedent(
            """\
            @Test(expected = IOException.class, timeout = 1000)
            public void test0() throws Throwable {
                foo.fail();
            }"""
        )
        ok, new, reason = run_op(OperatorId.REMOVE_TEST_EXPECTED, {}, method)
        self.assertTrue(ok, reason)
        self.assertNotIn("expected", new)
        self.assertIn("timeout", new)

    def test_remove_expected_when_absent(self):
        method = textwrap.dedent(
            """\
            @Test
            public void test0() throws Throwable {
                foo.fail();
            }"""
        )
        ok, _, reason = run_op(OperatorId.REMOVE_TEST_EXPECTED, {}, method)
        self.assertFalse(ok)
        self.assertIn("does not have", reason)


class TestRemoveTryCatchKeepBody(unittest.TestCase):
    def test_remove_try_catch_keep_body_ok(self):
        method = textwrap.dedent(
            """\
            @Test
            public void test0() throws Throwable {
                Foo foo = new Foo();
                try {
                    foo.risky();
                    fail("should throw");
                } catch (IOException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        ok, new, reason = run_op(
            OperatorId.REMOVE_TRY_CATCH_KEEP_BODY,
            {"try_begin_line": 4, "drop_fail_call": True},
            method,
        )
        self.assertTrue(ok, reason)
        self.assertNotIn("try {", new)
        self.assertNotIn("catch", new)
        self.assertNotIn("fail(", new)
        self.assertIn("foo.risky();", new)

    def test_locate_try_missing(self):
        method = textwrap.dedent(
            """\
            @Test
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.go();
            }"""
        )
        ok, _, reason = run_op(
            OperatorId.REMOVE_TRY_CATCH_KEEP_BODY,
            {"try_begin_line": 3},
            method,
        )
        self.assertFalse(ok)


class TestTryCatchToExpected(unittest.TestCase):
    def setUp(self) -> None:
        self.method = textwrap.dedent(
            """\
            @Test
            public void test0() throws Throwable {
                Foo foo = new Foo();
                try {
                    foo.risky();
                    fail("expecting exception");
                } catch (NullPointerException e) {
                    verifyException("Foo", e);
                }
            }"""
        )

    def test_try_catch_to_expected_ok(self):
        ok, new, reason = run_op(
            OperatorId.TRY_CATCH_TO_EXPECTED,
            {"exception_type": "NullPointerException"},
            self.method,
        )
        self.assertTrue(ok, reason)
        self.assertIn("@Test(expected = NullPointerException.class)", new)
        self.assertNotIn("try {", new)
        self.assertNotIn("fail(", new)
        self.assertIn("foo.risky();", new)

    def test_reject_when_already_has_expected(self):
        method = self.method.replace("@Test", "@Test(expected = Throwable.class)")
        ok, _, reason = run_op(
            OperatorId.TRY_CATCH_TO_EXPECTED,
            {"exception_type": "NullPointerException"},
            method,
        )
        self.assertFalse(ok)

    def test_reject_when_two_try_blocks(self):
        m2 = self.method.replace(
            "foo.risky();",
            "foo.risky();\n        try { foo.go(); } catch (Exception e) {}",
        )
        ok, _, reason = run_op(
            OperatorId.TRY_CATCH_TO_EXPECTED,
            {"exception_type": "NullPointerException"},
            m2,
        )
        self.assertFalse(ok)


class TestWrapWithAssertThrows(unittest.TestCase):
    def test_always_rejected(self):
        ok, _, reason = run_op(
            OperatorId.WRAP_WITH_ASSERT_THROWS,
            {"exception_type": "IOException"},
            METHOD_SIMPLE,
        )
        self.assertFalse(ok)
        self.assertIn("JUnit 5", reason)


class TestExtractToBefore(unittest.TestCase):
    def setUp(self) -> None:
        self.file_text = textwrap.dedent(
            """\
            public class FooTest {

                @Test
                public void test0() throws Throwable {
                    Foo foo = new Foo();
                    foo.init();
                    assertEquals(0, foo.getValue());
                }

                @Test
                public void test1() throws Throwable {
                    Foo foo = new Foo();
                    foo.init();
                    assertEquals(1, foo.setValue(1));
                }
            }
            """
        )

    def test_extract_common_prefix(self):
        plan = OperatorPlan(
            op=OperatorId.EXTRACT_TO_BEFORE,
            params={"target_methods": ["test0", "test1"]},
            smell_id="DS",
        )
        ctx = make_ctx(self.file_text)
        funcs = get_operator_funcs(OperatorId.EXTRACT_TO_BEFORE)
        pre_ok, pre_reason = funcs["pre"](plan, self.file_text, ctx)
        self.assertTrue(pre_ok, pre_reason)
        new = funcs["apply"](plan, self.file_text, ctx)
        post_ok, post_reason = funcs["post"](plan, self.file_text, new, ctx)
        self.assertTrue(post_ok, post_reason)
        self.assertIn("@org.junit.Before", new)
        self.assertIn("public void setUp()", new)
        # both tests should have lost the prefix
        self.assertEqual(new.count("new Foo();"), 1)

    def test_reject_when_before_exists(self):
        txt = self.file_text.replace("public class FooTest {", "public class FooTest {\n    @org.junit.Before\n    public void setUp() {}")
        plan = OperatorPlan(
            op=OperatorId.EXTRACT_TO_BEFORE,
            params={"target_methods": ["test0", "test1"]},
            smell_id="DS",
        )
        ctx = make_ctx(txt)
        funcs = get_operator_funcs(OperatorId.EXTRACT_TO_BEFORE)
        ok, reason = funcs["pre"](plan, txt, ctx)
        self.assertFalse(ok)

    def test_accept_when_net_line_count_shrinks(self):
        """Large DS groups produce net-negative line deltas (lines removed
        from N tests > setUp block size). Postcondition must accept."""
        # Build a file with 10 tests sharing a 3-line prefix.
        setup_lines = "\n".join(
            f"        Foo foo{i} = new Foo();\n        foo{i}.init();\n        foo{i}.primary(42);"
            for i in range(1)
        )
        prefix = "        Foo foo = new Foo();\n        foo.init();\n        foo.primary(42);"
        methods = "\n\n".join(
            textwrap.dedent(f"""\
                @Test
                public void test{i:02d}() throws Throwable {{
            {prefix}
                    assertEquals({i}, foo.get());
                }}""")
            for i in range(10)
        )
        txt = "public class FooTest {\n\n" + methods + "\n}\n"

        plan = OperatorPlan(
            op=OperatorId.EXTRACT_TO_BEFORE,
            params={"target_methods": [f"test{i:02d}" for i in range(10)]},
            smell_id="DS",
        )
        ctx = make_ctx(txt)
        funcs = get_operator_funcs(OperatorId.EXTRACT_TO_BEFORE)
        pre_ok, _ = funcs["pre"](plan, txt, ctx)
        self.assertTrue(pre_ok)
        new = funcs["apply"](plan, txt, ctx)
        # Net change should be negative for 10 tests × 3 prefix lines
        self.assertLess(len(new.splitlines()), len(txt.splitlines()))
        post_ok, reason = funcs["post"](plan, txt, new, ctx)
        self.assertTrue(post_ok, reason)
        self.assertIn("@org.junit.Before", new)


if __name__ == "__main__":
    unittest.main()

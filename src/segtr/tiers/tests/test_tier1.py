from __future__ import annotations

import textwrap
import unittest

from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.tiers.tier1_deterministic import (
    get_method_return_type,
    get_tier1_plan,
    is_last_call_void,
    is_simple_try_catch_pattern,
    plan_ac_simple,
    plan_ds,
    plan_nna,
    plan_tses_simple,
    verify_common_prefix_in_file,
)


def make_ctx(
    *,
    method_name: str = "test0",
    method_start_line: int = 1,
    method_text: str = "",
    file_text: str = "",
    cut_fqcn: str | None = None,
    cut_source: str | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        method_name=method_name,
        method_line_range=(method_start_line, method_start_line + len(method_text.splitlines()) - 1),
        file_text=file_text or method_text,
        cut_fqcn=cut_fqcn,
        cut_source=cut_source,
    )


CUT_SOURCE_MIXED = textwrap.dedent(
    """\
    package foo;
    public class Foo {
        public void risky() { throw new RuntimeException(); }
        public String lookupName(int i) { return "x"; }
        public int compute() { return 42; }
        public static void reset() {}
    }
    """
)


class TestPlanNNA(unittest.TestCase):
    def test_plan_converts_file_lines_to_method_lines(self):
        method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                Foo foo = new Foo();
                assertNotNull(foo);
                assertEquals(0, foo.getValue());
            }"""
        )
        ev = {
            "redundant_not_null_assertions": [
                {"begin_line": 103, "assert": "assertNotNull(foo);", "variable": "foo"},
            ]
        }
        ctx = make_ctx(method_start_line=101, method_text=method)
        plans = plan_nna(ev, method, ctx)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].op, OperatorId.REMOVE_ASSERTION)
        # file line 103 - method start 101 + 1 = 3
        self.assertEqual(plans[0].params["target_line"], 3)

    def test_plan_descending_order_for_multiple(self):
        method = "a\nb\nc\nd\ne\n"
        ev = {
            "redundant_not_null_assertions": [
                {"begin_line": 2},
                {"begin_line": 4},
                {"begin_line": 3},
            ]
        }
        ctx = make_ctx(method_start_line=1, method_text=method)
        plans = plan_nna(ev, method, ctx)
        lines = [p.params["target_line"] for p in plans]
        self.assertEqual(lines, sorted(lines, reverse=True))

    def test_no_evidence_returns_empty(self):
        ctx = make_ctx(method_start_line=1, method_text="x\ny\n")
        self.assertEqual(plan_nna({}, "x\ny\n", ctx), [])


class TestPlanDS(unittest.TestCase):
    def _file_with_shared_prefix(self) -> str:
        return textwrap.dedent(
            """\
            public class FooTest {

              @Test public void testA() {
                Foo foo = new Foo();
                foo.init();
                assertEquals(0, foo.getValue());
              }

              @Test public void testB() {
                Foo foo = new Foo();
                foo.init();
                assertEquals(1, foo.getValue());
              }
            }
            """
        )

    def _file_without_shared_prefix(self) -> str:
        return textwrap.dedent(
            """\
            public class FooTest {

              @Test public void testA() {
                Foo foo = new Foo();
                assertEquals(0, foo.getValue());
              }

              @Test public void testB() {
                Bar bar = new Bar();
                assertEquals(1, bar.getValue());
              }
            }
            """
        )

    def test_emits_plan_when_prefix_verified(self):
        ev = {
            "duplicated_setup_groups": [
                {"group_size": 2, "group_tests": ["testA", "testB"]}
            ]
        }
        plans = plan_ds(ev, self._file_with_shared_prefix(), make_ctx())
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].op, OperatorId.EXTRACT_TO_BEFORE)

    def test_defers_when_prefix_not_shared(self):
        """Tier 1 returns [] — the smell is left for Tier 2 rather than
        being counted as a rejection."""
        ev = {
            "duplicated_setup_groups": [
                {"group_size": 2, "group_tests": ["testA", "testB"]}
            ]
        }
        plans = plan_ds(ev, self._file_without_shared_prefix(), make_ctx())
        self.assertEqual(plans, [])

    def test_skip_if_before_exists(self):
        ev = {
            "duplicated_setup_groups": [
                {"group_size": 2, "group_tests": ["testA", "testB"]}
            ]
        }
        file_text = self._file_with_shared_prefix().replace(
            "public class FooTest {",
            "public class FooTest {\n  @Before public void setUp() {}",
        )
        plans = plan_ds(ev, file_text, make_ctx())
        self.assertEqual(plans, [])

    def test_skip_tiny_group(self):
        ev = {"duplicated_setup_groups": [{"group_size": 1, "group_tests": ["testA"]}]}
        plans = plan_ds(ev, "class F {}", make_ctx())
        self.assertEqual(plans, [])

    def test_picks_largest_verified_group(self):
        ev = {
            "duplicated_setup_groups": [
                {"group_size": 5, "group_tests": ["testA", "testB"]},
                {"group_size": 3, "group_tests": ["testA", "testB"]},
            ]
        }
        plans = plan_ds(ev, self._file_with_shared_prefix(), make_ctx())
        self.assertEqual(len(plans), 1)


class TestVerifyCommonPrefix(unittest.TestCase):
    def test_positive(self):
        text = textwrap.dedent(
            """\
            class T {
              public void testA() {
                Foo foo = new Foo();
                foo.init();
                assertEquals(0, foo.getValue());
              }

              public void testB() {
                Foo foo = new Foo();
                foo.init();
                assertEquals(1, foo.getValue());
              }
            }
            """
        )
        self.assertTrue(verify_common_prefix_in_file(text, ["testA", "testB"]))

    def test_negative_different_first_lines(self):
        text = textwrap.dedent(
            """\
            class T {
              public void testA() {
                Foo foo = new Foo();
                assertEquals(0, foo.getValue());
              }
              public void testB() {
                Bar bar = new Bar();
                assertEquals(1, bar.getValue());
              }
            }
            """
        )
        self.assertFalse(verify_common_prefix_in_file(text, ["testA", "testB"]))

    def test_negative_only_one_shared_line(self):
        text = textwrap.dedent(
            """\
            class T {
              public void testA() {
                Foo foo = new Foo();
                foo.aaa();
                assertEquals(0, foo.getValue());
              }
              public void testB() {
                Foo foo = new Foo();
                foo.bbb();
                assertEquals(1, foo.getValue());
              }
            }
            """
        )
        self.assertFalse(
            verify_common_prefix_in_file(text, ["testA", "testB"], min_common_lines=2)
        )

    def test_method_missing_returns_false(self):
        text = "class T { public void testA() { Foo foo = new Foo(); } }"
        self.assertFalse(verify_common_prefix_in_file(text, ["testA", "missing"]))


class TestIsSimpleTryCatch(unittest.TestCase):
    def test_simple_pattern_accepted(self):
        method = textwrap.dedent(
            """\
            @Test
            public void test0() {
                try {
                    foo.risky();
                    fail("should throw");
                } catch (NullPointerException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        self.assertTrue(is_simple_try_catch_pattern(method))

    def test_two_try_blocks_rejected(self):
        method = textwrap.dedent(
            """\
            public void test0() {
                try { foo.a(); fail(""); } catch (Exception e) { verifyException("",e); }
                try { foo.b(); fail(""); } catch (Exception e) { verifyException("",e); }
            }"""
        )
        self.assertFalse(is_simple_try_catch_pattern(method))

    def test_catch_body_with_logic_rejected(self):
        method = textwrap.dedent(
            """\
            public void test0() {
                try {
                    foo.risky();
                    fail();
                } catch (IOException e) {
                    foo.cleanup();
                    verifyException("Foo", e);
                }
            }"""
        )
        self.assertFalse(is_simple_try_catch_pattern(method))

    def test_missing_fail_rejected(self):
        method = textwrap.dedent(
            """\
            public void test0() {
                try {
                    foo.risky();
                } catch (IOException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        self.assertFalse(is_simple_try_catch_pattern(method))


class TestPlanTSES(unittest.TestCase):
    def test_void_last_call_emits_plan(self):
        method = textwrap.dedent(
            """\
            public void test0() {
                try {
                    foo.risky();
                    fail("x");
                } catch (NullPointerException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        ev = {
            "same_exception_scenario_groups": [
                {"exception_type": "java.lang.NullPointerException"}
            ]
        }
        plans = plan_tses_simple(
            ev, method, make_ctx(method_text=method, cut_source=CUT_SOURCE_MIXED)
        )
        self.assertIsNotNone(plans)
        self.assertEqual(plans[0].op, OperatorId.TRY_CATCH_TO_EXPECTED)
        self.assertEqual(plans[0].params["exception_type"], "NullPointerException")

    def test_non_void_last_call_deferred(self):
        """Return-type is `String` — naked call would trip NARV → skip Tier 1."""
        method = textwrap.dedent(
            """\
            public void test0() {
                try {
                    foo.lookupName(3);
                    fail("x");
                } catch (NullPointerException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        ev = {"same_exception_scenario_groups": [{"exception_type": "NullPointerException"}]}
        plans = plan_tses_simple(
            ev, method, make_ctx(method_text=method, cut_source=CUT_SOURCE_MIXED)
        )
        self.assertIsNone(plans)

    def test_assignment_captures_return_value(self):
        """Assigning the return value shields it from NARV — accepted."""
        method = textwrap.dedent(
            """\
            public void test0() {
                try {
                    String s = foo.lookupName(3);
                    fail("x");
                } catch (NullPointerException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        ev = {"same_exception_scenario_groups": [{"exception_type": "NullPointerException"}]}
        plans = plan_tses_simple(
            ev, method, make_ctx(method_text=method, cut_source=CUT_SOURCE_MIXED)
        )
        self.assertIsNotNone(plans)

    def test_new_call_accepted(self):
        method = textwrap.dedent(
            """\
            public void test0() {
                try {
                    new Foo(-1);
                    fail("x");
                } catch (IllegalArgumentException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        ev = {"same_exception_scenario_groups": [{"exception_type": "IllegalArgumentException"}]}
        plans = plan_tses_simple(
            ev, method, make_ctx(method_text=method, cut_source=CUT_SOURCE_MIXED)
        )
        self.assertIsNotNone(plans)

    def test_cut_source_missing_is_conservative(self):
        """Without CUT source we can't resolve return type → defer."""
        method = textwrap.dedent(
            """\
            public void test0() {
                try {
                    foo.risky();
                    fail("x");
                } catch (NullPointerException e) {
                    verifyException("Foo", e);
                }
            }"""
        )
        ev = {"same_exception_scenario_groups": [{"exception_type": "NullPointerException"}]}
        plans = plan_tses_simple(ev, method, make_ctx(method_text=method))
        self.assertIsNone(plans)

    def test_complex_pattern_returns_none(self):
        method = textwrap.dedent(
            """\
            public void test0() {
                try { foo.a(); fail(""); } catch (Exception e) {}
                try { foo.b(); fail(""); } catch (Exception e) {}
            }"""
        )
        ev = {"same_exception_scenario_groups": [{"exception_type": "Exception"}]}
        plans = plan_tses_simple(
            ev, method, make_ctx(method_text=method, cut_source=CUT_SOURCE_MIXED)
        )
        self.assertIsNone(plans)


class TestGetMethodReturnType(unittest.TestCase):
    def test_void_resolved(self):
        self.assertEqual(get_method_return_type(CUT_SOURCE_MIXED, "risky"), "void")

    def test_reference_type_resolved(self):
        self.assertEqual(get_method_return_type(CUT_SOURCE_MIXED, "lookupName"), "String")

    def test_primitive_resolved(self):
        self.assertEqual(get_method_return_type(CUT_SOURCE_MIXED, "compute"), "int")

    def test_static_void(self):
        self.assertEqual(get_method_return_type(CUT_SOURCE_MIXED, "reset"), "void")

    def test_unknown_returns_none(self):
        self.assertIsNone(get_method_return_type(CUT_SOURCE_MIXED, "missingMethod"))


class TestIsLastCallVoid(unittest.TestCase):
    def _ctx(self) -> ExecutionContext:
        return make_ctx(cut_source=CUT_SOURCE_MIXED)

    def test_void_call_accepted(self):
        body = "foo.risky();\nfail(\"x\");"
        self.assertTrue(is_last_call_void(body, self._ctx()))

    def test_non_void_call_rejected(self):
        body = "foo.lookupName(3);\nfail(\"x\");"
        self.assertFalse(is_last_call_void(body, self._ctx()))

    def test_new_expression_accepted(self):
        body = "new Foo(-1);\nfail(\"x\");"
        self.assertTrue(is_last_call_void(body, self._ctx()))

    def test_assignment_accepted(self):
        body = "String s = foo.lookupName(3);\nfail(\"x\");"
        self.assertTrue(is_last_call_void(body, self._ctx()))

    def test_verify_exception_is_skipped(self):
        body = "foo.risky();\nfail(\"x\");\nverifyException(\"Foo\", null);"
        # even with trailing verifyException, last effective stmt is risky()
        self.assertTrue(is_last_call_void(body, self._ctx()))

    def test_missing_cut_source_rejected(self):
        body = "foo.risky();\nfail(\"x\");"
        self.assertFalse(is_last_call_void(body, make_ctx()))

    def test_empty_body_rejected(self):
        self.assertFalse(is_last_call_void("", self._ctx()))

    def test_only_fail_rejected(self):
        self.assertFalse(is_last_call_void("fail(\"x\");", self._ctx()))


class TestPlanAC(unittest.TestCase):
    def setUp(self):
        self.method = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                Foo foo = new Foo();
                assertEquals(2, Constants.MAX_LEN);
                assertEquals(0, foo.getValue());
            }"""
        )

    def test_unrelated_constant_removed(self):
        ev = {
            "constant_assertions": [
                {"begin_line": 3, "constant": "org.example.Constants.MAX_LEN"}
            ]
        }
        ctx = make_ctx(
            method_start_line=1, method_text=self.method, cut_fqcn="org.example.Foo"
        )
        plans = plan_ac_simple(ev, self.method, ctx)
        self.assertIsNotNone(plans)
        self.assertEqual(plans[0].op, OperatorId.REMOVE_ASSERTION)
        self.assertEqual(plans[0].params["target_line"], 3)

    def test_cut_related_constant_kept(self):
        ev = {
            "constant_assertions": [
                {"begin_line": 3, "constant": "org.example.Foo.DEFAULT"}
            ]
        }
        ctx = make_ctx(
            method_start_line=1, method_text=self.method, cut_fqcn="org.example.Foo"
        )
        plans = plan_ac_simple(ev, self.method, ctx)
        self.assertIsNone(plans)

    def test_skip_when_would_leave_no_assertions(self):
        single_assert = textwrap.dedent(
            """\
            public void test0() throws Throwable {
                assertEquals(2, Constants.MAX_LEN);
            }"""
        )
        ev = {
            "constant_assertions": [
                {"begin_line": 2, "constant": "org.example.Constants.MAX_LEN"}
            ]
        }
        ctx = make_ctx(method_start_line=1, method_text=single_assert, cut_fqcn="org.example.Foo")
        plans = plan_ac_simple(ev, single_assert, ctx)
        self.assertIsNone(plans)


class TestDispatcher(unittest.TestCase):
    def test_unknown_smell_returns_none(self):
        plans = get_tier1_plan(
            "ARPM",
            {},
            method_text="",
            file_text="",
            ctx=make_ctx(),
        )
        self.assertIsNone(plans)


if __name__ == "__main__":
    unittest.main()

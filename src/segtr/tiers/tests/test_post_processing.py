from __future__ import annotations

import textwrap
import unittest

from smell_repair_v2.operators.base import ExecutionContext, OperatorId
from smell_repair_v2.tiers.post_processing import apply_narv_guard


CUT_SOURCE = textwrap.dedent("""\
    package com.example;
    public class Foo {
        public int compute(int seed) { return seed * 2; }
        public String lookupName(int id) { return "name"; }
        public void reset() {}
        public boolean processMsg(int code) { return true; }
    }
""")


def _ctx(cut_source=CUT_SOURCE):
    return ExecutionContext(
        method_name="test0",
        method_line_range=(1, 10),
        file_text="",
        cut_source=cut_source,
    )


class TestNarvGuardTrigger(unittest.TestCase):
    def test_no_trigger_without_try_catch_op(self):
        method = textwrap.dedent("""\
            @Test
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.compute(1);
                assertEquals(0, foo.compute(0));
            }""")
        out, changes = apply_narv_guard(
            method, [OperatorId.INSERT_ASSERTION], _ctx()
        )
        self.assertEqual(out, method)
        self.assertEqual(changes, [])

    def test_no_trigger_without_cut_source(self):
        method = "foo.compute(1);"
        out, changes = apply_narv_guard(
            method,
            [OperatorId.REMOVE_TRY_CATCH_KEEP_BODY],
            _ctx(cut_source=None),
        )
        self.assertEqual(out, method)
        self.assertEqual(changes, [])


class TestNarvGuardCapture(unittest.TestCase):
    def test_captures_non_void_call(self):
        method = textwrap.dedent("""\
            @Test(expected = NullPointerException.class)
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.compute(1);
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.REMOVE_TRY_CATCH_KEEP_BODY],
            _ctx(),
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].method_name, "compute")
        self.assertEqual(changes[0].return_type, "int")
        self.assertIn("int _captured0 = foo.compute(1);", out)

    def test_skips_void_call(self):
        method = textwrap.dedent("""\
            @Test(expected = Exception.class)
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.reset();
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.TRY_CATCH_TO_EXPECTED],
            _ctx(),
        )
        self.assertEqual(changes, [])
        self.assertNotIn("_captured", out)

    def test_skips_constructor_call(self):
        method = textwrap.dedent("""\
            @Test(expected = Exception.class)
            public void test0() throws Throwable {
                new Foo();
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.REMOVE_TRY_CATCH_KEEP_BODY],
            _ctx(),
        )
        self.assertEqual(changes, [])

    def test_skips_existing_assignment(self):
        method = textwrap.dedent("""\
            @Test(expected = Exception.class)
            public void test0() throws Throwable {
                Foo foo = new Foo();
                int x = foo.compute(1);
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.REMOVE_TRY_CATCH_KEEP_BODY],
            _ctx(),
        )
        self.assertEqual(changes, [])

    def test_multiple_captures_get_distinct_names(self):
        method = textwrap.dedent("""\
            @Test(expected = Exception.class)
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.compute(1);
                foo.lookupName(2);
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.REMOVE_TRY_CATCH_KEEP_BODY],
            _ctx(),
        )
        self.assertEqual(len(changes), 2)
        self.assertIn("int _captured0 = foo.compute(1);", out)
        self.assertIn("String _captured1 = foo.lookupName(2);", out)

    def test_skips_assertions(self):
        method = textwrap.dedent("""\
            @Test(expected = Exception.class)
            public void test0() throws Throwable {
                Foo foo = new Foo();
                assertEquals(0, foo.compute(0));
                foo.compute(1);
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.REMOVE_TRY_CATCH_KEEP_BODY],
            _ctx(),
        )
        # only the naked call captured, not the assertEquals
        self.assertEqual(len(changes), 1)
        self.assertIn("int _captured0 = foo.compute(1);", out)
        self.assertIn("assertEquals(0, foo.compute(0));", out)

    def test_preserves_indentation(self):
        method = textwrap.dedent("""\
            @Test
            public void test0() throws Throwable {
                Foo foo = new Foo();
                foo.processMsg(53);
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.TRY_CATCH_TO_EXPECTED],
            _ctx(),
        )
        # indentation of original line (4 spaces) preserved
        captured_line = [l for l in out.splitlines() if "_captured" in l][0]
        self.assertTrue(captured_line.startswith("    "))

    def test_unresolvable_method_skipped(self):
        method = textwrap.dedent("""\
            @Test(expected = Exception.class)
            public void test0() throws Throwable {
                Bar bar = new Bar();
                bar.unknownMethod(1);
            }""")
        out, changes = apply_narv_guard(
            method,
            [OperatorId.REMOVE_TRY_CATCH_KEEP_BODY],
            _ctx(),
        )
        # unknownMethod not in CUT_SOURCE → skip
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()

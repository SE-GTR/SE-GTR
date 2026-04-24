"""Tests for the Gate 5 coverage proxy (`_gate5_coverage_proxy`).

The proxy is a cheap, regex-based stand-in for full JaCoCo per-plan. It
should:
 - NOT reject healthy transforms (CAPTURE, REPLACE_ASSERTION, try-catch
   removal, null replacement, @Test(expected=…) ) — low false-positive rate.
 - REJECT three specific "coverage collapse" patterns:
   * test methods deleted,
   * test body emptied (`{}` or comments only),
   * > 30 % statement loss within test bodies.
"""
from __future__ import annotations

import textwrap
import unittest

from smell_repair_v2.operators.validator import (
    _count_executable_statements,
    _count_test_methods,
    _gate5_coverage_proxy,
    _has_empty_test_body,
)


CLASS_WITH_ONE_TEST = textwrap.dedent("""\
    public class FooTest {
        @Test
        public void test0() throws Throwable {
            Foo foo = new Foo();
            foo.init();
            foo.setX(3);
            assertEquals(3, foo.getX());
        }
    }
""")

CLASS_WITH_TWO_TESTS = textwrap.dedent("""\
    public class FooTest {
        @Test
        public void test0() throws Throwable {
            Foo foo = new Foo();
            foo.init();
            foo.setX(3);
            assertEquals(3, foo.getX());
        }
        @Test
        public void test1() throws Throwable {
            Foo bar = new Foo();
            bar.setY(5);
            assertEquals(5, bar.getY());
        }
    }
""")


class TestProxyHealthyTransforms(unittest.TestCase):
    """Real operator outputs must NOT be rejected by the proxy."""

    def test_capture_return_value_ok(self):
        modified = CLASS_WITH_ONE_TEST.replace(
            "foo.setX(3);",
            "int ignored = foo.setX(3);",
        )
        ok, reason = _gate5_coverage_proxy(CLASS_WITH_ONE_TEST, modified)
        self.assertTrue(ok, reason)

    def test_try_catch_keep_body_ok(self):
        orig = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    try {
                        foo.risky();
                        fail("expected");
                    } catch (Exception e) {
                        verifyException("Foo", e);
                    }
                }
            }
        """)
        mod = textwrap.dedent("""\
            public class FooTest {
                @Test(expected = Exception.class) public void test0() {
                    Foo foo = new Foo();
                    foo.risky();
                }
            }
        """)
        ok, reason = _gate5_coverage_proxy(orig, mod)
        self.assertTrue(ok, reason)

    def test_replace_assertion_ok(self):
        mod = CLASS_WITH_ONE_TEST.replace(
            "assertEquals(3, foo.getX());",
            "assertEquals(6, foo.doubleX());",
        )
        ok, reason = _gate5_coverage_proxy(CLASS_WITH_ONE_TEST, mod)
        self.assertTrue(ok, reason)

    def test_insert_assertion_ok(self):
        mod = CLASS_WITH_ONE_TEST.replace(
            "assertEquals(3, foo.getX());",
            "assertEquals(3, foo.getX());\n        assertTrue(foo.isReady());",
        )
        ok, reason = _gate5_coverage_proxy(CLASS_WITH_ONE_TEST, mod)
        self.assertTrue(ok, reason)

    def test_narv_guard_capture_ok(self):
        orig = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    foo.compute(0);
                    assertEquals(0, foo.getSeed());
                }
            }
        """)
        mod = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    int _captured0 = foo.compute(0);
                    assertEquals(0, foo.getSeed());
                }
            }
        """)
        ok, reason = _gate5_coverage_proxy(orig, mod)
        self.assertTrue(ok, reason)

    def test_remove_single_redundant_nna_ok(self):
        """NNA repair removes one assertNotNull; executable statement count
        is unaffected and no tests disappear."""
        orig = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    assertNotNull(foo);
                    assertEquals(0, foo.getValue());
                }
            }
        """)
        mod = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    assertEquals(0, foo.getValue());
                }
            }
        """)
        ok, reason = _gate5_coverage_proxy(orig, mod)
        self.assertTrue(ok, reason)


class TestProxyCoverageCollapse(unittest.TestCase):
    """The proxy SHOULD reject the three degenerate patterns."""

    def test_test_method_deleted(self):
        mod = CLASS_WITH_TWO_TESTS.replace(
            "@Test\n    public void test1() throws Throwable {\n"
            "        Foo bar = new Foo();\n"
            "        bar.setY(5);\n"
            "        assertEquals(5, bar.getY());\n"
            "    }\n",
            "",
        )
        ok, reason = _gate5_coverage_proxy(CLASS_WITH_TWO_TESTS, mod)
        self.assertFalse(ok)
        self.assertIn("test_count_lost", reason)

    def test_empty_body_introduced(self):
        mod = CLASS_WITH_ONE_TEST.replace(
            "Foo foo = new Foo();\n"
            "        foo.init();\n"
            "        foo.setX(3);\n"
            "        assertEquals(3, foo.getX());\n",
            "",
        )
        ok, reason = _gate5_coverage_proxy(CLASS_WITH_ONE_TEST, mod)
        self.assertFalse(ok)
        self.assertIn("empty_body_introduced", reason)

    def test_statement_loss_rejected(self):
        """A method that drops >30% of its executable statements is rejected."""
        orig = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    foo.init();
                    foo.prepare();
                    foo.configure();
                    foo.run();
                    foo.finish();
                    assertEquals(1, foo.state());
                }
            }
        """)
        # Drop 4 of 6 executable statements (−67%)
        mod = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    foo.run();
                    assertEquals(1, foo.state());
                }
            }
        """)
        ok, reason = _gate5_coverage_proxy(orig, mod)
        self.assertFalse(ok)
        self.assertIn("statement_loss", reason)

    def test_statement_loss_at_threshold_ok(self):
        """A 28% drop (below −30%) must still be accepted."""
        orig = textwrap.dedent("""\
            public class FooTest {
                @Test public void test0() {
                    Foo foo = new Foo();
                    foo.a();
                    foo.b();
                    foo.c();
                    foo.d();
                    foo.e();
                    foo.f();
                    foo.g();
                    assertEquals(1, foo.state());
                }
            }
        """)
        mod = orig.replace("                    foo.f();\n", "")
        mod = mod.replace("                    foo.g();\n", "")
        # 7 → 5 = -28.6% — should be accepted (threshold is -30%)
        ok, reason = _gate5_coverage_proxy(orig, mod)
        self.assertTrue(ok, reason)


class TestProxyInternals(unittest.TestCase):
    def test_count_test_methods(self):
        self.assertEqual(_count_test_methods(CLASS_WITH_ONE_TEST), 1)
        self.assertEqual(_count_test_methods(CLASS_WITH_TWO_TESTS), 2)
        self.assertEqual(_count_test_methods(""), 0)

    def test_count_executable_statements_ignores_assertions(self):
        n = _count_executable_statements(CLASS_WITH_ONE_TEST)
        # 3 executable stmts: new Foo(), init(), setX(3). assertEquals excluded.
        self.assertEqual(n, 3)

    def test_empty_body_detection(self):
        empty = textwrap.dedent("""\
            public class X {
                @Test public void test0() {
                }
            }
        """)
        self.assertTrue(_has_empty_test_body(empty))
        self.assertFalse(_has_empty_test_body(CLASS_WITH_ONE_TEST))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from smell_repair_v2.operators.validator import (
    MultiGateValidator,
    ValidatorConfig,
    _strip_strings_and_comments,
)


class TestValidatorGates(unittest.TestCase):
    def _make_validator(self, test_file: Path) -> MultiGateValidator:
        cfg = ValidatorConfig(
            project_root=test_file.parent,
            test_file=test_file,
            skip_compile=True,
            skip_tests=True,
        )
        return MultiGateValidator(cfg)

    def test_gate1_banned_new_call_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = "assertTrue(true);\n"
            modified = "assertThrows(IOException.class, () -> {});\n"
            tf.write_text(original)
            v = self._make_validator(tf)
            ok, reason = v.validate(original, modified)
            self.assertFalse(ok)
            self.assertIn("gate1", reason)

    def test_gate1_banned_new_import_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = "import org.junit.Test;\n"
            modified = "import org.junit.Test;\nimport org.junit.jupiter.api.Test;\n"
            tf.write_text(original)
            v = self._make_validator(tf)
            ok, reason = v.validate(original, modified)
            self.assertFalse(ok)
            self.assertIn("gate1", reason)

    def test_gate2_syntax_unbalanced_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = "public void test0() { assertTrue(true); }"
            modified = "public void test0() { assertTrue(true);"  # missing }
            tf.write_text(original)
            v = self._make_validator(tf)
            ok, reason = v.validate(original, modified)
            self.assertFalse(ok)
            self.assertIn("gate2", reason)

    def test_gate6_nna_introduction_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = textwrap.dedent(
                """\
                public void test0() {
                    Foo foo = new Foo();
                    assertEquals(0, foo.getValue());
                }"""
            )
            modified = textwrap.dedent(
                """\
                public void test0() {
                    Foo foo = new Foo();
                    assertNotNull(foo);
                    assertEquals(0, foo.getValue());
                }"""
            )
            tf.write_text(original)
            v = self._make_validator(tf)
            ok, reason = v.validate(original, modified)
            self.assertFalse(ok)
            self.assertIn("gate6", reason)

    def test_gate7_meaningful_assertion_loss_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = "assertTrue(true); assertFalse(false); assertEquals(1,1);"
            modified = "assertTrue(true);"
            tf.write_text(original)
            v = self._make_validator(tf)
            ok, reason = v.validate(original, modified)
            self.assertFalse(ok)
            self.assertIn("gate7", reason)

    def test_gate7_no_assertions_left_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = "assertNotNull(foo);"
            modified = "// nothing"
            tf.write_text(original)
            v = self._make_validator(tf)
            ok, reason = v.validate(original, modified)
            self.assertFalse(ok)
            self.assertIn("gate7", reason)

    def test_accept_when_all_gates_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = textwrap.dedent(
                """\
                public void test0() {
                    Foo foo = new Foo();
                    assertNotNull(foo);
                    assertEquals(0, foo.getValue());
                }"""
            )
            modified = textwrap.dedent(
                """\
                public void test0() {
                    Foo foo = new Foo();
                    assertEquals(0, foo.getValue());
                }"""
            )
            tf.write_text(original)
            v = self._make_validator(tf)
            ok, reason = v.validate(original, modified)
            self.assertTrue(ok, reason)
            self.assertEqual(reason, "accepted")
            # validator should have written modified to disk
            self.assertEqual(tf.read_text(), modified)

    def test_restore_on_syntax_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / "FooTest.java"
            original = "public void test0() { assertTrue(true); }"
            modified = "public void test0() { assertTrue(true);"
            tf.write_text(original)
            v = self._make_validator(tf)
            v.validate(original, modified)
            # gate 2 runs before any file write; file should still be original
            self.assertEqual(tf.read_text(), original)


class TestStripStringsAndComments(unittest.TestCase):
    def test_strip_line_comment(self):
        src = 'int x = 1; // comment with ) unbalanced'
        stripped = _strip_strings_and_comments(src)
        self.assertEqual(stripped.count(")"), 0)

    def test_strip_block_comment(self):
        src = 'int x = 1; /* hidden } unbalanced */ int y = 2;'
        stripped = _strip_strings_and_comments(src)
        self.assertEqual(stripped.count("}"), 0)

    def test_strip_string_literal(self):
        src = 'String s = "closing ) here"; int x = 1;'
        stripped = _strip_strings_and_comments(src)
        self.assertEqual(stripped.count(")"), 0)


if __name__ == "__main__":
    unittest.main()

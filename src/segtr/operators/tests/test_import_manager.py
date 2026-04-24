from __future__ import annotations

import textwrap
import unittest

from smell_repair_v2.operators.import_manager import ImportManager


class TestImportManager(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = ImportManager()

    def test_add_missing_assert_equals(self):
        text = textwrap.dedent(
            """\
            package foo;

            import org.junit.Test;

            public class FooTest {
                @Test public void test0() { assertEquals(1, 1); }
            }
            """
        )
        new, notes = self.mgr.reconcile(text, {"assertEquals"})
        self.assertIn("import static org.junit.Assert.assertEquals;", new)
        self.assertIn("added:org.junit.Assert.assertEquals", notes)

    def test_no_add_when_wildcard_present(self):
        text = textwrap.dedent(
            """\
            package foo;

            import static org.junit.Assert.*;
            import org.junit.Test;

            public class FooTest {}
            """
        )
        new, notes = self.mgr.reconcile(text, {"assertEquals", "assertTrue"})
        self.assertEqual(new, text)
        self.assertEqual(notes, [])

    def test_no_add_when_specific_import_already_present(self):
        text = textwrap.dedent(
            """\
            package foo;

            import static org.junit.Assert.assertEquals;
            import org.junit.Test;
            """
        )
        new, _ = self.mgr.reconcile(text, {"assertEquals"})
        # should not duplicate
        self.assertEqual(new.count("import static org.junit.Assert.assertEquals;"), 1)

    def test_strip_banned_junit5_import(self):
        text = textwrap.dedent(
            """\
            package foo;

            import org.junit.jupiter.api.Test;
            import org.junit.Test;

            public class FooTest {}
            """
        )
        new, notes = self.mgr.reconcile(text, set(), original_imports=set())
        self.assertNotIn("org.junit.jupiter.api.Test", new)
        self.assertIn("org.junit.Test", new)
        self.assertTrue(any("stripped_banned" in n for n in notes))

    def test_preserve_banned_if_originally_present(self):
        text = textwrap.dedent(
            """\
            package foo;

            import org.junit.jupiter.api.Test;

            public class FooTest {}
            """
        )
        new, notes = self.mgr.reconcile(
            text, set(), original_imports={"org.junit.jupiter.api.Test"}
        )
        self.assertIn("org.junit.jupiter.api.Test", new)

    def test_check_banned_calls(self):
        text = "assertThrows(IOException.class, () -> foo.run());"
        hits = self.mgr.check_banned_calls(text)
        self.assertEqual(hits, ["assertThrows"])

    def test_insert_after_existing_imports(self):
        text = textwrap.dedent(
            """\
            package foo;

            import java.util.List;
            import org.junit.Test;

            public class FooTest {}
            """
        )
        new, _ = self.mgr.reconcile(text, {"assertTrue"})
        # new import must appear after the last existing import
        idx_junit_test = new.index("import org.junit.Test;")
        idx_assert = new.index("assertTrue;")
        self.assertGreater(idx_assert, idx_junit_test)


if __name__ == "__main__":
    unittest.main()

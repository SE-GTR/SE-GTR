"""Unit tests for the source-level pieces of DynamicContextCollector —
getter extraction, instrumentation codegen, injection, stdout parsing.

End-to-end compile+run is exercised by the standalone checkpoint script
(``scripts/dynamic_capture_checkpoint.py``) since it needs a real Java
toolchain.
"""
from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from smell_repair_v2.dynamic.collector import (
    DynamicEvidence,
    GetterInfo,
    build_instrumentation_block,
    collect_observable_getters,
    extract_observable_getters,
    inject_at_line,
    parse_markers,
    resolve_parent_fqcn,
)


CUT_SOURCE = textwrap.dedent("""\
    package com.example;

    public class Foo {
        private int value;
        private boolean ready;

        public Foo() { }

        public int getValue() { return value; }

        public boolean isReady() { return ready; }

        public boolean hasValue(int v) { return value == v; }  // non-zero params → skip

        public static String getInstanceName() { return "F"; }  // static → skip

        private int getPrivate() { return 0; }                   // private → skip

        public void setValue(int v) { this.value = v; }          // void → skip

        public String toString() { return "Foo(" + value + ")"; }

        public int size() { return 1; }
    }
""")


class TestExtractObservableGetters(unittest.TestCase):
    def test_picks_public_zero_arg_non_void(self):
        getters = extract_observable_getters(CUT_SOURCE)
        names = [g.name for g in getters]
        self.assertIn("getValue", names)
        self.assertIn("isReady", names)
        self.assertIn("toString", names)
        self.assertIn("size", names)
        # filtered:
        self.assertNotIn("setValue", names)     # void
        self.assertNotIn("getInstanceName", names)  # static
        self.assertNotIn("getPrivate", names)   # private
        self.assertNotIn("hasValue", names)     # has but takes a param

    def test_return_types_correct(self):
        g = {x.name: x for x in extract_observable_getters(CUT_SOURCE)}
        self.assertEqual(g["getValue"].return_type, "int")
        self.assertEqual(g["isReady"].return_type, "boolean")
        self.assertEqual(g["toString"].return_type, "String")

    def test_max_getters_cap(self):
        # Note: our name regex requires an uppercase letter after `get`/`is`
        # (e.g. `getX`), so we use letter-suffixed names for this fixture.
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        big = "\n".join(
            f"public int get{letters[i]}() {{ return {i}; }}" for i in range(20)
        )
        src = "public class X {\n" + big + "\n}"
        getters = extract_observable_getters(src, max_getters=5)
        self.assertEqual(len(getters), 5)

    def test_empty_source(self):
        self.assertEqual(extract_observable_getters(""), [])


class TestBuildInstrumentationBlock(unittest.TestCase):
    def test_before_block_has_correct_marker(self):
        block = build_instrumentation_block(
            "foo", [GetterInfo("getValue", "int", 8)], phase="before", indent=""
        )
        self.assertIn("SE-GTR-DIFF-BEFORE:", block)
        self.assertIn('foo.getValue()', block)
        self.assertIn("__segtr_before", block)
        self.assertNotIn("__segtr_after", block)

    def test_after_block_uses_different_marker(self):
        block = build_instrumentation_block(
            "foo", [GetterInfo("getValue", "int", 8)], phase="after", indent=""
        )
        self.assertIn("SE-GTR-DIFF-AFTER:", block)
        self.assertIn("__segtr_after", block)

    def test_each_getter_wrapped_in_try_catch(self):
        block = build_instrumentation_block(
            "foo",
            [GetterInfo("getValue", "int", 8), GetterInfo("isReady", "boolean", 10)],
            phase="before", indent="",
        )
        # 2 getters → 2 inner try/catches plus outer one = 3 try occurrences
        self.assertEqual(block.count("try {"), 3)
        self.assertEqual(block.count("} catch"), 3)

    def test_invalid_phase_raises(self):
        with self.assertRaises(ValueError):
            build_instrumentation_block("foo", [], phase="middle", indent="")

    def test_empty_getters_emits_valid_java(self):
        block = build_instrumentation_block("foo", [], phase="before", indent="")
        self.assertIn("LinkedHashMap", block)
        self.assertIn("for ", block)


class TestInjectAtLine(unittest.TestCase):
    def test_inject_preserves_target_line(self):
        src = "line1\nline2\nline3\n"
        out = inject_at_line(src, act_call_line=2, before_code="// B", after_code="// A")
        expected = "line1\n// B\nline2\n// A\nline3\n"
        self.assertEqual(out, expected)

    def test_inject_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            inject_at_line("x\n", 5, "// B", "// A")

    def test_inject_at_first_line(self):
        src = "first\nsecond\n"
        out = inject_at_line(src, 1, "// B", "// A")
        self.assertEqual(out, "// B\nfirst\n// A\nsecond\n")

    def test_inject_block_ends_with_newline_even_if_not_given(self):
        src = "x\ny\n"
        out = inject_at_line(src, 1, "no_newline", "also_no_newline")
        self.assertIn("no_newline\n", out)
        self.assertIn("also_no_newline\n", out)


class TestParseMarkers(unittest.TestCase):
    def test_simple_markers(self):
        stdout = textwrap.dedent("""\
            Test output preamble
            SE-GTR-DIFF-BEFORE:getValue()=0
            SE-GTR-DIFF-BEFORE:isReady()=false
            Act call ran
            SE-GTR-DIFF-AFTER:getValue()=42
            SE-GTR-DIFF-AFTER:isReady()=true
            Done.
        """)
        before, after, errors = parse_markers(stdout)
        self.assertEqual(before, {"getValue()": "0", "isReady()": "false"})
        self.assertEqual(after, {"getValue()": "42", "isReady()": "true"})
        self.assertEqual(errors, [])

    def test_err_tokens(self):
        stdout = "SE-GTR-DIFF-BEFORE:getValue()=ERR:NullPointerException\n"
        before, after, errors = parse_markers(stdout)
        self.assertEqual(before["getValue()"], "ERR:NullPointerException")

    def test_phase_errors(self):
        stdout = "SE-GTR-DIFF-ERROR:after:SomeException\n"
        before, after, errors = parse_markers(stdout)
        self.assertEqual(errors, ["after:SomeException"])
        self.assertEqual(before, {})

    def test_no_markers(self):
        before, after, errors = parse_markers("just normal junit output\n")
        self.assertEqual(before, {})
        self.assertEqual(after, {})
        self.assertEqual(errors, [])


class TestDynamicEvidenceChangedFields(unittest.TestCase):
    def test_diff(self):
        ev = DynamicEvidence(
            state_before={"getValue()": "0", "isReady()": "false"},
            state_after={"getValue()": "42", "isReady()": "false"},
        )
        diff = ev.changed_fields()
        self.assertEqual(diff, {"getValue()": ("0", "42")})

    def test_to_dict_includes_changed_fields(self):
        ev = DynamicEvidence(
            state_before={"a()": "x"},
            state_after={"a()": "y"},
            capture_success=True,
        )
        d = ev.to_dict()
        self.assertIn("changed_fields", d)
        self.assertEqual(d["changed_fields"]["a()"], {"before": "x", "after": "y"})


class TestResolveParentFqcn(unittest.TestCase):
    def test_extends_with_import(self):
        src = textwrap.dedent("""\
            package foo.bar;
            import java.util.List;
            import pkg.base.Parent;
            public class Child extends Parent {}
        """)
        self.assertEqual(resolve_parent_fqcn(src), "pkg.base.Parent")

    def test_extends_same_package(self):
        src = textwrap.dedent("""\
            package foo.bar;
            public class Child extends Sibling {
                public int getX() { return 0; }
            }
        """)
        self.assertEqual(resolve_parent_fqcn(src), "foo.bar.Sibling")

    def test_no_extends_returns_none(self):
        src = "package foo; public class Standalone {}"
        self.assertIsNone(resolve_parent_fqcn(src))

    def test_generic_extends(self):
        src = textwrap.dedent("""\
            package p;
            public class Box<T> extends AbstractBox<T> {}
        """)
        self.assertEqual(resolve_parent_fqcn(src), "p.AbstractBox")


class TestCollectObservableGetters(unittest.TestCase):
    def test_cut_only_when_no_src_root(self):
        src = textwrap.dedent("""\
            package p;
            public class A extends Parent {
                public int getA() { return 0; }
            }
        """)
        getters, info = collect_observable_getters(src, project_src_root=None)
        self.assertEqual([g.name for g in getters], ["getA"])
        self.assertEqual(info["cut_count"], 1)
        self.assertEqual(info["parent_count"], 0)

    def test_merges_parent_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "p").mkdir()
            (root / "p" / "Parent.java").write_text(textwrap.dedent("""\
                package p;
                public class Parent {
                    public int getParentX() { return 1; }
                    public boolean isReady() { return true; }
                }
            """))
            child_src = textwrap.dedent("""\
                package p;
                public class Child extends Parent {
                    public int getChildY() { return 2; }
                }
            """)
            getters, info = collect_observable_getters(child_src, project_src_root=root)
            names = [g.name for g in getters]
            self.assertIn("getChildY", names)
            self.assertIn("getParentX", names)
            self.assertIn("isReady", names)
            by_name = {g.name: g for g in getters}
            self.assertEqual(by_name["getChildY"].source, "cut")
            self.assertEqual(by_name["getParentX"].source, "parent:p.Parent")
            self.assertEqual(info["parent_fqcn"], "p.Parent")
            self.assertEqual(info["cut_count"], 1)
            self.assertEqual(info["parent_count"], 2)

    def test_child_shadows_parent_same_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "p").mkdir()
            (root / "p" / "Parent.java").write_text(
                "package p; public class Parent { public int getValue() { return 0; } }"
            )
            child_src = textwrap.dedent("""\
                package p;
                public class Child extends Parent {
                    public int getValue() { return 99; }
                }
            """)
            getters, _ = collect_observable_getters(child_src, project_src_root=root)
            by_name = {g.name: g.source for g in getters}
            self.assertEqual(by_name["getValue"], "cut")

    def test_jdk_parent_skipped(self):
        src = textwrap.dedent("""\
            package p;
            import java.util.ArrayList;
            public class MyList extends ArrayList {
                public int getExtra() { return 0; }
            }
        """)
        with tempfile.TemporaryDirectory() as td:
            getters, info = collect_observable_getters(src, project_src_root=Path(td))
            self.assertEqual([g.name for g in getters], ["getExtra"])
            self.assertEqual(info["skipped_parent"], "jdk:java.util.ArrayList")

    def test_parent_source_missing_is_not_error(self):
        src = textwrap.dedent("""\
            package p;
            public class Orphan extends Nowhere {
                public int getSelf() { return 0; }
            }
        """)
        with tempfile.TemporaryDirectory() as td:
            getters, info = collect_observable_getters(src, project_src_root=Path(td))
            self.assertEqual([g.name for g in getters], ["getSelf"])
            self.assertEqual(info["skipped_parent"], "source_not_found")
            self.assertEqual(info["parent_fqcn"], "p.Nowhere")

    def test_max_getters_respected_across_cut_plus_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "p").mkdir()
            letters = "ABCDEFGHIJKL"
            parent_methods = "\n".join(
                f"    public int get{c}() {{ return 0; }}" for c in letters
            )
            (root / "p" / "Parent.java").write_text(
                f"package p;\npublic class Parent {{\n{parent_methods}\n}}\n"
            )
            child_src = (
                "package p;\npublic class Child extends Parent {\n"
                "    public int getChildOne() { return 0; }\n"
                "    public int getChildTwo() { return 0; }\n"
                "}\n"
            )
            getters, info = collect_observable_getters(
                child_src, project_src_root=root, max_getters=5,
            )
            self.assertEqual(len(getters), 5)
            # First two are CUT, next three are parent.
            self.assertEqual(info["cut_count"], 2)
            self.assertEqual(info["parent_count"], 3)


if __name__ == "__main__":
    unittest.main()

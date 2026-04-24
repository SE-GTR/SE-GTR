"""Tier 2 (template-guided) few-shot examples.

`test_method` is stored raw (no line-number prefixes). `build_plan_messages`
renders it with line numbers at prompt-assembly time, so the line numbers
in `expected_plan` refer to the RAW method (1-indexed, first line = 1).

Each example's `expected_plan` is validated in
`llm/tests/test_plan_fewshot_valid.py` by running it through the real
OperatorExecutor against `test_method`.
"""
from __future__ import annotations

from typing import Any, Dict, List


TIER2_EXAMPLES: Dict[str, List[Dict[str, Any]]] = {
    "ENET": [
        # ENET[0] — String null, try/catch scaffolding strips cleanly.
        {
            "description": "String-null argument replaced with empty string; try/catch removed.",
            "evidence_summary": (
                "null_argument_sites: ["
                "{arg_index: 0, arg_expr: null, kind: method_call}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test05() throws Throwable {\n"
                "    Parser parser = new Parser();\n"
                "    try {\n"
                "        parser.parse(null);\n"
                "        fail(\"Expecting exception: NullPointerException\");\n"
                "    } catch (NullPointerException e) {\n"
                "        verifyException(\"Parser\", e);\n"
                "    }\n"
                "    assertNotNull(parser);\n"
                "}"
            ),
            "cut_context": "public class Parser { public Document parse(String html); }",
            "expected_plan": [
                {
                    "op": "REPLACE_NULL_ARG",
                    "params": {
                        "target_line": 5,
                        "call_expr": "parser.parse",
                        "arg_index": 0,
                        "new_value": "\"\"",
                    },
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "Empty-string is the minimal valid String argument. After the null "
                "is replaced, the exception won't be thrown, so the try/catch is "
                "useless scaffolding and is stripped (fail() included)."
            ),
        },
        # ENET[1] — boxed primitive (Integer) null replaced with a literal.
        {
            "description": "Boxed-Integer null replaced with literal; try/catch removed.",
            "evidence_summary": (
                "null_argument_sites: [{arg_index: 0, arg_expr: (Integer) null, kind: method_call}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test08() throws Throwable {\n"
                "    IntStack stack = new IntStack();\n"
                "    try {\n"
                "        stack.push((Integer) null);\n"
                "        fail(\"Expecting NullPointerException\");\n"
                "    } catch (NullPointerException e) {\n"
                "        verifyException(\"IntStack\", e);\n"
                "    }\n"
                "    assertNotNull(stack);\n"
                "}"
            ),
            "cut_context": "public class IntStack { public void push(Integer v); public int size(); }",
            "expected_plan": [
                {
                    "op": "REPLACE_NULL_ARG",
                    "params": {
                        "target_line": 5,
                        "call_expr": "stack.push",
                        "arg_index": 0,
                        "new_value": "42",
                    },
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "For primitive/boxed numeric types, an arbitrary valid literal "
                "(0, 1, 42) works; the test's intent is that `push` succeeds — "
                "not that a specific value is used."
            ),
        },
    ],
    "EDIS": [
        # EDIS[0] — static/global reset via ADD_SETUP_CALL.
        {
            "description": "Missing global reset — prepend with ADD_SETUP_CALL; drop try/catch.",
            "evidence_summary": (
                "trigger_call: cfg.apply()  unmodified_variable: Config.INITIALISED (static)"
            ),
            "test_method": (
                "@Test\n"
                "public void test04() throws Throwable {\n"
                "    Config cfg = Config.getInstance();\n"
                "    try {\n"
                "        cfg.apply();\n"
                "        fail(\"Expecting IllegalStateException\");\n"
                "    } catch (IllegalStateException e) {\n"
                "        verifyException(\"Config\", e);\n"
                "    }\n"
                "}"
            ),
            "cut_context": (
                "public class Config {\n"
                "  public static Config getInstance();\n"
                "  public static void reset();\n"
                "  public void apply();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "ADD_SETUP_CALL",
                    "params": {"statement": "Config.reset();"},
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "`ADD_SETUP_CALL` can only inject at the method top, so it is "
                "suitable for static/singleton resets. Instance-level setup that "
                "depends on an earlier local declaration is NOT solvable with this "
                "operator alone — fall back to `ADD_TEST_EXPECTED` if no clean "
                "setup exists."
            ),
        },
        # EDIS[1] — acknowledge the exception as expected via ADD_TEST_EXPECTED.
        {
            "description": "Exception IS the expected behaviour — convert to @Test(expected=X.class).",
            "evidence_summary": (
                "trigger_call: conn.send(\"data\")  documented_contract: requires connect() first"
            ),
            "test_method": (
                "@Test\n"
                "public void test05() throws Throwable {\n"
                "    Connection conn = new Connection();\n"
                "    try {\n"
                "        conn.send(\"data\");\n"
                "        fail(\"Expecting IllegalStateException\");\n"
                "    } catch (IllegalStateException e) {\n"
                "        verifyException(\"Connection\", e);\n"
                "    }\n"
                "}"
            ),
            "cut_context": (
                "public class Connection {\n"
                "  public void connect();\n"
                "  public void send(String data);  // throws IllegalStateException if not connected\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "ADD_TEST_EXPECTED",
                    "params": {"exception_type": "IllegalStateException"},
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "When the documented contract says the method SHOULD throw without "
                "prior setup, the exception is the assertion — use "
                "@Test(expected=…) and keep the failing call naked. Safe because "
                "`conn.send()` returns void; no NARV risk."
            ),
        },
    ],
    "EDED": [
        # EDED[0] — external I/O acknowledged as expected.
        {
            "description": "External I/O throws — accept it as the expected test outcome.",
            "evidence_summary": (
                "external_dependency_exceptions: ["
                "{matched_exception_type: IOException, kind: network_access}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test01() throws Throwable {\n"
                "    HttpClient client = new HttpClient();\n"
                "    try {\n"
                "        client.fetch(\"http://example.com\");\n"
                "        fail(\"Expecting IOException\");\n"
                "    } catch (IOException e) {\n"
                "        verifyException(\"HttpClient\", e);\n"
                "    }\n"
                "}"
            ),
            "cut_context": (
                "public class HttpClient {\n"
                "  public void fetch(String url) throws IOException;\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "ADD_TEST_EXPECTED",
                    "params": {"exception_type": "IOException"},
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "Void return — @Test(expected=IOException.class) is the cleanest "
                "transformation and preserves the original test intent (I/O "
                "unreachable in test env)."
            ),
        },
        # EDED[1] — replace the external call with a deterministic alternative.
        {
            "description": "Swap external call for a local equivalent; drop try/catch.",
            "evidence_summary": (
                "external_dependency_exceptions: [{matched_exception_type: IOException}]  "
                "cut_has_local_alternative: loadDefaultConfig"
            ),
            "test_method": (
                "@Test\n"
                "public void test09() throws Throwable {\n"
                "    Calculator calc = new Calculator();\n"
                "    try {\n"
                "        calc.fetchRemoteConfig();\n"
                "        fail(\"Expecting IOException\");\n"
                "    } catch (IOException e) {\n"
                "        verifyException(\"Calculator\", e);\n"
                "    }\n"
                "    assertEquals(0, calc.getErrorCount());\n"
                "}"
            ),
            "cut_context": (
                "public class Calculator {\n"
                "  public void fetchRemoteConfig() throws IOException;\n"
                "  public void loadDefaultConfig();\n"
                "  public int getErrorCount();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "REPLACE_EXPRESSION",
                    "params": {
                        "target_line": 5,
                        "old_expr": "calc.fetchRemoteConfig()",
                        "new_expr": "calc.loadDefaultConfig()",
                    },
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "When the CUT exposes a local substitute that produces the same "
                "observable state, prefer it — the downstream assertion "
                "(`getErrorCount() == 0`) still holds and the test no longer "
                "depends on the network."
            ),
        },
    ],
    "TSES": [
        # TSES[0] — non-void method, capture return value then assert on it.
        {
            "description": "Non-void try-body: capture return, assert, then drop try/catch.",
            "evidence_summary": (
                "same_exception_scenario_groups: [{exception_type: NullPointerException, "
                "group_size: 3}]  last_try_call: non-void"
            ),
            "test_method": (
                "@Test\n"
                "public void test02() throws Throwable {\n"
                "    Foo foo = new Foo();\n"
                "    try {\n"
                "        foo.compute(0);\n"
                "        fail(\"Expecting exception\");\n"
                "    } catch (NullPointerException e) {\n"
                "        verifyException(\"Foo\", e);\n"
                "    }\n"
                "    assertNotNull(foo);\n"
                "}"
            ),
            "cut_context": "public class Foo { public int compute(int seed); }",
            "expected_plan": [
                {
                    "op": "CAPTURE_RETURN_VALUE",
                    "params": {
                        "target_line": 5,
                        "var_name": "result",
                        "var_type": "int",
                    },
                },
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 5,
                        "assert_type": "assertEquals",
                        "actual_expr": "result",
                        "expected_expr": "0",
                    },
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "Tier 1 TSES void-only filter defers this because compute() "
                "returns int. Capture first so the return value is bound to a "
                "local (Smelly-E will NOT flag NARV on an assignment), then "
                "assert on the captured value."
            ),
        },
        # TSES[1] — exception IS the intent; capture shields NARV, @Test(expected=) preserves semantics.
        {
            "description": "Preserve exception intent on a non-void call; avoid NARV via capture.",
            "evidence_summary": (
                "same_exception_scenario_groups: [{exception_type: IndexOutOfBoundsException}]  "
                "last_try_call: non-void  test_intent: exception"
            ),
            "test_method": (
                "@Test\n"
                "public void test07() throws Throwable {\n"
                "    Registry reg = new Registry();\n"
                "    try {\n"
                "        reg.lookup(-1);\n"
                "        fail(\"Expecting IndexOutOfBoundsException\");\n"
                "    } catch (IndexOutOfBoundsException e) {\n"
                "        verifyException(\"Registry\", e);\n"
                "    }\n"
                "}"
            ),
            "cut_context": "public class Registry { public Entry lookup(int index); }",
            "expected_plan": [
                {
                    "op": "CAPTURE_RETURN_VALUE",
                    "params": {
                        "target_line": 5,
                        "var_name": "ignored",
                        "var_type": "Entry",
                    },
                },
                {
                    "op": "ADD_TEST_EXPECTED",
                    "params": {"exception_type": "IndexOutOfBoundsException"},
                },
                {
                    "op": "REMOVE_TRY_CATCH_KEEP_BODY",
                    "params": {"try_begin_line": 4, "drop_fail_call": True},
                },
            ],
            "notes": (
                "When the catch clause expresses the test's real intent (an "
                "out-of-range index should throw), KEEP the exception via "
                "@Test(expected=…). The CAPTURE_RETURN_VALUE step is purely "
                "structural — it turns the naked call into an assignment so "
                "Smelly-E's NARV detector won't fire even though the call "
                "never actually reaches the assignment at runtime."
            ),
        },
    ],
    "AC": [
        # AC[0] — replace CUT-class constant assertion with a real instance check.
        {
            "description": "Constant-only assertion replaced with an observable CUT assertion.",
            "evidence_summary": (
                "constant_assertions: [{constant: Calculator.ANSWER, kind: cut_static}]  "
                "cut_instance_accessors: [getValue]"
            ),
            "test_method": (
                "@Test\n"
                "public void test00() throws Throwable {\n"
                "    Calculator calc = new Calculator();\n"
                "    calc.setValue(7);\n"
                "    assertEquals(42, Calculator.ANSWER);\n"
                "}"
            ),
            "cut_context": (
                "public class Calculator {\n"
                "  public static final int ANSWER = 42;\n"
                "  public int getValue();\n"
                "  public void setValue(int v);\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "REPLACE_ASSERTION",
                    "params": {
                        "target_line": 5,
                        "new_assert_type": "assertEquals",
                        "new_actual_expr": "calc.getValue()",
                        "new_expected_expr": "7",
                    },
                },
            ],
            "notes": (
                "The original line asserts the constant's value against itself, "
                "which carries no test information. Replace with an assertion "
                "that exercises the instance state that was set above."
            ),
        },
    ],
    "NARV": [
        # NARV can also be tackled at Tier 2 when there's a clear one-shot
        # template (e.g., use an existing assertion's expected value).
    ],
    "NASE": [
        # Deferred to Tier 3/4 where evidence drives the assertion target.
    ],
    "ARPM": [
        # Phase 2.2: to be filled when ARPM handler lands.
    ],
    "TSVM": [
        # Phase 2.4: Tier 4 is usually the better fit (observed-state assertion).
    ],
    "OIMT": [],
    "TOFA": [],
}

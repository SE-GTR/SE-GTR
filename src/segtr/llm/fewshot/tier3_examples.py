"""Tier 3 (evidence-guided) few-shot examples."""
from __future__ import annotations

from typing import Any, Dict, List


TIER3_EXAMPLES: Dict[str, List[Dict[str, Any]]] = {
    "NARV": [
        # NARV[0] — boolean return, simplest case.
        {
            "description": "Ignored boolean return — capture and assert.",
            "evidence_summary": (
                "unasserted_return_calls: "
                "[{expr: 'list.contains(\"hello\")', return_type: 'boolean', begin_line: 7}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test() {\n"
                "    List<String> list = new ArrayList<String>();\n"
                "    list.add(\"hello\");\n"
                "    list.add(\"world\");\n"
                "    assertEquals(2, list.size());\n"
                "    list.contains(\"hello\");\n"
                "}"
            ),
            "cut_context": "public class ArrayList<E> { public boolean contains(Object o); }",
            "expected_plan": [
                {
                    "op": "CAPTURE_RETURN_VALUE",
                    "params": {
                        "target_line": 7,
                        "var_name": "containsHello",
                        "var_type": "boolean",
                    },
                },
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 7,
                        "assert_type": "assertTrue",
                        "actual_expr": "containsHello",
                    },
                },
            ],
            "notes": (
                "Boolean return → assertTrue. Variable name reflects the semantic "
                "question being asked. An `assertNotNull` substitute would NOT count "
                "as a meaningful assertion — capture the real return value."
            ),
        },
        # NARV[1] — object return, assert on an observable property.
        {
            "description": "Ignored object return — capture and assert via one of its getters.",
            "evidence_summary": (
                "unasserted_return_calls: "
                "[{expr: 'list.subList(0, 1)', return_type: 'List<String>', begin_line: 5}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test() {\n"
                "    ArrayList<String> list = new ArrayList<String>();\n"
                "    list.add(\"hello\");\n"
                "    list.subList(0, 1);\n"
                "    assertEquals(1, list.size());\n"
                "}"
            ),
            "cut_context": (
                "public class ArrayList<E> { public List<E> subList(int fromIndex, int toIndex); }"
            ),
            "expected_plan": [
                {
                    "op": "CAPTURE_RETURN_VALUE",
                    "params": {
                        "target_line": 5,
                        "var_name": "sub",
                        "var_type": "List<String>",
                    },
                },
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 5,
                        "assert_type": "assertEquals",
                        "actual_expr": "sub.size()",
                        "expected_expr": "1",
                    },
                },
            ],
            "notes": (
                "For object returns, assert on an observable property (`size()`, "
                "`.get(0)`, etc.) rather than `assertNotNull`. NotNull would "
                "register as NNA and be flagged as a weak assertion."
            ),
        },
    ],
    "OIMT": [
        # OIMT[0] — replace a repeated init assertion with an Act-related one.
        {
            "description": "Repeated init assertion replaced with behavior (Act-related) assertion.",
            "evidence_summary": (
                "rules_triggered: [init_value_repeated_across_tests]  "
                "shared_init_assert_keys: ['getInitial'] "
                "nontrivial_calls: [{name: increment, scope: counter0}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test2() throws Throwable {\n"
                "    Counter counter = new Counter(10);\n"
                "    assertEquals(10, counter.getInitial());\n"
                "    counter.increment();\n"
                "    assertEquals(10, counter.getInitial());\n"
                "}"
            ),
            "cut_context": (
                "public class Counter {\n"
                "  public Counter(int initial);\n"
                "  public int getInitial();\n"
                "  public int getCurrent();\n"
                "  public void increment();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "REPLACE_ASSERTION",
                    "params": {
                        "target_line": 6,
                        "new_assert_type": "assertEquals",
                        "new_actual_expr": "counter.getCurrent()",
                        "new_expected_expr": "11",
                    },
                },
            ],
            "notes": (
                "The second `getInitial()` assertion is redundant — the init value "
                "can't change. Replace with an assertion on `getCurrent()` which "
                "reflects the actual `increment()` call. REPLACE keeps the assertion "
                "count stable so Gate 7 is satisfied."
            ),
        },
        # OIMT[1] — replace with boolean behavior check.
        {
            "description": "Repeated init assertion swapped for a behavior-flag assertion.",
            "evidence_summary": (
                "rules_triggered: [default_value_asserted]  "
                "shared_init_assert_keys: ['getMode'] "
                "nontrivial_calls: [{name: process, scope: consumer0}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test3() throws Throwable {\n"
                "    Consumer consumer = new Consumer();\n"
                "    assertEquals(\"default\", consumer.getMode());\n"
                "    consumer.process(\"event\");\n"
                "    assertEquals(\"default\", consumer.getMode());\n"
                "}"
            ),
            "cut_context": (
                "public class Consumer {\n"
                "  public String getMode();\n"
                "  public boolean isProcessed();\n"
                "  public void process(String event);\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "REPLACE_ASSERTION",
                    "params": {
                        "target_line": 6,
                        "new_assert_type": "assertTrue",
                        "new_actual_expr": "consumer.isProcessed()",
                    },
                },
            ],
            "notes": (
                "Boolean-valued behavior check is the strongest minimal-change fix. "
                "Do NOT use REMOVE_ASSERTION here — removing would drop the meaningful "
                "assertion count and trip Gate 7."
            ),
        },
    ],
    "TOFA": [
        # TOFA[0] — pure data holder: skip (empty plan).
        {
            "description": "CUT is a pure data holder — no meaningful behavior to exercise; return [].",
            "evidence_summary": (
                "non_assert_call_count: 0  "
                "calls: [{name: setX, kind: setter}, {name: setY, kind: setter}, "
                "{name: getX, kind: getter}, {name: getY, kind: getter}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test1() throws Throwable {\n"
                "    Point point = new Point();\n"
                "    point.setX(3);\n"
                "    point.setY(4);\n"
                "    assertEquals(3, point.getX());\n"
                "    assertEquals(4, point.getY());\n"
                "}"
            ),
            "cut_context": (
                "public class Point {\n"
                "  public Point();\n"
                "  public int getX();\n"
                "  public int getY();\n"
                "  public void setX(int x);\n"
                "  public void setY(int y);\n"
                "}  // no non-accessor methods\n"
            ),
            "expected_plan": [],
            "notes": (
                "When the CUT exposes ONLY getters/setters, there is no honest way "
                "to fix TOFA. Returning `[]` tells the system to skip — better than "
                "forcing an `assertNotNull` that would introduce NNA."
            ),
        },
        # TOFA[1] — CUT has non-trivial logic; add a behavior assertion.
        {
            "description": "CUT has a logic method — add an assertion that exercises it.",
            "evidence_summary": (
                "non_assert_call_count: 0  "
                "calls: [{name: deposit, kind: setter-like}]  "
                "cut_has_logic_method: isOverdrawn"
            ),
            "test_method": (
                "@Test\n"
                "public void test2() throws Throwable {\n"
                "    Account account = new Account();\n"
                "    account.deposit(100);\n"
                "    account.deposit(50);\n"
                "    assertEquals(150, account.getBalance());\n"
                "}"
            ),
            "cut_context": (
                "public class Account {\n"
                "  public Account();\n"
                "  public void deposit(int amount);\n"
                "  public int getBalance();\n"
                "  public boolean isOverdrawn();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 6,
                        "assert_type": "assertFalse",
                        "actual_expr": "account.isOverdrawn()",
                    },
                },
            ],
            "notes": (
                "`isOverdrawn()` is a CUT logic method whose value depends on the "
                "preceding `deposit()` calls, making it a causally-linked oracle. "
                "Prefer booleans over `assertNotNull` on the CUT."
            ),
        },
    ],
    "ARPM": [
        # ARPM[0] — swap inherited assertion for a CUT-state assertion.
        {
            "description": "Inherited-method assertion replaced with CUT-state assertion.",
            "evidence_summary": (
                "arpm_assertions: [{assertion_call: 'button.isFocusTraversalPolicySet()', "
                "cut_call: 'button.setSize(100, 30)', reason: 'inherited_unaffected'}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test5() throws Throwable {\n"
                "    Button button = new Button(\"OK\");\n"
                "    button.setSize(100, 30);\n"
                "    assertFalse(button.isFocusTraversalPolicySet());\n"
                "}"
            ),
            "cut_context": (
                "public class Button extends Component {\n"
                "  public Button(String label);\n"
                "  public void setSize(int w, int h);\n"
                "  public int getWidth();\n"
                "  public int getHeight();\n"
                "  // isFocusTraversalPolicySet is inherited from Component; unrelated\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "REPLACE_ASSERTION",
                    "params": {
                        "target_line": 5,
                        "new_assert_type": "assertEquals",
                        "new_actual_expr": "button.getWidth()",
                        "new_expected_expr": "100",
                    },
                },
            ],
            "notes": (
                "The original assertion checks an inherited flag that `setSize` can't "
                "change. Replace with an assertion that directly observes the Act "
                "call's effect on CUT state (`getWidth()` reflects `setSize(100, …)`)."
            ),
        },
        # ARPM[1] — replace with boolean CUT method.
        {
            "description": "Inherited assertion replaced with a boolean CUT-specific check.",
            "evidence_summary": (
                "arpm_assertions: [{assertion_call: 'field.isFocusCycleRoot()', "
                "cut_call: 'field.setText(\"hello\")', reason: 'inherited_unaffected'}]"
            ),
            "test_method": (
                "@Test\n"
                "public void test8() throws Throwable {\n"
                "    TextField field = new TextField();\n"
                "    field.setText(\"hello\");\n"
                "    assertFalse(field.isFocusCycleRoot());\n"
                "    assertEquals(\"hello\", field.getText());\n"
                "}"
            ),
            "cut_context": (
                "public class TextField extends Component {\n"
                "  public TextField();\n"
                "  public void setText(String text);\n"
                "  public String getText();\n"
                "  public boolean isEmpty();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "REPLACE_ASSERTION",
                    "params": {
                        "target_line": 5,
                        "new_assert_type": "assertFalse",
                        "new_actual_expr": "field.isEmpty()",
                    },
                },
            ],
            "notes": (
                "After `setText(\"hello\")`, `isEmpty()` should be false — that is "
                "causally linked to the Act call. Using REPLACE keeps the assertion "
                "count stable so Gate 7 is satisfied even when the method has only "
                "two assertions to start with."
            ),
        },
    ],
    "EDIS": [],
    "EDED": [],
}

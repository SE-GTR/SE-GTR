"""Tier 4 (dynamic-context) few-shot examples.

Each example carries an optional ``dynamic_evidence`` block — the observed
runtime state captured from a trial execution. When present, the expected
plan must only use values actually listed in ``changed_fields``, never
guessed. When absent (static-fallback mode), the example shows the LLM
how to fall back on Smelly-E's ``modified_fields`` + the CUT's public
observers.

Two NASE examples and two TSVM examples — each pair covers one dynamic
and one static-fallback case so the model sees both failure modes.
"""
from __future__ import annotations

from typing import Any, Dict, List


TIER4_EXAMPLES: Dict[str, List[Dict[str, Any]]] = {
    "NASE": [
        # NASE[0] — DYNAMIC mode. Observed state change via a getter.
        {
            "description": "Void call with observed state change — assert on post-state.",
            "evidence_summary": (
                "unverified_side_effect_calls: [{act_call: 'consumer.consume(list)', "
                "modified_fields: ['processedCount', 'lastEvent']}]"
            ),
            "dynamic_evidence": {
                "state_before": {
                    "consumer.getProcessedCount()": "0",
                    "consumer.getLastEvent()": "null",
                },
                "state_after": {
                    "consumer.getProcessedCount()": "1",
                    "consumer.getLastEvent()": "AccessEvent@501",
                },
                "changed_fields": {
                    "consumer.getProcessedCount()": {"before": "0", "after": "1"},
                    "consumer.getLastEvent()": {
                        "before": "null",
                        "after": "AccessEvent@501",
                    },
                },
                "unchanged_fields": [],
                "stdout": "Processing event: AccessEvent@501",
            },
            "test_method": (
                "@Test\n"
                "public void test() {\n"
                "    Consumer consumer = new Consumer();\n"
                "    consumer.setSounds(sounds);\n"
                "    consumer.consume(list);\n"
                "    assertEquals(1, list.size());\n"
                "}"
            ),
            "cut_context": (
                "public class Consumer {\n"
                "  public void consume(List<?> events);\n"
                "  public int getProcessedCount();\n"
                "  public Event getLastEvent();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 5,
                        "assert_type": "assertEquals",
                        "actual_expr": "consumer.getProcessedCount()",
                        "expected_expr": "1",
                    },
                },
            ],
            "notes": (
                "Uses the observed value (1), not a guess. Only asserts on fields "
                "that actually changed between state_before and state_after. "
                "`lastEvent` is skipped because the identity-hash AccessEvent@501 "
                "is non-deterministic and would flake."
            ),
        },
        # NASE[1] — STATIC-FALLBACK. No dynamic capture; infer from Smelly-E.
        {
            "description": (
                "Static-fallback (no dynamic capture). Infer observer from "
                "modified_fields + CUT public API."
            ),
            "evidence_summary": (
                "unverified_side_effect_calls: [{act_call: 'cart.addItem(\"banana\")', "
                "modified_fields: ['items', 'size']}] "
                "(dynamic capture unavailable — CUT has no public zero-arg observers "
                "of the mutated fields under a runtime-usable shape)"
            ),
            # No dynamic_evidence block — this is the whole point of the example.
            "test_method": (
                "@Test\n"
                "public void test() {\n"
                "    Cart cart = new Cart();\n"
                "    cart.addItem(\"apple\");\n"
                "    cart.addItem(\"banana\");\n"
                "    assertNotNull(cart);\n"
                "}"
            ),
            "cut_context": (
                "public class Cart {\n"
                "  public void addItem(String name);\n"
                "  public int getSize();\n"
                "  public boolean isEmpty();\n"
                "  public List<String> getItems();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 5,
                        "assert_type": "assertEquals",
                        "actual_expr": "cart.getSize()",
                        "expected_expr": "2",
                    },
                },
            ],
            "notes": (
                "No dynamic evidence — fall back on Smelly-E's modified_fields and "
                "the CUT's public observers. Pick the most direct correspondence "
                "(`size` field ↔ `getSize()` method). The expected value (2) is "
                "derivable from the two observed addItem calls. Do NOT use "
                "assertNotNull as a substitute oracle."
            ),
        },
    ],
    "TSVM": [
        # TSVM[0] — DYNAMIC mode. Two getters changed — emit one assertion each.
        {
            "description": (
                "Shared void method across tests — add observations on the two "
                "independent post-state changes."
            ),
            "evidence_summary": (
                "shared_void_calls: [{name: 'log.warn', call_count: 3, test_methods: "
                "['test01','test02','test03']}] "
                "— per-test repair: assert the side effect."
            ),
            "dynamic_evidence": {
                "state_before": {
                    "log.getMessageCount()": "0",
                    "log.isEmpty()": "true",
                    "log.getLevel()": "INFO",
                },
                "state_after": {
                    "log.getMessageCount()": "1",
                    "log.isEmpty()": "false",
                    "log.getLevel()": "INFO",
                },
                "changed_fields": {
                    "log.getMessageCount()": {"before": "0", "after": "1"},
                    "log.isEmpty()": {"before": "true", "after": "false"},
                },
                "unchanged_fields": ["log.getLevel()"],
            },
            "test_method": (
                "@Test\n"
                "public void test() {\n"
                "    Logger log = new Logger();\n"
                "    log.warn(\"msg\");\n"
                "}"
            ),
            "cut_context": (
                "public class Logger {\n"
                "  public void warn(String msg);\n"
                "  public int getMessageCount();\n"
                "  public boolean isEmpty();\n"
                "  public String getLevel();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 4,
                        "assert_type": "assertEquals",
                        "actual_expr": "log.getMessageCount()",
                        "expected_expr": "1",
                    },
                },
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 4,
                        "assert_type": "assertFalse",
                        "actual_expr": "log.isEmpty()",
                    },
                },
            ],
            "notes": (
                "Two getters in changed_fields → one assertion per getter. "
                "`getLevel()` is skipped (in unchanged_fields). Each assertion uses "
                "the observed literal (`1`, `false`). Do NOT add an assertion on "
                "`getLevel()` — it would add noise without catching regressions."
            ),
        },
        # TSVM[1] — STATIC-FALLBACK for a shared void call.
        {
            "description": (
                "Shared void method, dynamic capture unavailable — fall back on "
                "Smelly-E's modified_fields."
            ),
            "evidence_summary": (
                "shared_void_calls: [{name: 'bus.publish', call_count: 4}] "
                "modified_fields (static): ['history', 'subscriberCount'] "
                "(dynamic capture unavailable)"
            ),
            "test_method": (
                "@Test\n"
                "public void test() {\n"
                "    EventBus bus = new EventBus();\n"
                "    bus.publish(event);\n"
                "}"
            ),
            "cut_context": (
                "public class EventBus {\n"
                "  public void publish(Event e);\n"
                "  public int getHistorySize();\n"
                "  public int getSubscriberCount();\n"
                "  public boolean isEmpty();\n"
                "}"
            ),
            "expected_plan": [
                {
                    "op": "INSERT_ASSERTION",
                    "params": {
                        "after_line": 4,
                        "assert_type": "assertEquals",
                        "actual_expr": "bus.getHistorySize()",
                        "expected_expr": "1",
                    },
                },
            ],
            "notes": (
                "Without dynamic evidence, map a modified field (`history`) to the "
                "closest public observer (`getHistorySize()`). `subscriberCount` "
                "probably did not change from this call (subscribers are added via "
                "a different API), so we do not assert on it. Prefer one reliable "
                "assertion over multiple shaky ones."
            ),
        },
    ],
}

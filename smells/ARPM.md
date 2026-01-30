# ARPM — Assertion with Unrelated Parent Class Method

## Definition
The test contains an assertion that checks a method **inherited from a superclass**, but that inherited behavior is **unrelated** to the Act step (i.e., not affected by the method(s) under test).

## Why it matters
Such assertions add noise and can mislead developers about what the test intends to validate.

## How Smelly detects it (high-level)
- Collect methods invoked by the test (direct + indirect) across the CUT hierarchy.
- Approximate which fields might be modified (mostly direct assignments).
- If an assertion reads a **parent-class field** via an accessor, but that field is not modified by the invoked methods, flag ARPM.

## Repair playbook (preferred order)
1. **Replace the unrelated assertion with an assertion tied to the Act call**
   - Assert effects on CUT state, return values, or observable outputs that the Act call influences.
2. **If the assertion is only checking initialization, remove it**
   - Keep initialization checks in a dedicated test (but do not delete tests; remove only redundant assertions).
3. **Avoid “gaming” the detector**
   - Do not add random calls/assertions; ensure causal linkage.

## Mini example

### Before
```java
cut.paintComponent(g);
assertFalse(cut.isFocusTraversalPolicySet()); // inherited, unrelated
```

### After
```java
cut.paintComponent(g);
// assert something directly related to paintComponent outcome/state, if observable;
// otherwise, keep the call only if it is meaningful for the suite, and remove the unrelated assertion.
```

## LLM checklist
- Identify which assertions target inherited methods.
- Ask: “Would the Act call change what this assertion observes?”
- If no, remove/replace with an Act-related oracle.

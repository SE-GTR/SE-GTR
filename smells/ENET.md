# ENET — Exception Not Explicitly Thrown (Exceptions due to null arguments)

## Definition
The test triggers a `NullPointerException` due to **null arguments** (constructor or method call), where the exception is **not explicitly thrown/handled as part of the intended behavior** of the tested method.

## Why it matters
It can mislead developers into thinking the exception is expected behavior, while it may actually be a setup bug or misuse of the API.

## How Smelly detects it (high-level)
- Find constructor/method calls that receive `null` arguments.
- Check whether those null values are later dereferenced/used inside the CUT.
- If the resulting failure is not properly handled/structured, flag ENET.

## Repair playbook (preferred order)
1. **Replace null arguments with valid values and test normal behavior**
   - Create minimal valid objects/strings/collections.
2. **If null is intentionally part of the contract, make the expectation explicit**
   - Prefer `@Test(expected = NullPointerException.class)` (JUnit4) or a clear, minimal try/catch.
3. **Avoid “broad exception” tests**
   - Do not catch overly generic exceptions just to keep the test passing.

## Mini example

### Before
```java
try {
  cut.refresh(null);
  fail("Expecting exception");
} catch (NullPointerException e) {
  // ok
}
```

### After (normal behavior)
```java
cut.refresh(new TableCell(...));
assertEquals(expected, cut.getState());
```

## LLM checklist
- Decide whether null is misuse vs. contractual input.
- Prefer non-null valid inputs unless contract clearly requires null rejection.
- If exception is expected, make it explicit and minimal.

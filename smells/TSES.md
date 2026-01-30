# TSES — Testing the Same Exception Scenario

## Definition
Two or more tests handle the **same exception type** and share nearly identical setup, where the exception is likely triggered by the **same underlying root cause** (often a faulty setup), even though the tests call different methods.

## Why it matters
Such tests are semantically redundant: they do not meaningfully diversify fault detection and can misrepresent “expected” exception behavior.

## How Smelly detects it (high-level)
- Find pairs of tests with try/catch handling the same exception.
- Compare statements before the try/catch (must be identical).
- If the try blocks differ in ≤ a small threshold of statements, report TSES.

## Repair playbook (preferred order)
1. **Fix the setup so the method call executes normally**
   - Replace null/invalid arguments and complete required initialization.
   - Then assert the normal behavior.
2. **If the exception is truly part of the contract, make it explicit and specific**
   - Prefer `@Test(expected=...)` in JUnit4 (or `ExpectedException` rule) instead of ad-hoc try/catch.
   - Ensure each test targets a distinct exception-triggering condition (different inputs / different root cause).
3. **Avoid masking setup failures as “expected exceptions”**
   - Do not keep a test that only passes because the environment/setup is broken.

## Mini example

### Before
```java
try {
  cut.someMethod(null);
  fail("Expecting exception");
} catch (NullPointerException e) {
  // ok
}
```

### After (if null is not intended)
```java
cut.someMethod(validArg);
assertEquals(expected, cut.getState());
```

### After (if NPE is the contract)
```java
@Test(expected = NullPointerException.class)
public void testSomeMethod_null_throwsNPE() {
  cut.someMethod(null);
}
```

## LLM checklist
- Identify whether the exception is caused by invalid setup or by API contract.
- Prefer converting “setup-failure exceptions” into normal-behavior tests.
- If exception is intended, make expectation explicit and differentiate scenarios across tests.

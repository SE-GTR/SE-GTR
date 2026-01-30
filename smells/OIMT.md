# OIMT — Asserting Object Initialization Multiple Times

## Definition
Two or more tests repeatedly assert values that are **set in the constructor** or are **default-initialization values**, rather than asserting behavior related to each test’s Act step.

## Why it matters
Initialization checks are valid but become redundant when repeated across many tests, increasing maintenance cost and reducing signal-to-noise.

## How Smelly detects it (high-level)
- Find tests that initialize objects of the same class.
- Identify constructor arguments and default field values in the CUT.
- If multiple tests mainly assert those initialization values, Smelly reports OIMT.

## Repair playbook (preferred order)
1. **Convert the test’s assertions to target the Act step**
   - Remove redundant “default value” assertions if they are not affected by the Act call.
   - Add/strengthen assertions that validate the method being exercised.
2. **Keep at most one “initialization-focused” assertion block per object type**
   - Since we cannot delete tests, remove initialization assertions from *some* tests, not all.
3. **If initialization assertions are required as a precondition, minimize them**
   - Assert only what is strictly needed (avoid asserting multiple unrelated defaults).

## Mini example

### Before
```java
Foo foo = new Foo(1);
assertEquals(1, foo.getId());   // constructor value
foo.doWork();
assertEquals(1, foo.getId());   // repeated, unrelated to doWork
```

### After
```java
Foo foo = new Foo(1);
foo.doWork();
// assert effect of doWork (state/output/return value), not just constructor defaults
```

## LLM checklist
- Detect assertions that only restate constructor/default values.
- Prefer removing those assertions unless the Act step depends on them.
- Replace with Act-related assertions whenever possible.

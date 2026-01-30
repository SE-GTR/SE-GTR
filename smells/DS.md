# DS — Duplicated Setup (Duplicated setup)

## Definition
Two or more tests in the same suite share **at least two identical setup statements** (typically a common prefix before assertions).

## Why it matters
Duplicated setup inflates test code, complicates maintenance, and makes future edits error-prone.

## How Smelly detects it (high-level)
- Detect identical sequences of non-assert statements at the beginning of multiple tests (precision-oriented heuristic).

## Repair playbook (preferred order)
1. **Extract common setup into `@Before`**
   - Promote shared objects to instance fields, initialize them in `@Before`.
   - Ensure setup creates *fresh state per test* (do not reuse mutated objects across tests).
2. **Alternatively extract a private helper method**
   - Use when `@Before` would introduce too many shared fields or unclear state.
3. **Keep the test logic intact**
   - Do not delete tests; only factor out duplication.

## Mini example

### Before
```java
@Test public void testA() {
  Foo foo = new Foo();
  Bar bar = new Bar(1);
  // ...
}
@Test public void testB() {
  Foo foo = new Foo();
  Bar bar = new Bar(1);
  // ...
}
```

### After
```java
private Foo foo;
private Bar bar;

@Before public void setUp() {
  foo = new Foo();
  bar = new Bar(1);
}
```

## LLM checklist
- Identify identical setup prefix across flagged tests.
- Move shared setup into `@Before`, keep per-test state fresh.
- Ensure compilation and imports remain correct (JUnit4).

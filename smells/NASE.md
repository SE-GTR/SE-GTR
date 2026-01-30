# NASE — Not Asserted Side Effect (Not asserted side effect)

## Definition
The test executes at least one call (in the Act step) that **changes the state of the object under test (side effect)**, but **no assertion observes/verifies that effect**.

## Why it matters
These calls may improve structural coverage, but the test becomes weak as a regression oracle: behavior can change without any assertion failing.

## How Smelly detects it (high-level)
Smelly approximates side effects via static analysis by:
- finding fields that **may be modified** by methods executed by the test (mainly **direct assignments**), and
- checking whether any assertion **directly or indirectly** reads/validates those fields.

## Repair playbook (preferred order)
1. **Add an assertion that observes the side effect via public API**
   - Prefer *existing* observers: `getX()`, `size()`, `contains()`, `isEmpty()`, `hasX()`, `toArray().length`, etc.
   - Prefer *before/after* assertions if meaningful.
2. **Use existing return values as observation points**
   - If the Act call returns a value, capture it and assert it (often overlaps with NARV).
3. **If the side effect is not observable, remove or replace the side-effect call**
   - Do not delete the whole test; remove only the meaningless line(s).
4. **Optional (only if allowed): reflection-based observation**
   - Read private fields via reflection to assert the effect.
   - This is brittle; treat as a last resort.

## Common pitfalls
- Adding assertions on **unrelated fields/constants** to “satisfy” the smell.
- Using sleeps, time-dependent assertions, or external resources to observe effects.

## Mini example

### Before
```java
foo.add(item);            // side effect
assertEquals(0, foo.size()); // unrelated or wrong oracle
```

### After (preferred)
```java
int before = foo.size();
foo.add(item);
assertEquals(before + 1, foo.size());
```

## LLM checklist
- Identify which call likely mutates state.
- Find the most direct observer (getter/size/contains).
- Add a minimal, deterministic assertion that ties to the Act call.
- If no observer exists, remove the irrelevant call rather than inventing unrelated assertions.

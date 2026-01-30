# TSVM — Testing the Same Void Method (Multiple calls to the same void method)

## Definition
Two or more tests call the **same void method**, but the **side effects are not asserted** (i.e., NASE repeated across tests for that void method).

## Why it matters
Branch coverage may increase, but the suite gains little behavioral checking. Multiple weak tests for the same void method provide low value.

## How Smelly detects it (high-level)
- Identify tests with NASE.
- For those tests, identify the void call responsible for the unasserted side effect.
- If multiple tests share the same problematic void call, flag TSVM.

## Repair playbook (preferred order)
1. **Add assertions for the void method’s side effects**
   - Use getters/observers to check state changes.
   - Prefer before/after assertions.
2. **Ensure at least one test meaningfully checks the void method**
   - For other tests, either:
     - assert different branch-specific effects, or
     - remove the redundant void call if it is only for coverage.
3. **Optional: reflection-based observation (only if allowed)**
   - Last resort; brittle.

## Mini example

### Before
```java
cut.release(attrs);      // void, side effect
assertEquals(0, cut.getMaxReached()); // unrelated
```

### After
```java
int before = cut.getMaxConnections();
cut.release(attrs);
assertTrue(cut.getMaxConnections() <= before); // example effect
```

## LLM checklist
- Find repeated void calls and locate which state they should change.
- Add at least one clear oracle per void call.
- Remove redundant void calls if no meaningful oracle exists.

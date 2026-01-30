# NNA — Redundant “not null” Assertion (Not null assertion)

## Definition
An `assertNotNull` is redundant when:
1) it appears immediately after object creation, or
2) another assertion already implies the value is non-null (indirect non-null proof).

## Why it matters
Redundant assertions increase noise and test size without improving fault detection.

## How Smelly detects it (high-level)
- Detect `assertNotNull(x)` right after `x = new ...`.
- Detect `assertNotNull(x)` when later assertions already dereference/validate `x` in a way that would fail if it were null.

## Repair playbook
1. **Remove the redundant `assertNotNull`**
2. **Keep a non-null assertion only when it is the only oracle**
   - If there is no other assertion that would fail on null, you may keep it (rare in EvoSuite tests).

## Mini example

### Before
```java
Foo foo = new Foo();
assertNotNull(foo);
assertEquals(1, foo.getId()); // already implies foo != null
```

### After
```java
Foo foo = new Foo();
assertEquals(1, foo.getId());
```

## LLM checklist
- If the object is freshly created and immediately used, drop `assertNotNull`.
- If another assertion already dereferences the value, drop `assertNotNull`.

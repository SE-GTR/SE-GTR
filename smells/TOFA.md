# TOFA — Testing Only Field Accessors (Testing only field accesors)

## Definition
The test performs only object initialization and calls only **getters/setters** of the CUT, then asserts those trivial results.

## Why it matters
Accessor-only tests usually provide limited fault-detection value: the behavior is predictable and often not worth dedicated tests, especially at scale.

## How Smelly detects it (high-level)
- Collect method calls on the CUT from the test.
- Classify methods as “getter” (single return of a field) or “setter” (single assignment to a field).
- If the test only calls getters/setters, flag TOFA.

## Repair playbook (preferred order)
1. **If the CUT has real logic, add a logic-bearing call and assert its outcome**
   - Prefer a method whose behavior depends on internal state or input processing.
2. **If the CUT is a pure data holder (only accessors), accept that TOFA may be unavoidable**
   - In that case, focus on other smells first; do not invent meaningless calls.
3. **Avoid superficial “fixes”**
   - Calling `toString()` / `hashCode()` purely to silence the detector is usually not meaningful.

## Mini example

### Before
```java
User u = new User();
u.setName("a");
assertEquals("a", u.getName());
```

### After (only if there is real logic)
```java
User u = new User();
u.setName("a");
assertTrue(u.isValid());      // example logic method
```

## LLM checklist
- Check whether the CUT has any non-trivial public method.
- If yes: call it and assert its behavior.
- If no: consider leaving TOFA as-is rather than adding noise.

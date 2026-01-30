# AC — Asserting Constants (Asserting Constants)

## Definition
At least one assertion checks the value of a variable declared as `static final` (a constant).

## Why it matters
Asserting constants is uncommon because the assertion will only fail if the constant is manually changed; it rarely provides meaningful regression detection.

## How Smelly detects it (high-level)
- Collect constants (identifiers declared with `final`).
- Flag assertions that directly involve those constants.

## Repair playbook (preferred order)
1. **Remove the constant assertion if it is unrelated to the Act call**
2. **If a constant is used as part of a method contract, test the contract instead**
   - Assert behavior of a method that depends on the constant, not the constant itself.
3. **As a last resort, inline the literal**
   - This may silence the detector, but prefer removing the assertion when it provides no value.

## Mini example

### Before
```java
cut.connectionError("x");
assertEquals(0, ConnectionConsumer.DEFAULT_SUBSEQUENT_RETRIES);
```

### After (preferred)
```java
cut.connectionError("x");
// assert an effect of connectionError, if any; otherwise remove the constant assertion.
```

## LLM checklist
- Identify constant-only assertions.
- Prefer removing them unless they are truly part of an externally observable contract.

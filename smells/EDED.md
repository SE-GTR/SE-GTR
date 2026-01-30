# EDED — Exception Due to External Dependencies (Exceptions due to external dependencies)

## Definition
The test handles exceptions caused by **external dependencies/resources** (GUI/headless environment, network, file system, DB, missing classes), making the test **non-self-contained**.

## Why it matters
Such tests become flaky and environment-dependent; failures may not indicate regressions in the CUT.

## How Smelly detects it (high-level)
Smelly checks try/catch blocks and flags when the caught exception belongs to a predefined “external dependency” list
(e.g., `HeadlessException`, `SQLException`, network and I/O related exceptions).

## Repair playbook (preferred order)
1. **Avoid the external dependency**
   - Replace the call with a pure, deterministic alternative (preferred for EvoSuite tests).
2. **Stub/mock the dependency (lightweight)**
   - Use simple fakes or minimal in-memory substitutes when feasible.
3. **If the call provides no meaningful oracle, remove it**
   - Do not delete the test; remove only the external-dependent call/branch.
4. **Avoid converting to ignored/disabled tests**
   - Do not add `@Ignore`.

## Mini example

### Before
```java
try {
  gui.showView(); // can throw HeadlessException
  fail("Expecting exception");
} catch (HeadlessException e) {
  // ok
}
```

### After
```java
// Prefer removing the environment-dependent call if it is not core to the CUT behavior.
// Then assert a deterministic property that is actually related to the test goal.
```

## LLM checklist
- Detect external-resource exceptions and the call that triggers them.
- Prefer removing/replacing the call unless the CUT’s contract truly depends on the resource.
- Keep test deterministic and self-contained.

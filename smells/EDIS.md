# EDIS — Exception Due to Incomplete Setup (Exceptions due to incomplete setup)

## Definition
The test creates an object but does not initialize it properly (missing required calls / wrong call order), so a later call throws an exception. The test often passes by catching that exception.

## Why it matters
The test is not exercising meaningful behavior; it is documenting the generator’s inability to build valid object states rather than testing the CUT’s intended logic.

## How Smelly detects it (high-level)
- In try/catch tests, collect instance variables used by methods in the try block.
- Check whether those variables were initialized by constructors or methods invoked by the test.
- If required initialization is missing, flag EDIS.

## Repair playbook (preferred order)
1. **Complete the required initialization**
   - Use CUT code context to find required “init/tokenize/open/prepare” calls.
   - Set mandatory fields (via constructor args or setters) before calling the method under test.
2. **Reorder calls to respect lifecycle**
   - Many APIs require calling `init()` before `next()`, etc.
3. **Convert exception-catching tests into normal-behavior tests**
   - After fixing setup, remove the try/catch and assert real outputs/state.
4. **If proper setup is impossible without heavy dependencies, remove the problematic call**
   - Keep the test method; remove only the call(s) that depend on incomplete setup.

## Mini example

### Before
```java
Tokenizer t = new Tokenizer(); // missing tokenize(...)
try {
  t.nextElement();
  fail("Expecting exception");
} catch (NullPointerException e) {
  // ok
}
```

### After
```java
Tokenizer t = new Tokenizer();
t.tokenize("abc");
assertNotNull(t.nextElement());
```

## LLM checklist
- Identify which field/state is uninitialized (from CUT code).
- Add the minimal initialization call(s) before the failing method.
- Replace exception-based passing with meaningful assertions.

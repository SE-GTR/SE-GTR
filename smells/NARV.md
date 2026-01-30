# NARV — Not Asserted Return Value (Not asserted return value)

## Definition
The test calls at least one **non-void** method, but its **return value is never asserted or used** later in the test.

## Why it matters
The test “touches” code for coverage but does not validate the method’s behavior. This is a common weakness in automatically-generated tests.

## How Smelly detects it (high-level)
- Collect method calls that return a value (non-`void`).
- Check whether the returned value is:
  - used as an argument in an assertion,
  - stored in a variable and later used, or
  - passed to another call.
If none applies, Smelly reports NARV.

## Repair playbook (preferred order)
1. **Capture the return value and assert it**
   - For booleans: `assertTrue/False(result)`
   - For objects: `assertNotNull(result)` (only if not redundant), `assertEquals(expected, result)`
2. **If the return value is irrelevant to the test goal, remove the call**
   - Keep the test method; remove only the dead call.
3. **Replace with a stronger oracle**
   - If the method’s contract implies state change, assert that state (may overlap with NASE).

## Mini example

### Before
```java
obj.equals(other);  // return value ignored
assertEquals(1, obj.getId());
```

### After
```java
assertFalse(obj.equals(other));
assertEquals(1, obj.getId());
```

## LLM checklist
- Find non-void calls whose results are unused.
- Prefer asserting the returned value directly.
- Do not “assert something else” unless it is causally linked to the call.

from __future__ import annotations

import re
from typing import List, Set, Tuple

ASSERT_NOT_NULL_RE = re.compile(r"\bassertNotNull\s*\(\s*(?P<var>[A-Za-z_]\w*)\s*\)\s*;")
DECL_NEW_RE = re.compile(
    r"^\s*(?:final\s+)?(?P<type>[A-Za-z_][\w\.<>,\[\]]*)\s+(?P<var>[A-Za-z_]\w*)\s*=\s*new\s+.+;\s*$"
)

METHOD_BLOCK_RE = re.compile(
    r"(?ms)^\s*(?:@Test[^\n]*\n\s*)*(?:public\s+)?void\s+(?P<name>test\w+)\s*\([^)]*\)\s*(?:throws[^\{]+)?\{(?P<body>.*?)^\s*\}"
)


def remove_redundant_assert_not_null(java_text: str) -> Tuple[str, int]:
    """Heuristic NNA fix: remove redundant assertNotNull.

    - Case A: assertNotNull(var) immediately after `var = new ...`
    - Case B: within next 30 lines there exists another assertion that uses `var`
    """
    lines = java_text.splitlines()
    remove_idxs: Set[int] = set()

    for i, line in enumerate(lines):
        m = ASSERT_NOT_NULL_RE.search(line)
        if not m:
            continue
        var = m.group("var")

        prev = i - 1
        while prev >= 0 and lines[prev].strip() == "":
            prev -= 1
        if prev >= 0:
            md = DECL_NEW_RE.match(lines[prev])
            if md and md.group("var") == var:
                remove_idxs.add(i)
                continue

        window = "\n".join(lines[i + 1 : i + 31])
        if "assert" in window and re.search(rf"\b{re.escape(var)}\b", window):
            remove_idxs.add(i)

    if not remove_idxs:
        return java_text, 0
    new_lines = [ln for idx, ln in enumerate(lines) if idx not in remove_idxs]
    return "\n".join(new_lines) + ("\n" if java_text.endswith("\n") else ""), len(remove_idxs)


def extract_duplicated_setup_to_before(java_text: str, target_test_methods: List[str], min_common_lines: int = 2) -> Tuple[str, bool]:
    """Best-effort DS fix: extract common setup prefix into @Before setUp().

    This only triggers if:
    - at least two target tests exist
    - their method bodies share an identical line prefix of length >= min_common_lines
    - no existing @Before is present

    Note: This assumes EvoSuite-style declarations that can be promoted to fields.
    """
    if "@Before" in java_text or "org.junit.Before" in java_text:
        return java_text, False

    bodies: List[List[str]] = []
    for m in METHOD_BLOCK_RE.finditer(java_text):
        nm = m.group("name")
        if nm in target_test_methods:
            body_lines = [ln for ln in m.group("body").splitlines() if ln.strip() != ""]
            bodies.append(body_lines)

    if len(bodies) < 2:
        return java_text, False

    prefix: List[str] = []
    for i in range(min(len(b) for b in bodies)):
        li = bodies[0][i]
        if all(i < len(b) and b[i] == li for b in bodies[1:]):
            if "assert" in li or li.strip().startswith("try"):
                break
            prefix.append(li)
        else:
            break

    if len(prefix) < min_common_lines:
        return java_text, False

    # Promote declarations in prefix to fields
    decl_re = re.compile(r"^\s*(?:final\s+)?(?P<type>[A-Za-z_][\w\.<>,\[\]]*)\s+(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<rhs>.+);\s*$")
    field_decls: List[str] = []
    setup_lines: List[str] = []
    promoted: Set[str] = set()

    for ln in prefix:
        md = decl_re.match(ln)
        if md:
            ty, var, rhs = md.group("type"), md.group("var"), md.group("rhs")
            promoted.add(var)
            field_decls.append(f"  private {ty} {var};")
            setup_lines.append(f"    {var} = {rhs};")
        else:
            setup_lines.append(ln)

    insertion = "\n" + "\n".join(field_decls) + "\n\n" + "  @org.junit.Before\n  public void setUp() throws Exception {\n" + "\n".join(setup_lines) + "\n  }\n"
    class_open = java_text.find("{")
    if class_open < 0:
        return java_text, False
    new_text = java_text[: class_open + 1] + insertion + java_text[class_open + 1 :]

    # Remove prefix from each target method
    prefix_block = "\n".join(prefix)
    for nm in target_test_methods:
        new_text = re.sub(
            rf"(?ms)(\bvoid\s+{re.escape(nm)}\s*\([^)]*\)\s*(?:throws[^\{{]+)?\{{\s*){re.escape(prefix_block)}\n",
            r"\1",
            new_text,
            count=1,
        )
        # Remove types from remaining redeclarations of promoted vars
        for var in promoted:
            new_text = re.sub(
                rf"(?m)^\s*(?:final\s+)?[A-Za-z_][\w\.<>,\[\]]*\s+{re.escape(var)}\s*=",
                f"    {var} =",
                new_text,
            )

    return new_text, True

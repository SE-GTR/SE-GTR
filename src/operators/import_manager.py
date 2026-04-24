"""Manages JUnit 4 `import static` additions and strips JUnit 5 imports.

Only adds imports for assert methods that are actually used and don't already
have an explicit or wildcard import. Never removes imports that existed in the
original file (safe, additive-only reconcile).
"""
from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple


JUNIT4_STATIC_IMPORTS: Dict[str, str] = {
    "assertEquals": "import static org.junit.Assert.assertEquals;",
    "assertTrue": "import static org.junit.Assert.assertTrue;",
    "assertFalse": "import static org.junit.Assert.assertFalse;",
    "assertNotNull": "import static org.junit.Assert.assertNotNull;",
    "assertNull": "import static org.junit.Assert.assertNull;",
    "assertSame": "import static org.junit.Assert.assertSame;",
    "assertNotSame": "import static org.junit.Assert.assertNotSame;",
    "assertArrayEquals": "import static org.junit.Assert.assertArrayEquals;",
    "fail": "import static org.junit.Assert.fail;",
}


BANNED_IMPORT_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"^\s*import\s+(?:static\s+)?org\.junit\.jupiter\.[^;]+;", re.MULTILINE),
    re.compile(r"^\s*import\s+(?:static\s+)?org\.junit\.platform\.[^;]+;", re.MULTILINE),
]


BANNED_METHOD_CALLS: Tuple[str, ...] = (
    "assertThrows",
    "assertDoesNotThrow",
    "assertAll",
    "assumeTrue",
    "assumeFalse",
)


_IMPORT_LINE_RE = re.compile(r"^\s*import\s+(?:static\s+)?([^;]+);\s*$", re.MULTILINE)
_WILDCARD_ASSERT_RE = re.compile(
    r"^\s*import\s+static\s+org\.junit\.Assert\s*\.\s*\*\s*;\s*$",
    re.MULTILINE,
)


class ImportManager:
    def existing_imports(self, file_text: str) -> Set[str]:
        return set(_IMPORT_LINE_RE.findall(file_text))

    def has_wildcard_assert(self, file_text: str) -> bool:
        return bool(_WILDCARD_ASSERT_RE.search(file_text))

    def reconcile(
        self,
        file_text: str,
        used_asserts: Set[str],
        original_imports: Set[str] | None = None,
    ) -> Tuple[str, List[str]]:
        """Return (new_file_text, notes).

        - Strips banned imports that were NOT present in the original file.
        - Adds junit4 static imports for any used assert not already covered
          (by either explicit single import or wildcard Assert.*).
        """
        notes: List[str] = []
        text = file_text

        original_imports = original_imports or set()

        # --- strip banned imports that weren't there originally -------------
        def _strip_if_added(match: "re.Match[str]") -> str:
            line = match.group(0).strip()
            body_match = re.match(r"import\s+(?:static\s+)?([^;]+);", line)
            fqn = body_match.group(1).strip() if body_match else line
            if fqn in original_imports:
                return line  # preserve
            notes.append(f"stripped_banned:{fqn}")
            return ""

        for pat in BANNED_IMPORT_PATTERNS:
            text = pat.sub(_strip_if_added, text)

        # collapse back-to-back blank lines left by stripping
        text = re.sub(r"\n{3,}", "\n\n", text)

        # --- add missing junit4 static imports ------------------------------
        if self.has_wildcard_assert(text):
            return text, notes

        present = self.existing_imports(text)
        insertion_lines: List[str] = []
        for name in sorted(used_asserts):
            import_line = JUNIT4_STATIC_IMPORTS.get(name)
            if not import_line:
                continue
            fqn = re.match(r"import\s+(?:static\s+)?([^;]+);", import_line).group(1)  # type: ignore[union-attr]
            if fqn in present:
                continue
            insertion_lines.append(import_line)
            notes.append(f"added:{fqn}")

        if insertion_lines:
            text = self._insert_imports(text, insertion_lines)

        return text, notes

    def check_banned_calls(self, file_text: str) -> List[str]:
        hits: List[str] = []
        for name in BANNED_METHOD_CALLS:
            if re.search(rf"\b{name}\s*\(", file_text):
                hits.append(name)
        return hits

    def _insert_imports(self, text: str, new_imports: List[str]) -> str:
        """Insert new imports after the last existing import line, or after
        the package declaration, or at the top."""
        lines = text.splitlines(keepends=True)
        # find last import line
        last_import = -1
        package_line = -1
        for i, ln in enumerate(lines):
            if re.match(r"^\s*import\s", ln):
                last_import = i
            elif re.match(r"^\s*package\s", ln):
                package_line = i
        insert_at = (last_import + 1) if last_import >= 0 else (package_line + 2 if package_line >= 0 else 0)
        block = "\n".join(new_imports) + "\n"
        # ensure a blank line before the block if inserting after package
        if last_import < 0 and package_line >= 0:
            lines.insert(insert_at, "\n")
            insert_at += 1
        lines.insert(insert_at, block)
        return "".join(lines)

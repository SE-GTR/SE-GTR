"""7-gate validator:

  1. Banned pattern (no JUnit 5 imports/calls introduced)
  2. Syntax (balanced braces/parens/brackets)
  3. Compile (ant compile)
  4. Test execution (JUnitCore on the affected class)
  5. Coverage preservation (stub in Phase 1)
  6. Smell substitution (lightweight: no net increase in NNA)
  7. Assertion preservation (assertion count non-decreasing)

Failing any gate restores the original file text and returns the reason.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .base import ExecutionContext
from .catalog import ASSERT_CALL_RE
from .import_manager import BANNED_METHOD_CALLS


@dataclass
class ValidatorConfig:
    project_root: Path
    test_file: Path
    ant_cmd: str = "ant"
    java_cmd: str = "java"
    compile_targets: Tuple[str, ...] = ("compile", "compile-evosuite")
    compile_timeout_sec: int = 1800
    test_timeout_sec: int = 600
    skip_compile: bool = False
    skip_tests: bool = False
    coverage_delta_floor: float = -0.02
    original_imports: set[str] = field(default_factory=set)
    # RQ3 naive-LLM baseline only: disable Gate 6 (smell substitution) and
    # Gate 7 (assertion preservation) so we can measure what the repair
    # looks like under compile/test/coverage gates alone. SE-GTR's own
    # conditions always keep these gates on.
    skip_gate6_gate7: bool = False


_BANNED_NEW_IMPORT_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*import\s+(?:static\s+)?org\.junit\.jupiter\.[^;]+;", re.MULTILINE),
    re.compile(r"^\s*import\s+(?:static\s+)?org\.junit\.platform\.[^;]+;", re.MULTILINE),
)


class MultiGateValidator:
    def __init__(self, cfg: ValidatorConfig):
        self.cfg = cfg

    # ---- public API -------------------------------------------------------

    def validate(
        self,
        original_text: str,
        modified_text: str,
        ctx: Optional[ExecutionContext] = None,
        original_coverage: Optional[Dict[str, float]] = None,
    ) -> Tuple[bool, str]:
        banned = self._check_banned(original_text, modified_text)
        if banned:
            return False, f"gate1_banned:{banned}"

        if not self._balanced(modified_text):
            return False, "gate2_syntax:unbalanced"

        # write modified text to disk so ant + JUnitCore can see it
        self.cfg.test_file.write_text(modified_text, encoding="utf-8")

        if not self.cfg.skip_compile:
            compile_err = self._try_compile()
            if compile_err:
                self._restore(original_text)
                return False, f"gate3_compile:{compile_err[:200]}"

        if not self.cfg.skip_tests:
            test_err = self._try_run_tests()
            if test_err:
                self._restore(original_text)
                return False, f"gate4_test:{test_err[:200]}"

        delta = self._measure_coverage_delta(original_coverage)
        if delta is not None and delta < self.cfg.coverage_delta_floor:
            self._restore(original_text)
            return False, f"gate5_coverage:{delta:.4f}"

        # Gate 5 (proxy): regex-based lightweight check — per-plan JaCoCo would
        # add >500 ms × N plans so we only run full coverage once per project
        # (in pipeline_v2 / baseline scripts). The proxy rejects two patterns
        # that accounted for the bulk of v1's coverage collapse:
        #   (a) losing test methods entirely, and
        #   (b) introducing empty / no-op test bodies.
        proxy_ok, proxy_reason = _gate5_coverage_proxy(original_text, modified_text)
        if not proxy_ok:
            self._restore(original_text)
            return False, f"gate5_coverage_proxy:{proxy_reason}"

        if not self.cfg.skip_gate6_gate7:
            nna_gain = self._count_nna_introduced(original_text, modified_text)
            if nna_gain > 0:
                self._restore(original_text)
                return False, f"gate6_smell_sub:nna+{nna_gain}"

            # Gate 7 counts "meaningful" assertions (excluding assertNotNull, which
            # is the primary target of NNA repair). A method that loses only
            # assertNotNull calls while keeping all other assertions still passes.
            orig_meaningful = self._count_meaningful_assertions(original_text)
            new_meaningful = self._count_meaningful_assertions(modified_text)
            if new_meaningful < orig_meaningful:
                self._restore(original_text)
                return False, f"gate7_assert_loss:{orig_meaningful}->{new_meaningful}"
            if self._count_assertions(modified_text) < 1:
                self._restore(original_text)
                return False, "gate7_no_assertions_left"

        return True, "accepted"

    # ---- gate helpers -----------------------------------------------------

    def _check_banned(self, original: str, modified: str) -> Optional[str]:
        for name in BANNED_METHOD_CALLS:
            old_hits = len(re.findall(rf"\b{name}\s*\(", original))
            new_hits = len(re.findall(rf"\b{name}\s*\(", modified))
            if new_hits > old_hits:
                return f"new_call:{name}"
        for pat in _BANNED_NEW_IMPORT_PATTERNS:
            old_hits = len(pat.findall(original))
            new_hits = len(pat.findall(modified))
            if new_hits > old_hits:
                return f"new_import:{pat.pattern[:40]}"
        return None

    def _balanced(self, text: str) -> bool:
        # strip strings/comments before checking; simple state machine
        stripped = _strip_strings_and_comments(text)
        counts = {"(": 0, ")": 0, "[": 0, "]": 0, "{": 0, "}": 0}
        for ch in stripped:
            if ch in counts:
                counts[ch] += 1
        return (
            counts["("] == counts[")"]
            and counts["["] == counts["]"]
            and counts["{"] == counts["}"]
        )

    def _try_compile(self) -> Optional[str]:
        cmd = [self.cfg.ant_cmd, *self.cfg.compile_targets]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.cfg.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.cfg.compile_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "compile_timeout"
        except Exception as e:
            return f"compile_exception:{type(e).__name__}:{e}"
        if proc.returncode != 0:
            return _extract_compile_error(proc.stdout)
        return None

    def _try_run_tests(self) -> Optional[str]:
        fqcn = _test_class_fqcn(self.cfg.test_file)
        cp = _build_sf110_classpath(self.cfg.project_root)
        cmd = [self.cfg.java_cmd, "-cp", cp, "org.junit.runner.JUnitCore", fqcn]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.cfg.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.cfg.test_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "test_timeout"
        except Exception as e:
            return f"test_exception:{type(e).__name__}:{e}"
        if proc.returncode != 0:
            return _extract_test_error(proc.stdout)
        return None

    def _measure_coverage_delta(
        self, original_coverage: Optional[Dict[str, float]]
    ) -> Optional[float]:
        # Phase 1 stub — JaCoCo integration deferred to Phase 3.
        return None

    def _count_nna_introduced(self, original: str, modified: str) -> int:
        orig = len(re.findall(r"\bassertNotNull\s*\(", original))
        new = len(re.findall(r"\bassertNotNull\s*\(", modified))
        return max(0, new - orig)

    def _count_assertions(self, text: str) -> int:
        return len(ASSERT_CALL_RE.findall(text))

    def _count_meaningful_assertions(self, text: str) -> int:
        # Exclude assertions that convey little or no information about the
        # system under test: assertNotNull (subject of NNA repair) and fail()
        # (replaced by @Test(expected=...) annotation in TSES repair).
        weak = len(re.findall(r"\b(?:assertNotNull|fail)\s*\(", text))
        return self._count_assertions(text) - weak

    def _restore(self, original_text: str) -> None:
        self.cfg.test_file.write_text(original_text, encoding="utf-8")


# ----------------------------------------------------------------------------
# internal helpers (ported from pipeline.py)
# ----------------------------------------------------------------------------


def _read_java_package(java_file: Path) -> Optional[str]:
    try:
        with java_file.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                line_s = line.strip()
                if line_s.startswith("package "):
                    return line_s[len("package ") :].rstrip(";").strip()
                if line_s.startswith(("public class", "class ")):
                    break
    except Exception:
        return None
    return None


def _test_class_fqcn(test_file: Path) -> str:
    pkg = _read_java_package(test_file)
    cls = test_file.stem
    return f"{pkg}.{cls}" if pkg else cls


def _list_jars(root: Path) -> List[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(root.rglob("*.jar"))


def _build_sf110_classpath(project_root: Path) -> str:
    entries: List[Path] = []
    for sub in ("build/classes", "build/evosuite"):
        p = project_root / sub
        if p.exists():
            entries.append(p)
    entries += _list_jars(project_root / "lib")
    entries += _list_jars(project_root / "test-lib")
    shared = project_root.parent / "lib"
    entries += _list_jars(shared)
    seen = set()
    out: List[str] = []
    for p in entries:
        s = str(p.resolve())
        if s not in seen:
            out.append(s)
            seen.add(s)
    return os.pathsep.join(out)


# ----------------------------------------------------------------------------
# Gate 5 coverage proxy
# ----------------------------------------------------------------------------

_TEST_ANNOT_RE = re.compile(r"@Test\b")

# Assertion-call prefixes that should NOT count as "executable code" for the
# statement-density proxy (Gate 7 already enforces assertion preservation).
_ASSERT_CALL_NAMES = (
    "assertEquals", "assertTrue", "assertFalse", "assertNotNull",
    "assertNull", "assertSame", "assertNotSame", "assertArrayEquals",
    "fail", "verifyException",
)
_ASSERT_LINE_RE = re.compile(
    r"^\s*(?:" + "|".join(_ASSERT_CALL_NAMES) + r")\s*\("
)

# Find each test-method body: between its opening `{` and the matching `}`.
# Tolerates @Test on its own line (typical EvoSuite) or inline with the
# signature. The closing `}` is assumed to start its own line (EvoSuite
# and IDE-generated tests are consistently formatted).
_TEST_METHOD_BODY_RE = re.compile(
    r"(?ms)@Test\b.*?"
    r"void\s+test\w+\s*\([^)]*\)\s*(?:throws[^\{]+)?\{"
    r"(?P<body>.*?)^\s*\}"
)

# Ratio threshold: a modification may drop at most this fraction of the
# executable-statement count before Gate 5 proxy rejects it. Tuned loose
# (−30%) so healthy operator transforms (CAPTURE, ASSERT replace, try-catch
# removal) stay well within the budget.
_STATEMENT_LOSS_THRESHOLD = -0.30


def _count_test_methods(text: str) -> int:
    return len(_TEST_ANNOT_RE.findall(text))


def _iter_test_bodies(text: str) -> List[str]:
    return [m.group("body") for m in _TEST_METHOD_BODY_RE.finditer(text)]


def _count_executable_statements(text: str) -> int:
    """Approximate count of non-assertion, non-comment Java statements in
    all test method bodies. Counts each line ending in `;` (after stripping
    comments/strings) that is not an assertion/fail/verifyException call.
    """
    total = 0
    for body in _iter_test_bodies(text):
        stripped = _strip_strings_and_comments(body)
        for raw in stripped.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("//", "/*", "*")):
                continue
            if _ASSERT_LINE_RE.match(line):
                continue
            if not line.endswith(";"):
                continue
            if line in ("{", "}", ";"):
                continue
            total += 1
    return total


def _has_empty_test_body(text: str) -> bool:
    """True iff any test method body has zero executable statements AND
    zero assertions — i.e., the whole body collapsed to `{}` or comments
    only.
    """
    for body in _iter_test_bodies(text):
        stripped = _strip_strings_and_comments(body).strip()
        if not stripped:
            return True
        # only whitespace / trivial tokens?
        lines = [l.strip() for l in stripped.splitlines() if l.strip()]
        if not lines:
            return True
    return False


def _gate5_coverage_proxy(
    original_text: str, modified_text: str
) -> Tuple[bool, str]:
    """Regex-based Gate 5 stand-in (see MultiGateValidator docstring). Runs
    in < 1 ms per plan. Three checks:
      1. No test method was deleted.
      2. No test body became empty when it wasn't before.
      3. Executable-statement count did not drop by > 30 %.
    """
    orig_tests = _count_test_methods(original_text)
    new_tests = _count_test_methods(modified_text)
    if new_tests < orig_tests:
        return False, f"test_count_lost:{orig_tests}->{new_tests}"

    orig_empty = _has_empty_test_body(original_text)
    new_empty = _has_empty_test_body(modified_text)
    if new_empty and not orig_empty:
        return False, "empty_body_introduced"

    orig_stmts = _count_executable_statements(original_text)
    new_stmts = _count_executable_statements(modified_text)
    if orig_stmts > 0:
        delta = (new_stmts - orig_stmts) / orig_stmts
        if delta < _STATEMENT_LOSS_THRESHOLD:
            return False, f"statement_loss:{delta:.2%}"
    return True, "coverage_proxy_ok"


_COMPILE_ERR_RE = re.compile(r"(?m)^.*\.java:\d+: error: .+$")


def _extract_compile_error(output: str) -> str:
    hits = _COMPILE_ERR_RE.findall(output)
    return hits[0] if hits else output.strip().splitlines()[-1] if output.strip() else "compile_error"


_TEST_FAIL_RE = re.compile(r"(?m)^(?:FAILURES!!!|OK \(\d+)")


def _extract_test_error(output: str) -> str:
    for line in output.splitlines():
        if "FAILURES" in line or "Tests run:" in line:
            return line.strip()
    return output.strip().splitlines()[-1] if output.strip() else "test_error"


def _strip_strings_and_comments(text: str) -> str:
    """Remove Java string literals and comments so brace/paren counting is safe."""
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # line comment
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # block comment
        if ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        # string literal
        if ch == '"':
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        # char literal
        if ch == "'":
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)

"""Dynamic context collector for Tier 4 (NASE / TSVM).

Approach (chosen in Phase 2.4 planning, Q1-3 Option a): inject capture code
directly into the test-method source, recompile, run once with JUnitCore,
parse the stdout-emitted ``SE-GTR-DIFF-*`` markers, then unconditionally
restore the original file.

Why stdout markers + source injection (vs. JVMTI/ASM-based runtime hooks)?
  * zero dependencies on external instrumentation frameworks
  * works inside EvoSuite's EvoClassLoader without special handling
  * failures are observable (parse error) and recoverable (rollback)
  * the collected evidence is human-readable in the raw log

Rollback contract:
  - ``collect()`` ALWAYS restores the original test file, even when every
    intermediate step throws. A try/finally wraps the in-place edit.
  - The pre-edit bytes are held in memory AND written to ``<file>.segtr-bak``
    as a belt-and-suspenders guard against process kill.
"""
from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from smell_repair_v2.operators.validator import (
    _build_sf110_classpath,
    _test_class_fqcn,
)
from smell_repair_v2.project.ant import run_ant


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GetterInfo:
    name: str                 # e.g. "getX"
    return_type: str          # e.g. "int"
    line: int                 # 1-indexed line in the source file it was found in
    source: str = "cut"       # "cut" or "parent:<FQCN>" — for logging / debugging


@dataclass
class DynamicEvidence:
    state_before: Dict[str, str] = field(default_factory=dict)
    state_after: Dict[str, str] = field(default_factory=dict)
    stdout: str = ""
    capture_success: bool = False
    error: Optional[str] = None
    act_call_line: Optional[int] = None
    captured_getters: List[str] = field(default_factory=list)
    # {"getX": "cut", "getBorderColor": "parent:jahuwaldt.plot.PlotSymbol"}
    getter_sources: Dict[str, str] = field(default_factory=dict)
    stdout_tail: str = ""       # last 400 chars — useful in logs
    elapsed_ms: int = 0

    def changed_fields(self) -> Dict[str, Tuple[str, str]]:
        """Return {getter_name: (before, after)} only where before != after."""
        out: Dict[str, Tuple[str, str]] = {}
        for k, after_val in self.state_after.items():
            before_val = self.state_before.get(k)
            if before_val is not None and before_val != after_val:
                out[k] = (before_val, after_val)
        return out

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["changed_fields"] = {
            k: {"before": v[0], "after": v[1]} for k, v in self.changed_fields().items()
        }
        return d


# ---------------------------------------------------------------------------
# CUT getter extraction
# ---------------------------------------------------------------------------

# Match method declarations with a concrete modifier set. We require an
# explicit `public` modifier to skip package-private helpers, and reject
# anything `static` (we need instance-level observables).
_METHOD_DECL_RE = re.compile(
    r"(?ms)^(?P<indent>[ \t]*)"
    r"(?P<mods>(?:@[A-Za-z_][\w\.]*(?:\s*\([^)]*\))?\s+)*"
    r"(?:public\s+|protected\s+|private\s+|static\s+|final\s+|synchronized\s+"
    r"|native\s+|abstract\s+|default\s+)+)"
    r"(?:<[^>]+>\s+)?"
    r"(?P<rtype>[A-Za-z_][\w\.\<\>,\[\]]*?(?:\s*\[\s*\])*)"
    r"\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<params>[^\)]*)\)\s*(?:throws[^\{]+)?\{"
)

_GETTER_NAME_RE = re.compile(r"^(?:get[A-Z]\w*|is[A-Z]\w*|has[A-Z]\w*|size|length|isEmpty|count|toString)$")


def extract_observable_getters(
    cut_source: str,
    *,
    max_getters: int = 12,
    source_tag: str = "cut",
) -> List[GetterInfo]:
    """Scan a single Java source string for public non-static zero-arg non-void
    methods whose names look like observers. Returns at most ``max_getters``
    entries, in source order — earlier names (`getX`, `isEmpty`) tend to be
    the most stable/observable ones to assert on.

    ``source_tag`` is stamped into each returned GetterInfo; callers use this
    to distinguish CUT-declared from parent-inherited getters when scanning
    multiple files.
    """
    out: List[GetterInfo] = []
    for m in _METHOD_DECL_RE.finditer(cut_source):
        mods = m.group("mods") or ""
        if "public" not in mods:
            continue
        if "static" in mods:
            continue
        if "abstract" in mods:
            continue
        params = (m.group("params") or "").strip()
        if params:
            continue
        rtype = (m.group("rtype") or "").strip()
        if rtype == "void":
            continue
        name = m.group("name")
        if not _GETTER_NAME_RE.match(name):
            continue
        line = cut_source.count("\n", 0, m.start()) + 1
        out.append(GetterInfo(name=name, return_type=rtype, line=line, source=source_tag))
        if len(out) >= max_getters:
            break
    return out


# Matches `extends Foo` (generics allowed) on the class header line. We don't
# attempt to resolve nested generics or outer qualifiers — just the top-level
# simple name, which is what we need for single-step parent scan.
_EXTENDS_RE = re.compile(r"\bclass\s+\w+[^{]*?\bextends\s+([A-Za-z_]\w*)(?:<[^>]*>)?")
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w\.]*)\s*;", re.MULTILINE)
_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w\.]*)\s*;", re.MULTILINE)


def _is_jdk_class(fqcn: str) -> bool:
    return fqcn.startswith("java.") or fqcn.startswith("javax.") or fqcn.startswith("sun.")


def resolve_parent_fqcn(cut_source: str) -> Optional[str]:
    """Parse the CUT source and return the FQCN of its direct superclass, or
    None if the class has no `extends` clause (implicit Object).

    Resolution order:
      1. If an `import foo.bar.Parent` matches the simple name, use it.
      2. Else qualify with the CUT's own package.
      3. JDK parents are returned as-is (callers typically skip these).
    """
    em = _EXTENDS_RE.search(cut_source)
    if not em:
        return None
    simple = em.group(1)
    for im in _IMPORT_RE.finditer(cut_source):
        imp = im.group(1)
        if imp.endswith("." + simple):
            return imp
    pm = _PACKAGE_RE.search(cut_source)
    if pm:
        return pm.group(1) + "." + simple
    return simple


def _find_parent_source(
    parent_fqcn: str,
    project_src_root: Path,
) -> Optional[Path]:
    """Locate ``<project_src_root>/<FQCN-as-path>.java``. Returns None if the
    parent is a JDK class or the source is not in the project tree."""
    if _is_jdk_class(parent_fqcn):
        return None
    rel = Path(*parent_fqcn.split(".")).with_suffix(".java")
    p = project_src_root / rel
    if p.exists():
        return p
    simple = parent_fqcn.rsplit(".", 1)[-1]
    for cand in project_src_root.rglob(simple + ".java"):
        # Verify package matches to avoid collisions in big projects.
        text = cand.read_text(encoding="utf-8", errors="ignore")
        pm = _PACKAGE_RE.search(text)
        want_pkg = parent_fqcn.rsplit(".", 1)[0] if "." in parent_fqcn else ""
        if (pm.group(1) if pm else "") == want_pkg:
            return cand
    return None


def collect_observable_getters(
    cut_source: str,
    *,
    project_src_root: Optional[Path] = None,
    max_getters: int = 12,
) -> Tuple[List[GetterInfo], Dict[str, Any]]:
    """CUT-plus-one-parent getter discovery.

    Scans the CUT source, then (if ``project_src_root`` is provided and the
    CUT has a non-JDK direct parent whose source exists in the project tree)
    also scans that parent. Child-declared getters shadow parent-declared
    ones of the same name.

    Returns ``(getters, info)`` where ``info`` is a small debug dict:
      {"parent_fqcn": str|None, "parent_source": str|None,
       "cut_count": int, "parent_count": int, "skipped_parent": str|None}
    """
    info: Dict[str, Any] = {
        "parent_fqcn": None,
        "parent_source": None,
        "cut_count": 0,
        "parent_count": 0,
        "skipped_parent": None,
    }
    cut_getters = extract_observable_getters(cut_source, max_getters=max_getters, source_tag="cut")
    info["cut_count"] = len(cut_getters)
    seen = {g.name for g in cut_getters}

    if project_src_root is None or len(cut_getters) >= max_getters:
        return cut_getters, info

    parent_fqcn = resolve_parent_fqcn(cut_source)
    if not parent_fqcn:
        return cut_getters, info
    info["parent_fqcn"] = parent_fqcn
    if _is_jdk_class(parent_fqcn):
        info["skipped_parent"] = f"jdk:{parent_fqcn}"
        return cut_getters, info

    parent_file = _find_parent_source(parent_fqcn, project_src_root)
    if parent_file is None:
        info["skipped_parent"] = "source_not_found"
        return cut_getters, info
    info["parent_source"] = str(parent_file)

    parent_text = parent_file.read_text(encoding="utf-8", errors="ignore")
    remaining = max_getters - len(cut_getters)
    parent_getters_all = extract_observable_getters(
        parent_text, max_getters=max_getters, source_tag=f"parent:{parent_fqcn}",
    )
    picked: List[GetterInfo] = []
    for g in parent_getters_all:
        if g.name in seen:
            continue
        picked.append(g)
        seen.add(g.name)
        if len(picked) >= remaining:
            break
    info["parent_count"] = len(picked)
    return cut_getters + picked, info


# ---------------------------------------------------------------------------
# Instrumentation code generation
# ---------------------------------------------------------------------------

_MARKER_BEFORE = "SE-GTR-DIFF-BEFORE:"
_MARKER_AFTER = "SE-GTR-DIFF-AFTER:"
_MARKER_ERROR = "SE-GTR-DIFF-ERROR:"


def build_instrumentation_block(
    cut_var: str,
    getters: List[GetterInfo],
    phase: str,           # "before" or "after"
    indent: str = "        ",
) -> str:
    """Emit a Java block that captures each getter's value and prints it
    with the phase-specific stdout marker.

    Every getter invocation is wrapped in its own try/catch so one broken
    observable does not lose the rest. The whole block is itself wrapped so
    even a VM-level error emits a single parseable ERROR marker.
    """
    if phase not in ("before", "after"):
        raise ValueError(f"phase must be before/after, got {phase!r}")
    marker = _MARKER_BEFORE if phase == "before" else _MARKER_AFTER
    var_name = f"__segtr_{phase}"

    lines: List[str] = []
    lines.append(f"{indent}// === SE-GTR dynamic-capture: {phase} ===")
    lines.append(f"{indent}try {{")
    lines.append(f"{indent}    java.util.Map<String,String> {var_name} = new java.util.LinkedHashMap<String,String>();")
    for g in getters:
        key = f"{g.name}()"
        # String.valueOf handles null, primitives, and objects uniformly.
        lines.append(
            f"{indent}    try {{ {var_name}.put(\"{key}\", String.valueOf({cut_var}.{g.name}())); }} "
            f"catch (Throwable __e) {{ {var_name}.put(\"{key}\", \"ERR:\" + __e.getClass().getSimpleName()); }}"
        )
    lines.append(f"{indent}    for (java.util.Map.Entry<String,String> __e : {var_name}.entrySet()) {{")
    lines.append(f"{indent}        System.out.println(\"{marker}\" + __e.getKey() + \"=\" + __e.getValue());")
    lines.append(f"{indent}    }}")
    lines.append(f"{indent}}} catch (Throwable __e) {{")
    lines.append(f"{indent}    System.out.println(\"{_MARKER_ERROR}{phase}:\" + __e.getClass().getSimpleName());")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Injection (single-file edit with guaranteed rollback)
# ---------------------------------------------------------------------------


def _detect_line_indent(line: str) -> str:
    m = re.match(r"(\s*)", line)
    return m.group(1) if m else "        "


def inject_at_line(
    file_text: str,
    act_call_line: int,           # 1-indexed
    before_code: str,
    after_code: str,
) -> str:
    """Return ``file_text`` with ``before_code`` inserted immediately before
    ``act_call_line`` and ``after_code`` immediately after (both as standalone
    blocks). Preserves indentation of the act-call line.
    """
    lines = file_text.splitlines(keepends=True)
    if not (1 <= act_call_line <= len(lines)):
        raise ValueError(f"act_call_line {act_call_line} out of range [1, {len(lines)}]")
    act_line_text = lines[act_call_line - 1]
    # Make sure the inserted blocks end with a newline.
    def _ensure_nl(block: str) -> str:
        return block if block.endswith("\n") else block + "\n"
    before = _ensure_nl(before_code)
    after = _ensure_nl(after_code)
    out = lines[: act_call_line - 1] + [before] + [act_line_text] + [after] + lines[act_call_line:]
    return "".join(out)


# ---------------------------------------------------------------------------
# Stdout marker parsing
# ---------------------------------------------------------------------------


def parse_markers(stdout: str) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
    """Parse stdout into (state_before, state_after, error_messages).

    JUnitCore prints per-test progress dots without a trailing newline, so
    the first marker line in a run often arrives as ``"..............SE-GTR-DIFF-BEFORE:getX()=0"``.
    We search for the marker token anywhere in the line (not just at the
    start) to survive that prefix noise.
    """
    before: Dict[str, str] = {}
    after: Dict[str, str] = {}
    errors: List[str] = []

    def _find_payload(line: str, marker: str) -> Optional[str]:
        idx = line.find(marker)
        if idx < 0:
            return None
        return line[idx + len(marker):]

    for raw in stdout.splitlines():
        line = raw.strip()
        payload = _find_payload(line, _MARKER_BEFORE)
        if payload is not None:
            if "=" in payload:
                k, v = payload.split("=", 1)
                before[k.strip()] = v
            continue
        payload = _find_payload(line, _MARKER_AFTER)
        if payload is not None:
            if "=" in payload:
                k, v = payload.split("=", 1)
                after[k.strip()] = v
            continue
        payload = _find_payload(line, _MARKER_ERROR)
        if payload is not None:
            errors.append(payload)
    return before, after, errors


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class DynamicContextCollectorError(RuntimeError):
    pass


class DynamicContextCollector:
    """Orchestrates one NASE/TSVM dynamic capture on a single test method."""

    def __init__(
        self,
        project_root: Path,
        *,
        ant_cmd: str = "ant",
        java_cmd: str = "java",
        compile_timeout_sec: int = 600,
        test_timeout_sec: int = 120,
        max_getters: int = 12,
    ):
        self.project_root = Path(project_root)
        self.ant_cmd = ant_cmd
        self.java_cmd = java_cmd
        self.compile_timeout = compile_timeout_sec
        self.test_timeout = test_timeout_sec
        self.max_getters = max_getters

    # -- main entry point -------------------------------------------------

    def collect(
        self,
        *,
        test_file: Path,
        test_method_name: str,
        act_call_info: Dict[str, Any],
        cut_source: Optional[str],
        cut_fqcn: Optional[str] = None,
    ) -> DynamicEvidence:
        """Attempt one dynamic capture. See module docstring for contract."""
        t0 = time.monotonic()
        ev = DynamicEvidence()

        if not cut_source:
            ev.error = "cut_source_missing"
            ev.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return ev

        cut_var = (act_call_info or {}).get("scope")
        if not cut_var:
            ev.error = "act_call_scope_missing"
            ev.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return ev

        # Scan CUT + direct parent class (Option B). If project_src_root is
        # unavailable, falls back to CUT-only without error.
        src_root = (self.project_root / "src" / "main" / "java")
        getters, gi = collect_observable_getters(
            cut_source,
            project_src_root=src_root if src_root.exists() else None,
            max_getters=self.max_getters,
        )
        if not getters:
            ev.error = "no_observable_getters"
            ev.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return ev
        ev.captured_getters = [g.name for g in getters]
        ev.getter_sources = {g.name: g.source for g in getters}

        # Locate the act call on disk (evidence line may be stale if prior
        # tiers edited the file). Fall back to text search for `scope.name(`.
        original_text = test_file.read_text(encoding="utf-8", errors="ignore")
        act_line = self._locate_act_call(
            original_text, test_method_name,
            evidence_line=(act_call_info or {}).get("begin_line"),
            scope=cut_var,
            method_name=(act_call_info or {}).get("name"),
        )
        if act_line is None:
            ev.error = "act_call_not_located"
            ev.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return ev
        ev.act_call_line = act_line

        # Build + inject instrumentation, keeping a rollback copy.
        lines = original_text.splitlines(keepends=True)
        target_indent = _detect_line_indent(lines[act_line - 1])
        before_code = build_instrumentation_block(cut_var, getters, "before", indent=target_indent)
        after_code = build_instrumentation_block(cut_var, getters, "after", indent=target_indent)
        instrumented = inject_at_line(original_text, act_line, before_code, after_code)

        backup_path = test_file.with_suffix(test_file.suffix + ".segtr-bak")
        try:
            backup_path.write_text(original_text, encoding="utf-8")
            test_file.write_text(instrumented, encoding="utf-8")
            # Ant's javac uptodate check compares file timestamps at 1-second
            # resolution on many setups. Right after a fresh compile, the
            # existing .class and the just-written .java share the same
            # integer second → javac skips recompile and our markers never
            # land in the bytecode. Push the .java mtime a couple seconds
            # ahead to guarantee it looks strictly newer.
            _future = time.time() + 2
            os.utime(test_file, (_future, _future))

            # 1. compile (incremental is fine — we restore immediately after)
            if not self._try_compile():
                ev.error = "compile_error"
                return ev

            # 2. run JUnitCore on this one test class, capture stdout
            fqcn = _test_class_fqcn(test_file)
            stdout, rc, timed_out = self._try_run(fqcn)
            ev.stdout = stdout
            ev.stdout_tail = stdout[-400:] if stdout else ""
            if timed_out:
                ev.error = "test_timeout"
                return ev

            # 3. parse markers (available regardless of test pass/fail)
            before, after, errors = parse_markers(stdout)
            ev.state_before = before
            ev.state_after = after

            if not before and not after:
                # No markers emitted — either injection never ran (bad line)
                # or the agent/class-loader dropped stdout. Treat as failure.
                ev.error = f"no_markers:rc={rc}:tail={ev.stdout_tail[:120]}"
                return ev
            ev.capture_success = True
            if errors:
                # partial capture — still counts as success but flag the issue
                ev.error = "partial:" + ";".join(errors[:3])
            return ev
        except Exception as e:
            ev.error = f"{type(e).__name__}:{e}"
            return ev
        finally:
            # Always restore original file. Leave backup in place only if
            # the restore itself fails.
            try:
                test_file.write_text(original_text, encoding="utf-8")
                if backup_path.exists():
                    backup_path.unlink()
            except Exception:
                pass
            ev.elapsed_ms = int((time.monotonic() - t0) * 1000)

    # -- helpers ----------------------------------------------------------

    def _try_compile(self) -> bool:
        try:
            run_ant(self.project_root, ["compile", "compile-evosuite"],
                    timeout_sec=self.compile_timeout)
            return True
        except Exception:
            return False

    def _try_run(self, fqcn: str) -> Tuple[str, int, bool]:
        cp = _build_sf110_classpath(self.project_root)
        try:
            p = subprocess.run(
                [self.java_cmd, "-cp", cp, "org.junit.runner.JUnitCore", fqcn],
                cwd=str(self.project_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=self.test_timeout, check=False,
            )
            return p.stdout or "", p.returncode, False
        except subprocess.TimeoutExpired as e:
            return (e.output or "") if isinstance(e.output, str) else "", -1, True

    def _locate_act_call(
        self,
        file_text: str,
        test_method_name: str,
        *,
        evidence_line: Optional[int],
        scope: str,
        method_name: Optional[str],
    ) -> Optional[int]:
        lines = file_text.splitlines()
        # Try evidence_line first if it references the right call.
        if evidence_line and 1 <= evidence_line <= len(lines):
            line_text = lines[evidence_line - 1]
            if scope in line_text and (not method_name or method_name in line_text):
                return evidence_line

        # Otherwise find the test method boundary and search within it.
        method_re = re.compile(
            rf"(?ms)@Test\b.*?void\s+{re.escape(test_method_name)}\s*\([^)]*\)\s*"
            rf"(?:throws[^\{{]+)?\{{(?P<body>.*?)^\s*\}}"
        )
        m = method_re.search(file_text)
        if m is None:
            return None
        body_start_char = m.start("body")
        body_start_line = file_text.count("\n", 0, body_start_char) + 1

        body_lines = m.group("body").splitlines()
        needle_specific = f"{scope}.{method_name}(" if method_name else f"{scope}."
        needle_generic = f"{scope}."
        hit_specific = None
        hit_generic = None
        for i, ln in enumerate(body_lines):
            if needle_specific in ln and hit_specific is None:
                hit_specific = body_start_line + i
            if needle_generic in ln and hit_generic is None:
                hit_generic = body_start_line + i
        return hit_specific or hit_generic

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Lightweight parsing utilities: best-effort extraction for EvoSuite generated tests and SF110 classes.
# This is NOT a full Java parser. It trades completeness for simple deployment.

METHOD_START_RE = re.compile(
    r"(?ms)^\s*(?:@[^\n]+\n\s*)*"  # annotations
    r"(?:public|protected|private|static|final|synchronized|native|abstract|\s)+"
    r"(?:<[^>]+>\s+)?"
    r"[\w\[\]<>,\.\s]+\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<params>[^\)]*)\)\s*"
    r"(?:throws[^\{]+)?\{"
)

TEST_METHOD_START_RE = re.compile(
    r"(?ms)^\s*(?:@Test[^\n]*\n\s*)*"
    r"(?:public\s+)?void\s+(?P<name>test\w+)\s*\([^\)]*\)\s*(?:throws[^\{]+)?\{"
)

VAR_DECL_RE = re.compile(
    r"(?m)^\s*(?:final\s+)?(?P<type>[A-Za-z_][\w\.<>,\[\]]*)\s+(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<rhs>.+?);\s*$"
)

CALL_ON_VAR_RE = re.compile(r"\b(?P<var>[A-Za-z_]\w*)\.(?P<method>[A-Za-z_]\w*)\s*\(")
CALL_ON_CLASS_RE = re.compile(r"\b(?P<class>[A-Za-z_]\w*)\.(?P<method>[A-Za-z_]\w*)\s*\(")
CALL_NAME_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
FIELD_RE = re.compile(
    r"(?m)^\s*(?:public|protected|private|static|final|\s)+"
    r"[\w\[\]<>,\.\s]+\s+[A-Za-z_]\w*\s*(?:=\s*[^;]+)?;"
)

JAVA_KEYWORD_LIKE = {
    "if", "for", "while", "switch", "catch", "new", "return", "throw", "super", "this",
    "assertTrue", "assertFalse", "assertEquals", "assertNotNull", "assertNull", "fail",
}


@dataclass(frozen=True)
class ExtractedContext:
    test_file: Path
    test_class_name: str
    test_method_name: str
    test_method_code: str
    cut_fqcn: Optional[str]
    cut_source_file: Optional[Path]
    cut_relevant_code: str


def _is_escaped(src: str, idx: int) -> bool:
    # Count consecutive backslashes immediately before idx
    cnt = 0
    j = idx - 1
    while j >= 0 and src[j] == "\\":
        cnt += 1
        j -= 1
    return (cnt % 2) == 1


def _scan_to_matching_brace(src: str, open_brace_idx: int) -> int:
    """Return index of matching '}' for the '{' at open_brace_idx. Best-effort, handles strings/comments."""
    depth = 0
    i = open_brace_idx
    n = len(src)
    in_sq = False  # '
    in_dq = False  # "
    in_sl_comment = False
    in_ml_comment = False
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if in_sl_comment:
            if ch == "\n":
                in_sl_comment = False
            i += 1
            continue
        if in_ml_comment:
            if ch == "*" and nxt == "/":
                in_ml_comment = False
                i += 2
                continue
            i += 1
            continue

        if not in_sq and not in_dq:
            if ch == "/" and nxt == "/":
                in_sl_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_ml_comment = True
                i += 2
                continue

        if ch == '"' and not in_sq:
            if not _is_escaped(src, i):
                in_dq = not in_dq
            i += 1
            continue
        if ch == "'" and not in_dq:
            if not _is_escaped(src, i):
                in_sq = not in_sq
            i += 1
            continue

        if in_sq or in_dq:
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def extract_method_block(src: str, method_name: str, start_re: re.Pattern) -> Optional[str]:
    for m in start_re.finditer(src):
        if m.group("name") != method_name:
            continue
        # opening brace is last char of match
        open_idx = m.end() - 1
        close_idx = _scan_to_matching_brace(src, open_idx)
        if close_idx == -1:
            return None
        return src[m.start() : close_idx + 1]
    return None


def extract_test_method(test_file: Path, test_method_name: str) -> str:
    src = test_file.read_text(encoding="utf-8", errors="ignore")
    blk = extract_method_block(src, test_method_name, TEST_METHOD_START_RE)
    if not blk:
        raise ValueError(f"Cannot locate test method {test_method_name} in {test_file}")
    return blk


def infer_cut_calls_from_test(
    test_method_code: str, cut_simple: str
) -> Set[str]:
    """Infer CUT method names invoked in the test method (best-effort)."""
    var_types: Dict[str, str] = {}
    for line in test_method_code.splitlines():
        md = VAR_DECL_RE.match(line)
        if not md:
            continue
        ty = md.group("type").strip()
        ty = ty.split("<", 1)[0].strip()
        var_types[md.group("var")] = ty

    invoked: Set[str] = set()

    for m in CALL_ON_VAR_RE.finditer(test_method_code):
        var = m.group("var")
        meth = m.group("method")
        if meth in JAVA_KEYWORD_LIKE:
            continue
        ty = var_types.get(var)
        if ty and (ty == cut_simple or ty.endswith("." + cut_simple)):
            invoked.add(meth)

    for m in CALL_ON_CLASS_RE.finditer(test_method_code):
        cls = m.group("class")
        meth = m.group("method")
        if cls == cut_simple and meth not in JAVA_KEYWORD_LIKE:
            invoked.add(meth)

    return invoked


def _index_class_methods(cut_src: str) -> Set[str]:
    names: Set[str] = set()
    for m in METHOD_START_RE.finditer(cut_src):
        nm = m.group("name")
        if nm not in JAVA_KEYWORD_LIKE:
            names.add(nm)
    return names


def _normalize_signature(sig: str) -> str:
    lines = [ln for ln in sig.splitlines() if not ln.strip().startswith("@")]
    compact = " ".join(" ".join(lines).split())
    if compact.endswith("{"):
        compact = compact[:-1].rstrip()
    return compact


def _extract_method_signatures(cut_src: str) -> Dict[str, str]:
    sigs: Dict[str, str] = {}
    for m in METHOD_START_RE.finditer(cut_src):
        name = m.group("name")
        if name in JAVA_KEYWORD_LIKE:
            continue
        raw = cut_src[m.start() : m.end()]
        sig = _normalize_signature(raw)
        if sig:
            sigs[name] = sig
    return sigs


def _extract_field_signatures(cut_src: str, *, max_fields: int = 40) -> List[str]:
    first_method = METHOD_START_RE.search(cut_src)
    head = cut_src[: first_method.start()] if first_method else cut_src
    fields: List[str] = []
    for line in head.splitlines():
        if "(" in line:
            continue
        m = FIELD_RE.match(line)
        if not m:
            continue
        ln = line.strip()
        if "//" in ln:
            ln = ln.split("//", 1)[0].strip()
        if ln:
            fields.append(ln)
        if max_fields and len(fields) >= max_fields:
            break
    return fields


def _extract_method_names_from_expr(expr: str) -> Set[str]:
    names: Set[str] = set()
    for m in CALL_NAME_RE.finditer(expr):
        nm = m.group(1)
        if nm not in JAVA_KEYWORD_LIKE:
            names.add(nm)
    return names


def infer_cut_calls_from_evidence(evidence_by_smell: Dict[str, object]) -> Set[str]:
    names: Set[str] = set()

    def visit(obj: object) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in {"called_method", "void_method_name", "method_name"} and isinstance(v, str):
                    names.add(v)
                    continue
                if k in {"expr", "signature", "name"} and isinstance(v, str):
                    names.update(_extract_method_names_from_expr(v))
                    continue
                if isinstance(v, (dict, list)):
                    visit(v)
        elif isinstance(obj, list):
            for it in obj:
                visit(it)

    visit(evidence_by_smell)
    return {n for n in names if n and n not in JAVA_KEYWORD_LIKE}


def extract_relevant_cut_code(
    cut_source_file: Path, initial_methods: Set[str], max_transitive_depth: int = 1
) -> str:
    src = cut_source_file.read_text(encoding="utf-8", errors="ignore")
    known_methods = _index_class_methods(src)
    selected: Set[str] = set(initial_methods) & known_methods
    frontier: Set[str] = set(selected)

    for _ in range(max_transitive_depth):
        next_frontier: Set[str] = set()
        for meth in list(frontier):
            blk = extract_method_block(src, meth, METHOD_START_RE)
            if not blk:
                continue
            # unqualified calls: foo(...)
            for cm in re.finditer(r"(?m)(?<!\.)\b([A-Za-z_]\w*)\s*\(", blk):
                callee = cm.group(1)
                if callee in known_methods and callee not in selected and callee not in JAVA_KEYWORD_LIKE:
                    next_frontier.add(callee)
        selected |= next_frontier
        frontier = next_frontier

    header_m = re.search(r"(?ms)^(.*?\bclass\b[^{]*\{)", src)
    header = header_m.group(1) if header_m else "\n".join(src.splitlines()[:80]) + "\n{"

    blocks: List[str] = [header]
    for meth in sorted(selected):
        blk = extract_method_block(src, meth, METHOD_START_RE)
        if blk:
            blocks.append("\n" + blk.strip() + "\n")
    blocks.append("}")
    return "\n".join(blocks)


def build_cut_signature_context(
    cut_source_file: Path,
    method_names: Set[str],
    *,
    include_fields: bool = True,
    max_methods: int = 80,
    max_fields: int = 40,
) -> str:
    src = cut_source_file.read_text(encoding="utf-8", errors="ignore")
    header_m = re.search(r"(?ms)^(.*?\bclass\b[^{]*\{)", src)
    header = header_m.group(1).strip() if header_m else f"class {cut_source_file.stem} {{"

    sigs = _extract_method_signatures(src)
    selected: List[str] = []
    if method_names:
        for nm in sorted(method_names):
            sig = sigs.get(nm)
            if sig:
                selected.append(sig)
    else:
        for nm in sorted(sigs.keys()):
            selected.append(sigs[nm])

    if max_methods and len(selected) > max_methods:
        selected = selected[:max_methods]

    lines: List[str] = [header]
    if include_fields:
        for fld in _extract_field_signatures(src, max_fields=max_fields):
            lines.append("  " + fld)
    for sig in selected:
        lines.append("  " + (sig if sig.endswith(";") else sig + ";"))
    lines.append("}")
    return "\n".join(lines)


def build_extracted_context(
    *,
    test_file: Path,
    test_class_name: str,
    test_method_name: str,
    cut_fqcn: Optional[str],
    cut_source_file: Optional[Path],
    max_transitive_depth: int = 1,
    extra_method_names: Optional[Set[str]] = None,
    cut_context_mode: str = "full",
    cut_context_max_chars: int = 0,
    cut_signature_include_fields: bool = True,
    cut_signature_max_methods: int = 80,
) -> ExtractedContext:
    test_method_code = extract_test_method(test_file, test_method_name)
    cut_relevant_code = ""
    if cut_source_file and cut_source_file.exists():
        cut_simple = (cut_fqcn.split(".")[-1] if cut_fqcn else cut_source_file.stem)
        invoked = infer_cut_calls_from_test(test_method_code, cut_simple)
        if extra_method_names:
            invoked |= extra_method_names
        if cut_context_mode == "signature":
            cut_relevant_code = build_cut_signature_context(
                cut_source_file,
                invoked,
                include_fields=cut_signature_include_fields,
                max_methods=cut_signature_max_methods,
            )
        else:
            if invoked:
                cut_relevant_code = extract_relevant_cut_code(
                    cut_source_file, invoked, max_transitive_depth=max_transitive_depth
                )
            else:
                cut_relevant_code = cut_source_file.read_text(encoding="utf-8", errors="ignore")

    if cut_context_max_chars and len(cut_relevant_code) > cut_context_max_chars:
        cut_relevant_code = cut_relevant_code[:cut_context_max_chars].rstrip() + "\n... [truncated]"

    return ExtractedContext(
        test_file=test_file,
        test_class_name=test_class_name,
        test_method_name=test_method_name,
        test_method_code=test_method_code,
        cut_fqcn=cut_fqcn,
        cut_source_file=cut_source_file,
        cut_relevant_code=cut_relevant_code,
    )

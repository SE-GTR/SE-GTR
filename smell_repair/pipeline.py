from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import textwrap
import time
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from smell_repair.analysis.smelly import load_smelly_json, normalize_smelly_json, run_smelly
from smell_repair.llm.client import LlmConfig, OpenAICompatibleClient
from smell_repair.llm.prompts import PromptInputs, PromptLimits, build_messages, load_smell_guides
from smell_repair.project.ant import run_ant
from smell_repair.project.discover import (
    discover_projects,
    find_cut_source_file,
    find_evosuite_test_file,
    resolve_cut_fqcn_from_test,
)
from smell_repair.project import java_extract
from smell_repair.project.java_extract import build_extracted_context
from smell_repair.rules.deterministic import extract_duplicated_setup_to_before, remove_redundant_assert_not_null
from smell_repair.rules.guards import ensure_no_disallowed_markers, ensure_test_method_present
from smell_repair.utils.fs import copytree, ensure_empty_dir
from smell_repair.utils.log import JsonlLogger


SMELLY_NAME_TO_ID = {
    "Not asserted side effects": "NASE",
    "Not asserted return values": "NARV",
    "Assertion with not related parent class method": "ARPM",
    "Asserting object initialization multiple times": "OIMT",
    "Duplicated Setup": "DS",
    "Testing the same exception scenario": "TSES",
    "Multiple calls to the same void method": "TSVM",
    "Not null assertion": "NNA",
    "Exceptions due to null arguments": "ENET",
    "Exceptions due to incomplete setup": "EDIS",
    "Exceptions due to external dependencies": "EDED",
    "Testing only field accesors": "TOFA",
    "Asserting Constants": "AC",
}


@dataclass(frozen=True)
class RuntimeConfig:
    llm: Dict[str, Any]
    smelly: Dict[str, Any]
    ant: Dict[str, Any]
    repair: Dict[str, Any]
    logging: Dict[str, Any]


def load_config(path: Path) -> RuntimeConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RuntimeConfig(
        llm=raw.get("llm", {}),
        smelly=raw.get("smelly", {}),
        ant=raw.get("ant", {}),
        repair=raw.get("repair", {}),
        logging=raw.get("logging", {}),
    )


def _apply_unified_diff(project_root: Path, diff_text: str) -> None:
    """Apply a unified diff using system 'patch'."""
    tmp = project_root / ".tmp_patch.diff"
    tmp.write_text(diff_text, encoding="utf-8")
    try:
        subprocess.run(["patch", "-p1", "-i", str(tmp)], cwd=str(project_root), check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass

_DIFF_LINE_PREFIXES = (
    "diff --git",
    "index ",
    "--- ",
    "+++ ",
    "@@",
    "+",
    "-",
    " ",
    "\\",
)


def _split_fenced_blocks(text: str) -> List[str]:
    parts = text.split("```")
    blocks: List[str] = []
    for i in range(1, len(parts), 2):
        block = parts[i]
        lines = block.splitlines()
        if lines and lines[0].strip().lower().startswith(("diff", "java")):
            lines = lines[1:]
        blocks.append("\n".join(lines).strip())
    return blocks


def _looks_like_diff(text: str) -> bool:
    for line in text.splitlines():
        if line.startswith(("diff --git", "--- ", "@@")):
            return True
    return False


def _trim_to_diff(text: str) -> str:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(("diff --git", "--- ", "*** ", "@@")):
            start = i
            break
    if start is None:
        return text.strip()
    lines = lines[start:]
    out: List[str] = []
    started = False
    for line in lines:
        if line.startswith(_DIFF_LINE_PREFIXES):
            out.append(line)
            started = True
            continue
        if started:
            break
    return "\n".join(out).strip()


def _extract_refactored_method(raw: str, method_name: str) -> Optional[str]:
    text = raw.strip()
    if not text:
        return None
    candidates: List[str] = []
    if "```" in text:
        candidates = [b for b in _split_fenced_blocks(text) if b.strip()]
    if not candidates:
        candidates = [text]
    for cand in candidates:
        blk = java_extract.extract_method_block(cand, method_name, java_extract.TEST_METHOD_START_RE)
        if blk:
            return blk.strip()
    return None


def _find_test_method_span(src: str, method_name: str) -> Optional[Tuple[int, int, str]]:
    for m in java_extract.TEST_METHOD_START_RE.finditer(src):
        if m.group("name") != method_name:
            continue
        open_idx = m.end() - 1
        close_idx = java_extract._scan_to_matching_brace(src, open_idx)
        if close_idx == -1:
            return None
        line_start = src.rfind("\n", 0, m.start()) + 1
        indent_match = re.match(r"[ \t]*", src[line_start:])
        indent = indent_match.group(0) if indent_match else ""
        return m.start(), close_idx + 1, indent
    return None


def _normalize_method_block(block: str, indent: str) -> str:
    cleaned = textwrap.dedent(block).strip("\n")
    if not cleaned:
        return ""
    lines = cleaned.splitlines()
    return "\n".join(indent + line if line.strip() else "" for line in lines) + "\n"


def _replace_test_method(src: str, method_name: str, new_block: str) -> Optional[str]:
    span = _find_test_method_span(src, method_name)
    if not span:
        return None
    start, end, indent = span
    normalized = _normalize_method_block(new_block, indent)
    if not normalized:
        return None
    return src[:start] + normalized + src[end:]


def _make_unified_diff(old_text: str, new_text: str, relpath: str) -> str:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff_iter = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{relpath}",
        tofile=f"b/{relpath}",
        lineterm="",
    )
    diff = "\n".join(diff_iter)
    if diff:
        diff += "\n"
    return diff


def _file_relpath(project_root: Path, file_path: Path) -> str:
    return str(file_path.relative_to(project_root)).replace("\\", "/")


def _project_index_from_folder(folder_name: str) -> int:
    m = re.match(r"^(\d+)_", folder_name)
    return int(m.group(1)) if m else 10**9


def _list_jars(dir_path: Path) -> List[Path]:
    if not dir_path.exists():
        return []
    return sorted([p for p in dir_path.rglob("*.jar") if p.is_file()])


def _find_hamcrest_jar(search_root: Path) -> Optional[Path]:
    # Prefer a shared lib if present to avoid an expensive scan.
    preferred = search_root / "lib"
    if preferred.exists():
        hits = list(preferred.glob("hamcrest*.jar"))
        if hits:
            hits.sort()
            return hits[0]
    hits = list(search_root.rglob("hamcrest*.jar"))
    if not hits:
        return None
    hits.sort()
    return hits[0]


def _resolve_shared_lib_dir(projects_root: Path, ant_cfg: Dict[str, Any]) -> Optional[Path]:
    """Resolve an optional shared lib directory to copy jars from.

    Priority:
      1) ant.shared_lib_dir (explicit)
      2) <projects_root>/lib
      3) <projects_root>/../sf110_projects/lib (common layout for tmp roots)
    """
    cfg_dir = ant_cfg.get("shared_lib_dir") if isinstance(ant_cfg, dict) else None
    candidates: List[Path] = []
    if cfg_dir:
        candidates.append(Path(cfg_dir))
    candidates.append(projects_root / "lib")
    candidates.append(projects_root.parent / "sf110_projects" / "lib")
    for cand in candidates:
        try:
            if cand.exists() and cand.is_dir():
                return cand
        except Exception:
            continue
    return None


def _copy_shared_jars_into_projects(
    shared_dir: Path,
    projects: Dict[str, Any],
    logger: JsonlLogger,
) -> None:
    """Copy shared jars into each project's lib/test-lib if missing.

    This helps projects whose Ant compile target only references local lib/.
    """
    jars = sorted([p for p in shared_dir.glob("*.jar") if p.is_file()])
    if not jars:
        logger.log("shared_lib_dir_empty", path=str(shared_dir))
        return
    for proj in projects.values():
        copied = 0
        for dest_dir in (proj.root / "lib", proj.root / "test-lib"):
            dest_dir.mkdir(parents=True, exist_ok=True)
            for jar in jars:
                dest = dest_dir / jar.name
                if dest.exists():
                    continue
                shutil.copyfile(jar, dest)
                copied += 1
        logger.log(
            "project_shared_lib_copied",
            project=proj.real_name,
            shared_dir=str(shared_dir),
            jars=len(jars),
            copied=copied,
        )


def _evosuite_jar_aliases_from_build(build_xml: Path) -> List[str]:
    """Extract EvoSuite jar filenames referenced by a build.xml."""
    if not build_xml.exists():
        return []
    try:
        text = build_xml.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    # Match jar names like evosuite.jar or evosuite-standalone-runtime-1.2.0.jar
    names = set(re.findall(r"(evosuite[^\"'<>\\s]*\\.jar)", text, flags=re.IGNORECASE))
    # Normalize to lowercase on disk to avoid duplicates that differ only by case.
    return sorted({n.lower() for n in names})


def _build_sf110_classpath(project_root: Path) -> str:
    entries: List[Path] = []
    build_classes = project_root / "build" / "classes"
    build_evosuite = project_root / "build" / "evosuite"
    if build_classes.exists():
        entries.append(build_classes)
    if build_evosuite.exists():
        entries.append(build_evosuite)
    entries += _list_jars(project_root / "lib")
    entries += _list_jars(project_root / "test-lib")
    shared = project_root.parent / "lib"
    entries += _list_jars(shared)
    # Deduplicate while preserving order
    seen = set()
    out: List[str] = []
    for p in entries:
        s = str(p.resolve())
        if s not in seen:
            out.append(s)
            seen.add(s)
    return os.pathsep.join(out)


def _read_java_package(java_file: Path) -> Optional[str]:
    try:
        with java_file.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                line_s = line.strip()
                if line_s.startswith("package "):
                    return line_s[len("package "):].rstrip(";").strip()
                if line_s.startswith(("public class", "class ")):
                    break
    except Exception:
        return None
    return None


def _test_class_fqcn(test_file: Path) -> str:
    pkg = _read_java_package(test_file)
    cls = test_file.stem
    return f"{pkg}.{cls}" if pkg else cls


def _run_junit_class(project_root: Path, test_file: Path, *, java_cmd: str = "java", timeout_sec: int = 600) -> str:
    fqcn = _test_class_fqcn(test_file)
    cp = _build_sf110_classpath(project_root)
    cmd = [java_cmd, "-cp", cp, "org.junit.runner.JUnitCore", fqcn]
    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"JUnitCore failed for {fqcn}\n{proc.stdout}")
    return proc.stdout


def _collect_method_smells_and_evidence(
    smells_for_key: Dict[str, List[Any]],
) -> tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Collect per-test-method smell ids and per-smell evidence.

    Input: {smellName -> [SmellInstance,...]}
    Output:
      - method_to_smells: {testMethod -> [smellId,...]}
      - method_to_evidence: {testMethod -> {smellId -> evidenceDict}}
    """
    method_to_smells: Dict[str, List[str]] = {}
    method_to_evidence: Dict[str, Dict[str, Any]] = {}

    for smell_name, instances in smells_for_key.items():
        smell_id = SMELLY_NAME_TO_ID.get(smell_name)
        if not smell_id:
            continue

        for inst in instances:
            tm = getattr(inst, "test_method", str(inst))
            method_to_smells.setdefault(tm, [])
            if smell_id not in method_to_smells[tm]:
                method_to_smells[tm].append(smell_id)

            ev = getattr(inst, "evidence", None)
            if ev:
                method_to_evidence.setdefault(tm, {})
                # If multiple entries exist, keep the first non-empty one.
                method_to_evidence[tm].setdefault(smell_id, ev)

    return method_to_smells, method_to_evidence


def run_pipeline(
    *,
    config_path: Path,
    projects_root: Path,
    smelly_json_path: Path,
    out_root: Path,
    smells_dir: Path,
) -> Path:
    cfg = load_config(config_path)
    run_id = time.strftime("run_%Y%m%d_%H%M%S")
    run_dir = out_root / run_id
    ensure_empty_dir(run_dir)

    logger = JsonlLogger(run_dir / "logs" / "pipeline.jsonl", verbose=bool(cfg.logging.get("verbose", True)))
    logger.log("run_start", config=str(config_path), projects_root=str(projects_root), smelly_json=str(smelly_json_path))

    workdir = run_dir / "workdir"
    ensure_empty_dir(workdir)
    copytree(projects_root, workdir)
    logger.log("workdir_ready", workdir=str(workdir))

    projects = discover_projects(workdir)
    raw_smelly = load_smelly_json(smelly_json_path)
    smelly_norm = normalize_smelly_json(raw_smelly)

    # LLM client
    llm_cfg = LlmConfig(**cfg.llm)
    client = OpenAICompatibleClient(llm_cfg)

    # policy
    allow_reflection = bool(cfg.repair.get("allow_reflection_asserts", False))
    max_attempts = max(1, int(cfg.repair.get("max_llm_attempts", 3)))
    enable_det = bool(cfg.repair.get("enable_deterministic_rules", True))
    limit_tests = int(cfg.repair.get("limit_tests", 0))
    cut_context_mode = str(cfg.repair.get("cut_context_mode", "signature")).strip().lower()
    cut_context_max_chars = int(cfg.repair.get("cut_context_max_chars", 12000))
    cut_signature_include_fields = bool(cfg.repair.get("cut_signature_include_fields", True))
    cut_signature_max_methods = int(cfg.repair.get("cut_signature_max_methods", 80))

    prompt_limits = PromptLimits(
        max_smell_guides_chars=int(cfg.repair.get("max_smell_guides_chars", 12000)),
        max_evidence_chars=int(cfg.repair.get("max_evidence_chars", 8000)),
        max_test_method_chars=int(cfg.repair.get("max_test_method_chars", 8000)),
        max_cut_context_chars=int(cfg.repair.get("max_cut_context_chars", 12000)),
        max_compile_error_chars=int(cfg.repair.get("max_compile_error_chars", 4000)),
        evidence_max_list_items=int(cfg.repair.get("evidence_max_list_items", 6)),
        evidence_max_group_tests=int(cfg.repair.get("evidence_max_group_tests", 10)),
        evidence_max_prefix_stmts=int(cfg.repair.get("evidence_max_prefix_stmts", 2)),
        evidence_max_str_len=int(cfg.repair.get("evidence_max_str_len", 240)),
    )

    ant_cmd = str(cfg.ant.get("ant_cmd", "ant"))
    java_cmd = str(cfg.ant.get("java_cmd", "java"))
    targets_compile = list(cfg.ant.get("targets_compile", ["clean", "compile", "compile-evosuite"]))
    targets_test = list(cfg.ant.get("targets_test", []))
    validity_gate = bool(cfg.repair.get("enable_validity_gate", True))
    validity_gate_timeout = int(cfg.repair.get("validity_gate_timeout_sec", 600))

    # Smelly config for re-run
    smelly_jar = Path(cfg.smelly["jar"])
    evosuite_jar = Path(cfg.smelly["evosuite_runtime_jar"])
    junit_jar = Path(cfg.smelly["junit_jar"])

    # Ensure SF110-style shared lib directory exists for Ant builds (../lib from each project)
    shared_lib = workdir / "lib"
    shared_lib.mkdir(parents=True, exist_ok=True)
    try:
        # Some build.xml files reference different EvoSuite runtime jar names.
        evosuite_aliases = {"evosuite.jar", "evosuite-standalone-runtime-1.2.0.jar"}
        for proj in projects.values():
            evosuite_aliases.update(_evosuite_jar_aliases_from_build(proj.root / "build.xml"))
        for alias in sorted(evosuite_aliases):
            shutil.copyfile(evosuite_jar, shared_lib / alias)
        shutil.copyfile(junit_jar, shared_lib / "junit-4.11.jar")
        logger.log("shared_lib_ready", path=str(shared_lib), evosuite_aliases=sorted(evosuite_aliases))
    except Exception as e:
        logger.log("shared_lib_prepare_failed", error=str(e), path=str(shared_lib))

    # Add hamcrest to shared lib so JUnitCore can run tests (needed for validity gate)
    hamcrest_from_cfg = cfg.ant.get("hamcrest_jar") if isinstance(cfg.ant, dict) else None
    hamcrest_src = Path(hamcrest_from_cfg) if hamcrest_from_cfg else _find_hamcrest_jar(projects_root)
    if hamcrest_src and hamcrest_src.exists():
        try:
            shutil.copyfile(hamcrest_src, shared_lib / hamcrest_src.name)
            logger.log("shared_lib_hamcrest_ready", path=str(shared_lib / hamcrest_src.name))
        except Exception as e:
            logger.log("shared_lib_hamcrest_failed", error=str(e), src=str(hamcrest_src))
    else:
        logger.log("shared_lib_hamcrest_missing", search_root=str(projects_root))

    # Optionally hydrate each project lib/ from a shared SF110-style lib directory.
    shared_source_dir = _resolve_shared_lib_dir(projects_root, cfg.ant)
    if shared_source_dir:
        try:
            _copy_shared_jars_into_projects(shared_source_dir, projects, logger)
            logger.log("shared_lib_dir_used", path=str(shared_source_dir))
        except Exception as e:
            logger.log("shared_lib_dir_copy_failed", path=str(shared_source_dir), error=str(e))
    else:
        logger.log("shared_lib_dir_missing", search_root=str(projects_root))

    detectors = int(cfg.smelly.get("detectors", 0))
    mode = int(cfg.smelly.get("mode", 0))
    sufix = str(cfg.smelly.get("sufix", " "))

    processed = 0
    def _smelly_sort_key(k: str) -> Tuple[int, str]:
        if "." not in k:
            return (10**9, k)
        real, _ = k.split(".", 1)
        proj = projects.get(real)
        if not proj:
            return (10**9, k)
        return (_project_index_from_folder(proj.folder_name), k)

    for key in sorted(smelly_norm.keys(), key=_smelly_sort_key):
        smell_map = smelly_norm[key]
        # key format: "<realName>.<OriginalName>"
        if "." not in key:
            continue
        real_name, cut_simple = key.split(".", 1)
        project = projects.get(real_name)
        if not project:
            logger.log("skip_missing_project", key=key, real_name=real_name)
            continue

        test_file = find_evosuite_test_file(project, cut_simple)
        if not test_file:
            logger.log("skip_missing_test_file", key=key, project=str(project.root), cut_simple=cut_simple)
            continue

        # build per-method smell list (+ evidence if present in the Smelly JSON)
        method_to_smells, method_to_evidence = _collect_method_smells_and_evidence(smell_map)

        # deterministic edits at file-level
        file_text = test_file.read_text(encoding="utf-8", errors="ignore")
        file_changed = False

        if enable_det:
            # NNA
            if any("NNA" in sids for sids in method_to_smells.values()):
                new_text, removed = remove_redundant_assert_not_null(file_text)
                if removed > 0:
                    file_text = new_text
                    file_changed = True
                    logger.log("deterministic_nna", key=key, file=str(test_file), removed=removed)

            # DS: use methods flagged with DS
            ds_methods = [m for m, sids in method_to_smells.items() if "DS" in sids]
            if len(ds_methods) >= 2:
                new_text, changed = extract_duplicated_setup_to_before(file_text, ds_methods)
                if changed:
                    file_text = new_text
                    file_changed = True
                    logger.log("deterministic_ds", key=key, file=str(test_file), methods=ds_methods)

        if file_changed:
            test_file.write_text(file_text, encoding="utf-8")
            # compile to validate before LLM (best-effort)
            try:
                run_ant(project.root, targets_compile, ant_cmd=ant_cmd)
            except Exception as e:
                logger.log("compile_failed_after_deterministic", key=key, file=str(test_file), error=str(e))

        # per-method LLM fixes
        for test_method, smell_ids in method_to_smells.items():
            processed += 1
            if limit_tests and processed > limit_tests:
                logger.log("limit_reached", limit_tests=limit_tests)
                break

            # skip if only DS/NNA handled deterministically and no other smells remain
            remaining = [s for s in smell_ids if s not in {"NNA", "DS"}]
            if not remaining:
                continue

            # Resolve CUT file
            cut_fqcn = resolve_cut_fqcn_from_test(test_file, cut_simple)
            cut_src = find_cut_source_file(project, cut_fqcn) if cut_fqcn else None
            evidence_subset: Dict[str, Any] = {
                sid: ev
                for sid, ev in (method_to_evidence.get(test_method, {}) or {}).items()
                if sid in remaining
            }
            extra_methods = java_extract.infer_cut_calls_from_evidence(evidence_subset)

            try:
                ctx = build_extracted_context(
                    test_file=test_file,
                    test_class_name=test_file.stem,
                    test_method_name=test_method,
                    cut_fqcn=cut_fqcn,
                    cut_source_file=cut_src,
                    max_transitive_depth=1,
                    extra_method_names=extra_methods,
                    cut_context_mode=cut_context_mode,
                    cut_context_max_chars=cut_context_max_chars,
                    cut_signature_include_fields=cut_signature_include_fields,
                    cut_signature_max_methods=cut_signature_max_methods,
                )
            except Exception as e:
                logger.log("skip_missing_test_method", key=key, method=test_method, error=str(e))
                continue

            relpath = _file_relpath(project.root, test_file)
            smell_guides = load_smell_guides(smells_dir, remaining)

            original_text = test_file.read_text(encoding="utf-8", errors="ignore")
            compile_error: Optional[str] = None
            success = False
            last_completion: str = ""

            for attempt in range(1, max_attempts + 1):
                inp = PromptInputs(
                    smells=remaining,
                    smell_guides=smell_guides,
                    smell_evidence=evidence_subset,
                    allow_reflection_asserts=allow_reflection,
                    file_relpath=relpath,
                    ctx=ctx,
                    limits=prompt_limits,
                    compile_error=compile_error,
                )
                messages = build_messages(inp)
                logger.log("llm_request", key=key, method=test_method, attempt=attempt, smells=remaining)
                raw_completion = client.chat(messages)
                logger.log("llm_response", key=key, method=test_method, attempt=attempt, completion_preview=raw_completion[:2000])
                method_block = _extract_refactored_method(raw_completion, test_method)
                if method_block:
                    logger.log(
                        "llm_response_extracted",
                        key=key,
                        method=test_method,
                        attempt=attempt,
                        completion_preview=method_block[:2000],
                    )
                if not method_block:
                    compile_error = f"LLM output did not contain a full method declaration for {test_method}."
                    continue

                new_text = _replace_test_method(original_text, test_method, method_block)
                if not new_text:
                    compile_error = f"Failed to replace method {test_method} in source."
                    continue
                diff_text = _make_unified_diff(original_text, new_text, relpath)
                if not diff_text.strip():
                    compile_error = "LLM output produced no changes."
                    continue
                last_completion = diff_text
                test_file.write_text(new_text, encoding="utf-8")

                # guards
                new_text = test_file.read_text(encoding="utf-8", errors="ignore")
                try:
                    ensure_no_disallowed_markers(new_text)
                    ensure_test_method_present(new_text, test_method)
                except Exception as e:
                    compile_error = f"Guard failed: {e}"
                    test_file.write_text(original_text, encoding="utf-8")
                    continue

                # compile/test best-effort
                try:
                    run_ant(project.root, targets_compile, ant_cmd=ant_cmd)
                    if targets_test:
                        run_ant(project.root, targets_test, ant_cmd=ant_cmd)
                except Exception as e:
                    compile_error = str(e)
                    test_file.write_text(original_text, encoding="utf-8")
                    continue

                if validity_gate:
                    try:
                        _run_junit_class(
                            project.root,
                            test_file,
                            java_cmd=java_cmd,
                            timeout_sec=validity_gate_timeout,
                        )
                        logger.log("validity_gate_ok", key=key, method=test_method)
                    except Exception as e:
                        compile_error = f"Validity gate failed: {e}"
                        logger.log("validity_gate_failed", key=key, method=test_method, error=str(e))
                        test_file.write_text(original_text, encoding="utf-8")
                        continue

                success = True
                break

            # save patch and log
            patch_dir = run_dir / "patches" / real_name / cut_simple
            patch_dir.mkdir(parents=True, exist_ok=True)
            (patch_dir / f"{test_method}.diff").write_text(last_completion, encoding="utf-8")  # type: ignore[has-type]
            logger.log("method_done", key=key, method=test_method, success=success, smells=remaining)

        # per-project smelly rerun (optional but recommended): comment out if too slow
        try:
            # Create single-project temp root for Smelly
            tmp_root = run_dir / "tmp_smelly" / project.folder_name
            ensure_empty_dir(tmp_root.parent)
            shutil.copytree(project.root, tmp_root)
            out_json = run_smelly(
                smelly_jar=smelly_jar,
                evosuite_runtime_jar=evosuite_jar,
                junit_jar=junit_jar,
                source_path=tmp_root.parent,
                test_path=tmp_root.parent,
                output_dir=run_dir / "reports",
                output_name=f"smelly_after_{project.real_name}",
                detectors=detectors,
                mode=mode,
                sufix=sufix,
            )
            logger.log("smelly_rerun_ok", project=project.real_name, out=str(out_json))
        except Exception as e:
            logger.log("smelly_rerun_failed", project=project.real_name, error=str(e))

    logger.log("run_end")
    return run_dir

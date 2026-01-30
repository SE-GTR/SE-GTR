from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple


def list_jars(dir_path: Path) -> List[Path]:
    if not dir_path.exists():
        return []
    return sorted([p for p in dir_path.rglob("*.jar") if p.is_file()])


def guess_shared_lib_jars(project_root: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """
    SF110-style Ant projects often use ../lib/junit-*.jar and ../lib/evosuite*.jar.
    Prefer EvoSuite *standalone-runtime* when available.
    Return (junit_jar, evosuite_jar) if found.
    """
    shared = project_root.parent / "lib"
    junit = None
    for name in ("junit-4.11.jar", "junit-4.13.2.jar"):
        cand = shared / name
        if cand.exists():
            junit = cand
            break

    evo = None
    for name in (
        "evosuite-standalone-runtime-1.2.0.jar",
        "evosuite-standalone-runtime.jar",
        "evosuite.jar",
    ):
        cand = shared / name
        if cand.exists():
            evo = cand
            break

    return (junit, evo)


def build_sf110_classpath(project_root: Path, *, include_evosuite_tests: bool = True) -> List[Path]:
    """
    Build a best-effort runtime classpath for running EvoSuite tests on SF110 projects.

    Expected layout:
      - build/classes         (compiled production)
      - build/evosuite        (compiled evosuite tests)
      - lib/*.jar             (project deps)
      - test-lib/*.jar        (test deps)
      - ../lib/junit-4.11.jar (shared junit)
      - ../lib/evosuite-standalone-runtime-1.2.0.jar (preferred runtime)
      - ../lib/evosuite.jar   (fallback runtime)
    """
    cp: List[Path] = []
    build_classes = project_root / "build" / "classes"
    build_evosuite = project_root / "build" / "evosuite"
    if build_classes.exists():
        cp.append(build_classes)
    if include_evosuite_tests and build_evosuite.exists():
        cp.append(build_evosuite)

    cp += list_jars(project_root / "lib")
    cp += list_jars(project_root / "test-lib")

    # Also include shared SF110 lib jars (e.g., hamcrest) if present.
    shared_lib = project_root.parent / "lib"
    cp += list_jars(shared_lib)

    junit_jar, evo_jar = guess_shared_lib_jars(project_root)
    if junit_jar:
        cp.append(junit_jar)
    if evo_jar:
        cp.append(evo_jar)

    # Deduplicate while preserving order
    seen = set()
    out: List[Path] = []
    for p in cp:
        s = str(p.resolve())
        if s not in seen:
            out.append(p)
            seen.add(s)
    return out


def read_java_package(java_file: Path) -> Optional[str]:
    """Parse a Java file and return its 'package ...;' declaration, or None."""
    try:
        with java_file.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(200):  # header only
                line = f.readline()
                if not line:
                    break
                line_s = line.strip()
                if line_s.startswith("package "):
                    return line_s[len("package "):].rstrip(";").strip()
                if line_s.startswith("public class") or line_s.startswith("class "):
                    break
    except Exception:
        return None
    return None


def discover_evosuite_test_classes(project_root: Path) -> List[str]:
    """
    Discover EvoSuite *_ESTest.java classes under evosuite-tests/ and return FQCN list.
    """
    tests_root = project_root / "evosuite-tests"
    if not tests_root.exists():
        return []
    out: List[str] = []
    for p in sorted(tests_root.rglob("*_ESTest.java")):
        pkg = read_java_package(p)
        cls = p.stem
        if pkg:
            out.append(f"{pkg}.{cls}")
        else:
            out.append(cls)
    return out


def classpath_to_str(entries: List[Path]) -> str:
    return os.pathsep.join(str(p) for p in entries)

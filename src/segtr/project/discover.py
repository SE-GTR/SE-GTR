from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Project:
    folder_name: str
    real_name: str
    root: Path

    @property
    def src_main(self) -> Path:
        return self.root / "src" / "main" / "java"

    @property
    def evosuite_tests(self) -> Path:
        return self.root / "evosuite-tests"


REAL_NAME_RE = re.compile(r"^\d+_(.+)$")


def discover_projects(projects_root: Path) -> Dict[str, Project]:
    out: Dict[str, Project] = {}
    for p in projects_root.iterdir():
        if not p.is_dir():
            continue
        m = REAL_NAME_RE.match(p.name)
        if not m:
            continue
        real = m.group(1)
        out[real] = Project(folder_name=p.name, real_name=real, root=p)
    return out


def find_evosuite_test_file(project: Project, cut_simple_name: str) -> Optional[Path]:
    pattern = f"{cut_simple_name}_ESTest.java"
    matches = list(project.evosuite_tests.rglob(pattern))
    if not matches:
        return None
    matches.sort()
    return matches[0]


def read_java_package_and_imports(java_file: Path) -> Tuple[Optional[str], List[str]]:
    pkg: Optional[str] = None
    imports: List[str] = []
    for line in java_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("package "):
            pkg = line[len("package ") :].rstrip(";").strip()
        elif line.startswith("import "):
            imports.append(line[len("import ") :].rstrip(";").strip())
        if line.startswith("public class"):
            break
    return pkg, imports


def resolve_cut_fqcn_from_test(test_file: Path, cut_simple: str) -> Optional[str]:
    pkg, imports = read_java_package_and_imports(test_file)
    for imp in imports:
        if imp.endswith("." + cut_simple):
            return imp
    if pkg:
        return pkg + "." + cut_simple
    return None


def find_cut_source_file(project: Project, cut_fqcn: str) -> Optional[Path]:
    rel = Path(*cut_fqcn.split("."))
    p = project.src_main / (str(rel) + ".java")
    if p.exists():
        return p
    simple = cut_fqcn.split(".")[-1]
    candidates = list(project.src_main.rglob(simple + ".java"))
    if not candidates:
        return None
    want_pkg = ".".join(cut_fqcn.split(".")[:-1])
    for c in candidates:
        pkg, _ = read_java_package_and_imports(c)
        if pkg == want_pkg:
            return c
    candidates.sort()
    return candidates[0]

"""Thin wrapper around the JaCoCo CLI / agent for project-level coverage.

Usage:

    from smell_repair_v2.coverage.jacoco import run_jacoco
    result = run_jacoco(project_root, test_fqcns=[...])
    print(result.line_coverage, result.branch_coverage)

The agent is attached via ``-javaagent`` when spawning JUnitCore; the
resulting ``jacoco.exec`` is then fed through ``jacococli.jar report`` to
produce an XML report, which we parse for aggregate counters.

Per the Phase 2.4a spec we invoke this once per project (not per plan),
so a 30–60 s cost is acceptable.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from smell_repair_v2.operators.validator import (
    _build_sf110_classpath,
    _test_class_fqcn,
)
from smell_repair_v2.project.discover import (
    Project,
    find_evosuite_test_file,
)


# Defaults — override via kwargs if a different layout is in play.
REPO_ROOT = Path(__file__).resolve().parents[2]
_JACOCO_DIR = REPO_ROOT / "tools" / "jacoco"
DEFAULT_AGENT_JAR = _JACOCO_DIR / "jacocoagent.jar"
DEFAULT_CLI_JAR = _JACOCO_DIR / "jacococli.jar"


@dataclass
class CoverageResult:
    project: str
    line_covered: int = 0
    line_missed: int = 0
    branch_covered: int = 0
    branch_missed: int = 0
    instruction_covered: int = 0
    instruction_missed: int = 0
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    elapsed_sec: float = 0.0
    exec_path: Optional[Path] = None
    xml_path: Optional[Path] = None
    test_classes: List[str] = field(default_factory=list)

    @property
    def line_coverage(self) -> float:
        t = self.line_covered + self.line_missed
        return (self.line_covered / t) if t else 0.0

    @property
    def branch_coverage(self) -> float:
        t = self.branch_covered + self.branch_missed
        return (self.branch_covered / t) if t else 0.0

    @property
    def instruction_coverage(self) -> float:
        t = self.instruction_covered + self.instruction_missed
        return (self.instruction_covered / t) if t else 0.0

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "line_coverage": round(self.line_coverage, 4),
            "branch_coverage": round(self.branch_coverage, 4),
            "instruction_coverage": round(self.instruction_coverage, 4),
            "line_covered": self.line_covered,
            "line_missed": self.line_missed,
            "branch_covered": self.branch_covered,
            "branch_missed": self.branch_missed,
            "instruction_covered": self.instruction_covered,
            "instruction_missed": self.instruction_missed,
            "tests_total": self.tests_total,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "test_classes": list(self.test_classes),
            "elapsed_sec": round(self.elapsed_sec, 2),
        }


class JacocoError(RuntimeError):
    pass


def _enumerate_test_classes(project_root: Path) -> List[str]:
    out: List[str] = []
    for p in (project_root / "evosuite-tests").rglob("*_ESTest.java"):
        out.append(_test_class_fqcn(p))
    return sorted(set(out))


def _run_junit_with_agent(
    project_root: Path,
    test_fqcns: List[str],
    exec_out: Path,
    class_dump_dir: Path,
    agent_jar: Path,
    java_cmd: str = "java",
    timeout_sec: int = 1800,
    batch_size: int = 50,
) -> List[subprocess.CompletedProcess]:
    """Run JUnitCore with the JaCoCo agent attached.

    EvoSuite tests use ``@RunWith(EvoRunner.class)`` with
    ``separateClassLoader = true`` — the CUT gets loaded by ``EvoClassLoader``,
    whose bytecode is rewritten at load time. The JaCoCo agent has to dump
    those instrumented classes into ``class_dump_dir`` so the later report
    step can match coverage data against the exact bytecode the agent saw.
    Without ``classdumpdir``, counters stay at 0 even though tests executed.

    Runs tests in ``batch_size``-sized JUnitCore invocations (append-mode
    after the first) to keep per-call memory within a JVM.
    """
    cp = _build_sf110_classpath(project_root)
    procs: List[subprocess.CompletedProcess] = []
    for i in range(0, len(test_fqcns), batch_size):
        batch = test_fqcns[i : i + batch_size]
        append = "true" if i > 0 else "false"
        agent_arg = (
            f"-javaagent:{agent_jar}=destfile={exec_out},append={append},"
            f"classdumpdir={class_dump_dir}"
        )
        cmd = [java_cmd, agent_arg, "-cp", cp,
               "org.junit.runner.JUnitCore", *batch]
        p = subprocess.run(
            cmd, cwd=str(project_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout_sec, check=False,
        )
        procs.append(p)
    return procs


def _run_cli_report(
    exec_path: Path,
    classfiles: Iterable[Path],
    sourcefiles: Path,
    xml_out: Path,
    cli_jar: Path,
    java_cmd: str = "java",
    timeout_sec: int = 600,
) -> subprocess.CompletedProcess:
    xml_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        java_cmd, "-jar", str(cli_jar), "report", str(exec_path),
        "--xml", str(xml_out),
        "--sourcefiles", str(sourcefiles),
    ]
    for cf in classfiles:
        cmd.extend(["--classfiles", str(cf)])
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout_sec, check=False,
    )


def _parse_counters(xml_path: Path, result: CoverageResult) -> None:
    """Extract aggregate counters from a JaCoCo XML report."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    # Top-level <counter> children are aggregates over all packages.
    for c in root.findall("counter"):
        kind = c.get("type")
        covered = int(c.get("covered", 0))
        missed = int(c.get("missed", 0))
        if kind == "LINE":
            result.line_covered = covered
            result.line_missed = missed
        elif kind == "BRANCH":
            result.branch_covered = covered
            result.branch_missed = missed
        elif kind == "INSTRUCTION":
            result.instruction_covered = covered
            result.instruction_missed = missed


def _parse_junit_tallies(stdout: str, total_classes: int) -> tuple[int, int, int]:
    """Parse JUnitCore stdout for pass/fail tally.

    JUnitCore prints either ``OK (N tests)`` or
    ``Tests run: N, Failures: M`` at the end.
    Returns (tests_total, tests_passed, tests_failed).
    """
    total = passed = failed = 0
    for line in stdout.splitlines():
        s = line.strip()
        if s.startswith("OK ("):
            # OK (123 tests)
            try:
                n = int(s.split("(", 1)[1].split(" ", 1)[0])
                total += n
                passed += n
            except Exception:
                pass
        elif s.startswith("Tests run:"):
            # Tests run: 123,  Failures: 4
            try:
                parts = [p.strip() for p in s.split(",")]
                n = int(parts[0].split(":", 1)[1])
                f = int(parts[1].split(":", 1)[1])
                total += n
                failed += f
                passed += max(0, n - f)
            except Exception:
                pass
    return total, passed, failed


def run_jacoco(
    project_root: Path,
    test_fqcns: Optional[List[str]] = None,
    *,
    agent_jar: Path = DEFAULT_AGENT_JAR,
    cli_jar: Path = DEFAULT_CLI_JAR,
    work_subdir: str = "jacoco",
    java_cmd: str = "java",
    timeout_sec: int = 1800,
    project_name: Optional[str] = None,
) -> CoverageResult:
    """Run JaCoCo on ``project_root`` using its EvoSuite tests.

    ``test_fqcns`` defaults to every *_ESTest under ``evosuite-tests/``.
    Classes are drawn from ``build/classes`` (CUT) and sources from
    ``src/main/java``. ``jacoco.exec`` and the XML report are written under
    ``project_root / work_subdir / ...``.
    """
    if not agent_jar.exists():
        raise JacocoError(f"jacocoagent not found at {agent_jar}")
    if not cli_jar.exists():
        raise JacocoError(f"jacococli not found at {cli_jar}")

    result = CoverageResult(project=project_name or project_root.name)
    t0 = time.monotonic()

    if test_fqcns is None:
        test_fqcns = _enumerate_test_classes(project_root)
    result.test_classes = list(test_fqcns)

    work_dir = project_root / work_subdir
    work_dir.mkdir(parents=True, exist_ok=True)
    exec_path = work_dir / "jacoco.exec"
    class_dump_dir = work_dir / "classdump"
    xml_path = work_dir / "jacoco-report.xml"

    if exec_path.exists():
        exec_path.unlink()
    if class_dump_dir.exists():
        # clean old dumps so stale bytecode doesn't confuse the report
        import shutil as _shutil
        _shutil.rmtree(class_dump_dir)
    class_dump_dir.mkdir(parents=True, exist_ok=True)

    procs = _run_junit_with_agent(
        project_root, test_fqcns, exec_path, class_dump_dir, agent_jar,
        java_cmd=java_cmd, timeout_sec=timeout_sec,
    )
    # Aggregate tallies across all batches.
    total_tot = total_passed = total_failed = 0
    combined_stdout = []
    for p in procs:
        combined_stdout.append(p.stdout or "")
        t, ps, f = _parse_junit_tallies(p.stdout or "", 0)
        total_tot += t
        total_passed += ps
        total_failed += f
    result.tests_total = total_tot
    result.tests_passed = total_passed
    result.tests_failed = total_failed

    if not exec_path.exists():
        raise JacocoError(
            "jacoco.exec not produced; stdout_tail="
            + "\n---\n".join(s[-200:] for s in combined_stdout)[-800:]
        )

    # Prefer classdump (matches the exact instrumented bytecode the agent saw)
    # over build/classes.
    classfiles: List[Path] = []
    if class_dump_dir.exists() and any(class_dump_dir.iterdir()):
        classfiles.append(class_dump_dir)
    if (project_root / "build" / "classes").exists():
        classfiles.append(project_root / "build" / "classes")
    sourcefiles = project_root / "src" / "main" / "java"
    rp = _run_cli_report(
        exec_path, classfiles, sourcefiles, xml_path, cli_jar,
        java_cmd=java_cmd, timeout_sec=300,
    )
    if rp.returncode != 0 or not xml_path.exists():
        raise JacocoError(
            f"jacoco report failed; rc={rp.returncode}; "
            f"stdout_tail={(rp.stdout or '')[-400:]}"
        )

    _parse_counters(xml_path, result)
    result.elapsed_sec = time.monotonic() - t0
    result.exec_path = exec_path
    result.xml_path = xml_path
    return result

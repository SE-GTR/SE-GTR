from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# Reuse existing helpers to stay consistent with the pipeline.
from smell_repair.pipeline import _build_sf110_classpath, _test_class_fqcn
from smell_repair.project.ant import run_ant


PROJECT_RE = re.compile(r"^(\d+)_")


NATIVE_PAT = re.compile(r"UnsatisfiedLinkError|java\.library\.path|loadLibrary|JNI", re.IGNORECASE)
CLASSPATH_PAT = re.compile(r"NoClassDefFoundError|ClassNotFoundException", re.IGNORECASE)
ASSERT_PAT = re.compile(r"FAILURES!!!|AssertionError", re.IGNORECASE)


@dataclass
class ProjectResult:
    project: str
    index: int
    classification: str
    compile_ok: bool
    tests_seen: int
    tests_passed: int
    tests_failed: int
    note: str = ""


def _project_index(name: str) -> int:
    m = PROJECT_RE.match(name)
    return int(m.group(1)) if m else 10**9


def _iter_projects(root: Path) -> List[Path]:
    projs = [p for p in root.iterdir() if p.is_dir() and PROJECT_RE.match(p.name)]
    projs.sort(key=lambda p: (_project_index(p.name), p.name))
    return projs


def _test_files(project_root: Path) -> List[Path]:
    tests_root = project_root / "evosuite-tests"
    if not tests_root.exists():
        return []
    files = sorted(tests_root.rglob("*_ESTest.java"))
    return files


def _classify_failure(output: str) -> str:
    if NATIVE_PAT.search(output):
        return "native_issue"
    if CLASSPATH_PAT.search(output):
        return "classpath_issue"
    if ASSERT_PAT.search(output):
        return "assertion_fail"
    return "runtime_fail"


def _run_junit(project_root: Path, test_file: Path, *, java_cmd: str, timeout_sec: int) -> Tuple[bool, str]:
    fqcn = _test_class_fqcn(test_file)
    cp = _build_sf110_classpath(project_root)
    if not cp:
        return False, "empty_classpath"
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
    return proc.returncode == 0, proc.stdout


def classify_project(
    project_root: Path,
    *,
    ant_cmd: str,
    java_cmd: str,
    ant_targets: Sequence[str],
    junit_timeout_sec: int,
    max_test_classes: int,
) -> ProjectResult:
    name = project_root.name
    idx = _project_index(name)
    tests = _test_files(project_root)
    if not tests:
        return ProjectResult(name, idx, "no_tests", False, 0, 0, 0, note="no evosuite-tests")

    try:
        run_ant(project_root, list(ant_targets), ant_cmd=ant_cmd)
        compile_ok = True
    except Exception as e:  # noqa: BLE001
        return ProjectResult(name, idx, "compile_failed", False, 0, 0, 0, note=str(e)[:400])

    seen = passed = failed = 0
    classifications: List[str] = []
    failure_notes: List[str] = []

    for test_file in tests:
        if max_test_classes and seen >= max_test_classes:
            break
        seen += 1
        try:
            ok, out = _run_junit(project_root, test_file, java_cmd=java_cmd, timeout_sec=junit_timeout_sec)
        except subprocess.TimeoutExpired:
            classifications.append("timeout")
            failed += 1
            continue

        if ok:
            passed += 1
            classifications.append("ok")
        else:
            failed += 1
            fail_kind = _classify_failure(out)
            classifications.append(fail_kind)
            failure_notes.append(f"{_test_class_fqcn(test_file)}:{fail_kind}")
            # Native issues are consistently blocking; stop early to save time.
            if fail_kind == "native_issue":
                break

    # Aggregate classification with clear precedence.
    if "native_issue" in classifications:
        cls = "native_issue"
    elif passed > 0 and failed == 0:
        cls = "gate_ok"
    elif passed > 0 and failed > 0:
        cls = "mixed"
    elif classifications and all(c == "classpath_issue" for c in classifications):
        cls = "classpath_issue"
    elif "timeout" in classifications and failed == len(classifications):
        cls = "timeout"
    elif "assertion_fail" in classifications:
        cls = "assertion_fail"
    elif classifications:
        cls = classifications[-1]
    else:
        cls = "unknown"

    note = "; ".join(failure_notes[:5])
    return ProjectResult(name, idx, cls, compile_ok, seen, passed, failed, note=note[:400])


def _copy_project(src: Path, dst_root: Path) -> Path:
    dst = dst_root / src.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description="Baseline validity-gate classification for SF110 projects.")
    ap.add_argument("--projects-root", type=Path, required=True)
    ap.add_argument("--out-csv", type=Path, required=True)
    ap.add_argument("--ant-cmd", default="ant")
    ap.add_argument("--java-cmd", default="java")
    ap.add_argument(
        "--ant-targets",
        nargs="*",
        default=["clean", "compile", "compile-evosuite"],
        help="Ant targets to run before JUnitCore.",
    )
    ap.add_argument("--junit-timeout-sec", type=int, default=180)
    ap.add_argument(
        "--max-test-classes",
        type=int,
        default=1,
        help="Limit JUnitCore runs per project (0 means all).",
    )
    ap.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="Optional working directory root. Defaults to a temporary directory.",
    )
    ap.add_argument("--evosuite-jar", type=Path, default=None)
    ap.add_argument("--junit-jar", type=Path, default=None)
    ap.add_argument("--hamcrest-jar", type=Path, default=None)
    args = ap.parse_args()

    projects = _iter_projects(args.projects_root)
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)

    if args.work_root:
        work_root = args.work_root
        work_root.mkdir(parents=True, exist_ok=True)
    else:
        work_root = Path(tempfile.mkdtemp(prefix="baseline_sf110_"))

    # Prepare SF110-style shared runtime lib directory (../lib from each project).
    shared_lib = work_root / "lib"
    shared_lib.mkdir(parents=True, exist_ok=True)
    if args.evosuite_jar and args.evosuite_jar.exists():
        shutil.copyfile(args.evosuite_jar, shared_lib / "evosuite.jar")
    if args.junit_jar and args.junit_jar.exists():
        shutil.copyfile(args.junit_jar, shared_lib / "junit-4.11.jar")
    if args.hamcrest_jar and args.hamcrest_jar.exists():
        shutil.copyfile(args.hamcrest_jar, shared_lib / args.hamcrest_jar.name)

    results: List[ProjectResult] = []

    for proj in projects:
        # Copy per project to avoid polluting the dataset with build artifacts.
        proj_work_root = work_root / proj.name
        if proj_work_root.exists():
            shutil.rmtree(proj_work_root)
        proj_copy = _copy_project(proj, work_root)
        try:
            res = classify_project(
                proj_copy,
                ant_cmd=args.ant_cmd,
                java_cmd=args.java_cmd,
                ant_targets=args.ant_targets,
                junit_timeout_sec=args.junit_timeout_sec,
                max_test_classes=args.max_test_classes,
            )
            results.append(res)
        finally:
            # Keep disk usage bounded.
            if proj_copy.exists():
                shutil.rmtree(proj_copy, ignore_errors=True)

    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "project",
                "index",
                "classification",
                "compile_ok",
                "tests_seen",
                "tests_passed",
                "tests_failed",
                "note",
            ]
        )
        for r in results:
            w.writerow(
                [
                    r.project,
                    r.index,
                    r.classification,
                    int(r.compile_ok),
                    r.tests_seen,
                    r.tests_passed,
                    r.tests_failed,
                    r.note,
                ]
            )

    # Print a tiny summary for quick inspection.
    summary: dict[str, int] = {}
    for r in results:
        summary[r.classification] = summary.get(r.classification, 0) + 1
    print("projects:", len(results))
    for k in sorted(summary):
        print(f"{k}: {summary[k]}")
    print("csv:", args.out_csv)


if __name__ == "__main__":
    main()

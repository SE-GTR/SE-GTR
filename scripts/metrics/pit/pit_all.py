#!/usr/bin/env python3
"""Run PIT mutation testing for all projects under output/by_project using latest run_* workdir."""

from __future__ import annotations

import argparse
import csv
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple


def _latest_run_dir(project_dir: Path) -> Path | None:
    runs = sorted(project_dir.glob("run_*"), key=lambda p: p.name)
    return runs[-1] if runs else None


def _project_root(run_dir: Path, project_name: str) -> Path | None:
    cand = run_dir / "workdir" / project_name
    if cand.exists():
        return cand
    cand2 = run_dir / "workdir"
    if cand2.exists():
        return cand2
    return None


def _iter_projects(root: Path) -> List[Tuple[str, Path]]:
    projects: List[Tuple[str, Path]] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if not p.is_dir():
            continue
        projects.append((p.name, p))
    return projects


def _run_one(
    project_name: str,
    project_dir: str,
    out_root: str,
    pitest_home: str,
    python: str,
    ant_cmd: str,
    java_cmd: str,
    compile_targets: str,
    threads: int,
    timeout_const: int,
    target_classes: str,
    target_tests: str,
    green_tests_only: bool,
    test_timeout_sec: int,
) -> Tuple[str, str, str, str, str]:
    project_dir_path = Path(project_dir)
    out_root_path = Path(out_root)
    run_dir = _latest_run_dir(project_dir_path)
    if run_dir is None:
        return project_name, "", "", "no_run", ""

    proj_root = _project_root(run_dir, project_name)
    if proj_root is None:
        return project_name, str(run_dir), "", "missing_workdir", ""

    out_dir = out_root_path / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "pit_after.log"

    measure_script = Path(__file__).resolve().parent / "measure_pit.py"
    cmd = [
        python,
        str(measure_script),
        "--project",
        str(proj_root),
        "--out",
        str(out_dir),
        "--pitest-home",
        str(pitest_home),
        "--ant-cmd",
        ant_cmd,
        "--java-cmd",
        java_cmd,
        "--compile-targets",
        compile_targets,
        "--threads",
        str(threads),
        "--timeout-const",
        str(timeout_const),
    ]
    if target_classes.strip():
        cmd += ["--target-classes", target_classes.strip()]
    if target_tests.strip():
        cmd += ["--target-tests", target_tests.strip()]
    if green_tests_only:
        cmd += ["--green-tests-only", "--test-timeout-sec", str(int(test_timeout_sec))]

    with log_path.open("w", encoding="utf-8") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True, check=False)
    status = "ok" if p.returncode == 0 else "error"
    if status == "ok" and not (out_dir / "mutations.xml").exists():
        status = "no_report"
    return project_name, str(run_dir), str(proj_root), status, str(log_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch PIT runner (parallel) for all projects in output/by_project.")
    ap.add_argument(
        "--projects-root",
        type=Path,
        default=Path("/PATH/TO/REPO/output/by_project"),
        help="Root directory containing per-project run_* folders.",
    )
    ap.add_argument(
        "--out-root",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/pit/after"),
        help="Output root dir; each project writes to <out-root>/<project>/",
    )
    ap.add_argument(
        "--pitest-home",
        type=Path,
        required=True,
        help="Directory containing PIT jars, or a pitest-command-line jar.",
    )
    ap.add_argument("--python", type=str, default="python3", help="Python executable")
    ap.add_argument("--ant-cmd", type=str, default="ant")
    ap.add_argument("--java-cmd", type=str, default="java")
    ap.add_argument("--compile-targets", type=str, default="clean,compile,compile-evosuite")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--timeout-const", type=int, default=4000)
    ap.add_argument("--target-classes", type=str, default="")
    ap.add_argument("--target-tests", type=str, default="")
    ap.add_argument(
        "--green-tests-only",
        action="store_true",
        help="Run PIT using only test classes that pass without mutation.",
    )
    ap.add_argument("--test-timeout-sec", type=int, default=600, help="Timeout seconds per JUnit test class.")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/pit/pit_after_summary.csv"),
        help="Summary CSV path",
    )
    ap.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue even if a project fails.",
    )
    args = ap.parse_args()

    projects_root = args.projects_root.resolve()
    out_root = args.out_root.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    projects = _iter_projects(projects_root)
    rows: List[Tuple[str, str, str, str, str]] = []
    with ProcessPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
        futures = []
        for project_name, project_dir in projects:
            futures.append(
                ex.submit(
                    _run_one,
                    project_name,
                    str(project_dir),
                    str(out_root),
                    str(args.pitest_home),
                    args.python,
                    args.ant_cmd,
                    args.java_cmd,
                    args.compile_targets,
                    int(args.threads),
                    int(args.timeout_const),
                    args.target_classes,
                    args.target_tests,
                    args.green_tests_only,
                    int(args.test_timeout_sec),
                )
            )
        for fut in as_completed(futures):
            rows.append(fut.result())

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    with args.summary.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["project", "run_dir", "project_root", "status", "log"])
        w.writerows(sorted(rows, key=lambda r: r[0]))

    print(f"[OK] Summary written to: {args.summary}")
    if not args.continue_on_error:
        error_rows = [r for r in rows if r[3] in {"error", "missing_workdir", "no_run"}]
        if error_rows:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

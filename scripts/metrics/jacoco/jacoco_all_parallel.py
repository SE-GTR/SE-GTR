#!/usr/bin/env python3
"""Parallel JaCoCo runner for before/after projects."""

from __future__ import annotations

import argparse
import csv
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, List, Tuple


def _latest_run_dir(project_dir: Path) -> Path | None:
    runs = sorted(project_dir.glob("run_*"), key=lambda p: p.name)
    return runs[-1] if runs else None


def _project_root_after(run_dir: Path, project_name: str) -> Path | None:
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
    mode: str,
    projects_root: str,
    out_root: str,
    jacoco_agent: str,
    jacoco_cli: str,
    python: str,
    ant_cmd: str,
    java_cmd: str,
    compile_targets: str,
    batch_size: int,
    timeout_sec: int,
) -> Tuple[str, str, str, str, str]:
    project_dir_path = Path(project_dir)
    out_root_path = Path(out_root)

    if mode == "after":
        run_dir = _latest_run_dir(project_dir_path)
        if run_dir is None:
            return project_name, "", "", "no_run", ""
        proj_root = _project_root_after(run_dir, project_name)
        if proj_root is None:
            return project_name, str(run_dir), "", "missing_workdir", ""
        run_dir_str = str(run_dir)
    else:
        run_dir_str = ""
        proj_root = project_dir_path
        if not (proj_root / "build.xml").exists():
            return project_name, "", str(proj_root), "missing_build_xml", ""

    out_dir = out_root_path / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / ("jacoco_after.log" if mode == "after" else "jacoco_before.log")

    measure_script = Path(__file__).resolve().parent / "measure_jacoco.py"
    cmd = [
        python,
        str(measure_script),
        "--project",
        str(proj_root),
        "--out",
        str(out_dir),
        "--jacoco-agent",
        jacoco_agent,
        "--jacoco-cli",
        jacoco_cli,
        "--ant-cmd",
        ant_cmd,
        "--java-cmd",
        java_cmd,
        "--compile-targets",
        compile_targets,
        "--batch-size",
        str(batch_size),
        "--timeout-sec",
        str(timeout_sec),
    ]
    with log_path.open("w", encoding="utf-8") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True, check=False)
    status = "ok" if p.returncode == 0 else "error"
    if status == "ok" and not (out_dir / "jacoco.xml").exists():
        status = "no_tests"
    return project_name, run_dir_str, str(proj_root), status, str(log_path)


def _chunks(seq: Iterable[Tuple[str, Path]]) -> List[Tuple[str, Path]]:
    return list(seq)


def main() -> int:
    ap = argparse.ArgumentParser(description="Parallel JaCoCo runner (before/after).")
    ap.add_argument(
        "--mode",
        choices=["before", "after"],
        default="after",
        help="Run mode: before (baseline) or after (SE-GTR).",
    )
    ap.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        help="Root directory containing project folders.",
    )
    ap.add_argument(
        "--out-root",
        type=Path,
        default=None,
        help="Output root dir; each project writes to <out-root>/<project>/",
    )
    ap.add_argument("--jacoco-agent", type=Path, required=True, help="Path to jacocoagent.jar")
    ap.add_argument("--jacoco-cli", type=Path, required=True, help="Path to jacococli.jar")
    ap.add_argument("--python", type=str, default="python3", help="Python executable")
    ap.add_argument("--ant-cmd", type=str, default="ant")
    ap.add_argument("--java-cmd", type=str, default="java")
    ap.add_argument("--compile-targets", type=str, default="clean,compile,compile-evosuite")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--timeout-sec", type=int, default=1800)
    ap.add_argument("--workers", type=int, default=4, help="Number of parallel workers.")
    ap.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Do not fail the whole run if some projects error (default: exit non-zero if any error).",
    )
    ap.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Summary CSV path",
    )
    args = ap.parse_args()

    mode = args.mode
    if args.projects_root is None:
        if mode == "after":
            args.projects_root = Path("/PATH/TO/REPO/output/by_project")
        else:
            args.projects_root = Path("/PATH/TO/ISSTA2026/sf110_projects")
    if args.out_root is None:
        args.out_root = Path(
            f"/PATH/TO/REPO/output/analysis/jacoco/{mode}"
        )
    if args.summary is None:
        suffix = "after" if mode == "after" else "before"
        args.summary = Path(
            f"/PATH/TO/REPO/output/analysis/jacoco/jacoco_{suffix}_summary.csv"
        )

    projects_root = args.projects_root.resolve()
    out_root = args.out_root.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    projects = _chunks(_iter_projects(projects_root))

    rows: List[Tuple[str, str, str, str, str]] = []
    with ProcessPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
        futures = []
        for project_name, project_dir in projects:
            futures.append(
                ex.submit(
                    _run_one,
                    project_name,
                    str(project_dir),
                    mode,
                    str(projects_root),
                    str(out_root),
                    str(args.jacoco_agent),
                    str(args.jacoco_cli),
                    args.python,
                    args.ant_cmd,
                    args.java_cmd,
                    args.compile_targets,
                    int(args.batch_size),
                    int(args.timeout_sec),
                )
            )
        for fut in as_completed(futures):
            rows.append(fut.result())

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    with args.summary.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if mode == "after":
            w.writerow(["project", "run_dir", "project_root", "status", "log"])
        else:
            w.writerow(["project", "run_dir", "project_root", "status", "log"])
        w.writerows(sorted(rows, key=lambda r: r[0]))

    print(f"[OK] Summary written to: {args.summary}")
    if not args.continue_on_error:
        error_rows = [r for r in rows if r[3] in {"error", "missing_workdir", "missing_build_xml", "no_run"}]
        if error_rows:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

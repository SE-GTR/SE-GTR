#!/usr/bin/env python3
"""Run JaCoCo coverage for baseline projects (direct project roots)."""

from __future__ import annotations

import argparse
import csv
import subprocess
from pathlib import Path
from typing import List, Tuple


def _iter_projects(root: Path) -> List[Tuple[str, Path]]:
    projects: List[Tuple[str, Path]] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if not p.is_dir():
            continue
        projects.append((p.name, p))
    return projects


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch JaCoCo runner for baseline projects.")
    ap.add_argument(
        "--projects-root",
        type=Path,
        default=Path("/PATH/TO/ISSTA2026/sf110_projects"),
        help="Root directory containing baseline project folders.",
    )
    ap.add_argument(
        "--out-root",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/jacoco/before"),
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
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path("/PATH/TO/REPO/output/analysis/jacoco/jacoco_before_summary.csv"),
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

    measure_script = Path(__file__).resolve().parent / "measure_jacoco.py"
    if not measure_script.exists():
        raise SystemExit(f"measure_jacoco.py not found: {measure_script}")

    rows = []
    for project_name, project_root in _iter_projects(projects_root):
        build_xml = project_root / "build.xml"
        if not build_xml.exists():
            rows.append([project_name, str(project_root), "missing_build_xml", ""])
            if not args.continue_on_error:
                break
            continue

        out_dir = out_root / project_name
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "jacoco_before.log"

        cmd = [
            args.python,
            str(measure_script),
            "--project",
            str(project_root),
            "--out",
            str(out_dir),
            "--jacoco-agent",
            str(args.jacoco_agent),
            "--jacoco-cli",
            str(args.jacoco_cli),
            "--ant-cmd",
            args.ant_cmd,
            "--java-cmd",
            args.java_cmd,
            "--compile-targets",
            args.compile_targets,
            "--batch-size",
            str(args.batch_size),
            "--timeout-sec",
            str(args.timeout_sec),
        ]

        with log_path.open("w", encoding="utf-8") as f:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True, check=False)
        status = "ok" if p.returncode == 0 else "error"
        if status == "ok" and not (out_dir / "jacoco.xml").exists():
            status = "no_tests"
        rows.append([project_name, str(project_root), status, str(log_path)])

        if p.returncode != 0 and not args.continue_on_error:
            break

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    with args.summary.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["project", "project_root", "status", "log"])
        w.writerows(rows)

    print(f"[OK] Summary written to: {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

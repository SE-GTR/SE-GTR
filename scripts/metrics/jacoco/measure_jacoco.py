from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.metrics.common import build_sf110_classpath, classpath_to_str, discover_evosuite_test_classes


def run(cmd: List[str], *, cwd: Path | None = None, timeout: int | None = None, log_file: Path | None = None) -> int:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write("\n$ " + " ".join(cmd) + "\n")
            f.write(p.stdout + "\n")
    return p.returncode


def chunk(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i : i + n] for i in range(0, len(lst), n)]


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure line/branch coverage via JaCoCo for an SF110-style Ant project.")
    ap.add_argument("--project", type=Path, required=True, help="Project root (contains build.xml, src/, evosuite-tests/).")
    ap.add_argument("--out", type=Path, required=True, help="Output directory for jacoco.exec and reports.")
    ap.add_argument("--jacoco-agent", type=Path, required=True, help="Path to jacocoagent.jar")
    ap.add_argument("--jacoco-cli", type=Path, required=True, help="Path to jacococli.jar")
    ap.add_argument(
        "--class-dump-dir",
        type=Path,
        default=None,
        help="Directory to dump loaded classes (improves report accuracy when runtime uses jars).",
    )
    ap.add_argument("--ant-cmd", type=str, default="ant")
    ap.add_argument("--compile-targets", type=str, default="clean,compile,compile-evosuite")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--java-cmd", type=str, default="java")
    ap.add_argument("--timeout-sec", type=int, default=1800, help="Per-batch timeout for running JUnit.")
    args = ap.parse_args()

    project = args.project.resolve()
    out = args.out.resolve()
    # Normalize tool paths to absolute so relative inputs won't break under project cwd
    args.jacoco_agent = args.jacoco_agent.resolve()
    args.jacoco_cli = args.jacoco_cli.resolve()
    out.mkdir(parents=True, exist_ok=True)

    log_file = out / "jacoco_run.log"

    if not (project / "build.xml").exists():
        raise SystemExit(f"build.xml not found under: {project}")

    if not args.jacoco_agent.exists():
        raise SystemExit(f"jacoco-agent not found: {args.jacoco_agent}")
    if not args.jacoco_cli.exists():
        raise SystemExit(f"jacoco-cli not found: {args.jacoco_cli}")

    # 1) Compile (best-effort)
    targets = [t.strip() for t in args.compile_targets.split(",") if t.strip()]
    rc = run([args.ant_cmd, *targets], cwd=project, timeout=None, log_file=log_file)
    if rc != 0:
        print(f"[WARN] Ant compile returned non-zero ({rc}). JaCoCo may fail. See: {log_file}")

    # 2) Discover tests
    tests = discover_evosuite_test_classes(project)
    if not tests:
        print("[INFO] No EvoSuite tests found under evosuite-tests/. Nothing to run.")
        return

    # 3) Build classpath
    cp_entries = build_sf110_classpath(project, include_evosuite_tests=True)
    cp = classpath_to_str(cp_entries)

    jacoco_exec = out / "jacoco.exec"
    if jacoco_exec.exists():
        jacoco_exec.unlink()
    class_dump_dir = args.class_dump_dir or (out / "classdump")
    class_dump_dir.mkdir(parents=True, exist_ok=True)

    # 4) Run tests in batches
    batches = chunk(tests, max(1, int(args.batch_size)))
    for i, batch in enumerate(batches, start=1):
        append = "true" if i > 1 else "false"
        agent_opt = (
            f"-javaagent:{args.jacoco_agent}=destfile={jacoco_exec},append={append},"
            f"classdumpdir={class_dump_dir}"
        )

        cmd = [args.java_cmd, agent_opt, "-cp", cp, "org.junit.runner.JUnitCore", *batch]
        rc = run(cmd, cwd=project, timeout=int(args.timeout_sec), log_file=log_file)
        if rc != 0:
            # continue, but record; failing tests still may produce partial coverage
            print(f"[WARN] Batch {i}/{len(batches)} returned non-zero ({rc}). Continuing. See: {log_file}")

    # 5) Report
    html_dir = out / "jacoco-html"
    xml_path = out / "jacoco.xml"

    cmd_report = [
        args.java_cmd,
        "-jar",
        str(args.jacoco_cli),
        "report",
        str(jacoco_exec),
    ]
    # Use classdumpdir first so report matches executed bytecode.
    if class_dump_dir.exists():
        cmd_report += ["--classfiles", str(class_dump_dir)]
    if (project / "build" / "classes").exists():
        cmd_report += ["--classfiles", str(project / "build" / "classes")]
    cmd_report += [
        "--sourcefiles",
        str(project / "src" / "main" / "java"),
        "--html",
        str(html_dir),
        "--xml",
        str(xml_path),
    ]
    rc = run(cmd_report, cwd=project, timeout=None, log_file=log_file)
    if rc != 0:
        raise SystemExit(f"JaCoCo report generation failed ({rc}). See: {log_file}")

    print(f"[OK] JaCoCo coverage generated:\n  exec: {jacoco_exec}\n  xml:  {xml_path}\n  html: {html_dir}\n  log:  {log_file}")


if __name__ == "__main__":
    main()

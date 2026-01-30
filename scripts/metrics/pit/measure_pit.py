from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.metrics.common import build_sf110_classpath, classpath_to_str, discover_evosuite_test_classes, list_jars


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


def derive_target_class_prefixes(project_root: Path, *, min_segments: int = 3) -> List[str]:
    """
    Derive reasonably specific package prefixes from build/classes to avoid mutating dependencies.

    Strategy:
      - list .class files under build/classes (exclude inner classes)
      - compute package path segments
      - choose the longest common prefix if it is specific enough
      - otherwise, fall back to top-k frequent prefixes of length=min_segments

    Output: list of glob patterns like "net.sourceforge.squirrel_sql.*"
    """
    classes_dir = project_root / "build" / "classes"
    if not classes_dir.exists():
        return ["*"]  # fallback

    fqcn: List[str] = []
    for p in classes_dir.rglob("*.class"):
        if "$" in p.name:
            continue
        rel = p.relative_to(classes_dir).as_posix()
        if rel.endswith(".class"):
            rel = rel[:-6]
        fqcn.append(rel.replace("/", "."))

    if not fqcn:
        return ["*"]

    # longest common prefix by segments
    segs = [f.split(".")[:-1] for f in fqcn]  # package segments (exclude class name)
    common: List[str] = []
    for parts in zip(*segs):
        if all(x == parts[0] for x in parts):
            common.append(parts[0])
        else:
            break

    if len(common) >= min_segments:
        return [".".join(common) + ".*"]

    # fallback: frequent prefixes of min_segments
    counts: dict[str, int] = {}
    for pkg in segs:
        if len(pkg) >= min_segments:
            pref = ".".join(pkg[:min_segments])
        elif pkg:
            pref = ".".join(pkg)
        else:
            pref = ""
        if pref:
            counts[pref] = counts.get(pref, 0) + 1
    if not counts:
        # Default package (no package segments). Use explicit class names to avoid PIT mutating itself.
        return sorted(set(fqcn))

    # select prefixes that cover most classes, but limit to 5 patterns
    total = len(segs)
    selected: List[str] = []
    covered = 0
    for pref, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        selected.append(pref + ".*")
        covered += cnt
        if covered / max(1, total) >= 0.95 or len(selected) >= 5:
            break
    return selected


def build_pitest_cp(pitest_home: Path) -> str:
    jars: List[Path] = []
    if pitest_home.is_file() and pitest_home.suffix == ".jar":
        jars = [pitest_home]
    else:
        jars = list_jars(pitest_home)
    if not jars:
        raise SystemExit(
            "No PIT jars found. Provide --pitest-home as either:\n"
            "  (a) a directory that contains PIT jars (recursively), or\n"
            "  (b) a path to pitest-command-line-*.jar"
        )
    return os.pathsep.join(str(j) for j in jars)


def filter_passing_tests(
    test_classes: List[str],
    *,
    java_cmd: str,
    classpath: str,
    timeout_sec: int,
    log_file: Path,
) -> List[str]:
    """Run JUnitCore for each test class and return only passing tests."""
    passing: List[str] = []
    failed: List[str] = []
    for cls in test_classes:
        try:
            p = subprocess.run(
                [java_cmd, "-cp", classpath, "org.junit.runner.JUnitCore", cls],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failed.append(cls)
            continue
        if p.returncode == 0:
            passing.append(cls)
        else:
            failed.append(cls)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n[GREEN] passing tests: {len(passing)} / {len(test_classes)}\n")
        if failed:
            f.write("[GREEN] sample failing tests:\n")
            for cls in failed[:5]:
                f.write(f"  - {cls}\n")
    return passing


def main() -> None:
    ap = argparse.ArgumentParser(description="Run PIT mutation testing for an SF110-style Ant project (best-effort).")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="Output directory for PIT reports.")
    ap.add_argument("--pitest-home", type=Path, required=True, help="Directory containing PIT jars, or a pitest-command-line jar.")
    ap.add_argument("--ant-cmd", type=str, default="ant")
    ap.add_argument("--compile-targets", type=str, default="clean,compile,compile-evosuite")
    ap.add_argument("--java-cmd", type=str, default="java")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--timeout-const", type=int, default=4000, help="PIT timeoutConst (ms multiplier).")
    ap.add_argument("--target-classes", type=str, default="", help="Override PIT --targetClasses (comma-separated globs).")
    ap.add_argument("--target-tests", type=str, default="", help="Override PIT --targetTests (comma-separated globs).")
    ap.add_argument(
        "--green-tests-only",
        action="store_true",
        help="Run PIT using only test classes that pass without mutation.",
    )
    ap.add_argument("--test-timeout-sec", type=int, default=600, help="Timeout seconds per JUnit test class.")
    ap.add_argument(
        "--classpath-sep",
        type=str,
        default=",",
        help="Separator used inside PIT --classPath value (PIT expects comma-separated classpath).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print the PIT command without executing.")
    args = ap.parse_args()

    project = args.project.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    log_file = out / "pit_run.log"

    if not (project / "build.xml").exists():
        raise SystemExit(f"build.xml not found under: {project}")

    # 1) Compile
    targets = [t.strip() for t in args.compile_targets.split(",") if t.strip()]
    rc = run([args.ant_cmd, *targets], cwd=project, timeout=None, log_file=log_file)
    if rc != 0:
        print(f"[WARN] Ant compile returned non-zero ({rc}). PIT may fail. See: {log_file}")

    # 2) Build project classpath (for tests + dependencies)
    project_cp_entries = build_sf110_classpath(project, include_evosuite_tests=True)
    project_cp = args.classpath_sep.join(str(p) for p in project_cp_entries)
    project_cp_java = classpath_to_str(project_cp_entries)

    # 3) Targets
    if args.target_classes.strip():
        target_classes = args.target_classes.strip()
    else:
        patterns = derive_target_class_prefixes(project, min_segments=3)
        target_classes = ",".join(patterns)

    if args.target_tests.strip():
        target_tests = args.target_tests.strip()
    else:
        evo_tests = discover_evosuite_test_classes(project)
        if args.green_tests_only and evo_tests:
            passing = filter_passing_tests(
                evo_tests,
                java_cmd=args.java_cmd,
                classpath=project_cp_java,
                timeout_sec=int(args.test_timeout_sec),
                log_file=log_file,
            )
            if not passing:
                with log_file.open("a", encoding="utf-8") as f:
                    f.write("\n[GREEN] No passing tests found. Skipping PIT run.\n")
                print("[WARN] No passing tests found; PIT skipped.")
                return
            target_tests = ",".join(passing)
        elif evo_tests:
            # prefer matching EvoSuite tests; if none found, fall back to broad pattern
            roots = [p[:-2] for p in target_classes.split(",") if p.endswith(".*")]  # remove .*
            if roots:
                target_tests = ",".join([r + ".*ESTest" for r in roots])
            else:
                target_tests = "*ESTest"
        else:
            target_tests = "*Test"

    # 4) PIT classpath (tooling)
    pit_cp = build_pitest_cp(args.pitest_home)

    # 5) Build PIT command
    cmd = [
        args.java_cmd,
        "-cp",
        pit_cp,
        "org.pitest.mutationtest.commandline.MutationCoverageReport",
        "--reportDir",
        str(out),
        "--targetClasses",
        target_classes,
        "--targetTests",
        target_tests,
        "--sourceDirs",
        str(project / "src" / "main" / "java"),
        "--classPath",
        project_cp,
        "--threads",
        str(max(1, int(args.threads))),
        "--timeoutConst",
        str(int(args.timeout_const)),
        "--outputFormats",
        "HTML,XML",
    ]

    if args.dry_run:
        print(" ".join(cmd))
        return

    rc = run(cmd, cwd=project, timeout=None, log_file=log_file)
    if rc != 0:
        raise SystemExit(f"PIT failed ({rc}). See: {log_file}")

    print(f"[OK] PIT reports generated under: {out}\n  log: {log_file}\n  targetClasses: {target_classes}\n  targetTests: {target_tests}")


if __name__ == "__main__":
    main()

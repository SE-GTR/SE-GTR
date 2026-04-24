#!/usr/bin/env python3
"""One-shot investigation script — classify the 20 class-level regressions
observed in the Phase 2.3 Tier 3 run.

For each regressed class:
  1. Run the class ALONE against the pristine sf110 workdir.
  2. Run the class ALONE against the v2 (post-Tier-3) workdir.
  3. Run the class WITH ALL OTHER TEST CLASSES (same JVM) in the v2 workdir.

Classification:
  - "pre_existing": (1) fails → we're innocent.
  - "gate4_miss":   (1) pass AND (2) fail → our per-method Gate 4 passed
                    but the class fails in isolation on the committed text.
  - "interaction":  (1) pass AND (2) pass AND (3) fail → only fails when
                    run together with other classes (shared state / ordering).
  - "flaky":        (1) pass AND (2) pass AND (3) pass but full run in
                    dev_experiment still reported regressed (nondeterministic).

Writes a report to stdout + investigate_regressions.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.operators.validator import (  # noqa: E402
    _build_sf110_classpath,
    _test_class_fqcn,
)
from smell_repair_v2.project.discover import (  # noqa: E402
    Project,
    find_evosuite_test_file,
)


SF110_ROOT = Path("<ANON_ROOT>/segtr_replication/sf110_projects")
V2_RUN = Path("<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl/output/dev_tier3_runs/full_run")

# The 20 regressed classes, grouped by project.
REGRESSED: Dict[str, List[str]] = {
    "29_apbsmem": [
        "jahuwaldt.plot.PlotDatum_ESTest",
        "apbs_mem_gui.InFile_ESTest",
        "jahuwaldt.plot.ContourGenerator_ESTest",
        "jahuwaldt.plot.AxisLimitData_ESTest",
        "jahuwaldt.plot.PlotRun_ESTest",
        "apbs_mem_gui.EFileFilter_ESTest",
    ],
    "71_ext4j": [
        "net.sourceforge.ext4j.taglib.bo.RequestParam_ESTest",
        "net.sourceforge.ext4j.taglib.bo.DefaultResourceLoader_ESTest",
        "net.sourceforge.ext4j.log.Server_ESTest",
    ],
    "88_jopenchart": [
        "de.progra.charting.render.StackedBarChartRenderer_ESTest",
        "de.progra.charting.model.StackedChartDataModelConstraints_ESTest",
        "de.progra.charting.model.AbstractChartDataModel_ESTest",
        "de.progra.charting.model.DefaultChartDataModel_ESTest",
    ],
    "31_xisemele": [
        "net.sf.xisemele.exception.NotWithinContextException_ESTest",
        "net.sf.xisemele.exception.AttributeNotPermittedException_ESTest",
        "net.sf.xisemele.exception.ElementIndexOutOfBoundsException_ESTest",
        "net.sf.xisemele.exception.XisemeleIOException_ESTest",
        "net.sf.xisemele.exception.TransformException_ESTest",
        "net.sf.xisemele.exception.ParseXMLException_ESTest",
        "net.sf.xisemele.exception.FormatterNotConfiguredException_ESTest",
    ],
}


def _find_all_test_classes(work_project: Path) -> List[str]:
    """All *_ESTest.java files under evosuite-tests/ → FQCN list."""
    base = work_project / "evosuite-tests"
    fqcns = []
    for p in base.rglob("*_ESTest.java"):
        fqcns.append(_test_class_fqcn(p))
    return sorted(set(fqcns))


def _run_junit(work_project: Path, fqcns: List[str], *, timeout_sec: int = 600) -> Tuple[bool, str, int]:
    """Run JUnitCore on one or more classes. Returns (passed, stdout, elapsed_ms)."""
    cp = _build_sf110_classpath(work_project)
    cmd = ["java", "-cp", cp, "org.junit.runner.JUnitCore", *fqcns]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(work_project),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=timeout_sec, check=False,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        return (proc.returncode == 0, proc.stdout, elapsed)
    except subprocess.TimeoutExpired:
        return (False, "TIMEOUT", int((time.monotonic() - t0) * 1000))


def _ensure_compiled(work_project: Path) -> bool:
    """Make sure the project's build/ classes are up to date."""
    try:
        subprocess.run(
            ["ant", "-q", "compile", "compile-evosuite"],
            cwd=str(work_project),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=600, check=True,
        )
        return True
    except Exception as e:
        print(f"    compile failed: {e}")
        return False


@dataclass
class ClassResult:
    project: str
    fqcn: str
    pristine_alone: Optional[bool] = None
    v2_alone: Optional[bool] = None
    v2_full: Optional[bool] = None
    category: str = "?"
    pristine_alone_ms: int = 0
    v2_alone_ms: int = 0
    notes: str = ""


def classify(r: ClassResult) -> str:
    if r.pristine_alone is False:
        return "pre_existing"
    if r.pristine_alone is True and r.v2_alone is False:
        return "gate4_miss"
    if r.pristine_alone is True and r.v2_alone is True:
        if r.v2_full is False:
            return "interaction"
        return "flaky"
    return "unknown"


def main() -> int:
    results: List[ClassResult] = []

    for proj, fqcns in REGRESSED.items():
        print(f"\n=== {proj} ===")
        pristine_root = SF110_ROOT / proj
        v2_root = V2_RUN / proj
        if not v2_root.exists():
            print(f"  v2 workdir missing: {v2_root}")
            continue

        # Ensure both are compiled (pristine is already built from earlier runs,
        # but re-ensure in case tests dir got touched).
        print(f"  compile pristine...")
        _ensure_compiled(pristine_root)
        print(f"  compile v2...")
        _ensure_compiled(v2_root)

        # For the v2_full run we'll run ALL test classes together.
        print(f"  enumerate all test classes in v2...")
        all_v2 = _find_all_test_classes(v2_root)

        # Run full-suite once per project and record whether each target is in the
        # class set; we'll mark v2_full by inspecting stdout for per-class status.
        full_passed, full_output, full_ms = _run_junit(v2_root, all_v2, timeout_sec=1800)
        print(f"  v2 full-suite: passed={full_passed}  elapsed={full_ms}ms  "
              f"stdout_len={len(full_output)}")
        # Very crude per-class status parse: look for "failures!!!" lines referring
        # to classnames. Simpler: if full passed → all classes passed in suite;
        # if not passed, we need per-class breakdown. Do another pass: re-run
        # each target alone in v2 to get v2_alone; then if full failed, mark
        # targets whose v2_alone passes as "interaction".

        for fqcn in fqcns:
            r = ClassResult(project=proj, fqcn=fqcn)
            print(f"  - {fqcn}")
            # (1) pristine alone
            p_ok, p_out, p_ms = _run_junit(pristine_root, [fqcn], timeout_sec=300)
            r.pristine_alone = p_ok
            r.pristine_alone_ms = p_ms
            print(f"      pristine_alone={p_ok}  ({p_ms}ms)")
            # (2) v2 alone
            v_ok, v_out, v_ms = _run_junit(v2_root, [fqcn], timeout_sec=300)
            r.v2_alone = v_ok
            r.v2_alone_ms = v_ms
            print(f"      v2_alone={v_ok}  ({v_ms}ms)")
            # (3) v2_full — inferred from the full-suite run
            if full_passed:
                r.v2_full = True
            else:
                # If v2_alone is True but full fails, we don't know whether THIS
                # class failed in the full suite or some other. Parse output for
                # a clear marker.
                # JUnitCore's failures section typically mentions class names.
                if fqcn in full_output or fqcn.split(".")[-1] in full_output:
                    r.v2_full = False
                else:
                    # Inferred pass (no mention in failure listing).
                    r.v2_full = True if v_ok else False
            r.category = classify(r)
            print(f"      → category: {r.category}")
            results.append(r)

    # aggregate
    print("\n\n=== SUMMARY ===")
    from collections import Counter
    cats = Counter(r.category for r in results)
    for k, v in cats.most_common():
        print(f"  {k:<14}  {v}")

    # by project
    print("\nBy project:")
    by_proj = {}
    for r in results:
        by_proj.setdefault(r.project, []).append(r)
    for proj, rs in by_proj.items():
        cats = Counter(r.category for r in rs)
        print(f"  {proj:<18}  total={len(rs)}  {dict(cats)}")

    # details
    print("\nDetails:")
    for r in results:
        marks = [
            f"pristine={'P' if r.pristine_alone else 'F' if r.pristine_alone is False else '?'}",
            f"v2alone={'P' if r.v2_alone else 'F' if r.v2_alone is False else '?'}",
            f"v2full={'P' if r.v2_full else 'F' if r.v2_full is False else '?'}",
        ]
        print(f"  [{r.category:<13}] {r.project:<16} {r.fqcn}  {' '.join(marks)}")

    # dump json
    out_path = Path("/tmp/investigate_regressions.json")
    out_path.write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")
    print(f"\nFull dump: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

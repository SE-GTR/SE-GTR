#!/usr/bin/env python3
"""End-to-end Tier 1 validation:

1. Copy each project into an isolated workdir (originals untouched).
2. Apply Tier 1 operator plans per (class, smell, method) — each plan-group
   is tried with the full 7-gate validator **including Gate 3 (ant compile)
   and Gate 4 (JUnitCore execution)**. Rejected transforms are rolled back.
3. After every accepted transform is committed, re-run Smelly-E on the final
   workdir and diff smell counts against the original JSON.

Intentionally no new functionality: this is a measurement harness.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.analysis.smelly import run_smelly, load_smelly_json  # noqa: E402
from smell_repair_v2.operators.base import ExecutionContext, OperatorScope  # noqa: E402
from smell_repair_v2.operators.catalog import get_operator_scope  # noqa: E402
from smell_repair_v2.operators.executor import OperatorExecutor  # noqa: E402
from smell_repair_v2.operators.import_manager import ImportManager  # noqa: E402
from smell_repair_v2.operators.validator import MultiGateValidator, ValidatorConfig  # noqa: E402
from smell_repair_v2.project.discover import (  # noqa: E402
    Project,
    find_cut_source_file,
    find_evosuite_test_file,
    resolve_cut_fqcn_from_test,
)
from smell_repair_v2.project.java_extract import (  # noqa: E402
    TEST_METHOD_START_RE,
    _scan_to_matching_brace,
)
from smell_repair_v2.tiers.router import get_tier_for_smell  # noqa: E402
from smell_repair_v2.tiers.tier1_deterministic import get_tier1_plan  # noqa: E402


SMELLY_NAME_TO_ID = {
    "Not null assertion": "NNA",
    "Duplicated Setup": "DS",
    "Testing the same exception scenario": "TSES",
    "Asserting Constants": "AC",
}

SMELL_NAMES_OF_INTEREST = list(SMELLY_NAME_TO_ID.keys()) + [
    "Testing only field accesors",            # TOFA
    "Not asserted return values",             # NARV
    "Not asserted side effects",              # NASE
    "Test without assertions",                # TSVM (in this repo's naming)
    "Multiple calls to the same void method", # OIMT
    "Exceptions due to null arguments",       # ENET-ish
    "Asserting object initialization multiple times",  # ARPM
    "Assertion with not related parent class method",  # ARPM-like
]


SMELLY_CFG = {
    "jar": Path("<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl/tools/backup-smelly-evidence/target/smelly-1.0-shaded.jar"),
    "evosuite_runtime_jar": Path("<ANON_ROOT>/segtr_replication/evosuite-1.2.0/evosuite-standalone-runtime-1.2.0.jar"),
    "junit_jar": Path("<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl/tools/backup-smelly-evidence/junit-4.11.jar"),
}


@dataclass
class PlanOutcome:
    project: str
    class_key: str
    test_method: str
    smell_id: str
    n_plans: int
    accepted: bool
    stage: str           # "executor" | "gate1_banned" | "gate2_syntax" | "gate3_compile" | "gate4_test" | "gate5" | "gate6" | "gate7" | "accepted"
    reason: Optional[str] = None


@dataclass
class ProjectReport:
    project: str
    plan_outcomes: List[PlanOutcome] = field(default_factory=list)
    before_counts: Dict[str, int] = field(default_factory=dict)
    after_counts: Dict[str, int] = field(default_factory=dict)
    class_tests_before: Dict[str, bool] = field(default_factory=dict)
    class_tests_after: Dict[str, bool] = field(default_factory=dict)
    elapsed_sec: float = 0.0


# ----------------------------------------------------------------------------
# workdir setup
# ----------------------------------------------------------------------------


def prepare_workdir(workdir_root: Path, project_dir: Path) -> Path:
    """Copy <project_dir> into workdir_root/<name> and set up shared lib."""
    workdir_root.mkdir(parents=True, exist_ok=True)
    dest = workdir_root / project_dir.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(project_dir, dest, symlinks=True)
    # wipe stale build artefacts so the first ant run compiles fresh
    for sub in ("build/classes", "build/evosuite"):
        p = dest / sub
        if p.exists():
            shutil.rmtree(p)

    shared_lib = workdir_root / "lib"
    shared_lib.mkdir(exist_ok=True)
    need = [
        ("<ANON_ROOT>/segtr_replication/evosuite-1.2.0/evosuite-standalone-runtime-1.2.0.jar",
         "evosuite-standalone-runtime-1.2.0.jar"),
        ("<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl/tools/backup-smelly-evidence/junit-4.11.jar",
         "junit-4.11.jar"),
        ("<ANON_ROOT>/segtr_replication/sf110_projects_pilot/lib/hamcrest-core-1.3.jar",
         "hamcrest-core-1.3.jar"),
    ]
    for src_str, name in need:
        src = Path(src_str)
        if src.exists():
            dst = shared_lib / name
            if not dst.exists():
                shutil.copyfile(src, dst)
    # SF110 build.xml commonly references evosuite.jar — provide alias
    alias = shared_lib / "evosuite.jar"
    primary = shared_lib / "evosuite-standalone-runtime-1.2.0.jar"
    if primary.exists() and not alias.exists():
        shutil.copyfile(primary, alias)
    return dest


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def extract_method_with_range(file_text: str, method_name: str) -> Optional[Tuple[str, int, int]]:
    for m in TEST_METHOD_START_RE.finditer(file_text):
        if m.group("name") != method_name:
            continue
        open_idx = m.end() - 1
        close_idx = _scan_to_matching_brace(file_text, open_idx)
        if close_idx < 0:
            return None
        block = file_text[m.start() : close_idx + 1]
        start_line = file_text.count("\n", 0, m.start()) + 1
        end_line = file_text.count("\n", 0, close_idx + 1) + 1
        return block, start_line, end_line
    return None


def splice_method_back(file_text: str, method_text_old: str, method_text_new: str) -> str:
    if method_text_old not in file_text:
        return file_text
    return file_text.replace(method_text_old, method_text_new, 1)


def count_smells_by_id(smelly_data: Dict[str, Any]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for _cls, smells in smelly_data.items():
        for sname, items in smells.items():
            n = len(items)
            if n == 0:
                continue
            totals[sname] = totals.get(sname, 0) + n
    return totals


# ----------------------------------------------------------------------------
# core driver
# ----------------------------------------------------------------------------


def try_one_plan_group(
    *,
    project_root: Path,
    test_file: Path,
    plans,
    scope_text: str,
    method_text_old: Optional[str],
    ctx: ExecutionContext,
    original_imports,
    log_fh,
    project_name: str,
    class_key: str,
    method_name: str,
    smell_id: str,
) -> PlanOutcome:
    executor = OperatorExecutor(ImportManager())
    original_file_text = test_file.read_text(encoding="utf-8", errors="ignore")

    outcome = executor.execute_plan(scope_text, plans, ctx)
    any_success = any(r.success for r in outcome.results)
    if not any_success:
        reasons = [r.rejection_reason for r in outcome.results]
        po = PlanOutcome(
            project=project_name, class_key=class_key, test_method=method_name,
            smell_id=smell_id, n_plans=len(plans), accepted=False,
            stage="executor", reason=";".join(str(x) for x in reasons)[:500],
        )
        _log(log_fh, po)
        return po

    # compose modified file text
    if get_operator_scope(plans[0].op) == OperatorScope.FILE:
        modified_file_text = outcome.final_text
    else:
        assert method_text_old is not None
        modified_file_text = splice_method_back(
            original_file_text, method_text_old, outcome.final_text
        )
    # import reconcile
    mgr = ImportManager()
    modified_file_text, _ = mgr.reconcile(
        modified_file_text, outcome.used_asserts, original_imports=original_imports
    )

    # full validator (all gates)
    cfg = ValidatorConfig(
        project_root=project_root,
        test_file=test_file,
        skip_compile=False,
        skip_tests=False,
        original_imports=original_imports,
    )
    validator = MultiGateValidator(cfg)
    accepted, reason = validator.validate(original_file_text, modified_file_text, ctx)

    if accepted:
        # modified file is now on disk (validator wrote it); success committed
        po = PlanOutcome(
            project=project_name, class_key=class_key, test_method=method_name,
            smell_id=smell_id, n_plans=len(plans), accepted=True, stage="accepted",
            reason=None,
        )
    else:
        # validator restored original on failure
        stage = reason.split(":", 1)[0] if reason else "unknown"
        po = PlanOutcome(
            project=project_name, class_key=class_key, test_method=method_name,
            smell_id=smell_id, n_plans=len(plans), accepted=False, stage=stage,
            reason=reason[:500],
        )
    _log(log_fh, po)
    return po


def process_project(
    project_dir: Path,
    smelly_json_path: Path,
    workdir_root: Path,
    log_fh,
) -> ProjectReport:
    start = time.time()
    project_name = project_dir.name
    report = ProjectReport(project=project_name)

    # 1. prepare workdir
    work_project = prepare_workdir(workdir_root, project_dir)
    print(f"[{project_name}] workdir: {work_project}")

    # 2. initial compile (fresh build) so validator's incremental compile works
    from smell_repair_v2.project.ant import run_ant
    try:
        run_ant(work_project, ["clean", "compile", "compile-evosuite"], timeout_sec=600)
    except Exception as e:
        print(f"[{project_name}] initial compile failed: {e}")
        return report

    # 3. original smells
    with smelly_json_path.open() as f:
        original_smells = json.load(f)
    report.before_counts = count_smells_by_id(original_smells)

    # 4. baseline class-level test pass
    _measure_class_test_pass(work_project, original_smells, report.class_tests_before)

    # 5. iterate (class, smell)
    _iterate_tier1(
        original_smells=original_smells,
        work_project=work_project,
        project_name=project_name,
        report=report,
        log_fh=log_fh,
    )

    # 6. rerun Smelly-E on modified workdir
    after_smells = _rerun_smelly(work_project, workdir_root, project_name)
    if after_smells is not None:
        report.after_counts = count_smells_by_id(after_smells)

    # 7. post-repair class-level test pass
    _measure_class_test_pass(work_project, original_smells, report.class_tests_after)

    report.elapsed_sec = time.time() - start
    return report


def _iterate_tier1(
    *,
    original_smells: Dict[str, Any],
    work_project: Path,
    project_name: str,
    report: ProjectReport,
    log_fh,
) -> None:
    """Walk all Tier 1 evidence, try plan groups one at a time, committing
    those the validator accepts."""
    for smelly_key, class_smells in original_smells.items():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj_obj = Project(folder_name=work_project.name, real_name=work_project.name, root=work_project)
        test_file = find_evosuite_test_file(proj_obj, cut_simple)
        if test_file is None:
            continue
        cut_fqcn = resolve_cut_fqcn_from_test(test_file, cut_simple) or smelly_key
        # Resolve CUT source so Tier 1 handlers can inspect method return types
        # (used by TSES void-only precondition).
        cut_source_text: Optional[str] = None
        if cut_fqcn:
            cut_src_file = find_cut_source_file(proj_obj, cut_fqcn)
            if cut_src_file is not None and cut_src_file.exists():
                try:
                    cut_source_text = cut_src_file.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    cut_source_text = None
        mgr = ImportManager()
        original_imports = mgr.existing_imports(test_file.read_text(encoding="utf-8", errors="ignore"))

        # Process method-level smells (NNA / TSES / AC) BEFORE DS.
        # Rationale: DS is file-level and restructures the class (adds @Before,
        # removes prefix lines from each group_test). If DS runs first, the
        # original-file `begin_line` numbers for NNA/AC become invalid and
        # pristine method bodies won't splice. Method-level smells touch only
        # their target method body and leave setup prefixes untouched, so DS
        # can still run after them on the modified file.
        pristine_file_text = test_file.read_text(encoding="utf-8", errors="ignore")
        for smelly_name, smell_id in SMELLY_NAME_TO_ID.items():
            if smell_id == "DS":
                continue
            items = class_smells.get(smelly_name, []) or []
            if not items:
                continue
            by_method: Dict[str, List[Dict[str, Any]]] = {}
            for it in items:
                tm = it.get("test_method")
                if tm:
                    by_method.setdefault(tm, []).append(it)

            for method_name, tm_items in by_method.items():
                merged: Dict[str, Any] = {}
                for it in tm_items:
                    for k, v in (it.get("evidence") or {}).items():
                        if isinstance(v, list):
                            merged.setdefault(k, []).extend(v)
                        else:
                            merged[k] = v

                extract = extract_method_with_range(pristine_file_text, method_name)
                if extract is None:
                    continue
                method_text, start_line, end_line = extract
                # confirm the method body still exists verbatim on disk; if a
                # prior accepted plan rewrote it (shouldn't happen across
                # different methods, but DS can), skip to avoid a bad splice.
                current_file_text = test_file.read_text(encoding="utf-8", errors="ignore")
                if method_text not in current_file_text:
                    continue
                ctx = ExecutionContext(
                    method_name=method_name,
                    method_line_range=(start_line, end_line),
                    file_text=pristine_file_text, cut_fqcn=cut_fqcn,
                    cut_source=cut_source_text,
                )
                plans = get_tier1_plan(
                    smell_id, merged, method_text=method_text,
                    file_text=pristine_file_text, ctx=ctx,
                )
                if not plans:
                    continue
                po = try_one_plan_group(
                    project_root=work_project, test_file=test_file, plans=plans,
                    scope_text=method_text, method_text_old=method_text, ctx=ctx,
                    original_imports=original_imports, log_fh=log_fh,
                    project_name=project_name, class_key=smelly_key,
                    method_name=method_name, smell_id=smell_id,
                )
                report.plan_outcomes.append(po)

        # DS: file-level, applied AFTER method-level smells. Uses the current
        # (possibly modified) file state; Tier 1's `verify_common_prefix_in_file`
        # correctly detects whether the prior edits invalidated the shared prefix.
        ds_items = class_smells.get("Duplicated Setup", []) or []
        if ds_items and get_tier_for_smell("DS") == 1:
            merged = {"duplicated_setup_groups": []}
            seen = set()
            for it in ds_items:
                for g in it.get("evidence", {}).get("duplicated_setup_groups", []):
                    gid = g.get("group_id")
                    if gid in seen:
                        continue
                    seen.add(gid)
                    merged["duplicated_setup_groups"].append(g)
            if merged["duplicated_setup_groups"]:
                file_text_now = test_file.read_text(encoding="utf-8", errors="ignore")
                ctx = ExecutionContext(
                    method_name="__file__",
                    method_line_range=(1, len(file_text_now.splitlines())),
                    file_text=file_text_now, cut_fqcn=cut_fqcn,
                    cut_source=cut_source_text,
                )
                plans = get_tier1_plan(
                    "DS", merged, method_text=file_text_now,
                    file_text=file_text_now, ctx=ctx,
                ) or []
                if plans:
                    po = try_one_plan_group(
                        project_root=work_project, test_file=test_file, plans=plans,
                        scope_text=file_text_now, method_text_old=None, ctx=ctx,
                        original_imports=original_imports, log_fh=log_fh,
                        project_name=project_name, class_key=smelly_key,
                        method_name="__file__", smell_id="DS",
                    )
                    report.plan_outcomes.append(po)


def _measure_class_test_pass(
    work_project: Path,
    smelly_data: Dict[str, Any],
    out: Dict[str, bool],
) -> None:
    import subprocess
    from smell_repair_v2.operators.validator import _build_sf110_classpath, _test_class_fqcn

    classes_to_run: List[Path] = []
    for smelly_key in smelly_data.keys():
        if "." not in smelly_key:
            continue
        _, cut_simple = smelly_key.split(".", 1)
        proj_obj = Project(folder_name=work_project.name, real_name=work_project.name, root=work_project)
        tf = find_evosuite_test_file(proj_obj, cut_simple)
        if tf is not None and tf not in classes_to_run:
            classes_to_run.append(tf)

    cp = _build_sf110_classpath(work_project)
    for tf in classes_to_run:
        fqcn = _test_class_fqcn(tf)
        try:
            proc = subprocess.run(
                ["java", "-cp", cp, "org.junit.runner.JUnitCore", fqcn],
                cwd=str(work_project), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=300, check=False,
            )
            out[fqcn] = (proc.returncode == 0)
        except Exception:
            out[fqcn] = False


def _rerun_smelly(work_project: Path, workdir_root: Path, project_name: str) -> Optional[Dict[str, Any]]:
    """Smelly expects a directory containing project sub-folders (one project
    per sub-folder). v1 does: isolate the project into tmp_root/<project>/,
    then pass tmp_root.parent as both source_path and test_path.
    """
    smelly_out_dir = workdir_root / "smelly_after"
    smelly_out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"after_{project_name}"

    # Use a per-project parent so Smelly-E only scans THIS project.
    # (Prior runs in a shared tmp_smelly/ contaminated subsequent projects —
    #  after_<proj>.json would include every project from earlier runs.)
    tmp_parent = workdir_root / f"tmp_smelly_{project_name}"
    if tmp_parent.exists():
        shutil.rmtree(tmp_parent)
    tmp_parent.mkdir(parents=True)
    tmp_root = tmp_parent / project_name
    shutil.copytree(work_project, tmp_root, symlinks=True)

    try:
        print(f"[{project_name}] re-running Smelly-E (~1–3 min)...")
        out_path = run_smelly(
            smelly_jar=SMELLY_CFG["jar"],
            evosuite_runtime_jar=SMELLY_CFG["evosuite_runtime_jar"],
            junit_jar=SMELLY_CFG["junit_jar"],
            source_path=tmp_root.parent,
            test_path=tmp_root.parent,
            output_dir=smelly_out_dir,
            output_name=out_name,
            detectors=0, mode=0, sufix=" ",
            resume_analisis=False,
            timeout_sec=1800,
        )
        return load_smelly_json(out_path)
    except Exception as e:
        print(f"[{project_name}] Smelly-E re-run failed: {e}")
        traceback.print_exc()
        return None


def _log(fh, po: PlanOutcome) -> None:
    fh.write(json.dumps({
        "project": po.project, "class": po.class_key, "method": po.test_method,
        "smell": po.smell_id, "n_plans": po.n_plans, "accepted": po.accepted,
        "stage": po.stage, "reason": po.reason,
    }, ensure_ascii=False) + "\n")
    fh.flush()


# ----------------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------------


def _format_delta(before: Dict[str, int], after: Dict[str, int]) -> List[str]:
    lines: List[str] = []
    all_keys = sorted(set(before) | set(after))
    for k in all_keys:
        b = before.get(k, 0)
        a = after.get(k, 0)
        if b == 0 and a == 0:
            continue
        pct = (a - b) / b * 100.0 if b else 0.0
        lines.append(f"    {k:<55} {b:>4} → {a:<4}  ({'+'  if pct >= 0 else ''}{pct:.1f}%)")
    return lines


def _print_project_report(r: ProjectReport) -> None:
    print(f"\n## Project: {r.project}   (elapsed {r.elapsed_sec:.1f}s)")
    # Plan generation
    plans_by_smell: Dict[str, int] = {}
    for po in r.plan_outcomes:
        plans_by_smell[po.smell_id] = plans_by_smell.get(po.smell_id, 0) + 1
    print("  ### Plan Generation (plan-groups, = number of calls to get_tier1_plan)")
    for sid in ["NNA", "TSES", "DS", "AC"]:
        print(f"    {sid:<6} {plans_by_smell.get(sid, 0):>4} plan-groups")
    print(f"    Total: {sum(plans_by_smell.values())}")

    # Stage breakdown
    print("  ### Outcomes by stage")
    stage_counts: Dict[str, int] = {}
    for po in r.plan_outcomes:
        stage_counts[po.stage] = stage_counts.get(po.stage, 0) + 1
    for stage, n in sorted(stage_counts.items(), key=lambda x: -x[1]):
        print(f"    {stage:<20} {n}")

    n_accepted = sum(1 for po in r.plan_outcomes if po.accepted)
    n_total = len(r.plan_outcomes)
    rate = (n_accepted / n_total * 100.0) if n_total else 0.0
    print(f"  ### Final accepted: {n_accepted}/{n_total} ({rate:.1f}%)")

    # Per-smell acceptance
    print("  ### Acceptance by smell (plan-groups)")
    for sid in ["NNA", "TSES", "DS", "AC"]:
        tot = sum(1 for po in r.plan_outcomes if po.smell_id == sid)
        ok = sum(1 for po in r.plan_outcomes if po.smell_id == sid and po.accepted)
        r_pct = (ok / tot * 100.0) if tot else 0.0
        print(f"    {sid:<6} {ok:>4}/{tot:<4} ({r_pct:5.1f}%)")

    # Smelly-E diff
    print("  ### Smelly-E Re-detection")
    if not r.after_counts:
        print("    (after-counts unavailable — re-run failed)")
    else:
        for line in _format_delta(r.before_counts, r.after_counts):
            print(line)

    # Class-level test pass delta
    print("  ### Side effects (class-level JUnit pass)")
    cls_total = len(r.class_tests_before)
    if cls_total:
        before_pass = sum(1 for v in r.class_tests_before.values() if v)
        after_pass = sum(1 for v in r.class_tests_after.values() if v)
        print(f"    Before: {before_pass}/{cls_total}  After: {after_pass}/{cls_total}")
        regressed = [c for c in r.class_tests_after
                     if r.class_tests_before.get(c) and not r.class_tests_after[c]]
        if regressed:
            print(f"    Regressed classes ({len(regressed)}): {regressed[:8]}")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", nargs="+", default=["1_tullibee"])
    ap.add_argument("--sf110-root", default=str(REPO_ROOT.parent / "sf110_projects"))
    ap.add_argument("--workdir", default=str(REPO_ROOT / "output" / "e2e_tier1_workdir"))
    ap.add_argument("--log", default=str(REPO_ROOT / "output" / "e2e_tier1.jsonl"))
    args = ap.parse_args()

    workdir = Path(args.workdir)
    sf110_root = Path(args.sf110_root)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    reports: List[ProjectReport] = []
    with log_path.open("w", encoding="utf-8") as log_fh:
        for proj in args.projects:
            pdir = sf110_root / proj
            smelly_json = REPO_ROOT / "output" / "by_project" / proj / f"smelly_{proj}.json"
            if not pdir.exists() or not smelly_json.exists():
                print(f"[skip] {proj}: missing project or smelly json")
                continue
            r = process_project(pdir, smelly_json, workdir, log_fh)
            reports.append(r)
            _print_project_report(r)

    # overall summary
    print("\n\n=== Overall Summary ===")
    total_plans = sum(len(r.plan_outcomes) for r in reports)
    total_accepted = sum(sum(1 for po in r.plan_outcomes if po.accepted) for r in reports)
    print(f"Total plan-groups: {total_plans}")
    print(f"Accepted: {total_accepted} ({(total_accepted/total_plans*100.0) if total_plans else 0.0:.1f}%)")

    print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

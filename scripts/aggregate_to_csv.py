#!/usr/bin/env python3
"""Build every aggregate CSV for the SE-GTR replication package.

Run: python3 build_aggregates.py
Reads from the llm_smelly_repair_impl/ tree; writes into ../02_.../aggregate,
../03_.../aggregate, ../04_.../aggregate, ../05_.../aggregate.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path

SRC = Path("<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl")
PKG = Path("<ANON_ROOT>/segtr_replication/replication_package")

PHASE4 = SRC / "output/runs/phase4_main"
PHASE4_PIT = PHASE4 / "pit"
RQ3 = SRC / "output/runs/rq3_experiments"
ABL = RQ3 / "ablation"
UTREF = RQ3 / "utrefactor/run15"
NAIVE = RQ3 / "naive_llm"
PHASE_E = RQ3 / "pit"

DEV = {"1_tullibee", "29_apbsmem", "71_ext4j", "88_jopenchart", "31_xisemele"}

BAND_T1 = 15.0   # low   : pristine <15%
BAND_T2 = 25.0   # mod   : 15-25%; strong >=25%

SELECTED_15 = [
    "3_gaj", "4_rif", "11_imsmart", "14_omjstate", "90_dcparseargs",
    "7_sfmis", "8_gfarcegestionfa", "12_dsachat", "42_asphodel", "60_sugar",
    "2_a4j", "41_follow", "54_db-everywhere", "63_objectexplorer", "68_biblestudy",
]

# ---------- helpers ---------------------------------------------------------

def write_csv(path: Path, header_comments: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        for c in header_comments:
            f.write(f"# {c}\n")
        w = csv.writer(f, lineterminator="\n")
        for r in rows:
            w.writerow(r)


def read_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def float_or(v, default=None):
    if v in (None, "", "—"):
        return default
    try:
        return float(v)
    except Exception:
        return default


def fmt(v, nd=4):
    if v is None:
        return ""
    return f"{v:.{nd}f}"


# ---------- phase 4: 02_phase4_segtr_full/aggregate -------------------------

def build_phase4_aggregates() -> None:
    out = PKG / "02_phase4_segtr_full/aggregate"
    out.mkdir(parents=True, exist_ok=True)

    # --- smell_reduction_81.csv ------------------------------------------------
    src = PHASE4 / "aggregation/tables/table1_heldout_smell_reduction.csv"
    rows = list(csv.reader(open(src, encoding="utf-8")))
    write_csv(
        out / "smell_reduction_81.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Figure 3 (per-smell Δ% on 81 held-out projects)",
            "built by: _build/build_aggregates.py",
            "rows: 13 smell types (SMELL_ORDER)",
        ],
        rows,
    )

    # --- smell_reduction_86_all_completed.csv ---------------------------------
    # Parallel 86-project variant per user D.1. Union of heldout (table1) + dev (table2).
    ho = {r["smell"]: r for r in read_rows(PHASE4 / "aggregation/tables/table1_heldout_smell_reduction.csv")}
    dv = {r["smell"]: r for r in read_rows(PHASE4 / "aggregation/tables/table2_dev_smell_reduction.csv")}
    header = ["smell", "cohort", "before_total", "after_total", "delta_count", "delta_pct", "projects_improved", "projects_regressed"]
    merged: list[list] = [header]
    for sm in ho.keys():
        h, d = ho[sm], dv.get(sm, {})
        bt = int(h["before_total"]) + int(d.get("before_total", 0))
        at = int(h["after_total"]) + int(d.get("after_total", 0))
        delta = at - bt
        delta_pct = (delta / bt * 100) if bt else 0.0
        # Projects improved/regressed simply sum since dev (5) and heldout (81) are disjoint
        pi = int(h["projects_improved"]) + int(d.get("projects_improved", 0))
        pr = int(h["projects_regressed"]) + int(d.get("projects_regressed", 0))
        merged.append([sm, "all_completed", bt, at, delta, f"{delta_pct:+.1f}%", pi, pr])
    write_csv(
        out / "smell_reduction_86_all_completed.csv",
        [
            f"source: {(PHASE4 / 'aggregation/tables/table1_heldout_smell_reduction.csv').relative_to(SRC)}"
            f" + {(PHASE4 / 'aggregation/tables/table2_dev_smell_reduction.csv').relative_to(SRC)}",
            "paper reference: cohort=all_completed variant of Figure 3 (union of 81 heldout + 5 dev)",
            "built by: _build/build_aggregates.py",
            "rows: 13 smell types",
        ],
        merged,
    )

    # --- preservation_79coverage.csv (81-project coverage Δ) -----------------
    # Read per-project jacoco from per_project.json for each heldout project
    per_rows = [["project", "bin", "jacoco_line_before", "jacoco_line_after", "delta_line_pp",
                 "jacoco_branch_before", "jacoco_branch_after", "delta_branch_pp",
                 "class_tests_before", "class_tests_after", "class_regressions"]]
    # Get bin by reading selected_15 for those 15 else read from raw per_project.json
    phase4_projects = []
    for d in sorted((PHASE4).iterdir()):
        if not (d.is_dir() and d.name.startswith("project_")):
            continue
        proj = d.name[len("project_"):]
        pj = d / "per_project.json"
        if not pj.exists():
            continue
        if proj in DEV:
            continue
        phase4_projects.append((proj, pj))

    n_with_cov = 0
    coverage_rows = [per_rows[0]]
    for proj, pj in phase4_projects:
        data = json.load(open(pj))
        row = data[0] if isinstance(data, list) else data
        jb = row.get("jacoco_before", {}) or {}
        ja = row.get("jacoco_after", {}) or {}
        lb = jb.get("line_coverage")
        la = ja.get("line_coverage")
        bb = jb.get("branch_coverage")
        ba = ja.get("branch_coverage")
        tb = row.get("class_tests_before", "")
        ta = row.get("class_tests_after", "")
        reg = len(row.get("regressed_classes", []) or [])
        dl = (la - lb) * 100 if (lb is not None and la is not None) else None
        db = (ba - bb) * 100 if (bb is not None and ba is not None) else None
        if dl is not None:
            n_with_cov += 1
        coverage_rows.append([
            proj, "", fmt(lb, 4), fmt(la, 4), fmt(dl, 4) if dl is not None else "",
            fmt(bb, 4), fmt(ba, 4), fmt(db, 4) if db is not None else "",
            tb, ta, reg,
        ])
    # Compute aggregate summary row
    deltas = [float(r[4]) for r in coverage_rows[1:] if r[4] != ""]
    mean_d = sum(deltas) / len(deltas) if deltas else 0.0
    # Append an AGGREGATE row
    coverage_rows.append([
        "__AGGREGATE__", f"n={len(deltas)}", "", "", f"{mean_d:+.4f}",
        "", "", "", "", "", f"total_class_regressions={sum(int(r[-1]) for r in coverage_rows[1:-0])}",
    ])
    write_csv(
        out / "preservation_79coverage.csv",
        [
            f"source: {(PHASE4).relative_to(SRC)}/project_<N>/per_project.json (81 heldout projects)",
            "paper reference: §5 RQ2 — coverage mean Δ = +0.107 pp (n=79)",
            "built by: _build/build_aggregates.py",
            "rows: 81 heldout projects + aggregate row; 'delta_line_pp' blank when JaCoCo unavailable",
        ],
        coverage_rows,
    )

    # --- preservation_58mutation.csv -----------------------------------------
    pit_rows = read_rows(PHASE4_PIT / "per_project_pit_final.csv")
    mut_rows = [
        ["project", "category", "pristine_mutants", "pristine_killed", "pristine_score_pct",
         "v2_after_mutants", "v2_after_killed", "v2_after_score_pct", "delta_pp", "status", "band"]
    ]
    successes = []
    for r in pit_rows:
        ps = float_or(r["pristine_score_pct"])
        vs = float_or(r["v2_after_score_pct"])
        dp = float_or(r["delta_pp"])
        band = ""
        if ps is not None and vs is not None:
            if ps < BAND_T1:
                band = "low"
            elif ps < BAND_T2:
                band = "moderate"
            else:
                band = "strong"
            successes.append((r["project"], ps, vs, dp, band))
        mut_rows.append([
            r["project"], r.get("category", ""),
            r.get("pristine_mutants", ""), r.get("pristine_killed", ""), r.get("pristine_score_pct", ""),
            r.get("v2_after_mutants", ""), r.get("v2_after_killed", ""), r.get("v2_after_score_pct", ""),
            r.get("delta_pp", ""), r.get("status", ""), band,
        ])
    agg_n = len(successes)
    agg_mean = sum(r[3] for r in successes) / agg_n if agg_n else 0
    mut_rows.append(["__AGGREGATE__", f"n={agg_n}", "", "", "", "", "", "", f"{agg_mean:+.4f}", "", ""])
    write_csv(
        out / "preservation_58mutation.csv",
        [
            f"source: {(PHASE4_PIT / 'per_project_pit_final.csv').relative_to(SRC)}",
            "paper reference: §5 RQ2 — mutation mean Δ = +0.869 pp (n=58), across all Phase-4 completed projects with valid pristine + v2-after PIT",
            "built by: _build/build_aggregates.py",
            "rows: all Phase-4 projects (with PIT status); 'band' filled only for the 58 successes (low<15%, moderate 15–25%, strong ≥25%)",
        ],
        mut_rows,
    )

    # --- mutation_by_band.csv -------------------------------------------------
    bands_agg: dict[str, list[float]] = defaultdict(list)
    for _, ps, _, dp, band in successes:
        if dp is not None:
            bands_agg[band].append(dp)
    band_rows = [["band", "pristine_score_range", "n", "mean_delta_pp", "median_delta_pp"]]
    for band_name, rng in [("low", "<15%"), ("moderate", "15%–25%"), ("strong", "≥25%")]:
        vs = sorted(bands_agg[band_name])
        n = len(vs)
        mean = sum(vs) / n if n else 0
        median = vs[n // 2] if n else 0
        band_rows.append([band_name, rng, n, f"{mean:+.4f}", f"{median:+.4f}"])
    total = [v for vs in bands_agg.values() for v in vs]
    tm = sum(total) / len(total) if total else 0
    ts = sorted(total)
    tmed = ts[len(ts) // 2] if ts else 0
    band_rows.append(["OVERALL", "", len(total), f"{tm:+.4f}", f"{tmed:+.4f}"])
    write_csv(
        out / "mutation_by_band.csv",
        [
            f"source: derived from {(PHASE4_PIT / 'per_project_pit_final.csv').relative_to(SRC)}",
            "paper reference: §5 RQ2 band breakdown — low=+0.26, moderate=+1.16, strong=+0.95 pp",
            "built by: _build/build_aggregates.py",
            "rows: 3 bands + OVERALL; bands by pristine PIT score (low <15%, moderate 15–25%, strong ≥25%)",
        ],
        band_rows,
    )

    print(f"[phase4] wrote 4 aggregate CSVs to {out.relative_to(PKG)}")


# ---------- 03_baselines_3way/aggregate -------------------------------------

def build_baseline_aggregates() -> None:
    out = PKG / "03_baselines_3way/aggregate"
    out.mkdir(parents=True, exist_ok=True)

    # --- baseline_smell_comparison.csv (Figure 4 source) ---------------------
    src = UTREF / "three_way_comparison.csv"
    rows = list(csv.reader(open(src, encoding="utf-8")))
    write_csv(
        out / "baseline_smell_comparison.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Figure 4 (3-way smell-improvement comparison on 15-project cohort)",
            "built by: _build/build_aggregates.py",
            "rows: 15 (selected_15.csv order)",
        ],
        rows,
    )

    # --- baseline_preservation.csv (Table III) -------------------------------
    # Build from three_way_comparison + Phase-E PIT + per_project jacoco
    twc = {r["project"]: r for r in read_rows(src)}
    pit = {r["project"]: r for r in read_rows(PHASE_E / "per_project_pit.csv")}
    hdr = ["project", "bin", "full_delta_line_pp", "full_delta_mutation_pp", "full_regressions",
           "naive_delta_line_pp", "naive_delta_mutation_pp", "naive_regressions",
           "utref_delta_line_pp", "utref_delta_mutation_pp", "utref_regressions"]
    preserve_rows = [hdr]
    for p in SELECTED_15:
        t = twc.get(p, {})
        pp = pit.get(p, {})
        preserve_rows.append([
            p, t.get("bin", ""),
            t.get("full_delta_line_pp", ""), pp.get("full_delta_pp", ""), t.get("full_regressed", ""),
            t.get("naive_delta_line_pp", ""), pp.get("naive_delta_pp", ""), t.get("naive_regressed", ""),
            t.get("utref_delta_line_pp", ""), pp.get("utref_delta_pp", ""), t.get("utref_regressed", ""),
        ])
    # Aggregate row
    def mean_col(col_idx: int) -> float:
        vs = [float(r[col_idx]) for r in preserve_rows[1:] if r[col_idx] not in ("", "—")]
        return sum(vs) / len(vs) if vs else 0.0
    agg = ["__AGGREGATE__", "n=15"]
    for col in range(2, len(hdr)):
        vs = [float(r[col]) for r in preserve_rows[1:] if r[col] not in ("", "—")]
        if vs and isinstance(vs[0], float):
            agg.append(f"{sum(vs)/len(vs):+.4f}")
        else:
            agg.append("")
    preserve_rows.append(agg)
    write_csv(
        out / "baseline_preservation.csv",
        [
            f"source: {src.relative_to(SRC)} + {(PHASE_E / 'per_project_pit.csv').relative_to(SRC)}",
            "paper reference: Table III (3-way coverage/mutation/regressions on 15-project cohort)",
            "built by: _build/build_aggregates.py",
            "rows: 15 (selected_15.csv order) + __AGGREGATE__ row",
            "note: utref_regressions column reports the RAW count (total 8: 6 in 7_sfmis, 2 in 41_follow).",
            "  The paper's Table III reports UTRef=0 under the convention that un-compilable UTRef output",
            "  cannot regress tests (since tests never ran). See utref_compile_errors.csv for compile outcomes.",
        ],
        preserve_rows,
    )

    # --- class_regressions_detail.csv ---------------------------------------
    # For each of the 15 projects + each condition, walk per_project.json and pull regressed_classes
    regr_rows = [["project", "condition", "regressed_class", "tests_before", "tests_after"]]
    condition_paths = [
        ("full", PHASE4, "project_"),
        ("naive", NAIVE, "project_"),
        ("utrefactor", UTREF, "project_"),
    ]
    for p in SELECTED_15:
        for cond, base, prefix in condition_paths:
            pj = base / f"{prefix}{p}" / "per_project.json"
            if not pj.exists():
                continue
            try:
                d = json.load(open(pj))
            except Exception:
                continue
            row = d[0] if isinstance(d, list) else d
            for rc in row.get("regressed_classes", []) or []:
                if isinstance(rc, dict):
                    regr_rows.append([p, cond, rc.get("class", str(rc)),
                                       rc.get("before", ""), rc.get("after", "")])
                else:
                    regr_rows.append([p, cond, str(rc), "", ""])
    write_csv(
        out / "class_regressions_detail.csv",
        [
            "source: per_project.json[regressed_classes] for each condition × 15-project cohort",
            "paper reference: Table III — Full=0, Naive=25 class regressions",
            "built by: _build/build_aggregates.py",
            f"rows: one per regressed class per condition (total = {len(regr_rows)-1})",
            "note on UTRef: the raw per_project.json shows 8 regressed classes under UTRef (6 in 7_sfmis, 2 in 41_follow).",
            "  These are derived from per-class smell-substitution accounting on UTRef's output, which did not compile.",
            "  The paper reports UTRef=0 regressions under the convention 'un-compilable output cannot regress tests'.",
            "  Both numbers are preserved: the raw 8 appears here; the paper-convention 0 is in baseline_preservation.csv.",
        ],
        regr_rows,
    )

    # --- utref_compile_errors.csv -------------------------------------------
    # Status + reduction come from per_project_utrefactor.csv; delta_line "—" indicates
    # uncompilable output (JaCoCo could not measure). PIT status from per_project_pit.csv.
    utref_agg = {r["project"]: r for r in read_rows(UTREF / "per_project_utrefactor.csv")}
    pit_3way = {r["project"]: r for r in read_rows(PHASE_E / "per_project_pit.csv")}
    utref_rows = [["project", "bin", "status", "orch_elapsed_min", "smell_reduction_pct",
                    "delta_line_pp", "regressed_classes", "pit_status", "compile_outcome", "first_error_sig"]]
    for p in SELECTED_15:
        r = utref_agg.get(p, {})
        pp = pit_3way.get(p, {})
        status = r.get("status", "missing")
        dl = r.get("delta_line_pp", "")
        pit_st = pp.get("utref_status", "")
        # Classify:
        #   timeout            → UTRef tool timed out; no output produced
        #   completed + dl=""  → UTRef produced output but it did not compile (JaCoCo couldn't run)
        #   completed + dl!="" → UTRef output compiled (rare in this cohort)
        if status == "timeout":
            outcome = "timeout_incomplete"
        elif dl in ("", "—"):
            outcome = "output_did_not_compile"
        else:
            outcome = "compiled"
        first_err = ""
        log = UTREF / f"project_{p}" / "post_hoc.log"
        if log.exists():
            with open(log, errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if "error:" in s or ".java:" in s and "error" in s.lower():
                        first_err = s[:200]
                        break
        utref_rows.append([
            p, r.get("bin", ""), status, r.get("orch_elapsed_min", ""),
            r.get("smell_reduction_pct", ""), dl, r.get("regressed_classes", ""),
            pit_st, outcome, first_err,
        ])
    # Summary row
    from collections import Counter
    cnt = Counter(r[8] for r in utref_rows[1:])
    utref_rows.append(["__SUMMARY__",
                        "",
                        f"completed={cnt.get('output_did_not_compile',0)+cnt.get('compiled',0)} timeout={cnt.get('timeout_incomplete',0)}",
                        "", "", "", "",
                        f"pit_success=0/15",
                        f"output_did_not_compile={cnt.get('output_did_not_compile',0)} timeout_incomplete={cnt.get('timeout_incomplete',0)} compiled={cnt.get('compiled',0)}",
                        ""])
    write_csv(
        out / "utref_compile_errors.csv",
        [
            f"source: {(UTREF / 'per_project_utrefactor.csv').relative_to(SRC)} + {(PHASE_E / 'per_project_pit.csv').relative_to(SRC)} + per-project post_hoc.log",
            "paper reference: Table III — UTRef 12 compile-fail + 3 timeout-incomplete = 0 PIT successes",
            "built by: _build/build_aggregates.py",
            "rows: 15 (selected_15.csv order) + __SUMMARY__",
        ],
        utref_rows,
    )
    print(f"[baselines] wrote 4 aggregate CSVs to {out.relative_to(PKG)}")


# ---------- 04_ablation/aggregate -------------------------------------------

def build_ablation_aggregates() -> None:
    out = PKG / "04_ablation/aggregate"
    out.mkdir(parents=True, exist_ok=True)

    # --- tier_cumulative.csv (Figure 5 source) -------------------------------
    src = ABL / "table_D1_per_condition_aggregate.csv"
    rows = list(csv.reader(open(src, encoding="utf-8")))
    write_csv(
        out / "tier_cumulative.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Figure 5 (cumulative tier contribution — T1=4.92%, T1+T2=8.11%, T1–T3=13.20%, Full=13.78%)",
            "built by: _build/build_aggregates.py",
            "rows: 4 conditions (t1_only, t1_t2, t1_t2_t3, full) × 15 projects each",
        ],
        rows,
    )

    # --- per_smell_by_tier.csv (Table IV source) -----------------------------
    src = ABL / "table_D2_per_smell_by_condition.csv"
    rows = list(csv.reader(open(src, encoding="utf-8")))
    write_csv(
        out / "per_smell_by_tier.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Table IV (per-smell per-tier counts, 15-project cohort)",
            "built by: _build/build_aggregates.py",
            "rows: 14 smell names (Smelly-E long-form) × baseline+3 conditions+full",
        ],
        rows,
    )

    # --- regressions_by_condition.csv (zero-regression backup) --------------
    src = ABL / "per_project_ablation.csv"
    pp = read_rows(src)
    regr = [["condition", "n_projects", "sum_regressed_classes", "max_regressed_classes", "projects_with_any_regression"]]
    by_cond: dict[str, list[int]] = defaultdict(list)
    for r in pp:
        cond = r["condition"]
        reg = int(r.get("regressed_classes", "0") or 0)
        by_cond[cond].append(reg)
    for cond in ["t1_only", "t1_t2", "t1_t2_t3", "full"]:
        vs = by_cond.get(cond, [])
        n = len(vs)
        s = sum(vs)
        mx = max(vs) if vs else 0
        any_ = sum(1 for v in vs if v > 0)
        regr.append([cond, n, s, mx, any_])
    write_csv(
        out / "regressions_by_condition.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Figure 5 text — all 4 conditions had zero regressions",
            "built by: _build/build_aggregates.py",
            "rows: 4 conditions",
        ],
        regr,
    )

    # --- tier_incremental.csv (companion to Figure 5, Δ per step) ------------
    src = ABL / "table_D3_tier_incremental.csv"
    rows = list(csv.reader(open(src, encoding="utf-8")))
    write_csv(
        out / "tier_incremental.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Figure 5 (per-step Δ)",
            "built by: _build/build_aggregates.py",
            "rows: 4 step transitions",
        ],
        rows,
    )
    print(f"[ablation] wrote 4 aggregate CSVs to {out.relative_to(PKG)}")


# ---------- 05_phase_e_pit/aggregate ---------------------------------------

def build_phase_e_aggregates() -> None:
    out = PKG / "05_phase_e_pit/aggregate"
    out.mkdir(parents=True, exist_ok=True)
    src = PHASE_E / "table_E1_3way.csv"
    rows = list(csv.reader(open(src, encoding="utf-8")))
    write_csv(
        out / "pit_3way.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Table III mutation-Δ row (3-way PIT)",
            "built by: _build/build_aggregates.py",
            "rows: 3 conditions (full / naive / utref)",
        ],
        rows,
    )

    # Also copy the per-project 3-way PIT with a header
    src = PHASE_E / "per_project_pit.csv"
    rows = list(csv.reader(open(src, encoding="utf-8")))
    write_csv(
        out / "pit_3way_per_project.csv",
        [
            f"source: {src.relative_to(SRC)}",
            "paper reference: Table III mutation-Δ — per-project detail",
            "built by: _build/build_aggregates.py",
            "rows: 15 projects",
        ],
        rows,
    )
    print(f"[phase_e] wrote 2 aggregate CSVs to {out.relative_to(PKG)}")


def main() -> None:
    build_phase4_aggregates()
    build_baseline_aggregates()
    build_ablation_aggregates()
    build_phase_e_aggregates()
    print("All aggregates built.")


if __name__ == "__main__":
    main()

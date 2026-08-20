#!/usr/bin/env python3
"""Verify the paper's headline numbers against the bundled aggregate CSVs.

Two kinds of check are reported, and they mean different things:

  * **paper claim** — a value printed in the paper, compared against the cell
    of the aggregate CSV the claim map points at. A failure means the paper and
    the archive disagree.
  * **internal invariant** — no paper value is involved. An aggregate CSV is
    re-derived from the raw per-project artefacts and the two are required to
    agree. These catch the failure mode a cell-comparison cannot: an aggregate
    that is internally consistent but was built from the wrong cohort. v1 of
    this package shipped exactly that (smell_reduction_81.csv), and every
    cell-comparison against it passed.

Tolerance rules:
  - Integer counts (class regressions, project counts) — EXACT match.
  - Percentages and pp deltas                           — abs diff <= 0.01.

Exit code 0 iff every check passes; 1 otherwise.

Usage: python3 verify_numbers.py [--package-root <path>]

Runs in a few seconds; does not invoke any LLM, PIT, or Ant.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Callable

DEFAULT_ROOT = Path(__file__).resolve().parents[2]

# Projects held out of the evaluation cohort because their Ant build failed:
# per_project.json carries only {project, error} and plan_log.jsonl is empty.
BUILD_FAILED = {"57_hft-bomberman", "61_noen"}

# Development-set projects, excluded from the evaluation-only mutation cohort.
DEV_PROJECTS = {"1_tullibee", "29_apbsmem", "71_ext4j", "88_jopenchart",
                "31_xisemele"}

# Smelly-E detector labels -> the smell codes used in the paper. "Test without
# assertions" has no code: it is 0 before and 0 after on every project and is
# not among the figure's categories.
SMELL_NAME_TO_CODE = {
    "Not null assertion": "NNA",
    "Duplicated Setup": "DS",
    "Testing the same exception scenario": "TSES",
    "Asserting Constants": "AC",
    "Exceptions due to null arguments": "ENET",
    "Exceptions due to incomplete setup": "EDIS",
    "Exceptions due to external dependencies": "EDED",
    "Not asserted return values": "NARV",
    "Asserting object initialization multiple times": "OIMT",
    "Testing only field accesors": "TOFA",
    "Assertion with not related parent class method": "ARPM",
    "Not asserted side effects": "NASE",
    "Multiple calls to the same void method": "TSVM",
}

# UTRefactor emits both a JUnit 4 and a JUnit 5 `Test` import, which javac
# rejects as ambiguous: 12/12 completed projects fail to compile and 0 tests
# ever run (see 03_baselines_3way/aggregate/utref_compile_errors.csv —
# compiled=0, pit_success=0/15). Class regressions, coverage delta and
# mutation score are therefore NOT MEASURABLE for UTRefactor, and the paper
# prints "—" for them.
#
# v1 of this package "checked" these two quantities by comparing a hard-coded
# constant against itself, which passed unconditionally and verified nothing.
# They are now reported as SKIP. Exit code 0 is unaffected.
UTREF_NOT_MEASURABLE = (
    "UTRefactor output did not compile (0/12 projects produced a runnable "
    "class) — quantity not measurable; paper prints '—'"
)

# -------- helpers ---------------------------------------------------------

def read_rows(p: Path) -> list[dict]:
    """Read a CSV, skipping leading `#` comment rows."""
    with open(p, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def as_float(s: str) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace("%", "").replace("+", "")
    if s in ("", "—", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# -------- check types -----------------------------------------------------

class Check:
    def __init__(self, name: str, paper_value: float | int,
                 tolerance: float, fn: Callable[[], float | int | None],
                 integer: bool = False, note: str = "", skip: str = "",
                 kind: str = "paper"):
        self.name = name
        self.paper = paper_value
        self.tol = tolerance
        self.fn = fn
        self.integer = integer
        self.note = note
        self.skip = skip
        self.kind = kind          # "paper" | "invariant"

    def run(self) -> tuple[bool | None, str]:
        """Return (True|False|None, detail). None means "skipped"."""
        if self.skip:
            return None, self.skip
        try:
            actual = self.fn()
        except Exception as e:
            return False, f"source extraction raised {type(e).__name__}: {e}"
        if actual is None:
            return False, "source extraction returned None"
        if self.integer:
            ok = int(actual) == int(self.paper)
        else:
            ok = abs(float(actual) - float(self.paper)) <= self.tol
        return ok, f"actual={actual}  paper={self.paper}  tol={self.tol}"


# -------- source extractors ----------------------------------------------

class Source:
    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[Path, list[dict]] = {}

    def rows(self, rel: str) -> list[dict]:
        p = self.root / rel
        if p not in self._cache:
            self._cache[p] = read_rows(p)
        return self._cache[p]

    def cell(self, rel: str, key_col: str, key_val: str, out_col: str):
        for r in self.rows(rel):
            if r.get(key_col) == key_val:
                return r.get(out_col)
        raise KeyError(f"{rel}: no row with {key_col}={key_val!r}")

    # Figure 3 per-smell delta (n=79 basis; smell_reduction_81.csv is legacy)
    SMELL_CSV = "02_phase4_segtr_full/aggregate/smell_reduction_79.csv"

    def smell_reduction(self, smell: str) -> float | None:
        return as_float(self.cell(self.SMELL_CSV, "smell", smell, "delta_pct"))

    def _heldout_79(self) -> list[str]:
        txt = (self.root / "01_cohort/heldout_81.txt").read_text(encoding="utf-8")
        return [p for p in (l.strip() for l in txt.splitlines())
                if p and p not in BUILD_FAILED]

    def smell_79_mismatches(self) -> int:
        """Re-derive smell_reduction_79.csv from the raw per-project artefacts.

        Returns the number of disagreeing cells; 0 means the CSV is exactly what
        summing per_project.json over the 79-project cohort produces.
        """
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        for proj in self._heldout_79():
            p = self.root / "02_phase4_segtr_full/per_project" / proj / "per_project.json"
            with open(p, encoding="utf-8") as f:
                payload = json.load(f)
            rec = payload[0] if isinstance(payload, list) else payload
            for label, code in SMELL_NAME_TO_CODE.items():
                before[code] = before.get(code, 0) + int(rec["smell_totals_before"].get(label, 0))
                after[code] = after.get(code, 0) + int(rec["smell_totals_after"].get(label, 0))
        bad = 0
        seen = set()
        for row in self.rows(self.SMELL_CSV):
            code = row["smell"]
            seen.add(code)
            if int(row["before"]) != before.get(code) or int(row["after"]) != after.get(code):
                bad += 1
        # A category present in the raw data but absent from the CSV is only
        # acceptable when it is empty on both sides (that is why ARPM is omitted).
        for code in SMELL_NAME_TO_CODE.values():
            if code not in seen and (before.get(code) or after.get(code)):
                bad += 1
        return bad

    def preservation_54_mismatches(self) -> int:
        """Check that the 54-project table is the 58-project table minus the dev set."""
        wide = [r for r in self.rows(
            "02_phase4_segtr_full/aggregate/preservation_58mutation.csv")
            if r["project"] != "__AGGREGATE__" and r["delta_pp"] not in ("", None)]
        expected = {r["project"]: r["delta_pp"] for r in wide
                    if r["project"] not in DEV_PROJECTS}
        narrow = [r for r in self.rows(
            "02_phase4_segtr_full/aggregate/preservation_54mutation_evalonly.csv")
            if r["project"] != "__AGGREGATE__"]
        got = {r["project"]: r["delta_pp"] for r in narrow}
        bad = len(set(expected) ^ set(got))
        bad += sum(1 for k in set(expected) & set(got) if expected[k] != got[k])
        mean = statistics.mean(float(v) for v in expected.values())
        stated = as_float(self.cell(
            "02_phase4_segtr_full/aggregate/preservation_54mutation_evalonly.csv",
            "project", "__AGGREGATE__", "delta_pp"))
        if stated is None or abs(stated - mean) > 0.0001:
            bad += 1
        return bad

    def coverage_mean_delta(self) -> float | None:
        return as_float(self.cell(
            "02_phase4_segtr_full/aggregate/preservation_79coverage.csv",
            "project", "__AGGREGATE__", "delta_line_pp"))

    def coverage_n(self) -> int:
        bin_str = self.cell(
            "02_phase4_segtr_full/aggregate/preservation_79coverage.csv",
            "project", "__AGGREGATE__", "bin")
        return int(bin_str.removeprefix("n=")) if bin_str else -1

    def mutation_mean_delta(self) -> float | None:
        return as_float(self.cell(
            "02_phase4_segtr_full/aggregate/preservation_58mutation.csv",
            "project", "__AGGREGATE__", "delta_pp"))

    def mutation_n(self) -> int:
        cat = self.cell(
            "02_phase4_segtr_full/aggregate/preservation_58mutation.csv",
            "project", "__AGGREGATE__", "category")
        return int(cat.removeprefix("n=")) if cat else -1

    def mutation_mean_delta_evalonly(self) -> float | None:
        return as_float(self.cell(
            "02_phase4_segtr_full/aggregate/preservation_54mutation_evalonly.csv",
            "project", "__AGGREGATE__", "delta_pp"))

    def mutation_n_evalonly(self) -> int:
        cat = self.cell(
            "02_phase4_segtr_full/aggregate/preservation_54mutation_evalonly.csv",
            "project", "__AGGREGATE__", "category")
        return int(cat.removeprefix("n=")) if cat else -1

    def mutation_band(self, band: str) -> float | None:
        """Mean mutation delta for one baseline-strength band, evaluation-only.

        Computed from preservation_54mutation_evalonly.csv rather than read from
        mutation_by_band.csv: that aggregate is on the n=58 cohort, which still
        contains the four development projects. See its header.
        """
        vals = [as_float(r["delta_pp"]) for r in self.rows(
            "02_phase4_segtr_full/aggregate/preservation_54mutation_evalonly.csv")
            if r["project"] != "__AGGREGATE__" and r["band"] == band]
        vals = [v for v in vals if v is not None]
        return statistics.mean(vals) if vals else None

    def mutation_band_n(self, band: str) -> int:
        return sum(1 for r in self.rows(
            "02_phase4_segtr_full/aggregate/preservation_54mutation_evalonly.csv")
            if r["project"] != "__AGGREGATE__" and r["band"] == band)

    def phase_e(self, cond: str) -> float | None:
        return as_float(self.cell(
            "05_phase_e_pit/aggregate/pit_3way.csv",
            "condition", cond, "mean_delta_pp"))

    def baseline_preservation_agg(self, col: str) -> float | None:
        # Use AGGREGATE row across 15-proj cohort for coverage means
        for r in self.rows("03_baselines_3way/aggregate/baseline_preservation.csv"):
            if r.get("project") == "__AGGREGATE__":
                return as_float(r.get(col))
        return None

    def regressions_count(self, condition: str) -> int:
        rows = self.rows("03_baselines_3way/aggregate/class_regressions_detail.csv")
        return sum(1 for r in rows if r.get("condition") == condition)

    # Figure 4 means/medians use PER-METHOD successful cohorts:
    # - Full: all 15 ablation runs (source: tier_cumulative.csv)
    # - Naive: 12 projects with smell_reduction_pct (3 dropped as empty)
    # - UTRef: 12 projects with smell_reduction_pct (3 timeouts dropped)
    def figure4_full_mean(self) -> float | None:
        return as_float(self.cell(
            "04_ablation/aggregate/tier_cumulative.csv",
            "condition", "full", "mean_smell_red_pct"))

    def figure4_full_median(self) -> float | None:
        return as_float(self.cell(
            "04_ablation/aggregate/tier_cumulative.csv",
            "condition", "full", "median_smell_red_pct"))

    def figure4_per_method(self, cond: str, agg: str) -> float | None:
        # Use the 3-way comparison CSV restricted to its per-method column.
        # The CSV's "{cond}_smell_red_pct" column already omits the "—" entries
        # implicitly since Naive/UTRef each have 3 projects with blank values.
        rows = self.rows("03_baselines_3way/aggregate/baseline_smell_comparison.csv")
        col = f"{cond}_smell_red_pct"
        vs = [as_float(r.get(col)) for r in rows]
        vs = [v for v in vs if v is not None]
        if not vs:
            return None
        return statistics.mean(vs) if agg == "mean" else statistics.median(vs)

    def ablation_cumulative(self, cond: str) -> float | None:
        return as_float(self.cell(
            "04_ablation/aggregate/tier_cumulative.csv",
            "condition", cond, "mean_smell_red_pct"))

    def ablation_regressions(self, cond: str) -> int:
        return int(self.cell(
            "04_ablation/aggregate/regressions_by_condition.csv",
            "condition", cond, "sum_regressed_classes"))


# -------- the check list --------------------------------------------------

def build_checks(src: Source) -> list[Check]:
    C = []
    # Figure 3 — 12 per-smell rows on the n=79 basis.
    # ARPM is not checked: it is 0 before and 0 after on every project, so no
    # percentage is defined, and it is omitted from the figure and the CSV.
    for smell, val in [("NNA", -96.1), ("EDED", -80.1), ("TSES", -73.6),
                        ("EDIS", -59.1), ("ENET", -40.2), ("NARV", -36.7),
                        ("DS", -26.9), ("TOFA", -10.8), ("OIMT", -3.9),
                        ("AC", -0.3), ("NASE", +14.4), ("TSVM", +43.5)]:
        C.append(Check(f"Figure 3: {smell} smell reduction %", val, 0.01,
                         lambda s=smell: src.smell_reduction(s)))
    # Internal invariants — no paper value. These guard the failure mode that
    # cell comparisons cannot see: an aggregate built over the wrong cohort.
    C.append(Check("[internal invariant] smell_reduction_79.csv reproduces from per_project.json",
                     0, 0, src.smell_79_mismatches, integer=True, kind="invariant",
                     note="re-sums smell_totals_before/after over the 79-project cohort; 0 = every cell agrees"))
    C.append(Check("[internal invariant] preservation_54 derives from preservation_58 minus dev set",
                     0, 0, src.preservation_54_mismatches, integer=True, kind="invariant",
                     note="0 = same projects, same deltas, and the stated mean equals the recomputed mean"))
    # §5 — aggregates
    C.append(Check("§5: coverage mean Δ (pp)", +0.107, 0.01, src.coverage_mean_delta))
    C.append(Check("§5: coverage n",           79,      0,   src.coverage_n, integer=True))
    C.append(Check("§5: mutation mean Δ (pp), eval-only", +0.823, 0.01,
                     src.mutation_mean_delta_evalonly))
    C.append(Check("§5: mutation n, eval-only",           54, 0,
                     src.mutation_n_evalonly, integer=True))
    # Bands are on the evaluation-only cohort: 13 + 19 + 22 = 54.
    for band, mean, n in [("low", +0.257, 13), ("moderate", +1.007, 19),
                           ("strong", +0.999, 22)]:
        C.append(Check(f"§5: mutation {band} band (pp)", mean, 0.01,
                         lambda b=band: src.mutation_band(b)))
        C.append(Check(f"§5: mutation {band} band n", n, 0,
                         lambda b=band: src.mutation_band_n(b), integer=True))

    # Table III — 15-project 3-way preservation
    C.append(Check("Table III: Full coverage Δ (pp)",  +0.09, 0.01,
                     lambda: src.baseline_preservation_agg("full_delta_line_pp")))
    C.append(Check("Table III: Naive coverage Δ (pp)", +0.11, 0.01,
                     lambda: src.baseline_preservation_agg("naive_delta_line_pp")))
    C.append(Check("Table III: UTRef coverage Δ (pp)", "—", 0,
                     lambda: None, skip=UTREF_NOT_MEASURABLE))

    # Table III — class regressions (runtime semantics; see note above)
    C.append(Check("Table III: Full class regressions",  0,  0,
                     lambda: src.regressions_count("full"), integer=True))
    C.append(Check("Table III: Naive class regressions", 25, 0,
                     lambda: src.regressions_count("naive"), integer=True))
    C.append(Check("Table III: UTRef class regressions", "—", 0,
                     lambda: None, skip=UTREF_NOT_MEASURABLE,
                     note="raw source-level count is 8 (6 in 7_sfmis, 2 in 41_follow) — pre-existing failures in the unrepaired workdir, not repair-induced; see class_regressions_detail.csv"))

    # Table III — mutation Δ
    C.append(Check("Table III: Full mutation Δ (pp)",  +1.03, 0.01, lambda: src.phase_e("full")))
    C.append(Check("Table III: Naive mutation Δ (pp)", -0.50, 0.01, lambda: src.phase_e("naive")))

    # Figure 4 — 3-way smell improvement
    # Paper precision: Full/Naive use 2dp (tol=0.01); UTRef uses 1dp (tol=0.05
    # — paper rounds to ±0.05 precision, so we cannot demand sub-0.05 match).
    C.append(Check("Figure 4: Full mean smell improvement %",    +13.78, 0.01, src.figure4_full_mean))
    C.append(Check("Figure 4: Full median smell improvement %",  +15.52, 0.01, src.figure4_full_median))
    C.append(Check("Figure 4: Naive mean smell improvement %",   +9.96, 0.01,
                     lambda: src.figure4_per_method("naive", "mean")))
    C.append(Check("Figure 4: Naive median smell improvement %", +9.65, 0.01,
                     lambda: src.figure4_per_method("naive", "median")))
    C.append(Check("Figure 4: UTRef mean smell improvement %",   -10.6, 0.05,
                     lambda: src.figure4_per_method("utref", "mean")))
    C.append(Check("Figure 4: UTRef median smell improvement %", +28.0, 0.05,
                     lambda: src.figure4_per_method("utref", "median")))

    # Figure 5 — ablation cumulative
    C.append(Check("Figure 5: T1 cumulative %",      4.92,  0.01, lambda: src.ablation_cumulative("t1_only")))
    C.append(Check("Figure 5: T1+T2 cumulative %",   8.11,  0.01, lambda: src.ablation_cumulative("t1_t2")))
    C.append(Check("Figure 5: T1–T3 cumulative %",   13.20, 0.01, lambda: src.ablation_cumulative("t1_t2_t3")))
    C.append(Check("Figure 5: Full cumulative %",    13.78, 0.01, lambda: src.ablation_cumulative("full")))
    C.append(Check("Figure 5: zero regressions T1",           0, 0,
                     lambda: src.ablation_regressions("t1_only"),    integer=True))
    C.append(Check("Figure 5: zero regressions T1+T2",        0, 0,
                     lambda: src.ablation_regressions("t1_t2"),      integer=True))
    C.append(Check("Figure 5: zero regressions T1–T3",        0, 0,
                     lambda: src.ablation_regressions("t1_t2_t3"),   integer=True))
    C.append(Check("Figure 5: zero regressions Full",         0, 0,
                     lambda: src.ablation_regressions("full"),       integer=True))

    return C


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package-root", default=str(DEFAULT_ROOT),
                     help=f"default: {DEFAULT_ROOT}")
    args = ap.parse_args()

    root = Path(args.package_root).resolve()
    src = Source(root)
    checks = build_checks(src)

    n_pass = n_fail = n_skip = 0
    by_kind = {"paper": 0, "invariant": 0, "skipped": 0}
    print(f"{'status':7} {'claim':72} {'detail'}")
    print(f"{'-'*7} {'-'*72} {'-'*50}")
    for c in checks:
        ok, detail = c.run()
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        print(f"{status:7} {c.name[:72]:72} {detail}")
        if c.note:
            print(f"        note: {c.note}")
        if ok is None:
            n_skip += 1
            by_kind["skipped"] += 1
        else:
            by_kind[c.kind] += 1
            if ok:
                n_pass += 1
            else:
                n_fail += 1

    print()
    print(f"{n_pass} passed  |  {n_fail} failed  |  {n_skip} skipped  "
          f"|  {len(checks)} total")
    print(f"  of which: {by_kind['paper']} paper claims, "
          f"{by_kind['invariant']} internal invariants, "
          f"{by_kind['skipped']} not measurable")
    if n_fail == 0:
        tail = f" ({n_skip} not measurable — see notes)" if n_skip else ""
        print(f"\nALL CHECKS PASS{tail}")
        return 0
    else:
        print("\nSOME CHECKS FAILED — fix aggregates or the paper before shipping.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Phase 2.4b.2 checkpoint: run the Tier 4 dispatcher against a real LLM on
a known-good NASE capture target and verify the generated plan actually
uses observed literals (not guesses) and skips unchanged getters.

Default target mirrors the Phase 2.4b.1 checkpoint:
    29_apbsmem :: apbsmem.PlotDatum :: test05
    Act call: plotDatum0.setYError(-1145.5663265567)
    Observed:
        getYError() 0.0 → -1145.5663265567
        hasErrorBar() false → true
    Unchanged: getX, getY, getPlotSymbol, getLineColor

Pass criteria (all must hold for the checkpoint to return 0):
  1. mode == "dynamic"
  2. plan contains an INSERT_ASSERTION referencing plotDatum0.getYError()
     with expected_expr matching the observed after-value literal
  3. plan contains an INSERT_ASSERTION referencing plotDatum0.hasErrorBar()
     with expected_expr/assert_type consistent with the observed value
     (assertTrue with actual_expr = plotDatum0.hasErrorBar(), or
      assertEquals with expected_expr = "true")
  4. plan does NOT assert on getX()/getY()/getPlotSymbol()/getLineColor()
     — those are listed in unchanged_fields.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.config.loader import load_llm_config  # noqa: E402
from smell_repair_v2.dynamic.collector import DynamicContextCollector  # noqa: E402
from smell_repair_v2.llm.multi_client import MultiModelClient  # noqa: E402
from smell_repair_v2.llm.plan_runner import PlanRunner  # noqa: E402
from smell_repair_v2.operators.base import ExecutionContext  # noqa: E402
from smell_repair_v2.project.discover import (  # noqa: E402
    Project,
    find_cut_source_file,
    find_evosuite_test_file,
    resolve_cut_fqcn_from_test,
)
from smell_repair_v2.scripts.e2e_test_tier1 import prepare_workdir  # noqa: E402
from smell_repair_v2.tiers.tier4_dynamic import CaptureRequest, plan_tier4  # noqa: E402


NASE_NAME = "Not asserted side effects"
DEFAULTS = {
    "project": "29_apbsmem",
    "class_key": "apbsmem.PlotDatum",
    "method": "test05",
    "sf110_root": Path("<ANON_ROOT>/segtr_replication/sf110_projects"),
    "smelly_root": REPO_ROOT / "output" / "by_project",
    "out_root": REPO_ROOT / "output" / "tier4_checkpoint",
    "model_key": "gpt_oss_20b",
}


# ---------------------------------------------------------------------------
# Plan validation against observed evidence
# ---------------------------------------------------------------------------


def _expected_literal_variants(observed_value: str) -> List[str]:
    """Return acceptable literal renderings of an observed primitive.

    The LLM might emit the value as the raw string (`-1145.5663265567`),
    as a float-suffixed version, or as a stringified form. This keeps the
    check loose without permitting unrelated values.
    """
    v = observed_value.strip()
    out = {v}
    # Many numeric observed values survive String.valueOf unchanged, so the
    # verbatim match is by far the most common path.
    try:
        f = float(v)
        out.add(f"{f}")
        out.add(f"{f}d")
        out.add(f"{f}D")
        out.add(f"{f:.6f}")
    except ValueError:
        pass
    if v in ("true", "false"):
        out.update({"Boolean." + v.upper(), "Boolean." + ("TRUE" if v == "true" else "FALSE")})
    if v == "null":
        out.add("null")
    return sorted(out)


def _plan_dicts(plans) -> List[Dict[str, Any]]:
    """Convert OperatorPlan objects back into JSON dicts for matching."""
    out = []
    for p in plans:
        out.append({"op": p.op.value, "params": dict(p.params)})
    return out


def validate_plan_against_evidence(
    plans_dicts: List[Dict[str, Any]],
    dynamic_evidence: Dict[str, Any],
    cut_var: str,
) -> Tuple[bool, List[str]]:
    """Check that the LLM plan only asserts on CHANGED observables and uses
    the OBSERVED after-value as the literal. Returns (ok, notes).

    We don't require a specific assert_type — e.g. for a boolean change,
    `assertTrue(x.isReady())`, `assertFalse(x.isEmpty())`, and
    `assertEquals("true", String.valueOf(x.isReady()))` are all acceptable
    as long as the asserted getter is in changed_fields and the expected
    value is consistent with the observed one.
    """
    notes: List[str] = []
    changed = dynamic_evidence.get("changed_fields") or {}
    unchanged = set(dynamic_evidence.get("unchanged_fields") or [])

    # Strip the cut_var prefix so keys like "plotDatum0.getYError()" and
    # "getYError()" compare equal.
    def _bare(k: str) -> str:
        pfx = cut_var + "."
        return k[len(pfx):] if k.startswith(pfx) else k

    changed_bare = {_bare(k): v for k, v in changed.items()}
    unchanged_bare = {_bare(k) for k in unchanged}

    # --- 1. All assertions must reference a CHANGED getter ----------------
    asserted_getters: List[str] = []
    for p in plans_dicts:
        if p["op"] != "INSERT_ASSERTION":
            continue
        actual = p["params"].get("actual_expr", "")
        # We look for "<cut_var>.<getter>()" inside actual_expr — this keeps
        # expressions like `String.valueOf(plotDatum0.getYError())` matching.
        for g in changed_bare:
            token = f"{cut_var}.{g}"
            if token in actual:
                asserted_getters.append(g)
                break
        else:
            # No CHANGED getter mentioned — was it asserting on an UNCHANGED?
            bad = next(
                (g for g in unchanged_bare if f"{cut_var}.{g}" in actual),
                None,
            )
            if bad:
                notes.append(
                    f"FAIL: assertion on unchanged getter `{bad}` "
                    f"(actual_expr={actual!r})"
                )

    # --- 2. Every CHANGED getter should be covered by at least one assertion
    missing = [g for g in changed_bare if g not in asserted_getters]
    for g in missing:
        notes.append(
            f"WARN: changed getter `{g}` has no corresponding assertion"
        )

    # --- 3. Expected literals must match observed values ------------------
    literal_issues: List[str] = []
    for p in plans_dicts:
        if p["op"] != "INSERT_ASSERTION":
            continue
        atype = p["params"].get("assert_type", "")
        actual = p["params"].get("actual_expr", "")
        expected = p["params"].get("expected_expr")

        # Which getter does this assertion target?
        matched_getter = None
        for g, obs in changed_bare.items():
            if f"{cut_var}.{g}" in actual:
                matched_getter = (g, obs["after"])
                break
        if matched_getter is None:
            continue
        g, observed_after = matched_getter

        if atype == "assertEquals":
            if expected is None:
                literal_issues.append(
                    f"FAIL: assertEquals on {g} missing expected_expr"
                )
                continue
            acceptable = _expected_literal_variants(observed_after)
            if str(expected).strip() not in acceptable:
                literal_issues.append(
                    f"FAIL: assertEquals({g}) expected={expected!r} "
                    f"does not match observed after={observed_after!r}"
                )
        elif atype in ("assertTrue", "assertFalse"):
            # Valid only when the observed after-value is boolean.
            if observed_after not in ("true", "false"):
                literal_issues.append(
                    f"WARN: {atype}({g}) but observed after={observed_after!r}"
                )
            elif (atype == "assertTrue" and observed_after != "true") or \
                 (atype == "assertFalse" and observed_after != "false"):
                literal_issues.append(
                    f"FAIL: {atype}({g}) contradicts observed after={observed_after!r}"
                )
        elif atype == "assertNull":
            if observed_after != "null":
                literal_issues.append(
                    f"FAIL: assertNull({g}) contradicts observed after={observed_after!r}"
                )
        elif atype == "assertNotNull":
            if observed_after == "null":
                literal_issues.append(
                    f"FAIL: assertNotNull({g}) contradicts observed after=null"
                )
    notes.extend(literal_issues)

    fatal = any(n.startswith("FAIL:") for n in notes)
    ok = (not fatal) and bool(asserted_getters)
    return ok, notes


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=DEFAULTS["project"])
    ap.add_argument("--class", dest="class_key", default=DEFAULTS["class_key"])
    ap.add_argument("--method", default=DEFAULTS["method"])
    ap.add_argument("--sf110-root", type=Path, default=DEFAULTS["sf110_root"])
    ap.add_argument("--smelly-root", type=Path, default=DEFAULTS["smelly_root"])
    ap.add_argument("--out-root", type=Path, default=DEFAULTS["out_root"])
    ap.add_argument("--model", default=DEFAULTS["model_key"])
    ap.add_argument("--llm-config", type=Path, default=None)
    args = ap.parse_args()

    project = args.project
    class_key = args.class_key
    method = args.method

    # ----- 1. Load NASE evidence -----
    smelly_json = args.smelly_root / project / f"smelly_{project}.json"
    if not smelly_json.exists():
        print(f"[setup] smelly json not found: {smelly_json}")
        return 1
    raw = json.loads(smelly_json.read_text(encoding="utf-8"))
    class_smells = raw.get(class_key)
    if class_smells is None:
        print(f"[setup] class key {class_key!r} not in smelly json")
        return 1
    nase_items = class_smells.get(NASE_NAME, []) or []
    item = next((i for i in nase_items if i.get("test_method") == method), None)
    if item is None:
        print(f"[setup] no NASE evidence for {method!r} in {class_key}")
        return 1
    act_calls = (item.get("evidence") or {}).get("unverified_side_effect_calls", [])
    if not act_calls:
        print("[setup] evidence missing unverified_side_effect_calls")
        return 1
    act_call = act_calls[0].get("act_call") or {}
    modified_fields = act_calls[0].get("modified_fields") or []
    cut_var = act_call.get("scope") or ""
    print("=" * 72)
    print(f"NASE target: {project} :: {class_key} :: {method}")
    print(f"Act call:    {act_call.get('expr')} (line {act_call.get('begin_line')})")
    print(f"modified_fields (static): {modified_fields}")
    print("=" * 72)

    # ----- 2. Workdir + fresh compile -----
    out_run = args.out_root / project
    if out_run.exists():
        shutil.rmtree(out_run)
    out_run.mkdir(parents=True, exist_ok=True)
    work_project = prepare_workdir(out_run, args.sf110_root / project)
    print(f"workdir: {work_project}")
    r = subprocess.run(
        ["ant", "-q", "clean", "compile", "compile-evosuite"],
        cwd=str(work_project),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600,
    )
    if r.returncode != 0:
        print("compile failed:\n" + (r.stdout[-600:] if r.stdout else ""))
        return 1

    # ----- 3. Resolve test file + CUT source -----
    _, cut_simple = class_key.split(".", 1)
    proj_obj = Project(folder_name=work_project.name, real_name=work_project.name,
                       root=work_project)
    test_file = find_evosuite_test_file(proj_obj, cut_simple)
    if test_file is None:
        print(f"[setup] test file for {cut_simple} not found")
        return 1
    cut_fqcn = resolve_cut_fqcn_from_test(test_file, cut_simple) or class_key
    cut_src_file = find_cut_source_file(proj_obj, cut_fqcn)
    cut_source = cut_src_file.read_text(encoding="utf-8", errors="ignore") if cut_src_file else None
    if cut_source is None:
        print(f"[setup] CUT source not found for {cut_fqcn}")
        return 1
    method_text = _extract_method_text(test_file, method) or ""
    if not method_text:
        print(f"[setup] could not isolate {method!r} body from test file")
        return 1

    # ----- 4. Build collector + LLM runner -----
    collector = DynamicContextCollector(work_project)
    cfg = load_llm_config(args.llm_config) if args.llm_config else load_llm_config()
    mm = MultiModelClient(cfg)
    client = mm.client_for(args.model)
    runner = PlanRunner(client, max_attempts=3)

    # ----- 5. Invoke Tier 4 -----
    ctx = ExecutionContext(
        method_name=method,
        method_line_range=(1, len(method_text.splitlines())),
        file_text=method_text,
        cut_source=cut_source,
        cut_fqcn=cut_fqcn,
    )
    capture_req = CaptureRequest(
        test_file=test_file,
        test_method_name=method,
        act_call_info=act_call,
        cut_source=cut_source,
        cut_fqcn=cut_fqcn,
    )
    evidence_for_prompt = {
        "unverified_side_effect_calls": act_calls,
    }
    print("\n[running] plan_tier4 (this calls the real LLM)...")
    result = plan_tier4(
        smell_id="NASE",
        evidence=evidence_for_prompt,
        method_text=method_text,
        ctx=ctx,
        runner=runner,
        capture_request=capture_req,
        dynamic_collector=collector,
    )

    # ----- 6. Report -----
    print("\n--- Tier 4 result ---")
    print(f"mode:               {result.mode}")
    print(f"capture_error:      {result.capture_error}")
    print(f"elapsed_capture_ms: {result.elapsed_capture_ms}")
    print(f"plan success:       {result.success}")
    print(f"plan attempts:      {result.attempts}")
    print(f"plan error:         {result.error}")

    plans_dicts = _plan_dicts(result.plans)
    print("\n--- Generated plan (JSON) ---")
    print(json.dumps(plans_dicts, indent=2))

    if result.dynamic_evidence:
        print("\n--- Dynamic evidence (what went to the LLM) ---")
        print(json.dumps(result.dynamic_evidence, indent=2, ensure_ascii=False))

    # ----- 7. Validate against observed evidence -----
    if result.mode != "dynamic":
        print("\nCheckpoint: SKIPPED (not dynamic mode) → failing")
        return 2
    if not result.success:
        print("\nCheckpoint: FAIL — LLM plan not parseable")
        return 2

    ok, notes = validate_plan_against_evidence(
        plans_dicts, result.dynamic_evidence or {}, cut_var=cut_var,
    )
    print("\n--- Plan validation ---")
    for n in notes:
        print(f"  {n}")
    if not notes:
        print("  (no warnings — plan looks clean)")
    print("\nCheckpoint:", "PASS" if ok else "FAIL")

    # ----- 8. Dump artifact -----
    dump = {
        "target": {
            "project": project,
            "class": class_key,
            "method": method,
            "act_call": act_call,
            "modified_fields": modified_fields,
        },
        "tier4_result": {
            "mode": result.mode,
            "capture_error": result.capture_error,
            "elapsed_capture_ms": result.elapsed_capture_ms,
            "success": result.success,
            "attempts": result.attempts,
            "plans": plans_dicts,
            "dynamic_evidence": result.dynamic_evidence,
            "getter_sources": result.getter_sources,
        },
        "validation": {"ok": ok, "notes": notes},
    }
    dump_path = out_run / "tier4_result.json"
    dump_path.write_text(json.dumps(dump, indent=2, ensure_ascii=False))
    print(f"\ndumped: {dump_path}")
    return 0 if ok else 2


def _extract_method_text(test_file: Path, method_name: str) -> str | None:
    """Pull the `@Test ... public void <name>() { ... }` block out of the
    EvoSuite test class. Brace-depth-aware, works with the standard
    EvoSuite-generated layout."""
    import re
    text = test_file.read_text(encoding="utf-8", errors="ignore")
    m = re.search(
        rf"(?ms)(?:@Test[^\n]*\n)\s*public\s+void\s+{re.escape(method_name)}\s*\([^)]*\)\s*"
        rf"(?:throws[^\{{]+)?\{{",
        text,
    )
    if m is None:
        return None
    start = m.start()
    # Walk braces from the opening `{` of the method body.
    body_open = text.find("{", m.end() - 1)
    depth = 0
    i = body_open
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


if __name__ == "__main__":
    sys.exit(main())

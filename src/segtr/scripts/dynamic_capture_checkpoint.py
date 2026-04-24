#!/usr/bin/env python3
"""Phase 2.4b.1 checkpoint: run DynamicContextCollector end-to-end on a
real NASE evidence item from a dev-set project and print the resulting
``DynamicEvidence`` so we can judge whether the captured state is rich
enough to drive a Tier 4 LLM prompt.

Usage (defaults to 29_apbsmem / XSymbol / test0):
  python -m smell_repair_v2.scripts.dynamic_capture_checkpoint
  python -m smell_repair_v2.scripts.dynamic_capture_checkpoint \\
      --project 1_tullibee --class tullibee.EClientSocket --method test30

The script isolates the project under ``output/dynamic_checkpoint/<proj>/``
(reuses ``prepare_workdir``) so the sf110 source tree stays clean.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.analysis.smelly import load_smelly_json  # noqa: E402
from smell_repair_v2.dynamic.collector import DynamicContextCollector  # noqa: E402
from smell_repair_v2.project.discover import (  # noqa: E402
    Project,
    find_cut_source_file,
    find_evosuite_test_file,
    resolve_cut_fqcn_from_test,
)
from smell_repair_v2.scripts.e2e_test_tier1 import prepare_workdir  # noqa: E402


NASE_NAME = "Not asserted side effects"
DEFAULTS = {
    "project": "29_apbsmem",
    "class_key": "apbsmem.XSymbol",
    "method": "test0",
    "sf110_root": Path("<ANON_ROOT>/segtr_replication/sf110_projects"),
    "smelly_root": REPO_ROOT / "output" / "by_project",
    "out_root": REPO_ROOT / "output" / "dynamic_checkpoint",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=DEFAULTS["project"])
    ap.add_argument("--class", dest="class_key", default=DEFAULTS["class_key"])
    ap.add_argument("--method", default=DEFAULTS["method"])
    ap.add_argument("--sf110-root", type=Path, default=DEFAULTS["sf110_root"])
    ap.add_argument("--smelly-root", type=Path, default=DEFAULTS["smelly_root"])
    ap.add_argument("--out-root", type=Path, default=DEFAULTS["out_root"])
    args = ap.parse_args()

    project = args.project
    class_key = args.class_key
    method = args.method

    # 1. Load NASE evidence for the target (class, method)
    smelly_json = args.smelly_root / project / f"smelly_{project}.json"
    if not smelly_json.exists():
        print(f"[setup] smelly json not found: {smelly_json}")
        return 1
    raw = json.loads(smelly_json.read_text(encoding="utf-8"))
    class_smells = raw.get(class_key)
    if class_smells is None:
        print(f"[setup] class key {class_key!r} not in smelly json")
        print(f"        available sample: {list(raw)[:6]}")
        return 1
    nase_items = class_smells.get(NASE_NAME, []) or []
    # Pick the evidence item for the requested test method.
    item = None
    for it in nase_items:
        if it.get("test_method") == method:
            item = it
            break
    if item is None:
        print(f"[setup] no NASE evidence for method {method!r} in class {class_key!r}")
        print(f"        NASE methods in this class: "
              f"{sorted({i.get('test_method') for i in nase_items})}")
        return 1

    act_calls = (item.get("evidence") or {}).get("unverified_side_effect_calls", [])
    if not act_calls:
        print("[setup] evidence missing unverified_side_effect_calls")
        return 1
    act_call = act_calls[0].get("act_call") or {}
    print(f"=== NASE target ===\n"
          f"project: {project}\nclass:   {class_key}\nmethod:  {method}\n"
          f"act_call: {act_call.get('expr')} (line {act_call.get('begin_line')})")

    # 2. Isolated workdir + fresh compile
    out_run = args.out_root / project
    if out_run.exists():
        shutil.rmtree(out_run)
    out_run.mkdir(parents=True, exist_ok=True)
    work_project = prepare_workdir(out_run, args.sf110_root / project)
    print(f"\nworkdir: {work_project}")
    print("running `ant clean compile compile-evosuite`...")
    r = subprocess.run(
        ["ant", "-q", "clean", "compile", "compile-evosuite"],
        cwd=str(work_project),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600,
    )
    if r.returncode != 0:
        print("compile failed:")
        print(r.stdout[-600:])
        return 1

    # 3. Locate test file + CUT source
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
    print(f"test_file: {test_file}")
    print(f"cut_file:  {cut_src_file}")

    # 4. Run collector
    collector = DynamicContextCollector(work_project)
    print("\n=== invoking DynamicContextCollector.collect() ===")
    ev = collector.collect(
        test_file=test_file,
        test_method_name=method,
        act_call_info=act_call,
        cut_source=cut_source,
        cut_fqcn=cut_fqcn,
    )

    # 5. Report
    print("\n=== DynamicEvidence ===")
    print(f"capture_success: {ev.capture_success}")
    print(f"error:           {ev.error}")
    print(f"act_call_line:   {ev.act_call_line}")
    print(f"elapsed_ms:      {ev.elapsed_ms}")
    print(f"captured_getters ({len(ev.captured_getters)}): {ev.captured_getters}")
    print("\n-- state_before --")
    for k, v in ev.state_before.items():
        print(f"  {k} = {v}")
    print("\n-- state_after --")
    for k, v in ev.state_after.items():
        print(f"  {k} = {v}")
    print("\n-- changed fields (before ≠ after) --")
    for k, (b, a) in ev.changed_fields().items():
        print(f"  {k}: {b!r}  →  {a!r}")
    print("\n-- stdout tail --")
    print(ev.stdout_tail)

    # Write the full evidence as JSON for later reference.
    dump_path = out_run / "dynamic_evidence.json"
    dump_path.write_text(json.dumps(ev.to_dict(), indent=2, ensure_ascii=False))
    print(f"\ndumped: {dump_path}")
    return 0 if ev.capture_success else 2


if __name__ == "__main__":
    sys.exit(main())

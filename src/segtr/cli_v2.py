#!/usr/bin/env python3
"""CLI entry point for the integrated v2 pipeline.

Usage:
  python -m smell_repair_v2.cli_v2 \\
      --projects 1_tullibee 29_apbsmem \\
      --enable-tier1 --enable-tier2 --enable-tier3 --enable-tier4 \\
      --enable-project-jacoco \\
      --out runs/phase2_4c_initial

Phase 2.4c onward: --enable-tier4 activates the dynamic-context handler and
--enable-project-jacoco runs project-level JaCoCo before/after the tier
pipeline so the summary can report a coverage delta.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.pipeline_v2 import PipelineV2Config, run_pipeline_v2


DEFAULT_SF110_ROOT = Path("<ANON_ROOT>/segtr_replication/sf110_projects")
DEFAULT_SMELLY_JSON_ROOT = REPO_ROOT / "smell_repair_v2" / ".."  # see below
DEFAULT_SMELLY_JSON_ROOT = REPO_ROOT / "output" / "by_project"
DEFAULT_OUT_ROOT = REPO_ROOT / "output" / "pipeline_v2_runs"


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=None,
                   help="Path to llm_config.yaml (default: config/llm_config.yaml)")
    p.add_argument("--sf110-root", type=Path, default=DEFAULT_SF110_ROOT)
    p.add_argument("--smelly-json-root", type=Path, default=DEFAULT_SMELLY_JSON_ROOT)
    p.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    # --out is a convenience alias the Phase 2.4c checkpoint command uses.
    # When set it takes precedence over --out-root+--run-name and points
    # directly at the final artefacts directory.
    p.add_argument("--out", dest="out_path", type=Path, default=None,
                   help="Explicit output directory (overrides --out-root/--run-name).")
    p.add_argument("--run-name", type=str, default=None)
    p.add_argument("--projects", nargs="+", required=True)
    p.add_argument("--model-key", default="gpt_oss_20b")
    p.add_argument(
        "--condition", default=None,
        choices=["full", "naive_llm", "utrefactor",
                 "t1_only", "t1_t2", "t1_t2_t3"],
        help=("RQ3 / ablation switch. When set, overrides --enable-tier{1..4} "
              "according to the selected condition. 'full' keeps the default "
              "behaviour; 'naive_llm' routes methods through the one-shot "
              "LLM-rewrite baseline (Gate 6/7 disabled)."),
    )
    p.add_argument(
        "--cost-budget", type=float, default=None,
        help=("Override the per-model cost budget (USD) from llm_config.yaml "
              "for this run. Useful to cap naive baseline smoke tests."),
    )
    p.add_argument("--enable-tier1", action="store_true")
    p.add_argument("--enable-tier2", action="store_true")
    p.add_argument("--enable-tier3", action="store_true")
    p.add_argument("--enable-tier4", action="store_true",
                   help="Enable the Tier 4 dynamic-context handler.")
    p.add_argument("--tier4-reasoning-effort", default=None,
                   choices=["low", "medium", "high"],
                   help="Per-call reasoning-effort override for Tier 4 LLM calls.")
    p.add_argument("--enable-project-jacoco", action="store_true",
                   help="Run project-level JaCoCo before/after the pipeline.")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--limit-methods-per-cell", type=int, default=None)
    p.add_argument("--no-smelly-after", action="store_true",
                   help="Skip the post-run Smelly-E rerun.")
    return p.parse_args()


def main() -> int:
    a = _parse()
    # --out collapses out_root + run_name into one explicit directory. We do
    # this by setting out_root=<parent> and run_name=<basename> so the rest
    # of the pipeline stays unchanged.
    if a.out_path is not None:
        out_path = Path(a.out_path).resolve()
        out_root = out_path.parent
        run_name = out_path.name
    else:
        out_root = a.out_root
        run_name = a.run_name
    # --cost-budget overrides the dev budget (llm_config.yaml) for this run.
    # We mutate the config at load time so the MultiModelClient sees the
    # adjusted cap; do not persist the change.
    llm_config_path = a.config
    if a.cost_budget is not None:
        import os
        import tempfile
        import yaml
        from smell_repair_v2.config.loader import _DEFAULT_CONFIG_PATH
        src = a.config or _DEFAULT_CONFIG_PATH
        with open(src, "r", encoding="utf-8") as f:
            cfg_dict = yaml.safe_load(f)
        cfg_dict.setdefault("dev_experiment", {})["cost_budget_per_model_usd"] = a.cost_budget
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="llm_config_override_", suffix=".yaml")
        os.close(tmp_fd)
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg_dict, f)
        llm_config_path = Path(tmp_path)

    cfg = PipelineV2Config(
        projects_root=a.sf110_root,
        smelly_json_root=a.smelly_json_root,
        out_root=out_root,
        projects=list(a.projects),
        model_key=a.model_key,
        enable_tier1=a.enable_tier1,
        enable_tier2=a.enable_tier2,
        enable_tier3=a.enable_tier3,
        enable_tier4=a.enable_tier4,
        enable_project_jacoco=a.enable_project_jacoco,
        tier4_reasoning_effort=a.tier4_reasoning_effort,
        max_attempts=a.max_attempts,
        limit_methods_per_cell=a.limit_methods_per_cell,
        run_smelly_after=not a.no_smelly_after,
        llm_config_path=llm_config_path,
        run_name=run_name,
        condition=a.condition or "full",
    )
    run_dir = run_pipeline_v2(cfg)
    print(f"\nartefacts: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

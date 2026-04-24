#!/usr/bin/env python3
"""Phase 4 parallel runner for SF110 main experiment.

Fires one ``cli_v2`` subprocess per project, pooled at ``N`` workers.
Each worker writes its artefacts to ``<out>/project_<name>/`` so the
processes are filesystem-isolated (separate workdirs, logs, smelly_after
dirs, JaCoCo outputs).

Design decisions:

- **Subprocess-level parallelism** — not Python threading or asyncio.
  Each worker is a fresh Python interpreter running ``cli_v2``. This
  guarantees that an Ant/JVM hang in one project cannot jam the others,
  and lets us rely on OS-level signal handling.
- **Checkpointing** — ``checkpoint.json`` (atomic write) is the single
  source of truth for "which projects are done". Resume starts by
  reading this file and filtering the project list.
- **Budget tracking** — per-project cost is scraped from the
  ``raw_results.jsonl`` that each subprocess produces. The runner sums
  the ``d_cost_usd`` values; when the total crosses the global cap it
  stops admitting new projects but lets in-flight ones finish.
- **Worker ramp-up** — starts at ``initial_workers``. After
  ``ramp_up_after`` clean completions it resizes the pool to
  ``max_workers``. (ProcessPoolExecutor can't resize live, so we spawn
  a second pool for the remainder.)
- **Interim summaries** — every ``interim_summary_every`` completions
  the runner writes ``interim_summary_<N>.md`` next to the checkpoint.
- **Signals** — SIGINT/SIGTERM set a stop flag. Running workers finish
  their current project; no new projects are admitted; checkpoint is
  flushed one last time.

Output layout::

    runs/phase4_main/
      ├── project_classification.csv   # from Step 1 (precondition)
      ├── checkpoint.json              # {"completed":[...], "failed":[...]}
      ├── failed_projects.json         # detailed failure records
      ├── cost_ledger.csv              # project → d_cost, elapsed_min
      ├── interim_summary_10.md
      ├── interim_summary_20.md
      ├── project_<name>/              # one per project (cli_v2 --out)
      │     ├── raw_results.jsonl
      │     ├── pipeline_summary.md
      │     ├── per_project.json
      │     └── smelly_after/after_<name>.json
      └── final_summary.md             # written when all done
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

TOTAL_PROJECTS = 94  # SF110 pipeline universe (A-exclude = 0)
STALL_HOURS = 3.0          # widened for 180-min B-2 retry batch
ALERT_COST_CAP_USD = 50.0  # B-2: use full budget cap, not intermediate $40
ALERT_TIMEOUT_COUNT = 3
ALERT_FAILED_PCT = 0.10
ALERT_RATE_LIMIT_CONSECUTIVE = 3
RAMP_MEM_PCT_CAP = 60.0
RAMP_LOAD_CAP = 12.0


REPO_ROOT = Path(__file__).resolve().parent.parent
SMELL_REPAIR = REPO_ROOT / "smell_repair_v2"


# ---------------------------------------------------------------------------
# Config + CSV plumbing
# ---------------------------------------------------------------------------


@dataclass
class Phase4Config:
    dev_projects: Set[str]
    dev_reuse: Dict[str, Path]
    initial_workers: int
    max_workers: int
    ramp_up_after: int
    total_budget_usd: float
    per_project_budget_usd: float
    timeout_per_project_min: int
    interim_every: int
    pipeline_args: Dict[str, Any]


def load_yaml(path: Path) -> Phase4Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    par = raw.get("parallel", {})
    bud = raw.get("budget", {})
    reuse_raw = raw.get("dev_reuse_sources", {}) or {}
    reuse = {k: (REPO_ROOT / v).resolve() for k, v in reuse_raw.items()}
    return Phase4Config(
        dev_projects=set(raw.get("dev_projects") or []),
        dev_reuse=reuse,
        initial_workers=int(par.get("initial_workers", 6)),
        max_workers=int(par.get("max_workers", 8)),
        ramp_up_after=int(par.get("ramp_up_after", 8)),
        total_budget_usd=float(bud.get("total_usd", 50.0)),
        per_project_budget_usd=float(bud.get("per_project_usd", 2.0)),
        timeout_per_project_min=int(bud.get("timeout_per_project_min", 90)),
        interim_every=int(raw.get("interim_summary_every", 10)),
        pipeline_args=raw.get("pipeline") or {},
    )


def load_project_list(csv_path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with csv_path.open(newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# Checkpoint (atomic JSON)
# ---------------------------------------------------------------------------


def load_checkpoint(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"completed": [], "failed": [], "started_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"completed": [], "failed": [], "started_at": None}


def save_checkpoint(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Per-project worker (runs in a child process)
# ---------------------------------------------------------------------------


def _run_cli_v2(
    project: str,
    out_dir: str,
    llm_config_path: str,
    pipeline_args: Dict[str, Any],
    timeout_sec: int,
    cost_budget_env: float,
) -> Dict[str, Any]:
    """Invoke ``cli_v2`` for one project. Returns a dict with rc + timing.

    This runs in a child Python process under the ProcessPoolExecutor; it
    MUST be picklable (module-level function, serializable kwargs only).
    """
    cmd = [
        sys.executable, "-m", "smell_repair_v2.cli_v2",
        "--config", llm_config_path,
        "--projects", project,
        "--out", out_dir,
    ]
    # Map yaml ``pipeline:`` flags onto cli_v2 args.
    if pipeline_args.get("enable_tier1"):
        cmd.append("--enable-tier1")
    if pipeline_args.get("enable_tier2"):
        cmd.append("--enable-tier2")
    if pipeline_args.get("enable_tier3"):
        cmd.append("--enable-tier3")
    if pipeline_args.get("enable_tier4"):
        cmd.append("--enable-tier4")
    if pipeline_args.get("enable_project_jacoco"):
        cmd.append("--enable-project-jacoco")
    if pipeline_args.get("reasoning_effort_tier4"):
        cmd.extend(["--tier4-reasoning-effort",
                    str(pipeline_args["reasoning_effort_tier4"])])
    if "max_attempts" in pipeline_args:
        cmd.extend(["--max-attempts", str(pipeline_args["max_attempts"])])
    if "model_key" in pipeline_args:
        cmd.extend(["--model-key", str(pipeline_args["model_key"])])
    if pipeline_args.get("run_smelly_after") is False:
        cmd.append("--no-smelly-after")

    env = os.environ.copy()
    env["SE_GTR_COST_BUDGET_USD"] = f"{cost_budget_env:.2f}"

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout_sec, env=env,
        )
        elapsed = time.monotonic() - t0
        return {
            "project": project,
            "rc": proc.returncode,
            "elapsed_sec": elapsed,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - t0
        return {
            "project": project,
            "rc": -1,
            "elapsed_sec": elapsed,
            "stdout_tail": (e.output or "")[-4000:] if isinstance(e.output, str) else "",
            "timed_out": True,
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {
            "project": project,
            "rc": -2,
            "elapsed_sec": elapsed,
            "stdout_tail": f"{type(e).__name__}: {e}",
            "timed_out": False,
        }


def compute_project_cost(project_out_dir: Path) -> float:
    """Sum d_cost_usd from the project's raw_results.jsonl."""
    jsonl = project_out_dir / "raw_results.jsonl"
    if not jsonl.exists():
        return 0.0
    total = 0.0
    try:
        with jsonl.open(encoding="utf-8") as f:
            for ln in f:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                v = rec.get("d_cost_usd")
                if isinstance(v, (int, float)):
                    total += float(v)
    except Exception:
        return 0.0
    return total


# ---------------------------------------------------------------------------
# Dev reuse
# ---------------------------------------------------------------------------


def reuse_dev_project(
    project: str,
    source_run_dir: Path,
    target_project_dir: Path,
) -> bool:
    """Copy the per-project artefacts from a prior run into the current
    run's layout. Returns True iff the copy produced everything we need."""
    if not source_run_dir.exists():
        return False
    target_project_dir.mkdir(parents=True, exist_ok=True)

    # 1. raw_results.jsonl — the phase 2.4c run puts all projects in one file;
    #    filter by project to keep per-project artifact clean.
    src_jsonl = source_run_dir / "raw_results.jsonl"
    if src_jsonl.exists():
        dst_jsonl = target_project_dir / "raw_results.jsonl"
        with src_jsonl.open(encoding="utf-8") as sf, \
                dst_jsonl.open("w", encoding="utf-8") as df:
            for ln in sf:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("project") == project:
                    df.write(ln)

    # 2. smelly_after/after_<project>.json
    src_smelly = source_run_dir / "smelly_after" / f"after_{project}.json"
    if src_smelly.exists():
        dst_smelly_dir = target_project_dir / "smelly_after"
        dst_smelly_dir.mkdir(exist_ok=True)
        shutil.copy2(src_smelly, dst_smelly_dir / f"after_{project}.json")

    # 3. per_project.json + pipeline_summary.md — synthesize a per-project
    #    entry from the prior run's aggregate summary.
    src_pp = source_run_dir / "per_project.json"
    if src_pp.exists():
        try:
            entries = json.loads(src_pp.read_text(encoding="utf-8"))
            match = next((e for e in entries if e.get("project") == project), None)
            if match is not None:
                (target_project_dir / "per_project.json").write_text(
                    json.dumps([match], indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            pass

    src_md = source_run_dir / "pipeline_summary.md"
    if src_md.exists():
        shutil.copy2(src_md, target_project_dir / "pipeline_summary_source.md")

    return (target_project_dir / "raw_results.jsonl").exists()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


STOP_FLAG = threading.Event()


def _install_signal_handlers() -> None:
    def _h(signum, frame):
        print(f"\n[runner] signal {signum} received — stopping gracefully", flush=True)
        STOP_FLAG.set()
    signal.signal(signal.SIGINT, _h)
    signal.signal(signal.SIGTERM, _h)


def admit_projects(
    cfg: Phase4Config,
    projects: List[str],
    completed: Set[str],
    failed: Set[str],
) -> List[str]:
    """Return the pending list (completed/failed filtered out), small
    projects first to front-load quick wins. Project 'size' is proxied by
    the first numeric prefix: 1_, 5_, 10_ etc — smaller project indices
    tend to be smaller SF110 projects in our empirical ordering."""
    pending = [p for p in projects if p not in completed and p not in failed]
    def _key(name: str) -> tuple:
        head = name.split("_", 1)[0]
        try:
            return (0, int(head))
        except ValueError:
            return (1, name)
    return sorted(pending, key=_key)


# ---------------------------------------------------------------------------
# Runtime metrics (enriched)
# ---------------------------------------------------------------------------


def _resource_snapshot(out_dir: Path) -> Dict[str, Any]:
    """Best-effort machine snapshot. Falls back to /proc parsing if
    psutil is unavailable."""
    out: Dict[str, Any] = {}
    # Load average — always available on Linux
    try:
        la1, la5, la15 = os.getloadavg()
        out["load_1min"] = la1
        out["load_5min"] = la5
    except (OSError, AttributeError):
        out["load_1min"] = 0.0
        out["load_5min"] = 0.0

    if _HAS_PSUTIL:
        vm = psutil.virtual_memory()
        out["mem_pct"] = vm.percent
        out["mem_used_gb"] = (vm.total - vm.available) / (1024 ** 3)
        out["mem_total_gb"] = vm.total / (1024 ** 3)
        out["cpu_pct_1sec"] = psutil.cpu_percent(interval=1.0)
        out["cpu_count"] = psutil.cpu_count()
    else:
        try:
            with open("/proc/meminfo") as f:
                lines = f.read().splitlines()
            mi = {k.strip(): int(v.split()[0]) for k, v in
                  (ln.split(":", 1) for ln in lines if ":" in ln)}
            total = mi.get("MemTotal", 1)
            avail = mi.get("MemAvailable", total)
            out["mem_pct"] = (total - avail) / total * 100.0
            out["mem_used_gb"] = (total - avail) / (1024 ** 2)
            out["mem_total_gb"] = total / (1024 ** 2)
        except Exception:
            out["mem_pct"] = 0.0
            out["mem_used_gb"] = 0.0
            out["mem_total_gb"] = 0.0
        out["cpu_pct_1sec"] = 0.0
        out["cpu_count"] = os.cpu_count() or 0

    try:
        stat = shutil.disk_usage(str(out_dir))
        out["disk_free_gb"] = stat.free / (1024 ** 3)
    except Exception:
        out["disk_free_gb"] = 0.0
    return out


def _aggregate_project_metrics(out_dir: Path, completed: List[str]) -> Dict[str, Any]:
    """Walk each completed project's raw_results.jsonl for Tier 4 and Gate
    stats. Dev-reuse projects have a filtered jsonl that still contains the
    full Tier 4 + validator events."""
    tier4_dynamic = 0
    tier4_static = 0
    tier4_attempts = 0
    dyn_success = 0
    gate_counts: Dict[str, int] = {}
    total_llm_cost = 0.0

    for proj in completed:
        jsonl = out_dir / f"project_{proj}" / "raw_results.jsonl"
        if not jsonl.exists():
            continue
        try:
            with jsonl.open(encoding="utf-8") as f:
                for ln in f:
                    try:
                        rec = json.loads(ln)
                    except Exception:
                        continue
                    # Tier 4 capture metrics
                    if rec.get("event") == "tier4_result":
                        tier4_attempts += 1
                        mode = rec.get("mode")
                        if mode == "dynamic":
                            tier4_dynamic += 1
                            dyn_success += 1
                        elif mode == "static_fallback":
                            tier4_static += 1
                    # Validator gate rejections
                    if rec.get("stage") == "validator" and not rec.get("final_accepted"):
                        reason = (rec.get("validator_reason") or "").split(":", 1)[0]
                        if reason:
                            gate_counts[reason] = gate_counts.get(reason, 0) + 1
                    # Per-call cost
                    v = rec.get("d_cost_usd")
                    if isinstance(v, (int, float)):
                        total_llm_cost += float(v)
        except Exception:
            continue

    return {
        "tier4_attempts": tier4_attempts,
        "tier4_dynamic": tier4_dynamic,
        "tier4_static": tier4_static,
        "tier4_dynamic_pct": (tier4_dynamic / tier4_attempts * 100.0) if tier4_attempts else 0.0,
        "gate_counts": gate_counts,
        "total_llm_cost": total_llm_cost,
    }


def _classify_failures(failed: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Group failed project names by reason bucket."""
    out: Dict[str, List[str]] = {
        "timeout": [], "rc_nonzero": [], "no_summary": [],
        "build_fail": [], "llm_fail": [], "other": [],
    }
    for f in failed:
        reason = (f.get("reason") or "other").lower()
        tail = (f.get("stdout_tail") or "").lower()
        if reason == "timeout":
            out["timeout"].append(f["project"])
        elif "build" in tail and "fail" in tail and "initial compile" in tail:
            out["build_fail"].append(f["project"])
        elif "openrouter" in tail or "rate" in tail and "limit" in tail:
            out["llm_fail"].append(f["project"])
        elif reason in out:
            out[reason].append(f["project"])
        else:
            out["other"].append(f["project"])
    return out


def _detect_rate_limit_streak(failed: List[Dict[str, Any]]) -> int:
    """Count trailing consecutive LLM/rate-limit failures (looks at the
    tail of the failed list)."""
    streak = 0
    for f in reversed(failed):
        tail = (f.get("stdout_tail") or "").lower()
        reason = (f.get("reason") or "").lower()
        if reason == "timeout":
            break
        if "429" in tail or "rate" in tail and "limit" in tail or "too many requests" in tail:
            streak += 1
        else:
            break
    return streak


def _project_cost_average(cost_map: Dict[str, float], completed: List[str]) -> float:
    """Mean cost of completed *non-reused* projects — dev-reuse entries are
    included because their cost is real spending replayed on the ledger."""
    vals = [cost_map[p] for p in completed if p in cost_map]
    return (sum(vals) / len(vals)) if vals else 0.0


def write_interim_summary(
    out_dir: Path,
    idx: int,
    completed: List[str],
    failed: List[Dict[str, Any]],
    cost_map: Dict[str, float],
    duration_map: Dict[str, float],
) -> None:
    path = out_dir / f"interim_summary_{idx}.md"
    now = datetime.now().isoformat(timespec='seconds')

    # --- 1. Cost trajectory ---
    total_cost = sum(cost_map.values())
    n_done = len(completed)
    avg = _project_cost_average(cost_map, completed)
    projected_total = avg * TOTAL_PROJECTS
    headroom = ALERT_COST_CAP_USD - projected_total

    # --- 2. Failure breakdown ---
    failure_buckets = _classify_failures(failed)

    # --- 3. Tier 4 + Gate from per-project jsonls ---
    metrics = _aggregate_project_metrics(out_dir, completed)

    # --- 4. Resource snapshot ---
    resources = _resource_snapshot(out_dir)

    # --- Render report ---
    succ_durations = [duration_map[p] for p in completed if duration_map.get(p, 0) > 0]
    avg_min = (sum(succ_durations) / len(succ_durations) / 60.0) if succ_durations else 0.0

    lines: List[str] = [
        f"# Phase 4 interim summary @ {idx} projects — {now}",
        "",
        f"- completed: **{n_done}/{TOTAL_PROJECTS}** | failed: **{len(failed)}** | pending: **{TOTAL_PROJECTS - n_done - len(failed)}**",
        f"- avg completed-project runtime: {avg_min:.1f} min (excludes dev-reuse)",
        "",
        "## 1. Cost trajectory",
        "",
        f"- spent: **${total_cost:.4f}**  (per-project avg: ${avg:.4f})",
        f"- projected total (extrapolated to {TOTAL_PROJECTS}): **${projected_total:.2f}**",
        f"- budget headroom vs $50 cap: **${50.0 - projected_total:.2f}**",
        f"- alert cap ($40) headroom: **${headroom:+.2f}** "
        f"({'⚠ WOULD TRIGGER' if headroom < 0 else 'OK'})",
        "",
        "## 2. Failure breakdown",
        "",
        f"- timeout: {len(failure_buckets['timeout'])}  "
        f"→ {failure_buckets['timeout'] or '—'}",
        f"- rc_nonzero: {len(failure_buckets['rc_nonzero'])}  "
        f"→ {failure_buckets['rc_nonzero'] or '—'}",
        f"- no_summary: {len(failure_buckets['no_summary'])}  "
        f"→ {failure_buckets['no_summary'] or '—'}",
        f"- build_fail: {len(failure_buckets['build_fail'])}  "
        f"→ {failure_buckets['build_fail'] or '—'}",
        f"- llm_fail:  {len(failure_buckets['llm_fail'])}  "
        f"→ {failure_buckets['llm_fail'] or '—'}",
        f"- other:     {len(failure_buckets['other'])}  "
        f"→ {failure_buckets['other'] or '—'}",
        "",
        "## 3. Tier 4 dynamic capture",
        "",
        f"- total attempts: {metrics['tier4_attempts']}",
        f"- dynamic success: **{metrics['tier4_dynamic']}** "
        f"({metrics['tier4_dynamic_pct']:.1f}%)",
        f"- static fallback: {metrics['tier4_static']}",
        "",
        "## 4. Gate activity (cumulative rejections across completed projects)",
        "",
        "| Gate | Count |",
        "|---|---:|",
    ]
    for gate in ("gate3_compile", "gate4_test", "gate5_coverage",
                 "gate6_smell_sub", "gate7_assert_loss",
                 "gate7_no_assertions_left"):
        lines.append(f"| {gate} | {metrics['gate_counts'].get(gate, 0)} |")

    lines.extend([
        "",
        "## 5. Resource snapshot",
        "",
        f"- CPU load (1min): **{resources['load_1min']:.2f}** "
        f"(cap {RAMP_LOAD_CAP} / cores {resources['cpu_count']})",
        f"- memory: **{resources['mem_pct']:.1f}%** used  "
        f"({resources['mem_used_gb']:.1f} / {resources['mem_total_gb']:.1f} GB)  "
        f"[cap {RAMP_MEM_PCT_CAP}%]",
        f"- disk free (workdir): {resources['disk_free_gb']:.1f} GB",
        "",
        "## Cost per project (top 10 by cost)",
        "",
        "| project | cost ($) | elapsed (min) |",
        "|---|---:|---:|",
    ])
    by_cost = sorted(cost_map.items(), key=lambda kv: kv[1], reverse=True)[:10]
    for p, c in by_cost:
        m = duration_map.get(p, 0.0) / 60.0
        lines.append(f"| {p} | {c:.4f} | {m:.1f} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_ramp_readiness(
    cfg: "Phase4Config",
    completed: List[str],
    failed: List[Dict[str, Any]],
    out_dir: Path,
    session_failed_start: int = 0,
) -> Tuple[bool, List[str]]:
    """Return (all_conditions_met, list_of_failed_conditions)."""
    non_reuse_completed = [p for p in completed if p not in cfg.dev_reuse]
    session_failed = failed[session_failed_start:]
    timeout_count = sum(1 for f in session_failed if f.get("reason") == "timeout")
    resources = _resource_snapshot(out_dir)
    conds = [
        (len(non_reuse_completed) >= cfg.ramp_up_after,
         f"completed(non-reuse)={len(non_reuse_completed)}<{cfg.ramp_up_after}"),
        (timeout_count == 0,
         f"session_timeouts={timeout_count}>0"),
        (len(session_failed) <= 1,
         f"session_failed={len(session_failed)}>1"),
        (resources["mem_pct"] < RAMP_MEM_PCT_CAP,
         f"mem={resources['mem_pct']:.1f}%>={RAMP_MEM_PCT_CAP}%"),
        (resources["load_1min"] < RAMP_LOAD_CAP,
         f"load={resources['load_1min']:.1f}>={RAMP_LOAD_CAP}"),
    ]
    reasons_failed = [msg for ok, msg in conds if not ok]
    return len(reasons_failed) == 0, reasons_failed


def check_alert_conditions(
    cost_map: Dict[str, float],
    completed: List[str],
    failed: List[Dict[str, Any]],
    last_progress_ts: float,
    dev_reuse: Dict[str, Path],
    session_failed_start: int = 0,
) -> List[str]:
    """Return list of triggered alert reasons. Empty list = all clear.

    ``session_failed_start`` is the index at which the current run began —
    anything before it is prior-run failures (e.g. permanent exclusions
    loaded from checkpoint) and MUST NOT count toward session alerts.
    Without this guard a resume after an exclusion would immediately
    re-trigger the timeout alarm.
    """
    triggered: List[str] = []
    session_failed = failed[session_failed_start:]

    # 1. cost projection
    non_reuse = [p for p in completed if p not in dev_reuse]
    if non_reuse:
        avg = sum(cost_map.get(p, 0.0) for p in non_reuse) / len(non_reuse)
        projected = avg * TOTAL_PROJECTS
        if projected > ALERT_COST_CAP_USD:
            triggered.append(f"cost_projected=${projected:.2f}>${ALERT_COST_CAP_USD}")
    # 2. timeouts — session only
    timeout_count = sum(1 for f in session_failed if f.get("reason") == "timeout")
    if timeout_count >= ALERT_TIMEOUT_COUNT:
        triggered.append(f"session_timeouts={timeout_count}>={ALERT_TIMEOUT_COUNT}")
    # 3. failure percent — session only (pre-run exclusions don't count)
    failure_threshold = int(TOTAL_PROJECTS * ALERT_FAILED_PCT)
    if len(session_failed) >= failure_threshold:
        triggered.append(
            f"session_failed={len(session_failed)}>="
            f"{failure_threshold}({int(ALERT_FAILED_PCT*100)}% of {TOTAL_PROJECTS})"
        )
    # 4. rate-limit streak — session only (tail of session_failed)
    rl_streak = _detect_rate_limit_streak(session_failed)
    if rl_streak >= ALERT_RATE_LIMIT_CONSECUTIVE:
        triggered.append(f"rate_limit_consecutive={rl_streak}")
    # 5. stall
    stall_sec = time.time() - last_progress_ts
    if stall_sec > STALL_HOURS * 3600:
        triggered.append(f"stalled={stall_sec/3600:.2f}h>{STALL_HOURS}h")
    return triggered


def write_alert_file(out_dir: Path, alert_type: str, reasons: List[str]) -> None:
    """Write a human-visible alert marker + timestamp. Also printed to stdout."""
    ts = datetime.now().isoformat(timespec='seconds')
    msg = f"[{ts}] ALERT {alert_type}: {'; '.join(reasons)}"
    print("\n" + "=" * 72, flush=True)
    print(msg, flush=True)
    print("=" * 72 + "\n", flush=True)
    f = out_dir / f"ALERT_{alert_type}.md"
    with f.open("a", encoding="utf-8") as af:
        af.write(msg + "\n")


def write_final_summary(out_dir: Path, completed: List[str], failed: List[Dict[str, Any]],
                        cost_map: Dict[str, float], duration_map: Dict[str, float]) -> None:
    lines = [
        f"# Phase 4 final summary — {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- completed projects: **{len(completed)}**",
        f"- failed projects: **{len(failed)}**",
        f"- total cost: **${sum(cost_map.values()):.4f}**",
    ]
    if duration_map:
        dur_min = [v / 60.0 for v in duration_map.values()]
        dur_min.sort()
        lines.extend([
            f"- per-project minutes — min/median/max: "
            f"{dur_min[0]:.1f} / {dur_min[len(dur_min)//2]:.1f} / {dur_min[-1]:.1f}",
        ])
    if failed:
        lines.extend(["", "## Failures", "", "| project | reason |", "|---|---|"])
        for f in failed:
            lines.append(f"| {f['project']} | {f.get('reason','?')} |")
    (out_dir / "final_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path,
                    default=SMELL_REPAIR / "config" / "main_experiment.yaml")
    ap.add_argument("--llm-config", type=Path,
                    default=SMELL_REPAIR / "config" / "llm_config.yaml")
    ap.add_argument("--project-csv", type=Path,
                    default=REPO_ROOT / "output" / "runs" / "phase4_main" / "project_classification.csv")
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT / "output" / "runs" / "phase4_main")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N pending projects (smoke test).")
    ap.add_argument("--only", nargs="*", default=None,
                    help="Restrict to these specific projects (smoke test).")
    args = ap.parse_args()

    cfg = load_yaml(args.config)
    rows = load_project_list(args.project_csv)
    pipeline_projects = [r["project"] for r in rows if r["phase4_pipeline"] == "included"]
    if args.only:
        pipeline_projects = [p for p in pipeline_projects if p in set(args.only)]

    args.out.mkdir(parents=True, exist_ok=True)
    ckpt_path = args.out / "checkpoint.json"
    fail_path = args.out / "failed_projects.json"
    ledger_path = args.out / "cost_ledger.csv"

    ckpt = load_checkpoint(ckpt_path)
    completed: List[str] = list(ckpt.get("completed") or [])
    failed: List[Dict[str, Any]] = list(ckpt.get("failed") or [])
    cost_map: Dict[str, float] = dict(ckpt.get("cost_map") or {})
    duration_map: Dict[str, float] = dict(ckpt.get("duration_map") or {})

    if ckpt.get("started_at") is None:
        ckpt["started_at"] = datetime.now().isoformat(timespec="seconds")

    # --- 1. Dev reuse: materialize prior artefacts into this run ---
    for proj, src in cfg.dev_reuse.items():
        target = args.out / f"project_{proj}"
        if proj in completed or target.exists():
            continue
        if proj not in pipeline_projects:
            continue
        print(f"[reuse] {proj} ← {src}")
        if reuse_dev_project(proj, src, target):
            c = compute_project_cost(target)
            cost_map[proj] = c
            duration_map[proj] = 0.0
            completed.append(proj)
            print(f"[reuse] {proj} OK cost=${c:.4f}")
        else:
            print(f"[reuse] {proj} FAILED — will re-run")

    save_checkpoint(ckpt_path, {
        "completed": completed, "failed": failed,
        "cost_map": cost_map, "duration_map": duration_map,
        "started_at": ckpt["started_at"],
    })

    # --- 1b. Detect any project whose workdir has a pipeline_summary.md
    # but is not in the checkpoint (happens when the runner was killed
    # between worker-finish and _handle_done). Reclaim them for free.
    failed_names = {f["project"] for f in failed}
    for p in pipeline_projects:
        if p in completed or p in failed_names:
            continue
        pdir = args.out / f"project_{p}"
        if (pdir / "pipeline_summary.md").exists():
            c = compute_project_cost(pdir)
            cost_map[p] = c
            duration_map[p] = 0.0
            completed.append(p)
            print(f"[resume] {p} found complete in workdir, cost=${c:.4f}")
    save_checkpoint(ckpt_path, {
        "completed": completed, "failed": failed,
        "cost_map": cost_map, "duration_map": duration_map,
        "started_at": ckpt["started_at"],
    })

    # --- 2. Determine pending ---
    pending = admit_projects(
        cfg, pipeline_projects,
        set(completed), {f["project"] for f in failed},
    )
    if args.limit:
        pending = pending[: args.limit]

    print(f"[runner] total pipeline projects: {len(pipeline_projects)}")
    print(f"[runner] completed (incl. dev reuse): {len(completed)}")
    print(f"[runner] failed so far: {len(failed)}")
    print(f"[runner] pending: {len(pending)}")
    print(f"[runner] budget: ${sum(cost_map.values()):.2f} / ${cfg.total_budget_usd:.2f}")

    if not pending:
        print("[runner] nothing to do")
        write_final_summary(args.out, completed, failed, cost_map, duration_map)
        return 0

    _install_signal_handlers()

    # --- 3. Worker pool ---
    workers = cfg.initial_workers
    ramped = False
    ramp_probe_done = False      # we only auto-probe once after ramp_up_after
    t_start = time.time()
    last_progress_ts = t_start   # stall detection — advanced on each completion
    in_flight: Dict[Future, str] = {}
    # Anything already in `failed` at this point is from a prior run
    # (e.g. permanent exclusions). Alerts must ignore those.
    session_failed_start = len(failed)
    print(f"[runner] session starts with {session_failed_start} pre-existing failures (exclusions)")

    idx_completed = len(completed)
    # We use a manual admission loop instead of ``executor.map`` so we can:
    #   - throttle based on total cost
    #   - resize pool after ramp_up_after successes
    #   - stop admitting on SIGINT while letting in-flight finish
    def _launch(pool: ProcessPoolExecutor, project: str) -> Future:
        out_dir = args.out / f"project_{project}"
        return pool.submit(
            _run_cli_v2, project, str(out_dir),
            str(args.llm_config), cfg.pipeline_args,
            cfg.timeout_per_project_min * 60,
            cfg.total_budget_usd,   # env var — per-project pipeline uses this as its own budget
        )

    def _handle_done(f: Future, pool: ProcessPoolExecutor) -> None:
        nonlocal idx_completed, last_progress_ts
        project = in_flight.pop(f)
        last_progress_ts = time.time()
        try:
            res = f.result()
        except Exception as e:
            res = {"project": project, "rc": -3,
                   "elapsed_sec": 0.0, "stdout_tail": f"{type(e).__name__}: {e}",
                   "timed_out": False}
        target = args.out / f"project_{project}"
        cost = compute_project_cost(target)
        duration_map[project] = float(res.get("elapsed_sec", 0.0))
        cost_map[project] = cost

        ok = (res["rc"] == 0) and (target / "pipeline_summary.md").exists()
        if ok:
            completed.append(project)
            idx_completed += 1
            print(f"[done ] {project} ✓ rc={res['rc']} cost=${cost:.4f} "
                  f"elapsed={res['elapsed_sec']/60.0:.1f}min", flush=True)
        else:
            reason = "timeout" if res["timed_out"] else (
                "rc_nonzero" if res["rc"] != 0 else "no_summary")
            rec = {
                "project": project, "reason": reason,
                "rc": res["rc"], "elapsed_min": res["elapsed_sec"] / 60.0,
                "stdout_tail": res["stdout_tail"][-1000:],
            }
            failed.append(rec)
            print(f"[FAIL ] {project} reason={reason} rc={res['rc']}", flush=True)

        save_checkpoint(ckpt_path, {
            "completed": completed, "failed": failed,
            "cost_map": cost_map, "duration_map": duration_map,
            "started_at": ckpt["started_at"],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        fail_path.write_text(json.dumps(failed, indent=2, ensure_ascii=False), encoding="utf-8")

        # cost ledger — append-only CSV
        write_header = not ledger_path.exists()
        with ledger_path.open("a", newline="", encoding="utf-8") as lf:
            w = csv.writer(lf)
            if write_header:
                w.writerow(["project", "cost_usd", "elapsed_min", "status"])
            w.writerow([project, f"{cost:.6f}", f"{res['elapsed_sec']/60.0:.2f}",
                        "ok" if ok else rec["reason"]])

        if idx_completed % cfg.interim_every == 0:
            write_interim_summary(args.out, idx_completed, completed, failed, cost_map, duration_map)

    pool = ProcessPoolExecutor(max_workers=workers)
    try:
        # Prime the pool
        while pending and len(in_flight) < workers and not STOP_FLAG.is_set():
            if sum(cost_map.values()) >= cfg.total_budget_usd:
                print(f"[runner] total budget ${cfg.total_budget_usd} reached — stopping admission")
                break
            p = pending.pop(0)
            in_flight[_launch(pool, p)] = p

        # Dispatch loop — wait for any, handle, admit next
        while in_flight:
            done_list: List[Future] = []
            # Poll every 30s. Between polls, check stall condition so that
            # even if NO completion happens we still trip the 2h alert.
            while True:
                done_list = [f for f in list(in_flight.keys()) if f.done()]
                if done_list:
                    break
                if time.time() - last_progress_ts > STALL_HOURS * 3600:
                    write_alert_file(args.out, "hard_alert",
                        [f"stalled={((time.time()-last_progress_ts)/3600):.2f}h>{STALL_HOURS}h "
                         f"no_completion in_flight={len(in_flight)}"])
                    STOP_FLAG.set()
                    break
                if STOP_FLAG.is_set():
                    break
                time.sleep(30.0)
            if STOP_FLAG.is_set() and not done_list:
                break
            for f in done_list:
                _handle_done(f, pool)

            # Auto-ramp N=initial → max after `ramp_up_after` clean
            # non-reuse completions AND all 5 health conditions met.
            if (not ramped and not ramp_probe_done
                    and cfg.max_workers > workers):
                non_reuse_done = sum(1 for p in completed if p not in cfg.dev_reuse)
                if non_reuse_done >= cfg.ramp_up_after:
                    ramp_probe_done = True
                    ok, failed_conds = check_ramp_readiness(
                        cfg, completed, failed, args.out,
                        session_failed_start=session_failed_start,
                    )
                    if ok:
                        print(f"[runner] ramp check PASSED — scaling {workers} → {cfg.max_workers}", flush=True)
                        pool.shutdown(wait=True)
                        workers = cfg.max_workers
                        pool = ProcessPoolExecutor(max_workers=workers)
                        ramped = True
                    else:
                        write_alert_file(args.out, "ramp_blocked", failed_conds)

            # Hard alerts — trigger STOP_FLAG + notify
            alerts = check_alert_conditions(
                cost_map, completed, failed, last_progress_ts, cfg.dev_reuse,
                session_failed_start=session_failed_start,
            )
            if alerts:
                write_alert_file(args.out, "hard_alert", alerts)
                STOP_FLAG.set()

            # Admit more
            while (pending and len(in_flight) < workers
                   and not STOP_FLAG.is_set()
                   and sum(cost_map.values()) < cfg.total_budget_usd):
                p = pending.pop(0)
                in_flight[_launch(pool, p)] = p

        elapsed_h = (time.time() - t_start) / 3600.0
        print(f"[runner] all workers drained in {elapsed_h:.2f}h")
    finally:
        pool.shutdown(wait=True)

    write_final_summary(args.out, completed, failed, cost_map, duration_map)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Smoke test: ping each configured model once, verify thinking-mode is
actually disabled, and that the response is a clean JSON object.

Runs a JSON-requiring prompt (``{"status": "ok"}``) under ``max_tokens=200``
— enough for a clean JSON answer but *too small* to let reasoning tokens
dominate. A model that still emits a ``<think>`` block despite our
disable knobs will either

  1. return empty content (reasoning consumed the budget), or
  2. leak a ``<think>...</think>`` literal into the content.

Both conditions are called out explicitly so Phase 2.2 can decide how to
handle each model before launching the dev run.

Usage:
  python -m smell_repair_v2.scripts.smoke_test_multi_client [--config PATH]

Exit codes:
  0  — every model returned clean, parseable JSON with no thinking leakage
  1  — config/setup error (e.g., placeholder api_key)
  2  — at least one model produced a degraded response (see per-model WARN)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.config.loader import load_llm_config  # noqa: E402
from smell_repair_v2.llm.multi_client import (  # noqa: E402
    MultiModelClient,
    contains_thinking_artifact,
)


PING_MESSAGES = [
    {
        "role": "system",
        "content": (
            "You are a status reporter. Your ONLY output must be the exact "
            "JSON object: {\"status\": \"ok\"}. No prose, no markdown, no "
            "thinking, no code fences."
        ),
    },
    {"role": "user", "content": "Ping."},
]


def _try_parse_json(content: str) -> tuple[bool, str]:
    """Return (ok, short_reason). Tolerates surrounding whitespace."""
    s = content.strip()
    if not s:
        return False, "empty content"
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        return False, f"JSONDecodeError: {e.msg}"
    if not isinstance(obj, dict):
        return False, f"top-level type {type(obj).__name__}, expected object"
    if obj.get("status") != "ok":
        return False, f"status != ok (got {obj.get('status')!r})"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    try:
        cfg = load_llm_config(args.config)
        cfg.require()
    except FileNotFoundError as e:
        print(f"[setup] {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"[setup] {e}", file=sys.stderr)
        return 1

    client = MultiModelClient(cfg)
    print(f"Pinging {len(client.model_keys)} model(s) with a JSON-only prompt...\n")

    rows: List[Dict[str, Any]] = []
    degraded: List[str] = []

    for key in client.model_keys:
        spec = client.model_spec(key)
        t0 = time.monotonic()
        try:
            resp = client.chat(key, PING_MESSAGES, max_tokens=200)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            rows.append({
                "key": key, "id": spec.id, "status": "ERROR",
                "latency_ms": elapsed_ms, "cost": 0.0,
                "in_tok": 0, "out_tok": 0,
                "thinking": False, "json_ok": False,
                "answer": f"{type(e).__name__}: {e}",
            })
            degraded.append(f"{key}: {type(e).__name__}: {e}")
            continue

        thinking_present = contains_thinking_artifact(resp.content)
        json_ok, json_reason = _try_parse_json(resp.content)
        status = "OK" if (json_ok and not thinking_present) else "WARN"
        if status == "WARN":
            reasons: List[str] = []
            if thinking_present:
                reasons.append("thinking leak")
            if not json_ok:
                reasons.append(f"json: {json_reason}")
            degraded.append(f"{key} -> " + ", ".join(reasons))

        rows.append({
            "key": key, "id": spec.id, "status": status,
            "latency_ms": resp.latency_ms,
            "cost": resp.cost_usd,
            "in_tok": resp.input_tokens,
            "out_tok": resp.output_tokens,
            "thinking": thinking_present,
            "json_ok": json_ok,
            "answer": resp.content.strip()[:120] or "(empty)",
        })

    # Pretty table
    print(f"{'MODEL':<18}  {'ID':<34}  {'STATUS':<5}  {'LAT':>7}  {'IN':>5}  "
          f"{'OUT':>5}  {'THK':>3}  {'JSN':>3}  {'$':>9}  ANSWER")
    print("-" * 130)
    for r in rows:
        print(
            f"{r['key']:<18}  {r['id']:<34}  {r['status']:<5}  "
            f"{str(r['latency_ms']) + 'ms':>7}  {r['in_tok']:>5}  {r['out_tok']:>5}  "
            f"{'Y' if r['thinking'] else '-':>3}  "
            f"{'Y' if r['json_ok'] else '-':>3}  "
            f"${r['cost']:>8.6f}  {r['answer']}"
        )

    print()
    if degraded:
        print(f"DEGRADED ({len(degraded)}/{len(rows)}):")
        for d in degraded:
            print(f"  - {d}")
        print()
        print(
            "Note: WARN does not mean the model is unusable — it means the\n"
            "thinking-disable knobs for that model need another pass before\n"
            "Tier 2 dev experiments. Share this output so we can iterate."
        )
        return 2

    print(f"All {len(rows)} model(s) returned clean JSON with no thinking leakage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

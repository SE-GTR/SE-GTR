#!/usr/bin/env python3
"""Tier 3 reasoning_effort smoke test.

Goal: decide whether gpt_oss_20b should run Tier 3 with `reasoning_effort`
level "low" (same as Tier 2) or "medium" (more thought, higher cost /
latency). Runs a realistic NARV-shaped Tier 3 prompt 3x at each level and
reports artifacts / parse success / latency / tokens.

Usage:
  python3 -m smell_repair_v2.scripts.tier3_reasoning_smoke
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from smell_repair_v2.config.loader import load_llm_config  # noqa: E402
from smell_repair_v2.llm.fewshot import TIER3_EXAMPLES  # noqa: E402
from smell_repair_v2.llm.multi_client import (  # noqa: E402
    MultiModelClient,
    contains_thinking_artifact,
)
from smell_repair_v2.llm.plan_parser import PlanParseError, parse_plan_response  # noqa: E402
from smell_repair_v2.llm.prompts import (  # noqa: E402
    PlanPromptInputs,
    PlanPromptLimits,
    TIER_ALLOWED_OPERATORS,
    build_plan_messages,
)

MODEL_KEY = "gpt_oss_20b"
ITERS = 3
LEVELS = ["low", "medium"]


# Realistic Tier 3 NARV prompt
NARV_METHOD = (
    "@Test\n"
    "public void test() {\n"
    "    List<String> list = new ArrayList<String>();\n"
    "    list.add(\"hello\");\n"
    "    list.add(\"world\");\n"
    "    assertEquals(2, list.size());\n"
    "    list.contains(\"hello\");\n"
    "}"
)

NARV_EVIDENCE = {
    "unasserted_return_calls": [
        {"expr": "list.contains(\"hello\")", "return_type": "boolean", "begin_line": 7}
    ]
}

CUT_CTX = (
    "public interface List<E> {\n"
    "  boolean add(E e);\n"
    "  boolean contains(Object o);\n"
    "  int size();\n"
    "}"
)


def _build_messages() -> List[Dict[str, str]]:
    inp = PlanPromptInputs(
        smell_id="NARV",
        tier=3,
        evidence=NARV_EVIDENCE,
        test_method_code=NARV_METHOD,
        cut_context=CUT_CTX,
        cut_fqcn="java.util.List",
        allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
        fewshot_examples=TIER3_EXAMPLES.get("NARV", []),
        limits=PlanPromptLimits(),
    )
    return build_plan_messages(inp)


def _run_once(multi: MultiModelClient, level: str) -> Dict[str, Any]:
    extra = {
        "reasoning": {"effort": level},
        "reasoning_effort": level,
    }
    messages = _build_messages()
    resp = multi.chat(MODEL_KEY, messages, extra_body=extra, max_tokens=2000)

    thinking = contains_thinking_artifact(resp.content or "")
    try:
        plans = parse_plan_response(
            resp.content or "",
            smell_id="NARV",
            allowed_operators=list(TIER_ALLOWED_OPERATORS[3]),
        )
        parse_ok = True
        parse_err = None
        plan_summary = [{"op": p.op.value, "params": p.params} for p in plans]
    except PlanParseError as e:
        parse_ok = False
        parse_err = e.reason
        plan_summary = None

    return {
        "level": level,
        "latency_ms": resp.latency_ms,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "cost_usd": resp.cost_usd,
        "thinking_artifact": thinking,
        "parse_ok": parse_ok,
        "parse_error": parse_err,
        "plan": plan_summary,
        "raw": resp.content,
    }


def main() -> int:
    cfg = load_llm_config()
    cfg.require()
    multi = MultiModelClient(cfg)

    all_results: List[Dict[str, Any]] = []
    for level in LEVELS:
        for i in range(ITERS):
            print(f"\n=== {level.upper()} #{i+1} ===")
            r = _run_once(multi, level)
            all_results.append(r)
            print(f"  latency: {r['latency_ms']}ms  in={r['input_tokens']} out={r['output_tokens']}  "
                  f"cost=${r['cost_usd']:.6f}  thinking={r['thinking_artifact']}  "
                  f"parse={'OK' if r['parse_ok'] else 'FAIL: ' + str(r['parse_error'])}")
            if r["plan"] is not None:
                print(f"  plan: {json.dumps(r['plan'], ensure_ascii=False)}")
            print(f"  raw preview: {(r['raw'] or '')[:300].rstrip()}")

    # summary
    print("\n\n=== SUMMARY ===")
    for level in LEVELS:
        rs = [r for r in all_results if r["level"] == level]
        lat = sum(r["latency_ms"] for r in rs) / len(rs)
        out_tok = sum(r["output_tokens"] for r in rs) / len(rs)
        cost = sum(r["cost_usd"] for r in rs) / len(rs)
        think = sum(1 for r in rs if r["thinking_artifact"])
        ok = sum(1 for r in rs if r["parse_ok"])
        print(f"{level:<8} avg_lat={lat:.0f}ms  avg_out={out_tok:.0f}tok  avg_$={cost:.6f}  "
              f"thinking={think}/{len(rs)}  parse_ok={ok}/{len(rs)}")

    # dump full for human review
    out_path = Path("/tmp/tier3_reasoning_smoke.json")
    out_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"\nFull dump: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

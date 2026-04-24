"""Routes a smell_id to a tier.

Tier 1: deterministic (no LLM).
Tier 2: structured LLM (template-filled operator plans).
Tier 3: reasoning LLM (free-form plans, more guidance).
Tier 4: out-of-scope (skip, keep as-is).
"""
from __future__ import annotations

from typing import Dict

TIER_MAP: Dict[str, int] = {
    # Tier 1 — fully deterministic
    "NNA": 1,
    "DS": 1,
    "AC": 1,
    "TSES": 1,  # simple pattern only; complex falls through to Tier 2
    # Tier 2 — structured LLM (Phase 2)
    "ARPM": 2,
    "NARV": 2,
    "NASE": 2,
    "TSVM": 2,
    "OIMT": 2,
    "ENET": 2,
    # Tier 3 — reasoning LLM (Phase 2)
    "AOIMT": 3,  # Asserting object init multiple times (if present)
    "EDED": 3,
    "EDIS": 3,
    "TOFA": 3,
    # Tier 4 — skip (currently nothing)
}


def get_tier_for_smell(smell_id: str) -> int:
    return TIER_MAP.get(smell_id, 4)

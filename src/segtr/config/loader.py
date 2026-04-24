"""Loads the Phase 2.2 multi-model config yaml into typed dataclasses."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


_PLACEHOLDER = "PLACEHOLDER_SET_BY_USER"
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "llm_config.yaml"


class BudgetExceededError(RuntimeError):
    """Raised when a model's cumulative spend crosses the per-model budget."""


@dataclass(frozen=True)
class ModelSpec:
    key: str
    id: str
    display_name: str
    input_price_per_m: float
    output_price_per_m: float
    context_window: int
    notes: str = ""


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    base_url: str
    timeout_sec: int
    default_headers: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DevExperimentConfig:
    projects: List[str]
    tier2_smells: List[str]
    max_requests_per_method: int
    cost_budget_per_model_usd: float
    temperature: float
    max_output_tokens: int


@dataclass(frozen=True)
class LlmRuntimeConfig:
    openrouter: OpenRouterConfig
    models: Dict[str, ModelSpec]
    dev: DevExperimentConfig

    def require(self) -> None:
        """Raise a friendly error if the api_key is still a placeholder."""
        if self.openrouter.api_key in ("", _PLACEHOLDER):
            raise RuntimeError(
                "OpenRouter api_key is not set.\n"
                f"Edit {_DEFAULT_CONFIG_PATH}  (or export OPENROUTER_API_KEY)\n"
                "and replace PLACEHOLDER_SET_BY_USER with your real key."
            )


def _as_model_spec(key: str, raw: Dict) -> ModelSpec:
    pricing = raw.get("pricing") or {}
    return ModelSpec(
        key=key,
        id=str(raw["id"]),
        display_name=str(raw.get("display_name", key)),
        input_price_per_m=float(pricing.get("input", 0.0)),
        output_price_per_m=float(pricing.get("output", 0.0)),
        context_window=int(raw.get("context_window", 0)),
        notes=str(raw.get("notes", "")),
    )


def load_llm_config(path: Optional[Path] = None) -> LlmRuntimeConfig:
    """Parse the yaml into typed config.

    - Default path is ``smell_repair_v2/config/llm_config.yaml``.
    - If that file is missing but the template ``llm_config.example.yaml``
      exists, the loader raises a friendly "copy it and fill in your key"
      error rather than a confusing FileNotFoundError.
    - ``OPENROUTER_API_KEY`` env var, when set, overrides the yaml key —
      useful for CI or when running smoke tests without editing the yaml.
    """
    if path is None:
        path = _DEFAULT_CONFIG_PATH
    path = Path(path)

    if not path.exists():
        template = path.with_name("llm_config.example.yaml")
        if template.exists():
            raise FileNotFoundError(
                f"{path} not found.\n"
                f"Copy the template and set your OpenRouter API key:\n"
                f"  cp {template} {path}\n"
                f"  # then edit {path} and replace PLACEHOLDER_SET_BY_USER"
            )
        raise FileNotFoundError(f"{path} not found and no template available")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    or_raw = data.get("openrouter") or {}
    api_key = str(or_raw.get("api_key", "")).strip()
    env_override = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_override:
        api_key = env_override

    openrouter = OpenRouterConfig(
        api_key=api_key,
        base_url=str(or_raw.get("base_url", "https://openrouter.ai/api/v1")),
        timeout_sec=int(or_raw.get("timeout_sec", 120)),
        default_headers={str(k): str(v) for k, v in (or_raw.get("default_headers") or {}).items()},
    )

    models_raw = data.get("models") or {}
    models = {key: _as_model_spec(key, raw) for key, raw in models_raw.items()}

    dev_raw = data.get("dev_experiment") or {}
    # Env override for per-run cost budgets (Phase 4 main experiment raises
    # the default $10 dev budget to $50 for SF110-wide runs without touching
    # the committed yaml). Set SE_GTR_COST_BUDGET_USD=<N> before launch.
    env_budget = os.environ.get("SE_GTR_COST_BUDGET_USD", "").strip()
    cost_budget = (
        float(env_budget) if env_budget
        else float(dev_raw.get("cost_budget_per_model_usd", 10.0))
    )
    dev = DevExperimentConfig(
        projects=list(dev_raw.get("projects") or []),
        tier2_smells=list(dev_raw.get("tier2_smells") or []),
        max_requests_per_method=int(dev_raw.get("max_requests_per_method", 3)),
        cost_budget_per_model_usd=cost_budget,
        temperature=float(dev_raw.get("temperature", 0.2)),
        max_output_tokens=int(dev_raw.get("max_output_tokens", 2000)),
    )

    return LlmRuntimeConfig(openrouter=openrouter, models=models, dev=dev)

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from smell_repair_v2.config.loader import (
    BudgetExceededError,
    load_llm_config,
)


_YAML = textwrap.dedent("""\
    openrouter:
      api_key: "sk-test-key"
      base_url: "https://openrouter.ai/api/v1"
      timeout_sec: 90
      default_headers:
        HTTP-Referer: "https://example.com"
        X-Title: "test"
    models:
      a_small:
        id: "vendor/a-small"
        display_name: "A Small"
        pricing: {input: 0.10, output: 0.30}
        context_window: 131072
        notes: "cheap"
      b_big:
        id: "vendor/b-big"
        display_name: "B Big"
        pricing: {input: 0.50, output: 2.00}
        context_window: 262144
        notes: "expensive"
    dev_experiment:
      projects: ["proj_a", "proj_b"]
      tier2_smells: ["ENET", "EDIS"]
      max_requests_per_method: 3
      cost_budget_per_model_usd: 5.0
      temperature: 0.2
      max_output_tokens: 1500
""")


class TestLoadLlmConfig(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tf = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        tf.write(body)
        tf.close()
        return Path(tf.name)

    def test_parses_yaml(self):
        p = self._write(_YAML)
        try:
            cfg = load_llm_config(p)
        finally:
            p.unlink()
        self.assertEqual(cfg.openrouter.api_key, "sk-test-key")
        self.assertEqual(cfg.openrouter.timeout_sec, 90)
        self.assertEqual(cfg.openrouter.default_headers["X-Title"], "test")
        self.assertEqual(set(cfg.models), {"a_small", "b_big"})
        self.assertEqual(cfg.models["a_small"].input_price_per_m, 0.10)
        self.assertEqual(cfg.models["b_big"].output_price_per_m, 2.00)
        self.assertEqual(cfg.dev.projects, ["proj_a", "proj_b"])
        self.assertEqual(cfg.dev.cost_budget_per_model_usd, 5.0)

    def test_placeholder_api_key_raises_on_require(self):
        y = _YAML.replace("sk-test-key", "PLACEHOLDER_SET_BY_USER")
        p = self._write(y)
        try:
            cfg = load_llm_config(p)
            with self.assertRaises(RuntimeError) as ctx:
                cfg.require()
            self.assertIn("PLACEHOLDER_SET_BY_USER", str(ctx.exception))
        finally:
            p.unlink()

    def test_env_override_wins(self):
        p = self._write(_YAML)
        try:
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-from-env"}):
                cfg = load_llm_config(p)
            self.assertEqual(cfg.openrouter.api_key, "sk-from-env")
        finally:
            p.unlink()

    def test_missing_config_points_to_template(self):
        # choose a path that doesn't exist but has the example template next to it
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "llm_config.yaml"
            template = Path(tmp) / "llm_config.example.yaml"
            template.write_text(_YAML, encoding="utf-8")
            with self.assertRaises(FileNotFoundError) as ctx:
                load_llm_config(target)
            self.assertIn("Copy the template", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

# LLM configuration

## Model

SE-GTR Full and the Naive baseline both use **`openai/gpt-oss-20b`**
(a publicly available 20B-parameter open-weights model, hosted by any
OpenAI-compatible inference provider). The model name is a public
identifier and is given verbatim. Endpoint URLs and API keys are
placeholders.

## Endpoint

In the provided configs — `configs/*.yaml` in the GitHub repository,
`00_code/configs/*.yaml` in the Zenodo archive — the endpoint is:

```yaml
llm:
  base_url: "<OPENAI_COMPATIBLE_ENDPOINT>"
  api_key:  "${LLM_API_KEY}"
```

Replace `<OPENAI_COMPATIBLE_ENDPOINT>` with any provider that serves
`openai/gpt-oss-20b` over the OpenAI Chat Completions API. All reported
runs use `openai/gpt-oss-20b` served through OpenRouter. This archive
contains no cross-provider comparison.

## Decoding settings

Tier 1 is LLM-free (deterministic operators, no request is made). Tiers 2–4
share one flat decoding profile — there is no per-tier temperature schedule:

- temperature = 0.2
- top_p = 0.9
- max_output_tokens = 2000
- request_timeout_sec = 120

These are the values the pipeline actually used. They come from the LLM
configuration file the pipeline reads at run time (see the tree layout in the
top-level README) — `openrouter.timeout_sec`, `dev_experiment.temperature`,
`dev_experiment.max_output_tokens` — with the same defaults hard-coded in the
config loader; `top_p` is set in the multi-model client. The `llm:` blocks in
those `configs/*.yaml` files are NOT read at runtime; they still read
2048 / 300 and were deliberately left untouched. See "Vestigial and
non-executed configuration" in the top-level README for why, and
`CHANGELOG_v2.md` in the Zenodo archive for what v1 documented here.

## Retries

Each plan is attempted at most 3 times (`max_llm_attempts_per_plan = 3`).
Retries are triggered by:
- empty response
- parse failure (non-JSON or schema-violating output)
- validator rejection at a *parse-class* gate (gate 1 or 2)

Retries are NOT triggered by a compile-gate or run-gate rejection —
those are considered semantic failures and the plan is rejected as-is.

## Cost

The Phase-4 main run made **24,542 LLM calls** on the held-out cohort and
**26,216** across all 86 completed projects, using `openai/gpt-oss-20b`.

The paper reports cost as plan-and-tier activity, not in currency.
`openai/gpt-oss-20b` is an open-weights model, so any dollar amount is a
property of one hosting provider's price list on the run dates rather than
of SE-GTR, and will not reproduce elsewhere. Per-plan and per-tier counts are
in the Zenodo archive, in `02_phase4_segtr_full/plan_log_all.jsonl` (31,811
plans) and in each project's `summary.json`. v1 of the archive quoted dollar
figures here; see `CHANGELOG_v2.md` in the Zenodo archive for what they were
and why they were withdrawn.

## No secrets

No API keys, session tokens, or host IDs appear in this package. If
you find a string matching `sk-[A-Za-z0-9-]{20,}` anywhere, it is a
packaging bug — please report.

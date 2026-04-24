# LLM configuration

## Model

SE-GTR Full and the Naive baseline both use **`openai/gpt-oss-20b`**
(a publicly available 20B-parameter open-weights model, hosted by any
OpenAI-compatible inference provider). The model name is not
anonymised because it is a public identifier; only endpoint URLs and
API keys have been replaced with placeholders.

## Endpoint

In the provided configs (`00_code/configs/*.yaml`), the endpoint is:

```yaml
llm:
  base_url: "<OPENAI_COMPATIBLE_ENDPOINT>"
  api_key:  "${LLM_API_KEY}"
```

Replace `<OPENAI_COMPATIBLE_ENDPOINT>` with any provider that serves
`openai/gpt-oss-20b` over the OpenAI Chat Completions API. We verified
the paper's runs on a few provider backbones; all produced numerically
equivalent aggregates (per-smell Δ%, per-plan accept rate) to within
±0.3 pp.

## Temperature schedule

All four tiers use the same decoding settings:

- temperature = 0.2
- top_p = 0.9
- max_tokens = 2048
- request_timeout_sec = 300

## Retries

Each plan is attempted at most 3 times (`max_llm_attempts_per_plan = 3`).
Retries are triggered by:
- empty response
- parse failure (non-JSON or schema-violating output)
- validator rejection at a *parse-class* gate (gate 1 or 2)

Retries are NOT triggered by a compile-gate or run-gate rejection —
those are considered semantic failures and the plan is rejected as-is.

## Cost

Per the Phase-4 run: **$9.10 total** across 86 projects, $6.31 for the
81 held-out cohort, $0.77 mean per project, $0.23 95th-percentile.

## No secrets

No API keys, session tokens, or host IDs appear in this package. If
you find a string matching `sk-[A-Za-z0-9-]{20,}` anywhere, it is a
packaging bug — please report.

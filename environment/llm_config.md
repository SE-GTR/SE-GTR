# LLM configuration

## Model

SE-GTR Full and the Naive baseline both use **`openai/gpt-oss-20b`**
(a publicly available 20B-parameter open-weights model, hosted by any
OpenAI-compatible inference provider). The model name is a public
identifier and is given verbatim. Endpoint URLs and API keys are
placeholders.

## Endpoint

In the provided configs (`00_code/configs/*.yaml`), the endpoint is:

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

## Retries

Each plan is attempted at most 3 times (`max_llm_attempts_per_plan = 3`).
Retries are triggered by:
- empty response
- parse failure (non-JSON or schema-violating output)
- validator rejection at a *parse-class* gate (gate 1 or 2)

Retries are NOT triggered by a compile-gate or run-gate rejection —
those are considered semantic failures and the plan is rejected as-is.

## No secrets

No API keys, session tokens, or host IDs appear in this package. If
you find a string matching `sk-[A-Za-z0-9-]{20,}` anywhere, it is a
packaging bug — please report.

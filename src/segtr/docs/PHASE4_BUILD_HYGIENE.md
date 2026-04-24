# Phase 4 Build-Hygiene Memo

Context: the Phase 2.3 investigation surfaced a stale-build artifact that
inflated reported class-level regressions from 0 to 20. Option 3 (a single
`ant clean compile compile-evosuite` before the final class-pass
measurement) resolved it for dev. **Phase 4 (SF110 main experiment, 94
projects)** must reconsider the trade-off.

## Observed stale-build pattern

- Incremental ant only recompiles sources whose mtime exceeds the `.class`
  mtime. Under hundreds of in-run source rewrites on the same test file,
  ant’s dependency graph for the `compile-evosuite` target can leave
  inconsistent output (`.class` newer than the *intermediate* source it
  was compiled from, but older than the final source).
- Gate 3–4 (per-method validator) runs JUnitCore on the test class right
  after ant returns RC=0. That transient snapshot usually matches the
  source, so per-method gates are honest.
- Post-hoc aggregate measurement (`_measure_class_test_pass_after`) sees
  the final source paired with a stale class file; spurious failures
  result.

Symmetric verification on pristine workdirs (no source churn): incremental
and clean rebuild give identical results for 5/5 dev projects. Hypothesis
confirmed — the failure mode is churn-specific.

## Option 1 — Clean every Gate 4 call

`ValidatorConfig.compile_targets = ("clean", "compile", "compile-evosuite")`

- Cost per invocation: ~1 s per project (measured on dev set).
- Phase 4 invocation count estimate:
  - 94 projects × avg ~30 accepted plans per project × 1 clean call per
    plan ≈ 2 820 clean calls (multiple models → multiply accordingly).
  - For a single model run: ~1 h added on top of per-plan compile+test
    time.
  - For 5-model Phase 4: ~5 h added. Rough ceiling 8 h if accepted plan
    counts scale higher than dev.

Trade-off:
- Pro: Each per-plan Gate 3–4 judgement uses a known-consistent build,
  so "plan X passed" is fully trustworthy.
- Con: 30–60 % runtime increase.

## Option 3 (current) — Clean only before final aggregate measurement

- Cost: 1 s × project × model ≈ negligible (< 10 min total across
  Phase 4).
- Covers the reported numbers but leaves per-plan Gate 3–4 on incremental
  build.
- Risk: if incremental ant occasionally returns RC=0 for a stale build
  that happens to pass JUnitCore but would fail on a clean build, a plan
  may be incorrectly committed. Our dev investigation found zero such
  cases in 276 accepted plans — plans that actually failed under clean
  build all failed under incremental too. No evidence this risk
  materialises; still worth keeping in mind.

## Recommendation for Phase 4

**Primary**: Enable Option 1. At the scale of the main experiment the
numeric claims are load-bearing; an 8-hour overhead is cheap insurance
against a reviewer asking "how do you know Gate 4 is honest under churn?"

**Alternative** (if runtime is tight): Stick with Option 3, and add a
supplementary post-run audit that re-runs each accepted plan against a
clean build and reports any divergence. Same total cost (~8 h) but moves
the overhead out of the critical path.

**Do NOT**: rely on Option 3 alone without the audit. The dev investigation
ruled out divergences on a 276-plan sample, but SF110 scale (10–20×)
warrants direct confirmation.

## Implementation pointers

- Option 1: single-line change to ``MultiGateValidator`` default
  ``compile_targets`` (or override via ``ValidatorConfig``).
- Audit script: walk `raw_results.jsonl` for accepted plans, replay each
  against a freshly-cleaned workdir (parallelisable by project).
- Both Options leave Phase 2 / 2.3 dev results unaffected; Option 3 is
  already live.

#!/usr/bin/env python3
"""Consolidate all SE-GTR experiment results into one markdown document
for handoff to the paper-writing Claude session.

Spec: concatenate listed files with <<<FILE: ...>>> markers, preserve
content as-is (no summarisation), tag section breaks. Missing files
logged as `[FILE NOT FOUND]`. Reports stats at end.

Output location: normally `/mnt/user-data/uploads/` per spec, but that
path is not writable here without sudo, so fall back to
`output/uploads/` inside the repo. The user can move it.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
OUTPUT = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl/output/uploads/all_results_consolidated.md')


# ---------------------------------------------------------------------------
# Files to include, in order, with per-section headers.
# ---------------------------------------------------------------------------

SECTIONS = [
    ('## 1. Project Selection & Metadata', [
        'output/runs/rq3_experiments/selection/selected_15.csv',
        'output/runs/rq3_experiments/selection/pool_32_healthy.csv',
        'output/runs/rq3_experiments/selection/selection_log.md',
    ]),
    ('## 2. Phase 4 — Main SF110 Experiment (86 projects)', [
        'output/runs/phase4_main/aggregation/observation_summary.md',
        'output/runs/phase4_main/aggregation/dev_vs_heldout_consistency.md',
        'output/runs/phase4_main/aggregation/tables/table1_heldout_smell_reduction.md',
        'output/runs/phase4_main/aggregation/tables/table2_dev_smell_reduction.md',
        'output/runs/phase4_main/aggregation/tables/table3_tier_contribution.md',
        'output/runs/phase4_main/aggregation/tables/table4_gate_activity.md',
        'output/runs/phase4_main/aggregation/tables/table5_dynamic_capture.md',
        'output/runs/phase4_main/aggregation/tables/table6_coverage_delta.md',
        'output/runs/phase4_main/aggregation/tables/table1_heldout_smell_reduction.csv',
        'output/runs/phase4_main/aggregation/tables/table6_coverage_delta.csv',
    ]),
    ('## 3. Phase 4.5 — PIT Mutation Validation', [
        'output/runs/phase4_main/pit/table7_final.md',
        'output/runs/phase4_main/pit/table7_final.csv',
        'output/runs/phase4_main/pit/per_project_pit_final.csv',
        'output/runs/phase4_main/pit/validation/final_validity_assessment.md',
        'output/runs/phase4_main/pit/root_cause/executive_summary.md',
        'output/runs/phase4_main/pit/root_cause/pattern_summary.md',
        'output/runs/phase4_main/pit/root_cause/gate7_review.md',
    ]),
    ('## 4. Phase B — Naive LLM Baseline (15 projects)', [
        'output/runs/rq3_experiments/naive_llm/naive_summary.md',
        'output/runs/rq3_experiments/naive_llm/per_project_naive.csv',
        'output/runs/rq3_experiments/naive_llm/aggregate_by_bin.csv',
        'output/runs/rq3_experiments/naive_llm/phase4_full_vs_naive.csv',
        'output/runs/rq3_experiments/naive_llm/smell_substitution.csv',
    ]),
    ('## 5. Phase C — UTRefactor Baseline (15 projects)', [
        'output/runs/rq3_experiments/utrefactor/run15/utrefactor_summary.md',
        'output/runs/rq3_experiments/utrefactor/run15/updated_3way_analysis.md',
        'output/runs/rq3_experiments/utrefactor/run15/method_count_analysis.md',
        'output/runs/rq3_experiments/utrefactor/run15/jacoco_remeasure.csv',
        'output/runs/rq3_experiments/utrefactor/run15/composite_quality.csv',
        'output/runs/rq3_experiments/utrefactor/run15/three_way_comparison.csv',
        'output/runs/rq3_experiments/utrefactor/run15/smell_substitution_3way.csv',
        'output/runs/rq3_experiments/utrefactor/run15/anomaly_90_dcparseargs.md',
        'output/runs/rq3_experiments/utrefactor/smoke/smoke_30_bpmail.md',
    ]),
    ('## 6. Phase D — Ablation (T1, T1+T2, T1+T2+T3; 15 projects each)', [
        'output/runs/rq3_experiments/ablation/ablation_summary.md',
        'output/runs/rq3_experiments/ablation/per_project_ablation.csv',
        'output/runs/rq3_experiments/ablation/table_D1_per_condition_aggregate.csv',
        'output/runs/rq3_experiments/ablation/table_D2_per_smell_by_condition.csv',
        'output/runs/rq3_experiments/ablation/table_D3_tier_incremental.csv',
    ]),
    ('## 7. Phase E — PIT 3-way Comparison (Full / Naive / UTRefactor)', [
        'output/runs/rq3_experiments/pit/pit_rq3_summary.md',
        'output/runs/rq3_experiments/pit/per_project_pit.csv',
        'output/runs/rq3_experiments/pit/table_E1_3way.csv',
    ]),
]


def _dump_file(out_fh, rel_path: str, stats: dict) -> None:
    """Emit `<<<FILE: rel_path>>>` followed by contents or error."""
    out_fh.write(f'<<<FILE: {rel_path}>>>\n')
    full = REPO_ROOT / rel_path
    if not full.exists():
        out_fh.write('[FILE NOT FOUND]\n\n')
        stats['not_found'].append(rel_path)
        return
    try:
        content = full.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        out_fh.write('[FILE ERROR: non-UTF-8 contents; likely binary]\n\n')
        stats['error'].append((rel_path, 'non-utf8'))
        return
    except Exception as e:
        out_fh.write(f'[FILE ERROR: {type(e).__name__}: {e}]\n\n')
        stats['error'].append((rel_path, str(e)))
        return

    # CSV files: wrap in a fenced block so the original Claude renderer
    # doesn't interpret comma-separated lines as markdown tables.
    if rel_path.endswith('.csv'):
        out_fh.write('```csv\n')
        out_fh.write(content)
        if not content.endswith('\n'):
            out_fh.write('\n')
        out_fh.write('```\n\n')
    else:
        out_fh.write(content)
        if not content.endswith('\n'):
            out_fh.write('\n')
        out_fh.write('\n')
    stats['ok'].append((rel_path, len(content)))


def _dump_operator_catalog(out_fh, stats: dict) -> None:
    """Section 8 — operator + validator code references."""
    ops_dir = REPO_ROOT / 'smell_repair_v2' / 'operators'
    out_fh.write('\n## 8. Operator Catalog & Validator Code References\n\n')

    # Explicit README/catalog.md if present
    for candidate in ('README.md', 'catalog.md', 'CATALOG.md'):
        p = ops_dir / candidate
        if p.exists():
            _dump_file(out_fh, f'smell_repair_v2/operators/{candidate}', stats)

    # Directory listing
    if ops_dir.exists():
        out_fh.write('<<<DIR LISTING: smell_repair_v2/operators/>>>\n\n')
        py_files = sorted(p for p in ops_dir.iterdir() if p.suffix == '.py')
        for pf in py_files:
            out_fh.write(f'- {pf.name}\n')
        out_fh.write('\n')

        # Per-operator head 30 lines (docstring / top-of-file comments)
        for pf in py_files:
            if pf.name in ('__init__.py',):
                continue
            rel = f'smell_repair_v2/operators/{pf.name}'
            head_n = 100 if pf.name == 'validator.py' else 30
            out_fh.write(f'<<<FILE HEAD ({head_n} lines): {rel}>>>\n')
            try:
                lines = pf.read_text(encoding='utf-8').splitlines()[:head_n]
                out_fh.write('```python\n')
                out_fh.write('\n'.join(lines) + '\n')
                out_fh.write('```\n\n')
                stats['ok'].append((f'{rel} [head {head_n}]', sum(len(l) for l in lines)))
            except Exception as e:
                out_fh.write(f'[FILE ERROR: {type(e).__name__}: {e}]\n\n')
                stats['error'].append((rel, str(e)))

        # Validator constants
        val = ops_dir / 'validator.py'
        if val.exists():
            out_fh.write('<<<VALIDATOR CONSTANTS SNIPPET: BANNED_METHOD_CALLS / '
                         '_BANNED_NEW_IMPORT_PATTERNS / 7-gate docstring>>>\n')
            try:
                t = val.read_text(encoding='utf-8')
                out_fh.write('```python\n')
                # Grab the 7-gate docstring (first `"""..."""`)
                import re
                doc_m = re.search(r'"""(.*?)"""', t, re.DOTALL)
                if doc_m:
                    out_fh.write('# 7-gate module docstring\n"""')
                    out_fh.write(doc_m.group(1))
                    out_fh.write('"""\n\n')
                # Pull banned-patterns blocks
                for pat in (
                    r'_BANNED_NEW_IMPORT_PATTERNS.*?\n\)',
                    r'BANNED_METHOD_CALLS.*?\n\)',
                ):
                    m = re.search(pat, t, re.DOTALL)
                    if m:
                        out_fh.write('# ' + pat + '\n')
                        out_fh.write(m.group(0) + '\n\n')
                # Also import_manager if it's where BANNED_METHOD_CALLS lives
                im = ops_dir / 'import_manager.py'
                if im.exists():
                    t2 = im.read_text(encoding='utf-8')
                    m = re.search(r'BANNED_METHOD_CALLS.*?\n[\)\]]', t2, re.DOTALL)
                    if m:
                        out_fh.write('# from smell_repair_v2/operators/import_manager.py\n')
                        out_fh.write(m.group(0) + '\n\n')
                out_fh.write('```\n\n')
                stats['ok'].append(('<validator constants snippet>', 0))
            except Exception as e:
                out_fh.write(f'[ERROR: {e}]\n\n')


def _maybe_phase2(out_fh, stats: dict) -> None:
    """Section 9 — optional Phase 2 (5-LLM comparison) if present."""
    p22 = REPO_ROOT / 'output/runs/phase2_2_multi_llm'
    if not p22.exists():
        return
    out_fh.write('\n## 9. Phase 2 — 5-LLM Comparison (supplementary)\n\n')
    for md in sorted(p22.rglob('*.md'))[:10]:
        rel = md.relative_to(REPO_ROOT).as_posix()
        _dump_file(out_fh, rel, stats)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    stats = {'ok': [], 'not_found': [], 'error': []}

    total_files = sum(len(files) for _, files in SECTIONS)
    with OUTPUT.open('w', encoding='utf-8') as f:
        f.write('# SE-GTR All Experiment Results — Consolidated\n\n')
        f.write(f'- Generated: {datetime.now().isoformat(timespec="seconds")}\n')
        f.write(f'- Repo root: `{REPO_ROOT}`\n')
        f.write(f'- Planned file count: {total_files} (+ operator catalog + '
                f'optional Phase 2 supplementary)\n')
        f.write('- Per-file marker: `<<<FILE: <rel_path>>>>` followed by '
                'content (fenced in ```csv``` blocks for CSVs, raw '
                'otherwise).\n\n')
        f.write('---\n\n')

        for header, files in SECTIONS:
            f.write(f'\n{header}\n\n')
            for rel in files:
                _dump_file(f, rel, stats)

        _dump_operator_catalog(f, stats)
        _maybe_phase2(f, stats)

    # Report
    size = OUTPUT.stat().st_size
    print(f'\nwrote: {OUTPUT}')
    print(f'size : {size:,} bytes ({size/1024/1024:.2f} MB)')
    print(f'rough token estimate: ~{int(size * 0.25):,} tokens')
    print(f'ok       : {len(stats["ok"])}')
    print(f'not_found: {len(stats["not_found"])}')
    print(f'errors   : {len(stats["error"])}')
    if stats['not_found']:
        print('\nMISSING FILES:')
        for rel in stats['not_found']:
            print(f'  - {rel}')
    if stats['error']:
        print('\nERRORS:')
        for rel, err in stats['error']:
            print(f'  - {rel}: {err}')


if __name__ == '__main__':
    main()

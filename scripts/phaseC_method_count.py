#!/usr/bin/env python3
"""Phase C follow-up — test-method structure analysis.

For each of the 12 RQ3-completed projects, count ``@Test``-annotated
methods in three states:

  1. BEFORE  — pristine SF110 EvoSuite tests (source of truth: the
     project's `jacoco_before.tests_total` field from Phase 4's
     per_project.json; cross-checked via a ripgrep on the SF110 source
     tree).
  2. AFTER Full — Phase-4-Full's post-run workdir (same `tests_total`
     from `jacoco_after` field).
  3. AFTER UTRefactor — Phase-C run15 workdir, counted directly via
     ripgrep on `src/test/java` + `evosuite-tests/`.

A method-count inflation under UTRefactor (tests_after > tests_before)
is evidence of the "split every test method into one @Test per split"
mechanism described in UTRefactor's paper. Reporting that alongside the
smell delta lets us separate genuine smell repair from smell
dilution-by-splitting.

Outputs:
  output/runs/rq3_experiments/utrefactor/run15/method_count_analysis.csv
  output/runs/rq3_experiments/utrefactor/run15/method_count_analysis.md
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
RUN_DIR = REPO / 'output/runs/rq3_experiments/utrefactor/run15'
NAIVE_DIR = REPO / 'output/runs/rq3_experiments/naive_llm'
FULL_DIR = REPO / 'output/runs/phase4_main'
SELECTION_CSV = REPO / 'output/runs/rq3_experiments/selection/selected_15.csv'
SF110_ROOT = Path('<ANON_ROOT>/segtr_replication/sf110_projects')


_AT_TEST_RE = re.compile(
    r'@Test(?:\s*\([^)]*\))?\s*(?:public|private|protected|\s)+\s+'
    r'[A-Za-z_][\w<>,\[\]\s]*?\s+[A-Za-z_]\w*\s*\(',
    re.MULTILINE,
)


def _count_at_test_in_tree(root: Path) -> int:
    """Count ``@Test``-annotated methods under ``root``.

    Scans every ``*.java`` file for the ``@Test`` annotation, then an
    ensuing method declaration. Uses a text-level regex so it works on
    UTRefactor's split files, SE-GTR's in-place edits, and pristine
    EvoSuite output uniformly. Not a Java parser — good enough for
    counting; excludes ``*_scaffolding.java`` so we don't double-count
    the EvoSuite boilerplate.
    """
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob('*.java'):
        if p.name.endswith('_scaffolding.java'):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        total += len(_AT_TEST_RE.findall(text))
    return total


def _load_full_tests(project: str) -> Dict[str, Any]:
    pp = FULL_DIR / f'project_{project}' / 'per_project.json'
    if not pp.exists():
        return {}
    try:
        d: Any = json.load(pp.open())
        if isinstance(d, list):
            d = d[0] if d else {}
        return {
            'before': (d.get('jacoco_before') or {}).get('tests_total'),
            'after': (d.get('jacoco_after') or {}).get('tests_total'),
        }
    except Exception:
        return {}


def _load_naive_tests(project: str) -> Dict[str, Any]:
    pp = NAIVE_DIR / f'project_{project}' / 'per_project.json'
    if not pp.exists():
        return {}
    try:
        d: Any = json.load(pp.open())
        if isinstance(d, list):
            d = d[0] if d else {}
        return {
            'before': (d.get('jacoco_before') or {}).get('tests_total'),
            'after': (d.get('jacoco_after') or {}).get('tests_total'),
        }
    except Exception:
        return {}


def _count_pristine(project: str) -> int:
    """Count @Tests in the pristine SF110 project's test trees."""
    sf = SF110_ROOT / project
    n = 0
    for sub in ('src/test/java', 'evosuite-tests'):
        n += _count_at_test_in_tree(sf / sub)
    return n


def _count_full_workdir(project: str) -> int:
    """Count @Tests in Phase-4-Full's post-run workdir.

    Phase 4 writes the final workdir under
    `output/runs/phase4_main/project_<proj>/<proj>/`.
    """
    work = FULL_DIR / f'project_{project}' / project
    n = 0
    for sub in ('src/test/java', 'evosuite-tests'):
        n += _count_at_test_in_tree(work / sub)
    return n


def _count_naive_workdir(project: str) -> int:
    """Phase-B-Naive's workdir; layout mirrors Phase 4."""
    # Naive runner writes to {out}/project_<proj>/<proj>/ (cli_v2 out)
    work = NAIVE_DIR / f'project_{project}' / project
    n = 0
    for sub in ('src/test/java', 'evosuite-tests'):
        n += _count_at_test_in_tree(work / sub)
    return n


def _utref_workdir(project: str) -> Optional[Path]:
    proj_artefacts = RUN_DIR / 'by_project_tree' / 'by_project' / project
    if not proj_artefacts.exists():
        return None
    runs = sorted(proj_artefacts.glob('run_*'))
    if not runs:
        return None
    work = runs[-1] / 'workdir' / project
    return work if work.exists() else None


def _count_utref(project: str) -> int:
    work = _utref_workdir(project)
    if work is None:
        return 0
    n = 0
    for sub in ('src/test/java', 'evosuite-tests'):
        n += _count_at_test_in_tree(work / sub)
    return n


def main() -> int:
    sel = {r['project']: r for r in csv.DictReader(SELECTION_CSV.open())}
    projects = list(sel.keys())

    rows: List[Dict[str, Any]] = []
    for proj in projects:
        row: Dict[str, Any] = {
            'project': proj,
            'bin': sel[proj]['bin'],
        }
        # Pristine = ripgrep on SF110 tree
        pristine = _count_pristine(proj)
        # Phase-4 reported tests_total (from jacoco)
        full_tests = _load_full_tests(proj)
        naive_tests = _load_naive_tests(proj)

        row['pristine_rg'] = pristine
        row['pristine_jacoco_phase4'] = full_tests.get('before')
        row['full_after_jacoco'] = full_tests.get('after')
        row['full_after_rg'] = _count_full_workdir(proj)
        row['naive_after_jacoco'] = naive_tests.get('after')
        row['naive_after_rg'] = _count_naive_workdir(proj)
        row['utref_after_rg'] = _count_utref(proj)

        # Use ripgrep-on-workdir numbers for consistency (all three from
        # source files). Deltas vs pristine_rg.
        def _delta(x: int) -> Optional[int]:
            return (x - pristine) if pristine else None

        row['delta_full'] = _delta(row['full_after_rg'])
        row['delta_naive'] = _delta(row['naive_after_rg'])
        row['delta_utref'] = _delta(row['utref_after_rg'])
        row['utref_ratio'] = round(
            row['utref_after_rg'] / pristine, 3) if pristine else None
        rows.append(row)

    # CSV
    headers = ['project', 'bin',
               'pristine_rg', 'pristine_jacoco_phase4',
               'full_after_jacoco', 'full_after_rg', 'delta_full',
               'naive_after_jacoco', 'naive_after_rg', 'delta_naive',
               'utref_after_rg', 'delta_utref', 'utref_ratio']
    out_csv = RUN_DIR / 'method_count_analysis.csv'
    with out_csv.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Markdown
    lines: List[str] = []
    lines.append('# Phase C follow-up — test-method-count analysis (3-way)')
    lines.append('')
    lines.append(
        '**Question.** Does UTRefactor achieve its per-project smell '
        'reduction by genuinely repairing tests, or by splitting each '
        'original test into many small `@Test`s so each carries fewer '
        'smells?')
    lines.append('')
    lines.append(
        '**Method.** Count `@Test`-annotated methods in '
        '`src/test/java/` ∪ `evosuite-tests/` for each project, under '
        'three states: the pristine SF110 tree, Phase-4-Full\'s '
        'post-run workdir, and Phase-C-UTRefactor\'s post-run workdir. '
        'Counts exclude `*_scaffolding.java`.')
    lines.append('')
    lines.append(
        '**Method-count table (completed projects only; timeouts '
        'excluded from UTRefactor column).**')
    lines.append('')
    lines.append(
        '| project | bin | pristine | Full after | Δ Full | Naive after | Δ Naive | UTRef after | Δ UTRef | UTRef ratio |'
    )
    lines.append(
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|'
    )
    for r in rows:
        utref = r['utref_after_rg']
        utref_d = r['delta_utref']
        utref_r = r['utref_ratio']
        utref_str = f'{utref}' if utref else '**(timeout)**'
        utref_d_str = f'{utref_d:+d}' if utref_d is not None and utref else '—'
        utref_r_str = f'{utref_r:.2f}×' if utref_r and utref else '—'
        lines.append(
            f'| {r["project"]} | {r["bin"]} | {r["pristine_rg"]} '
            f'| {r["full_after_rg"]} | {r["delta_full"]:+d} '
            f'| {r["naive_after_rg"]} | {r["delta_naive"]:+d} '
            f'| {utref_str} | {utref_d_str} | {utref_r_str} |'
        )

    # Aggregate
    completed = [r for r in rows if r['utref_after_rg']]
    if completed:
        mean_ratio = sum(r['utref_ratio'] for r in completed) / len(completed)
        median_ratio = sorted(r['utref_ratio'] for r in completed)[len(completed) // 2]
        above_1 = [r for r in completed if r['utref_ratio'] > 1.05]
        lines.append('')
        lines.append('**Aggregate (12 UTRefactor-completed projects).**')
        lines.append('')
        lines.append(f'- Mean UTRef / pristine ratio: **{mean_ratio:.2f}×**')
        lines.append(f'- Median UTRef / pristine ratio: **{median_ratio:.2f}×**')
        lines.append(
            f'- Projects with ratio > 1.05 (≥5 % more @Tests than pristine): '
            f'**{len(above_1)} / {len(completed)}**'
        )
        if above_1:
            lines.append('')
            lines.append('Projects where UTRefactor *increased* the test-method count:')
            for r in sorted(above_1, key=lambda x: -x['utref_ratio']):
                lines.append(
                    f'  - `{r["project"]}`: pristine {r["pristine_rg"]} → '
                    f'utref {r["utref_after_rg"]} (**{r["utref_ratio"]:.2f}×**)'
                )

    lines.append('')
    lines.append('## Interpretation')
    lines.append('')
    lines.append(
        'A ratio materially above 1.0 is direct evidence of UTRefactor\'s '
        'per-method splitting step: an original `@Test` is turned into N '
        'smaller `@Test`s (each exercising one assertion cluster). '
        'Smell counts are *per method*, so if one large method with 5 '
        'OIMT-flaggable patterns is split into 5 methods of 1 pattern '
        'each, the aggregate OIMT count stays the same but the '
        'number of *methods flagged* changes. Conversely, if UTRefactor '
        'genuinely removed smells the after-count of smelly-flagged '
        'tests would drop even though method count holds.')
    lines.append('')
    lines.append(
        'Combined with the §6 per-smell substitution table from '
        '`utrefactor_summary.md`, this lets us distinguish:')
    lines.append(
        '- **genuine repair** (method count roughly preserved, smell '
        'count down) — the Full condition\'s profile, and the "good" '
        'outcome.\n'
        '- **splitting-driven apparent reduction** (method count up, '
        'smell count down proportionally) — UTRefactor\'s ratio > 1 '
        'projects. The paper should treat these cases with scepticism.'
    )

    (RUN_DIR / 'method_count_analysis.md').write_text(
        '\n'.join(lines) + '\n', encoding='utf-8'
    )
    print(f'wrote: {RUN_DIR / "method_count_analysis.md"}')
    print(f'wrote: {out_csv}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

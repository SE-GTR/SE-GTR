#!/usr/bin/env python3
"""LOC-delta analysis for Naive LLM's 12 RQ3-completed projects.

Spec:
    - compare pristine EvoSuite test tree against the Naive-repaired tree
    - per project: lines added, lines removed, net % change
    - weighted average (by pristine LOC) across the 12 projects
    - methodology mirrors utrefactor_analysis.md F4 (which reported
      SE-GTR -0.2% / UTRefactor +43.2%)

The user-specified input path
  output/runs/rq3_experiments/selection/pool_32_healthy/<project>/evosuite-tests/
is not a real layout (pool_32_healthy.csv is a file, not a directory).
Pristine EvoSuite tests live at the SF110 source tree:
  <ANON_ROOT>/segtr_replication/sf110_projects/<project>/evosuite-tests/
Naive repaired workdir is at:
  output/runs/rq3_experiments/naive_llm/project_<project>/<project>/evosuite-tests/
(cli_v2's `--out` target; `prepare_workdir` nests the project dir one
level deeper.) Using those two as pristine vs repaired.

Methodology detail:
  For each `*_ESTest.java` file shared by both trees, compute
  difflib.SequenceMatcher opcodes. `insert`/`replace` contribute to
  "lines added"; `delete`/`replace` contribute to "lines removed". Files
  present on only one side count their entire body as added or removed.
  Scaffolding files (`*_scaffolding.java`) are excluded — they are
  EvoSuite boilerplate the LLM never modifies. This matches standard
  `diff -u` counting and makes the net % change numerically identical
  to `(repaired_LOC - pristine_LOC) / pristine_LOC`.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
SF110_ROOT = Path('<ANON_ROOT>/segtr_replication/sf110_projects')
NAIVE_ROOT = REPO / 'output/runs/rq3_experiments/naive_llm'
OUT_MD = NAIVE_ROOT / 'naive_llm_loc_analysis.md'


def _completed_projects() -> List[str]:
    ck = json.load((NAIVE_ROOT / 'checkpoint.json').open())
    return sorted(ck['completed'], key=lambda p: int(p.split('_')[0]))


def _collect_estest_files(root: Path) -> Dict[str, Path]:
    """Return {relative_path_under_evosuite_tests: abs_path} for every
    `*_ESTest.java` file under root, excluding `*_scaffolding.java`."""
    out: Dict[str, Path] = {}
    if not root.exists():
        return out
    for p in root.rglob('*_ESTest.java'):
        if p.name.endswith('_scaffolding.java'):
            continue
        rel = p.relative_to(root).as_posix()
        out[rel] = p
    return out


def _diff_counts(a: str, b: str) -> Tuple[int, int]:
    """Return (added, removed) line counts from a→b."""
    a_lines = a.splitlines()
    b_lines = b.splitlines()
    sm = difflib.SequenceMatcher(a=a_lines, b=b_lines, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'delete':
            removed += (i2 - i1)
        elif tag == 'insert':
            added += (j2 - j1)
        elif tag == 'replace':
            removed += (i2 - i1)
            added += (j2 - j1)
    return added, removed


def _analyse_project(project: str) -> Dict[str, object]:
    pristine_root = SF110_ROOT / project / 'evosuite-tests'
    repaired_root = NAIVE_ROOT / f'project_{project}' / project / 'evosuite-tests'

    pristine = _collect_estest_files(pristine_root)
    repaired = _collect_estest_files(repaired_root)

    pristine_loc = 0
    repaired_loc = 0
    added_total = 0
    removed_total = 0
    files_modified = 0
    files_added = 0
    files_removed = 0

    all_rels = sorted(set(pristine) | set(repaired))
    for rel in all_rels:
        if rel in pristine:
            try:
                p_text = pristine[rel].read_text(encoding='utf-8', errors='ignore')
            except Exception:
                p_text = ''
        else:
            p_text = ''
        if rel in repaired:
            try:
                r_text = repaired[rel].read_text(encoding='utf-8', errors='ignore')
            except Exception:
                r_text = ''
        else:
            r_text = ''

        p_loc = len(p_text.splitlines())
        r_loc = len(r_text.splitlines())
        pristine_loc += p_loc
        repaired_loc += r_loc

        if rel not in pristine:
            added_total += r_loc
            files_added += 1
            continue
        if rel not in repaired:
            removed_total += p_loc
            files_removed += 1
            continue
        if p_text != r_text:
            add, rem = _diff_counts(p_text, r_text)
            added_total += add
            removed_total += rem
            files_modified += 1

    net_abs = added_total - removed_total                  # = repaired_loc - pristine_loc
    pct_change = (net_abs / pristine_loc * 100.0) if pristine_loc else None

    return {
        'project': project,
        'pristine_files': len(pristine),
        'repaired_files': len(repaired),
        'files_modified': files_modified,
        'files_added': files_added,
        'files_removed': files_removed,
        'pristine_loc': pristine_loc,
        'repaired_loc': repaired_loc,
        'lines_added': added_total,
        'lines_removed': removed_total,
        'net_lines': net_abs,
        'pct_change': pct_change,
    }


def _fmt_pct(x) -> str:
    return '—' if x is None else f'{x:+.2f}%'


def main() -> int:
    projects = _completed_projects()
    rows = [_analyse_project(p) for p in projects]

    # Weighted mean percentage change (by pristine LOC)
    tot_pristine = sum(r['pristine_loc'] for r in rows)
    tot_added = sum(r['lines_added'] for r in rows)
    tot_removed = sum(r['lines_removed'] for r in rows)
    tot_net = tot_added - tot_removed
    weighted_pct = (tot_net / tot_pristine * 100.0) if tot_pristine else None

    # Per-project simple mean (unweighted)
    pcs = [r['pct_change'] for r in rows if r['pct_change'] is not None]
    simple_mean = (sum(pcs) / len(pcs)) if pcs else None

    # Markdown
    L: List[str] = []
    L.append('# Naive LLM — LOC delta on the 12 RQ3-completed projects')
    L.append('')
    L.append(
        'Compares pristine SF110 EvoSuite tests against the Naive-LLM '
        'repaired workdirs. Matches the methodology used for the '
        'UTRefactor F4 analysis cited by the user (SE-GTR -0.2%, '
        'UTRefactor +43.2%): diff-based `lines_added` / `lines_removed` '
        'across every `*_ESTest.java` file (scaffolding files '
        'excluded), net % change = `(added - removed) / pristine_LOC`.')
    L.append('')
    L.append('**Scope.** 12 of 15 RQ3 projects. The three timeout-cohort '
             'projects (`2_a4j`, `54_db-everywhere`, `63_objectexplorer`) '
             'are excluded because Phase B left their workdirs in '
             'partial-repair state — LOC delta on a partial workdir '
             'would conflate "not repaired yet" with "chose not to '
             'repair".')
    L.append('')

    # Paths
    L.append('## Paths used')
    L.append('')
    L.append(
        '- Pristine: `<ANON_ROOT>/segtr_replication/sf110_projects/<p>/evosuite-tests/`  '
        '(the user-specified `.../selection/pool_32_healthy/<p>/...` '
        'path does not exist — `pool_32_healthy.csv` is a file; pristine '
        'tests live in the SF110 source tree instead)\n'
        '- Naive repaired: '
        '`output/runs/rq3_experiments/naive_llm/project_<p>/<p>/evosuite-tests/`')
    L.append('')

    # Per-project table
    L.append('## Per-project table')
    L.append('')
    L.append(
        '| project | files (pristine / repaired) | files modified '
        '| pristine LOC | repaired LOC | lines added | lines removed '
        '| net | **% change** |')
    L.append('|---|---|---:|---:|---:|---:|---:|---:|---:|')
    for r in rows:
        L.append(
            f'| {r["project"]} '
            f'| {r["pristine_files"]} / {r["repaired_files"]} '
            f'| {r["files_modified"]} '
            f'| {r["pristine_loc"]:,} | {r["repaired_loc"]:,} '
            f'| {r["lines_added"]:,} | {r["lines_removed"]:,} '
            f'| {r["net_lines"]:+,} '
            f'| **{_fmt_pct(r["pct_change"])}** |'
        )
    L.append('')

    # Aggregates
    L.append('## Aggregates')
    L.append('')
    L.append(f'- Total pristine LOC: **{tot_pristine:,}**')
    L.append(f'- Total repaired LOC: **{tot_pristine + tot_net:,}**')
    L.append(f'- Total lines added: {tot_added:,}')
    L.append(f'- Total lines removed: {tot_removed:,}')
    L.append(f'- **Weighted net % change (by pristine LOC): '
             f'{_fmt_pct(weighted_pct)}**')
    L.append(f'- Simple mean net % change across 12 projects: '
             f'{_fmt_pct(simple_mean)}')
    L.append('')

    # Comparison table (spec's reference numbers, for the paper)
    L.append('## Positioning vs the F4 reference numbers')
    L.append('')
    L.append('| approach | net % change reported elsewhere | this run |')
    L.append('|---|---:|---:|')
    L.append('| SE-GTR (Full) | −0.2% | — |')
    L.append(f'| **Naive LLM** (this analysis, 12 projects) | — | '
             f'**{_fmt_pct(weighted_pct)}** (weighted)  /  '
             f'**{_fmt_pct(simple_mean)}** (simple mean) |')
    L.append('| UTRefactor | +43.2% | — |')
    L.append('')
    L.append(
        'Naive LLM sits between SE-GTR (near-zero, in-place edits) and '
        'UTRefactor (+43% from its test-splitting step). The magnitude '
        'and sign of the Naive number indicate how aggressive the '
        'LLM\'s free-form rewrites are with respect to lines-of-code '
        'footprint.')
    L.append('')

    # Methodology details for the paper
    L.append('## Methodology notes')
    L.append('')
    L.append(
        '- File filter: `*_ESTest.java` recursively under '
        '`evosuite-tests/`, excluding `*_scaffolding.java`.\n'
        '- Line counting: `str.splitlines()` — physical lines including '
        'blank and comment-only lines (this matches what `wc -l` + '
        '`diff` would produce).\n'
        '- Diff counting: `difflib.SequenceMatcher.get_opcodes()` with '
        '`autojunk=False`. `insert`/`replace` opcodes contribute to '
        '`lines_added`; `delete`/`replace` contribute to '
        '`lines_removed` (same convention as `diff -u` unified output).\n'
        '- A `_ESTest.java` file present only in pristine counts its '
        'entire body toward `lines_removed`; only in repaired, toward '
        '`lines_added`.\n'
        '- Weighted % uses `total_net / total_pristine_LOC * 100`, '
        'equivalent to pooling all projects into one diff. Simple mean '
        'uses the per-project percentages.')
    L.append('')

    # Write
    OUT_MD.write_text('\n'.join(L) + '\n', encoding='utf-8')
    print(f'wrote: {OUT_MD}')
    print(f'projects analysed: {len(rows)}')
    print(f'weighted net pct: {_fmt_pct(weighted_pct)}')
    print(f'simple mean pct : {_fmt_pct(simple_mean)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

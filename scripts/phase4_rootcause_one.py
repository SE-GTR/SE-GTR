#!/usr/bin/env python3
"""Root-cause analysis for one Phase-4 PIT outlier project.

For the target project:
  1. Diff pristine vs v2-repaired evosuite-tests at the test-method level.
  2. Classify each method-level change into operator-style buckets
     (pattern A–F) based on the diff shape.
  3. Enumerate v2 SURVIVED mutants whose mutatedClass matches a
     modified CUT (class-level match with corresponding *_ESTest).
  4. Emit a project-specific markdown dossier:
       output/runs/phase4_main/pit/root_cause/<project>_analysis.md

Patterns:
  A — NNA removal (at least one `assertNotNull` line disappeared)
  B — try-catch scaffolding gone (raw `try {` / `catch (...) {` disappeared)
  C — assertion text replaced (assert* line rewritten, no net delete)
  D — assertion deleted (OIMT / duplicate-init strip)
  E — new assertion inserted (INSERT_ASSERTION / NARV CAPTURE)
  F — other (blank / setup-only / unclassified)
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO = Path('<ANON_ROOT>/segtr_replication/llm_smelly_repair_impl')
PRISTINE = Path('<ANON_ROOT>/segtr_replication/sf110_projects')
OUT = REPO / 'output' / 'runs' / 'phase4_main' / 'pit' / 'root_cause'

TEST_METHOD_RE = re.compile(
    r'(?ms)^(\s*@Test[^\n]*\n)?\s*public\s+void\s+(\w+)\s*\([^)]*\)\s*'
    r'(?:throws[^{]+)?\{',
)

MUTATION_XPATH_FIELDS = ('mutatedClass', 'mutatedMethod', 'lineNumber',
                         'mutator', 'status')


# ---------------------------------------------------------------------------
# Method extraction
# ---------------------------------------------------------------------------


def extract_methods(source: str) -> Dict[str, str]:
    """Return {method_name: full_method_text} by brace-walking."""
    out: Dict[str, str] = {}
    for m in TEST_METHOD_RE.finditer(source):
        name = m.group(2)
        body_open = source.find('{', m.end() - 1)
        depth = 0
        i = body_open
        while i < len(source):
            c = source[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    out[name] = source[m.start(): i + 1]
                    break
            i += 1
    return out


# ---------------------------------------------------------------------------
# Per-method pattern classification
# ---------------------------------------------------------------------------


@dataclass
class MethodChange:
    test_class: str
    method: str
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)


def classify_change(mc: MethodChange) -> None:
    """Populate mc.patterns based on added/removed line shape."""
    removed = '\n'.join(mc.removed_lines)
    added = '\n'.join(mc.added_lines)

    if 'assertNotNull' in removed and 'assertNotNull' not in added:
        mc.patterns.append('A_NNA_removal')

    try_removed = re.search(r'\btry\s*\{', removed) is not None
    catch_removed = re.search(r'\bcatch\s*\(', removed) is not None
    try_in_added = re.search(r'\btry\s*\{', added) is not None
    if (try_removed or catch_removed) and not try_in_added:
        mc.patterns.append('B_try_catch_gone')

    asserts_removed = len(re.findall(r'\bassert\w+\s*\(', removed))
    asserts_added = len(re.findall(r'\bassert\w+\s*\(', added))
    if asserts_removed > 0 and asserts_added > 0 \
            and asserts_removed == asserts_added:
        mc.patterns.append('C_assertion_replaced')

    if asserts_removed > asserts_added and 'A_NNA_removal' not in mc.patterns:
        mc.patterns.append('D_assertion_deleted')

    if asserts_added > asserts_removed:
        mc.patterns.append('E_assertion_inserted')

    if not mc.patterns:
        mc.patterns.append('F_other')


def diff_files(pristine_path: Path, v2_path: Path,
               test_class: str) -> List[MethodChange]:
    p_text = pristine_path.read_text(encoding='utf-8', errors='ignore')
    v_text = v2_path.read_text(encoding='utf-8', errors='ignore')
    p_methods = extract_methods(p_text)
    v_methods = extract_methods(v_text)
    changes: List[MethodChange] = []
    for name in sorted(set(p_methods) | set(v_methods)):
        p_body = p_methods.get(name, '')
        v_body = v_methods.get(name, '')
        if p_body == v_body:
            continue
        mc = MethodChange(test_class=test_class, method=name)
        diff = unified_diff(
            p_body.splitlines(), v_body.splitlines(), lineterm='', n=0,
        )
        for line in diff:
            if line.startswith(('+++', '---', '@@')):
                continue
            if line.startswith('+'):
                mc.added_lines.append(line[1:])
            elif line.startswith('-'):
                mc.removed_lines.append(line[1:])
        classify_change(mc)
        changes.append(mc)
    return changes


# ---------------------------------------------------------------------------
# PIT mutation load
# ---------------------------------------------------------------------------


@dataclass
class Mutant:
    cls: str
    method: str
    line: str
    mutator: str
    status: str


def load_mutants(xml_path: Path) -> List[Mutant]:
    tree = ET.parse(xml_path)
    out = []
    for m in tree.getroot().iter('mutation'):
        out.append(Mutant(
            cls=m.findtext('mutatedClass') or '',
            method=m.findtext('mutatedMethod') or '',
            line=m.findtext('lineNumber') or '',
            mutator=m.findtext('mutator') or '',
            status=m.get('status') or '',
        ))
    return out


# ---------------------------------------------------------------------------
# CUT ↔ *_ESTest mapping
# ---------------------------------------------------------------------------


def estest_for_cut(cut_fqcn: str) -> str:
    """Conventional EvoSuite: `pkg.Foo` → `pkg.Foo_ESTest`."""
    return cut_fqcn + '_ESTest'


def cut_for_estest(estest_fqcn: str) -> str:
    if estest_fqcn.endswith('_ESTest'):
        return estest_fqcn[:-len('_ESTest')]
    return estest_fqcn


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_project(project: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    pristine_test_root = PRISTINE / project / 'evosuite-tests'
    v2_test_root = (REPO / 'output' / 'runs' / 'phase4_main'
                    / f'project_{project}' / project / 'evosuite-tests')
    v2_mutations = (REPO / 'output' / 'runs' / 'phase4_main' / 'pit'
                    / 'per_project' / project / 'mutations.xml')

    if not v2_mutations.exists():
        raise SystemExit(f'v2 mutations.xml missing for {project}')

    # 1) Enumerate modified test files
    import subprocess
    proc = subprocess.run(
        ['diff', '-rq', str(pristine_test_root), str(v2_test_root)],
        stdout=subprocess.PIPE, text=True,
    )
    differing: List[Tuple[Path, Path]] = []
    for line in proc.stdout.splitlines():
        if line.startswith('Files ') and line.endswith(' differ'):
            # "Files <pristine_path> and <v2_path> differ"
            parts = line[len('Files '):-len(' differ')].split(' and ')
            if len(parts) == 2:
                differing.append((Path(parts[0]), Path(parts[1])))

    # 2) Method-level classification
    all_changes: List[MethodChange] = []
    for p, v in differing:
        if not p.name.endswith('_ESTest.java'):
            continue
        rel = v.relative_to(v2_test_root)
        test_class = rel.with_suffix('').as_posix().replace('/', '.')
        for mc in diff_files(p, v, test_class):
            all_changes.append(mc)

    # 3) Modified-CUT set (by stripping _ESTest)
    modified_tests: Set[str] = {mc.test_class for mc in all_changes}
    modified_cuts: Set[str] = {cut_for_estest(t) for t in modified_tests}

    # 4) Load mutants + partition by class / status
    mutants = load_mutants(v2_mutations)
    all_survived = [m for m in mutants if m.status == 'SURVIVED']
    survived_in_modified = [m for m in all_survived if m.cls in modified_cuts]

    # 5) Pattern frequency
    from collections import Counter
    pattern_count: Counter[str] = Counter()
    pattern_methods: Dict[str, List[MethodChange]] = {}
    for mc in all_changes:
        for p in mc.patterns:
            pattern_count[p] += 1
            pattern_methods.setdefault(p, []).append(mc)

    # 6) Overlay: which modified CUT has how many survived mutants
    from collections import Counter as C2
    cut_survived_count = C2(m.cls for m in survived_in_modified)

    # 7) Per-modified-method, count survived-in-its-CUT (coarse)
    method_to_cut_survived: Dict[Tuple[str, str], int] = {}
    for mc in all_changes:
        cut = cut_for_estest(mc.test_class)
        method_to_cut_survived[(mc.test_class, mc.method)] = \
            cut_survived_count.get(cut, 0)

    # --- Report ---
    lines: List[str] = [
        f'# Root cause analysis — `{project}`',
        '',
        f'Pristine test root: `{pristine_test_root}`',
        f'v2 test root: `{v2_test_root}`',
        f'v2 mutations.xml: `{v2_mutations}`',
        '',
        '## Scope',
        '',
        f'- Test files differing: **{len(differing)}**',
        f'- Modified test methods: **{len(all_changes)}**',
        f'- Modified distinct test classes (= CUTs): **{len(modified_cuts)}**',
        f'- Total mutants (v2): **{len(mutants)}**  '
        f'(SURVIVED={len(all_survived)}, KILLED={sum(1 for m in mutants if m.status == "KILLED")})',
        f'- Mutants **in modified CUTs**: '
        f'**{sum(1 for m in mutants if m.cls in modified_cuts)}**  '
        f'(SURVIVED {len(survived_in_modified)})',
        '',
        '## Pattern distribution over modified methods',
        '',
        '| pattern | methods | share |',
        '|---|---:|---:|',
    ]
    total_mc = len(all_changes)
    for pat in ('A_NNA_removal', 'B_try_catch_gone',
                'C_assertion_replaced', 'D_assertion_deleted',
                'E_assertion_inserted', 'F_other'):
        n = pattern_count.get(pat, 0)
        pct = (n / total_mc * 100.0) if total_mc else 0.0
        lines.append(f'| {pat} | {n} | {pct:.1f}% |')

    # Modified CUTs × survived weight
    lines.extend([
        '',
        '## Modified CUTs and their survived-mutant load (top 20)',
        '',
        '| test_class | survived mutants in CUT |',
        '|---|---:|',
    ])
    for cls, n in cut_survived_count.most_common(20):
        lines.append(f'| {estest_for_cut(cls)} | {n} |')

    # Pattern-specific sample method diffs (first 3 per pattern)
    lines.append('\n## Representative diffs (≤3 per pattern)\n')
    for pat in ('A_NNA_removal', 'B_try_catch_gone',
                'C_assertion_replaced', 'D_assertion_deleted',
                'E_assertion_inserted', 'F_other'):
        lines.append(f'### {pat}')
        samples = pattern_methods.get(pat, [])[:3]
        for mc in samples:
            surv = method_to_cut_survived.get((mc.test_class, mc.method), 0)
            lines.append('')
            lines.append(f'**{mc.test_class}::{mc.method}** '
                         f'(survived-in-CUT: {surv})')
            if mc.removed_lines:
                lines.append('```diff')
                for l in mc.removed_lines[:8]:
                    lines.append(f'- {l}')
                for l in mc.added_lines[:8]:
                    lines.append(f'+ {l}')
                lines.append('```')
        if not samples:
            lines.append('\n*(no samples for this pattern)*')
        lines.append('')

    # Summary of suspect attribution
    lines.extend([
        '## Attribution summary',
        '',
        f'Of **{len(all_survived)}** total v2 SURVIVED mutants, '
        f'**{len(survived_in_modified)}** lie inside a CUT whose '
        f'*_ESTest was modified by Phase-4 pipeline '
        f'(= {len(survived_in_modified)/max(1,len(all_survived))*100:.1f}%).',
        '',
        'These are the mutants most likely to have lost a kill due to '
        'the repair. Pattern distribution above quantifies which '
        'operator family dominates the weakening.',
        '',
    ])

    out_path = OUT / f'{project}_analysis.md'
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'wrote: {out_path}')

    # Also dump a short machine-readable summary for later aggregation
    import json
    summary = {
        'project': project,
        'differing_files': len(differing),
        'modified_methods': len(all_changes),
        'modified_cuts': sorted(modified_cuts),
        'total_mutants': len(mutants),
        'survived_total': len(all_survived),
        'killed_total': sum(1 for m in mutants if m.status == "KILLED"),
        'survived_in_modified_cuts': len(survived_in_modified),
        'pattern_count': dict(pattern_count),
    }
    (OUT / f'{project}_summary.json').write_text(
        json.dumps(summary, indent=2), encoding='utf-8'
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('project')
    a = ap.parse_args()
    analyze_project(a.project)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

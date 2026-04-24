from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


JsonObj = Dict[str, Any]


@dataclass(frozen=True)
class EvidenceRender:
    """Rendered evidence + an evidence-driven repair plan template."""

    smell_id: str
    compact_json: JsonObj
    plan: str


def _truncate_str(s: Any, max_len: int) -> Any:
    if not isinstance(s, str):
        return s
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _limit_list(v: Any, max_items: int) -> Any:
    if not isinstance(v, list):
        return v
    return v[:max_items]


def _compact_range(d: JsonObj) -> JsonObj:
    out: JsonObj = {}
    for k in ("begin_line", "begin_col", "end_line", "end_col"):
        if k in d:
            out[k] = d[k]
    return out


def _compact_call(call: Any, *, max_str_len: int = 220) -> Any:
    if not isinstance(call, dict):
        return call
    keep = [
        "expr",
        "name",
        "scope",
        "args",
        "declaring_type",
        "signature",
        "return_type",
    ]
    out: JsonObj = {}
    for k in keep:
        if k in call and call[k] is not None:
            out[k] = call[k]
    # shrink strings
    for k, v in list(out.items()):
        if isinstance(v, str):
            out[k] = _truncate_str(v, max_str_len)
        elif isinstance(v, list):
            out[k] = [_truncate_str(x, max_str_len) for x in v]
    out.update(_compact_range(call))
    return out


def _compact_ctor(ctor: Any, *, max_str_len: int = 220) -> Any:
    if not isinstance(ctor, dict):
        return ctor
    keep = ["expr", "type", "args", "resolved_type"]
    out: JsonObj = {}
    for k in keep:
        if k in ctor and ctor[k] is not None:
            out[k] = ctor[k]
    for k, v in list(out.items()):
        if isinstance(v, str):
            out[k] = _truncate_str(v, max_str_len)
        elif isinstance(v, list):
            out[k] = [_truncate_str(x, max_str_len) for x in v]
    out.update(_compact_range(ctor))
    return out


def compact_evidence_for_prompt(
    smell_id: str,
    evidence: Optional[JsonObj],
    *,
    max_list_items: int = 6,
    max_group_tests: int = 10,
    max_prefix_stmts: int = 2,
    max_str_len: int = 240,
) -> JsonObj:
    """Return a compact, smell-aware JSON object suitable to embed in prompts.

    The goal is to (1) preserve the most actionable info (callsites/fields/groups) and
    (2) keep the prompt token budget bounded.
    """

    if not evidence:
        return {}

    e = evidence

    # --- Group-based smells ---
    if smell_id == "DS":
        groups = []
        for g in _limit_list(e.get("duplicated_setup_groups", []), max_list_items) or []:
            if not isinstance(g, dict):
                continue
            groups.append(
                {
                    "group_id": g.get("group_id"),
                    "group_size": g.get("group_size"),
                    "group_tests": _limit_list(g.get("group_tests", []), max_group_tests),
                    "prefix_statements": _limit_list(g.get("prefix_statements", []), max_prefix_stmts),
                }
            )
        return {"duplicated_setup_groups": groups}

    if smell_id == "TSES":
        groups = []
        for g in _limit_list(e.get("same_exception_scenario_groups", []), max_list_items) or []:
            if not isinstance(g, dict):
                continue
            groups.append(
                {
                    "group_id": g.get("group_id"),
                    "group_size": g.get("group_size"),
                    "exception_type": g.get("exception_type"),
                    "group_tests": _limit_list(g.get("group_tests", []), max_group_tests),
                    "rule": _truncate_str(g.get("rule"), max_str_len),
                }
            )
        return {"same_exception_scenario_groups": groups}

    if smell_id == "TSVM":
        groups = []
        for g in _limit_list(e.get("same_void_method_groups", []), max_list_items) or []:
            if not isinstance(g, dict):
                continue
            groups.append(
                {
                    "group_id": g.get("group_id"),
                    "void_method_name": g.get("void_method_name"),
                    "group_size": g.get("group_size"),
                    "group_tests": _limit_list(g.get("group_tests", []), max_group_tests),
                }
            )
        return {"same_void_method_groups": groups}

    # --- Callsite / field / assert driven smells ---
    if smell_id == "NARV":
        calls = []
        for c in _limit_list(e.get("unasserted_return_calls", []), max_list_items) or []:
            calls.append(_compact_call(c, max_str_len=max_str_len))
        return {"unasserted_return_calls": calls}

    if smell_id == "NASE":
        items = []
        for it in _limit_list(e.get("unverified_side_effect_calls", []), max_list_items) or []:
            if not isinstance(it, dict):
                continue
            items.append(
                {
                    "act_call": _compact_call(it.get("act_call"), max_str_len=max_str_len),
                    "called_method": it.get("called_method"),
                    "assignment_count": it.get("assignment_count"),
                    "modified_fields": _limit_list(it.get("modified_fields", []), max_list_items),
                }
            )
        return {"unverified_side_effect_calls": items}

    if smell_id == "ARPM":
        items = []
        for it in _limit_list(e.get("arpm_assertions", []), max_list_items) or []:
            if not isinstance(it, dict):
                continue
            items.append(
                {
                    "assertion_call": _compact_call(it.get("assertion_call"), max_str_len=max_str_len),
                    "cut_call": _compact_call(it.get("cut_call"), max_str_len=max_str_len),
                    "cut_declaring_type": it.get("cut_declaring_type"),
                    "ancestor_declaring_type": it.get("ancestor_declaring_type"),
                    "reason": it.get("reason"),
                    "return_name": it.get("return_name"),
                    "return_changed_during_test": it.get("return_changed_during_test"),
                }
            )
        return {"arpm_assertions": items}

    if smell_id == "TOFA":
        calls = []
        for c in _limit_list(e.get("calls", []), max_list_items) or []:
            if not isinstance(c, dict):
                continue
            cc = _compact_call(c, max_str_len=max_str_len)
            if isinstance(cc, dict):
                if "kind" in c:
                    cc["kind"] = c.get("kind")
            calls.append(cc)
        out: JsonObj = {"non_assert_call_count": e.get("non_assert_call_count"), "calls": calls}
        return out

    if smell_id == "AC":
        items = []
        for it in _limit_list(e.get("constant_assertions", []), max_list_items) or []:
            if not isinstance(it, dict):
                continue
            items.append(
                {
                    "assert": _truncate_str(it.get("assert"), max_str_len),
                    "assert_method": it.get("assert_method"),
                    "constant": _truncate_str(it.get("constant"), max_str_len),
                    **_compact_range(it),
                }
            )
        return {"constant_assertions": items}

    if smell_id == "NNA":
        items = []
        for it in _limit_list(e.get("redundant_not_null_assertions", []), max_list_items) or []:
            if not isinstance(it, dict):
                continue
            items.append(
                {
                    "assert": _truncate_str(it.get("assert"), max_str_len),
                    "variable": it.get("variable"),
                    "redundant_because_new_object": it.get("redundant_because_new_object"),
                    "redundant_because_other_assert": it.get("redundant_because_other_assert"),
                    **_compact_range(it),
                }
            )
        return {"redundant_not_null_assertions": items}

    if smell_id == "ENET":
        out: JsonObj = {}
        out["first_statement_is_try"] = e.get("first_statement_is_try")

        tcs = []
        for it in _limit_list(e.get("try_catch_blocks", []), max_list_items) or []:
            if not isinstance(it, dict):
                continue
            tcs.append(
                {
                    "catch_types": _limit_list(it.get("catch_types", []), max_list_items),
                    **_compact_range(it),
                }
            )
        out["try_catch_blocks"] = tcs

        sites = []
        for s in _limit_list(e.get("null_argument_sites", []), max_list_items) or []:
            if not isinstance(s, dict):
                continue
            entry: JsonObj = {
                "kind": s.get("kind"),
                "arg_index": s.get("arg_index"),
                "arg_expr": _truncate_str(s.get("arg_expr"), max_str_len),
                "in_try": s.get("in_try"),
            }
            if s.get("kind") == "method_call":
                entry["call"] = _compact_call(s.get("call"), max_str_len=max_str_len)
            if s.get("kind") == "constructor_call":
                entry["constructor"] = _compact_ctor(s.get("constructor"), max_str_len=max_str_len)
            sites.append(entry)
        out["null_argument_sites"] = sites
        return out

    if smell_id == "EDED":
        items = []
        for it in _limit_list(e.get("external_dependency_exceptions", []), max_list_items) or []:
            if not isinstance(it, dict):
                continue
            items.append(
                {
                    "matched_exception_type": it.get("matched_exception_type"),
                    "catch_types": _limit_list(it.get("catch_types", []), max_list_items),
                    "try_range": it.get("try_range"),
                }
            )
        return {"external_dependency_exceptions": items}

    if smell_id == "EDIS":
        items = []
        for it in _limit_list(e.get("incomplete_setup_evidence", []), max_list_items) or []:
            if not isinstance(it, dict):
                continue
            items.append(
                {
                    "trigger_call": _compact_call(it.get("trigger_call"), max_str_len=max_str_len),
                    "called_method": it.get("called_method"),
                    "unmodified_variable": it.get("unmodified_variable"),
                    "declared_but_not_initialized": _limit_list(it.get("declared_but_not_initialized", []), max_list_items),
                    "modified_variables": _limit_list(it.get("modified_variables", []), max_list_items),
                }
            )
        return {"incomplete_setup_evidence": items}

    if smell_id == "OIMT":
        out: JsonObj = {}
        if "rules_triggered" in e:
            out["rules_triggered"] = _limit_list(e.get("rules_triggered", []), max_list_items)
        if "shared_init_assert_keys" in e:
            out["shared_init_assert_keys"] = _limit_list(e.get("shared_init_assert_keys", []), max_list_items)

        ocs = []
        for oc in _limit_list(e.get("object_creations", []), max_list_items) or []:
            ocs.append(_compact_ctor(oc, max_str_len=max_str_len))
        if ocs:
            out["object_creations"] = ocs

        acs = []
        for ac in _limit_list(e.get("assert_calls", []), max_list_items) or []:
            acs.append(_compact_call(ac, max_str_len=max_str_len))
        if acs:
            out["assert_calls"] = acs

        ncs = []
        for nc in _limit_list(e.get("nontrivial_calls", []), max_list_items) or []:
            ncs.append(_compact_call(nc, max_str_len=max_str_len))
        if ncs:
            out["nontrivial_calls"] = ncs

        return out

    # Unknown / not yet mapped smell: return shallow-truncated JSON.
    shallow: JsonObj = {}
    for k, v in e.items():
        if isinstance(v, list):
            shallow[k] = _limit_list(v, max_list_items)
        else:
            shallow[k] = _truncate_str(v, max_str_len)
    return shallow


def _plan_from_compact(smell_id: str, c: JsonObj) -> str:
    """Evidence-driven plan templates.

    These are intentionally *templates*, not strict constraints. The goal is to
    align the model's attention with the evidence and reduce wandering edits.
    """

    if smell_id == "NARV":
        calls = c.get("unasserted_return_calls") or []
        lines = [
            "1) For each unasserted return-value call below, store the return value in a local variable.",
            "2) Add at least one deterministic assertion that uses that value (prefer meaningfully checking behavior).",
            "   - boolean -> assertTrue/assertFalse",
            "   - collection/array -> assert size/isEmpty/contains",
            "   - object -> assertNotNull only if you also assert something behavior-related",
        ]
        if calls:
            lines.append("\nCalls to fix:")
            for i, call in enumerate(calls, 1):
                if isinstance(call, dict):
                    lines.append(f"- [{i}] {call.get('expr')} (ret={call.get('return_type')}, line={call.get('begin_line')})")
        return "\n".join(lines)

    if smell_id == "NASE":
        items = c.get("unverified_side_effect_calls") or []
        lines = [
            "1) Identify the side-effect act call(s) listed below.",
            "2) Prefer adding assertions that observe the side effect via public API (getters/size/contains/isEmpty).",
            "3) Use before/after assertions if possible (capture value before act, then compare after).",
            "4) If no observable effect exists, remove/replace only the *specific* act line(s), not the whole test.",
        ]
        if items:
            lines.append("\nSide-effect calls to verify:")
            for i, it in enumerate(items, 1):
                if isinstance(it, dict):
                    act = it.get("act_call") or {}
                    mf = it.get("modified_fields")
                    lines.append(
                        f"- [{i}] act={getattr(act, 'get', lambda _k: None)('expr') if isinstance(act, dict) else act} (modified_fields={mf})"
                    )
        return "\n".join(lines)

    if smell_id == "ARPM":
        items = c.get("arpm_assertions") or []
        lines = [
            "1) Locate the problematic assertion call(s) below.",
            "2) Replace or rewrite the assertion so it checks behavior that is actually affected by the CUT act call.",
            "3) Prefer asserting on the direct return value or an observable post-state related to the act call.",
            "4) Avoid keeping assertions that only check ancestor/parent behavior unrelated to the act.",
        ]
        if items:
            lines.append("\nProblematic assertions:")
            for i, it in enumerate(items, 1):
                if isinstance(it, dict):
                    a = it.get("assertion_call") or {}
                    cut = it.get("cut_call") or {}
                    lines.append(
                        f"- [{i}] assertion={a.get('expr') if isinstance(a, dict) else a} | act={cut.get('expr') if isinstance(cut, dict) else cut} | reason={it.get('reason')}"
                    )
        return "\n".join(lines)

    if smell_id == "TOFA":
        calls = c.get("calls") or []
        lines = [
            "1) This test appears to only exercise trivial getters/setters.",
            "2) Add at least one non-trivial behavior interaction (method that changes state or performs logic), and assert its effect.",
            "3) If only accessors exist, assert a meaningful invariant that cannot be satisfied by constructor args alone.",
        ]
        if calls:
            lines.append("\nAccessor calls observed:")
            for i, call in enumerate(calls, 1):
                if isinstance(call, dict):
                    lines.append(f"- [{i}] {call.get('expr')} (kind={call.get('kind')}, line={call.get('begin_line')})")
        return "\n".join(lines)

    if smell_id == "AC":
        items = c.get("constant_assertions") or []
        lines = [
            "1) Identify assertions that compare or check public static constants unrelated to CUT behavior.",
            "2) Prefer assertions on values produced/affected by the act call (return values or post-state).",
            "3) If a constant is a valid expected value, tie it to a CUT result (e.g., assertEquals(CONSTANT, cut.method(...))).",
        ]
        if items:
            lines.append("\nConstant assertions:")
            for i, it in enumerate(items, 1):
                if isinstance(it, dict):
                    lines.append(f"- [{i}] {it.get('assert')} | constant={it.get('constant')} (line={it.get('begin_line')})")
        return "\n".join(lines)

    if smell_id == "ENET":
        sites = c.get("null_argument_sites") or []
        lines = [
            "1) Identify null argument sites below that trigger NullPointerException.",
            "2) Prefer replacing null with a minimal valid value and assert normal behavior.",
            "3) If null rejection is the intended contract, make the expectation explicit (JUnit4 @Test(expected=...)).",
            "4) Avoid broad catch(Exception) patterns and avoid try/catch that hides failures.",
        ]
        if sites:
            lines.append("\nNull argument sites:")
            for i, s in enumerate(sites, 1):
                if isinstance(s, dict):
                    lines.append(f"- [{i}] kind={s.get('kind')} arg_index={s.get('arg_index')} arg={s.get('arg_expr')} in_try={s.get('in_try')}")
        return "\n".join(lines)

    if smell_id == "EDED":
        items = c.get("external_dependency_exceptions") or []
        lines = [
            "1) This test catches exceptions commonly caused by external dependencies (I/O/network).",
            "2) Prefer removing the external dependency by using local deterministic resources (temp files, in-memory streams) or stubbing/mocking when possible.",
            "3) If the exception is truly expected by the contract, make it explicit and minimal.",
        ]
        if items:
            lines.append("\nMatched exception types:")
            for i, it in enumerate(items, 1):
                if isinstance(it, dict):
                    lines.append(f"- [{i}] matched={it.get('matched_exception_type')} catch_types={it.get('catch_types')}")
        return "\n".join(lines)

    if smell_id == "EDIS":
        items = c.get("incomplete_setup_evidence") or []
        lines = [
            "1) Identify the trigger call(s) and the unmodified/uninitialized variable(s) below.",
            "2) Fix the setup: initialize the missing field/variable before the act call (constructor, setter, factory, or minimal object).",
            "3) After fixing setup, replace try/catch with deterministic assertions on expected behavior when possible.",
        ]
        if items:
            lines.append("\nIncomplete setup evidence:")
            for i, it in enumerate(items, 1):
                if isinstance(it, dict):
                    trig = it.get("trigger_call") or {}
                    lines.append(
                        f"- [{i}] trigger={trig.get('expr') if isinstance(trig, dict) else trig} | unmodified={it.get('unmodified_variable')}"
                    )
        return "\n".join(lines)

    if smell_id == "OIMT":
        lines = [
            "1) If assertions only restate constructor args / default initialization, remove or replace them with behavior-focused assertions.",
            "2) Prefer exercising a non-trivial call and asserting its effect.",
            "3) Keep the test deterministic and avoid adding redundant assertNotNull-only checks.",
        ]
        rt = c.get("rules_triggered")
        if rt:
            lines.append(f"Rules triggered: {rt}")
        nt = c.get("nontrivial_calls")
        if nt:
            lines.append("\nNon-trivial calls present (candidates to assert on):")
            for i, call in enumerate(nt, 1):
                if isinstance(call, dict):
                    lines.append(f"- [{i}] {call.get('expr')} (line={call.get('begin_line')})")
        return "\n".join(lines)

    # Group smells that we may still pass to LLM (if not handled deterministically)
    if smell_id in {"TSES", "TSVM", "DS"}:
        lines = [
            "1) This smell is group-based (involves multiple tests in the same class).",
            "2) Prefer extracting shared code into @Before or helper methods.",
            "3) Since deleting tests is not allowed, try to differentiate each test by focusing on distinct inputs/assertions.",
        ]
        return "\n".join(lines)

    # Default minimal plan
    return "1) Use the evidence JSON to locate the problematic lines.\n2) Apply the smell's repair playbook with minimal, deterministic changes."


def render_evidence_for_prompt(
    smell_id: str,
    evidence: Optional[JsonObj],
    *,
    max_list_items: int = 6,
    max_group_tests: int = 10,
    max_prefix_stmts: int = 2,
    max_str_len: int = 240,
) -> EvidenceRender:
    compact = compact_evidence_for_prompt(
        smell_id,
        evidence,
        max_list_items=max_list_items,
        max_group_tests=max_group_tests,
        max_prefix_stmts=max_prefix_stmts,
        max_str_len=max_str_len,
    )
    plan = _plan_from_compact(smell_id, compact)
    return EvidenceRender(smell_id=smell_id, compact_json=compact, plan=plan)


def evidence_block_markdown(er: EvidenceRender) -> str:
    """Pretty block that can be embedded directly in prompts."""

    compact_json = json.dumps(er.compact_json, indent=2, ensure_ascii=False)
    return (
        f"## {er.smell_id} evidence (Smelly, compact)\n"
        f"```json\n{compact_json}\n```\n"
        f"Evidence-driven repair plan template:\n{er.plan}\n"
    )

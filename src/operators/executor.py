"""OperatorExecutor: applies a list of OperatorPlans sequentially,
enforcing precondition → apply → postcondition for each step and tracking
line-number shifts so later plans' `*_line` params stay valid.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

from .base import (
    ExecutionContext,
    OperatorId,
    OperatorPlan,
    OperatorResult,
    OperatorScope,
    LINE_PARAM_KEYS,
)
from .catalog import get_operator_funcs, get_operator_scope
from .import_manager import ImportManager, JUNIT4_STATIC_IMPORTS
from .line_tracker import LineTracker


# operators that affect line counts; executor uses this to update the tracker
_LINE_DELTA_RULES: Dict[OperatorId, str] = {
    OperatorId.INSERT_ASSERTION: "insert_after_line",
    OperatorId.INSERT_STATEMENT: "insert_after_line",
    OperatorId.ADD_SETUP_CALL: "insert_at_method_top",
    OperatorId.REMOVE_ASSERTION: "delete_target_line",
    OperatorId.REMOVE_STATEMENT: "delete_target_line",
    # REPLACE operators don't shift lines (same count in and out)
    # REMOVE_TRY_CATCH_KEEP_BODY / TRY_CATCH_TO_EXPECTED re-flow text, but
    # subsequent ops on the same method would be ambiguous anyway; we treat
    # the line tracker as invalidated after such ops.
    OperatorId.REMOVE_TRY_CATCH_KEEP_BODY: "invalidate",
    OperatorId.TRY_CATCH_TO_EXPECTED: "invalidate",
    OperatorId.EXTRACT_TO_BEFORE: "invalidate",
}


@dataclass
class ExecutorOutcome:
    final_text: str
    results: List[OperatorResult]
    used_asserts: Set[str] = field(default_factory=set)
    tracker_invalidated: bool = False


def _assert_types_in_plan(plan: OperatorPlan) -> Set[str]:
    used: Set[str] = set()
    for key in ("assert_type", "new_assert_type"):
        val = plan.params.get(key)
        if isinstance(val, str) and val in JUNIT4_STATIC_IMPORTS:
            used.add(val)
    return used


class OperatorExecutor:
    def __init__(self, import_manager: ImportManager | None = None):
        self.import_manager = import_manager or ImportManager()

    def execute_plan(
        self,
        text: str,
        plan: List[OperatorPlan],
        ctx: ExecutionContext,
    ) -> ExecutorOutcome:
        current = text
        tracker = LineTracker(original_line_count=len(text.splitlines()))
        results: List[OperatorResult] = []
        used_asserts: Set[str] = set()
        invalidated = False

        for step in plan:
            # 0. scope check — FILE-scope ops require full file text.
            scope = get_operator_scope(step.op)
            if scope == OperatorScope.FILE and current is not ctx.file_text and current != ctx.file_text:
                # permissive: assume caller passes file_text when running FILE ops.
                pass  # no-op; caller's responsibility

            # 1. translate line-number params using tracker
            if invalidated:
                adjusted = step
            else:
                adjusted = self._translate_params(step, tracker)

            funcs = get_operator_funcs(step.op)

            # 2. precondition
            pre_ok, pre_reason = funcs["pre"](adjusted, current, ctx)
            if not pre_ok:
                results.append(
                    OperatorResult(
                        op=step.op,
                        success=False,
                        modified_text=None,
                        rejection_reason=f"precondition:{pre_reason}",
                        meta={"adjusted_params": dict(adjusted.params)},
                    )
                )
                continue

            # 3. apply
            try:
                new_text = funcs["apply"](adjusted, current, ctx)
            except Exception as e:
                results.append(
                    OperatorResult(
                        op=step.op,
                        success=False,
                        modified_text=None,
                        rejection_reason=f"apply_error:{type(e).__name__}:{e}",
                        meta={"adjusted_params": dict(adjusted.params)},
                    )
                )
                continue

            # 4. postcondition
            post_ok, post_reason = funcs["post"](adjusted, current, new_text, ctx)
            if not post_ok:
                results.append(
                    OperatorResult(
                        op=step.op,
                        success=False,
                        modified_text=None,
                        rejection_reason=f"postcondition:{post_reason}",
                        meta={"adjusted_params": dict(adjusted.params)},
                    )
                )
                continue

            # 5. commit
            rule = _LINE_DELTA_RULES.get(step.op, "none")
            if rule == "insert_after_line":
                # count inserted lines: diff between new and old
                delta = len(new_text.splitlines()) - len(current.splitlines())
                after = adjusted.params.get("after_line", 0)
                tracker.record_insert(after_original_line=after, count=max(0, delta))
            elif rule == "insert_at_method_top":
                delta = len(new_text.splitlines()) - len(current.splitlines())
                tracker.record_insert(after_original_line=1, count=max(0, delta))
            elif rule == "delete_target_line":
                delta = len(current.splitlines()) - len(new_text.splitlines())
                target = adjusted.params.get("target_line", 0)
                tracker.record_delete(start_original_line=target, count=max(0, delta))
            elif rule == "invalidate":
                invalidated = True

            current = new_text
            used_asserts |= _assert_types_in_plan(step)
            results.append(
                OperatorResult(
                    op=step.op,
                    success=True,
                    modified_text=new_text,
                    rejection_reason=None,
                    meta={"adjusted_params": dict(adjusted.params)},
                )
            )

        return ExecutorOutcome(
            final_text=current,
            results=results,
            used_asserts=used_asserts,
            tracker_invalidated=invalidated,
        )

    def _translate_params(
        self, plan: OperatorPlan, tracker: LineTracker
    ) -> OperatorPlan:
        new_params: Dict[str, Any] = dict(plan.params)
        for key in LINE_PARAM_KEYS:
            if key in new_params and isinstance(new_params[key], int):
                new_params[key] = tracker.translate(new_params[key])
        return OperatorPlan(op=plan.op, params=new_params, smell_id=plan.smell_id)

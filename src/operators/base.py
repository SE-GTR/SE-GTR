from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class OperatorId(str, Enum):
    INSERT_ASSERTION = "INSERT_ASSERTION"
    REMOVE_ASSERTION = "REMOVE_ASSERTION"
    REPLACE_ASSERTION = "REPLACE_ASSERTION"
    CAPTURE_RETURN_VALUE = "CAPTURE_RETURN_VALUE"
    REPLACE_EXPRESSION = "REPLACE_EXPRESSION"
    REPLACE_NULL_ARG = "REPLACE_NULL_ARG"
    INSERT_STATEMENT = "INSERT_STATEMENT"
    REMOVE_STATEMENT = "REMOVE_STATEMENT"
    ADD_SETUP_CALL = "ADD_SETUP_CALL"
    TRY_CATCH_TO_EXPECTED = "TRY_CATCH_TO_EXPECTED"
    REMOVE_TRY_CATCH_KEEP_BODY = "REMOVE_TRY_CATCH_KEEP_BODY"
    WRAP_WITH_ASSERT_THROWS = "WRAP_WITH_ASSERT_THROWS"
    ADD_TEST_EXPECTED = "ADD_TEST_EXPECTED"
    REMOVE_TEST_EXPECTED = "REMOVE_TEST_EXPECTED"
    EXTRACT_TO_BEFORE = "EXTRACT_TO_BEFORE"


class OperatorScope(str, Enum):
    METHOD = "method"
    FILE = "file"


@dataclass
class OperatorPlan:
    op: OperatorId
    params: Dict[str, Any]
    smell_id: str


@dataclass
class OperatorResult:
    op: OperatorId
    success: bool
    modified_text: Optional[str]
    rejection_reason: Optional[str]
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionContext:
    method_name: str
    method_line_range: Tuple[int, int]
    file_text: str
    cut_source: Optional[str] = None
    cut_fqcn: Optional[str] = None
    cut_public_methods: List[Dict[str, str]] = field(default_factory=list)
    existing_imports: Set[str] = field(default_factory=set)


LINE_PARAM_KEYS = ("after_line", "target_line", "try_begin_line")

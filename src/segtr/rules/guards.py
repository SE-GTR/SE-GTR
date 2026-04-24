from __future__ import annotations

import re

DISALLOWED_MARKERS = ["@Ignore", "org.junit.Ignore"]


def ensure_test_method_present(java_text: str, method_name: str) -> None:
    if not re.search(rf"\bvoid\s+{re.escape(method_name)}\s*\(", java_text):
        raise ValueError(f"Test method disappeared: {method_name}")


def ensure_no_disallowed_markers(java_text: str) -> None:
    for m in DISALLOWED_MARKERS:
        if m in java_text:
            raise ValueError(f"Disallowed marker found: {m}")

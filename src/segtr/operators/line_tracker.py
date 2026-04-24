"""Translates original (LLM-supplied) line numbers to current line numbers as
operators are applied in sequence. Shifts are relative to the original text.

Convention:
  - original_line is 1-indexed.
  - An insertion after original_line=L adds `count` lines; any subsequent
    query for original_line > L returns original_line + count.
  - A deletion starting at original_line=L for `count` lines shifts all queries
    for original_line > L by -count (and `L..L+count-1` map to L-1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class LineTracker:
    original_line_count: int
    _shifts: List[Tuple[int, int]] = field(default_factory=list)

    def record_insert(self, after_original_line: int, count: int = 1) -> None:
        if count <= 0:
            return
        self._shifts.append((after_original_line, count))

    def record_delete(self, start_original_line: int, count: int = 1) -> None:
        if count <= 0:
            return
        # deletion at start_original_line: lines >= start get shifted by -count,
        # so pivot = start_original_line - 1 (affects only queries > pivot).
        self._shifts.append((start_original_line - 1, -count))

    def translate(self, original_line: int) -> int:
        adjusted = original_line
        for pivot, delta in self._shifts:
            if original_line > pivot:
                adjusted += delta
        return max(1, adjusted)

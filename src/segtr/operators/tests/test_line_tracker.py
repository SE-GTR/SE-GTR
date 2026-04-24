from __future__ import annotations

import unittest

from smell_repair_v2.operators.line_tracker import LineTracker


class TestLineTracker(unittest.TestCase):
    def test_noop(self):
        t = LineTracker(original_line_count=10)
        self.assertEqual(t.translate(5), 5)

    def test_single_insert_shifts_later_lines(self):
        t = LineTracker(original_line_count=10)
        t.record_insert(after_original_line=3, count=1)
        # lines <= 3 unchanged
        self.assertEqual(t.translate(1), 1)
        self.assertEqual(t.translate(3), 3)
        # lines > 3 shift by +1
        self.assertEqual(t.translate(4), 5)
        self.assertEqual(t.translate(10), 11)

    def test_multiple_inserts_accumulate(self):
        t = LineTracker(original_line_count=10)
        t.record_insert(after_original_line=2, count=1)
        t.record_insert(after_original_line=5, count=2)
        # line 6 (after both inserts): 6 + 1 + 2 = 9
        self.assertEqual(t.translate(6), 9)
        self.assertEqual(t.translate(3), 4)
        self.assertEqual(t.translate(2), 2)

    def test_delete_shifts_later_lines(self):
        t = LineTracker(original_line_count=10)
        t.record_delete(start_original_line=4, count=1)
        # lines < 4 unchanged; line 4+ shifted by -1
        self.assertEqual(t.translate(3), 3)
        self.assertEqual(t.translate(5), 4)
        self.assertEqual(t.translate(10), 9)

    def test_mixed_insert_delete(self):
        t = LineTracker(original_line_count=20)
        t.record_insert(after_original_line=3, count=2)
        t.record_delete(start_original_line=7, count=1)
        # line 10: +2 from first insert, -1 from delete → 11
        self.assertEqual(t.translate(10), 11)
        # line 2: unchanged
        self.assertEqual(t.translate(2), 2)
        # line 5: +2, 5 < 7 so no delete effect → 7
        self.assertEqual(t.translate(5), 7)
        # line 7: +2, delete affects lines > 6, so -1 → 8
        self.assertEqual(t.translate(7), 8)

    def test_translate_does_not_go_below_one(self):
        t = LineTracker(original_line_count=5)
        t.record_delete(start_original_line=1, count=5)
        self.assertEqual(t.translate(1), 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for the Tier 2 dev experiment scaffolding.

These only exercise the metric aggregation and output generators — the core
processing loop is I/O heavy (Ant + JUnit + Smelly) and is covered by the
dry-run we kick off manually.
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from smell_repair_v2.scripts.dev_experiment_tier2 import (
    PerCellMetrics,
    TIER2_SMELLY_NAME_TO_ID,
    _aggregate_by_model,
    _cell_row,
    _write_aggregate_by_model_csv,
    _write_cost_report,
    _write_model_comparison_md,
    _write_summary_csv,
)


class _FakeUsageStats:
    def __init__(self, reqs, in_t, out_t, cost, errors=0, lat=0):
        self.total_requests = reqs
        self.total_input_tokens = in_t
        self.total_output_tokens = out_t
        self.total_cost_usd = cost
        self.errors = errors
        self.total_latency_ms = lat

    def avg_latency_ms(self):
        return self.total_latency_ms / self.total_requests if self.total_requests else 0.0


class _FakeMulti:
    def __init__(self, per_model):
        self._per = per_model

    def all_usage(self):
        return self._per


def _cell(model, project, smell, **overrides):
    c = PerCellMetrics(model_key=model, project=project, smell_id=smell)
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class TestPerCellMetricsHelpers(unittest.TestCase):
    def test_smell_reduction_pct(self):
        c = _cell("m", "p", "ENET", smell_before_count=10, smell_after_count=4)
        self.assertAlmostEqual(c.smell_reduction_pct(), 60.0)
        c2 = _cell("m", "p", "ENET", smell_before_count=0)
        self.assertEqual(c2.smell_reduction_pct(), 0.0)

    def test_avg_latency(self):
        c = _cell("m", "p", "ENET", llm_calls=3, total_latency_ms=1500)
        self.assertAlmostEqual(c.avg_latency_per_call_ms(), 500.0)

    def test_precondition_reason_bucketing(self):
        c = _cell("m", "p", "ENET")
        c.record_precondition_reason("precondition:target_line 12 out of range [1, 7]")
        c.record_precondition_reason("precondition:target_line 99 out of range [1, 10]")
        c.record_precondition_reason("apply_error:ZeroDivisionError")
        # both target_line rejections bucket under the same key
        self.assertEqual(c.precondition_fail_reasons["precondition:target_line 12 out of range [1, 7]"], 1)
        self.assertEqual(c.precondition_fail_reasons["precondition:target_line 99 out of range [1, 10]"], 1)
        self.assertEqual(c.precondition_fail_reasons["apply_error:ZeroDivisionError"], 1)


class TestAggregator(unittest.TestCase):
    def test_sums_across_cells(self):
        cells = {
            ("m1", "p1", "ENET"): _cell(
                "m1", "p1", "ENET",
                total_plans_generated=4, final_accepted=3,
                smell_before_count=8, smell_after_count=3,
                total_input_tokens=1000, total_output_tokens=200,
                total_cost_usd=0.005, total_latency_ms=2500, llm_calls=2,
            ),
            ("m1", "p2", "EDIS"): _cell(
                "m1", "p2", "EDIS",
                total_plans_generated=2, final_accepted=1,
                smell_before_count=5, smell_after_count=4,
                total_input_tokens=500, total_output_tokens=100,
                total_cost_usd=0.0015, total_latency_ms=1200, llm_calls=1,
            ),
            ("m2", "p1", "ENET"): _cell(
                "m2", "p1", "ENET",
                total_plans_generated=3, final_accepted=3,
                smell_before_count=8, smell_after_count=2,
                total_cost_usd=0.01, total_latency_ms=3000, llm_calls=3,
            ),
        }
        agg = _aggregate_by_model(cells)
        self.assertEqual(set(agg), {"m1", "m2"})
        self.assertEqual(agg["m1"]["total_plans_generated"], 6)
        self.assertEqual(agg["m1"]["final_accepted"], 4)
        self.assertAlmostEqual(agg["m1"]["total_cost_usd"], 0.0065)
        self.assertEqual(agg["m1"]["smell_before_total"], 13)
        self.assertEqual(agg["m1"]["smell_after_total"], 7)
        self.assertEqual(agg["m2"]["total_plans_generated"], 3)


class TestOutputFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cells = {
            ("m1", "p1", "ENET"): _cell(
                "m1", "p1", "ENET",
                total_plans_generated=4, final_accepted=3,
                parse_success=4, parse_failures=0,
                smell_before_count=8, smell_after_count=3,
                total_cost_usd=0.005, total_latency_ms=2500, llm_calls=2,
                total_input_tokens=1000, total_output_tokens=200,
                precondition_pass=4, precondition_fail=0,
                new_narv_introduced=1,
            ),
            ("m2", "p1", "ENET"): _cell(
                "m2", "p1", "ENET",
                total_plans_generated=3, final_accepted=1,
                parse_success=3, parse_failures=1,
                smell_before_count=8, smell_after_count=6,
                total_cost_usd=0.02, total_latency_ms=6000, llm_calls=4,
                total_input_tokens=2000, total_output_tokens=400,
                gate_compile_reject=2,
                new_narv_introduced=0,
            ),
        }

    def test_summary_csv_writes_expected_rows(self):
        path = _write_summary_csv(self.run_dir, self.cells)
        self.assertTrue(path.exists())
        with path.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        by_model = {r["model_key"]: r for r in rows}
        self.assertEqual(by_model["m1"]["final_accepted"], "3")
        self.assertEqual(by_model["m1"]["smell_reduction_pct"], "62.5")
        self.assertEqual(by_model["m2"]["gate_compile_reject"], "2")

    def test_aggregate_csv_one_row_per_model(self):
        agg = _aggregate_by_model(self.cells)
        path = _write_aggregate_by_model_csv(self.run_dir, agg)
        with path.open() as f:
            rows = list(csv.reader(f))
        self.assertGreaterEqual(len(rows), 3)  # header + 2 models
        header = rows[0]
        self.assertEqual(header[0], "model_key")
        self.assertIn("final_accepted", header)

    def test_model_comparison_md_contains_each_model(self):
        agg = _aggregate_by_model(self.cells)
        path = _write_model_comparison_md(self.run_dir, agg, budget_exhausted={"m2"})
        text = path.read_text()
        self.assertIn("m1", text)
        self.assertIn("m2", text)
        self.assertIn("budget_exhausted", text)
        self.assertIn("| Model", text)

    def test_cost_report_md(self):
        multi = _FakeMulti(per_model={
            "m1": _FakeUsageStats(reqs=2, in_t=1000, out_t=200, cost=0.005, lat=2500),
            "m2": _FakeUsageStats(reqs=4, in_t=2000, out_t=400, cost=0.02, lat=6000),
        })
        agg = _aggregate_by_model(self.cells)
        path = _write_cost_report(self.run_dir, multi, agg)
        text = path.read_text()
        self.assertIn("m1", text)
        self.assertIn("m2", text)
        self.assertIn("$0.0050", text)
        self.assertIn("$0.0200", text)


class TestGateClassification(unittest.TestCase):
    """`_process_plan_group` increments specific gate fields based on the
    validator's reason prefix. Verify the mapping."""
    def test_mapping_coverage(self):
        from smell_repair_v2.scripts.dev_experiment_tier2 import _GATE_TO_FIELD
        self.assertIn("gate1_banned", _GATE_TO_FIELD)
        self.assertIn("gate3_compile", _GATE_TO_FIELD)
        self.assertIn("gate4_test", _GATE_TO_FIELD)
        self.assertIn("gate7_assert_loss", _GATE_TO_FIELD)
        # ensure every mapped field exists on PerCellMetrics
        sample = PerCellMetrics("m", "p", "s")
        for target in _GATE_TO_FIELD.values():
            self.assertTrue(hasattr(sample, target), f"missing field {target}")


if __name__ == "__main__":
    unittest.main()

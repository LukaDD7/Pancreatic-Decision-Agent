"""Diagnostic-event clock counterexamples; synthetic records only."""
import copy
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.diagnostic_events import (
    build_diagnostic_events,
    diagnostic_event_map,
    derive_preoperative_task_bounds,
)


def _clock(value):
    return dict(value=value, precision="second", raw=value) if value else dict(
        value=None, precision="unknown")


def _source(source_id, *, exam=None, report=None, verified=None):
    clocks = dict(exam_time=_clock(exam), report_time=_clock(report),
                  verified_time=_clock(verified))
    return dict(source_id=source_id, kind="imaging", title="合成MR",
                text="合成检查报告，不含患者数据。", clocks=clocks,
                clock=_clock(verified or exam))


def _evidence(*source_ids):
    return dict(event_id="EA-SYNTHETIC", source_ids=list(source_ids))


class DiagnosticEventTests(unittest.TestCase):
    def test_HDE001_mr_retains_three_clocks_and_builds_event_map(self):
        source = _source("S-MR", exam="2022-12-14 12:17:25",
                         report="2022-12-14 14:56:32",
                         verified="2022-12-16 15:33:07")
        events = build_diagnostic_events([source], [_evidence("S-MR")])
        event = events[0]
        self.assertEqual(event["exam_time"]["value"], "2022-12-14 12:17:25")
        self.assertEqual(event["report_time"]["value"], "2022-12-14 14:56:32")
        self.assertEqual(event["verified_time"]["value"], "2022-12-16 15:33:07")
        self.assertEqual(event["report_time"]["raw"], "2022-12-14 14:56:32")
        self.assertEqual(event["source_clocks"]["S-MR"], source["clocks"])
        self.assertIsNot(event["source_clocks"]["S-MR"], source["clocks"])
        self.assertIs(diagnostic_event_map(events)["EA-SYNTHETIC"], event)

    def test_HDE002_report_time_has_priority_over_exam_and_verified_time(self):
        source = _source("S-R", exam="2026-03-01 08:00:00",
                         report="2026-03-01 11:00:00",
                         verified="2026-03-01 12:00:00")
        event = build_diagnostic_events([source], [_evidence("S-R")])[0]
        self.assertEqual(event["visibility_basis"], "report_time")
        self.assertEqual(event["visibility_clock"], source["clocks"]["report_time"])
        self.assertNotEqual(event["visibility_clock"]["value"],
                            source["clocks"]["exam_time"]["value"])
        self.assertNotEqual(event["visibility_clock"]["value"],
                            source["clocks"]["verified_time"]["value"])

    def test_HDE003_verified_time_is_explicit_fallback_when_report_is_missing(self):
        source = _source("S-V", exam="2026-03-01 08:00:00",
                         verified="2026-03-01 12:00:00")
        event = build_diagnostic_events([source], [_evidence("S-V")])[0]
        self.assertIsNone(event["report_time"]["value"])
        self.assertEqual(event["visibility_basis"], "verified_time_fallback")
        self.assertEqual(event["visibility_clock"]["value"], "2026-03-01 12:00:00")

    def test_HDE004_exam_time_alone_cannot_impersonate_visibility(self):
        source = _source("S-U", exam="2026-03-01 08:00:00")
        event = build_diagnostic_events([source], [_evidence("S-U")])[0]
        self.assertEqual(event["visibility_basis"], "unknown")
        self.assertEqual(event["visibility_clock"],
                         dict(value=None, precision="unknown"))
        self.assertEqual(event["exam_time"]["value"], "2026-03-01 08:00:00")

    def test_HDE005_multisource_clock_conflict_is_flagged_not_arbitrarily_resolved(self):
        sources = [
            _source("S-A", report="2026-03-01 11:00:00",
                    verified="2026-03-01 12:00:00"),
            _source("S-B", report="2026-03-01 11:05:00",
                    verified="2026-03-01 12:00:00"),
        ]
        event = build_diagnostic_events(sources, [_evidence("S-A", "S-B")])[0]
        self.assertIn("report_time", event["audit"]["clock_inconsistent_flags"])
        self.assertIsNone(event["report_time"]["value"])
        self.assertEqual(event["visibility_basis"], "unknown")
        self.assertIsNone(event["visibility_clock"]["value"])
        self.assertEqual(event["source_clocks"]["S-A"]["report_time"]["value"],
                         "2026-03-01 11:00:00")
        self.assertEqual(event["source_clocks"]["S-B"]["report_time"]["value"],
                         "2026-03-01 11:05:00")

    def test_HDE006_missing_and_duplicate_sources_have_explicit_audit(self):
        source = _source("S-A", verified="2026-03-01 12:00:00")
        event = build_diagnostic_events(
            [source], [_evidence("S-A", "S-MISSING", "S-A")])[0]
        self.assertEqual(event["source_ids"], ["S-A", "S-MISSING"])
        self.assertEqual(event["audit"]["duplicate_source_ids"], ["S-A"])
        self.assertEqual(event["audit"]["missing_source_ids"], ["S-MISSING"])
        self.assertEqual(event["audit"]["resolved_source_ids"], ["S-A"])
        self.assertEqual(event["audit"]["requested_source_ids"],
                         ["S-A", "S-MISSING", "S-A"])
        self.assertEqual(event["visibility_basis"], "verified_time_fallback")

    def test_HDE007_sources_and_evidence_inventory_are_not_mutated(self):
        sources = [_source("S-A", exam="2026-03-01 08:00:00",
                           report="2026-03-01 11:00:00")]
        inventory = [_evidence("S-A", "S-A")]
        sources_before = copy.deepcopy(sources)
        inventory_before = copy.deepcopy(inventory)
        events = build_diagnostic_events(sources, inventory)
        events[0]["source_clocks"]["S-A"]["report_time"]["value"] = "changed"
        events[0]["source_ids"].append("S-NEW")
        self.assertEqual(sources, sources_before)
        self.assertEqual(inventory, inventory_before)

    def test_HDE008_operation_boundary_requires_an_exact_operation_time_field(self):
        operative = dict(source_id="S-OP", title="虚构手术记录",
                         text="手术时间：2026-03-02 09:00-2026-03-02 13:00\n其余正文")
        other = dict(source_id="S-D", title="虚构病程", text="拟于明日手术。")
        bounds = derive_preoperative_task_bounds([other, operative])
        self.assertEqual(bounds["operation_start"]["value"], "2026-03-02 09:00")
        self.assertEqual(bounds["evidence"][0]["source_id"], "S-OP")
        self.assertEqual(derive_preoperative_task_bounds([other])["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()

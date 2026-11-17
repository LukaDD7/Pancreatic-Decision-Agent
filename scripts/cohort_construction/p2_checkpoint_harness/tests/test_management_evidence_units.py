"""Synthetic source-routing counterexamples for management evidence units."""
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units
from scripts.cohort_construction.p2_pathway_replay.core import clock


def _source(source_id, kind, text, *, title="交付报告", exam=None,
            exam_time=None, report_time=None, verified_time=None):
    clocks = {}
    for role, value in (("exam_time", exam_time), ("report_time", report_time),
                        ("verified_time", verified_time)):
        if value is not None:
            clocks[role] = clock(value)
    source_time = ((verified_time or report_time or exam_time) if kind == "imaging"
                   else (exam_time or report_time or verified_time))
    clock_basis = ("verified_time_proxy" if kind == "imaging" and verified_time
                   else "exam_time_proxy_release_unknown")
    return dict(source_id=source_id, kind=kind, title=title, text=text,
                source_sha256=source_id * 64,
                clock=clock(source_time), clock_basis=clock_basis, clocks=clocks,
                provenance={"exam_no_raw": exam})


class ManagementEvidenceUnitTests(unittest.TestCase):
    def test_pathology_is_complete_stable_performed_report_without_exam_time(self):
        text = "病理所见完整原文。\n免疫组化完整原文。"
        pathology = _source("P", "pathology", text, report_time="2026-03-02 12:00")
        units = build_units([pathology])
        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual(unit["kind"], "performed_test_report")
        self.assertEqual(unit["document_profile"], "performed_test_report")
        self.assertEqual(unit["source_ids"], ["P"])
        self.assertEqual(unit["original_texts"][0]["text"], text)
        self.assertEqual(unit["unit_id"], build_units([pathology])[0]["unit_id"])
        self.assertIsNone(unit["exam_clock"]["value"])
        self.assertEqual(unit["clock"]["value"], "2026-03-02 12:00:00")
        self.assertEqual(unit["clock_basis"], "report_time_proxy")

    def test_report_visibility_is_not_replaced_by_exam_or_source_clock(self):
        pathology = _source("P", "pathology", "完整病理报告。",
                            exam_time="2026-03-01 08:00",
                            report_time="2026-03-02 11:00")
        unit = build_units([pathology])[0]
        self.assertEqual(unit["exam_clock"]["value"], "2026-03-01 08:00:00")
        self.assertEqual(unit["clock"]["value"], "2026-03-02 11:00:00")
        self.assertEqual(unit["time_upper_bound"], unit["exam_clock"])
        self.assertNotEqual(unit["clock"], unit["exam_clock"])

    def test_non_imaging_report_requires_visibility_time(self):
        pathology = _source("P", "pathology", "检查已执行但交付时间未知。",
                            exam_time="2026-03-01 08:00")
        audit = []
        self.assertEqual(build_units([pathology], routing_audit=audit), [])
        self.assertEqual(audit[0]["disposition"], "REPORT_VISIBILITY_UNAVAILABLE")
        self.assertEqual(audit[0]["reason"], "REPORT_VISIBILITY_TIME_REQUIRED")

    def test_distinct_pathologies_never_merge_by_title_or_drop_sources(self):
        first = _source("P1", "pathology", "第一份完整病理。",
                        title="病理报告", report_time="2026-03-02 11:00")
        second = _source("P2", "pathology", "第二份完整病理。",
                         title="病理报告", report_time="2026-03-02 11:00")
        units = build_units([first, second])
        self.assertEqual(len(units), 2)
        self.assertEqual({sid for unit in units for sid in unit["source_ids"]},
                         {"P1", "P2"})
        self.assertTrue(all(len(unit["source_ids"]) == 1 for unit in units))
        self.assertEqual({unit["original_texts"][0]["text"] for unit in units},
                         {"第一份完整病理。", "第二份完整病理。"})
        self.assertEqual(len({unit["unit_id"] for unit in units}), 2)

    def test_procedure_and_operation_deliveries_use_same_report_interface(self):
        for kind in ("procedure", "operation"):
            with self.subTest(kind=kind):
                source = _source("O", kind, "腹腔镜探查交付记录完整原文。",
                                 report_time="2026-03-03 15:00")
                unit = build_units([source])[0]
                self.assertEqual(unit["kind"], "performed_test_report")
                self.assertEqual(unit["document_profile"], "performed_test_report")
                self.assertEqual(unit["original_texts"][0]["text"], source["text"])
                self.assertEqual(unit["clock_basis"], "report_time_proxy")

    def test_ordinary_lab_is_not_dual_reviewed_but_identified_lab_delivery_is(self):
        ordinary = _source("L", "lab", "普通检验结果。", title="交付报告",
                           report_time="2026-03-02 10:00")
        delivery = _source("D", "lab", "检查交付记录完整原文。", title="交付报告",
                           exam="EXAM-1", exam_time="2026-03-02 08:00",
                           report_time="2026-03-02 10:00")
        audit = []
        units = build_units([ordinary, delivery], routing_audit=audit)
        self.assertEqual([unit["source_ids"] for unit in units], [["D"]])
        self.assertEqual(units[0]["kind"], "performed_test_report")
        routes = {row["source_id"]: row for row in audit}
        self.assertEqual(routes["L"]["disposition"], "NON_DISCOVERY_SOURCE")
        self.assertEqual(routes["D"]["disposition"],
                         "MANAGEMENT_REPORT_CANDIDATE_GROUP_MEMBER")

    def test_imaging_grouping_and_clock_behavior_are_unchanged(self):
        first = _source("I1", "imaging", "影像版本一。", title="增强CT", exam="EXAM-1",
                        exam_time="2026-03-01 08:00", report_time="2026-03-01 11:00",
                        verified_time="2026-03-01 12:00")
        second = _source("I2", "imaging", "影像版本二。", title="增强CT", exam="EXAM-1",
                         exam_time="2026-03-01 08:00", report_time="2026-03-01 11:00",
                         verified_time="2026-03-01 12:00")
        other = _source("I3", "imaging", "另一检查。", title="增强MR", exam="EXAM-1",
                        exam_time="2026-03-01 08:00", report_time="2026-03-01 11:00",
                        verified_time="2026-03-01 12:00")
        units = build_units([first, second, other])
        grouped = next(unit for unit in units if len(unit["source_ids"]) == 2)
        self.assertEqual(grouped["source_ids"], ["I1", "I2"])
        self.assertEqual(grouped["clock"]["value"], "2026-03-01 12:00:00")
        self.assertEqual(grouped["exam_clock"]["value"], "2026-03-01 08:00:00")
        self.assertEqual(grouped["clock_basis"], "verified_time_proxy")
        self.assertEqual(len(units), 2)


if __name__ == "__main__":
    unittest.main()

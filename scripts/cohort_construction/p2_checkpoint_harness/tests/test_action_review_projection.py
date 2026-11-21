"""Case-level action review projection tests using only synthetic records."""
from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.actions import canonical_action
from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials import (
    candidate_material_manifest_sha256,
    project_candidate_materials,
)
from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials_html import (
    render_html,
)
from scripts.cohort_construction.p2_checkpoint_harness.stages.management_trajectory import (
    project_management_trajectory,
)
from scripts.cohort_construction.p2_checkpoint_harness.tests.test_management_trajectory import (
    AFTER,
    candidate,
    diagnostic,
    review,
    source,
)


FLAG = "ACTION_TASK_MISMATCH_REVIEW_REQUIRED"
ACTOR = "case_level_action_review_llm"
REMOVE_REASON = "CASE_LEVEL_ACTION_REVIEW_REMOVE_INDEPENDENT_CP"


class ActionReviewProjectionTests(unittest.TestCase):
    def setUp(self):
        self.bounds = dict(
            operation_start=dict(value="2026-03-02 10:00", precision="minute"),
            evidence=[dict(source_id="S-OP")])
        self.kwargs = dict(
            sources=[source("S-CONTEXT", "既往资料。"),
                     source("S-OP", "手术时间：2026-03-02 10:00-12:00。",
                            clock="2026-03-02 12:00"),
                     source("CT-POST", "术后CT报告。")],
            events=[diagnostic("DE-POST-CT", "CT-POST")],
            reviews=[review("DE-POST-CT", AFTER, target="新辅助化疗",
                            action_type="START_NAT")],
            bounds=self.bounds)
        self.baseline = self.project(**self.kwargs)
        self.checkpoint = self.baseline["candidate_checkpoints"][0]
        self.assertIn(FLAG, self.checkpoint["review_flags"])

    def project(self, *, candidates=(), sources=(), events=(), reviews=(), bounds=None,
                adjudications=None):
        return project_management_trajectory(
            case_id="SYN-ACTION-REVIEW", candidates=candidates, sources=sources,
            diagnostic_events=events, diagnostic_reviews=reviews,
            task_time_bounds=bounds or dict(operation_start=dict(value=None)),
            checkpoint_adjudications=adjudications)

    def action_review(self, checkpoint, disposition="RETAIN_CP_WITH_ACTION"):
        value = dict(
            checkpoint_id=checkpoint["checkpoint_id"], disposition=disposition,
            rationale="合成病例级动作复审结论", actor=ACTOR,
            original_candidate_actions=deepcopy(checkpoint["candidate_actions"]),
            trigger_flags=[FLAG], operation_start=deepcopy(self.bounds["operation_start"]),
            operation_source_ids=["S-OP"], source_reads_verified=True)
        if disposition == "RETAIN_CP_WITH_ACTION":
            value["candidate_action"] = canonical_action("POSTOP_FOLLOW_UP_CT")
        else:
            value["candidate_action"] = None
        return value

    def adjudications(self, *reviews):
        return dict(format="p2-checkpoint-adjudication-0.3.1", items=[],
                    action_reviews=list(reviews))

    def test_remove_postoperative_ct_preserves_report_and_conserves_migrations(self):
        kwargs = deepcopy(self.kwargs)
        kwargs["sources"].append(source("MR-PRE", "术前MR报告。"))
        kwargs["events"].append(diagnostic(
            "DE-PRE-MR", "MR-PRE", exam="2026-03-01 08:00",
            report="2026-03-01 12:00"))
        kwargs["reviews"].append(review("DE-PRE-MR", AFTER))
        baseline = self.project(**kwargs)
        checkpoint = next(item for item in baseline["candidate_checkpoints"]
                          if FLAG in item["review_flags"])
        other = next(item for item in baseline["candidate_checkpoints"]
                     if item["checkpoint_id"] != checkpoint["checkpoint_id"])

        result = self.project(**dict(kwargs, adjudications=self.adjudications(
            self.action_review(checkpoint, "REMOVE_INDEPENDENT_CP"))))

        self.assertEqual([item["checkpoint_id"] for item in result["candidate_checkpoints"]],
                         [other["checkpoint_id"]])
        self.assertEqual(len(result["diagnostic_events"]), 2)
        self.assertEqual(len(result["migrations"]), len(baseline["migrations"]))
        self.assertTrue(result["summary"]["migration_conserved"])
        self.assertTrue(result["summary"]["diagnostic_migration_conserved"])
        migration = next(item for item in result["migrations"]
                         if item["event_id"] == "DE-POST-CT"
                         and item["node_kind"] == AFTER)
        self.assertEqual(migration["migration_status"], "CONTEXT_ONLY")
        self.assertEqual(migration["reason"], REMOVE_REASON)
        self.assertIsNone(migration["to_checkpoint_id"])
        event = next(item for item in result["diagnostic_events"]
                     if item["event_id"] == "DE-POST-CT")
        self.assertIsNone(event["candidate_checkpoint_ids"][AFTER])
        audit = next(item for item in event["generation_audits"]
                     if item["node_kind"] == AFTER)
        self.assertFalse(audit["generated"])
        self.assertIsNone(audit["checkpoint_id"])
        self.assertEqual(audit["reason"], REMOVE_REASON)
        self.assertEqual(result["summary"]["action_reviewed_count"], 1)
        self.assertEqual(result["summary"]["action_review_disposition_counts"],
                         {"REMOVE_INDEPENDENT_CP": 1})

    def test_retain_replaces_action_synchronizes_cycle_and_exposes_html_metadata(self):
        reviewed = self.action_review(self.checkpoint)
        result = self.project(**dict(
            self.kwargs, adjudications=self.adjudications(reviewed)))
        checkpoint = result["candidate_checkpoints"][0]
        expected = canonical_action("POSTOP_FOLLOW_UP_CT")
        self.assertEqual(checkpoint["candidate_actions"][0]["action_id"],
                         expected["action_id"])
        self.assertEqual(checkpoint["candidate_actions"][0]["target"], expected["target"])
        self.assertEqual(checkpoint["candidate_actions"][0]["actor"], ACTOR)
        self.assertEqual(checkpoint["original_candidate_actions"],
                         self.checkpoint["candidate_actions"])
        self.assertEqual(checkpoint["action_review"]["original_candidate_actions"],
                         self.checkpoint["candidate_actions"])
        self.assertEqual(checkpoint["action_review_provenance"]["actor"], ACTOR)
        self.assertIn(FLAG, checkpoint["review_flags"])
        self.assertEqual(result["action_cycles"][0]["candidate_actions"],
                         checkpoint["candidate_actions"])

        material = project_candidate_materials(
            case_id="SYN-ACTION-REVIEW", sources=self.kwargs["sources"], candidates=[],
            projection=result, candidate_episodes=result["management_episodes"])
        packet = material["packets"][0]
        self.assertEqual(packet["action_review"]["disposition"], "RETAIN_CP_WITH_ACTION")
        self.assertEqual(packet["candidate_actions"], checkpoint["candidate_actions"])
        page = render_html(material, candidate_material_manifest_sha256(material))
        self.assertIn('class=\\"action-review\\"', page)
        self.assertIn("病例级动作复审", page)
        self.assertIn("RETAIN_CP_WITH_ACTION", page)
        self.assertIn("新辅助化疗", page)
        self.assertIn("POSTOP_FOLLOW_UP_CT", page)
        self.assertIn("术后复查胸腹盆增强CT", page)
        self.assertIn("合成病例级动作复审结论", page)

    def test_same_reviewed_action_keeps_baseline_management_ids_and_adjudications(self):
        first_text = "术后拟开始新辅助化疗。"
        second_text = "术后再次讨论胰十二指肠切除术。"
        candidates = [
            candidate("EV-FIRST", "S-FIRST", first_text, first_text,
                      clock="2026-03-03 08:00", action_type="START_NAT",
                      target="新辅助化疗"),
            candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                      clock="2026-03-04 08:00", action_type="ADVANCE_RESECTION",
                      target="胰十二指肠切除术"),
        ]
        sources = [
            source("S-OP", "手术时间：2026-03-02 10:00-12:00。",
                   clock="2026-03-02 12:00"),
            source("S-FIRST", first_text, profile="clinical_other",
                   clock="2026-03-03 08:00"),
            source("S-SECOND", second_text, profile="clinical_other",
                   clock="2026-03-04 08:00"),
        ]
        kwargs = dict(candidates=candidates, sources=sources, bounds=self.bounds)
        baseline = self.project(**kwargs)
        self.assertEqual(len(baseline["candidate_checkpoints"]), 2)
        first, second = baseline["candidate_checkpoints"]
        adjudications = self.adjudications(
            self.action_review(first), self.action_review(second))
        adjudications["items"] = [
            dict(checkpoint_id=first["checkpoint_id"], disposition="RETAIN_CP",
                 evolution="FIRST_VISIBLE", comparator_checkpoint_id=None,
                 evidence_supports=[], rationale="保留基线首个管理CP"),
            dict(checkpoint_id=second["checkpoint_id"], disposition="RETAIN_CP",
                 evolution="REPEATED_PLAN", comparator_checkpoint_id=first["checkpoint_id"],
                 evidence_supports=[], rationale="保留基线第二个管理CP"),
        ]

        result = self.project(**dict(kwargs, adjudications=adjudications))

        self.assertEqual([item["checkpoint_id"] for item in result["candidate_checkpoints"]],
                         [first["checkpoint_id"], second["checkpoint_id"]])
        self.assertEqual([item["member_event_ids"] for item in result["candidate_checkpoints"]],
                         [["EV-FIRST"], ["EV-SECOND"]])
        for checkpoint in result["candidate_checkpoints"]:
            self.assertEqual(len(checkpoint["candidate_actions"]), 1)
            self.assertEqual(checkpoint["candidate_actions"][0]["action_id"],
                             "POSTOP_FOLLOW_UP_CT")
            self.assertEqual(checkpoint["candidate_actions"][0]["actor"], ACTOR)
            self.assertEqual(checkpoint["global_adjudication"]["checkpoint_id"],
                             checkpoint["checkpoint_id"])
        self.assertEqual(len(result["action_cycles"]), 2)
        self.assertEqual(result["summary"]["action_reviewed_count"], 2)

    def test_removed_after_between_management_cps_preserves_baseline_split(self):
        first_text = "术后门诊复查胸腹盆增强CT。"
        second_text = "术后再次安排胸腹盆增强CT复查。"
        candidates = [
            candidate("EV-BEFORE-REPORT", "S-BEFORE-REPORT", first_text, first_text,
                      clock="2026-03-03 08:00", action_type="ORDER_DIAGNOSTIC_TEST",
                      target="术后复查胸腹盆增强CT"),
            candidate("EV-AFTER-REPORT", "S-AFTER-REPORT", second_text, second_text,
                      clock="2026-03-05 08:00", action_type="ORDER_DIAGNOSTIC_TEST",
                      target="术后复查胸腹盆增强CT"),
        ]
        kwargs = dict(
            candidates=candidates,
            sources=[
                source("S-OP", "手术时间：2026-03-02 10:00-12:00。",
                       clock="2026-03-02 12:00"),
                source("S-BEFORE-REPORT", first_text, profile="clinical_other",
                       clock="2026-03-03 08:00"),
                source("CT-MIDDLE", "术后CT报告。"),
                source("S-AFTER-REPORT", second_text, profile="clinical_other",
                       clock="2026-03-05 08:00"),
            ],
            events=[diagnostic("DE-MIDDLE", "CT-MIDDLE", exam="2026-03-04 08:00",
                               report="2026-03-04 12:00")],
            reviews=[review("DE-MIDDLE", AFTER, target="新辅助化疗",
                            action_type="START_NAT")],
            bounds=self.bounds)
        baseline = self.project(**kwargs)
        management = [item for item in baseline["candidate_checkpoints"]
                      if item["kind"] == "MANAGEMENT_REASSESSMENT"]
        self.assertEqual(len(management), 2)
        first, second = management
        removed = next(item for item in baseline["candidate_checkpoints"]
                       if item["kind"] == AFTER)
        adjudications = self.adjudications(
            self.action_review(removed, "REMOVE_INDEPENDENT_CP"))
        adjudications["items"] = [
            dict(checkpoint_id=first["checkpoint_id"], disposition="RETAIN_CP",
                 evolution="FIRST_VISIBLE", comparator_checkpoint_id=None,
                 evidence_supports=[], rationale="保留报告前管理CP"),
            dict(checkpoint_id=second["checkpoint_id"], disposition="RETAIN_CP",
                 evolution="REPEATED_PLAN", comparator_checkpoint_id=first["checkpoint_id"],
                 evidence_supports=[], rationale="保留报告后管理CP"),
        ]

        result = self.project(**dict(kwargs, adjudications=adjudications))

        retained_management = [item for item in result["candidate_checkpoints"]
                               if item["kind"] == "MANAGEMENT_REASSESSMENT"]
        self.assertEqual([item["checkpoint_id"] for item in retained_management],
                         [first["checkpoint_id"], second["checkpoint_id"]])
        self.assertFalse(any(item["kind"] == AFTER
                             for item in result["candidate_checkpoints"]))
        self.assertEqual(len(result["migrations"]), len(baseline["migrations"]))
        self.assertTrue(result["summary"]["migration_conserved"])

    def test_old_adjudication_without_action_reviews_is_unchanged(self):
        old = self.project(**dict(self.kwargs, adjudications=dict(
            format="p2-checkpoint-adjudication-0.2.3", items=[])))
        self.assertEqual(old, self.baseline)
        self.assertEqual(old["candidate_checkpoints"][0]["candidate_actions"][0]["target"],
                         "新辅助化疗")
        self.assertEqual(old["summary"]["action_reviewed_count"], 0)
        self.assertEqual(old["summary"]["action_review_disposition_counts"], {})
        self.assertEqual(old["format_version"], "management-trajectory-0.3.7")

    def test_invalid_unknown_duplicate_and_unflagged_reviews_are_rejected(self):
        reviewed = self.action_review(self.checkpoint)
        with self.assertRaisesRegex(ValueError, "duplicate action review"):
            self.project(**dict(self.kwargs, adjudications=self.adjudications(
                reviewed, deepcopy(reviewed))))

        unknown = deepcopy(reviewed)
        unknown["checkpoint_id"] = "CP-D-UNKNOWN"
        with self.assertRaisesRegex(ValueError, "unknown action review checkpoint"):
            self.project(**dict(self.kwargs, adjudications=self.adjudications(unknown)))

        preoperative_kwargs = dict(
            sources=[source("S-OP", "手术时间：2026-03-02 10:00-12:00。",
                            clock="2026-03-02 12:00"),
                     source("CT-PRE", "术前CT报告。")],
            events=[diagnostic("DE-PRE-CT", "CT-PRE", exam="2026-03-01 08:00",
                               report="2026-03-01 12:00")],
            reviews=[review("DE-PRE-CT", AFTER)], bounds=self.bounds)
        preoperative = self.project(**preoperative_kwargs)
        unflagged = self.action_review(preoperative["candidate_checkpoints"][0])
        with self.assertRaisesRegex(ValueError, "lacks trigger flag"):
            self.project(**dict(preoperative_kwargs,
                                adjudications=self.adjudications(unflagged)))

        free_text = deepcopy(reviewed)
        free_text["candidate_action"] = dict(
            action_id="OTHER_SPECIFIED", action_type="OTHER_SPECIFIED",
            target="内镜下胆道支架置入减黄")
        result = self.project(**dict(
            self.kwargs, adjudications=self.adjudications(free_text)))
        self.assertEqual(result["candidate_checkpoints"][0]["candidate_actions"][0]["target"],
                         "内镜下胆道支架置入减黄")

    def test_action_review_must_bind_operation_time_and_source_boundary(self):
        reviewed = self.action_review(self.checkpoint)
        cases = []

        changed_start = deepcopy(reviewed)
        changed_start["operation_start"]["value"] = "2026-03-02 10:01"
        cases.append(("changed operation_start", changed_start,
                      "operation_start mismatch"))

        empty_sources = deepcopy(reviewed)
        empty_sources["operation_source_ids"] = []
        cases.append(("empty operation_source_ids", empty_sources,
                      "operation_source_ids mismatch"))

        wrong_source = deepcopy(reviewed)
        wrong_source["operation_source_ids"] = ["S-CONTEXT"]
        cases.append(("wrong operation source", wrong_source,
                      "operation_source_ids mismatch"))

        for name, value, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    self.project(**dict(
                        self.kwargs, adjudications=self.adjudications(value)))

    def test_retained_checkpoint_allows_missing_action_and_does_not_phase_validate(self):
        reviewed = self.action_review(self.checkpoint)
        reviewed["candidate_action"] = None
        result = self.project(**dict(self.kwargs, adjudications=self.adjudications(reviewed)))
        self.assertEqual(result["candidate_checkpoints"][0]["candidate_actions"], [])
        self.assertEqual(result["action_cycles"][0]["candidate_actions"], [])

        reviewed = self.action_review(self.checkpoint)
        reviewed["candidate_action"] = canonical_action("DIAG_PANCREATIC_CONTRAST_CT")
        result = self.project(**dict(self.kwargs, adjudications=self.adjudications(reviewed)))
        self.assertEqual(result["candidate_checkpoints"][0]["candidate_actions"][0]["action_id"],
                         "DIAG_PANCREATIC_CONTRAST_CT")

    def test_dictionary_phase_controls_mismatch_with_legacy_keyword_fallback(self):
        records = [
            ("known preoperative action", canonical_action(
                "DIAG_PANCREATIC_CONTRAST_CT"), True),
            ("known postoperative action", canonical_action(
                "POSTOP_PATHOLOGY_REVIEW"), False),
            ("unknown legacy action", dict(
                action_id="LEGACY_NAT", action_type="START_NAT", target="新辅助化疗"), True),
        ]
        for name, action, mismatch in records:
            with self.subTest(name=name):
                text = "术后管理记录：" + action["target"] + "。"
                item = candidate(
                    "EV-PHASE", "S-PHASE", text, text, clock="2026-03-03 08:00",
                    candidate_action=deepcopy(action))
                result = self.project(
                    candidates=[item],
                    sources=[
                        source("S-OP", "手术时间：2026-03-02 10:00-12:00。",
                               clock="2026-03-02 12:00"),
                        source("S-PHASE", text, profile="clinical_other",
                               clock="2026-03-03 08:00")],
                    bounds=self.bounds)
                checkpoint = result["candidate_checkpoints"][0]
                self.assertEqual(FLAG in checkpoint["review_flags"], mismatch)
                self.assertEqual(checkpoint["phase"], "POSTOPERATIVE_TRAJECTORY")

    def test_management_cp_remove_is_rejected(self):
        text = "术后拟开始新辅助化疗。"
        item = candidate("EV-MANAGEMENT", "S-MANAGEMENT", text, text,
                         clock="2026-03-03 08:00", action_type="START_NAT",
                         target="新辅助化疗")
        sources = [source("S-OP", "手术时间：2026-03-02 10:00-12:00。",
                          clock="2026-03-02 12:00"),
                   source("S-MANAGEMENT", text, profile="clinical_other",
                          clock="2026-03-03 08:00")]
        baseline = self.project(candidates=[item], sources=sources, bounds=self.bounds)
        checkpoint = baseline["candidate_checkpoints"][0]
        reviewed = self.action_review(checkpoint, "REMOVE_INDEPENDENT_CP")
        with self.assertRaisesRegex(ValueError, "cannot remove management CP"):
            self.project(candidates=[item], sources=sources, bounds=self.bounds,
                         adjudications=self.adjudications(reviewed))


if __name__ == "__main__":
    unittest.main()

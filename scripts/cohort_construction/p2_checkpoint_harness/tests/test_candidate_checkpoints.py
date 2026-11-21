"""Offline candidate Checkpoint reprojection; all records are synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_checkpoints import (
    project_candidate_checkpoints,
)


def source(source_id, text, *, profile="preoperative_course", clock="2026-03-01",
           kind="document"):
    return dict(source_id=source_id, text=text, document_profile=profile, kind=kind,
                title=source_id + "-标题", clock=dict(value=clock),
                clock_basis="body_record_time")


def candidate(event_id, source_id, text, quote, *, function="new_decision", **kwargs):
    start = text.index(quote)
    return dict(event_id=event_id, status="ACCEPTED", scope="yes",
                record_function=function,
                clock=dict(value=kwargs.pop("clock", "2026-03-01")),
                clock_basis="body_record_time",
                candidate_action=kwargs.pop("candidate_action", dict(
                    action_type="ORDER_DIAGNOSTIC_TEST", target="目标检查")),
                span=dict(source_id=source_id, quote=quote,
                          start=start, end=start + len(quote)), **kwargs)


def review(kind, disposition="yes", **kwargs):
    value = dict(candidate_kind=kind, disposition=disposition,
                 reason="Discovery研究范围审核", candidate_action=dict(
                     action_type="ORDER_DIAGNOSTIC_TEST", target="胰腺增强MR"))
    value.update(kwargs)
    return value


def diagnostic(event_id="DE-MR", **kwargs):
    value = dict(event_id=event_id, source_ids=["MR-1", "MR-2"],
                 exam_time="2026-03-02 08:00", report_time="2026-03-02 12:00",
                 verified_time="2026-03-02 13:00", visibility_clock=None,
                 visibility_basis=None,
                 source_clocks={"MR-1": {"exam_time": "2026-03-02 08:00"}})
    value.update(kwargs)
    return value


class CandidateCheckpointTests(unittest.TestCase):
    def project(self, candidates=(), sources=(), events=(), reviews=None):
        sources = list(sources)
        if events and not sources:
            sources = [source("S-CONTEXT", "既往临床资料。", clock="2026-03-01")]
        if reviews is None:
            reviews = [review("pre_exam_evaluation", diagnostic_event_id=event["event_id"])
                       for event in events]
        return project_candidate_checkpoints(
            case_id="SYN", candidates=candidates, sources=sources,
            diagnostic_events=events, diagnostic_reviews=reviews)

    def test_admission_repeat_and_postoperative_profile_do_not_become_checkpoints(self):
        admission_text = "患者为求手术治疗，今日收入我科。"
        repeat_text = "前次已拟手术，本次再次回述。"
        postoperative_text = "术后继续常规补液。"
        further_treatment_text = "今为进一步治疗来我院就诊，门诊以\"胰腺肿瘤\"收入院"
        admission = candidate("EV-ADMIT", "S-A", admission_text, admission_text)
        further_treatment = candidate("EV-ADMIT-FURTHER", "S-AF", further_treatment_text,
                                      further_treatment_text)
        repeat = candidate("EV-REPEAT", "S-R", repeat_text, repeat_text,
                           function="repeat")
        postoperative = candidate("EV-POST", "S-P", postoperative_text,
                                  postoperative_text)
        result = self.project(
            [admission, further_treatment, repeat, postoperative],
            [source("S-A", admission_text), source("S-AF", further_treatment_text),
             source("S-R", repeat_text),
             source("S-P", postoperative_text, profile="postoperative_course")])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual([item["migration_status"] for item in result["migrations"]],
                         ["SUPPORT_ONLY", "SUPPORT_ONLY", "SUPPORT_ONLY", "TASK_OUT_OF_SCOPE"])
        self.assertEqual(result["migrations"][0]["reason"],
                         "ADMISSION_INTENT_NOT_INDEPENDENT_DECISION")
        self.assertEqual(result["migrations"][1]["reason"],
                         "ADMISSION_INTENT_NOT_INDEPENDENT_DECISION")
        self.assertEqual(len(result["support_records"]), 3)

    def test_five_spans_from_one_source_merge_into_one_review_candidate(self):
        text = "拟行手术。|复查后评估。|暂缓推进。|补充证据。|调整方案。"
        quotes = text.split("|")
        candidates = [candidate("EV-%d" % index, "S-FIVE", text, quote,
                                function="reassessment" if index % 2 else "new_decision")
                      for index, quote in enumerate(quotes)]
        result = self.project(candidates, [source("S-FIVE", text)])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["kind"], "management_decision")
        self.assertEqual(len(checkpoint["member_event_ids"]), 5)
        self.assertEqual(len(checkpoint["masked_spans"]), 5)
        self.assertTrue(checkpoint["program_generated_task_header"])
        self.assertEqual(checkpoint["management_question_source"],
                         "program_generated_task_header_not_model_quote")
        self.assertEqual(checkpoint["actionability_status"],
                         "DISCOVERY_REVIEWED_MANAGEMENT_CANDIDATE")
        self.assertEqual(len(checkpoint["candidate_actions"]), 5)
        self.assertEqual(checkpoint["review_status"], "REVIEW_REQUIRED")
        self.assertTrue(all(item["to_checkpoint_id"] == checkpoint["checkpoint_id"]
                            for item in result["migrations"]))

    def test_diagnostic_event_has_one_actionable_pre_exam_checkpoint(self):
        result = self.project(events=[diagnostic()], reviews=[
            review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
            review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        pre = result["candidate_checkpoints"][0]
        self.assertEqual(pre["kind"], "pre_exam_evaluation")
        self.assertEqual(pre["source_ids"], ["MR-1", "MR-2"])
        self.assertEqual(pre["clock"]["value"], "2026-03-02 08:00")
        self.assertEqual(pre["evaluation_time_role"], "before_exam_execution")
        self.assertTrue(pre["mask_whole_diagnostic_event"])
        self.assertFalse(pre["show_complete_diagnostic_event"])
        self.assertIn("胰腺增强MR", pre["management_question"])
        self.assertEqual(pre["candidate_actions"][0]["target"], "胰腺增强MR")
        self.assertEqual(pre["candidate_actions"][0]["reason"], "Discovery研究范围审核")
        self.assertEqual(result["diagnostic_events"][0]["generation_audits"][1]["reason"],
                         "SAME_DIAGNOSTIC_SOURCE_PRE_EXAM_CP_RETAINED")
        self.assertEqual(result["summary"]["diagnostic_events_with_multiple_checkpoints"], 0)

    def test_report_time_is_explicit_pre_exam_fallback(self):
        result = self.project(events=[diagnostic(exam_time=None)])
        pre = result["candidate_checkpoints"][0]
        self.assertEqual(pre["clock"]["value"], "2026-03-02 12:00")
        self.assertEqual(pre["time_basis"], "report_time_fallback_for_pre_exam_evaluation")
        self.assertTrue(pre["clock_degradation"]["degraded"])
        self.assertTrue(pre["clock_degradation"]["same_clock_as_evidence_arrival"])

    def test_pathology_pre_exam_uses_specimen_before_report_fallback(self):
        event = diagnostic(
            source_ids=["PATH-1"], exam_time=None,
            source_clocks={"PATH-1": {"specimen_time": dict(
                value="2026-03-02 09:30", precision="minute")}})
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("PATH-1", "合成病理报告。", kind="pathology")],
            events=[event])
        pre = result["candidate_checkpoints"][0]
        self.assertEqual(pre["clock"]["value"], "2026-03-02 09:30")
        self.assertEqual(pre["time_basis"], "specimen_time_fallback_for_pre_exam_evaluation")
        self.assertEqual(pre["evaluation_time_role"], "before_pathology_specimen_collection")
        self.assertEqual(pre["clock_degradation"], dict(
            degraded=True, basis="specimen_time_fallback_for_pre_exam_evaluation",
            same_clock_as_evidence_arrival=False,
            reason="SPECIMEN_TIME_USED_FOR_MISSING_PATHOLOGY_EXAM_TIME"))
        self.assertEqual(result["diagnostic_events"][0]["specimen_time"]["value"],
                         "2026-03-02 09:30")

    def test_pathology_specimen_equal_report_marks_actual_same_clock_review(self):
        event = diagnostic(
            source_ids=["PATH-1"], exam_time=None,
            report_time=dict(value="2026-03-02 12:00", precision="minute"),
            source_clocks={"PATH-1": {"specimen_time": dict(
                value="2026-03-02 12:00", precision="minute")}})
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[],
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("PATH-1", "合成病理报告。", kind="pathology")],
            diagnostic_events=[event], diagnostic_reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=dict(operation_start=dict(
                value="2026-03-02 10:00", precision="minute")))
        pre = next(item for item in result["candidate_checkpoints"]
                   if item["kind"] == "pre_exam_evaluation")
        post = next(item for item in result["candidate_checkpoints"]
                    if item["kind"] == "evidence_arrival_reassessment")
        self.assertEqual(pre["clock"]["value"], post["clock"]["value"])
        self.assertEqual(pre["time_basis"], "specimen_time_fallback_for_pre_exam_evaluation")
        self.assertTrue(pre["clock_degradation"]["same_clock_as_evidence_arrival"])
        self.assertEqual(pre["clock_degradation"]["review_status"], "REVIEW_REQUIRED")
        self.assertEqual(pre["clock_degradation"]["review_reason"],
                         "PRE_AND_EVIDENCE_ARRIVAL_SHARE_EVALUATION_CLOCK")
        self.assertEqual(pre["provenance"]["clock_degradation"],
                         pre["clock_degradation"])

    def test_conflicting_pathology_specimen_still_falls_back_to_report(self):
        event = diagnostic(
            source_ids=["PATH-1"], exam_time=None,
            source_clocks={"PATH-1": {"specimen_time": dict(
                value="2026-03-02 09:30", precision="minute")}},
            audit=dict(clock_inconsistent_flags=["specimen_time"]))
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("PATH-1", "合成病理报告。", kind="pathology")], events=[event])
        pre = result["candidate_checkpoints"][0]
        self.assertEqual(pre["clock"]["value"], "2026-03-02 12:00")
        self.assertEqual(pre["time_basis"], "report_time_fallback_for_pre_exam_evaluation")
        self.assertTrue(pre["clock_degradation"]["same_clock_as_evidence_arrival"])

    def test_ct_mr_does_not_use_specimen_time_fallback(self):
        event = diagnostic(
            source_ids=["CT-1"], exam_time=None,
            source_clocks={"CT-1": {"specimen_time": dict(
                value="2026-03-02 09:30", precision="minute")}})
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("CT-1", "合成CT报告。", kind="imaging")],
            events=[event])
        pre = result["candidate_checkpoints"][0]
        self.assertEqual(pre["clock"]["value"], "2026-03-02 12:00")
        self.assertEqual(pre["time_basis"], "report_time_fallback_for_pre_exam_evaluation")
        self.assertIsNone(result["diagnostic_events"][0]["specimen_time"]["value"])

    def test_pathology_exam_time_takes_precedence_over_specimen_time(self):
        event = diagnostic(
            source_ids=["PATH-1"],
            source_clocks={"PATH-1": {"specimen_time": dict(
                value="2026-03-02 07:30", precision="minute")}})
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("PATH-1", "合成病理报告。", kind="pathology")],
            events=[event])
        pre = result["candidate_checkpoints"][0]
        self.assertEqual(pre["clock"]["value"], "2026-03-02 08:00")
        self.assertEqual(pre["time_basis"], "exam_time")
        self.assertFalse(pre["clock_degradation"]["degraded"])

    def test_post_report_checkpoint_requires_separate_agent_acceptance(self):
        result = self.project(events=[diagnostic()], reviews=[
            review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
            review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR",
                   candidate_action=dict(action_type="ADVANCE_RESECTION", target="根治性切除"))])
        post = result["candidate_checkpoints"][0]
        self.assertEqual(post["kind"], "evidence_arrival_reassessment")
        self.assertEqual(post["clock"]["value"], "2026-03-02 12:00")
        self.assertTrue(post["show_complete_diagnostic_event"])
        self.assertFalse(post["mask_whole_diagnostic_event"])
        self.assertTrue(post["provenance"]["evidence_arrival_does_not_imply_evidence_used"])

    def test_missing_review_and_picc_rejection_do_not_become_checkpoints(self):
        missing = self.project(events=[diagnostic()], reviews=[])
        self.assertEqual(missing["candidate_checkpoints"], [])
        rejected = self.project(events=[diagnostic()], reviews=[
            review("pre_exam_evaluation", "no", reason="PICC仅用于输液通路",
                   diagnostic_event_id="DE-MR")])
        self.assertEqual(rejected["candidate_checkpoints"], [])
        self.assertEqual(rejected["migrations"][0]["migration_status"], "TASK_OUT_OF_SCOPE")
        self.assertEqual(rejected["migrations"][0]["reason"],
                         "DISCOVERY_AGENT_REJECTED_OUT_OF_RESEARCH_SCOPE")

    def test_missing_diagnostic_clocks_generate_audits_not_fake_checkpoints(self):
        event = diagnostic(event_id="DE-NO-CLOCK", exam_time=None,
                           report_time=None, verified_time=None)
        result = self.project(events=[event], reviews=[
            review("pre_exam_evaluation", diagnostic_event_id="DE-NO-CLOCK"),
            review("evidence_arrival_reassessment", diagnostic_event_id="DE-NO-CLOCK")])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual([item["migration_status"] for item in result["migrations"]],
                         ["REVIEW_REQUIRED", "NOT_GENERATED"])
        self.assertEqual([item["reason"] for item in result["migrations"]],
                         ["MISSING_EXAM_AND_REPORT_TIME_FOR_EVALUATION_ANCHOR",
                          "MISSING_REPORT_AND_VERIFIED_TIME_VISIBILITY_UNKNOWN"])
        self.assertEqual(result["diagnostic_events"][0]["visibility_basis"],
                         "unknown")
        self.assertFalse(any(item["generated"] for item in
                             result["diagnostic_events"][0]["generation_audits"]))

    def test_checkpoint_ids_are_stable_under_input_reordering(self):
        text = "拟行手术。|复查后评估。"
        candidates = [candidate("EV-A", "S", text, "拟行手术。"),
                      candidate("EV-B", "S", text, "复查后评估。",
                                function="reassessment")]
        sources = [source("S", text)]
        first = self.project(candidates, sources, [diagnostic()])
        second = self.project(list(reversed(candidates)), sources, [diagnostic()])
        self.assertEqual({item["checkpoint_id"] for item in first["candidate_checkpoints"]},
                         {item["checkpoint_id"] for item in second["candidate_checkpoints"]})
        repeated = self.project(candidates, sources, [diagnostic()])
        self.assertEqual(first, repeated)

    def test_all_38_candidate_migrations_are_conserved(self):
        text = "|".join("决定%d。" % index for index in range(38))
        candidates = [candidate("EV-%02d" % index, "S-38", text,
                                "决定%d。" % index) for index in range(38)]
        result = self.project(candidates, [source("S-38", text)])
        candidate_migrations = [item for item in result["migrations"]
                                if item["migration_type"] == "candidate_span"]
        self.assertEqual(len(candidate_migrations), 38)
        self.assertEqual({item["event_id"] for item in candidate_migrations},
                         {item["event_id"] for item in candidates})
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertTrue(result["summary"]["candidate_migration_conserved"])
        self.assertEqual(result["summary"]["candidate_migration_count"], 38)
        self.assertFalse(result["summary"]["clinical_accuracy_assessed"])

    def test_action_plan_0_3_separates_plan_status_from_three_clinical_modes(self):
        from jsonschema import validate, ValidationError

        text = "拟行手术。"
        result = self.project([candidate("EV-A", "S", text, text)],
                              [source("S", text)], [diagnostic()])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        for checkpoint in result["candidate_checkpoints"]:
            space = checkpoint["action_space"]
            self.assertEqual(space["schema_version"], "management-action-plan-0.3.0")
            self.assertEqual(space["dictionary_version"], "action-dictionary-0.2.0-draft")
            self.assertEqual(len(space["dictionary_hash"]), 64)
            self.assertEqual(space["dictionary_status"], "DRAFT_NOT_CLINICAL_REFERENCE")
            self.assertEqual(space["modes"],
                             ["ADVANCE", "HOLD_FOR_EVIDENCE", "REDIRECT"])
            self.assertEqual(space["plan_statuses"],
                             ["ACTIONS_PROPOSED", "NO_NEW_ACTION", "ABSTAIN"])
            schema = space["action_plan_schema"]
            self.assertEqual(schema["required"], ["plan_status", "actions", "rationale"])
            item_schema = schema["properties"]["actions"]["items"]
            self.assertEqual(item_schema["required"], ["action_id"])
            self.assertIn("DIAG_PANCREATIC_CONTRAST_CT",
                          item_schema["properties"]["action_id"]["enum"])
            self.assertIn("action_type", item_schema["properties"])
            self.assertIn("target", item_schema["properties"])
            proposed = dict(plan_status="ACTIONS_PROPOSED", mode="ADVANCE", rationale="合成理由",
                            evidence_to_obtain=["增强CT"],
                            actions=[dict(action_id="DIAG_PANCREATIC_CONTRAST_CT",
                                          action_type="ORDER_DIAGNOSTIC_TEST",
                                          target="胰腺增强CT扫描",
                                          uncertainty_addressed="待明确问题",
                                          dependencies=["前置条件"], conditions=["适用条件"])])
            validate(proposed, schema)
            validate(dict(plan_status="ACTIONS_PROPOSED", mode="HOLD_FOR_EVIDENCE",
                          rationale="只需选择ID", actions=[dict(
                              action_id="DIAG_PANCREATIC_CONTRAST_CT")]), schema)
            with self.assertRaises(ValidationError):
                validate(dict(plan_status="ACTIONS_PROPOSED", mode="ADVANCE", rationale="自由文本",
                              actions=[dict(action_type="imaging", target="CT")]), schema)
            with self.assertRaises(ValidationError):
                validate(dict(proposed, actions=[dict(action_id="A-1")]), schema)
            validate(dict(plan_status="NO_NEW_ACTION", mode="HOLD_FOR_EVIDENCE",
                          actions=[], rationale="无需新增动作"), schema)
            validate(dict(plan_status="ABSTAIN", actions=[], rationale="技术拒答"), schema)
            with self.assertRaises(ValidationError):
                validate(dict(plan_status="ACTIONS_PROPOSED", mode="ADVANCE", actions=[],
                              rationale="缺少动作"), schema)
            with self.assertRaises(ValidationError):
                validate(dict(plan_status="NO_NEW_ACTION", mode="ADVANCE",
                              actions=proposed["actions"], rationale="应为空"), schema)
            with self.assertRaises(ValidationError):
                validate(dict(plan_status="ABSTAIN", mode="ABSTAIN", actions=[],
                              rationale="不得成为第四Mode"), schema)
            for status in ("NO_NEW_ACTION", "ABSTAIN"):
                with self.subTest(status=status, field="evidence_to_obtain"):
                    with self.assertRaises(ValidationError):
                        validate(dict(plan_status=status, mode="ADVANCE", actions=[],
                                      rationale="不得偷带检查", evidence_to_obtain=["增强CT"]), schema)
            self.assertTrue(space["abstain_is_technical_status_not_mode"])
            self.assertFalse(space["answer_prefilled"])

    def test_action_dictionary_is_engineering_draft_not_prefilled_reference(self):
        path = Path(__file__).parents[4] / "configs" / "action_dictionary.v0.2.draft.json"
        dictionary = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(dictionary["status"], "DRAFT_NOT_CLINICAL_REFERENCE")
        self.assertFalse(dictionary["clinical_reference"])
        self.assertFalse(dictionary["required_patient_bundle"])
        self.assertFalse(dictionary["prefill_blind_review_answers"])
        self.assertEqual(set(dictionary["freeze_requirements"][key] for key in
                             ("historical_usage", "current_usage", "center_availability")),
                         {"PENDING_FREEZE"})
        labels = " ".join(item["label"] for item in dictionary["actions"])
        for expected in ("胰腺增强CT", "腹部平扫CT", "胸部CT", "胸部平片", "胰腺MR",
                         "MRCP", "EUS", "FNA", "穿刺", "分期腹腔镜", "CA19-9",
                         "外院原始影像", "MDT", "胰十二指肠切除", "远端胰腺切除",
                         "全胰腺切除", "AG", "FOLFIRINOX", "PTCD", "ERCP",
                         "术后病理复核", "术后复查CT", "术后辅助治疗MDT", "随访"):
            self.assertIn(expected, labels)
        self.assertTrue(all(not item["required_for_every_patient"]
                            for item in dictionary["actions"]))
        self.assertNotIn("surgery", {item["label"] for item in dictionary["actions"]})
        self.assertNotIn("imaging", {item["label"] for item in dictionary["actions"]})

    def test_legacy_frozen_free_text_action_still_projects_without_inventing_an_id(self):
        text = "讨论后拟行胰腺增强CT。"
        item = candidate("EV-LEGACY", "S-LEGACY", text, text,
                         candidate_action=dict(action_type="imaging", target="增强CT"))
        result = self.project([item], [source("S-LEGACY", text)])
        action = result["candidate_checkpoints"][0]["candidate_actions"][0]
        self.assertEqual(action["action_type"], "imaging")
        self.assertEqual(action["target"], "增强CT")
        self.assertNotIn("action_id", action)
        self.assertEqual(result["candidate_checkpoints"][0]["action_space"]["schema_version"],
                         "management-action-plan-0.3.0")

    def test_missing_management_clock_cannot_create_empty_checkpoint(self):
        text = "拟行手术。"
        item = candidate("EV-NO-CLOCK", "S", text, text, clock=None)
        result = self.project([item], [source("S", text, clock=None)])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["migrations"][0]["reason"], "MISSING_EVALUATION_CLOCK")

    def test_placeholder_only_report_cannot_create_evidence_arrival_checkpoint(self):
        event = diagnostic(audit=dict(placeholder_only_event=True))
        result = self.project(
            events=[event],
            reviews=[review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
                     review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][1]["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["migrations"][1]["reason"],
                         "PLACEHOLDER_ONLY_REPORT_NOT_EVIDENCE_ARRIVAL")
        self.assertIsNone(result["diagnostic_events"][0]["candidate_checkpoint_ids"].get(
            "evidence_arrival_reassessment"))

    def test_operation_start_separates_preoperative_and_postoperative_diagnostic_nodes(self):
        event = diagnostic(
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            report_time=dict(value="2026-03-02 12:00", precision="minute"))
        bounds = dict(operation_start=dict(
            value="2026-03-02 10:00", precision="minute"), status="TEST")
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[],
            sources=[source("S-CONTEXT", "既往临床资料。")], diagnostic_events=[event],
            diagnostic_reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=bounds)
        self.assertEqual([item["kind"] for item in result["candidate_checkpoints"]],
                         ["pre_exam_evaluation", "evidence_arrival_reassessment"])
        pre, post = result["candidate_checkpoints"]
        self.assertTrue(pre["primary_evaluation_eligible"])
        self.assertFalse(post["primary_evaluation_eligible"])
        self.assertEqual(post["evaluation_scope"], "NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY")
        self.assertEqual(result["migrations"][1]["migration_status"], "RETAINED")
        self.assertEqual(result["summary"]["primary_evaluation_checkpoint_count"], 1)
        self.assertEqual(result["summary"]["non_primary_evaluation_checkpoint_count"], 1)

    def test_postoperative_after_flags_preoperative_action_without_rewriting_it(self):
        for action_type, target in (("START_NAT", "新辅助化疗"),
                                    ("ADVANCE_RESECTION", "根治性切除"),
                                    ("MANAGE", "胰十二指肠切除术")):
            with self.subTest(action_type=action_type, target=target):
                event = diagnostic(
                    exam_time=dict(value="2026-03-02 08:00", precision="minute"),
                    report_time=dict(value="2026-03-02 12:00", precision="minute"))
                result = project_candidate_checkpoints(
                    case_id="SYN", candidates=[],
                    sources=[source("S-CONTEXT", "既往临床资料。")], diagnostic_events=[event],
                    diagnostic_reviews=[
                        review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
                        review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR",
                               candidate_action=dict(action_type=action_type, target=target))],
                    task_time_bounds=dict(operation_start=dict(
                        value="2026-03-02 10:00", precision="minute")))
                post = result["candidate_checkpoints"][0]
                self.assertEqual(post["kind"], "evidence_arrival_reassessment")
                self.assertFalse(post["primary_evaluation_eligible"])
                self.assertEqual(post["action_task_review_flags"],
                                 ["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"])
                self.assertEqual(post["candidate_actions"][0]["action_type"], action_type)
                self.assertEqual(post["candidate_actions"][0]["target"], target)
                audit = result["diagnostic_events"][0]["generation_audits"][1]
                self.assertEqual(audit["action_task_review_flags"],
                                 ["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"])
                self.assertEqual(result["migrations"][1]["action_task_review_flags"],
                                 ["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"])

    def test_preoperative_exam_reported_after_operation_needs_review_without_action(self):
        event = diagnostic(
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            report_time=dict(value="2026-03-02 12:00", precision="minute"))
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[], sources=[source("S-CONTEXT", "既往临床资料。")],
            diagnostic_events=[event], diagnostic_reviews=[
                review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR",
                       candidate_action=None)],
            task_time_bounds=dict(operation_start=dict(
                value="2026-03-02 10:00", precision="minute")))
        post = result["candidate_checkpoints"][0]
        self.assertEqual(post["candidate_actions"], [])
        self.assertEqual(post["action_task_review_flags"],
                         ["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"])

    def test_postoperative_after_diagnostic_action_has_no_task_mismatch_flag(self):
        event = diagnostic(
            exam_time=dict(value="2026-03-02 11:00", precision="minute"),
            report_time=dict(value="2026-03-02 12:00", precision="minute"))
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[], sources=[source("S-CONTEXT", "既往临床资料。")],
            diagnostic_events=[event], diagnostic_reviews=[
                review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR",
                       candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST", target="复查CT"))],
            task_time_bounds=dict(operation_start=dict(
                value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(result["candidate_checkpoints"][0]["action_task_review_flags"], [])
        recurrence = project_candidate_checkpoints(
            case_id="SYN", candidates=[], sources=[source("S-CONTEXT", "既往临床资料。")],
            diagnostic_events=[event], diagnostic_reviews=[
                review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR",
                       candidate_action=dict(
                           action_type="reassessment", target="胰十二指肠切除术后术区复发可能性大"))],
            task_time_bounds=dict(operation_start=dict(
                value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(recurrence["candidate_checkpoints"][0]["action_task_review_flags"], [])

    def test_operation_day_coarse_diagnostic_clocks_still_require_review(self):
        event = diagnostic(exam_time="2026-03-02", report_time="2026-03-02")
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[],
            sources=[source("S-CONTEXT", "既往临床资料。")], diagnostic_events=[event],
            diagnostic_reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=dict(operation_start=dict(value="2026-03-02 10:00")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual([item["reason"] for item in result["migrations"]],
                         ["OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN"] * 2)

    def test_cross_source_action_merge_does_not_drop_postoperative_report(self):
        event = diagnostic(
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            report_time=dict(value="2026-03-02 12:00", precision="minute"))
        text = "拟行胰腺增强MR。"
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[candidate("EV-PLAN", "S-PLAN", text, text)],
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("S-PLAN", text, clock="2026-03-01 09:00")],
            diagnostic_events=[event], diagnostic_reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=dict(operation_start=dict(
                value="2026-03-02 10:00", precision="minute")))
        post = next(item for item in result["candidate_checkpoints"]
                    if item["kind"] == "evidence_arrival_reassessment")
        self.assertFalse(post["primary_evaluation_eligible"])
        self.assertEqual(result["diagnostic_events"][0]["candidate_checkpoint_ids"][
            "evidence_arrival_reassessment"], post["checkpoint_id"])

    def test_same_source_management_does_not_drop_postoperative_report(self):
        event = diagnostic(
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            report_time=dict(value="2026-03-02 12:00", precision="minute"))
        text = "拟行胰腺增强MR。"
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[candidate(
                "EV-PLAN", "MR-1", text, text, clock="2026-03-02 08:00")],
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("MR-1", text, clock="2026-03-02 08:00")],
            diagnostic_events=[event], diagnostic_reviews=[
                review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=dict(operation_start=dict(
                value="2026-03-02 10:00", precision="minute")))
        post = next(item for item in result["candidate_checkpoints"]
                    if item["kind"] == "evidence_arrival_reassessment")
        self.assertFalse(post["primary_evaluation_eligible"])

    def test_management_empty_candidate_clock_uses_source_for_operation_day_review(self):
        text = "拟行手术。"
        item = candidate("EV-DATE", "S-DATE", text, text)
        item["clock"] = dict(value=None, precision="unknown")
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[item],
            sources=[source("S-DATE", text, clock="2026-03-02")], diagnostic_events=[],
            task_time_bounds=dict(operation_start=dict(
                value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "REVIEW_REQUIRED")

    def test_date_only_operation_start_makes_same_day_diagnostic_order_unknown(self):
        event = diagnostic(
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            report_time=dict(value="2026-03-02 12:00", precision="minute"))
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[], sources=[source("S-CONTEXT", "既往临床资料。")],
            diagnostic_events=[event], diagnostic_reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=dict(operation_start=dict(value="2026-03-02", precision="date")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual([item["reason"] for item in result["migrations"]],
                         ["OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN"] * 2)

    def test_conflicting_report_visibility_is_not_repaired_by_verified_time(self):
        event = diagnostic(
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            report_time=dict(value=None, precision="unknown"),
            verified_time=dict(value="2026-03-02 13:00", precision="minute"),
            visibility_clock=dict(value=None, precision="unknown"),
            visibility_basis="unknown",
            audit=dict(clock_inconsistent_flags=["report_time"]))
        result = self.project(events=[event], reviews=[
            review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
            review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")])
        self.assertEqual([item["kind"] for item in result["candidate_checkpoints"]],
                         ["pre_exam_evaluation"])
        self.assertEqual(result["migrations"][1]["reason"],
                         "MISSING_OR_CONFLICTING_REPORT_VISIBILITY")

    def test_pre_exam_without_visible_clinical_context_is_not_a_checkpoint(self):
        event = diagnostic()
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[], sources=[], diagnostic_events=[event],
            diagnostic_reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=dict(operation_start=dict(value=None)))
        self.assertEqual([item["kind"] for item in result["candidate_checkpoints"]],
                         ["evidence_arrival_reassessment"])
        self.assertEqual(result["migrations"][0]["reason"],
                         "NO_VISIBLE_PRE_EXAM_CLINICAL_CONTEXT")

    def test_same_source_management_and_pre_exam_become_one_checkpoint(self):
        text = "阅片后拟行胰十二指肠切除术。"
        report = source("MR-1", text)
        item = candidate("EV-POST", "MR-1", text, text)
        event = diagnostic(source_ids=["MR-1"])
        result = self.project([item], [source("S-CONTEXT", "既往资料。"), report], [event])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertEqual(result["candidate_checkpoints"][0]["kind"], "pre_exam_evaluation")
        migration = next(item for item in result["migrations"] if item["event_id"] == "EV-POST")
        self.assertEqual(migration["migration_status"], "SUPPORT_ONLY")
        self.assertEqual(migration["to_checkpoint_id"],
                         result["candidate_checkpoints"][0]["checkpoint_id"])

    def test_same_source_management_blocks_post_report_checkpoint(self):
        text = "阅片后拟行胰十二指肠切除术。"
        report = source("MR-1", text)
        item = candidate("EV-POST", "MR-1", text, text)
        event = diagnostic(source_ids=["MR-1"])
        result = self.project([item], [source("S-CONTEXT", "既往资料。"), report], [event], reviews=[
            review("pre_exam_evaluation", "no", diagnostic_event_id="DE-MR"),
            review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")])
        self.assertEqual([cp["kind"] for cp in result["candidate_checkpoints"]],
                         ["management_decision"])
        post_migration = next(item for item in result["migrations"]
                              if item.get("node_kind") == "evidence_arrival_reassessment")
        self.assertEqual(post_migration["migration_status"], "SUPPORT_ONLY")
        self.assertEqual(post_migration["to_checkpoint_id"],
                         result["candidate_checkpoints"][0]["checkpoint_id"])

    def test_cross_source_same_diagnostic_action_uses_management_checkpoint(self):
        text = "讨论后拟行胰腺增强CT。"
        item = candidate("EV-CT", "S-M", text, text,
                         candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST",
                                               target="胰腺增强CT"))
        event = diagnostic(source_ids=["MR-1"])
        result = self.project([item], [source("S-M", text), source("MR-1", "CT报告")], [event], reviews=[
            review("pre_exam_evaluation", diagnostic_event_id="DE-MR",
                   candidate_action=dict(action_type="imaging", target="增强CT"))])
        self.assertEqual([cp["kind"] for cp in result["candidate_checkpoints"]],
                         ["management_decision"])
        pre_migration = next(item for item in result["migrations"]
                             if item.get("node_kind") == "pre_exam_evaluation")
        self.assertEqual(pre_migration["migration_status"], "SUPPORT_ONLY")
        self.assertEqual(pre_migration["to_checkpoint_id"],
                         result["candidate_checkpoints"][0]["checkpoint_id"])

    def test_pathology_management_claim_uses_the_same_specimen_anchor(self):
        text = "讨论后拟行病理活检。"
        item = candidate(
            "EV-PATH", "S-PLAN", text, text, clock="2026-03-02 10:00",
            candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST", target="病理活检"))
        event = diagnostic(
            source_ids=["PATH-1"], exam_time=None,
            source_clocks={"PATH-1": {"specimen_time": dict(
                value="2026-03-02 09:30", precision="minute")}})
        result = self.project(
            [item], [source("S-CONTEXT", "既往资料。"), source("S-PLAN", text),
                     source("PATH-1", "病理报告。", kind="pathology")], [event], reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR",
                       candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST", target="病理活检"))])
        pre = next(cp for cp in result["candidate_checkpoints"] if cp["kind"] == "pre_exam_evaluation")
        self.assertEqual(pre["time_basis"], "specimen_time_fallback_for_pre_exam_evaluation")
        self.assertEqual(result["diagnostic_events"][0]["generation_audits"][0]["reason"],
                         "specimen_time_fallback_for_pre_exam_evaluation")

    def test_one_management_action_does_not_suppress_later_distinct_ct(self):
        text = "讨论后拟行胰腺增强CT。"
        item = candidate("EV-CT", "S-M", text, text,
                         candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST",
                                               target="胰腺增强CT"))
        first = diagnostic(event_id="DE-CT-1", source_ids=["CT-1"])
        second = diagnostic(event_id="DE-CT-2", source_ids=["CT-2"],
                            exam_time="2026-03-04 08:00", report_time="2026-03-04 12:00")
        reviews = [review("pre_exam_evaluation", diagnostic_event_id=event["event_id"],
                          candidate_action=dict(action_type="imaging", target="增强CT"))
                   for event in (first, second)]
        result = self.project([item], [source("S-M", text), source("CT-1", "第一次"),
                                       source("CT-2", "第二次")], [first, second], reviews=reviews)
        self.assertEqual([cp["kind"] for cp in result["candidate_checkpoints"]],
                         ["management_decision", "pre_exam_evaluation"])
        claimed = next(item for item in result["diagnostic_events"]
                       if item["event_id"] == "DE-CT-1")
        later = next(item for item in result["diagnostic_events"]
                     if item["event_id"] == "DE-CT-2")
        self.assertEqual(claimed["generation_audits"][0]["reason"],
                         "SAME_DIAGNOSTIC_ACTION_MANAGEMENT_CP_RETAINED")
        self.assertTrue(later["generation_audits"][0]["generated"])

    def test_management_candidate_without_reference_action_remains_a_checkpoint(self):
        text = "拟行手术。"
        item = candidate("EV-NO-ACTION", "S", text, text, candidate_action={})
        result = self.project([item], [source("S", text)])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertEqual(result["candidate_checkpoints"][0]["candidate_actions"], [])
        self.assertEqual(result["migrations"][0]["reason"],
                         "MANAGEMENT_CANDIDATE_PENDING_REVIEW")

    def test_diagnostic_candidate_without_reference_action_remains_a_checkpoint(self):
        result = self.project(
            events=[diagnostic()],
            reviews=[review("pre_exam_evaluation", diagnostic_event_id="DE-MR",
                            candidate_action=None)])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertEqual(result["candidate_checkpoints"][0]["candidate_actions"], [])

    def test_same_source_support_record_has_checkpoint_destination(self):
        text = "拟行手术。前次已执行。"
        kept = candidate("EV-KEEP", "S", text, "拟行手术。")
        support = candidate("EV-SUPPORT", "S", text, "前次已执行。", function="repeat")
        result = self.project([kept, support], [source("S", text)])
        checkpoint_id = result["candidate_checkpoints"][0]["checkpoint_id"]
        self.assertEqual(result["migrations"][1]["to_checkpoint_id"], checkpoint_id)
        self.assertEqual(result["support_records"][0]["to_checkpoint_id"], checkpoint_id)

    def test_inputs_are_not_modified(self):
        text = "拟行手术。"
        candidates = [candidate("EV-A", "S", text, text)]
        sources = [source("S", text)]
        events = [diagnostic()]
        before = deepcopy((candidates, sources, events))
        result = self.project(candidates, sources, events)
        self.assertEqual((candidates, sources, events), before)
        result["candidate_checkpoints"][0]["source_ids"].append("MUTATED")
        self.assertEqual((candidates, sources, events), before)


if __name__ == "__main__":
    unittest.main()

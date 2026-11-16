"""Offline candidate Checkpoint reprojection; all records are synthetic."""
from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_checkpoints import (
    project_candidate_checkpoints,
)


def source(source_id, text, *, profile="preoperative_course", clock="2026-03-01"):
    return dict(source_id=source_id, text=text, document_profile=profile,
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
        admission = candidate("EV-ADMIT", "S-A", admission_text, admission_text)
        repeat = candidate("EV-REPEAT", "S-R", repeat_text, repeat_text,
                           function="repeat")
        postoperative = candidate("EV-POST", "S-P", postoperative_text,
                                  postoperative_text)
        result = self.project(
            [admission, repeat, postoperative],
            [source("S-A", admission_text), source("S-R", repeat_text),
             source("S-P", postoperative_text, profile="postoperative_course")])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual([item["migration_status"] for item in result["migrations"]],
                         ["SUPPORT_ONLY", "SUPPORT_ONLY", "TASK_OUT_OF_SCOPE"])
        self.assertEqual(result["migrations"][0]["reason"],
                         "ADMISSION_INTENT_NOT_INDEPENDENT_DECISION")
        self.assertEqual(len(result["support_records"]), 2)

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
                         "DISCOVERY_AGENT_ACTIONABLE_CANDIDATE")
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

    def test_every_retained_checkpoint_has_concrete_action_plan_schema(self):
        text = "拟行手术。"
        result = self.project([candidate("EV-A", "S", text, text)],
                              [source("S", text)], [diagnostic()])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        for checkpoint in result["candidate_checkpoints"]:
            space = checkpoint["action_space"]
            self.assertEqual(space["modes"],
                             ["ADVANCE", "HOLD_FOR_EVIDENCE", "REDIRECT"])
            schema = space["action_plan_schema"]
            self.assertEqual(schema["required"], ["mode", "actions", "rationale"])
            self.assertEqual(schema["properties"]["actions"]["minItems"], 1)
            self.assertEqual(schema["properties"]["actions"]["items"]["required"],
                             ["action_type", "target", "timing"])
            self.assertFalse(space["answer_prefilled"])

    def test_missing_management_clock_cannot_create_empty_checkpoint(self):
        text = "拟行手术。"
        item = candidate("EV-NO-CLOCK", "S", text, text, clock=None)
        result = self.project([item], [source("S", text, clock=None)])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["migrations"][0]["reason"], "MISSING_EVALUATION_CLOCK")

    def test_operation_start_separates_preoperative_and_postoperative_diagnostic_nodes(self):
        event = diagnostic(exam_time="2026-03-02 08:00", report_time="2026-03-02 12:00")
        bounds = dict(operation_start=dict(value="2026-03-02 10:00"), status="TEST")
        result = project_candidate_checkpoints(
            case_id="SYN", candidates=[],
            sources=[source("S-CONTEXT", "既往临床资料。")], diagnostic_events=[event],
            diagnostic_reviews=[
                review("pre_exam_evaluation", diagnostic_event_id="DE-MR"),
                review("evidence_arrival_reassessment", diagnostic_event_id="DE-MR")],
            task_time_bounds=bounds)
        self.assertEqual([item["kind"] for item in result["candidate_checkpoints"]],
                         ["pre_exam_evaluation"])
        self.assertEqual(result["migrations"][1]["migration_status"], "TASK_OUT_OF_SCOPE")
        self.assertEqual(result["migrations"][1]["reason"],
                         "AFTER_OPERATION_START_TASK_OUT_OF_SCOPE")

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

    def test_management_candidate_without_concrete_action_is_not_a_checkpoint(self):
        text = "拟行手术。"
        item = candidate("EV-NO-ACTION", "S", text, text, candidate_action={})
        result = self.project([item], [source("S", text)])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["reason"],
                         "DISCOVERY_CONCRETE_ACTION_REQUIRED")

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

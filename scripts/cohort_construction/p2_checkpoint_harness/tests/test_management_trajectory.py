"""Goal-oriented trajectory projection tests; every clinical record is synthetic."""
from copy import deepcopy
import hashlib
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials import (
    project_candidate_materials,
)
from scripts.cohort_construction.p2_checkpoint_harness.stages.management_trajectory import (
    project_management_trajectory,
)


BEFORE = "BEFORE_ACQUISITION"
AFTER = "AFTER_EVIDENCE_ARRIVAL"
REASSESSMENT = "MANAGEMENT_REASSESSMENT"


def source(source_id, text, *, profile="preoperative_course", clock="2026-03-01"):
    return dict(source_id=source_id, text=text, title=source_id + "-标题",
                document_profile=profile, clock=dict(value=clock),
                clock_basis="body_record_time",
                deidentified_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                source_sha256=hashlib.sha256(text.encode()).hexdigest())


def candidate(event_id, source_id, text, quote, *, clock="2026-03-01",
              function="new_decision", action_type="ADVANCE_RESECTION",
              target="胰十二指肠切除术", **kwargs):
    start = text.index(quote)
    return dict(event_id=event_id, status="ACCEPTED", scope="yes",
                record_function=function, clock=dict(value=clock),
                clock_basis="body_record_time",
                candidate_action=dict(action_type=action_type, target=target),
                span=dict(source_id=source_id, quote=quote,
                          start=start, end=start + len(quote)), **kwargs)


def diagnostic(event_id, source_id, *, exam="2026-03-02 08:00",
               report="2026-03-02 12:00"):
    return dict(event_id=event_id, source_ids=[source_id],
                exam_time=dict(value=exam, precision="minute") if exam else None,
                report_time=dict(value=report, precision="minute") if report else None,
                verified_time=None, visibility_clock=None, visibility_basis=None,
                source_clocks={})


def review(event_id, kind, *, target="根治性切除", action_type="ADVANCE_RESECTION",
           disposition="yes"):
    return dict(diagnostic_event_id=event_id, candidate_kind=kind,
                disposition=disposition, reason="合成Discovery审核",
                candidate_action=dict(action_type=action_type, target=target))


class ManagementTrajectoryTests(unittest.TestCase):
    def project(self, *, candidates=(), sources=(), events=(), reviews=(), bounds=None,
                adjudications=None):
        return project_management_trajectory(
            case_id="SYN-TRAJECTORY", candidates=candidates, sources=sources,
            diagnostic_events=events, diagnostic_reviews=reviews,
            task_time_bounds=bounds or dict(operation_start=dict(value=None)),
            checkpoint_adjudications=adjudications)

    def provisional_management_id(self, candidates, sources):
        result = self.project(candidates=candidates, sources=sources)
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        return result["candidate_checkpoints"][0]["checkpoint_id"]

    def test_old_raw_span_is_rejected_before_trajectory_masking(self):
        raw = "姓名：张测试。拟行胰十二指肠切除术。"
        item = candidate("EV-OLD", "S-OLD", raw, "拟行胰十二指肠切除术。")
        sanitized = "[姓名已去标识化]拟行胰十二指肠切除术。"
        sources = [source("S-OLD", sanitized)]
        with self.assertRaisesRegex(ValueError, "CANDIDATE_SPAN_SOURCE_TEXT_MISMATCH"):
            self.project(candidates=[item], sources=sources)

    def test_same_ct_has_two_cutoffs_and_before_question_does_not_leak_target(self):
        event = diagnostic("DE-CT", "CT-1")
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"), source("CT-1", "CT报告。")],
            events=[event], reviews=[
                review("DE-CT", "pre_exam_evaluation", target="胰腺增强CT",
                       action_type="ORDER_DIAGNOSTIC_TEST"),
                review("DE-CT", "evidence_arrival_reassessment")])
        self.assertEqual([cp["kind"] for cp in result["candidate_checkpoints"]],
                         [BEFORE, AFTER])
        before, after = result["candidate_checkpoints"]
        self.assertEqual(before["clock"]["value"], "2026-03-02 08:00")
        self.assertEqual(after["clock"]["value"], "2026-03-02 12:00")
        self.assertNotIn("CT", before["management_question"])
        self.assertNotIn("胰腺增强CT", before["management_question"])
        self.assertEqual(before["candidate_actions"][0]["target"], "胰腺增强CT")
        self.assertFalse(before["candidate_actions"][0]["answer_prefilled"])
        self.assertFalse(before["candidate_actions"][0]["clinical_reference_created"])
        self.assertEqual(result["diagnostic_events"][0]["candidate_checkpoint_ids"], {
            BEFORE: before["checkpoint_id"], AFTER: after["checkpoint_id"]})
        self.assertEqual(result["summary"]["diagnostic_events_with_multiple_checkpoints"], 1)

    def test_duplicate_surgery_documents_merge_and_keep_every_support_source(self):
        records = [
            ("EV-PLAN", "S-PLAN", "拟行胰十二指肠切除术。", "new_decision", "2026-03-01"),
            ("EV-SUMMARY", "S-SUMMARY", "小结：拟行胰十二指肠切除术。", "reassessment", "2026-03-02"),
            ("EV-REPEAT", "S-REPEAT", "再次回述拟行胰十二指肠切除术。", "repeat", "2026-03-03"),
        ]
        candidates = [candidate(event_id, source_id, text, text, clock=clock,
                                function=function)
                      for event_id, source_id, text, function, clock in records]
        sources = [source(source_id, text, clock=clock)
                   for _, source_id, text, _, clock in records]
        result = self.project(candidates=candidates, sources=sources)
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["kind"], REASSESSMENT)
        self.assertEqual(checkpoint["source_ids"], ["S-PLAN", "S-REPEAT", "S-SUMMARY"])
        self.assertEqual(checkpoint["support_source_ids"], checkpoint["source_ids"])
        self.assertEqual({span["source_id"] for span in checkpoint["masked_spans"]},
                         set(checkpoint["source_ids"]))
        self.assertIn("EV-REPEAT", checkpoint["supporting_event_ids"])
        self.assertIn("repeat", checkpoint["record_functions"])
        self.assertEqual(result["support_records"][0]["event_id"], "EV-REPEAT")
        cycle = result["action_cycles"][0]
        self.assertEqual(cycle["support_source_ids"], checkpoint["support_source_ids"])
        self.assertEqual(cycle["member_event_ids"], ["EV-PLAN", "EV-SUMMARY"])

    def test_pathology_arrival_accepts_nat_without_duplicate_chemotherapy_cp(self):
        text = "病理结果回报后拟开始新辅助化疗。"
        management = candidate(
            "EV-NAT", "S-NAT", text, text, clock="2026-03-02 13:00",
            action_type="START_NAT", target="新辅助化疗",
            input_evidence_ids=["DE-PATH"], new_evidence_event_ids=["DE-PATH"])
        event = diagnostic("DE-PATH", "PATH-1")
        result = self.project(
            candidates=[management],
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("PATH-1", "病理报告。"), source("S-NAT", text,
                                                          clock="2026-03-02 13:00")],
            events=[event], reviews=[
                review("DE-PATH", AFTER, target="新辅助化疗", action_type="START_NAT")])
        self.assertEqual([cp["kind"] for cp in result["candidate_checkpoints"]], [AFTER])
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["candidate_actions"][0]["action_type"], "START_NAT")
        self.assertIn("S-NAT", checkpoint["support_source_ids"])
        self.assertEqual(checkpoint["supporting_event_ids"], ["EV-NAT"])
        migration = next(item for item in result["migrations"]
                         if item["event_id"] == "EV-NAT")
        self.assertEqual(migration["migration_status"], "SUPPORT_ONLY")
        self.assertEqual(migration["to_checkpoint_id"], checkpoint["checkpoint_id"])

    def test_simultaneous_ct_mr_do_not_merge_joint_arrival_without_declaration(self):
        events = [diagnostic("DE-CT", "CT-1"), diagnostic("DE-MR", "MR-1")]
        reviews = []
        for event, target in zip(events, ("增强CT", "增强MR")):
            reviews.extend([
                review(event["event_id"], BEFORE, target=target,
                       action_type="ORDER_DIAGNOSTIC_TEST"),
                review(event["event_id"], AFTER)])
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("CT-1", "CT报告。"), source("MR-1", "MR报告。")],
            events=events, reviews=reviews)
        self.assertEqual(len(result["candidate_checkpoints"]), 4)
        self.assertEqual([cp["kind"] for cp in result["candidate_checkpoints"]],
                         [BEFORE, BEFORE, AFTER, AFTER])
        first_ids, second_ids = [event["candidate_checkpoint_ids"]
                                 for event in result["diagnostic_events"]]
        self.assertNotEqual(first_ids[BEFORE], second_ids[BEFORE])
        self.assertNotEqual(first_ids[AFTER], second_ids[AFTER])
        self.assertEqual(len(result["action_cycles"]), 4)

    def test_simultaneous_ct_mr_merge_arrival_with_explicit_joint_group(self):
        for field in ("joint_evidence_group_id", "joint_group_id"):
            with self.subTest(field=field):
                events = [diagnostic("DE-CT", "CT-1"), diagnostic("DE-MR", "MR-1")]
                for event in events:
                    event[field] = "JOINT-IMAGING-1"
                result = self.project(
                    sources=[source("S-CONTEXT", "既往临床资料。"),
                             source("CT-1", "CT报告。"), source("MR-1", "MR报告。")],
                    events=events,
                    reviews=[review("DE-CT", AFTER), review("DE-MR", AFTER)])
                self.assertEqual(len(result["candidate_checkpoints"]), 1)
                after = result["candidate_checkpoints"][0]
                self.assertEqual(after["kind"], AFTER)
                self.assertEqual(after["diagnostic_event_ids"], ["DE-CT", "DE-MR"])
                first_ids, second_ids = [event["candidate_checkpoint_ids"]
                                         for event in result["diagnostic_events"]]
                self.assertEqual(first_ids[AFTER], second_ids[AFTER])

    def test_postoperative_icu_round_is_outside_main_task(self):
        text = "术后转ICU监护并常规补液。"
        item = candidate("EV-ICU", "S-ROUND", text, text,
                         clock="2026-03-03 08:00", action_type="ICU_CARE",
                         target="术后监护")
        result = self.project(
            candidates=[item], sources=[source("S-ROUND", text, profile="round",
                                               clock="2026-03-03 08:00")],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "TASK_OUT_OF_SCOPE")
        self.assertEqual(result["migrations"][0]["reason"],
                         "POSTOPERATIVE_ROUTINE_MANAGEMENT_OUT_OF_MAIN_TASK")

    def test_ct_mr_pathology_and_surgery_stay_in_one_continuous_episode(self):
        events = [
            diagnostic("DE-CT", "CT-1"),
            diagnostic("DE-MR", "MR-1", exam="2026-03-04 08:00",
                       report="2026-03-04 12:00"),
            diagnostic("DE-PATH", "PATH-1", exam="2026-03-05 08:00",
                       report="2026-03-05 12:00"),
        ]
        reviews = [review(event["event_id"], AFTER) for event in events]
        text = "评估后拟行腹腔镜探查。"
        exploration = candidate("EV-EXPLORATION", "S-EXPLORATION", text, text,
                                clock="2026-03-06", action_type="SURGICAL_EXPLORATION",
                                target="腹腔镜探查")
        result = self.project(
            candidates=[exploration],
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("CT-1", "CT报告。"), source("MR-1", "MR报告。"),
                     source("PATH-1", "病理报告。"),
                     source("S-EXPLORATION", text, clock="2026-03-06")],
            events=events, reviews=reviews)
        self.assertEqual(len(result["management_episodes"]), 1)
        episode = result["management_episodes"][0]
        self.assertEqual(len(episode["checkpoint_ids"]), 4)
        self.assertEqual(set(episode["checkpoint_ids"]),
                         {cp["checkpoint_id"] for cp in result["candidate_checkpoints"]})
        self.assertTrue(all(cycle["episode_id"] == episode["episode_id"]
                            for cycle in result["action_cycles"]))
        self.assertIn("SURGICAL_EXPLORATION",
                      [transition["to_phase"] for transition in episode["phase_transitions"]])

    def test_new_condition_does_not_overmerge_same_action(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "新出现黄疸，调整后仍拟行胰十二指肠切除术。"
        result = self.project(
            candidates=[candidate("EV-1", "S-1", first_text, first_text),
                        candidate("EV-2", "S-2", second_text, second_text,
                                  clock="2026-03-02")],
            sources=[source("S-1", first_text), source("S-2", second_text,
                                                       clock="2026-03-02")])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)

    def test_candidate_and_diagnostic_migrations_are_conserved(self):
        kept_text = "拟行胰十二指肠切除术。"
        repeat_text = "再次回述手术计划。"
        icu_text = "术后转ICU监护。"
        candidates = [
            candidate("EV-KEEP", "S-KEEP", kept_text, kept_text),
            candidate("EV-REPEAT", "S-REPEAT", repeat_text, repeat_text,
                      function="repeat"),
            candidate("EV-ICU", "S-ICU", icu_text, icu_text,
                      clock="2026-03-03 09:00", action_type="ICU_CARE",
                      target="术后监护"),
        ]
        events = [diagnostic("DE-CT", "CT-1")]
        result = self.project(
            candidates=candidates,
            sources=[source("S-CONTEXT", "既往资料。"),
                     source("S-KEEP", kept_text), source("S-REPEAT", repeat_text),
                     source("S-ICU", icu_text, profile="clinical_other",
                            clock="2026-03-03 09:00"), source("CT-1", "CT报告。")],
            events=events,
            reviews=[review("DE-CT", BEFORE, target="增强CT",
                            action_type="ORDER_DIAGNOSTIC_TEST"),
                     review("DE-CT", AFTER)],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00")))
        self.assertTrue(result["summary"]["candidate_migration_conserved"])
        self.assertTrue(result["summary"]["diagnostic_migration_conserved"])
        self.assertTrue(result["summary"]["migration_conserved"])
        self.assertEqual(result["summary"]["candidate_migration_count"], 3)
        self.assertEqual(result["summary"]["diagnostic_migration_count"], 2)
        candidate_migrations = [item for item in result["migrations"]
                                if item["migration_type"] == "candidate_span"]
        self.assertEqual({item["event_id"] for item in candidate_migrations},
                         {"EV-KEEP", "EV-REPEAT", "EV-ICU"})

    def test_evidence_arrival_separates_otherwise_identical_management_questions(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "再次讨论拟行胰十二指肠切除术。"
        candidates = [
            candidate("EV-BEFORE", "S-BEFORE", first_text, first_text),
            candidate("EV-AFTER", "S-AFTER", second_text, second_text,
                      clock="2026-03-03", function="reassessment"),
        ]
        result = self.project(
            candidates=candidates,
            sources=[source("S-CONTEXT", "既往资料。"),
                     source("S-BEFORE", first_text),
                     source("S-AFTER", second_text, clock="2026-03-03"),
                     source("CT-1", "CT报告。")],
            events=[diagnostic("DE-CT", "CT-1")],
            reviews=[review("DE-CT", AFTER, target="胰十二指肠切除术")])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        management = next(cp for cp in result["candidate_checkpoints"]
                          if cp["kind"] == REASSESSMENT)
        arrival = next(cp for cp in result["candidate_checkpoints"] if cp["kind"] == AFTER)
        self.assertEqual(management["member_event_ids"], ["EV-BEFORE"])
        self.assertEqual(arrival["supporting_event_ids"], ["EV-AFTER"])

    def test_joint_reports_with_different_next_actions_do_not_overmerge(self):
        events = [diagnostic("DE-CT", "CT-1"), diagnostic("DE-MR", "MR-1")]
        result = self.project(
            sources=[source("CT-1", "CT报告。"), source("MR-1", "MR报告。")],
            events=events,
            reviews=[review("DE-CT", AFTER),
                     review("DE-MR", AFTER, target="新辅助化疗", action_type="START_NAT")])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        self.assertNotEqual(result["diagnostic_events"][0]["candidate_checkpoint_ids"][AFTER],
                            result["diagnostic_events"][1]["candidate_checkpoint_ids"][AFTER])

    def test_existing_material_projection_consumes_both_states_and_episode(self):
        event = diagnostic("DE-CT", "CT-1")
        sources = [source("S-CONTEXT", "既往临床资料。"), source("CT-1", "CT报告。")]
        result = self.project(
            sources=sources, events=[event], reviews=[
                review("DE-CT", BEFORE, target="胰腺增强CT",
                       action_type="ORDER_DIAGNOSTIC_TEST"),
                review("DE-CT", AFTER)])
        materials = project_candidate_materials(
            case_id="SYN-TRAJECTORY", sources=sources, candidates=[], projection=result,
            candidate_episodes=result["management_episodes"])
        self.assertEqual(len(materials["packets"]), 2)
        self.assertEqual({packet["episode_id"] for packet in materials["packets"]},
                         {result["management_episodes"][0]["episode_id"]})
        self.assertTrue(all(packet["management_goal"] == "PDAC_CURATIVE_RESECTION_PATH"
                            for packet in materials["packets"]))
        self.assertTrue(all(packet["action_cycle"] and packet["evidence_state"]
                            for packet in materials["packets"]))
        before = next(packet for packet in materials["packets"] if packet["kind"] == BEFORE)
        target = next(card for card in before["included_sources"]
                      if card["source_id"] == "CT-1")
        self.assertEqual(target["text"], "[目标检查组全部内容已遮蔽]")

    def test_attached_management_source_is_not_unmasked_as_new_evidence(self):
        text = "病理回报后拟开始新辅助化疗。"
        management = candidate(
            "EV-NAT", "S-NAT", text, text, clock="2026-03-02 12:00",
            action_type="START_NAT", target="新辅助化疗")
        event = diagnostic("DE-PATH", "PATH-1")
        sources = [source("PATH-1", "病理报告。"),
                   source("S-NAT", text, clock="2026-03-02 12:00")]
        result = self.project(
            candidates=[management], sources=sources, events=[event],
            reviews=[review("DE-PATH", AFTER, target="新辅助化疗",
                            action_type="START_NAT")])
        materials = project_candidate_materials(
            case_id="SYN-TRAJECTORY", sources=sources, candidates=[management],
            projection=result, candidate_episodes=result["management_episodes"])
        packet = materials["packets"][0]
        report = next(item for item in packet["included_sources"]
                      if item["source_id"] == "PATH-1")
        support = next(item for item in packet["included_sources"]
                       if item["source_id"] == "S-NAT")
        self.assertEqual(report["text"], "病理报告。")
        self.assertNotIn("拟开始新辅助化疗", support["text"])

    def test_repeat_with_real_condition_increment_is_restored_as_retained(self):
        text = "新增黄疸，再次讨论后仍拟行胰十二指肠切除术。"
        item = candidate("EV-REPEAT-CHANGE", "S-REPEAT-CHANGE", text, text,
                         function="repeat", condition_changed=True)
        result = self.project(
            candidates=[item],
            sources=[source("S-REPEAT-CHANGE", text)])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertEqual(result["candidate_checkpoints"][0]["member_event_ids"],
                         ["EV-REPEAT-CHANGE"])
        self.assertEqual(result["migrations"][0]["migration_status"], "RETAINED")
        self.assertEqual(result["migrations"][0]["reason"],
                         "REPEAT_WITH_STATE_INCREMENT_RETAINED")

    def test_repeat_without_increment_remains_support_only(self):
        text = "再次回述拟行胰十二指肠切除术。"
        item = candidate("EV-REPEAT", "S-REPEAT", text, text, function="repeat")
        result = self.project(candidates=[item], sources=[source("S-REPEAT", text)])
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "SUPPORT_ONLY")
        self.assertEqual(result["migrations"][0]["reason"],
                         "RECORD_FUNCTION_NOT_INDEPENDENT_DECISION")

    def test_diagnostic_before_attachment_respects_new_condition_increment(self):
        text = "新增黄疸，拟完善胰腺增强CT。"
        item = candidate("EV-CT-CHANGE", "S-CT-CHANGE", text, text,
                         action_type="ORDER_DIAGNOSTIC_TEST", target="胰腺增强CT",
                         condition_changed=True)
        result = self.project(
            candidates=[item],
            sources=[source("S-CONTEXT", "既往临床资料。"),
                     source("S-CT-CHANGE", text), source("CT-1", "CT报告。")],
            events=[diagnostic("DE-CT", "CT-1")],
            reviews=[review("DE-CT", BEFORE, target="胰腺增强CT",
                            action_type="ORDER_DIAGNOSTIC_TEST")])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        management = next(cp for cp in result["candidate_checkpoints"]
                          if cp["kind"] == REASSESSMENT)
        self.assertEqual(management["member_event_ids"], ["EV-CT-CHANGE"])
        migration = next(item for item in result["migrations"]
                         if item["event_id"] == "EV-CT-CHANGE")
        self.assertEqual(migration["migration_status"], "RETAINED")
        self.assertEqual(migration["to_checkpoint_id"], management["checkpoint_id"])

    def test_after_attachment_rejects_same_action_with_different_input_evidence(self):
        text = "病理结果回报后拟开始新辅助化疗。"
        item = candidate("EV-NAT", "S-NAT", text, text, clock="2026-03-02 13:00",
                         action_type="START_NAT", target="新辅助化疗",
                         input_evidence_ids=["DE-OTHER"])
        result = self.project(
            candidates=[item],
            sources=[source("PATH-1", "病理报告。"),
                     source("S-NAT", text, clock="2026-03-02 13:00")],
            events=[diagnostic("DE-PATH", "PATH-1")],
            reviews=[review("DE-PATH", AFTER, target="新辅助化疗",
                            action_type="START_NAT")])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        management = next(cp for cp in result["candidate_checkpoints"]
                          if cp["kind"] == REASSESSMENT)
        self.assertEqual(management["member_event_ids"], ["EV-NAT"])
        migration = next(row for row in result["migrations"]
                         if row["event_id"] == "EV-NAT")
        self.assertEqual(migration["migration_status"], "RETAINED")
        self.assertEqual(migration["to_checkpoint_id"], management["checkpoint_id"])

    def test_same_source_same_cutoff_is_one_composite_action_question(self):
        text = "拟行胰十二指肠切除术。拟开始新辅助化疗。"
        items = [
            candidate("EV-SURGERY", "S-SAME", text, "拟行胰十二指肠切除术。"),
            candidate("EV-NAT", "S-SAME", text, "拟开始新辅助化疗。",
                      action_type="START_NAT", target="新辅助化疗"),
        ]
        result = self.project(candidates=items, sources=[source("S-SAME", text)])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(set(checkpoint["member_event_ids"]), {"EV-SURGERY", "EV-NAT"})
        self.assertEqual(len(checkpoint["candidate_actions"]), 2)

    def test_interleaved_sources_do_not_split_same_source_same_cutoff(self):
        text = "拟行胰十二指肠切除术。拟开始新辅助化疗。"
        other_text = "拟行腹腔镜探查。"
        items = [
            candidate("EV-A", "S-SAME", text, "拟行胰十二指肠切除术。"),
            candidate("EV-B", "S-OTHER", other_text, other_text,
                      action_type="SURGICAL_EXPLORATION", target="腹腔镜探查"),
            candidate("EV-C", "S-SAME", text, "拟开始新辅助化疗。",
                      action_type="START_NAT", target="新辅助化疗"),
        ]
        result = self.project(candidates=items, sources=[
            source("S-SAME", text), source("S-OTHER", other_text)])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        composite = next(cp for cp in result["candidate_checkpoints"]
                         if "S-SAME" in cp["source_ids"])
        self.assertEqual(set(composite["member_event_ids"]), {"EV-A", "EV-C"})
        self.assertEqual(len(composite["candidate_actions"]), 2)

    def test_partial_after_match_does_not_absorb_other_composite_actions(self):
        text = "拟行胰十二指肠切除术。拟开始新辅助化疗。"
        items = [
            candidate("EV-SURGERY", "S-SAME", text, "拟行胰十二指肠切除术。"),
            candidate("EV-NAT", "S-SAME", text, "拟开始新辅助化疗。",
                      action_type="START_NAT", target="新辅助化疗"),
        ]
        result = self.project(
            candidates=items, sources=[source("PATH-1", "病理报告。"),
                                      source("S-SAME", text)],
            events=[diagnostic("DE-PATH", "PATH-1")],
            reviews=[review("DE-PATH", AFTER, target="新辅助化疗",
                            action_type="START_NAT")])
        self.assertEqual(len(result["candidate_checkpoints"]), 2)
        management = next(cp for cp in result["candidate_checkpoints"]
                          if cp["kind"] == REASSESSMENT)
        self.assertEqual({action["action_type"] for action in management["candidate_actions"]},
                         {"ADVANCE_RESECTION", "START_NAT"})
        self.assertEqual(set(management["member_event_ids"]), {"EV-SURGERY", "EV-NAT"})

    def test_same_source_pure_repeat_ignores_record_question_id(self):
        text = "拟行胰十二指肠切除术。"
        items = [
            candidate("EV-1", "S-SAME", text, text,
                      unresolved_management_question_id="UQ-RECORD-1"),
            candidate("EV-2", "S-SAME", text, text, function="reassessment",
                      unresolved_management_question_id="UQ-RECORD-2"),
        ]
        result = self.project(candidates=items, sources=[source("S-SAME", text)])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertEqual(result["candidate_checkpoints"][0]["member_event_ids"],
                         ["EV-1", "EV-2"])

    def test_same_source_same_cutoff_increments_stay_in_one_state(self):
        text = "拟行胰十二指肠切除术。"
        for field in ("condition_changed", "evidence_increment"):
            with self.subTest(field=field):
                items = [candidate("EV-1", "S-SAME", text, text),
                         candidate("EV-2", "S-SAME", text, text,
                                   function="reassessment", **{field: True})]
                result = self.project(candidates=items, sources=[source("S-SAME", text)])
                self.assertEqual(len(result["candidate_checkpoints"]), 1)
                self.assertTrue(result["candidate_checkpoints"][0]["candidate_actions"])

    def test_action_timing_and_conditions_participate_in_merge_signature(self):
        text = "拟行胰十二指肠切除术。"
        for field, first_value, second_value in (
                ("timing", "立即", "两周后"),
                ("conditions", ["无禁忌"], ["肝功能改善后"])):
            with self.subTest(field=field):
                first = candidate("EV-1", "S-SAME", text, text)
                second = candidate("EV-2", "S-OTHER", text, text,
                                   function="reassessment")
                first["candidate_action"][field] = first_value
                second["candidate_action"][field] = second_value
                result = self.project(candidates=[first, second],
                                      sources=[source("S-SAME", text),
                                               source("S-OTHER", text)])
                self.assertEqual(len(result["candidate_checkpoints"]), 2)

    def test_operation_day_date_only_diagnostic_order_requires_review(self):
        event = diagnostic("DE-CT", "CT-1", exam="2026-03-02", report="2026-03-02")
        event["exam_time"]["precision"] = "date"
        event["report_time"]["precision"] = "date"
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"), source("CT-1", "CT报告。")],
            events=[event],
            reviews=[review("DE-CT", BEFORE, target="增强CT",
                            action_type="ORDER_DIAGNOSTIC_TEST"),
                     review("DE-CT", AFTER)],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(result["candidate_checkpoints"], [])
        migrations = {row["node_kind"]: row for row in result["migrations"]}
        for kind in (BEFORE, AFTER):
            self.assertEqual(migrations[kind]["migration_status"], "REVIEW_REQUIRED")
            self.assertEqual(migrations[kind]["reason"],
                             "OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN")

        earlier = diagnostic("DE-CT", "CT-1", exam="2026-03-01", report="2026-03-01")
        earlier["exam_time"]["precision"] = "date"
        earlier["report_time"]["precision"] = "date"
        retained = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"), source("CT-1", "CT报告。")],
            events=[earlier],
            reviews=[review("DE-CT", BEFORE, target="增强CT",
                            action_type="ORDER_DIAGNOSTIC_TEST"),
                     review("DE-CT", AFTER)],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00", precision="minute")))
        self.assertCountEqual([cp["kind"] for cp in retained["candidate_checkpoints"]],
                              [BEFORE, AFTER])

    def test_coarse_same_day_diagnostic_pair_orders_acquisition_before_arrival(self):
        event = diagnostic("DE-CT", "CT-1", exam="2026-03-02", report="2026-03-02")
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"), source("CT-1", "CT报告。")],
            events=[event], reviews=[
                review("DE-CT", BEFORE, target="增强CT", action_type="ORDER_DIAGNOSTIC_TEST"),
                review("DE-CT", AFTER)],
            bounds=dict(operation_start=dict(value="2026-03-03 10:00", precision="minute")))
        self.assertEqual([item["kind"] for item in result["candidate_checkpoints"]],
                         [BEFORE, AFTER])

    def test_empty_candidate_clock_falls_back_for_operation_day_review(self):
        text = "拟行胰十二指肠切除术。"
        item = candidate("EV-DATE", "S-DATE", text, text, clock="2026-03-02")
        item["clock"] = dict(value=None, precision="unknown")
        result = self.project(
            candidates=[item], sources=[source("S-DATE", text, clock="2026-03-02")],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["migrations"][0]["reason"],
                         "OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN")

    def test_date_only_operation_start_makes_same_day_diagnostic_order_unknown(self):
        event = diagnostic("DE-CT", "CT-1", exam="2026-03-02 08:00",
                           report="2026-03-02 12:00")
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"), source("CT-1", "CT报告。")],
            events=[event], reviews=[review("DE-CT", BEFORE), review("DE-CT", AFTER)],
            bounds=dict(operation_start=dict(value="2026-03-02", precision="date")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual([item["reason"] for item in result["migrations"]],
                         ["OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN"] * 2)

    def test_postoperative_evidence_arrival_is_retained_outside_primary_evaluation(self):
        event = diagnostic("DE-MR", "MR-1", exam="2026-03-02 08:00",
                           report="2026-03-02 12:00")
        result = self.project(
            sources=[source("S-CONTEXT", "既往临床资料。"), source("MR-1", "MR报告。")],
            events=[event], reviews=[review("DE-MR", AFTER)],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["kind"], AFTER)
        self.assertFalse(checkpoint["primary_evaluation_eligible"])
        self.assertEqual(checkpoint["evaluation_scope"],
                         "NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY")
        self.assertEqual(result["action_cycles"][0]["primary_evaluation_eligible"], False)
        migration = next(item for item in result["migrations"]
                         if item.get("node_kind") == AFTER)
        self.assertEqual(migration["reason"],
                         "ACTIONABLE_DIAGNOSTIC_PROJECTION_NON_PRIMARY_TASK")
        self.assertEqual(result["summary"]["primary_evaluation_checkpoint_count"], 0)
        self.assertEqual(result["summary"]["non_primary_evaluation_checkpoint_count"], 1)

    def test_postoperative_routine_exclusion_ignores_historical_resection_quote(self):
        text = "既往行胰腺癌切除，本次术后转ICU监护并常规补液。"
        item = candidate("EV-ICU-HISTORY", "S-ROUND", text, text,
                         clock="2026-03-03 08:00", action_type="ICU_CARE",
                         target="术后监护")
        result = self.project(
            candidates=[item],
            sources=[source("S-ROUND", text, profile="round",
                            clock="2026-03-03 08:00")],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "TASK_OUT_OF_SCOPE")
        self.assertEqual(result["migrations"][0]["reason"],
                         "POSTOPERATIVE_ROUTINE_MANAGEMENT_OUT_OF_MAIN_TASK")

    def test_duplicate_actions_keep_event_provenance_without_duplicate_answers(self):
        text = "拟行胰十二指肠切除术。"
        items = [candidate("EV-1", "S-SAME", text, text),
                 candidate("EV-2", "S-SAME", text, text, function="reassessment")]
        result = self.project(candidates=items, sources=[source("S-SAME", text)])
        actions = result["candidate_checkpoints"][0]["candidate_actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["merged_event_ids"], ["EV-1", "EV-2"])

    def test_same_target_and_cutoff_across_record_types_is_one_question(self):
        text = "拟行胰十二指肠切除术。"
        first = candidate("EV-1", "S-1", text, text)
        second = candidate("EV-2", "S-2", text, text, function="reassessment",
                           action_type="PREOPERATIVE_ASSESSMENT")
        result = self.project(candidates=[first, second], sources=[
            source("S-1", text, profile="preoperative_discussion"),
            source("S-2", text, profile="preoperative_summary")])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertEqual(len(result["candidate_checkpoints"][0]["candidate_actions"]), 2)

    def test_rejected_report_does_not_split_unchanged_management_question(self):
        first_text = "拟行EUS+FNA。"
        second_text = "再次记录拟行EUS+FNA。"
        items = [candidate("EV-1", "S-1", first_text, first_text,
                           action_type="ORDER_EUS", target="EUS+FNA"),
                 candidate("EV-2", "S-2", second_text, second_text,
                           clock="2026-03-03", function="reassessment",
                           action_type="EUS_EVALUATION", target="EUS+FNA")]
        result = self.project(
            candidates=items, sources=[source("S-1", first_text),
                                      source("S-2", second_text, clock="2026-03-03"),
                                      source("CT-1", "CT报告。")],
            events=[diagnostic("DE-CT", "CT-1")],
            reviews=[review("DE-CT", AFTER, disposition="no")])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        self.assertEqual(set(result["candidate_checkpoints"][0]["member_event_ids"]),
                         {"EV-1", "EV-2"})

    def test_admission_and_generic_workup_are_support_not_checkpoint(self):
        for text, target in (("收入院进一步诊治。", "收入院进一步诊治"),
                             ("入院常规及特殊检查。", "入院常规及特殊检查")):
            with self.subTest(target=target):
                item = candidate("EV-ADMIT", "S-ADMIT", text, text,
                                 action_type="ADMISSION", target=target)
                result = self.project(candidates=[item], sources=[source("S-ADMIT", text)])
                self.assertEqual(result["candidate_checkpoints"], [])
                self.assertEqual(result["migrations"][0]["migration_status"], "SUPPORT_ONLY")
                self.assertIn(result["migrations"][0]["reason"], {
                    "ADMISSION_INTENT_NOT_INDEPENDENT_DECISION",
                    "ADMISSION_OR_GENERIC_WORKUP_NOT_INDEPENDENT_DECISION"})

    def test_postoperative_resection_or_surveillance_candidate_is_out_of_task(self):
        text = "术后随访，既往已行胰十二指肠切除术。"
        item = candidate("EV-POST", "S-POST", text, text,
                         clock="2026-03-03 08:00", action_type="SURGERY",
                         target="胰十二指肠切除术")
        result = self.project(
            candidates=[item], sources=[source("S-POST", text, profile="clinical_other",
                                              clock="2026-03-03 08:00")],
            bounds=dict(operation_start=dict(value="2026-03-02 10:00", precision="minute")))
        self.assertEqual(result["candidate_checkpoints"], [])
        self.assertEqual(result["migrations"][0]["migration_status"], "TASK_OUT_OF_SCOPE")
        self.assertEqual(result["migrations"][0]["reason"],
                         "AFTER_OPERATION_START_TASK_OUT_OF_SCOPE")

    def test_completed_nat_moves_to_post_treatment_not_back_to_nat(self):
        records = [
            ("EV-NAT", "S-NAT", "拟开始新辅助化疗。", "START_NAT", "新辅助化疗", "2026-03-01"),
            ("EV-SUGGEST", "S-SUGGEST", "建议行化学治疗。", "START_NAT", "化学治疗", "2026-03-02"),
            ("EV-COMPLETE", "S-COMPLETE", "昨日行AG方案第一次化疗，今日出院。", "DISCHARGE", "出院", "2026-03-03"),
        ]
        items = [candidate(event_id, source_id, text, text, clock=clock,
                           action_type=action_type, target=target)
                 for event_id, source_id, text, action_type, target, clock in records]
        result = self.project(candidates=items, sources=[
            source(source_id, text, clock=clock)
            for _, source_id, text, _, _, clock in records])
        self.assertEqual([cp["phase"] for cp in result["candidate_checkpoints"]],
                         ["NAT_OR_CONVERSION", "NAT_OR_CONVERSION",
                          "POST_TREATMENT_REASSESSMENT"])

    def test_global_support_only_moves_composite_members_to_diagnostic_cp(self):
        event = diagnostic("DE-CT", "CT-1")
        text = "转入后拟行EUS+FNA，评估后拟行胰十二指肠切除术。"
        items = [
            candidate("EV-EUS", "S-TRANSFER", text, "拟行EUS+FNA",
                      clock="2026-03-03", action_type="ORDER_EUS", target="EUS+FNA"),
            candidate("EV-SURGERY", "S-TRANSFER", text, "拟行胰十二指肠切除术。",
                      clock="2026-03-03"),
        ]
        sources = [source("CT-1", "CT报告。"),
                   source("S-TRANSFER", text, clock="2026-03-03")]
        sources[1]["clocks"] = {"source_create_time": dict(
            value="2026-03-04 09:00", precision="minute")}
        reviews = [review("DE-CT", AFTER)]
        management_id = self.provisional_management_id(items, sources)
        diagnostic_id = self.project(
            sources=sources, events=[event], reviews=reviews
        )["candidate_checkpoints"][0]["checkpoint_id"]
        result = self.project(
            candidates=items, sources=sources, events=[event], reviews=reviews,
            adjudications={"items": [dict(
                checkpoint_id=management_id, disposition="SUPPORT_ONLY",
                evolution="SAME_ROUND_SUPPORT", comparator_checkpoint_id=diagnostic_id,
                evidence_supports=["DE-CT"], rationale="合成全局裁决")]})
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["kind"], AFTER)
        self.assertEqual(set(checkpoint["supporting_event_ids"]), {"EV-EUS", "EV-SURGERY"})
        self.assertEqual(checkpoint["support_source_ids"], ["CT-1", "S-TRANSFER"])
        transfer_spans = [span for span in checkpoint["masked_spans"]
                          if span["source_id"] == "S-TRANSFER"]
        self.assertEqual(len(transfer_spans), 2)
        migrations = {row["event_id"]: row for row in result["migrations"]
                      if row["migration_type"] == "candidate_span"}
        self.assertEqual(set(migrations), {"EV-EUS", "EV-SURGERY"})
        for migration in migrations.values():
            self.assertEqual(migration["migration_status"], "SUPPORT_ONLY")
            self.assertEqual(migration["reason"], "GLOBAL_ADJUDICATION_SAME_ROUND_SUPPORT")
            self.assertEqual(migration["to_checkpoint_id"], diagnostic_id)
        self.assertEqual({row["event_id"] for row in result["support_records"]},
                         {"EV-EUS", "EV-SURGERY"})
        self.assertTrue(all(row["to_checkpoint_id"] == diagnostic_id
                            for row in result["support_records"]))
        self.assertEqual(checkpoint["supporting_adjudications"], [dict(
            checkpoint_id=management_id, disposition="SUPPORT_ONLY",
            evolution="SAME_ROUND_SUPPORT", comparator_checkpoint_id=diagnostic_id,
            evidence_supports=["DE-CT"], rationale="合成全局裁决")])
        packet = project_candidate_materials(
            case_id="SYN-TRAJECTORY", sources=sources, candidates=items,
            projection=result)["packets"][0]
        self.assertEqual(packet["supporting_adjudications"],
                         checkpoint["supporting_adjudications"])
        self.assertNotIn("S-TRANSFER", {item["source_id"]
                                       for item in packet["included_sources"]})
        self.assertEqual(packet["masked_projection"]["excluded_support_source_ids"],
                         ["S-TRANSFER"])
        material_spans = [span for span in packet["masked_projection"]["masked_spans"]
                          if span["source_id"] == "S-TRANSFER"]
        for item in items:
            span = item["span"]
            self.assertTrue(any(mask["start"] <= span["start"]
                                and span["end"] <= mask["end"]
                                for mask in material_spans))
        original = next(item for item in packet["researcher_target_originals"]
                        if item["source_id"] == "S-TRANSFER")
        self.assertEqual(original["text"], text)

    def test_global_retain_new_evidence_round_skips_legacy_signature_merge(self):
        event = diagnostic("DE-CT", "CT-1")
        text = "再次讨论拟行胰十二指肠切除术。"
        item = candidate("EV-SURGERY", "S-PLAN", text, text, clock="2026-03-03")
        sources = [source("CT-1", "CT报告。"),
                   source("S-PLAN", text, clock="2026-03-03")]
        reviews = [review("DE-CT", AFTER, target="胰十二指肠切除术")]
        management_id = self.provisional_management_id([item], sources)
        legacy = self.project(candidates=[item], sources=sources,
                              events=[event], reviews=reviews)
        self.assertEqual(len(legacy["candidate_checkpoints"]), 1)
        result = self.project(
            candidates=[item], sources=sources, events=[event], reviews=reviews,
            adjudications=[dict(
                checkpoint_id=management_id, disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id=None,
                evidence_supports=["DE-CT"], rationale="新证据轮次保留独立CP")])
        self.assertCountEqual([cp["kind"] for cp in result["candidate_checkpoints"]],
                              [AFTER, REASSESSMENT])
        management = next(cp for cp in result["candidate_checkpoints"]
                          if cp["kind"] == REASSESSMENT)
        self.assertEqual(management["checkpoint_id"], management_id)
        self.assertEqual(management["member_event_ids"], ["EV-SURGERY"])
        self.assertEqual(management["global_adjudication"]["rationale"],
                         "新证据轮次保留独立CP")
        self.assertEqual(management["global_adjudication"]["evidence_supports"], ["DE-CT"])
        migration = next(row for row in result["migrations"]
                         if row["event_id"] == "EV-SURGERY")
        self.assertEqual(migration["migration_status"], "RETAINED")
        self.assertEqual(migration["to_checkpoint_id"], management_id)

    def test_global_support_only_merges_different_action_without_new_evidence(self):
        event = diagnostic("DE-CT", "CT-1")
        text = "拟行胰十二指肠切除术。"
        item = candidate("EV-SURGERY", "S-PLAN", text, text, clock="2026-03-03")
        sources = [source("CT-1", "CT报告。"),
                   source("S-PLAN", text, clock="2026-03-03")]
        reviews = [review("DE-CT", AFTER, target="新辅助化疗", action_type="START_NAT")]
        management_id = self.provisional_management_id([item], sources)
        diagnostic_id = self.project(
            sources=sources, events=[event], reviews=reviews
        )["candidate_checkpoints"][0]["checkpoint_id"]
        result = self.project(
            candidates=[item], sources=sources, events=[event], reviews=reviews,
            adjudications=[dict(
                checkpoint_id=management_id, disposition="SUPPORT_ONLY",
                evolution="DIFFERENT_TASK", comparator_checkpoint_id=diagnostic_id,
                evidence_supports=[], rationale="无新增证据，不保留不同动作CP")])
        self.assertEqual([cp["checkpoint_id"] for cp in result["candidate_checkpoints"]],
                         [diagnostic_id])
        self.assertEqual(result["candidate_checkpoints"][0]["supporting_event_ids"],
                         ["EV-SURGERY"])
        migration = next(row for row in result["migrations"]
                         if row["event_id"] == "EV-SURGERY")
        self.assertEqual(migration["migration_status"], "SUPPORT_ONLY")
        self.assertEqual(migration["reason"], "GLOBAL_ADJUDICATION_DIFFERENT_TASK")

    def test_global_review_required_keeps_event_as_review_attachment(self):
        event = diagnostic("DE-CT", "CT-1")
        text = "拟行胰十二指肠切除术。"
        item = candidate("EV-SURGERY", "S-PLAN", text, text, clock="2026-03-03")
        sources = [source("CT-1", "CT报告。"),
                   source("S-PLAN", text, clock="2026-03-03")]
        reviews = [review("DE-CT", AFTER)]
        management_id = self.provisional_management_id([item], sources)
        diagnostic_id = self.project(
            sources=sources, events=[event], reviews=reviews
        )["candidate_checkpoints"][0]["checkpoint_id"]
        result = self.project(
            candidates=[item], sources=sources, events=[event], reviews=reviews,
            adjudications=[dict(
                checkpoint_id=management_id, disposition="REVIEW_REQUIRED",
                evolution="UNCERTAIN", comparator_checkpoint_id=diagnostic_id,
                evidence_supports=[], rationale="全局裁决不确定")])
        self.assertEqual([cp["checkpoint_id"] for cp in result["candidate_checkpoints"]],
                         [diagnostic_id])
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["supporting_event_ids"], ["EV-SURGERY"])
        self.assertIn("S-PLAN", checkpoint["support_source_ids"])
        self.assertTrue(any(span["source_id"] == "S-PLAN"
                            for span in checkpoint["masked_spans"]))
        migration = next(row for row in result["migrations"]
                         if row["event_id"] == "EV-SURGERY")
        self.assertEqual(migration["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(migration["reason"], "GLOBAL_ADJUDICATION_UNCERTAIN")
        self.assertEqual(migration["to_checkpoint_id"], diagnostic_id)
        self.assertEqual(migration["span"]["quote"], text)
        self.assertEqual(result["support_records"], [])

    def test_global_support_only_can_attach_to_earlier_management_cp(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "拟开始新辅助化疗。"
        items = [
            candidate("EV-FIRST", "S-FIRST", first_text, first_text),
            candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                      clock="2026-03-03", action_type="START_NAT", target="新辅助化疗"),
        ]
        sources = [source("S-FIRST", first_text),
                   source("S-SECOND", second_text, clock="2026-03-03")]
        provisional = self.project(candidates=items, sources=sources)
        ids = {cp["member_event_ids"][0]: cp["checkpoint_id"]
               for cp in provisional["candidate_checkpoints"]}
        result = self.project(candidates=items, sources=sources, adjudications=[
            dict(checkpoint_id=ids["EV-FIRST"], disposition="RETAIN_CP",
                 evolution="FIRST_VISIBLE", comparator_checkpoint_id=None,
                 evidence_supports=[], rationale="保留首个CP"),
            dict(checkpoint_id=ids["EV-SECOND"], disposition="SUPPORT_ONLY",
                 evolution="REPEATED_PLAN", comparator_checkpoint_id=ids["EV-FIRST"],
                 evidence_supports=[], rationale="归入既有management CP")])
        self.assertEqual([cp["checkpoint_id"] for cp in result["candidate_checkpoints"]],
                         [ids["EV-FIRST"]])
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["member_event_ids"], ["EV-FIRST"])
        self.assertEqual(checkpoint["supporting_event_ids"], ["EV-SECOND"])
        self.assertEqual(checkpoint["support_source_ids"], ["S-FIRST", "S-SECOND"])
        migration = next(row for row in result["migrations"]
                         if row["event_id"] == "EV-SECOND")
        self.assertEqual(migration["migration_status"], "SUPPORT_ONLY")
        self.assertEqual(migration["to_checkpoint_id"], ids["EV-FIRST"])
        self.assertEqual(result["support_records"][0]["to_checkpoint_id"], ids["EV-FIRST"])

    def test_minute_clock_recovers_matching_source_raw_for_ordering(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.ordering import compare
        first_text = "拟行胰十二指肠切除术。"
        second_text = "拟开始新辅助化疗。"
        first = candidate("EV-MINUTE-1", "S-MINUTE-1", first_text, first_text,
                          clock="2026-03-01 10:00:00")
        second = candidate("EV-MINUTE-2", "S-MINUTE-2", second_text, second_text,
                           clock="2026-03-01 11:00:00", action_type="START_NAT",
                           target="新辅助化疗")
        sources = [source("S-MINUTE-1", first_text, clock="2026-03-01 10:00:00"),
                   source("S-MINUTE-2", second_text, clock="2026-03-01 11:00:00")]
        for item, raw in zip(sources, ("2026-03-01 10:00", "2026-03-01 11:00")):
            item["clock"].update(precision="minute", raw=raw)
        for item in (first, second):
            item["clock"]["precision"] = "minute"
        result = self.project(candidates=[first, second], sources=sources)
        checkpoints = result["candidate_checkpoints"]
        self.assertEqual(compare(checkpoints[0], checkpoints[1])["relation"], "before")
        self.assertEqual(checkpoints[0]["clock"]["raw"], "2026-03-01 10:00")

    def test_global_review_without_comparator_retains_visible_review_checkpoint(self):
        text = "时间不明，拟行胰十二指肠切除术。"
        item = candidate("EV-UNCERTAIN", "S-UNCERTAIN", text, text)
        sources = [source("S-UNCERTAIN", text)]
        checkpoint_id = self.provisional_management_id([item], sources)
        result = self.project(candidates=[item], sources=sources, adjudications=[dict(
            checkpoint_id=checkpoint_id, disposition="REVIEW_REQUIRED",
            evolution="UNCERTAIN", comparator_checkpoint_id=None,
            evidence_supports=[], rationale="没有可定序的比较状态")])
        self.assertEqual(len(result["candidate_checkpoints"]), 1)
        checkpoint = result["candidate_checkpoints"][0]
        self.assertEqual(checkpoint["checkpoint_id"], checkpoint_id)
        self.assertEqual(checkpoint["global_adjudication"]["rationale"],
                         "没有可定序的比较状态")
        migration = result["migrations"][0]
        self.assertEqual(migration["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(migration["to_checkpoint_id"], checkpoint_id)
        packet = project_candidate_materials(
            case_id="SYN-TRAJECTORY", sources=sources, candidates=[item],
            projection=result)["packets"][0]
        self.assertEqual(packet["global_adjudication"]["disposition"], "REVIEW_REQUIRED")

    def test_global_review_chain_uses_final_destination_in_migration_and_packet(self):
        records = [
            ("EV-A", "S-A", "拟行胰十二指肠切除术。", "ADVANCE_RESECTION", "手术", "2026-03-01"),
            ("EV-B", "S-B", "拟开始新辅助化疗。", "START_NAT", "化疗", "2026-03-02"),
            ("EV-C", "S-C", "转入后再次评估。", "TRANSFER", "转科", "2026-03-03"),
        ]
        items = [candidate(event_id, source_id, text, text, clock=clock,
                           action_type=action_type, target=target)
                 for event_id, source_id, text, action_type, target, clock in records]
        sources = [source(source_id, text, clock=clock)
                   for _, source_id, text, _, _, clock in records]
        provisional = self.project(candidates=items, sources=sources)
        ids = {cp["member_event_ids"][0]: cp["checkpoint_id"]
               for cp in provisional["candidate_checkpoints"]}
        result = self.project(candidates=items, sources=sources, adjudications=[
            dict(checkpoint_id=ids["EV-A"], disposition="RETAIN_CP",
                 evolution="FIRST_VISIBLE", comparator_checkpoint_id=None,
                 evidence_supports=[], rationale="首见"),
            dict(checkpoint_id=ids["EV-B"], disposition="SUPPORT_ONLY",
                 evolution="SAME_ROUND_SUPPORT", comparator_checkpoint_id=ids["EV-A"],
                 evidence_supports=[], rationale="同轮支持"),
            dict(checkpoint_id=ids["EV-C"], disposition="REVIEW_REQUIRED",
                 evolution="UNCERTAIN", comparator_checkpoint_id=ids["EV-B"],
                 evidence_supports=[], rationale="关系待核")])
        self.assertEqual([cp["checkpoint_id"] for cp in result["candidate_checkpoints"]],
                         [ids["EV-A"]])
        migrations = {row["event_id"]: row for row in result["migrations"]}
        self.assertEqual(migrations["EV-B"]["to_checkpoint_id"], ids["EV-A"])
        self.assertEqual(migrations["EV-C"]["to_checkpoint_id"], ids["EV-A"])
        packet = project_candidate_materials(
            case_id="SYN-TRAJECTORY", sources=sources, candidates=items,
            projection=result)["packets"][0]
        self.assertEqual({row["event_id"] for row in packet["migration_records"]},
                         {"EV-A", "EV-B", "EV-C"})
        self.assertEqual({row["checkpoint_id"] for row in packet["supporting_adjudications"]},
                         {ids["EV-B"], ids["EV-C"]})

    def test_global_adjudication_rejects_unknown_checkpoint_and_comparator(self):
        text = "拟行胰十二指肠切除术。"
        item = candidate("EV-SURGERY", "S-PLAN", text, text)
        sources = [source("S-PLAN", text)]
        management_id = self.provisional_management_id([item], sources)
        with self.assertRaisesRegex(ValueError, "unknown adjudicated management checkpoint"):
            self.project(candidates=[item], sources=sources, adjudications=[dict(
                checkpoint_id="CP-M-UNKNOWN", disposition="RETAIN_CP",
                evolution="FIRST_VISIBLE", comparator_checkpoint_id=None,
                evidence_supports=[], rationale="未知CP")])
        with self.assertRaisesRegex(ValueError, "unknown comparator checkpoint"):
            self.project(candidates=[item], sources=sources, adjudications=[dict(
                checkpoint_id=management_id, disposition="SUPPORT_ONLY",
                evolution="REPEATED_PLAN", comparator_checkpoint_id="CP-M-UNKNOWN",
                evidence_supports=[], rationale="未知comparator")])

    def test_global_adjudication_rejects_same_clock_or_later_comparator(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "拟开始新辅助化疗。"
        items = [
            candidate("EV-FIRST", "S-FIRST", first_text, first_text),
            candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                      clock="2026-03-03", action_type="START_NAT", target="新辅助化疗"),
        ]
        sources = [source("S-FIRST", first_text),
                   source("S-SECOND", second_text, clock="2026-03-03")]
        provisional = self.project(candidates=items, sources=sources)
        ids = {cp["member_event_ids"][0]: cp["checkpoint_id"]
               for cp in provisional["candidate_checkpoints"]}
        with self.assertRaisesRegex(ValueError, "strictly earlier"):
            self.project(candidates=items, sources=sources, adjudications=[dict(
                checkpoint_id=ids["EV-FIRST"], disposition="SUPPORT_ONLY",
                evolution="REPEATED_PLAN", comparator_checkpoint_id=ids["EV-SECOND"],
                evidence_supports=[], rationale="晚于当前CP")])

        same_clock = candidate("EV-SAME", "S-SAME", second_text, second_text,
                               action_type="START_NAT", target="新辅助化疗")
        same_sources = [source("S-FIRST", first_text), source("S-SAME", second_text)]
        same_items = [items[0], same_clock]
        provisional = self.project(candidates=same_items, sources=same_sources)
        ids = {cp["member_event_ids"][0]: cp["checkpoint_id"]
               for cp in provisional["candidate_checkpoints"]}
        with self.assertRaisesRegex(ValueError, "strictly earlier"):
            self.project(candidates=same_items, sources=same_sources, adjudications=[dict(
                checkpoint_id=ids["EV-SAME"], disposition="SUPPORT_ONLY",
                evolution="REPEATED_PLAN", comparator_checkpoint_id=ids["EV-FIRST"],
                evidence_supports=[], rationale="同钟comparator")])

    def test_global_adjudication_rejects_comparator_cycle(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "拟开始新辅助化疗。"
        items = [
            candidate("EV-FIRST", "S-FIRST", first_text, first_text),
            candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                      clock="2026-03-03", action_type="START_NAT", target="新辅助化疗"),
        ]
        sources = [source("S-FIRST", first_text),
                   source("S-SECOND", second_text, clock="2026-03-03")]
        provisional = self.project(candidates=items, sources=sources)
        ids = {cp["member_event_ids"][0]: cp["checkpoint_id"]
               for cp in provisional["candidate_checkpoints"]}
        adjudications = []
        for checkpoint_id, comparator_id in ((ids["EV-FIRST"], ids["EV-SECOND"]),
                                             (ids["EV-SECOND"], ids["EV-FIRST"])):
            adjudications.append(dict(
                checkpoint_id=checkpoint_id, disposition="SUPPORT_ONLY",
                evolution="REPEATED_PLAN", comparator_checkpoint_id=comparator_id,
                evidence_supports=[], rationale="循环comparator"))
        with self.assertRaisesRegex(ValueError, "cyclic"):
            self.project(candidates=items, sources=sources, adjudications=adjudications)

    def test_inputs_are_not_modified_and_ids_are_stable_under_reordering(self):
        text = "拟行胰十二指肠切除术。"
        candidates = [candidate("EV-1", "S-1", text, text)]
        sources = [source("S-CONTEXT", "既往资料。"), source("S-1", text),
                   source("CT-1", "CT报告。")]
        events = [diagnostic("DE-CT", "CT-1")]
        reviews = [review("DE-CT", BEFORE, target="增强CT",
                          action_type="ORDER_DIAGNOSTIC_TEST"),
                   review("DE-CT", AFTER)]
        before = deepcopy((candidates, sources, events, reviews))
        first = self.project(candidates=candidates, sources=sources,
                             events=events, reviews=reviews)
        second = self.project(candidates=list(reversed(candidates)),
                              sources=list(reversed(sources)),
                              events=list(reversed(events)), reviews=list(reversed(reviews)))
        self.assertEqual((candidates, sources, events, reviews), before)
        self.assertEqual({cp["checkpoint_id"] for cp in first["candidate_checkpoints"]},
                         {cp["checkpoint_id"] for cp in second["candidate_checkpoints"]})


if __name__ == "__main__":
    unittest.main()

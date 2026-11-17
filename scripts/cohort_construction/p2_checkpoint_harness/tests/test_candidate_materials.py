from copy import deepcopy
import json
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_checkpoints import project_candidate_checkpoints
from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials import project_candidate_materials
from scripts.cohort_construction.p2_checkpoint_harness.stages.diagnostic_events import build_diagnostic_events


def clock(value):
    return dict(value=value, precision="second" if value else "unknown")


class CandidateMaterialTests(unittest.TestCase):
    def setUp(self):
        self.management_text = "评估病情。\n拟行胰十二指肠切除术。\n拟施麻醉方式：全麻\n手术指征：需手术切除。\n之后记录。"
        self.sources = [
            dict(source_id="S-M", title="虚构病程", kind="document",
                 clock=clock("2022-12-14 10:00:00"), clock_basis="body_record_time",
                 clocks={}, text=self.management_text),
            dict(source_id="S-D", title="胰腺MR平扫+增强", kind="imaging",
                 clock=clock("2022-12-16 15:33:07"), clock_basis="verified_time",
                 clocks=dict(exam_time=clock("2022-12-14 12:17:25"),
                             report_time=clock("2022-12-14 14:56:32"),
                             verified_time=clock("2022-12-16 15:33:07")),
                 text="虚构MR所见及结论"),
            dict(source_id="S-L", title="虚构后续病程", kind="document",
                 clock=clock("2022-12-17 10:00:00"), clock_basis="body_record_time",
                 clocks={}, text="后续决定手术。"),
        ]
        start = self.management_text.index("拟行胰十二指肠切除术。")
        span = dict(source_id="S-M", quote="拟行胰十二指肠切除术。", start=start,
                    end=start + len("拟行胰十二指肠切除术。"))
        self.candidates = [dict(event_id="EV-M", status="ACCEPTED", scope="yes",
                                record_function="new_decision", clock=clock("2022-12-14 10:00:00"),
                                candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST",
                                                      target="胰十二指肠切除术"),
                                origin_document_profile="round", span=span)]
        self.inventory = [dict(event_id="EA-D", discovery_unit_id="U-D", source_ids=["S-D"])]
        events = build_diagnostic_events(self.sources, self.inventory)
        self.events = events
        self.projection = self.diagnostic_projection("yes", "no")

    def diagnostic_projection(self, pre, post):
        reviews = []
        for kind, disposition, action in (
                ("pre_exam_evaluation", pre, "MR"),
                ("evidence_arrival_reassessment", post, "根治性切除")):
            reviews.append(dict(discovery_unit_id="U-D", candidate_kind=kind,
                                disposition=disposition, reason="合成Discovery审核",
                                candidate_action=dict(action_type="MANAGE", target=action)))
        return project_candidate_checkpoints(
            case_id="SYN", candidates=self.candidates, sources=self.sources,
            diagnostic_events=self.events, diagnostic_reviews=reviews)

    def material(self, projection=None):
        return project_candidate_materials(
            case_id="SYN", sources=self.sources, candidates=self.candidates,
            projection=projection or self.projection)

    def packet(self, kind, projection=None):
        return next(packet for packet in self.material(projection)["packets"]
                    if packet["kind"] == kind)

    def test_pre_exam_has_time_question_and_no_target_modality_or_result(self):
        packet = self.packet("pre_exam_evaluation")
        self.assertEqual(packet["evaluation"]["clock"]["value"], "2022-12-14 12:17:25")
        self.assertIn("是否应安排MR", packet["management_question"])
        projected = "".join(source["title"] + source["text"]
                            for source in packet["included_sources"])
        self.assertNotIn("MR", projected)
        self.assertNotIn("虚构MR所见及结论", projected)
        self.assertEqual(packet["masked_projection"]["whole_source_ids"], ["S-D"])
        self.assertIn("MR", packet["researcher_target_originals"][0]["text"]
                      + packet["researcher_target_originals"][0]["title"])

    def test_report_uses_report_time_shows_report_and_excludes_future(self):
        packet = self.packet("evidence_arrival_reassessment",
                             self.diagnostic_projection("no", "yes"))
        self.assertEqual(packet["evaluation"]["clock"]["value"], "2022-12-14 14:56:32")
        target = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-D")
        self.assertEqual(target["text"], "虚构MR所见及结论")
        self.assertIn("S-L", {source["source_id"] for source in packet["excluded_sources"]})

    def test_one_diagnostic_source_has_one_checkpoint_and_no_answer_pair(self):
        packets = [packet for packet in self.material()["packets"]
                   if packet["kind"] != "management_decision"]
        self.assertEqual(len(packets), 1)
        pre = packets[0]
        self.assertFalse(any(item["relation"] == "evidence_arrival"
                             for item in pre["related_checkpoints"]))
        self.assertTrue(pre["not_doctor_release"])
        self.assertFalse(pre["masked_projection"]["cross_document_reviewed"])

    def test_discovery_only_result_has_no_relation_or_episode_container(self):
        from scripts.cohort_construction.p2_checkpoint_harness.render_candidate_materials import _stage_items
        raw = dict(relations=None, episodes=None)
        self.assertEqual(_stage_items(raw, "relations", "recorded"), [])
        self.assertEqual(_stage_items(raw, "episodes", "episodes"), [])

    def test_management_source_group_masks_target_span(self):
        packet = self.packet("management_decision")
        self.assertEqual(packet["target"]["document_profile"], "round")
        self.assertEqual(self.packet("pre_exam_evaluation")["target"]["document_profile"],
                         "performed_test_report")
        target = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-M")
        self.assertNotIn("拟行胰十二指肠切除术", target["text"])
        self.assertNotIn("拟施麻醉方式", target["text"])
        self.assertNotIn("手术指征：需手术切除", target["text"])
        self.assertIn("评估病情", target["text"])

    def test_management_action_fallback_masks_actions_but_preserves_adjacent_facts(self):
        actions = (
            "可考虑予抑酸/抑酶",
            "诊疗计划：充分术前准备",
            "取消手术",
            "穿刺活检",
            "决定下一步治疗",
            "转入普外科",
            "择期手术",
            "完善检查",
            "出院/随诊",
        )
        for index, action in enumerate(actions):
            with self.subTest(action=action):
                source_id = "S-ACTION-" + str(index)
                text = "胰头占位伴胆道梗阻，" + action + "，肝功能仍异常。"
                self.sources.append(dict(
                    source_id=source_id, title="合成管理病程", kind="document",
                    clock=clock("2022-12-13 10:00:00"), clock_basis="body_record_time",
                    clocks={}, text=text))
                packet = self.packet("management_decision")
                card = next(source for source in packet["included_sources"]
                            if source["source_id"] == source_id)
                self.assertNotIn(action, card["text"])
                self.assertIn("胰头占位伴胆道梗阻", card["text"])
                self.assertIn("肝功能仍异常", card["text"])

    def test_document_created_after_cutoff_is_excluded_without_mutating_source(self):
        original_sources = deepcopy(self.sources)
        for index, key in enumerate(("source_create_time", "create_time", "record_create_time")):
            with self.subTest(key=key):
                source_id = "S-CREATED-" + str(index)
                source = dict(
                    source_id=source_id, title="晚创建病程", kind="document",
                    clock=clock("2022-12-13 10:00:00"), clock_basis="body_record_time",
                    clocks={key: clock("2022-12-14 11:00:00")}, text="截止后创建的正文。")
                self.sources.append(source)
                original = deepcopy(source)
                packet = self.packet("management_decision")
                excluded = next(item for item in packet["excluded_sources"]
                                if item["source_id"] == source_id)
                self.assertEqual(excluded["reason"], "SOURCE_CREATED_AFTER_CANDIDATE_CUTOFF")
                self.assertNotIn(source_id, {item["source_id"]
                                             for item in packet["included_sources"]})
                self.assertEqual(source, original)
        self.assertEqual(self.sources[:3], original_sources)

    def test_same_day_coarse_creation_clock_is_not_treated_as_exactly_after_cutoff(self):
        self.sources.append(dict(
            source_id="S-SAME-DAY", title="同日病程", kind="document",
            clock=clock("2022-12-13 10:00:00"), clock_basis="body_record_time",
            clocks=dict(source_create_time=clock("2022-12-14")), text="同日但先后不确定。"))
        packet = self.packet("management_decision")
        card = next(source for source in packet["included_sources"]
                    if source["source_id"] == "S-SAME-DAY")
        self.assertEqual(card["text"], "同日但先后不确定。")

    def test_report_time_visibility_is_not_overridden_by_document_creation_rule(self):
        self.sources[1]["clocks"]["source_create_time"] = clock("2022-12-15 10:00:00")
        packet = self.packet("evidence_arrival_reassessment",
                             self.diagnostic_projection("no", "yes"))
        report = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-D")
        self.assertEqual(report["text"], "虚构MR所见及结论")
        self.assertEqual(report["clock"]["value"], "2022-12-14 14:56:32")
        self.assertIsNone(report["clocks"]["source_create_time"]["value"])
        self.assertEqual(self.sources[1]["clocks"]["source_create_time"]["value"],
                         "2022-12-15 10:00:00")

    def test_checkpoint_without_visible_context_becomes_review_migration_not_task(self):
        self.sources[0]["clocks"]["source_create_time"] = clock("2022-12-15 10:00:00")
        material = self.material(self.diagnostic_projection("no", "no"))
        self.assertEqual(material["packets"], [])
        self.assertEqual(material["summary"]["material_no_visible_context_count"], 1)
        self.assertEqual(material["summary"]["material_action_cycle_count"], 0)
        self.assertEqual(material["summary"]["material_evidence_state_count"], 0)
        self.assertEqual(material["summary"]["migration_reason_counts"], {
            "DISCOVERY_AGENT_REJECTED_OUT_OF_RESEARCH_SCOPE": 2,
            "NO_VISIBLE_SOURCE_AT_CANDIDATE_CUTOFF": 1})
        migration = next(item for item in material["migrations"]
                         if item.get("event_id") == "EV-M")
        self.assertEqual(migration["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(migration["reason"], "NO_VISIBLE_SOURCE_AT_CANDIDATE_CUTOFF")
        self.assertIsNone(migration["to_checkpoint_id"])
        self.assertTrue(all(not episode["checkpoint_ids"]
                            for episode in material["management_episodes"]))
        self.assertTrue(all(not episode["phase_transitions"]
                            for episode in material["management_episodes"]))

    def test_before_with_only_wholly_masked_target_has_no_answerable_task(self):
        self.sources[0]["clocks"]["source_create_time"] = clock("2022-12-14 13:00:00")
        material = self.material()
        self.assertFalse(any(packet["kind"] == "pre_exam_evaluation"
                             for packet in material["packets"]))
        self.assertEqual(material["summary"]["material_no_visible_context_count"], 2)
        migration = next(item for item in material["migrations"]
                         if item.get("event_id") == "EA-D"
                         and item.get("node_kind") == "pre_exam_evaluation")
        self.assertEqual(migration["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(migration["reason"], "NO_VISIBLE_SOURCE_AT_CANDIDATE_CUTOFF")

    def test_before_ct_alias_does_not_leak_target_modality(self):
        self.sources[1]["title"] = "胰腺CT增强"
        self.sources.append(dict(
            source_id="S-CT-CONTEXT", title="既往病史", kind="document",
            clock=clock("2022-12-13 10:00:00"), clock_basis="body_record_time",
            clocks={}, text="外院CT提示胰头占位，具体报告暂未调取。"))
        packet = self.packet("pre_exam_evaluation")
        context = next(source for source in packet["included_sources"]
                       if source["source_id"] == "S-CT-CONTEXT")
        self.assertNotIn("CT", context["text"])
        self.assertEqual(context["text"], "[目标管理判断已遮蔽]")

    def test_candidate_relations_and_episodes_replace_temporary_containers(self):
        base = self.material()["packets"]
        management = next(packet for packet in base if packet["kind"] == "management_decision")
        pre = next(packet for packet in base if packet["kind"] == "pre_exam_evaluation")
        relation = dict(source_checkpoint_id=management["checkpoint_id"],
                        target_checkpoint_id=pre["checkpoint_id"],
                        relation="same_management_process", confidence="supported",
                        explanation="合成关系", basis_quotes=[])
        material = project_candidate_materials(
            case_id="SYN", sources=self.sources, candidates=self.candidates,
            projection=self.projection, candidate_relations=[relation],
            candidate_episodes=[dict(episode_id="EP-NEW", checkpoint_ids=[
                management["checkpoint_id"], pre["checkpoint_id"]])])
        self.assertEqual({packet["episode_id"] for packet in material["packets"]}, {"EP-NEW"})
        self.assertTrue(all(packet["candidate_organization_applied"]
                            for packet in material["packets"]))
        self.assertTrue(all(packet["episode_navigation"]["total"] == 2
                            for packet in material["packets"]))
        self.assertTrue(all(packet["sequence_navigation"]["total"] == 2
                            for packet in material["packets"]))
        projected_management = next(packet for packet in material["packets"]
                                    if packet["kind"] == "management_decision")
        edge = next(item for item in projected_management["related_checkpoints"]
                    if item["checkpoint_id"] == pre["checkpoint_id"])
        self.assertEqual(edge["direction"], "outgoing")
        self.assertEqual(edge["relation"], "same_management_process")

    def test_global_navigation_does_not_depend_on_episode(self):
        material = self.material()
        ordered = sorted(material["packets"], key=lambda item: item["display_order"]["index"])
        self.assertGreater(len(ordered), 1)
        self.assertEqual(ordered[0]["episode_navigation"]["total"], 1)
        self.assertIsNone(ordered[0]["sequence_navigation"]["previous_checkpoint_id"])
        self.assertEqual(ordered[0]["sequence_navigation"]["next_checkpoint_id"],
                         ordered[1]["checkpoint_id"])
        self.assertEqual(ordered[1]["sequence_navigation"]["previous_checkpoint_id"],
                         ordered[0]["checkpoint_id"])

    def test_candidate_organization_rejects_unknown_endpoints_and_members(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_CANDIDATE_RELATION_ENDPOINT"):
            project_candidate_materials(
                case_id="SYN", sources=self.sources, candidates=self.candidates,
                projection=self.projection, candidate_episodes=[],
                candidate_relations=[dict(source_checkpoint_id="CP-NOPE",
                                          target_checkpoint_id="CP-ALSO-NOPE")])
        with self.assertRaisesRegex(ValueError, "UNKNOWN_CANDIDATE_EPISODE_MEMBER"):
            project_candidate_materials(
                case_id="SYN", sources=self.sources, candidates=self.candidates,
                projection=self.projection, candidate_relations=[],
                candidate_episodes=[dict(episode_id="EP-BAD", checkpoint_ids=["CP-NOPE"])])


    def test_relation_quotes_do_not_enter_timed_packet_projection(self):
        post_projection = self.diagnostic_projection("no", "yes")
        base = self.material(post_projection)["packets"]
        management = next(packet for packet in base if packet["kind"] == "management_decision")
        post = next(packet for packet in base if packet["kind"] == "evidence_arrival_reassessment")
        relation = dict(source_checkpoint_id=post["checkpoint_id"],
                        target_checkpoint_id=management["checkpoint_id"],
                        relation="evidence_used", confidence="supported",
                        explanation="未来决定立即行根治性胰十二指肠切除术。",
                        shared_management_goal="未来根治性胰十二指肠切除术",
                        basis_quotes=[dict(checkpoint_id=management["checkpoint_id"],
                                           source_id="S-M", quote="拟行胰十二指肠切除术。")])
        material = project_candidate_materials(
            case_id="SYN", sources=self.sources, candidates=self.candidates,
            projection=post_projection, candidate_relations=[relation],
            candidate_episodes=[dict(episode_id="EP-" + packet["checkpoint_id"],
                                     checkpoint_ids=[packet["checkpoint_id"]])
                                for packet in base])
        packet = next(packet for packet in material["packets"]
                      if packet["kind"] == "evidence_arrival_reassessment")
        edge = next(item for item in packet["related_checkpoints"]
                    if item["relation"] == "evidence_used")
        self.assertNotIn("basis_quotes", edge)
        self.assertNotIn("explanation", edge)
        self.assertNotIn("shared_management_goal", edge)
        self.assertNotIn("未来根治性胰十二指肠切除术", json.dumps(edge, ensure_ascii=False))
        self.assertEqual(edge["basis_quote_count"], 1)
        target = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-M")
        self.assertNotIn("拟行胰十二指肠切除术", target["text"])

    def test_new_same_event_relation_masks_other_checkpoint_answer(self):
        self.sources[2]["clock"] = clock("2022-12-13 10:00:00")
        projection = deepcopy(self.projection)
        original = next(item for item in projection["candidate_checkpoints"]
                        if item["kind"] == "management_decision")
        other = deepcopy(original)
        other.update(checkpoint_id="CP-OTHER", source_ids=["S-L"],
                     clock=clock("2022-12-13 10:00:00"),
                     masked_spans=[dict(source_id="S-L", start=0,
                                        end=len("后续决定手术。"), quote="后续决定手术。")])
        projection["candidate_checkpoints"].append(other)
        relation = dict(source_checkpoint_id=original["checkpoint_id"],
                        target_checkpoint_id="CP-OTHER", relation="same_event",
                        confidence="supported", explanation="合成严格重复")
        episodes = [dict(episode_id="EP-" + item["checkpoint_id"],
                         checkpoint_ids=[item["checkpoint_id"]])
                    for item in projection["candidate_checkpoints"]]
        material = project_candidate_materials(
            case_id="SYN", sources=self.sources, candidates=self.candidates,
            projection=projection, candidate_relations=[relation],
            candidate_episodes=episodes)
        packet = next(packet for packet in material["packets"]
                      if packet["checkpoint_id"] == original["checkpoint_id"])
        other_source = next(source for source in packet["included_sources"]
                            if source["source_id"] == "S-L")
        self.assertNotIn("后续决定手术", other_source["text"])

    def test_arrival_masks_same_time_management_action_but_not_target_report(self):
        self.sources.append(dict(
            source_id="S-SAME", title="同刻病程", kind="document",
            clock=clock("2022-12-14 14:56:32"), clock_basis="body_record_time",
            clocks={}, text="同意上述意见，恶性待排，需手术治疗。阅片后建议转肿瘤内科接受系统化疗。"))
        packet = self.packet("evidence_arrival_reassessment",
                             self.diagnostic_projection("no", "yes"))
        same_time = next(source for source in packet["included_sources"]
                         if source["source_id"] == "S-SAME")
        self.assertNotIn("系统化疗", same_time["text"])
        self.assertNotIn("转肿瘤内科", same_time["text"])
        report = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-D")
        self.assertEqual(report["text"], "虚构MR所见及结论")

    def test_pre_exam_masks_target_restatement_in_another_diagnostic_report(self):
        self.sources.append(dict(
            source_id="S-OTHER", title="另一份影像报告", kind="imaging",
            clock=clock("2022-12-13 09:00:00"), clock_basis="report_time",
            clocks=dict(report_time=clock("2022-12-13 09:00:00")),
            text="结合同日增强MR：胰头占位，包绕肠系膜上动脉。"))
        self.events = build_diagnostic_events(self.sources, self.inventory + [
            dict(event_id="EA-OTHER", discovery_unit_id="U-OTHER", source_ids=["S-OTHER"])])
        self.projection = self.diagnostic_projection("yes", "no")
        packet = self.packet("pre_exam_evaluation")
        other = next(source for source in packet["included_sources"]
                     if source["source_id"] == "S-OTHER")
        self.assertNotIn("包绕肠系膜上动脉", other["text"])
        self.assertNotIn("增强MR", other["text"])

    def test_future_body_datetime_and_source_clocks_are_redacted(self):
        self.sources[0]["text"] += "\n签名时间：2022-12-15 10:44:00"
        self.sources[0]["text"] += "\n点号日期：2022.12.15 10:44:00"
        self.sources[0]["text"] += "\n既往时间：2022-12-13 09:00:00"
        packet = self.packet("pre_exam_evaluation")
        target = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-D")
        self.assertIsNone(target["clocks"]["report_time"]["value"])
        self.assertIsNone(target["clocks"]["verified_time"]["value"])
        self.assertEqual(target["clocks"]["exam_time"]["value"], "2022-12-14 12:17:25")
        management = next(source for source in packet["included_sources"]
                          if source["source_id"] == "S-M")
        self.assertNotIn("2022-12-15 10:44:00", management["text"])
        self.assertNotIn("2022.12.15 10:44:00", management["text"])
        self.assertIn("2022-12-13 09:00:00", management["text"])
        original = next(source for source in packet["researcher_target_originals"]
                        if source["source_id"] == "S-D")
        self.assertEqual(original["clocks"]["verified_time"]["value"], "2022-12-16 15:33:07")

    def test_organized_material_requires_complete_checkpoint_coverage(self):
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_CANDIDATE_EPISODE_COVERAGE"):
            project_candidate_materials(
                case_id="SYN", sources=self.sources, candidates=self.candidates,
                projection=self.projection, candidate_relations=[], candidate_episodes=[])

    def test_organization_input_is_bound_to_case_and_projection(self):
        from scripts.cohort_construction.p2_checkpoint_harness.render_candidate_materials import (
            _projection_sha256, _validate_organization)
        projection_hash = _projection_sha256(self.projection)
        relations = dict(format="p2-candidate-cp-relations-0.1.0", case_id="SYN",
                         candidate_projection_sha256=projection_hash, relations=[])
        episodes = dict(format="p2-candidate-cp-episodes-0.1.0", case_id="SYN",
                        candidate_projection_sha256=projection_hash, episodes=[],
                        conservation=dict(all_candidate_checkpoints_conserved=True))
        _validate_organization(relations, episodes, case_id="SYN", projection=self.projection)
        episodes["candidate_projection_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "PROJECTION_MISMATCH"):
            _validate_organization(relations, episodes, case_id="SYN", projection=self.projection)


if __name__ == "__main__":
    unittest.main()

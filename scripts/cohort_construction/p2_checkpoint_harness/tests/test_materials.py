"""Candidate material projection; synthetic text only, no clinical Reference."""
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.materials import (
    project_materials, write_material_set, digest,
)
from scripts.cohort_construction.p2_checkpoint_harness.stages.materials_html import render_html


class MaterialTests(unittest.TestCase):
    def test_HCM001_candidate_anchor_masks_coexisting_decisions_and_keeps_time_roles(self):
        source = dict(source_id="S-A", title="病程", kind="document",
                      source_sha256=None, clock=dict(value="2026-03-05"),
                      clock_basis="body_record_time", clocks={},
                      text="拟定手术。白蛋白较低，必要时补充白蛋白。之后出院。")
        import hashlib
        source["source_sha256"] = hashlib.sha256(source["text"].encode()).hexdigest()
        events = [
            dict(event_id="EV-A", status="ACCEPTED", scope="yes", record_function="new_decision",
                 clock=dict(value="2026-03-05"), span=dict(source_id="S-A", quote="拟定手术。", start=0, end=5)),
            dict(event_id="EV-B", status="ACCEPTED", scope="yes", record_function="new_decision",
                 clock=dict(value="2026-03-06"), span=dict(source_id="S-A", quote="白蛋白较低，必要时补充白蛋白。", start=5, end=20)),
        ]
        result = project_materials(case_id="SYN", sources=[source], candidates=events,
                                   evidence_inventory=[], relations=[], episodes=[
                                       dict(episode_id="EP001", event_ids=["EV-A", "EV-B"], open_questions=[])])
        self.assertEqual(len(result["packets"]), 2)
        text = result["packets"][0]["included_sources"][0]["text"]
        self.assertNotIn("拟定手术。", text)
        self.assertNotIn("必要时补充白蛋白", text)
        self.assertIn("之后出院。", text)  # Explicitly unassessed adjacent leakage.
        self.assertFalse(result["packets"][0]["blinding"]["adjacent_explanations_results_and_leakage_reviewed"])
        self.assertNotEqual(result["packets"][0]["target"]["clock_role"], "confirmed_choice_time")
        self.assertFalse(result["not_doctor_release"] is False)

    def test_HCM002_unknown_clock_retains_original_source_without_false_mask_claim(self):
        import hashlib
        text = "拟手术并继续治疗。"
        source = dict(source_id="S-U", title="手术核查", kind="document",
                      source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                      clock=dict(value=None), clock_basis="body_record_time",
                      clocks=dict(source_create_time=dict(value="2026-03-05 12:00")), text=text)
        event = dict(event_id="EV-U", status="ACCEPTED", scope="yes",
                     record_function="execution", clock=dict(value=None),
                     span=dict(source_id="S-U", quote="拟手术并继续治疗。", start=0, end=9))
        result = project_materials(case_id="SYN", sources=[source], candidates=[event],
                                   evidence_inventory=[], relations=[], episodes=[])
        packet = result["packets"][0]
        self.assertEqual(packet["included_sources"][0]["text"], text)
        self.assertEqual(packet["included_sources"][0]["clocks"]["source_create_time"]["value"],
                         "2026-03-05 12:00")
        self.assertFalse(packet["blinding"]["target_candidate_masked_in_its_source"])
        self.assertTrue(packet["blinding"]["prior_source_content_preserved"])

    def test_HCM003_prior_operation_is_evidence_not_masked_at_later_checkpoint(self):
        import hashlib
        text = "实施切除。后续补充白蛋白。"
        source = dict(source_id="S-P", title="手术记录", kind="document",
                      source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                      clock=dict(value="2026-03-01"), clock_basis="body_record_time",
                      clocks={}, text=text)
        later_text = "继续补充白蛋白。"
        later_source = dict(source_id="S-L", title="病程", kind="document",
                            source_sha256=hashlib.sha256(later_text.encode()).hexdigest(),
                            clock=dict(value="2026-03-05"), clock_basis="body_record_time",
                            clocks={}, text=later_text)
        events = [dict(event_id="EV-P", status="ACCEPTED", scope="yes",
                       record_function="execution", clock=dict(value="2026-03-01"),
                       span=dict(source_id="S-P", quote="实施切除。", start=0, end=5)),
                  dict(event_id="EV-L", status="ACCEPTED", scope="yes",
                       record_function="new_decision", clock=dict(value="2026-03-05"),
                       span=dict(source_id="S-L", quote="继续补充白蛋白。", start=0, end=8))]
        result = project_materials(case_id="SYN", sources=[source, later_source], candidates=events,
                                   evidence_inventory=[], relations=[], episodes=[])
        packet = next(packet for packet in result["packets"]
                      if packet["candidate_event_id"] == "EV-L")
        self.assertIn("实施切除。", packet["included_sources"][0]["text"])


class MaterialInspectionTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        self.text = "当前决定：评估切除。\n之后补充白蛋白。\n完成记录。"
        self.later_text = "继续观察。"
        self.sources = [
            dict(source_id="S-A", title="虚构病程", kind="document",
                 clock=dict(value="2026-03-05 10:00:00", precision="minute"),
                 clock_basis="body_record_time",
                 clocks=dict(source_create_time=dict(value="2026-03-05 10:02:00")),
                 source_sha256=hashlib.sha256(self.text.encode()).hexdigest(), text=self.text),
            dict(source_id="S-B", title="虚构同意书", kind="document",
                 clock=dict(value=None), clock_basis="body_record_time",
                 clocks=dict(source_create_time=dict(value="2026-03-06 08:00:00")),
                 source_sha256=hashlib.sha256(self.later_text.encode()).hexdigest(),
                 text=self.later_text)]
        self.events = [
            dict(event_id="EV-A", status="ACCEPTED", scope="yes",
                 record_function="new_decision", clock=dict(value="2026-03-05 10:00:00"),
                 span=dict(source_id="S-A", quote="当前决定：评估切除。", start=0, end=10),
                 rationale="发现阶段LLM的范围说明"),
            dict(event_id="EV-B", status="ACCEPTED", scope="yes", record_function="execution",
                 clock=dict(value="2026-03-05 11:00:00"),
                 span=dict(source_id="S-A", quote="之后补充白蛋白。", start=11, end=19)),
            dict(event_id="EV-C", status="ACCEPTED", scope="yes",
                 record_function="reassessment", clock=dict(value=None, precision="unknown"),
                 span=dict(source_id="S-B", quote="继续观察。", start=0, end=5))]
        self.episodes = [dict(episode_id="EP001", event_ids=["EV-A", "EV-B"],
                              open_questions=["NO_DOCUMENTED_CHOICE_TIME"]),
                         dict(episode_id="EP002", event_ids=["EV-C"], open_questions=[])]
        self.relations = [dict(source_event_id="EV-A", target_event_id="EV-B",
                               relation="same_management_process", confidence="supported",
                               explanation="合成关系，不是临床确认",
                               basis_quotes=[dict(source_id="S-A", quote="当前决定：评估切除。"),
                                             dict(source_id="S-A", quote="之后补充白蛋白。")])]

    def project(self):
        return project_materials(case_id="SYN", sources=self.sources, candidates=self.events,
                                 evidence_inventory=[], relations=self.relations,
                                 episodes=self.episodes)

    def test_HCM004_current_candidate_and_masked_source_are_separate_layers(self):
        packet = self.project()["packets"][0]
        self.assertEqual(packet["researcher_candidate_inspection"]["span"]["quote"],
                         "当前决定：评估切除。")
        self.assertEqual(packet["target"]["source_title"], "虚构病程")
        self.assertIn("source_create_time", packet["target"]["source_clocks"])
        self.assertIn("current_candidate_source_masked_projection",
                      packet["included_sources"][0]["presentation_scope"])
        self.assertIn("[当前候选定位原文已遮蔽]", packet["included_sources"][0]["text"])
        self.assertIn("[目标时点之后的候选定位原文已遮蔽]", packet["included_sources"][0]["text"])
        self.assertNotIn("当前决定：评估切除。", packet["included_sources"][0]["text"])
        self.assertEqual(packet["researcher_candidate_inspection"]["visibility"],
                         "private_research_audit_not_doctor_release")

    def test_HCM005_neighbors_and_agent_relation_basis_are_clickable_data(self):
        packet = self.project()["packets"][0]
        self.assertEqual(packet["episode_navigation"]["previous_checkpoint_id"], None)
        self.assertEqual(packet["episode_navigation"]["next_checkpoint_id"], "CP-B")
        self.assertEqual(packet["episode_navigation"]["order_role"],
                         "candidate_display_order_not_confirmed_clinical_sequence")
        related = packet["related_candidates"][0]
        self.assertEqual(related["checkpoint_id"], "CP-B")
        self.assertEqual(related["span"]["quote"], "之后补充白蛋白。")
        self.assertEqual(related["relation"]["explanation"], "合成关系，不是临床确认")
        self.assertEqual(packet["related_candidates"][0]["within_current_episode"], True)

    def test_HCM006_unknown_clock_has_source_clues_not_empty_masked_claim(self):
        packet = next(p for p in self.project()["packets"]
                      if p["candidate_event_id"] == "EV-C")
        self.assertEqual(len(packet["included_sources"]), 1)
        self.assertEqual(packet["included_sources"][0]["text"], "继续观察。")
        self.assertIn("source_create_time", packet["target"]["source_clocks"])
        self.assertFalse(packet["blinding"]["target_candidate_masked_in_its_source"])

    def test_HCM007_renders_one_selected_checkpoint_with_navigation_and_bound_response(self):
        material = self.project()
        material["packets"][1]["related_candidates"] = []
        page = render_html(material, digest({"synthetic": "manifest"}))
        self.assertEqual(page.count('class=\\"review\\"'), 3)
        self.assertEqual(page.count('id="checkpoint"'), 1)
        self.assertIn('class=\\"previous\\"', page)
        self.assertIn('class=\\"next\\"', page)
        self.assertIn("查看 Agent 提议的候选定位原文", page)
        self.assertIn('id=\\"panel-increment\\"', page)
        self.assertIn('id=\\"panel-target\\"', page)
        self.assertIn('id=\\"panel-relations\\"', page)
        self.assertIn("遮蔽前后对照", page)
        self.assertIn("Agent 已记录直接关系", page)
        self.assertIn("不等于临床上无关系", page)
        self.assertIn("development-preview-0.2", page)
        self.assertIn("candidate_display_order_not_confirmed_clinical_sequence", page)
        self.assertIn("material_packet_content_sha256", page)
        self.assertIn("caseId=\"SYN\"", page)
        self.assertIn("未请求网络", page)
        self.assertIn("上一候选", page)
        self.assertIn("下一候选", page)

    def test_HCM008_written_manifest_binds_navigation_enriched_packet(self):
        import tempfile
        from pathlib import Path
        material = self.project()
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_material_set(material, directory)
            packet = next(p for p in manifest["packets"] if p["checkpoint_id"] == "CP-A")
            saved = __import__("json").loads((Path(directory) / "CP-A.json").read_text())
            self.assertEqual(packet["content_sha256"], saved["content_sha256"])
            self.assertIn("display_order", saved)
            self.assertEqual(manifest["format"], "p2-candidate-checkpoint-material-0.2.0-set")


if __name__ == "__main__":
    unittest.main()
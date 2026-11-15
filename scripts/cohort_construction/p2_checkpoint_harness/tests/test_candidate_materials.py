import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_checkpoints import project_candidate_checkpoints
from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials import project_candidate_materials
from scripts.cohort_construction.p2_checkpoint_harness.stages.diagnostic_events import build_diagnostic_events


def clock(value):
    return dict(value=value, precision="second" if value else "unknown")


class CandidateMaterialTests(unittest.TestCase):
    def setUp(self):
        self.management_text = "评估病情。\n拟行MR检查。\n拟施麻醉方式：全麻\n之后记录。"
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
        start = self.management_text.index("拟行MR检查。")
        span = dict(source_id="S-M", quote="拟行MR检查。", start=start,
                    end=start + len("拟行MR检查。"))
        self.candidates = [dict(event_id="EV-M", status="ACCEPTED", scope="yes",
                                record_function="new_decision", clock=clock("2022-12-14 10:00:00"),
                                origin_document_profile="round", span=span)]
        self.inventory = [dict(event_id="EA-D", source_ids=["S-D"])]
        events = build_diagnostic_events(self.sources, self.inventory)
        self.projection = project_candidate_checkpoints(
            case_id="SYN", candidates=self.candidates, sources=self.sources,
            diagnostic_events=events)

    def material(self):
        return project_candidate_materials(
            case_id="SYN", sources=self.sources, candidates=self.candidates,
            projection=self.projection)

    def packet(self, kind):
        return next(packet for packet in self.material()["packets"]
                    if packet["kind"] == kind)

    def test_pre_exam_has_time_question_and_no_target_modality_or_result(self):
        packet = self.packet("pre_exam_evaluation")
        self.assertEqual(packet["evaluation"]["clock"]["value"], "2022-12-14 12:17:25")
        self.assertIn("下一步管理", packet["management_question"])
        projected = "".join(source["title"] + source["text"]
                            for source in packet["included_sources"])
        self.assertNotIn("MR", projected)
        self.assertNotIn("虚构MR所见及结论", projected)
        self.assertEqual(packet["masked_projection"]["whole_source_ids"], ["S-D"])
        self.assertIn("MR", packet["researcher_target_originals"][0]["text"]
                      + packet["researcher_target_originals"][0]["title"])

    def test_report_uses_report_time_shows_report_and_excludes_future(self):
        packet = self.packet("evidence_arrival_reassessment")
        self.assertEqual(packet["evaluation"]["clock"]["value"], "2022-12-14 14:56:32")
        target = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-D")
        self.assertEqual(target["text"], "虚构MR所见及结论")
        self.assertIn("S-L", {source["source_id"] for source in packet["excluded_sources"]})

    def test_diagnostic_pair_is_linked_and_not_a_doctor_release(self):
        pre = self.packet("pre_exam_evaluation")
        post = self.packet("evidence_arrival_reassessment")
        self.assertEqual(pre["related_checkpoints"][0]["checkpoint_id"],
                         post["checkpoint_id"])
        self.assertEqual(pre["episode_id"], post["episode_id"])
        self.assertTrue(pre["not_doctor_release"])
        self.assertFalse(pre["masked_projection"]["cross_document_reviewed"])

    def test_management_source_group_masks_target_span(self):
        packet = self.packet("management_decision")
        target = next(source for source in packet["included_sources"]
                      if source["source_id"] == "S-M")
        self.assertNotIn("拟行MR检查", target["text"])
        self.assertNotIn("拟施麻醉方式", target["text"])
        self.assertIn("评估病情", target["text"])


if __name__ == "__main__":
    unittest.main()

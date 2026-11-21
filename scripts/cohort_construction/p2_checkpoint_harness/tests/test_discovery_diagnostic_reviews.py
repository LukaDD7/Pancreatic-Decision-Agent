"""Discovery diagnostic-candidate screening counterexamples; synthetic only."""
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_harness.harness import (
    Harness, HarnessConfig,
)
from scripts.cohort_construction.p2_checkpoint_harness.pipeline import CaseRunner
from scripts.cohort_construction.p2_checkpoint_harness.stages.actions import action_ids
from scripts.cohort_construction.p2_checkpoint_harness.stages.discovery import build_prompt
from scripts.cohort_construction.p2_checkpoint_harness.tests.test_pipeline import (
    StubTransport, _finish, _proposal,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools import ToolError, ToolRegistry
from scripts.cohort_construction.p2_checkpoint_harness.tools.proposals import (
    ProposalStore, register,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import SourceIndex


REPORT = "增强CT：胰头占位，包绕肠系膜上动脉。"


def _unit(kind="performed_test_report"):
    return dict(unit_id="TU-I", source_ids=["S-I"], kind=kind,
                document_profile=kind, clock=dict(value="2026-03-03"),
                original_texts=[dict(source_id="S-I", text=REPORT)])


def _store(kind="performed_test_report"):
    unit = _unit(kind)
    source = dict(source_id="S-I", text=REPORT, source_sha256="i" * 64,
                  clock=dict(value="2026-03-03", precision="date"))
    return ProposalStore(source_index=SourceIndex([source], units=[unit]),
                         unit_id=unit["unit_id"], unit_kind=kind)


class DiagnosticReviewToolTests(unittest.TestCase):
    def test_both_candidate_kinds_are_saved_and_revisions_keep_previous_record(self):
        store = _store()
        tool = store.tool_review_diagnostic_candidate()
        pre = tool.run(candidate_kind="pre_exam_evaluation", disposition="yes",
                       reason="检查安排可用于评估根治路径",
                       action_id="DIAG_PANCREATIC_CONTRAST_CT",
                       action_type="perform_diagnostic_test", target="增强CT")
        post = tool.run(candidate_kind="evidence_arrival_reassessment",
                        disposition="uncertain", reason="尚无明确可作答的后续动作")
        self.assertTrue(pre["review_id"].startswith("DR-"))
        self.assertNotEqual(pre["review_id"], post["review_id"])
        self.assertEqual(pre["candidate_action"]["target"], "胰腺增强CT扫描")
        self.assertEqual(pre["candidate_action"]["original_target"], "增强CT")
        self.assertEqual(pre["candidate_action"]["original_action_type"],
                         "perform_diagnostic_test")
        tool.run(candidate_kind="pre_exam_evaluation", disposition="no",
                 reason="修订：当前候选不属于研究任务")
        summary = store.summary()
        self.assertEqual(summary["diagnostic_candidate_reviews"], 2)
        self.assertEqual(summary["diagnostic_no"], 1)
        self.assertEqual(summary["diagnostic_uncertain"], 1)
        self.assertEqual(summary["diagnostic_revisions"], 1)
        self.assertTrue(summary["diagnostic_review_complete"])
        self.assertEqual(summary["diagnostic_missing_candidate_kinds"], [])
        revision = store.diagnostic_revisions[0]
        self.assertEqual(revision["previous_disposition"], "yes")
        self.assertEqual(revision["previous_record"]["candidate_action"]["target"],
                         "胰腺增强CT扫描")
        self.assertIn("不是专家真值", post["note"])

    def test_picc_is_explicitly_rejected_for_rationale_not_keyword_filter(self):
        tool = _store().tool_review_diagnostic_candidate()
        result = tool.run(
            candidate_kind="pre_exam_evaluation", disposition="no",
            reason="PICC是输液通路，不属于PDAC根治路径")
        self.assertEqual(result["disposition"], "no")
        self.assertIsNone(result["candidate_action"])

    def test_yes_requires_action_id_and_other_requires_specific_target(self):
        tool = _store().tool_review_diagnostic_candidate()
        with self.assertRaisesRegex(ToolError, "DIAGNOSTIC_ACTION_ID_REQUIRED"):
            tool.run(candidate_kind="pre_exam_evaluation", disposition="yes",
                     reason="需要检查", target="增强CT")
        with self.assertRaisesRegex(ToolError, "ACTION_TARGET_REQUIRED_FOR_OTHER_SPECIFIED"):
            tool.run(candidate_kind="evidence_arrival_reassessment", disposition="yes",
                     reason="需要额外动作", action_id="OTHER_SPECIFIED")
        with self.assertRaisesRegex(ToolError, "ACTION_TARGET_ABSTRACT_FOR_OTHER_SPECIFIED"):
            tool.run(candidate_kind="evidence_arrival_reassessment", disposition="yes",
                     reason="抽象目标不够", action_id="OTHER_SPECIFIED", target="完善检查")

    def test_reason_is_required_for_every_disposition(self):
        tool = _store().tool_review_diagnostic_candidate()
        for disposition in ("yes", "no", "uncertain"):
            with self.subTest(disposition=disposition):
                with self.assertRaisesRegex(ToolError, "DIAGNOSTIC_REVIEW_REASON_REQUIRED"):
                    tool.run(candidate_kind="pre_exam_evaluation",
                             disposition=disposition, reason=" ",
                             action_id="DIAG_PANCREATIC_CONTRAST_CT")

    def test_non_diagnostic_unit_cannot_call_or_spoof_unit_id(self):
        store = _store("document")
        registry = ToolRegistry(approve=lambda *args: True)
        register(registry, store)
        tool = registry.get("review_diagnostic_candidate")
        with self.assertRaisesRegex(ToolError,
                                    "DIAGNOSTIC_REVIEW_REQUIRES_PERFORMED_TEST_REPORT"):
            tool.run(candidate_kind="pre_exam_evaluation", disposition="no",
                     reason="非检查单位")
        self.assertNotIn("unit_id", tool.input_schema["properties"])
        with self.assertRaisesRegex(TypeError, "unit_id"):
            tool.run(candidate_kind="pre_exam_evaluation", disposition="no",
                     reason="伪造单位", unit_id="TU-I")


class ManagementActionTests(unittest.TestCase):
    def test_new_decision_and_reassessment_require_and_save_action(self):
        store = _store("document")
        tool = store.tool_propose_candidate()
        for function in ("new_decision", "reassessment"):
            with self.subTest(function=function):
                with self.assertRaisesRegex(ToolError, "CANDIDATE_ACTION_ID_REQUIRED"):
                    tool.run(local_id=function, scope="yes", record_function=function,
                             source_id="S-I", quote=REPORT, target="增强CT")
        result = tool.run(local_id="decision", scope="yes", record_function="new_decision",
                          source_id="S-I", quote=REPORT,
                          action_id="DIAG_PANCREATIC_CONTRAST_CT",
                          action_type="reassess_resectability", target="胰头肿瘤")
        self.assertTrue(result["accepted"])
        action = store.proposals["decision"]["record"]["candidate_action"]
        self.assertEqual(action["action_id"], "DIAG_PANCREATIC_CONTRAST_CT")
        self.assertEqual(action["action_type"], "ORDER_DIAGNOSTIC_TEST")
        self.assertEqual(action["target"], "胰腺增强CT扫描")
        self.assertEqual(action["original_action_type"], "reassess_resectability")
        self.assertEqual(action["original_target"], "胰头肿瘤")
        self.assertEqual(tool.input_schema["properties"]["action_id"]["enum"], action_ids())

    def test_scope_no_does_not_need_an_action(self):
        result = _store("document").tool_propose_candidate().run(
            local_id="excluded", scope="no", record_function="generic",
            source_id="S-I", quote=REPORT, reason="范围外")
        self.assertTrue(result["accepted"])


class DiagnosticPipelineTests(unittest.TestCase):
    def test_reviews_and_management_action_reach_auditable_artifacts(self):
        document = dict(source_id="S-D", text="讨论后拟行胰十二指肠切除术。",
                        source_sha256="d" * 64,
                        clock=dict(value="2026-03-05", precision="date"))
        report = dict(source_id="S-I", text=REPORT, source_sha256="i" * 64,
                      clock=dict(value="2026-03-03", precision="date"))
        document_unit = dict(unit_id="DU-D", source_ids=["S-D"], kind="document",
                             document_profile="preoperative_discussion",
                             original_texts=[dict(source_id="S-D", text=document["text"])])
        report_unit = _unit()
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(
                case_id="SYN", run_dir=Path(directory) / "run",
                cache_dir=Path(directory) / "cache",
                transport=StubTransport([
                    _proposal("surgery", "讨论后拟行胰十二指肠切除术",
                              action="胰十二指肠切除术", source_id="S-D"),
                    _finish("document-finish"),
                    dict(tool_calls=[dict(id="pre", name="review_diagnostic_candidate",
                                          arguments=dict(
                                              candidate_kind="pre_exam_evaluation",
                                              disposition="yes", reason="检查可用于根治路径评估",
                                              action_id="DIAG_PANCREATIC_CONTRAST_CT"))]),
                    dict(tool_calls=[dict(id="pre-revision", name="review_diagnostic_candidate",
                                          arguments=dict(
                                              candidate_kind="pre_exam_evaluation",
                                              disposition="yes", reason="修订：检查可用于根治路径评估",
                                              action_id="DIAG_PANCREATIC_CONTRAST_CT"))]),
                    dict(tool_calls=[dict(id="post", name="review_diagnostic_candidate",
                                          arguments=dict(
                                              candidate_kind="evidence_arrival_reassessment",
                                              disposition="yes", reason="报告后可重新评估可切除性",
                                              action_id="REVIEW_MDT",
                                              management_question="是否仍可按根治路径推进"))]),
                    _finish("report-finish"),
                ]), config=HarnessConfig(context_window=1_000_000))
            self.addCleanup(harness.close)
            runner = CaseRunner(harness=harness, sources=[document, report],
                                units=[document_unit, report_unit], case_id="SYN")
            runner.run_discovery()
            self.assertEqual(runner.events[0]["candidate_action"]["action_id"],
                             "SURG_PANCREATICODUODENECTOMY")
            self.assertEqual(runner.events[0]["candidate_action"]["action_type"],
                             "SURGICAL_RESECTION")
            self.assertEqual(runner.events[0]["candidate_action"]["target"],
                             "实施胰十二指肠切除术")
            self.assertEqual(len(runner.diagnostic_reviews), 2)
            artifacts = runner.artifacts()
            self.assertEqual(artifacts["diagnostic_candidate_reviews"],
                             runner.diagnostic_reviews)
            self.assertEqual(artifacts["counts"]["diagnostic_yes"], 2)
            self.assertEqual(artifacts["counts"]["diagnostic_candidate_review_revisions"], 1)
            self.assertEqual(artifacts["diagnostic_candidate_review_revisions"][0]
                             ["previous_record"]["reason"], "检查可用于根治路径评估")
            self.assertEqual(artifacts["discovery"]["units"]["TU-I"]["summary"]
                             ["diagnostic_review_complete"], True)
            post = next(review for review in runner.diagnostic_reviews
                        if review["candidate_kind"] == "evidence_arrival_reassessment")
            self.assertEqual(post["management_question"], "是否仍可按根治路径推进")
            self.assertEqual(post["review_role"],
                             "discovery_llm_screening_not_expert_reference")
            self.assertEqual([item["candidate_kind"] for item in runner._organization_objects()],
                             ["management_expression", "evidence_artifact"])
            self.assertFalse(any(item.get("review_role")
                                 for item in runner._organization_objects()))

    def test_performed_report_prompt_requires_both_reviews_and_picc_rationale(self):
        prompt = build_prompt(_unit())
        self.assertIn("pre_exam_evaluation", prompt)
        self.assertIn("evidence_arrival_reassessment", prompt)
        self.assertIn("review_diagnostic_candidate", prompt)
        self.assertIn("输液通路不属于PDAC根治路径", prompt)
        self.assertIn("不得编造检查选择原因", prompt)
        self.assertIn("不要另传 unit_id", prompt)
        self.assertIn("不得因缺少医嘱、指征或部署句而 uncertain", prompt)
        self.assertIn("不要求报告已写明管理决定", prompt)
        self.assertIn("action_id", prompt)
        self.assertIn("仅 OTHER_SPECIFIED 另填具体 target", prompt)


if __name__ == "__main__":
    unittest.main()

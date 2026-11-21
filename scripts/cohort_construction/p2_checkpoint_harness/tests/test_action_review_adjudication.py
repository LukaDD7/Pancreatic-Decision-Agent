"""Synthetic counterexamples for case-level action/phase review."""
from copy import deepcopy
from types import SimpleNamespace
import tempfile
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.spill import SpillStore, read_spilled
from scripts.cohort_construction.p2_checkpoint_harness.stages import (
    checkpoint_adjudication as adjudication,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools import Tool, ToolError, ToolRegistry
from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import SourceIndex


MANAGEMENT = "MANAGEMENT_REASSESSMENT"
AFTER = "AFTER_EVIDENCE_ARRIVAL"


def source(source_id, text, clock):
    return dict(source_id=source_id, text=text, title=source_id,
                clock=dict(value=clock, precision="minute" if clock else "unknown"))


def candidate(event_id, source_id, text, clock):
    return dict(event_id=event_id, record_function="reassessment",
                clock=dict(value=clock, precision="minute"),
                candidate_action=dict(action_type="REASSESS_RESECTABILITY",
                                      target="可切除性再评估"),
                span=dict(source_id=source_id, quote=text, start=0, end=len(text)))


def checkpoint(checkpoint_id, kind, clock, source_ids, actions, *, flagged=False, members=()):
    return dict(checkpoint_id=checkpoint_id, kind=kind,
                clock=dict(value=clock, precision="minute"), source_ids=source_ids,
                member_event_ids=list(members), supporting_event_ids=[],
                candidate_actions=deepcopy(actions), masked_spans=[],
                primary_evaluation_eligible=not flagged,
                review_flags=["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"] if flagged else [])


class ActionReviewAgent:
    def __init__(self, registry):
        self.registry = registry

    def act(self, prompt, *, final_tool):
        self.registry.get("list_candidate_checkpoints").run()
        context = self.registry.get("read_checkpoint_action_review_context").run("CP-CT")
        self.registry.get("read_source").run("S-CT")
        self.registry.get("read_source").run(context["operation_source_ids"][0])
        self.registry.get("review_checkpoint_action").run(
            checkpoint_id="CP-CT", disposition="REMOVE_INDEPENDENT_CP",
            rationale="手术完成后才回报的术前CT不再承担独立术前评价任务。")
        final = final_tool.run(note="复审完成")
        return SimpleNamespace(stop="answered", final=final)


class ActionReviewHarness:
    def session_for(self, name):
        return object()

    def new_registry(self, *, session):
        return ToolRegistry(approve=lambda *args: True)

    def agent(self, *, registry, session, system_prompt):
        return ActionReviewAgent(registry)


class ActionReviewAdjudicationTests(unittest.TestCase):
    def make_store(self):
        operation = source("S-OP", "手术时间：2026-03-02 09:00-12:00。", "2026-03-02 12:00")
        ct = source("S-CT", "术前增强CT示胰头肿瘤。", "2026-03-02 15:55")
        old_text = "建议完善分期评估。"
        new_text = "术后病理回报，讨论后续管理。"
        old = candidate("EV-OLD", "S-OLD", old_text, "2026-03-01 09:00")
        current = candidate("EV-NEW", "S-NEW", new_text, "2026-03-03 09:00")
        first = checkpoint("CP-FIRST", MANAGEMENT, "2026-03-01 09:00", ["S-OLD"],
                           [old["candidate_action"]], members=["EV-OLD"])
        ct_cp = checkpoint("CP-CT", AFTER, "2026-03-02 15:55", ["S-CT"],
                           [dict(action_type="REASSESS_RESECTABILITY",
                                 target="可切除性再评估")], flagged=True)
        last = checkpoint("CP-LAST", MANAGEMENT, "2026-03-03 09:00", ["S-NEW"],
                          [current["candidate_action"]], members=["EV-NEW"])
        registry = ToolRegistry(approve=lambda *args: True)
        sources = [operation, ct, source("S-OLD", old_text, "2026-03-01 09:00"),
                   source("S-NEW", new_text, "2026-03-03 09:00")]
        source_index = SourceIndex(sources)
        index = adjudication.AdjudicationCheckpointIndex([first, ct_cp, last], [old, current])
        source_index.register(registry)
        for tool in index.tools():
            registry.register(tool)
        bounds = dict(operation_start=dict(value="2026-03-02 09:00", precision="minute"),
                      evidence=[dict(source_id="S-OP")])
        store = adjudication.checkpoint_adjudication_tools(
            registry, checkpoint_index=index, source_index=source_index,
            task_time_bounds=bounds)
        return store, registry

    def test_action_review_context_includes_acquisition_clock_without_action(self):
        store, registry = self.make_store()
        checkpoint = store.checkpoints.by_id["CP-CT"]
        checkpoint["candidate_actions"] = []
        store.source_index.require("S-CT")["clocks"] = dict(
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            report_time=dict(value="2026-03-02 15:55", precision="minute"))
        context = registry.get("read_checkpoint_action_review_context").run("CP-CT")
        self.assertEqual(context["acquisition_clocks"], [dict(
            source_id="S-CT",
            exam_time=dict(value="2026-03-02 08:00", precision="minute"),
            specimen_time=None)])
        self.assertIn("是否留空不影响本复审", context["note"])

    def test_full_current_and_operation_records_are_required(self):
        store, registry = self.make_store()
        listed = registry.get("list_candidate_checkpoints").run()["checkpoints"]
        self.assertIn("ACTION_TASK_MISMATCH_REVIEW_REQUIRED",
                      next(item for item in listed if item["checkpoint_id"] == "CP-CT")[
                          "review_flags"])
        call = dict(checkpoint_id="CP-CT", disposition="REMOVE_INDEPENDENT_CP",
                    rationale="术后回报，不再具有独立术前任务。")
        with self.assertRaisesRegex(ToolError, "ACTION_REVIEW_CONTEXT_NOT_READ"):
            store.review_action(**call)
        registry.get("read_checkpoint_action_review_context").run("CP-CT")
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_SOURCE_NOT_READ"):
            store.review_action(**call)
        registry.get("read_source").run("S-CT")
        with self.assertRaisesRegex(ToolError, "OPERATION_RECORD_NOT_FULLY_READ"):
            store.review_action(**call)
        self.assertNotIn("CP-CT", store.action_reviews)

    def test_spilled_preview_is_not_full_delivery_until_locator_is_read(self):
        store, registry = self.make_store()
        store.source_index.require("S-CT")["text"] = "术前CT。" + "病程内容。" * 500
        registry.get("read_checkpoint_action_review_context").run("CP-CT")
        registry.get("read_source").run("S-OP")
        with tempfile.TemporaryDirectory() as directory:
            spill = SpillStore(directory, max_inline_tokens=10)
            spilled_registry = ToolRegistry(approve=lambda *args: True, spill=spill)
            store.source_index.register(spilled_registry)
            spilled_registry.register(Tool(
                name="read_spilled", description="分页读取溢出结果",
                input_schema=dict(type="object"),
                run=lambda path, start=0, end=None: read_spilled(
                    path, start, end, allowed_root=directory)))
            result = spilled_registry.execute([dict(
                id="read", name="read_source", arguments=dict(source_id="S-CT"))])[0]
            self.assertTrue(result.metadata["spilled"])
            with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_SOURCE_NOT_FULLY_READ"):
                store.review_action(
                    checkpoint_id="CP-CT", disposition="REMOVE_INDEPENDENT_CP",
                    rationale="头尾预览没有提供全文。")
            spill.max_inline_tokens = 100000
            locator = result.metadata["locator"]
            read_result = spilled_registry.execute([dict(
                id="spill", name="read_spilled", arguments=dict(path=locator))])[0]
            self.assertFalse(read_result.is_error)
            item = store.review_action(
                checkpoint_id="CP-CT", disposition="REMOVE_INDEPENDENT_CP",
                rationale="已完整读取术后回报及手术记录。")["item"]
            self.assertTrue(item["source_reads_verified"])

    def test_retained_checkpoint_action_is_optional_and_open(self):
        store, registry = self.make_store()
        registry.get("read_checkpoint_action_review_context").run("CP-CT")
        registry.get("read_source").run("S-CT")
        registry.get("read_source").run("S-OP")
        item = store.review_action(
            checkpoint_id="CP-CT", disposition="RETAIN_CP_WITH_ACTION",
            rationale="保留独立术后管理任务，不要求填写参考动作。")["item"]
        self.assertIsNone(item["candidate_action"])

        store, registry = self.make_store()
        registry.get("read_checkpoint_action_review_context").run("CP-CT")
        registry.get("read_source").run("S-CT")
        registry.get("read_source").run("S-OP")
        item = store.review_action(
            checkpoint_id="CP-CT", disposition="RETAIN_CP_WITH_ACTION",
            action_id="OTHER_SPECIFIED", action_type="BILIARY_DRAINAGE",
            target="经皮胆道引流减黄", rationale="字典外动作仅作参考。")["item"]
        self.assertEqual(item["candidate_action"]["target"], "经皮胆道引流减黄")
        self.assertEqual(item["candidate_action"]["original_action_type"],
                         "BILIARY_DRAINAGE")
        self.assertEqual(item["actor"], "case_level_action_review_llm")
        self.assertTrue(item["source_reads_verified"])

    def test_removed_diagnostic_checkpoint_reconnects_comparator(self):
        store, registry = self.make_store()
        with self.assertRaisesRegex(ToolError, "ACTION_REVIEWS_REQUIRED_BEFORE"):
            store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="尚未完成动作复审。")
        registry.get("read_checkpoint_action_review_context").run("CP-CT")
        registry.get("read_source").run("S-CT")
        registry.get("read_source").run("S-OP")
        store.review_action(checkpoint_id="CP-CT", disposition="REMOVE_INDEPENDENT_CP",
                            rationale="报告时间代理在术后，不再承担独立术前评价。")
        self.assertNotIn("CP-CT", store.retained_checkpoint_ids)
        registry.get("read_checkpoint_adjudication_context").run("CP-FIRST")
        registry.get("read_source").run("S-OLD")
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见候选状态。")
        context = registry.get("read_checkpoint_adjudication_context").run("CP-LAST")
        self.assertEqual(context["comparator_checkpoint_id"], "CP-FIRST")

    def test_management_cannot_be_removed_by_action_review(self):
        store, registry = self.make_store()
        checkpoint = store.checkpoints.by_id["CP-LAST"]
        checkpoint["review_flags"] = ["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"]
        checkpoint["primary_evaluation_eligible"] = False
        registry.get("read_checkpoint_action_review_context").run("CP-LAST")
        registry.get("read_source").run("S-NEW")
        registry.get("read_source").run("S-OP")
        with self.assertRaisesRegex(ToolError, "MANAGEMENT_ACTION_REVIEW_CANNOT_REMOVE_CP"):
            store.review_action(checkpoint_id="CP-LAST", disposition="REMOVE_INDEPENDENT_CP",
                                rationale="管理CP仍需证据演化裁决。")

    def test_only_action_review_case_still_runs_and_finishes(self):
        operation = source("S-OP", "手术时间：2026-03-02 09:00-12:00。", "2026-03-02 12:00")
        ct = source("S-CT", "术前增强CT示胰头肿瘤。", "2026-03-02 15:55")
        ct_cp = checkpoint("CP-CT", AFTER, "2026-03-02 15:55", ["S-CT"],
                           [dict(action_type="REASSESS_RESECTABILITY", target="可切除性再评估")],
                           flagged=True)
        result = adjudication.run_checkpoint_adjudication(
            ActionReviewHarness(), case_id="SYN-ACTION-ONLY", sources=[operation, ct],
            candidates=[], projection=dict(
                candidate_checkpoints=[ct_cp],
                summary=dict(task_time_bounds=dict(
                    operation_start=dict(value="2026-03-02 09:00", precision="minute"),
                    evidence=[dict(source_id="S-OP")]))))
        self.assertTrue(result["summary"]["complete"])
        self.assertEqual(result["summary"]["action_reviewed_count"], 1)
        self.assertEqual(result["summary"]["adjudicated_count"], 0)
        self.assertEqual(result["action_reviews"][0]["disposition"], "REMOVE_INDEPENDENT_CP")


if __name__ == "__main__":
    unittest.main()

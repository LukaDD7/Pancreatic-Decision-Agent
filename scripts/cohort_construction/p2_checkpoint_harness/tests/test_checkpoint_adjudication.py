"""Case-level candidate-CP adjudication tests; all records are synthetic."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages import (
    checkpoint_adjudication as adjudication,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools import (
    ToolError, ToolRegistry,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import SourceIndex


MANAGEMENT = "MANAGEMENT_REASSESSMENT"
AFTER = "AFTER_EVIDENCE_ARRIVAL"


def source(source_id, text, clock):
    return dict(source_id=source_id, text=text, title=source_id,
                clock=dict(value=clock, precision="date" if clock else "unknown"),
                clock_basis="synthetic_record_clock")


def candidate(event_id, source_id, text, quote, *, clock="2026-03-01",
              action_type="ADVANCE_RESECTION", target="胰十二指肠切除术"):
    start = text.index(quote)
    return dict(event_id=event_id, status="ACCEPTED", scope="yes",
                record_function="reassessment", clock=dict(value=clock, precision="date"),
                candidate_action=dict(action_type=action_type, target=target),
                span=dict(source_id=source_id, quote=quote, start=start,
                          end=start + len(quote)))


def checkpoint(checkpoint_id, members, *, kind=MANAGEMENT, clock="2026-03-01",
               actions=None, source_ids=None):
    if actions is None:
        actions = [deepcopy(item["candidate_action"]) for item in members]
    if source_ids is None:
        source_ids = sorted({item["span"]["source_id"] for item in members})
    return dict(checkpoint_id=checkpoint_id, kind=kind,
                clock=dict(value=clock, precision="date" if clock else "unknown"),
                source_ids=source_ids,
                member_event_ids=[item["event_id"] for item in members],
                supporting_event_ids=[], candidate_actions=deepcopy(actions),
                masked_spans=[deepcopy(item["span"]) for item in members],
                review_status="REVIEW_REQUIRED",
                candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION")


def citation(source_id, text, quote):
    start = text.index(quote)
    return dict(source_id=source_id, quote=quote, start=start,
                end=start + len(quote))


def prepare_call(registry, call):
    context = registry.get("read_checkpoint_adjudication_context").run(
        call["checkpoint_id"])
    current = context["current"]
    if current.get("source_ids"):
        registry.get("read_source").run(current["source_ids"][0])
    comparator = context["comparator"] or context["review_comparator"]
    if comparator and call.get("disposition") == "RETAIN_CP" \
            and call.get("evolution") == "NEW_EVIDENCE_ROUND":
        target_ids = comparator.get("diagnostic_target_source_ids") or []
        if target_ids:
            registry.get("read_source").run(target_ids[0])
    return context


def make_store(checkpoints, candidates, sources, *, autoread=True):
    source_index = SourceIndex(sources)
    checkpoint_index = adjudication.AdjudicationCheckpointIndex(checkpoints, candidates)
    registry = ToolRegistry(approve=lambda *args: True)
    source_index.register(registry)
    for tool in checkpoint_index.tools():
        registry.register(tool)
    store = adjudication.checkpoint_adjudication_tools(
        registry, checkpoint_index=checkpoint_index, source_index=source_index)
    if autoread:
        original = store.adjudicate

        def prepared_adjudicate(**call):
            if call.get("checkpoint_id") in store.checkpoints.by_id \
                    and store.checkpoints.by_id[call["checkpoint_id"]].get("kind") == MANAGEMENT \
                    and call.get("checkpoint_id") not in store.items:
                prepare_call(registry, call)
            return original(**call)

        store.adjudicate = prepared_adjudicate
    return store, registry


def make_report_restatement_store(report_text, current_text, *, extra_sources=()):
    member = candidate("EV-CURRENT", "S-CURRENT", current_text, "查房记录。",
                       clock="2026-03-03")
    diagnostic = checkpoint("CP-MR", [], kind=AFTER, clock="2026-03-01",
                            source_ids=["S-MR-OLD"])
    old_report = source("S-MR-OLD", report_text, "2026-03-01")
    old_report.update(kind="imaging", title="MR增强")
    current_source = source("S-CURRENT", current_text, "2026-03-03")
    current_source["kind"] = "document"
    return make_store([
        diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
        [member], [old_report, current_source, *extra_sources])


class FakeAgent:
    def __init__(self, registry, calls, finish=True):
        self.registry = registry
        self.calls = calls
        self.finish = finish

    def act(self, prompt, *, final_tool):
        self.registry.get("list_candidate_checkpoints").run(limit=200)
        for call in self.calls:
            prepare_call(self.registry, call)
            self.registry.get("adjudicate_checkpoint").run(**call)
        final = final_tool.run(note="合成裁决完成") if self.finish else dict(note="仅文字总结")
        return SimpleNamespace(stop="answered", final=final)


class FakeHarness:
    def __init__(self, calls, finish=True):
        self.calls = calls
        self.finish = finish
        self.agent_count = 0

    def session_for(self, name):
        return object()

    def new_registry(self, *, session):
        return ToolRegistry(approve=lambda *args: True)

    def agent(self, *, registry, session, system_prompt):
        self.agent_count += 1
        return FakeAgent(registry, self.calls, self.finish)


class CheckpointAdjudicationTests(unittest.TestCase):
    def test_program_validates_completion_only_after_full_agent_adjudication(self):
        text = "拟行胰十二指肠切除术。"
        member = candidate("EV-FIRST", "S-FIRST", text, text)
        projection = dict(candidate_checkpoints=[checkpoint("CP-FIRST", [member])])
        sources = [source("S-FIRST", text, "2026-03-01")]
        call = dict(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                    evolution="FIRST_VISIBLE", rationale="首见锚点。")
        result = adjudication.run_checkpoint_adjudication(
            FakeHarness([call], finish=False), case_id="SYN-FINISH",
            sources=sources, candidates=[member], projection=projection)
        self.assertTrue(result["summary"]["complete"])
        self.assertEqual(result["summary"]["stop"], "answered")
        self.assertTrue(result["summary"]["final"]["program_completion_validation"]["finished"])

        incomplete = adjudication.run_checkpoint_adjudication(
            FakeHarness([], finish=False), case_id="SYN-NO-FINISH",
            sources=sources, candidates=[member], projection=projection)
        self.assertFalse(incomplete["summary"]["complete"])
        self.assertEqual(incomplete["summary"]["stop"], "not_finished")
        self.assertEqual(incomplete["summary"]["missing_checkpoint_ids"], ["CP-FIRST"])
        self.assertNotIn("program_completion_validation", incomplete["summary"]["final"])

    def test_ct_post_eus_transfer_is_support_only_on_ct_checkpoint(self):
        ct_text = "增强CT示胰头占位，未见远处转移。"
        transfer_text = "转入胰腺外科，继续EUS+FNA并择期手术。"
        member = candidate("EV-TRANSFER", "S-TRANSFER", transfer_text, transfer_text,
                           clock="2026-03-03", action_type="EUS_EVALUATION",
                           target="EUS+FNA并择期手术")
        ct_checkpoint = checkpoint(
            "CP-CT-AFTER", [], kind=AFTER, clock="2026-03-02",
            actions=[dict(action_type="ORDER_EUS", target="EUS+FNA")],
            source_ids=["S-CT"])
        transfer_checkpoint = checkpoint("CP-TRANSFER", [member], clock="2026-03-03")
        call = dict(checkpoint_id="CP-TRANSFER", disposition="SUPPORT_ONLY",
                    evolution="SAME_ROUND_SUPPORT",
                    comparator_checkpoint_id="CP-CT-AFTER", evidence_supports=[],
                    rationale="转入记录延续CT后EUS+FNA计划，转科和择期手术措辞不是新增证据。")
        harness = FakeHarness([call])
        result = adjudication.run_checkpoint_adjudication(
            harness, case_id="SYN-EUS", sources=[
                source("S-CT", ct_text, "2026-03-02"),
                source("S-TRANSFER", transfer_text, "2026-03-03")],
            candidates=[member],
            projection=dict(candidate_checkpoints=[ct_checkpoint, transfer_checkpoint]))

        self.assertEqual(result["format"], "p2-checkpoint-adjudication-0.2.0")
        self.assertEqual(result["case_id"], "SYN-EUS")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["checkpoint_id"], "CP-TRANSFER")
        self.assertEqual(item["disposition"], "SUPPORT_ONLY")
        self.assertEqual(item["comparator_checkpoint_id"], "CP-CT-AFTER")
        self.assertEqual(item["evolution"], "SAME_ROUND_SUPPORT")
        self.assertEqual(item["actor"], "case_level_adjudication_llm")
        self.assertTrue(result["summary"]["complete"])
        self.assertEqual(result["summary"]["diagnostic_checkpoint_count"], 1)
        self.assertTrue(result["summary"]["candidate_checkpoint_list_coverage"]["complete"])
        self.assertFalse(result["summary"]["clinical_reference_created"])

    def test_unique_evidence_quote_does_not_require_manual_offsets(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "复查MRI后仍拟行胰十二指肠切除术。"
        evidence_text = "MRI示胰头占位较前缩小，未见远处转移。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-03")
        store, registry = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-SECOND", [second], clock="2026-03-03")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-03"),
                              source("S-MRI", evidence_text, "2026-03-02")])
        schema = registry.get("adjudicate_checkpoint").input_schema
        self.assertNotIn("start", schema["properties"]["evidence_supports"]["items"]["properties"])
        self.assertNotIn("end", schema["properties"]["evidence_supports"]["items"]["properties"])
        store.adjudicate(
            checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
            evolution="FIRST_VISIBLE", rationale="首个可见管理锚点。")
        quote = "未见远处转移"
        result = store.adjudicate(
            checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
            evidence_supports=[dict(source_id="S-MRI", quote=quote)],
            rationale="程序按唯一quote核验并生成区间。")
        self.assertEqual(result["item"]["evidence_supports"][0]["end"],
                         evidence_text.index(quote) + len(quote))

    def test_external_restatement_can_retain_new_evidence_with_specific_review_tags(self):
        first_text = "建议开始新辅助治疗。"
        current_text = "新辅助治疗6疗程后，外院MRI提示肿瘤体积缩小。重新评估根治手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text,
                          action_type="START_NAT", target="新辅助治疗")
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, "重新评估根治手术。",
                            clock="2026-03-03")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-CURRENT", [current], clock="2026-03-03")],
            [first, current], [source("S-FIRST", first_text, "2026-03-01"),
                               source("S-CURRENT", current_text, "2026-03-03")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见治疗方向。")
        item = store.adjudicate(
            checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND",
            evidence_source_status="EXTERNAL_RESTATEMENT",
            evidence_supports=[dict(
                source_id="S-CURRENT", quote="外院MRI提示肿瘤体积缩小")],
            rationale="当前文书明确记载治疗后外院复查变化，原报告未交付不妨碍证据演化。")["item"]
        self.assertEqual(item["review_status"], "REVIEW_REQUIRED")
        self.assertCountEqual(item["review_tags"], [
            "EXTERNAL_EVIDENCE_ORIGINAL_NOT_VERIFIED", "EVIDENCE_TIME_NOT_VERIFIED"])
        self.assertEqual(item["evidence_source_status"], "EXTERNAL_RESTATEMENT")
        self.assertEqual(item["disposition"], "RETAIN_CP")

    def test_checkpoint_list_is_compact_but_detail_retains_actions_and_spans(self):
        text = "拟行胰十二指肠切除术。"
        member = candidate("EV-FIRST", "S-FIRST", text, text)
        index = adjudication.AdjudicationCheckpointIndex(
            [checkpoint("CP-FIRST", [member])], [member])
        tools = {tool.name: tool for tool in index.tools()}
        listed = tools["list_candidate_checkpoints"].run()["checkpoints"][0]
        self.assertNotIn("candidate_actions", listed)
        self.assertNotIn("masked_spans", listed)
        detail = tools["read_candidate_checkpoint"].run("CP-FIRST")
        self.assertEqual(detail["candidate_actions"], [member["candidate_action"]])
        self.assertEqual(detail["masked_spans"], [member["span"]])

    def test_support_requires_comparator_detail_but_not_diagnostic_report(self):
        ct_text = "增强CT示胰头占位，未见远处转移。"
        transfer_text = "转入后继续EUS+FNA。"
        member = candidate("EV-TRANSFER", "S-TRANSFER", transfer_text, transfer_text,
                           clock="2026-03-03", action_type="ORDER_EUS", target="EUS+FNA")
        ct_checkpoint = checkpoint(
            "CP-CT-AFTER", [], kind=AFTER, clock="2026-03-02",
            actions=[dict(action_type="ORDER_EUS", target="EUS+FNA")],
            source_ids=["S-CT"])
        store, registry = make_store([
            ct_checkpoint, checkpoint("CP-TRANSFER", [member], clock="2026-03-03")],
            [member], [source("S-CT", ct_text, "2026-03-02"),
                       source("S-TRANSFER", transfer_text, "2026-03-03")], autoread=False)
        registry.get("read_candidate_checkpoint").run("CP-TRANSFER")
        registry.get("read_source").run("S-TRANSFER")
        call = dict(checkpoint_id="CP-TRANSFER", disposition="SUPPORT_ONLY",
                    evolution="SAME_ROUND_SUPPORT", comparator_checkpoint_id="CP-CT-AFTER",
                    rationale="转入记录延续CT后计划。")
        with self.assertRaisesRegex(ToolError, "COMPARATOR_NOT_READ"):
            store.adjudicate(**call)
        comparator = registry.get("read_candidate_checkpoint").run("CP-CT-AFTER")
        self.assertEqual(comparator["diagnostic_target_source_ids"], ["S-CT"])
        self.assertEqual(comparator["candidate_actions"][0]["target"], "EUS+FNA")
        self.assertNotIn("S-CT", store.source_index.read_source_ids)
        self.assertEqual(store.adjudicate(**call)["item"]["disposition"], "SUPPORT_ONLY")

    def test_new_evidence_still_requires_current_context_diagnostic_report(self):
        ct_text = "增强CT示胰头占位，未见远处转移。"
        current_text = "复查CT后再次评估。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-CT-AFTER", [], kind=AFTER, clock="2026-03-02",
                                source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-CT", ct_text, "2026-03-02"),
                       source("S-CURRENT", current_text, "2026-03-03")], autoread=False)
        registry.get("read_checkpoint_adjudication_context").run("CP-CURRENT")
        registry.get("read_source").run("S-CURRENT")
        call = dict(checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                    evolution="NEW_EVIDENCE_ROUND",
                    evidence_supports=[dict(source_id="S-CT", quote="未见远处转移")],
                    rationale="即使证据引用最终无效，也必须先读取诊断目标报告。")
        with self.assertRaisesRegex(
                ToolError, "COMPARATOR_DIAGNOSTIC_SOURCE_NOT_READ_IN_CURRENT_CONTEXT"):
            store.adjudicate(**call)
        registry.get("read_source").run("S-CT")
        with self.assertRaisesRegex(ToolError, "COMPARATOR_TARGET_CANNOT_BE_NEW_EVIDENCE"):
            store.adjudicate(**call)

    def test_scoped_search_uses_automatic_comparator_and_returns_local_hits(self):
        current_text = "查房后再次评估手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-01",
                                source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-CT", "第一次增强CT。", "2026-03-01"),
                       source("S-NEW", "新增强CT：未见腹膜转移。", "2026-03-02"),
                       source("S-CURRENT", current_text, "2026-03-03")])
        interval_tool = registry.get("list_checkpoint_interval_sources")
        self.assertEqual(interval_tool.input_schema["required"], ["checkpoint_id"])
        index = interval_tool.run("CP-CURRENT")
        self.assertEqual(index["comparator_checkpoint_id"], "CP-CT")
        self.assertEqual([row["source_id"] for row in index["sources"]], ["S-NEW"])
        self.assertNotIn("comparator_checkpoint_id", interval_tool.input_schema["properties"])
        tool = registry.get("search_checkpoint_sources")
        index = tool.run("CP-CURRENT")
        self.assertEqual(index["total_sources"], 1)
        self.assertNotIn("text", index["sources"][0])
        hit = tool.run("CP-CURRENT", query="腹膜", context_chars=20)["hits"][0]
        self.assertEqual(hit["source_id"], "S-NEW")
        self.assertIn("未见腹膜转移", hit["text"])
        self.assertEqual(index["comparator_checkpoint_id"], "CP-CT")
        current = tool.run("CP-CURRENT", scope="CURRENT")
        self.assertEqual([row["source_id"] for row in current["sources"]], ["S-CURRENT"])

    def test_scoped_search_does_not_mark_hidden_cp_details_as_read(self):
        current_text = "新CT后再次评估。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-01",
                                source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-CT", "旧CT。", "2026-03-01"),
                       source("S-NEW", "新CT：未见转移。", "2026-03-02"),
                       source("S-CURRENT", current_text, "2026-03-03")], autoread=False)
        search = registry.get("search_checkpoint_sources")
        result = search.run("CP-CURRENT", scope="COMPARATOR")
        self.assertNotIn("current", result)
        self.assertNotIn("comparator", result)
        registry.get("read_source").run("S-CURRENT")
        registry.get("read_source").run("S-CT")
        call = dict(checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                    evolution="NEW_EVIDENCE_ROUND",
                    evidence_supports=[dict(source_id="S-NEW", quote="未见转移")],
                    rationale="独立新报告构成新证据。")
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_NOT_READ"):
            store.adjudicate(**call)
        self.assertEqual(store.checkpoints.read_checkpoint_ids, set())
        prepare_call(registry, call)
        self.assertEqual(store.adjudicate(**call)["item"]["disposition"], "RETAIN_CP")

    def test_context_and_adjudication_cannot_share_a_tool_batch(self):
        current_text = "新CT后再次评估。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-01",
                                source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-CT", "旧CT。", "2026-03-01"),
                       source("S-NEW", "新CT：未见转移。", "2026-03-02"),
                       source("S-CURRENT", current_text, "2026-03-03")], autoread=False)
        registry.get("search_checkpoint_sources").run(
            "CP-CURRENT", scope="COMPARATOR")
        registry.get("read_source").run("S-CURRENT")
        registry.get("read_source").run("S-CT")
        results = registry.execute([
            dict(id="read", name="read_checkpoint_adjudication_context",
                 arguments=dict(checkpoint_id="CP-CURRENT")),
            dict(id="write", name="adjudicate_checkpoint", arguments=dict(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(source_id="S-NEW", quote="未见转移")],
                rationale="独立新报告构成新证据。"))])
        self.assertFalse(results[0].is_error)
        self.assertTrue(results[1].is_error)
        self.assertIn("TOOL_REQUIRES_RESULTS_FROM_PREVIOUS_BATCH", results[1].content)
        self.assertEqual(store.items, {})
        retry = registry.execute([dict(id="retry", name="adjudicate_checkpoint", arguments=dict(
            checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND",
            evidence_supports=[dict(source_id="S-NEW", quote="未见转移")],
            rationale="独立新报告构成新证据。"))])
        self.assertFalse(retry[0].is_error)
        self.assertEqual(store.items["CP-CURRENT"]["disposition"], "RETAIN_CP")

    def test_review_cannot_use_internal_context_lookup_as_detail_read(self):
        current_text = "同日CT后拟行手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text)
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member])], [member],
            [source("S-CT", "CT报告。", "2026-03-01"),
             source("S-CURRENT", current_text, "2026-03-01")], autoread=False)
        registry.get("read_source").run("S-CURRENT")
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_NOT_READ"):
            store.adjudicate(checkpoint_id="CP-CURRENT", disposition="REVIEW_REQUIRED",
                             evolution="UNCERTAIN", rationale="同刻关系待核。")
        self.assertEqual(store.checkpoints.read_checkpoint_ids, set())

    def test_scoped_search_caps_constructed_hits_for_frequent_query(self):
        current_text = "再次评估。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-01",
                                source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-CT", "旧CT。", "2026-03-01"),
                       source("S-NEW", "。" * 10000, "2026-03-02"),
                       source("S-CURRENT", current_text, "2026-03-03")])
        result = registry.get("search_checkpoint_sources").run(
            "CP-CURRENT", query="。", limit=1)
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["hits"]), 1)

    def test_checkpoint_adjudication_skill_is_discoverable(self):
        from pathlib import Path
        from scripts.cohort_construction.p2_checkpoint_harness.skills import SkillRegistry

        root = Path(adjudication.__file__).resolve().parents[1] / "skills"
        skill = SkillRegistry([root]).load("checkpoint-adjudication")
        self.assertIn("最少必要读取", skill.body)
        self.assertIn("search_checkpoint_sources", skill.body)

    def test_management_comparator_reuses_prior_current_detail_read(self):
        first_text = "拟行手术。"
        second_text = "MRI后拟行手术。"
        evidence_text = "MRI未见远处转移。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-03")
        store, registry = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-SECOND", [second], clock="2026-03-03")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-03"),
                              source("S-MRI", evidence_text, "2026-03-02")], autoread=False)
        registry.get("read_candidate_checkpoint").run("CP-FIRST")
        registry.get("read_source").run("S-FIRST")
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        registry.get("read_candidate_checkpoint").run("CP-SECOND")
        registry.get("read_source").run("S-SECOND")
        call = dict(checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                    evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                    evidence_supports=[dict(source_id="S-MRI", quote="未见远处转移")],
                    rationale="MRI是新增患者证据。")
        prepare_call(registry, call)
        self.assertEqual(store.adjudicate(**call)["item"]["evolution"],
                         "NEW_EVIDENCE_ROUND")

    def test_support_uses_automatic_comparator_without_interval_review(self):
        first_text = "拟行手术。"
        second_text = "再次讨论拟行手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-03")
        ct = source("S-CT", "增强CT未见远处转移。", "2026-03-02")
        ct["kind"] = "imaging"
        store, registry = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-SECOND", [second], clock="2026-03-03")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-03"), ct],
            autoread=False)
        prepare_call(registry, dict(checkpoint_id="CP-FIRST"))
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        context = prepare_call(registry, dict(checkpoint_id="CP-SECOND"))
        self.assertEqual(context["comparator_checkpoint_id"], "CP-FIRST")
        self.assertFalse(context["interval_review_required"])
        self.assertNotIn("review_checkpoint_interval_sources", registry.names())
        item = store.adjudicate(
            checkpoint_id="CP-SECOND", disposition="SUPPORT_ONLY",
            evolution="REPEATED_PLAN", comparator_checkpoint_id="CP-DOES-NOT-CONTROL-RESULT",
            rationale="复述既定计划，程序自动确定比较对象。")["item"]
        self.assertEqual(item["comparator_checkpoint_id"], "CP-FIRST")
        self.assertNotIn("interval_evidence_review", item)
        page = registry.get("list_checkpoint_interval_sources").run(
            checkpoint_id="CP-SECOND", comparator_checkpoint_id="CP-FIRST")
        self.assertEqual(page["total"], 1)
        self.assertNotIn("text", page["sources"][0])

    def test_current_source_is_required_and_interval_evidence_can_be_cited_directly(self):
        first_text = "拟行手术。"
        second_text = "再次讨论拟行手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-03")
        ct = source("S-CT", "增强CT显示新发病灶。", "2026-03-02")
        ct["kind"] = "imaging"
        store, registry = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-SECOND", [second], clock="2026-03-03")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-03"), ct],
            autoread=False)
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_NOT_READ"):
            store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="首见锚点。")
        registry.get("read_checkpoint_adjudication_context").run("CP-FIRST")
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_SOURCE_NOT_READ"):
            store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="首见锚点。")
        registry.get("read_source").run("S-FIRST")
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        prepare_call(registry, dict(checkpoint_id="CP-SECOND"))
        retained = store.adjudicate(
            checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-IGNORED",
            evidence_supports=[dict(source_id="S-CT", quote="新发病灶")],
            rationale="按需核对的区间CT提供新发病灶证据。")["item"]
        self.assertEqual(retained["comparator_checkpoint_id"], "CP-FIRST")
        self.assertEqual(retained["evidence_supports"][0]["source_id"], "S-CT")

    def test_same_action_with_new_patient_evidence_is_retained(self):
        first_text = "讨论后拟行胰十二指肠切除术。"
        second_text = "复查MRI后仍拟行胰十二指肠切除术。"
        evidence_text = "MRI示胰头占位较前缩小，未见远处转移。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-03")
        sources = [source("S-FIRST", first_text, "2026-03-01"),
                   source("S-SECOND", second_text, "2026-03-03"),
                   source("S-MRI", evidence_text, "2026-03-02")]
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-SECOND", [second], clock="2026-03-03")],
            [first, second], sources)
        store.adjudicate(
            checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
            evolution="FIRST_VISIBLE", rationale="首个可见管理锚点。")
        result = store.adjudicate(
            checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
            evidence_supports=[citation("S-MRI", evidence_text, "未见远处转移")],
            rationale="相同手术动作前出现新的MRI分期证据。")
        self.assertEqual(result["item"]["disposition"], "RETAIN_CP")
        self.assertEqual(result["item"]["evidence_supports"][0]["source_id"], "S-MRI")
        self.assertEqual(result["item"]["evidence_supports"][0]["quote"], "未见远处转移")
        store.finish_tool().run()

    def test_different_action_without_new_evidence_cannot_be_retained(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "改为先开始新辅助化疗。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-DIFFERENT", "S-DIFFERENT", second_text, second_text,
                           clock="2026-03-02", action_type="START_NAT", target="新辅助化疗")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-DIFFERENT", [second], clock="2026-03-02")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-DIFFERENT", second_text, "2026-03-02")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "REQUIRES_EVIDENCE_SUPPORT"):
            store.adjudicate(
                checkpoint_id="CP-DIFFERENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[], rationale="仅动作改变，没有新增患者证据。")
        with self.assertRaisesRegex(ToolError, "SUPPORT_ONLY_EVOLUTION_INVALID"):
            store.adjudicate(
                checkpoint_id="CP-DIFFERENT", disposition="SUPPORT_ONLY",
                evolution="DIFFERENT_TASK", comparator_checkpoint_id="CP-FIRST",
                rationale="不同任务不能作为同轮支持归并。")

    def test_future_exam_plan_overlapping_current_action_requires_review(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "拟下周复查增强CT后再评估手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-FUTURE-PLAN", "S-PLAN", second_text, second_text,
                           clock="2026-03-02", action_type="ORDER_DIAGNOSTIC_TEST",
                           target="下周复查增强CT")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-FUTURE-PLAN", [second], clock="2026-03-02")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-PLAN", second_text, "2026-03-02")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_OVERLAPS_CURRENT_MEMBER"):
            store.adjudicate(
                checkpoint_id="CP-FUTURE-PLAN", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[citation("S-PLAN", second_text, "拟下周复查增强CT")],
                rationale="错误地把未来检查计划冒充已到达结果。")
        self.assertNotIn("CP-FUTURE-PLAN", store.items)
        item = store.adjudicate(
            checkpoint_id="CP-FUTURE-PLAN", disposition="REVIEW_REQUIRED",
            evolution="UNCERTAIN", comparator_checkpoint_id="CP-FIRST",
            evidence_supports=[], rationale="未来计划与结果关系需人工复核。")["item"]
        self.assertEqual(item["review_status"], "REVIEW_REQUIRED")
        self.assertEqual(item["evidence_supports"], [])

    def test_supporting_management_plan_cannot_support_new_evidence_round(self):
        first_text = "拟行胰十二指肠切除术。"
        current_text = "再次讨论根治性切除路径。"
        plan_text = "拟开始新辅助化疗。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                            clock="2026-03-02")
        plan = candidate("EV-PLAN", "S-PLAN", plan_text, plan_text,
                         clock="2026-03-02", action_type="START_NAT", target="新辅助化疗")
        current_checkpoint = checkpoint("CP-CURRENT", [current], clock="2026-03-02")
        current_checkpoint["supporting_event_ids"] = ["EV-PLAN"]
        current_checkpoint["source_ids"].append("S-PLAN")
        current_checkpoint["masked_spans"].append(deepcopy(plan["span"]))
        store, _ = make_store(
            [checkpoint("CP-FIRST", [first]), current_checkpoint],
            [first, current, plan],
            [source("S-FIRST", first_text, "2026-03-01"),
             source("S-CURRENT", current_text, "2026-03-02"),
             source("S-PLAN", plan_text, "2026-03-02")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_OVERLAPS_CURRENT_MEMBER"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[citation("S-PLAN", plan_text, plan_text)],
                rationale="附属管理计划不能冒充新患者证据。")

    def test_current_action_quote_cannot_support_new_evidence_round(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "再次讨论后仍拟行胰十二指肠切除术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-02")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-SECOND", [second], clock="2026-03-02")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-02")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_OVERLAPS_CURRENT_MEMBER"):
            store.adjudicate(
                checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[citation("S-SECOND", second_text, "仍拟行胰十二指肠切除术")],
                rationale="错误地以当前动作句充当证据。")

    def test_first_visible_anchor_needs_no_comparator_or_evidence(self):
        text = "初次讨论拟行胰十二指肠切除术。"
        member = candidate("EV-FIRST", "S-FIRST", text, text)
        store, _ = make_store([checkpoint("CP-FIRST", [member])], [member],
                              [source("S-FIRST", text, "2026-03-01")])
        item = store.adjudicate(
            checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
            evolution="FIRST_VISIBLE", rationale="全病例首个可见管理锚点。")["item"]
        self.assertIsNone(item["comparator_checkpoint_id"])
        self.assertEqual(item["evidence_supports"], [])
        self.assertEqual(item["review_status"], "CANDIDATE_ADJUDICATED")
        self.assertTrue(store.finish_tool().run()["finished"])

    def test_future_and_unknown_clock_evidence_are_not_silently_accepted(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "复查后仍拟行胰十二指肠切除术。"
        future_text = "后续MRI未见远处转移。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-02")
        checkpoints = [checkpoint("CP-FIRST", [first]),
                       checkpoint("CP-SECOND", [second], clock="2026-03-02")]
        candidates = [first, second]
        base_sources = [source("S-FIRST", first_text, "2026-03-01"),
                        source("S-SECOND", second_text, "2026-03-02")]
        store, _ = make_store(checkpoints, candidates, base_sources + [
            source("S-FUTURE", future_text, "2026-03-03")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_CLOCK_AFTER_CHECKPOINT"):
            store.adjudicate(
                checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[citation("S-FUTURE", future_text, "未见远处转移")],
                rationale="未来报告不能支持当前CP。")

        store, _ = make_store(checkpoints, candidates, base_sources + [
            source("S-UNKNOWN", future_text, None)])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_CLOCK_UNKNOWN_REQUIRES_REVIEW"):
            store.adjudicate(
                checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[citation("S-UNKNOWN", future_text, "未见远处转移")],
                rationale="证据可见时间未知。")
        item = store.adjudicate(
            checkpoint_id="CP-SECOND", disposition="REVIEW_REQUIRED",
            evolution="UNCERTAIN", comparator_checkpoint_id="CP-FIRST",
            evidence_supports=[citation("S-UNKNOWN", future_text, "未见远处转移")],
            rationale="证据原文存在，但可见clock未知，保留人工复核。")["item"]
        self.assertEqual(item["review_status"], "REVIEW_REQUIRED")

    def test_support_requires_strictly_earlier_program_comparator(self):
        first_text = "拟行EUS+FNA。"
        second_text = "转入后继续EUS+FNA。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text,
                          action_type="ORDER_EUS", target="EUS+FNA")
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-02", action_type="ORDER_EUS", target="EUS+FNA")
        for comparator_clock in ("2026-03-02", None, "2026-03-03"):
            with self.subTest(comparator_clock=comparator_clock):
                store, registry = make_store([
                    checkpoint("CP-FIRST", [first], clock=comparator_clock),
                    checkpoint("CP-SECOND", [second], clock="2026-03-02")],
                    [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                                      source("S-SECOND", second_text, "2026-03-02")])
                expected = ("UNORDERED_CANDIDATE_STATE_REQUIRES_REVIEW"
                            if comparator_clock in {"2026-03-02", None}
                            else "SUPPORT_ONLY_REQUIRES_COMPARATOR")
                with self.assertRaisesRegex(ToolError, expected):
                    store.adjudicate(
                        checkpoint_id="CP-SECOND", disposition="SUPPORT_ONLY",
                        evolution="SAME_ROUND_SUPPORT", comparator_checkpoint_id="CP-FIRST",
                        rationale="手工ID不能绕过程序确定的时间关系。")
                item = store.adjudicate(
                    checkpoint_id="CP-SECOND", disposition="REVIEW_REQUIRED",
                    evolution="UNCERTAIN", comparator_checkpoint_id="CP-SECOND",
                    rationale="没有严格更早状态，保留待核。")["item"]
                self.assertIsNone(item["comparator_checkpoint_id"])
                self.assertIsNotNone(registry.get("read_checkpoint_adjudication_context"))

    def test_finish_rejects_missing_unknown_and_duplicate_adjudications(self):
        first_text = "拟行手术。"
        second_text = "再次讨论拟行手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-02")
        checkpoints = [checkpoint("CP-FIRST", [first]),
                       checkpoint("CP-SECOND", [second], clock="2026-03-02")]
        candidates = [first, second]
        evidence_text = "复查MRI示未见远处转移。"
        sources = [source("S-FIRST", first_text, "2026-03-01"),
                   source("S-SECOND", second_text, "2026-03-02"),
                   source("S-MRI", evidence_text, "2026-03-02")]
        first_call = dict(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                          evolution="FIRST_VISIBLE", rationale="首见锚点。")

        store, _ = make_store(checkpoints, candidates, sources)
        store.adjudicate(**first_call)
        with self.assertRaisesRegex(ToolError, "CP-SECOND"):
            store.finish_tool().run()

        store, _ = make_store(checkpoints, candidates, sources)
        with self.assertRaisesRegex(ToolError, "UNKNOWN_CHECKPOINT_ID"):
            store.adjudicate(checkpoint_id="CP-UNKNOWN", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="未知ID。")
        store.adjudicate(**first_call)
        store.adjudicate(checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                         evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                         evidence_supports=[citation("S-MRI", evidence_text, "未见远处转移")],
                         rationale="合成完整性检查。")
        with self.assertRaisesRegex(ToolError, "UNKNOWN_CHECKPOINT_ID"):
            store.finish_tool().run()

        store, _ = make_store(checkpoints, candidates, sources)
        store.adjudicate(**first_call)
        with self.assertRaisesRegex(ToolError, "DUPLICATE_CHECKPOINT_ADJUDICATION"):
            store.adjudicate(**first_call)
        with self.assertRaisesRegex(ToolError, "DUPLICATE_CHECKPOINT_ADJUDICATION"):
            store.finish_tool().run()

    def test_diagnostic_checkpoints_are_excluded_from_required_coverage(self):
        diagnostic = checkpoint("CP-DIAGNOSTIC", [], kind=AFTER, source_ids=["S-CT"])
        store, _ = make_store([diagnostic], [], [source("S-CT", "CT报告。", "2026-03-02")])
        with self.assertRaisesRegex(ToolError, "DIAGNOSTIC_CHECKPOINT_NOT_ADJUDICATED"):
            store.adjudicate(checkpoint_id="CP-DIAGNOSTIC", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="诊断CP不应提交。")
        store, _ = make_store([diagnostic], [], [source("S-CT", "CT报告。", "2026-03-02")])
        self.assertTrue(store.finish_tool().run()["finished"])
        result = store.result(case_id="SYN-DIAGNOSTIC", stop="answered")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["summary"]["management_checkpoint_count"], 0)
        self.assertTrue(result["summary"]["complete"])

    def test_baseline_evidence_before_comparator_is_not_a_new_evidence_round(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "复查后仍拟行胰十二指肠切除术。"
        baseline_text = "既往增强CT示未见远处转移。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-02")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-SECOND", [second], clock="2026-03-02")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-02"),
                              source("S-BASELINE", baseline_text, "2026-02-28")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_NOT_NEW_AFTER_COMPARATOR"):
            store.adjudicate(
                checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[citation("S-BASELINE", baseline_text, "未见远处转移")],
                rationale="错误地拿比较CP之前的基线患者证据充当新增证据。")

    def test_comparator_cannot_skip_latest_retained_diagnostic_state(self):
        first_text = "拟行胰十二指肠切除术。"
        second_text = "再次讨论拟行胰十二指肠切除术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-02",
                                source_ids=["S-CT"])
        store, registry = make_store([
            checkpoint("CP-FIRST", [first]), diagnostic,
            checkpoint("CP-SECOND", [second], clock="2026-03-03")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-CT", "增强CT未见远处转移。", "2026-03-02"),
                              source("S-SECOND", second_text, "2026-03-03")])
        self.assertNotIn("comparator_checkpoint_id",
                         registry.get("adjudicate_checkpoint").input_schema["properties"])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        context = registry.get("read_checkpoint_adjudication_context").run("CP-SECOND")
        self.assertEqual(context["comparator_checkpoint_id"], "CP-CT")
        with self.assertRaisesRegex(ToolError, "COMPARATOR_TARGET_CANNOT_BE_NEW_EVIDENCE"):
            store.adjudicate(
                checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-CT",
                evidence_supports=[citation("S-CT", "增强CT未见远处转移。", "未见远处转移")],
                rationale="CT报告属于比较状态本身，不是其后新增证据。")

    def test_first_visible_cannot_ignore_existing_diagnostic_checkpoint(self):
        text = "CT后拟行胰十二指肠切除术。"
        member = candidate("EV-AFTER", "S-AFTER", text, text, clock="2026-03-03")
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-02",
                                source_ids=["S-CT"])
        store, _ = make_store([diagnostic, checkpoint("CP-AFTER", [member], clock="2026-03-03")],
                              [member], [source("S-CT", "CT报告。", "2026-03-02"),
                                         source("S-AFTER", text, "2026-03-03")])
        with self.assertRaisesRegex(ToolError, "FIRST_VISIBLE_REQUIRES_NO_PRIOR_RETAINED_STATE"):
            store.adjudicate(checkpoint_id="CP-AFTER", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="错误忽略已有诊断状态。")

    def test_same_clock_candidates_cannot_each_claim_first_visible(self):
        first_text = "拟行手术。"
        second_text = "再次讨论拟行手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text)
        store, _ = make_store([checkpoint("CP-FIRST", [first]),
                               checkpoint("CP-SECOND", [second])], [first, second], [
                                   source("S-FIRST", first_text, "2026-03-01"),
                                   source("S-SECOND", second_text, "2026-03-01")])
        for checkpoint_id in ("CP-FIRST", "CP-SECOND"):
            with self.subTest(checkpoint_id=checkpoint_id):
                with self.assertRaisesRegex(ToolError, "UNORDERED_CANDIDATE_STATE_REQUIRES_REVIEW"):
                    store.adjudicate(checkpoint_id=checkpoint_id, disposition="RETAIN_CP",
                                     evolution="FIRST_VISIBLE", rationale="同刻候选不能忽略。")

    def test_non_actionable_support_can_be_cited_as_new_evidence(self):
        first_text = "拟行胰十二指肠切除术。"
        evidence_text = "新辅助化疗6疗程，肿瘤体积缩小。"
        decision_text = "排除禁忌后限期手术。"
        current_text = evidence_text + decision_text
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        evidence = candidate("EV-EVIDENCE", "S-CURRENT", current_text, evidence_text,
                             clock="2026-03-03")
        evidence["candidate_action"] = None
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, decision_text,
                            clock="2026-03-03")
        current_checkpoint = checkpoint("CP-CURRENT", [current], clock="2026-03-03")
        current_checkpoint["supporting_event_ids"] = ["EV-EVIDENCE"]
        store, _ = make_store(
            [checkpoint("CP-FIRST", [first]), current_checkpoint],
            [first, evidence, current],
            [source("S-FIRST", first_text, "2026-03-01"),
             source("S-CURRENT", current_text, "2026-03-03")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        item = store.adjudicate(
            checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
            evidence_supports=[citation("S-CURRENT", current_text, "肿瘤体积缩小")],
            rationale="疗效复述提供新增患者状态，具体来源仍待核。")["item"]
        self.assertEqual(item["disposition"], "RETAIN_CP")
        self.assertEqual(item["evolution"], "NEW_EVIDENCE_ROUND")

    def test_other_checkpoint_action_cannot_be_new_patient_evidence(self):
        first_text = "拟行胰十二指肠切除术。"
        middle_text = "经讨论改为新辅助化疗。"
        current_text = "再次评估是否手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        middle = candidate("EV-MIDDLE", "S-MIDDLE", middle_text, middle_text,
                           clock="2026-03-02", action_type="START_NAT", target="新辅助化疗")
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                            clock="2026-03-03")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-MIDDLE", [middle], clock="2026-03-02"),
            checkpoint("CP-CURRENT", [current], clock="2026-03-03")],
            [first, middle, current], [source("S-FIRST", first_text, "2026-03-01"),
                                       source("S-MIDDLE", middle_text, "2026-03-02"),
                                       source("S-CURRENT", current_text, "2026-03-03")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        store.adjudicate(checkpoint_id="CP-MIDDLE", disposition="SUPPORT_ONLY",
                         evolution="REPEATED_PLAN", comparator_checkpoint_id="CP-FIRST",
                         rationale="仅计划变化，归并为首见状态支持记录。")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_OVERLAPS_MANAGEMENT_CANDIDATE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-FIRST",
                evidence_supports=[citation("S-MIDDLE", middle_text, "改为新辅助化疗")],
                rationale="错误地以其他文书的动作句作为患者证据。")

    def test_two_same_clock_management_reviews_do_not_form_comparator_cycle(self):
        first_text = "拟行手术。"
        second_text = "改为新辅助治疗。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           action_type="START_NAT", target="新辅助治疗")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]), checkpoint("CP-SECOND", [second])],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-01")])
        for checkpoint_id in ("CP-FIRST", "CP-SECOND"):
            item = store.adjudicate(
                checkpoint_id=checkpoint_id, disposition="REVIEW_REQUIRED",
                evolution="UNCERTAIN", rationale="同刻管理候选不可定序。")["item"]
            self.assertIsNone(item["comparator_checkpoint_id"])

    def test_mixed_precision_latest_states_require_review_without_arbitrary_comparator(self):
        text = "再次评估手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", text, text, clock="2026-03-03")
        early = checkpoint("CP-DATE", [], kind=AFTER, clock="2026-03-01",
                           source_ids=["S-DATE"])
        late = checkpoint("CP-MINUTE", [], kind=AFTER, clock="2026-03-01 12:00",
                          source_ids=["S-MINUTE"])
        late["clock"]["precision"] = "minute"
        store, registry = make_store([
            early, late, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-DATE", "CT报告。", "2026-03-01"),
                       source("S-MINUTE", "MR报告。", "2026-03-01 12:00"),
                       source("S-CURRENT", text, "2026-03-03")])
        context = registry.get("read_checkpoint_adjudication_context").run("CP-CURRENT")
        self.assertIsNone(context["comparator_checkpoint_id"])
        self.assertEqual(set(context["parallel_latest_checkpoint_ids"]),
                         {"CP-DATE", "CP-MINUTE"})
        with self.assertRaisesRegex(ToolError, "PARALLEL_LATEST_STATES_REQUIRE_REVIEW"):
            store.adjudicate(checkpoint_id="CP-CURRENT", disposition="SUPPORT_ONLY",
                             evolution="SAME_ROUND_SUPPORT", rationale="不得任选一个。")
        item = store.adjudicate(checkpoint_id="CP-CURRENT", disposition="REVIEW_REQUIRED",
                                evolution="UNCERTAIN", rationale="并列最近状态待核。")["item"]
        self.assertIsNone(item["comparator_checkpoint_id"])

    def test_unknown_diagnostic_clock_blocks_first_visible(self):
        text = "拟行手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", text, text, clock="2026-03-03")
        diagnostic = checkpoint("CP-UNKNOWN", [], kind=AFTER, clock=None,
                                source_ids=["S-UNKNOWN"])
        store, _ = make_store([diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
                              [member], [source("S-UNKNOWN", "CT报告。", None),
                                         source("S-CURRENT", text, "2026-03-03")])
        with self.assertRaisesRegex(ToolError, "UNORDERED_CANDIDATE_STATE_REQUIRES_REVIEW"):
            store.adjudicate(checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="不能忽略未知时间状态。")

    def test_review_requires_current_detail_source_but_not_diagnostic_report(self):
        text = "CT后拟行手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", text, text)
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member])], [member],
            [source("S-CT", "增强CT报告。", "2026-03-01"),
             source("S-CURRENT", text, "2026-03-01")], autoread=False)
        call = dict(checkpoint_id="CP-CURRENT", disposition="REVIEW_REQUIRED",
                    evolution="UNCERTAIN", rationale="同刻证据关系待核。")
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_NOT_READ"):
            store.adjudicate(**call)
        registry.get("read_checkpoint_adjudication_context").run("CP-CURRENT")
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_SOURCE_NOT_READ"):
            store.adjudicate(**call)
        registry.get("read_source").run("S-CURRENT", start=3)
        with self.assertRaisesRegex(ToolError, "CURRENT_CHECKPOINT_SOURCE_NOT_FULLY_READ"):
            store.adjudicate(**call)
        registry.get("read_source").run("S-CURRENT")
        self.assertNotIn("S-CT", store.source_index.read_source_ids)
        item = store.adjudicate(**call)["item"]
        self.assertEqual(item["comparator_checkpoint_id"], "CP-CT")

    def test_diagnostic_target_is_kept_when_report_contains_management_span(self):
        text = "增强CT未见转移。CT后拟行手术。"
        ct_text = "增强CT未见转移。"
        member = candidate("EV-CURRENT", "S-CT", text, "CT后拟行手术。", clock="2026-03-03")
        diagnostic = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-02",
                                source_ids=["S-CT"])
        store, registry = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-CT", text, "2026-03-02")])
        context = registry.get("read_checkpoint_adjudication_context").run("CP-CURRENT")
        self.assertEqual(context["comparator"]["diagnostic_target_source_ids"], ["S-CT"])
        registry.get("read_source").run("S-CT")
        item = store.adjudicate(checkpoint_id="CP-CURRENT", disposition="SUPPORT_ONLY",
                                evolution="SAME_ROUND_SUPPORT", rationale="同报告含管理句。")["item"]
        self.assertEqual(item["comparator_checkpoint_id"], "CP-CT")

    def test_comparator_target_cannot_support_another_new_evidence_round(self):
        mr_text = "胰腺癌（胰头部，23X16mm）。\n远处转移：未见。"
        current_text = "MR后安排EUS+FNA。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-MR", [], kind=AFTER, clock="2026-03-01",
                                source_ids=["S-MR"])
        store, _ = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [source("S-MR", mr_text, "2026-03-02"),
                       source("S-CURRENT", current_text, "2026-03-03")])
        with self.assertRaisesRegex(ToolError, "COMPARATOR_TARGET_CANNOT_BE_NEW_EVIDENCE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(source_id="S-MR", quote="远处转移：未见。")],
                rationale="错误复用comparator已有的MR。")

    def test_dated_report_restatement_cannot_be_retimestamped_by_current_document(self):
        first_text = "拟完善检查。"
        current_text = "查房记录。2026-03-01 MR增强,肿块评估：胰头部可见肿块，大小约23X16mm。"
        mr_text = "[original_findings]\n肿块评估：胰头部可见肿块，大小约23X16mm。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, "查房记录。",
                            clock="2026-03-03")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]), checkpoint("CP-CURRENT", [current], clock="2026-03-03")],
            [first, current], [source("S-FIRST", first_text, "2026-03-01"),
                               source("S-MR", mr_text, "2026-03-01"),
                               source("S-CURRENT", current_text, "2026-03-03")])
        store.source_index.by_id["S-CURRENT"]["kind"] = "document"
        store.source_index.by_id["S-MR"]["kind"] = "imaging"
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(
                    source_id="S-CURRENT",
                    quote="2026-03-01 MR增强,肿块评估：胰头部可见肿块，大小约23X16mm。")],
                rationale="错误地把旧MR复述当成新证据。")

    def test_comparator_report_copy_with_same_hash_cannot_bypass_identity_guard(self):
        report_text = "MR增强示胰头肿块，未见远处转移。"
        current_text = "再次评估后安排EUS+FNA。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        diagnostic = checkpoint("CP-MR", [], kind=AFTER, clock="2026-03-01",
                                source_ids=["S-MR-A"])
        original = source("S-MR-A", report_text, "2026-03-01")
        duplicate = source("S-MR-B", report_text, "2026-03-02")
        original["source_sha256"] = duplicate["source_sha256"] = "same-report-hash"
        store, _ = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [original, duplicate,
                       source("S-CURRENT", current_text, "2026-03-03")])
        with self.assertRaisesRegex(ToolError, "COMPARATOR_TARGET_CANNOT_BE_NEW_EVIDENCE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(source_id="S-MR-B", quote="未见远处转移")],
                rationale="换source_id不能绕过同一报告身份封禁。")

    def test_same_hash_with_different_exam_date_can_be_new_evidence(self):
        report_text = "MR增强示胰头肿块，未见远处转移。"
        current_text = "再次评估后安排EUS+FNA。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-04")
        diagnostic = checkpoint("CP-MR", [], kind=AFTER, clock="2026-03-02",
                                source_ids=["S-MR-OLD"])
        old_mr = source("S-MR-OLD", report_text, "2026-03-02")
        new_mr = source("S-MR-NEW", report_text, "2026-03-03")
        old_mr["clocks"] = dict(exam_time=dict(value="2026-03-01", precision="date"),
                                report_time=dict(value="2026-03-02", precision="date"))
        new_mr["clocks"] = dict(exam_time=dict(value="2026-03-02", precision="date"),
                                report_time=dict(value="2026-03-03", precision="date"))
        old_mr["source_sha256"] = new_mr["source_sha256"] = "same-text-not-same-exam"
        store, _ = make_store([
            diagnostic, checkpoint("CP-CURRENT", [member], clock="2026-03-04")],
            [member], [old_mr, new_mr,
                       source("S-CURRENT", current_text, "2026-03-04")])
        item = store.adjudicate(
            checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND",
            evidence_supports=[dict(source_id="S-MR-NEW", quote="未见远处转移")],
            rationale="正文哈希相同但检查日期不同的新报告可以构成新增证据。")["item"]
        self.assertEqual(item["evidence_supports"][0]["source_id"], "S-MR-NEW")

    def test_unpadded_recitation_date_cannot_bypass_original_report_guard(self):
        first_text = "拟完善检查。"
        current_text = "查房记录。影像回顾：2026-3-1 MR增强,肿块评估：胰头部可见肿块，大小约23X16mm。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text,
                          clock="2026-03-02")
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, "查房记录。",
                            clock="2026-03-03")
        old_mr = source("S-MR-OLD", "肿块评估：胰头部可见肿块，大小约23X16mm。",
                        "2026-03-01")
        old_mr["kind"] = "imaging"
        current_source = source("S-CURRENT", current_text, "2026-03-03")
        current_source["kind"] = "document"
        store, _ = make_store([
            checkpoint("CP-FIRST", [first], clock="2026-03-02"),
            checkpoint("CP-CURRENT", [current], clock="2026-03-03")],
            [first, current], [source("S-FIRST", first_text, "2026-03-02"),
                               old_mr, current_source])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(
                    source_id="S-CURRENT",
                    quote="影像回顾：2026-3-1 MR增强,肿块评估：胰头部可见肿块，大小约23X16mm。")],
                rationale="未补零日期不能把旧报告重新定时。")

    def test_dated_report_restatement_is_detected_after_a_text_prefix(self):
        first_text = "拟完善检查。"
        current_text = "查房记录。影像回顾：2026-03-01 MR增强,肿块评估：胰头部可见肿块，大小约23X16mm。"
        mr_text = "[original_findings]\n肿块评估：胰头部可见肿块，大小约23X16mm。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, "查房记录。",
                            clock="2026-03-03")
        mr = source("S-MR", mr_text, "2026-03-01")
        mr["kind"] = "imaging"
        current_source = source("S-CURRENT", current_text, "2026-03-03")
        current_source["kind"] = "document"
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-CURRENT", [current], clock="2026-03-03")],
            [first, current], [source("S-FIRST", first_text, "2026-03-01"),
                               mr, current_source])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(
                    source_id="S-CURRENT",
                    quote="影像回顾：2026-03-01 MR增强,肿块评估：胰头部可见肿块，大小约23X16mm。")],
                rationale="日期不在引文开头也不能绕过旧复述识别。")

    def test_undated_excerpt_of_comparator_report_cannot_be_retimestamped(self):
        findings = "肿块评估：胰头部可见肿块，大小约23X16mm。"
        quotes = [findings, "影像回顾：" + findings, "MR增强：" + findings,
                  "2026/03/01 MR增强：" + findings,
                  "2026-03-03 查房：2026-03-01 MR增强：" + findings]
        for quote in quotes:
            with self.subTest(quote=quote):
                store, _ = make_report_restatement_store(
                    "既往CT示胰头肿块。本次MR增强：" + findings,
                    "查房记录。" + quote)
                with self.assertRaisesRegex(
                        ToolError, "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE"):
                    store.adjudicate(
                        checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                        evolution="NEW_EVIDENCE_ROUND",
                        evidence_supports=[dict(source_id="S-CURRENT", quote=quote)],
                        rationale="前缀、日期格式和混合模态均不能把旧MR复述变成新证据。")

    def test_future_identical_report_is_not_used_to_reject_current_document(self):
        findings = "肿块评估：胰头部可见肿块，大小约23X16mm。"
        future = source("S-MR-FUTURE", findings, "2026-03-04")
        future.update(kind="imaging", title="MR增强")
        unknown = source("S-MR-UNKNOWN", findings, None)
        unknown.update(kind="imaging", title="MR增强")
        store, _ = make_report_restatement_store(
            "基线胸片未见明显异常。", "查房记录。" + findings,
            extra_sources=[future, unknown])
        item = store.adjudicate(
            checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND",
            evidence_supports=[dict(source_id="S-CURRENT", quote=findings)],
            rationale="未来同文报告不能反向证明当前文书是旧复述。")["item"]
        self.assertEqual(item["evidence_supports"][0]["source_id"], "S-CURRENT")

    def test_newline_and_datetime_report_restatements_are_blocked(self):
        findings = "肿块评估：胰头部可见肿块，大小约23X16mm。"
        for prefix in ("2026-03-01 MR增强\n", "2026-03-01 08:00 MR增强："):
            with self.subTest(prefix=prefix):
                store, _ = make_report_restatement_store(
                    findings, "查房记录。影像回顾：" + prefix + findings)
                with self.assertRaisesRegex(
                        ToolError, "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE"):
                    store.adjudicate(
                        checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                        evolution="NEW_EVIDENCE_ROUND",
                        evidence_supports=[dict(source_id="S-CURRENT", quote=prefix + findings)],
                        rationale="换行或时分格式不能绕过原始报告核验。")

    def test_short_report_fragments_with_extra_suffix_are_blocked(self):
        store, _ = make_report_restatement_store(
            "胰头肿块，未见转移。",
            "查房记录。2026-03-01 MR增强：胰头肿块，未见转移。其它诊断：左肾囊肿伴周围组织未见明显异常。")
        with self.assertRaisesRegex(ToolError, "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(
                    source_id="S-CURRENT",
                    quote="2026-03-01 MR增强：胰头肿块，未见转移。其它诊断：左肾囊肿伴周围组织未见明显异常。")],
                rationale="两个短报告分句及额外后缀仍应识别旧复述。")

    def test_recitation_returns_original_candidates_without_guessing_modality(self):
        findings = "肿块评估：胰头部可见肿块，大小约23X16mm。"
        ct = source("S-CT-SAME-DAY", findings, "2026-03-01")
        ct.update(kind="imaging", title="CT增强")
        store, _ = make_report_restatement_store(
            findings, "查房记录。2026-03-01 MR增强：" + findings, extra_sources=[ct])
        with self.assertRaises(ToolError) as caught:
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(
                    source_id="S-CURRENT", quote="2026-03-01 MR增强：" + findings)],
                rationale="同日CT不能冒充MR复述的原始来源。")
        self.assertIn("S-MR-OLD", str(caught.exception))
        self.assertIn("S-CT-SAME-DAY", str(caught.exception))
        self.assertIn("匹配候选", str(caught.exception))

    def test_long_restatement_with_punctuation_change_and_trailing_text_is_blocked(self):
        first_text = "拟完善检查。"
        findings = "肿块评估：胰头部可见肿块，T1WI稍低信号、T2WI稍高信号、DWI受限，增强后可见轻度强化，大小约23X16mm。"
        current_text = "查房记录。影像回顾：2026-03-01 MR增强," + findings[:-1] + " 胰胆管评估：胰管截断伴扩张：有。 其它诊断：左肾囊肿。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, "查房记录。",
                            clock="2026-03-03")
        old_mr = source("S-MR-OLD", findings + "\n远处转移：未见。", "2026-03-01")
        old_mr["kind"] = "imaging"
        current_source = source("S-CURRENT", current_text, "2026-03-03")
        current_source["kind"] = "document"
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-CURRENT", [current], clock="2026-03-03")],
            [first, current], [source("S-FIRST", first_text, "2026-03-01"),
                               old_mr, current_source])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaisesRegex(ToolError, "SECONDARY_RESTATEMENT_REQUIRES_ORIGINAL_SOURCE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(
                    source_id="S-CURRENT",
                    quote="影像回顾：2026-03-01 MR增强," + findings[:-1]
                          + " 胰胆管评估：胰管截断伴扩张：有。 其它诊断：左肾囊肿。")],
                rationale="标点差异和额外诊断不能把同日旧MR复述变成新证据。")

    def test_same_report_text_on_different_dates_requires_original_without_choosing_one(self):
        first_text = "拟完善检查。"
        findings = "肿块评估：胰头部可见肿块，大小约23X16mm。"
        current_text = "查房记录。影像回顾：2026-03-02 MR增强," + findings
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        current = candidate("EV-CURRENT", "S-CURRENT", current_text, "查房记录。",
                            clock="2026-03-03")
        old_mr = source("S-MR-OLD", findings, "2026-03-01")
        new_mr = source("S-MR-NEW", findings, "2026-03-02")
        old_mr["kind"] = new_mr["kind"] = "imaging"
        current_source = source("S-CURRENT", current_text, "2026-03-03")
        current_source["kind"] = "document"
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]),
            checkpoint("CP-CURRENT", [current], clock="2026-03-03")],
            [first, current], [source("S-FIRST", first_text, "2026-03-01"),
                               old_mr, new_mr, current_source])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="RETAIN_CP",
                         evolution="FIRST_VISIBLE", rationale="首见锚点。")
        with self.assertRaises(ToolError) as caught:
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND",
                evidence_supports=[dict(
                    source_id="S-CURRENT", quote="影像回顾：2026-03-02 MR增强," + findings)],
                rationale="相同结论的新检查只能匹配同日原始报告。")
        self.assertIn("S-MR-NEW", str(caught.exception))
        self.assertIn("S-MR-OLD", str(caught.exception))
        self.assertIn("匹配候选", str(caught.exception))
        item = store.adjudicate(
            checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND",
            evidence_supports=[dict(source_id="S-MR-NEW", quote=findings)],
            rationale="引用同日新MR原始报告可以形成新证据轮次。")["item"]
        self.assertEqual(item["evidence_supports"][0]["source_id"], "S-MR-NEW")

    def test_diagnostic_report_read_before_current_context_does_not_satisfy_gate(self):
        current_text = "会诊后建议手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        comparator = checkpoint("CP-CT", [], kind=AFTER, clock="2026-03-02",
                                source_ids=["S-CT"])
        ct = source("S-CT", "增强CT报告。", "2026-03-02")
        store, registry = make_store([
            comparator, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [ct, source("S-CURRENT", current_text, "2026-03-03")], autoread=False)
        registry.get("read_source").run("S-CT")
        registry.get("read_checkpoint_adjudication_context").run("CP-CURRENT")
        registry.get("read_source").run("S-CURRENT")
        call = dict(checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                    evolution="NEW_EVIDENCE_ROUND",
                    evidence_supports=[dict(source_id="S-CT", quote="增强CT报告")],
                    rationale="新增证据裁决必须先建立当前上下文再读取诊断报告。")
        with self.assertRaisesRegex(
                ToolError, "COMPARATOR_DIAGNOSTIC_SOURCE_NOT_READ_IN_CURRENT_CONTEXT"):
            store.adjudicate(**call)
        report = registry.get("read_source").run("S-CT")
        self.assertEqual(report["adjudication_visibility"]["checkpoint_id"], "CP-CURRENT")
        with self.assertRaisesRegex(ToolError, "COMPARATOR_TARGET_CANNOT_BE_NEW_EVIDENCE"):
            store.adjudicate(**call)

    def test_future_comparator_report_is_explicitly_identity_only(self):
        current_text = "会诊后建议手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", current_text, current_text,
                           clock="2026-03-03")
        comparator = checkpoint("CP-CT-BEFORE", [], kind="BEFORE_ACQUISITION",
                                clock="2026-03-02", source_ids=["S-CT"])
        ct = source("S-CT", "增强CT报告。", "2026-03-05")
        ct["clocks"] = dict(report_time=dict(value="2026-03-04", precision="date"))
        store, registry = make_store([
            comparator, checkpoint("CP-CURRENT", [member], clock="2026-03-03")],
            [member], [ct, source("S-CURRENT", current_text, "2026-03-03")], autoread=False)
        registry.get("read_checkpoint_adjudication_context").run("CP-CURRENT")
        registry.get("read_source").run("S-CURRENT")
        report = registry.get("read_source").run("S-CT")
        visibility = report["adjudication_visibility"]
        self.assertTrue(visibility["is_comparator_target"])
        self.assertEqual(visibility["report_time_relation_to_current"], "after")
        self.assertEqual(visibility["evidence_use"], "COMPARATOR_EXISTING_EVIDENCE_NOT_NEW")
        self.assertIn("当前时点不可见", visibility["warning"])

    def test_first_visible_cannot_follow_earlier_management_review(self):
        first_text = "拟行手术。"
        second_text = "再次评估手术。"
        first = candidate("EV-FIRST", "S-FIRST", first_text, first_text)
        second = candidate("EV-SECOND", "S-SECOND", second_text, second_text,
                           clock="2026-03-03")
        store, _ = make_store([
            checkpoint("CP-FIRST", [first]), checkpoint("CP-SECOND", [second], clock="2026-03-03")],
            [first, second], [source("S-FIRST", first_text, "2026-03-01"),
                              source("S-SECOND", second_text, "2026-03-03")])
        store.adjudicate(checkpoint_id="CP-FIRST", disposition="REVIEW_REQUIRED",
                         evolution="UNCERTAIN", rationale="首个候选资料不足，待核。")
        with self.assertRaisesRegex(ToolError, "FIRST_VISIBLE_REQUIRES_NO_PRIOR_RETAINED_STATE"):
            store.adjudicate(checkpoint_id="CP-SECOND", disposition="RETAIN_CP",
                             evolution="FIRST_VISIBLE", rationale="不能越过更早待核状态。")

    def test_store_before_comparator_result_round_trips_through_projection(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.management_trajectory import (
            project_management_trajectory,
        )
        text = "转入后继续EUS+FNA。"
        member = candidate("EV-TRANSFER", "S-TRANSFER", text, text,
                           clock="2026-03-03", action_type="ORDER_EUS", target="EUS+FNA")
        event = dict(event_id="DE-CT", source_ids=["S-CT"],
                     exam_time=dict(value="2026-03-02 08:00", precision="minute"),
                     report_time=dict(value="2026-03-02 12:00", precision="minute"),
                     verified_time=None, visibility_clock=None, visibility_basis=None,
                     source_clocks={})
        reviews = [dict(diagnostic_event_id="DE-CT", candidate_kind="BEFORE_ACQUISITION",
                        disposition="yes", reason="合成检查前审核",
                        candidate_action=dict(action_type="ORDER_EUS", target="EUS+FNA"))]
        sources = [source("S-CONTEXT", "入院评估胰头占位。", "2026-03-01"),
                   source("S-CT", "增强CT报告。", "2026-03-02"),
                   source("S-TRANSFER", text, "2026-03-03")]
        kwargs = dict(case_id="SYN-BEFORE-ROUNDTRIP", candidates=[member], sources=sources,
                      diagnostic_events=[event], diagnostic_reviews=reviews,
                      task_time_bounds=dict(operation_start=dict(value=None)))
        provisional = project_management_trajectory(**kwargs)
        before_id = next(cp["checkpoint_id"] for cp in provisional["candidate_checkpoints"]
                         if cp["kind"] == "BEFORE_ACQUISITION")
        management_id = next(cp["checkpoint_id"] for cp in provisional["candidate_checkpoints"]
                             if cp["kind"] == MANAGEMENT)
        result = adjudication.run_checkpoint_adjudication(
            FakeHarness([dict(checkpoint_id=management_id, disposition="SUPPORT_ONLY",
                              evolution="SAME_ROUND_SUPPORT", comparator_checkpoint_id=before_id,
                              rationale="无新增证据，归入检查前状态。")]),
            case_id="SYN-BEFORE-ROUNDTRIP", sources=sources, candidates=[member],
            projection=provisional)
        self.assertTrue(result["summary"]["complete"])
        projected = project_management_trajectory(
            **dict(kwargs, checkpoint_adjudications=result))
        self.assertEqual(len(projected["candidate_checkpoints"]), 1)
        self.assertEqual(projected["candidate_checkpoints"][0]["checkpoint_id"], before_id)
        self.assertEqual(projected["candidate_checkpoints"][0]["supporting_event_ids"],
                         ["EV-TRANSFER"])

    def test_same_clock_review_result_round_trips_through_projection(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.management_trajectory import (
            project_management_trajectory,
        )
        text = "报告同刻记录拟行手术。"
        member = candidate("EV-REVIEW", "S-REVIEW", text, text,
                           clock="2026-03-02 12:00")
        event = dict(event_id="DE-CT", source_ids=["S-CT"], exam_time=None,
                     report_time=dict(value="2026-03-02 12:00", precision="minute"),
                     verified_time=None, visibility_clock=None, visibility_basis=None,
                     source_clocks={})
        reviews = [dict(diagnostic_event_id="DE-CT",
                        candidate_kind="AFTER_EVIDENCE_ARRIVAL", disposition="yes",
                        reason="合成报告后审核",
                        candidate_action=dict(action_type="START_NAT", target="新辅助化疗"))]
        sources = [source("S-CT", "增强CT报告。", "2026-03-02 12:00"),
                   source("S-REVIEW", text, "2026-03-02 12:00")]
        kwargs = dict(case_id="SYN-REVIEW-ROUNDTRIP", candidates=[member], sources=sources,
                      diagnostic_events=[event], diagnostic_reviews=reviews,
                      task_time_bounds=dict(operation_start=dict(value=None)))
        provisional = project_management_trajectory(**kwargs)
        diagnostic_id = next(cp["checkpoint_id"] for cp in provisional["candidate_checkpoints"]
                             if cp["kind"] == AFTER)
        management_id = next(cp["checkpoint_id"] for cp in provisional["candidate_checkpoints"]
                             if cp["kind"] == MANAGEMENT)
        result = adjudication.run_checkpoint_adjudication(
            FakeHarness([dict(checkpoint_id=management_id, disposition="REVIEW_REQUIRED",
                              evolution="UNCERTAIN", comparator_checkpoint_id=diagnostic_id,
                              rationale="同刻关系不可定序，保留待核。")]),
            case_id="SYN-REVIEW-ROUNDTRIP", sources=sources, candidates=[member],
            projection=provisional)
        self.assertTrue(result["summary"]["complete"])
        projected = project_management_trajectory(
            **dict(kwargs, checkpoint_adjudications=result))
        self.assertCountEqual(
            [cp["checkpoint_id"] for cp in projected["candidate_checkpoints"]],
            [diagnostic_id, management_id])
        review = next(cp for cp in projected["candidate_checkpoints"]
                      if cp["checkpoint_id"] == management_id)
        self.assertEqual(review["global_adjudication"]["disposition"], "REVIEW_REQUIRED")
        self.assertEqual(projected["migrations"][0]["migration_status"], "REVIEW_REQUIRED")
        self.assertEqual(projected["migrations"][0]["to_checkpoint_id"], management_id)

    def test_unverifiable_member_span_is_reported_not_treated_as_nonoverlap(self):
        text = "复查后仍拟行手术。"
        member = candidate("EV-CURRENT", "S-CURRENT", text, text, clock="2026-03-02")
        evidence_text = "MRI未见远处转移。"
        cp = checkpoint("CP-CURRENT", [member], clock="2026-03-02")
        store, _ = make_store([cp], [], [
            source("S-CURRENT", text, "2026-03-02"),
            source("S-MRI", evidence_text, "2026-03-01")])
        with self.assertRaisesRegex(ToolError, "MEMBER_SPANS_NOT_VERIFIABLE"):
            store.adjudicate(
                checkpoint_id="CP-CURRENT", disposition="RETAIN_CP",
                evolution="FIRST_VISIBLE",
                evidence_supports=[citation("S-MRI", evidence_text, "未见远处转移")],
                rationale="缺成员候选时不能证明不重叠。")
        result = store.result(case_id="SYN-LIMITATION", stop="not_finished")
        self.assertEqual(result["summary"]["limitations"][0]["code"],
                         "MEMBER_CANDIDATE_NOT_SUPPLIED")
        self.assertFalse(result["summary"]["complete"])


if __name__ == "__main__":
    unittest.main()

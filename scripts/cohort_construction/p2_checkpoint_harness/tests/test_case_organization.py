"""Counterexamples for one-pass case-level candidate and evidence organization.

Synthetic text only. Citation verification does not establish clinical truth.
"""
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages import case_organization as C
from scripts.cohort_construction.p2_checkpoint_harness.stages.episodes import EpisodeBuilder
from scripts.cohort_construction.p2_checkpoint_harness.tools import ToolError, ToolRegistry
from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import SourceIndex
from scripts.cohort_construction.p2_checkpoint_harness.tests.test_pipeline import _sources, _units


def _objects():
    return [
        dict(event_id="E-old", object_kind="evidence_artifact", source_ids=["S-A"],
             status="SOURCE_INDEXED", span=dict(source_id="S-A", quote="复查增强CT示胰头占位"),
             clock=dict(value="2026-03-01", precision="date")),
        dict(event_id="E-late", object_kind="evidence_artifact", source_ids=["S-B"],
             status="SOURCE_INDEXED", span=dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术"),
             clock=dict(value="2026-03-05", precision="date")),
        dict(event_id="M1", candidate_kind="management_expression", source_ids=["S-B"],
             status="ACCEPTED", span=dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术"),
             clock=dict(value="2026-03-05", precision="date")),
        dict(event_id="M2", candidate_kind="management_expression", source_ids=["S-A"],
             status="ACCEPTED", span=dict(source_id="S-A", quote="复查增强CT示胰头占位"),
             clock=dict(value="2026-03-01", precision="date")),
        dict(event_id="M3", candidate_kind="management_expression", source_ids=["S-C"],
             status="ACCEPTED", span=dict(source_id="S-C", quote="生命体征平稳，继续观察"),
             clock=dict(value="2026-03-02", precision="date")),
    ]


class CaseOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry(approve=lambda *args: True)
        self.objects = _objects()
        self.recorded = C.organization_relation_tools(
            self.registry, events=self.objects, source_index=SourceIndex(_sources(), units=_units()))
        self.tool = self.registry.get("record_relation")
        self.quotes = [dict(source_id="S-A", quote="复查增强CT示胰头占位"),
                       dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")]

    def test_HCO010_list_contains_exact_spans_without_repeated_reading_metadata(self):
        index = C.EventIndex(self.objects, source_index=None)
        tool = next(tool for tool in index.tools() if tool.name == "list_events")
        result = tool.run(limit=200)
        self.assertEqual(result["returned"], len(self.objects))
        self.assertIn("span", result["events"][2])
        self.assertNotIn("discovery_unit_id", result["events"][2])
        self.assertNotIn("discovery_source_ids", result["events"][2])

    def test_HCO001_evidence_is_a_first_class_directed_previous_to_later_object(self):
        result = self.tool.run(source_event_id="E-old", target_event_id="M1",
                               relation="evidence_used", confidence="supported",
                               basis_quotes=self.quotes, explanation="既往CT→后续手术意向")
        self.assertEqual(result["relation"]["retrieval_clock_comparison"], "before")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_USE_DIRECTION_INVALID"):
            self.tool.run(source_event_id="M1", target_event_id="E-old",
                          relation="evidence_used", confidence="supported",
                          basis_quotes=self.quotes, explanation="方向反了")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_USE_CLOCK_DIRECTION_INVALID"):
            self.tool.run(source_event_id="E-late", target_event_id="M2",
                          relation="evidence_used", confidence="supported",
                          basis_quotes=self.quotes, explanation="晚报告不能充当既往证据")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_ARTIFACT_NOT_MANAGEMENT_ENDPOINT"):
            self.tool.run(source_event_id="E-old", target_event_id="M1",
                          relation="same_management_process", confidence="supported",
                          basis_quotes=self.quotes, explanation="证据不是管理过程")

    def test_HCO002_only_supported_process_merges_context_only_is_retained(self):
        self.tool.run(source_event_id="M1", target_event_id="M2",
                      relation="same_management_process", confidence="context_only",
                      basis_quotes=self.quotes, explanation="只表明上下文，不支持合并")
        result = EpisodeBuilder(self.objects[2:4], relations=self.recorded["relations"],
                                process_merge_policy="supported_only").assemble()
        self.assertEqual(result["singleton_episodes"], 2)
        self.assertEqual(result["merged_edges"], [])
        self.assertEqual(result["unresolved_relations"][0]["reason"],
                         "PROCESS_CONTEXT_ONLY_NOT_MERGED")
        self.tool.run(source_event_id="M1", target_event_id="M2",
                      relation="same_management_process", confidence="supported",
                      basis_quotes=self.quotes, explanation="合成反例：原文明确支持同一目标")
        result = EpisodeBuilder(self.objects[2:4], relations=self.recorded["relations"],
                                process_merge_policy="supported_only").assemble()
        self.assertEqual(result["singleton_episodes"], 0)
        self.assertEqual(result["relation_semantics"],
                         "supported_only")

    def test_HCO006_legacy_default_keeps_context_only_process_merge(self):
        # The shared builder must not silently turn the old comparison path into
        # the new supported-only semantics. New case organization opts in above.
        self.tool.run(source_event_id="M1", target_event_id="M2",
                      relation="same_management_process", confidence="context_only",
                      basis_quotes=self.quotes, explanation="旧路径保留历史上下文合并语义")
        result = EpisodeBuilder(self.objects[2:4],
                                relations=self.recorded["relations"]).assemble()
        self.assertEqual(result["relation_semantics"],
                         "legacy_supported_and_context_only")
        self.assertEqual(result["singleton_episodes"], 0)
        self.assertEqual(result["merged_edges"][0]["confidence"], "context_only")
        self.assertEqual(result["unresolved_relations"], [])

    def test_HCO003_contains_and_overlap_do_not_impersonate_identity_or_process(self):
        for relation in ("contains", "overlap"):
            self.tool.run(source_event_id="M1", target_event_id="M2", relation=relation,
                          confidence="supported", basis_quotes=self.quotes,
                          explanation="组合/部分重叠保留，不压平")
        result = EpisodeBuilder(self.objects[2:4], relations=self.recorded["relations"]).assemble()
        self.assertEqual(len(result["episodes"]), 2)
        self.assertEqual([row["relation"] for row in result["composition_relations"]],
                         ["contains", "overlap"])
        self.assertFalse(result["duplicate_candidate_groups"][0]["duplicate_candidate"])

    def test_HCO004_unknown_clock_is_explicit_not_reversed_or_invented(self):
        self.objects[0]["clock"] = {}
        self.objects[1]["clock"] = {}
        result = self.tool.run(source_event_id="E-old", target_event_id="M1",
                               relation="evidence_used", confidence="uncertain",
                               explanation="检索时钟未知")
        self.assertEqual(result["relation"]["retrieval_clock_comparison"], "unknown")
        self.assertTrue(result["relation"]["comparison_not_choice_time"])

    def test_HCO008_evidence_artifact_uses_visibility_not_exam_clock(self):
        from scripts.cohort_construction.p2_auto_checkpoint_v04.context import build_units
        from scripts.cohort_construction.p2_checkpoint_harness.pipeline import _evidence_artifact

        def clock(value):
            return dict(value=value, precision="second", raw=value)

        imaging = dict(source_id="S-I", kind="imaging", title="增强CT",
                       text="合成影像所见。", source_sha256="i" * 64,
                       clock=clock("2026-03-01 12:00:00"), clock_basis="verified_time_proxy",
                       clocks=dict(exam_time=clock("2026-03-01 08:00:00"),
                                   report_time=clock("2026-03-01 11:00:00"),
                                   verified_time=clock("2026-03-01 12:00:00")))
        unit = build_units([imaging])[0]
        artifact = _evidence_artifact(unit)
        self.assertEqual(artifact["clock"]["value"], "2026-03-01 12:00:00")
        self.assertEqual(artifact["clock_basis"], "verified_time_proxy")
        self.assertEqual(unit["exam_clock"]["value"], "2026-03-01 08:00:00")
        self.assertNotEqual(unit["time_upper_bound"], unit["clock"])

    def test_HCO009_agent_surface_is_relation_recording_only(self):
        from scripts.cohort_construction.p2_checkpoint_harness.harness import Harness
        from scripts.cohort_construction.p2_checkpoint_harness.pipeline import CaseRunner
        from scripts.cohort_construction.p2_checkpoint_harness.tests.test_pipeline import (
            StubTransport,
        )
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(case_id="RELATION-ONLY", run_dir=Path(tmp) / "run",
                              cache_dir=Path(tmp) / "cache",
                              transport=StubTransport([dict(tool_calls=[dict(
                id="relations-done", name="finish", arguments=dict(note="done"))])]))
            runner = CaseRunner(harness=harness, sources=_sources(), units=_units(),
                                case_id="RELATION-ONLY")
            runner.events = [dict(self.objects[2], status="ACCEPTED", demoted=False,
                                  scope="yes", event_id="M1")]
            runner.evidence_inventory = [self.objects[0]]
            result = runner.run_case_organization()
            self.assertEqual(result["stop"], "answered")
            schemas = harness.transport.tools_seen
            self.assertNotIn("preview_episodes", schemas)
            self.assertNotIn("event_episode", schemas)
            self.assertNotIn("demote_event", schemas)
            self.assertIn("record_relation", schemas)
            self.assertIn("finish", schemas)
            called = [event["payload"].get("name") for event in
                      harness.sessions()["case_organization"].events()
                      if event["type"] == "tool/call"]
            self.assertEqual(called, ["finish"])
            self.assertIsNone(runner.artifacts()["episode_review"])
            self.assertEqual(runner.artifacts()["case_organization"]["agent_task"],
                             "record_candidate_relations_only")
            harness.close()
        with self.assertRaisesRegex(ToolError, "RELATION_BASIS_NOT_ENDPOINT_SPAN"):
            self.tool.run(source_event_id="E-old", target_event_id="M1",
                          relation="evidence_used", confidence="supported",
                          basis_quotes=[dict(source_id="S-A", quote="患者腹痛加重"),
                                        dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")],
                          explanation="同文书另一句不是本证据span")
        bad = dict(self.objects[0], status="PROPOSED_WITH_PROBLEMS")
        bad_registry = ToolRegistry(approve=lambda *args: True)
        self.recorded = C.organization_relation_tools(
            bad_registry, events=[bad, self.objects[2]], source_index=SourceIndex(_sources(), units=_units()))
        with self.assertRaisesRegex(ToolError, "RELATION_ENDPOINT_SPAN_NOT_VERIFIED"):
            bad_registry.get("record_relation").run(
                source_event_id="E-old", target_event_id="M1",
                relation="evidence_used", confidence="uncertain",
                basis_quotes=[], explanation="问题候选不能组织")
        self.assertEqual(self.recorded["relations"], [])

    def test_HCO005_same_input_legacy_cannot_see_evidence_new_stage_can(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.ordering import relation_tools
        legacy_registry = ToolRegistry(approve=lambda *args: True)
        relation_tools(legacy_registry, events=self.objects[2:], source_index=SourceIndex(_sources(), units=_units()))
        with self.assertRaisesRegex(ToolError, "UNKNOWN_EVENT_ID"):
            legacy_registry.get("record_relation").run(
                source_event_id="E-old", target_event_id="M1",
                relation="evidence_comparison_previous", confidence="supported",
                basis_quotes=self.quotes, explanation="旧ordering对象池没有证据")
        self.assertIn("evidence_types",
                      [tool.name for tool in C.EventIndex(
                          self.objects, source_index=None).tools()])
        self.assertGreaterEqual(len(self.objects), 5)


if __name__ == "__main__":
    unittest.main()

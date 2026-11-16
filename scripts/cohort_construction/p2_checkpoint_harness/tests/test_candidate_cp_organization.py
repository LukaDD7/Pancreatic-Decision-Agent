"""Candidate-CP relation and independent-runner tests; all records are synthetic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cohort_construction.p2_checkpoint_harness import (
    run_candidate_cp_organization as run_candidate,
)
from scripts.cohort_construction.p2_checkpoint_harness import run_case
from scripts.cohort_construction.p2_checkpoint_harness.stages import (
    candidate_cp_organization as organization,
)
from scripts.cohort_construction.p2_checkpoint_harness.tests.test_run_case import (
    synthetic_bundle,
)
from scripts.cohort_construction.p2_checkpoint_harness.tools import ToolError, ToolRegistry
from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import SourceIndex


TEXTS = {
    "S-A": "术前讨论：结合增强CT示胰头占位，拟行胰十二指肠切除术。",
    "S-B": "复核后仍按同一胰十二指肠切除术目标推进。",
    "S-REPORT": "增强CT示胰头占位，未见远处转移。",
    "S-REPEAT": "增强CT。间隔。增强CT。",
    "S-THIRD": "第三方文书内容。",
}


def source(source_id):
    return dict(source_id=source_id, text=TEXTS[source_id],
                source_sha256=hashlib.sha256(TEXTS[source_id].encode()).hexdigest(),
                clock=dict(value="2026-03-01", precision="date"))


def checkpoint(checkpoint_id, kind, source_id, *, quote=None, clock="2026-03-01",
               diagnostic_event_id=None):
    spans = []
    if quote is not None:
        start = TEXTS[source_id].index(quote)
        spans.append(dict(source_id=source_id, quote=quote, start=start,
                          end=start + len(quote)))
    return dict(checkpoint_id=checkpoint_id, kind=kind, source_ids=[source_id],
                member_event_ids=(["EV-1", "EV-2"]
                                  if kind == "management_decision" else []),
                masked_spans=spans, clock=dict(value=clock, precision="date"),
                candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION",
                review_status="REVIEW_REQUIRED",
                diagnostic_event_id=diagnostic_event_id)


def citation(checkpoint_id, source_id, quote):
    return dict(checkpoint_id=checkpoint_id, source_id=source_id, quote=quote)


class CandidateCPRelationTests(unittest.TestCase):
    def setUp(self):
        self.sources = [source(item) for item in TEXTS]
        self.index = SourceIndex(self.sources)
        self.registry = ToolRegistry(approve=lambda *args: True)
        self.first = checkpoint("CP-A", "management_decision", "S-A",
                                quote="拟行胰十二指肠切除术。")
        self.second = checkpoint("CP-B", "management_decision", "S-B",
                                 quote="仍按同一胰十二指肠切除术目标推进。",
                                 clock="2026-03-02")
        self.arrival = checkpoint(
            "CP-REPORT", "evidence_arrival_reassessment", "S-REPORT",
            clock="2026-03-01", diagnostic_event_id="DE-1")
        self.pre = checkpoint("CP-PRE", "pre_exam_evaluation", "S-REPORT",
                              clock="2026-02-28", diagnostic_event_id="DE-1")

    def record(self, checkpoints, **overrides):
        registry = ToolRegistry(approve=lambda *args: True)
        recorded = organization.candidate_cp_relation_tools(
            registry, checkpoints=checkpoints, source_index=self.index)
        arguments = dict(
            source_checkpoint_id="CP-A", target_checkpoint_id="CP-B",
            relation="same_management_process", confidence="supported",
            basis_quotes=[citation("CP-A", "S-A", "拟行胰十二指肠切除术。"),
                          citation("CP-B", "S-B", "同一胰十二指肠切除术目标推进")],
            explanation="两端原文明确指向同一手术目标。",
            shared_management_goal="胰十二指肠切除术")
        arguments.update(overrides)
        tool = registry.get("record_relation").run(**arguments)
        return recorded, tool

    def test_CCPO001_same_goal_supported_relation_merges_whole_cps(self):
        checkpoints = [self.first, self.second, deepcopy(self.arrival)]
        recorded, relation = self.record(checkpoints)
        self.assertTrue(relation["recorded"])
        result = organization.assemble_candidate_cp_episodes(
            checkpoints, recorded["relations"])
        episode = next(item for item in result["episodes"] if "CP-A" in item["checkpoint_ids"])
        self.assertEqual(set(episode["checkpoint_ids"]), {"CP-A", "CP-B"})
        self.assertNotIn("event_ids", episode)
        self.assertNotIn("member_event_ids", episode)
        self.assertEqual(self.first["member_event_ids"], ["EV-1", "EV-2"])
        self.assertTrue(result["conservation"]["all_candidate_checkpoints_conserved"])
        self.assertEqual(result["unconnected_checkpoint_ids"], ["CP-REPORT"])

    def test_same_core_procedure_with_different_preparation_wording_still_merges(self):
        first, relation = self.record([self.first, self.second])
        edge = relation["relation"]
        edge["shared_management_goal"] = "排除手术禁忌后限期行胰十二指肠切除术"
        edge.pop("normalized_shared_management_goal", None)
        second = deepcopy(edge)
        second["relation_id"] = "CPR-OTHER"
        second["shared_management_goal"] = "胰十二指肠切除术，术前排除手术禁忌并充分准备"
        result = organization.assemble_candidate_cp_episodes(
            [self.first, self.second], [edge, second])
        self.assertEqual(len(result["episodes"]), 1)
        self.assertEqual(result["unresolved_relations"], [])
        self.assertEqual(result["episodes"][0]["normalized_management_goals"],
                         ["procedure: pancreatoduodenectomy"])

    def test_CCPO002_non_supported_or_goal_less_process_relation_is_refused(self):
        with self.assertRaisesRegex(ToolError, "SUPPORTED_CONFIDENCE"):
            self.record([self.first, self.second], confidence="uncertain")
        with self.assertRaisesRegex(ToolError, "REQUIRES_SHARED_GOAL"):
            self.record([self.first, self.second], shared_management_goal="")

    def test_CCPO003_fake_or_third_party_citation_is_refused(self):
        with self.assertRaisesRegex(ToolError, "NOT_VERIFIED"):
            self.record([self.first, self.second],
                        basis_quotes=[citation("CP-A", "S-A", "伪造的手术计划"),
                                      citation("CP-B", "S-B", "同一胰十二指肠切除术目标推进")])
        with self.assertRaisesRegex(ToolError, "OUTSIDE_CHECKPOINT_SOURCES"):
            self.record([self.first, self.second],
                        basis_quotes=[citation("CP-A", "S-THIRD", "第三方文书内容。"),
                                      citation("CP-B", "S-B", "同一胰十二指肠切除术目标推进")])
        with self.assertRaisesRegex(ToolError, "MISSING_ENDPOINT"):
            self.record([self.first, self.second],
                        basis_quotes=[citation("CP-A", "S-A", "拟行胰十二指肠切除术。"),
                                      citation("CP-A", "S-A", "术前讨论")])

    def test_CCPO004_explicit_offsets_are_checked(self):
        quotes = [citation("CP-A", "S-A", "拟行胰十二指肠切除术。"),
                  citation("CP-B", "S-B", "同一胰十二指肠切除术目标推进")]
        quotes[0].update(start=0, end=3)
        with self.assertRaisesRegex(ToolError, "OFFSET"):
            self.record([self.first, self.second], basis_quotes=quotes)

    def test_CCPO005_diagnostic_pair_needs_no_management_span_and_stays_separate(self):
        projection = dict(diagnostic_events=[dict(
            event_id="DE-1", candidate_checkpoint_ids=dict(
                pre_exam_evaluation="CP-PRE",
                evidence_arrival_reassessment="CP-REPORT"))])
        relations = organization.program_diagnostic_pair_relations(projection)
        self.assertEqual(len(relations), 1)
        relation = relations[0]
        self.assertEqual(relation["relation"], "evidence_arrival")
        self.assertEqual(relation["basis_quotes"], [])
        self.assertTrue(relation["program_diagnostic_pair"])
        self.assertFalse(relation["management_span_required"])
        result = organization.assemble_candidate_cp_episodes(
            [self.pre, self.arrival, self.first], relations)
        self.assertEqual(len(result["program_diagnostic_pair_edges"]), 1)
        pair = next(item for item in result["episodes"]
                    if "CP-PRE" in item["checkpoint_ids"])
        self.assertEqual(set(pair["checkpoint_ids"]), {"CP-PRE", "CP-REPORT"})
        self.assertEqual(result["evidence_references"], [])

    def test_CCPO006_evidence_used_requires_textual_link_and_does_not_merge(self):
        registry = ToolRegistry(approve=lambda *args: True)
        organization.candidate_cp_relation_tools(
            registry, checkpoints=[self.arrival, self.first], source_index=self.index)
        with self.assertRaisesRegex(ToolError, "REQUIRES_EXPLICIT_TEXTUAL_LINK"):
            registry.get("record_relation").run(
                source_checkpoint_id="CP-REPORT", target_checkpoint_id="CP-A",
                relation="evidence_used", confidence="supported",
                basis_quotes=[citation("CP-REPORT", "S-REPORT", "增强CT示胰头占位"),
                              citation("CP-A", "S-A", "拟行胰十二指肠切除术。")],
                explanation="报告有CT结论且另一处有手术计划，但没有原文使用连接。")
        registry = ToolRegistry(approve=lambda *args: True)
        recorded = organization.candidate_cp_relation_tools(
            registry, checkpoints=[self.arrival, self.first], source_index=self.index)
        relation = registry.get("record_relation").run(
            source_checkpoint_id="CP-REPORT", target_checkpoint_id="CP-A",
            relation="evidence_used", confidence="supported",
            basis_quotes=[citation("CP-REPORT", "S-REPORT", "增强CT示胰头占位"),
                          citation("CP-A", "S-A", "结合增强CT示胰头占位，拟行胰十二指肠切除术。")],
            explanation="目标原文以结合增强CT明确连接证据与后续手术判断。")["relation"]
        self.assertEqual(relation["evidence_use_basis"],
                         "explicit_source_text_required_not_temporal_similarity")
        self.assertEqual(relation["evidence_text_link"]["method"],
                         "normalized_evidence_quote_contained")
        result = organization.assemble_candidate_cp_episodes(
            [self.arrival, self.first], [relation])
        self.assertEqual(len(result["evidence_references"]), 1)
        self.assertEqual(len(result["episodes"]), 2)
        self.assertEqual(result["merged_edges"], [])

    def test_CCPO007_evidence_used_direction_and_clock_are_enforced(self):
        registry = ToolRegistry(approve=lambda *args: True)
        recorded = organization.candidate_cp_relation_tools(
            registry, checkpoints=[self.arrival, self.first], source_index=self.index)
        self.assertTrue(recorded["relations"] == [])
        with self.assertRaisesRegex(ToolError, "DIRECTION_INVALID"):
            registry.get("record_relation").run(
                source_checkpoint_id="CP-A", target_checkpoint_id="CP-REPORT",
                relation="evidence_used", confidence="supported",
                basis_quotes=[citation("CP-A", "S-A", "拟行胰十二指肠切除术。"),
                              citation("CP-REPORT", "S-REPORT", "增强CT示胰头占位")],
                explanation="反向关系。")
        later = deepcopy(self.arrival)
        later["clock"] = dict(value="2026-03-03", precision="date")
        registry = ToolRegistry(approve=lambda *args: True)
        organization.candidate_cp_relation_tools(
            registry, checkpoints=[later, self.first], source_index=self.index)
        with self.assertRaisesRegex(ToolError, "CLOCK_DIRECTION_INVALID"):
            registry.get("record_relation").run(
                source_checkpoint_id="CP-REPORT", target_checkpoint_id="CP-A",
                relation="evidence_used", confidence="supported",
                basis_quotes=[citation("CP-REPORT", "S-REPORT", "增强CT示胰头占位"),
                              citation("CP-A", "S-A", "拟行胰十二指肠切除术。")],
                explanation="晚到的报告不能作为此前判断的已使用证据。")

    def test_CCPO008_identity_and_composition_do_not_merge(self):
        for kind in ("same_event", "contains", "overlap"):
            with self.subTest(kind=kind):
                registry = ToolRegistry(approve=lambda *args: True)
                recorded = organization.candidate_cp_relation_tools(
                    registry, checkpoints=[self.first, self.second], source_index=self.index)
                registry.get("record_relation").run(
                    source_checkpoint_id="CP-A", target_checkpoint_id="CP-B",
                    relation=kind, confidence="supported",
                    basis_quotes=[citation("CP-A", "S-A", "拟行胰十二指肠切除术。"),
                                  citation("CP-B", "S-B", "同一胰十二指肠切除术目标推进")],
                    explanation="合成测试关系。")
                result = organization.assemble_candidate_cp_episodes(
                    [self.first, self.second], recorded["relations"])
                self.assertEqual(len(result["episodes"]), 2)

    def test_CCPO009_unconnected_cps_are_conserved_as_singletons(self):
        result = organization.assemble_candidate_cp_episodes(
            [self.first, self.second, self.arrival], [])
        self.assertEqual(len(result["episodes"]), 3)
        self.assertEqual(result["singleton_episodes"], 3)
        self.assertEqual(result["conservation"]["unique_assigned_checkpoint_count"], 3)
        self.assertEqual(set(result["unconnected_checkpoint_ids"]),
                         {"CP-A", "CP-B", "CP-REPORT"})

    def test_CCPO014_repeated_quote_requires_and_preserves_explicit_location(self):
        repeated = checkpoint("CP-REPEAT", "management_decision", "S-REPEAT",
                              quote="增强CT。")
        arguments = dict(
            source_checkpoint_id="CP-REPEAT", target_checkpoint_id="CP-B",
            relation="same_management_process", confidence="supported",
            basis_quotes=[citation("CP-REPEAT", "S-REPEAT", "增强CT"),
                          citation("CP-B", "S-B", "同一胰十二指肠切除术目标推进")],
            explanation="合成重复引文位置测试。", shared_management_goal="限期行胰十二指肠切除术")
        registry = ToolRegistry(approve=lambda *args: True)
        organization.candidate_cp_relation_tools(
            registry, checkpoints=[repeated, self.second], source_index=self.index)
        with self.assertRaisesRegex(ToolError, "OFFSET_REQUIRED_FOR_REPEATED_QUOTE"):
            registry.get("record_relation").run(**arguments)
        start = TEXTS["S-REPEAT"].rindex("增强CT")
        arguments["basis_quotes"][0].update(start=start, end=start + len("增强CT"))
        relation = registry.get("record_relation").run(**arguments)["relation"]
        verdict = relation["citation_verdicts"][0]
        self.assertEqual(verdict["reason"], "QUOTE_VERIFIED_AT_EXPLICIT_OFFSET")
        self.assertEqual(verdict["selected_occurrence"], [start, start + len("增强CT")])
        self.assertEqual((verdict["start"], verdict["end"]),
                         (start, start + len("增强CT")))

    def test_CCPO015_conflicting_component_goals_block_transitive_merge(self):
        third = checkpoint("CP-C", "management_decision", "S-B",
                           quote="同一胰十二指肠切除术目标推进", clock="2026-03-03")

        def relation(source_id, target_id, goal):
            return dict(source_checkpoint_id=source_id, target_checkpoint_id=target_id,
                        relation="same_management_process", confidence="supported",
                        shared_management_goal=goal, relation_id=source_id + target_id)

        consistent = organization.assemble_candidate_cp_episodes(
            [self.first, self.second], [relation(
                "CP-A", "CP-B", "限期行胰十二指肠切除术")])
        self.assertEqual(len(consistent["episodes"]), 1)
        self.assertEqual(consistent["episodes"][0]["normalized_management_goals"],
                         ["procedure: pancreatoduodenectomy"])
        relations = [relation("CP-A", "CP-B", "限期行胰十二指肠切除术"),
                     relation("CP-B", "CP-C", "全身化疗")]
        result = organization.assemble_candidate_cp_episodes(
            [self.first, self.second, third], relations)
        self.assertEqual(result["merged_edges"], [])
        self.assertEqual(len(result["episodes"]), 3)
        self.assertEqual(len(result["unresolved_relations"]), 2)
        self.assertEqual({item["reason"] for item in result["unresolved_relations"]},
                         {"CONFLICTING_NORMALIZED_MANAGEMENT_GOALS"})
        self.assertEqual([item["relation"]["relation_id"]
                          for item in result["unresolved_relations"]],
                         ["CP-ACP-B", "CP-BCP-C"])
        episode_b = next(item for item in result["episodes"]
                         if item["checkpoint_ids"] == ["CP-B"])
        self.assertEqual(episode_b["management_goal_status"], "CONFLICTING")
        self.assertEqual(set(episode_b["normalized_management_goals"]),
                         {"procedure: pancreatoduodenectomy",
                          "systemic_treatment: unspecified"})


class FinishTransport:
    def __init__(self, cache, settings, *, config=None):
        self.receipts = []
        self.list_calls = 0
        self.offset = 0

    def complete(self, messages, *, tools=None, task="agent_step", purpose=None):
        receipt = dict(task=task, usage=dict(prompt_tokens=5, completion_tokens=2))
        self.receipts.append(receipt)
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            content = json.loads(message.get("content") or "{}")
            if "checkpoints" not in content:
                continue
            if content.get("truncated"):
                self.offset += content["returned"]
                self.list_calls += 1
                return dict(tool_calls=[dict(
                    id="list-next", name="list_candidate_checkpoints",
                    arguments=dict(offset=self.offset, limit=200))]), receipt
            return dict(tool_calls=[dict(id="finish-1", name="finish", arguments={})]), receipt
        self.list_calls += 1
        return dict(tool_calls=[dict(
            id="list-first", name="list_candidate_checkpoints",
            arguments=dict(offset=0, limit=200))]), receipt


class EarlyFinishTransport(FinishTransport):
    def complete(self, messages, *, tools=None, task="agent_step", purpose=None):
        receipt = dict(task=task, usage=dict(prompt_tokens=5, completion_tokens=2))
        self.receipts.append(receipt)
        return dict(tool_calls=[dict(id="finish-early", name="finish", arguments={})]), receipt


class CandidateCPRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.sources = self.directory / "source_fhir_r4_bundle.json"
        self.sources.write_text(json.dumps(synthetic_bundle(), ensure_ascii=False),
                                encoding="utf-8")
        self.discovery = self.directory / "case_result.json"
        text = "临床团队决定完善增强CT后评估手术"
        start = ("2026-03-01 病程记录\n复查增强CT示胰头占位，" + text + "\n").index(text)
        self.discovery.write_text(json.dumps(dict(
            case_id="SYN-01", candidate_spans=[dict(
                event_id="EV-SYN", status="ACCEPTED", scope="yes",
                record_function="new_decision",
                candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST", target="增强CT"),
                discovery_document_profile="preoperative_course",
                clock=dict(value="2026-03-01", precision="date"),
                clock_basis="body_record_time",
                span=dict(source_id="SYN-1", quote=text, start=start,
                          end=start + len(text)))],
            evidence_inventory=[], out_of_scope_candidates=[],
            discovery=dict(units={})), ensure_ascii=False), encoding="utf-8")
        self.output = self.directory / "new-output"
        self.cache = self.directory / "new-cache"

    def tearDown(self):
        self._tmp.cleanup()

    def argv(self, *extra):
        return ["--discovery-result", str(self.discovery), "--sources", str(self.sources),
                "--output", str(self.output), "--cache", self.cache.as_posix(),
                "--case-id", "SYN-01", *extra]

    def contract(self):
        import argparse
        args = argparse.Namespace(
            discovery_result=self.discovery, sources=self.sources, case_id="SYN-01",
            config=None)
        plan, _, _ = run_candidate._prepare(args)
        return run_candidate.run_contract(args, plan)

    def authorization(self, contract):
        return dict(run_case.AUTH_REQUIRED, execution_scope=run_candidate.EXECUTION_SCOPE,
                    case_id="SYN-01", discovery_result_sha256=contract[
                        "discovery_result_sha256"],
                    sources_sha256=contract["sources_sha256"], config_sha256=None,
                    code_sha256=run_case._code_digest(contract["code_hashes"]),
                    limits=dict(max_requests=10, max_total_tokens=10000))

    def test_incomplete_frozen_diagnostic_review_cannot_enter_cp_organization(self):
        raw = json.loads(self.discovery.read_text(encoding="utf-8"))
        raw["discovery"]["units"]["TU-I"] = dict(summary=dict(
            diagnostic_review_required=True, diagnostic_review_complete=False))
        self.discovery.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit,
                                    "FROZEN_DISCOVERY_DIAGNOSTIC_REVIEW_INCOMPLETE:TU-I"):
            run_candidate.main(self.argv("--dry-run"))

    def test_CCPO010_dry_run_binds_new_scope_projection_and_writes_nothing(self):
        self.assertEqual(run_candidate.main(self.argv("--dry-run")), 0)
        contract, binding = self.contract()
        self.assertEqual(contract["execution_scope"], run_candidate.EXECUTION_SCOPE)
        self.assertIn("discovery_result_sha256", binding)
        self.assertIn("candidate_projection_sha256", contract)
        self.assertEqual(contract["plan"]["candidate_checkpoint_ids"] and
                         len(contract["plan"]["candidate_checkpoint_ids"]), 1)
        self.assertIn("scripts/cohort_construction/p2_checkpoint_harness/"
                      "stages/candidate_cp_organization.py", contract["code_hashes"])
        for relative in ("scripts/cohort_construction/model_api.py",
                         "scripts/cohort_construction/p2_auto_checkpoint/fhir_io.py",
                         "scripts/cohort_construction/p2_auto_checkpoint_v04/context.py"):
            self.assertIn(relative, contract["code_hashes"])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.cache.exists())

    def test_CCPO011_authorization_binds_frozen_discovery(self):
        contract, _ = self.contract()
        authorization = self.authorization(contract)
        authorization["discovery_result_sha256"] = "0" * 64
        with self.assertRaisesRegex(SystemExit, "BINDING_MISMATCH:discovery_result_sha256"):
            run_candidate.validate_authorization(authorization, contract)

    def test_CCPO012_output_and_cache_must_be_new(self):
        self.output.mkdir()
        with self.assertRaisesRegex(SystemExit, "OUTPUT_PATH_MUST_BE_NEW"):
            run_candidate.main(self.argv("--dry-run"))
        self.output.rmdir()
        self.cache.mkdir()
        with self.assertRaisesRegex(SystemExit, "CACHE_PATH_MUST_BE_NEW"):
            run_candidate.main(self.argv("--dry-run"))

    def test_CCPO013_stub_execution_writes_five_artifacts_without_discovery_rerun(self):
        contract, _ = self.contract()
        authorization = self.directory / "authorization.json"
        authorization.write_text(json.dumps(self.authorization(contract)), encoding="utf-8")
        before = self.discovery.read_bytes()
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   FinishTransport):
            self.assertEqual(run_candidate.main(self.argv(
                "--execute", "--authorization", str(authorization))), 0)
        self.assertEqual(self.discovery.read_bytes(), before)
        for name in ("candidate_cp_relations.json", "candidate_cp_episodes.json",
                     "run_contract.json", "summary.json", "model_receipts.json"):
            self.assertTrue((self.output / name).is_file(), name)
        relations = json.loads((self.output / "candidate_cp_relations.json").read_text())
        episodes = json.loads((self.output / "candidate_cp_episodes.json").read_text())
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(len(relations["candidate_checkpoints"]), 1)
        self.assertEqual(episodes["episodes"][0]["checkpoint_ids"],
                         [relations["candidate_checkpoints"][0]["checkpoint_id"]])
        self.assertNotIn("event_ids", episodes["episodes"][0])
        self.assertTrue(episodes["conservation"]["all_candidate_checkpoints_conserved"])
        self.assertFalse(summary["stages"]["discovery_input"]["discovery_rerun"])
        self.assertEqual(summary["status"], "COMPLETE")
        self.assertEqual(summary["receipts"], 2)
        self.assertTrue(summary["stages"]["candidate_cp_organization"][
            "candidate_checkpoint_list_coverage"]["complete"])
        for name in ("candidate_cp_relations.json", "candidate_cp_episodes.json",
                     "run_contract.json", "model_receipts.json"):
            self.assertEqual(summary["output_hashes"][name],
                             hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        session_hashes = {name: value for name, value in summary["output_hashes"].items()
                          if name.startswith("session/sessions/")}
        self.assertTrue(session_hashes)

    def test_CCPO016_finish_before_complete_listing_is_incomplete(self):
        contract, _ = self.contract()
        authorization = self.directory / "authorization.json"
        authorization.write_text(json.dumps(self.authorization(contract)), encoding="utf-8")
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   EarlyFinishTransport):
            self.assertEqual(run_candidate.main(self.argv(
                "--execute", "--authorization", str(authorization))), 1)
        summary = json.loads((self.output / "summary.json").read_text())
        stage = summary["stages"]["candidate_cp_organization"]
        self.assertEqual(summary["status"], "INCOMPLETE")
        self.assertEqual(stage["stop"], "incomplete_cp_coverage")
        self.assertEqual(len(stage["candidate_checkpoint_list_coverage"][
            "unlisted_checkpoint_ids"]), 1)

    def test_CCPO017_more_than_200_cps_require_and_satisfy_pagination(self):
        from scripts.cohort_construction.p2_checkpoint_harness.harness import (
            Harness, HarnessConfig,
        )

        checkpoints = [checkpoint("CP-%03d" % index, "management_decision", "S-A",
                                  quote="拟行胰十二指肠切除术。")
                       for index in range(201)]
        transport = FinishTransport(self.cache, object())
        harness = Harness(case_id="SYN-PAGING", run_dir=self.output / "session",
                          cache_dir=self.cache, transport=transport,
                          config=HarnessConfig(max_inline_tool_tokens=1000000))
        runner = organization.CandidateCPOrganizationRunner(
            harness=harness, source_index=SourceIndex([source("S-A")]),
            projection=dict(candidate_checkpoints=checkpoints, diagnostic_events=[]),
            case_id="SYN-PAGING")
        result = runner.run()
        harness.close()
        self.assertEqual(result["stage"]["stop"], "answered")
        self.assertEqual(transport.list_calls, 2)
        self.assertEqual(result["stage"]["candidate_checkpoint_list_coverage"][
            "listed_checkpoint_count"], 201)

    def test_CCPO018_input_mutation_after_authorization_is_refused_before_second_read(self):
        contract, _ = self.contract()
        authorization = self.directory / "authorization.json"
        authorization.write_text(json.dumps(self.authorization(contract)), encoding="utf-8")

        def mutate_after_authorization(*args):
            self.discovery.write_text("{}", encoding="utf-8")
            return dict(max_requests=10, max_total_tokens=10000)

        with patch.object(run_candidate, "validate_authorization", mutate_after_authorization):
            with self.assertRaisesRegex(
                    SystemExit, "INPUT_HASH_CHANGED_AFTER_AUTHORIZATION:discovery_result_sha256"):
                run_candidate.main(self.argv(
                    "--execute", "--authorization", str(authorization)))
        self.assertFalse(self.output.exists())
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()

"""Stage sequencing: nothing a stage produced may vanish in the next one.

These tests drive the whole three-stage chain with a stub provider, so what is
checked is the pipeline's own bookkeeping: zero-event units are recorded, a
proposal with a bad citation survives, a demotion annotates rather than deletes,
and unresolved relations are reported.
"""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_harness import session as S
from scripts.cohort_construction.p2_checkpoint_harness.harness import (
    Harness, HarnessConfig,
)
from scripts.cohort_construction.p2_checkpoint_harness.pipeline import CaseRunner

DOC_A = "2026-03-01 病程记录\n患者腹痛加重，复查增强CT示胰头占位，考虑可切除。\n临床团队决定完善增强CT后评估手术。\n"
DOC_B = "2026-03-05 术前讨论\n讨论后拟行胰十二指肠切除术。\n"
DOC_C = "2026-03-02 护理记录\n生命体征平稳，继续观察。\n"


class StubTransport:
    """Replays canned answers in order and records what each request saw."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []
        self.receipts = []
        self.tools_seen = set()

    def complete(self, messages, *, tools=None, task="agent_step", purpose=None):
        self.seen.append([dict(message) for message in messages])
        self.tools_seen.update(tool.get("name") for tool in tools or ())
        if not self.answers:
            raise AssertionError("StubTransport ran out of answers (task=%s)" % task)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        receipt = dict(status="SUCCESS", usage=dict(prompt_tokens=10, completion_tokens=5))
        self.receipts.append(receipt)
        return json.loads(text), receipt


def _sources():
    return [
        dict(source_id="S-A", kind="document", title="病程记录", text=DOC_A,
             source_sha256="a" * 64, clock=dict(value="2026-03-01", precision="date"),
             clock_basis="body_record_time", clocks={}),
        dict(source_id="S-B", kind="document", title="术前讨论", text=DOC_B,
             source_sha256="b" * 64, clock=dict(value="2026-03-05", precision="date"),
             clock_basis="body_record_time", clocks={}),
        dict(source_id="S-C", kind="document", title="护理记录", text=DOC_C,
             source_sha256="c" * 64, clock=dict(value="2026-03-02", precision="date"),
             clock_basis="body_record_time", clocks={}),
    ]


def _units():
    return [
        dict(unit_id="DU-A", source_ids=["S-A"], kind="document",
             document_profile="progress_note", clock=dict(value="2026-03-01"),
             original_texts=[dict(source_id="S-A", text=DOC_A)]),
        dict(unit_id="DU-B", source_ids=["S-B"], kind="document",
             document_profile="preoperative_discussion", clock=dict(value="2026-03-05"),
             original_texts=[dict(source_id="S-B", text=DOC_B)]),
        dict(unit_id="DU-C", source_ids=["S-C"], kind="document",
             document_profile="nursing_note", clock=dict(value="2026-03-02"),
             original_texts=[dict(source_id="S-C", text=DOC_C)]),
    ]


def _proposal(local_id, quote, action=None, record_function="new_decision",
              source_id="S-A", scope="yes", **span_offsets):
    arguments = dict(local_id=local_id, scope=scope,
                     record_function=record_function,
                     source_id=source_id, quote=quote, **span_offsets)
    if scope == "yes" and record_function in ("new_decision", "reassessment"):
        action_id = ("SURG_PANCREATICODUODENECTOMY" if "胰十二指肠" in (action or quote)
                     else "DIAG_PANCREATIC_CONTRAST_CT" if "CT" in (action or quote)
                     else "OTHER_SPECIFIED")
        arguments["action_id"] = action_id
        if action_id == "OTHER_SPECIFIED":
            arguments["target"] = action or quote
    return dict(tool_calls=[dict(id="c-" + local_id, name="propose_candidate",
                                 arguments=arguments)])


def _finish(call_id="f"):
    return dict(tool_calls=[dict(id=call_id, name="finish", arguments=dict(note="done"))])


def _preview(call_id="preview"):
    return dict(tool_calls=[dict(id=call_id, name="preview_episodes", arguments={})])


def _review_finish(call_id="review-finish", *, runner=None, evidence_checks=None):
    arguments = dict(note="done")
    if runner is not None:
        arguments.update(
            reviewed_event_ids=[event["event_id"]
                                for event in runner._organization_objects()],
            evidence_checks=evidence_checks or [])
    return dict(tool_calls=[dict(id=call_id, name="finish", arguments=arguments)])


def _episode_review_answers(call_id="episode-review", *, runner=None,
                            evidence_checks=None):
    """Exercise the new case-level review instead of treating its session as a no-op."""
    return [_preview(call_id), _review_finish(
        call_id + "-finish", runner=runner, evidence_checks=evidence_checks)]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _runner(self, answers, name="run"):
        harness = Harness(case_id="T", run_dir=self.dir / name,
                          cache_dir=self.dir / (name + "-cache"),
                          transport=StubTransport(answers),
                          config=HarnessConfig(context_window=1_000_000))
        runner = CaseRunner(harness=harness, sources=_sources(), units=_units(),
                            case_id="T")
        self.addCleanup(harness.close)
        return runner

    def test_HPL001_zero_event_unit_is_recorded_not_retried(self):
        answers = [
            _finish("f1"),                                   # DU-A: nothing found
            _finish("f2"),                                   # DU-B
            _finish("f3"),                                   # DU-C
            _finish("f4"),                                   # ordering
        ]
        runner = self._runner(answers)
        summary = runner.run_discovery()
        self.assertEqual(summary["units"], 3)
        self.assertEqual(summary["zero_event_units"], 3)
        self.assertEqual(len(runner.discovery), 3)
        artifacts = runner.artifacts()
        self.assertEqual(artifacts["counts"]["events"], 0)
        # No unit disappears from the record just because it produced nothing.
        self.assertEqual(artifacts["discovery"]["units_run"], 3)
        self.assertEqual(artifacts["coverage"]["units_not_run"], 0)

    def test_HPL002_event_with_bad_citation_survives_with_its_problem(self):
        answers = [
            _proposal("e1", "原文里根本没有这句话", "决定手术"),
            _finish("f1"),
            _finish("f2"),
            _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.run_discovery()
        self.assertEqual(len(runner.events), 1)
        event = runner.events[0]
        self.assertEqual(event["status"], "PROPOSED_WITH_PROBLEMS")
        verdicts = [verdict for verdict in event["citation_verdicts"] if not verdict["ok"]]
        self.assertEqual(verdicts[0]["reason"], "QUOTE_NOT_IN_SOURCE")
        record = runner.discovery["DU-A"]
        self.assertEqual(len(record["problems"]), 1)
        artifacts = runner.artifacts()
        self.assertEqual(artifacts["counts"]["with_problems"], 1)
        # It still reaches the next stage rather than being dropped.
        self.assertEqual(artifacts["counts"]["events"], 1)

    def test_HPL003_demotion_annotates_and_event_stays(self):
        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"),
            _finish("f2"),
            _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.run_discovery()
        target = runner.events[0]["event_id"]
        runner.harness.transport.answers = [
            dict(tool_calls=[dict(id="d1", name="demote_event", arguments=dict(
                event_id=target, reason="只是回述",
                basis_quotes=[dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术")]))]),
            _finish("f4"),
        ]
        runner.run_ordering()
        self.assertEqual(len(runner.events), 1)
        event = runner.events[0]
        self.assertTrue(event["demoted"])
        self.assertEqual(event["demotion"]["reason"], "只是回述")
        self.assertEqual(event["demotion"]["discovery_status"], "ACCEPTED")
        runner.run_episodes()
        self.assertEqual(runner.audit["event_count"], 0)
        # The demoted event is still in the artifact, with its earlier verdict.
        self.assertEqual(runner.artifacts()["counts"]["demoted"], 1)

    def test_HPL004_relations_assemble_episodes_and_unresolved_are_kept(self):
        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"),
            _proposal("e2", "讨论后拟行胰十二指肠切除术", "拟行胰十二指肠切除术",
                      source_id="S-B"),
            _finish("f2"),
            _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.run_discovery()
        first = next(event["event_id"] for event in runner.events
                     if event["origin_source_ids"] == ["S-A"])
        second = next(event["event_id"] for event in runner.events
                      if event["origin_source_ids"] == ["S-B"])
        relation_answer = dict(tool_calls=[dict(id="r1", name="record_relation", arguments=dict(
            source_event_id=first, target_event_id=second,
            relation="same_management_process", confidence="supported",
            basis_quotes=[
                dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术"),
                dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")],
            explanation="同一次术前评估"))])
        runner.harness.transport.answers = [
            relation_answer, _finish("f4")] + _episode_review_answers(runner=runner)
        runner.run_ordering()
        runner.run_episodes()
        artifacts = runner.artifacts()
        self.assertEqual(artifacts["counts"]["episodes"], 1)
        self.assertEqual(artifacts["episodes"]["episodes"][0]["event_ids"],
                         [first, second])
        # context_only merges as a candidate and says so.
        self.assertIn("PROCESS_LINK_IS_CANDIDATE_NOT_CONFIRMED",
                      artifacts["episodes"]["episodes"][0]["open_questions"])

    def test_HPL005_uncertain_relation_is_reported_not_silently_dropped(self):
        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"),
            _proposal("e2", "讨论后拟行胰十二指肠切除术", "拟行胰十二指肠切除术",
                      source_id="S-B"),
            _finish("f2"),
            _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.run_discovery()
        first, second = [event["event_id"] for event in runner.events]
        runner.harness.transport.answers = [
            dict(tool_calls=[dict(id="r1", name="record_relation", arguments=dict(
                source_event_id=first, target_event_id=second,
                relation="same_management_process", confidence="uncertain",
                basis_quotes=[dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术")],
                explanation="证据不足"))]),
            _finish("f4")] + _episode_review_answers(runner=runner)
        runner.run_ordering()
        runner.run_episodes()
        artifacts = runner.artifacts()
        self.assertEqual(artifacts["counts"]["episodes"], 2)
        reasons = [item["reason"] for item in artifacts["relations"]["unresolved"]]
        self.assertIn("RELATION_UNCERTAIN", reasons)
        self.assertEqual(artifacts["audit"]["unresolved_relation_count"], 1)

    def test_HPL006_units_not_run_are_named(self):
        runner = self._runner([_finish("f1")])
        runner.run_discovery(only_unit_ids={"DU-A"})
        artifacts = runner.artifacts()
        self.assertEqual(artifacts["coverage"]["units_not_run"], 2)
        self.assertIn("DU-B", artifacts["coverage"]["units_not_run_ids"])

    def test_HPL007_stage_stop_reasons_are_preserved(self):
        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "x"),
            # No finish: the turn ends on the model's declared final object.
            dict(final=dict(note="giving up")),
            _finish("f2"), _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.run_discovery(only_unit_ids={"DU-A"})
        self.assertEqual(runner.stage_stops[0]["stop"], "answered")
        self.assertEqual(len(runner.events), 1)

    def test_HPL010_a_unit_that_never_finishes_is_not_an_empty_finished_unit(self):
        # A readable answer with no `final` and no finish() is not a zero-event
        # unit: the model may simply have stopped halfway. Keep any proposals it
        # did make, but put the stop reason on the record.
        answers = [dict(text="我正在看这份文书，还需要继续核对。")]
        runner = self._runner(answers)
        summary = runner.run_discovery(only_unit_ids={"DU-A"})
        self.assertEqual(summary["units_with_stop"], {"not_finished": 1})
        self.assertEqual(runner.stage_stops[0]["stop"], "not_finished")
        self.assertEqual(summary["zero_event_units"], 1)

    def test_HPL011_one_transport_failure_preserves_partial_work_and_runs_other_units(self):
        # Live r5 counterexample: one unit's three timeouts aborted discovery
        # after nine units and left 28 untouched. A unit failing to finish does
        # not license abandoning the rest, nor turning its partial work into a
        # completed zero-event unit.
        from scripts.cohort_construction.p2_checkpoint_harness.transport import (
            TransportFailed,
        )

        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            TransportFailed(dict(status="FAILED", attempts=[
                dict(attempt=1, reason_code="TRANSPORT_ERROR")])),
            _finish("f2"),
            _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.harness.config.max_transport_recovery_attempts = 0
        summary = runner.run_discovery()
        self.assertEqual(summary["units_with_stop"], {
            "request_failed": 1, "answered": 2})
        self.assertEqual(summary["events"], 1)
        failed = runner.discovery["DU-A"]
        self.assertEqual(failed["stop"], "request_failed")
        self.assertEqual(failed["failure"]["reason_codes"], ["TRANSPORT_ERROR"])
        self.assertEqual(failed["summary"]["proposals"], 1)
        self.assertEqual(runner.artifacts()["coverage"]["units_not_run"], 0)
        self.assertEqual([event["payload"]["stop"] for event in
                          runner.harness.sessions()["discovery-DU-A"].events()
                          if event["type"] == "turn/end"], ["request_failed"])

        harness = Harness(case_id="OTHER", run_dir=self.dir / "other-run",
                          cache_dir=self.dir / "other-cache",
                          transport=StubTransport([_proposal(
                              "e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
                              AssertionError("local bookkeeping defect")]),
                          config=HarnessConfig(context_window=1_000_000))
        self.addCleanup(harness.close)
        runner = CaseRunner(harness=harness, sources=_sources(), units=_units(),
                            case_id="OTHER")
        with self.assertRaises(AssertionError):
            runner.run_discovery(only_unit_ids={"DU-A"})

    def test_HPL012_transport_failure_recovers_in_fresh_bounded_attempt(self):
        # Continuing the other units is only damage limitation. Recovery gives
        # the failed unit a fresh session, while identical local_ids replace
        # the earlier proposal rather than duplicating the event.
        from scripts.cohort_construction.p2_checkpoint_harness.transport import (
            TransportFailed,
        )

        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            TransportFailed(dict(status="FAILED", attempts=[
                dict(attempt=1, reason_code="TRANSPORT_ERROR")])),
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("retry-finish"),
            _finish("f2"), _finish("f3"),
        ]
        runner = self._runner(answers)
        summary = runner.run_discovery()
        self.assertEqual(summary["units_with_stop"], {"answered": 3})
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["transport_recoveries"], 1)
        self.assertEqual(summary["transport_recovered_units"], 1)
        record = runner.discovery["DU-A"]
        self.assertEqual(record["stop"], "answered")
        self.assertEqual(record["summary"]["revisions"], 1)
        self.assertEqual(record["recovery"]["attempts"], 1)
        self.assertEqual(record["recovery"]["recovered"], True)
        self.assertEqual(record["requests"], 4)
        self.assertEqual([event["payload"]["stop"] for event in
                          runner.harness.sessions()["discovery-DU-A"].events()
                          if event["type"] == "turn/end"], ["request_failed"])
        self.assertIn("discovery-DU-A-recovery-1", runner.harness.sessions())
        self.assertEqual(runner.artifacts()["counts"]["events"], 1)

    def test_HPL013_recovery_is_bounded_and_never_swallows_local_defects(self):
        from scripts.cohort_construction.p2_checkpoint_harness.transport import (
            TransportFailed,
        )

        failure = TransportFailed(dict(status="FAILED", attempts=[
            dict(attempt=1, reason_code="TRANSPORT_ERROR")]))
        runner = self._runner([failure, failure, _finish("other-unit")])
        runner.harness.config.max_transport_recovery_attempts = 1
        summary = runner.run_discovery(only_unit_ids={"DU-A"})
        self.assertEqual(summary["units_with_stop"], {"request_failed": 1})
        self.assertEqual(summary["transport_recoveries"], 1)
        self.assertEqual(summary["transport_recovery_exhausted"], 1)
        self.assertEqual(runner.discovery["DU-A"]["recovery"]["recovered"], False)

        harness = Harness(case_id="OTHER", run_dir=self.dir / "other-run",
                          cache_dir=self.dir / "other-cache",
                          transport=StubTransport([
                              TransportFailed(dict(status="FAILED", attempts=[])),
                              AssertionError("local bookkeeping defect")]),
                          config=HarnessConfig(context_window=1_000_000))
        self.addCleanup(harness.close)
        runner = CaseRunner(harness=harness, sources=_sources(), units=_units(),
                            case_id="OTHER")
        with self.assertRaises(AssertionError):
            runner.run_discovery(only_unit_ids={"DU-A"})

    def test_HPL014_ordering_transport_failure_recovers_and_keeps_relations(self):
        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"),
            _finish("f2"), _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.run_discovery()
        from scripts.cohort_construction.p2_checkpoint_harness.transport import (
            TransportFailed,
        )
        runner.harness.transport.answers = [
            TransportFailed(dict(status="FAILED", attempts=[
                dict(attempt=1, reason_code="TRANSPORT_ERROR")])),
            _finish("ordering-recovery"),
        ]
        result = runner.run_ordering()
        self.assertEqual(result["stop"], "answered")
        self.assertEqual(result["recovery"]["attempts"], 1)
        self.assertEqual(result["recovery"]["recovered"], True)
        self.assertIn("ordering-recovery-1", runner.harness.sessions())
        self.assertEqual([event["payload"]["stop"] for event in
                          runner.harness.sessions()["ordering"].events()
                          if event["type"] == "turn/end"], ["request_failed"])

    def test_HPL015_supported_relation_and_demotion_quotes_are_verified(self):
        answers = [
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"),
            _proposal("e2", "讨论后拟行胰十二指肠切除术", "拟行胰十二指肠切除术",
                      source_id="S-B"),
            _finish("f2"), _finish("f3"),
        ]
        runner = self._runner(answers)
        runner.run_discovery()
        first, second = [event["event_id"] for event in runner.events]
        runner.harness.transport.answers = [
            dict(tool_calls=[dict(id="r1", name="record_relation", arguments=dict(
                source_event_id=first, target_event_id=second,
                relation="same_event", confidence="supported",
                basis_quotes=[dict(source_id="S-B", quote="原文不存在")],
                explanation="无引文不能成为同一事件"))]),
            dict(tool_calls=[dict(id="r2", name="record_relation", arguments=dict(
                source_event_id=first, target_event_id=second,
                relation="same_event", confidence="supported",
                basis_quotes=[dict(source_id="S-C", quote="生命体征平稳，继续观察。")],
                explanation="第三份文书不是端点证据"))]),
            dict(tool_calls=[dict(id="r3", name="record_relation", arguments=dict(
                source_event_id=first, target_event_id=second,
                relation="same_event", confidence="supported",
                basis_quotes=[
                    dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术"),
                    dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")],
                explanation="两条端点引文可定位"))]),
            _finish("f4"),
        ] + _episode_review_answers("first-episode-review", runner=runner)
        result = runner.run_ordering()
        self.assertEqual(result["relations"], 1)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["stop"], "answered")
        errors = [event["payload"]["content"] for event in
                  runner.harness.sessions()["ordering"].events()
                  if event["type"] == "tool/result" and event["payload"]["is_error"]]
        self.assertTrue(any("RELATION_BASIS_NOT_VERIFIED" in error for error in errors))
        self.assertTrue(any("RELATION_BASIS_OUTSIDE_ENDPOINT_SOURCES" in error
                            for error in errors))
        runner.run_episodes()
        groups = {event_id: group for episode in runner.artifacts()["episodes"]["episodes"]
                  for group in episode["event_groups"] for event_id in group["event_ids"]}
        self.assertEqual(sorted(groups[first]["event_ids"]), [first])
        self.assertEqual(sorted(groups[second]["event_ids"]), [second])
        self.assertFalse(groups[first]["duplicate_candidate"])
        # A duplicate identity claim alone must not silently merge two separate
        # management processes. Cross-container identity remains auditable below.
        runner.harness.transport.answers = [
            dict(tool_calls=[dict(id="r5", name="record_relation", arguments=dict(
                source_event_id=first, target_event_id=second,
                relation="same_management_process", confidence="supported",
                basis_quotes=[
                    dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术"),
                    dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")],
                explanation="候选过程合并必须另有管理过程关系"))]),
            _finish("f5"),
        ] + _episode_review_answers("second-episode-review", runner=runner)
        runner.run_ordering()
        runner.run_episodes()
        episodes = runner.artifacts()["episodes"]["episodes"]
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(episodes[0]["event_groups"]), 1)
        self.assertEqual(sorted(episodes[0]["event_groups"][0]["event_ids"]),
                         sorted([first, second]))
        self.assertTrue(episodes[0]["event_groups"][0]["duplicate_candidate"])

    def test_HPL017_cross_process_duplicate_identity_is_retained_globally(self):
        runner = self._runner([
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"),
            _proposal("e2", "讨论后拟行胰十二指肠切除术", "拟行胰十二指肠切除术",
                      source_id="S-B"),
            _finish("f2"), _finish("f3"),
        ])
        runner.run_discovery()
        first, second = [event["event_id"] for event in runner.events]
        runner.harness.transport.answers = [
            dict(tool_calls=[dict(id="r1", name="record_relation", arguments=dict(
                source_event_id=first, target_event_id=second,
                relation="same_event", confidence="supported",
                basis_quotes=[
                    dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术"),
                    dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")],
                explanation="同一重复候选，但不自动等同管理过程"))]),
            _finish("f2"),
        ] + _episode_review_answers(runner=runner)
        runner.run_ordering()
        runner.run_episodes()
        artifacts = runner.artifacts()
        self.assertEqual(len(artifacts["episodes"]["episodes"]), 2)
        self.assertEqual(artifacts["episodes"]["duplicate_candidate_groups"], [
            dict(group_id=first, event_ids=sorted([first, second]),
                 duplicate_candidate=True)])

    def test_HPL016_read_unit_exposes_only_that_units_original_text(self):
        runner = self._runner([])
        tool = runner.index.tool_read_unit()
        result = tool.run(unit_id="DU-A")
        self.assertEqual(result["sources"][0]["text"], DOC_A)
        self.assertNotIn(DOC_B, str(result))
        self.assertIn("不是已确认的临床 episode", result["note"])
        from scripts.cohort_construction.p2_checkpoint_harness.tools import ToolError
        with self.assertRaises(ToolError):
            tool.run(unit_id="not-a-unit")

    def test_HPL018_action_origin_comes_from_its_citation_not_reading_assignment(self):
        # Live r9: the admission-note session retrieved the adjacent first-course
        # note. That retrieval must not relabel the cited action as an admission
        # action or lend it the admission note's clock. Nor should the agent have
        # to reject a real, exactly located source merely to satisfy task scope.
        answers = [_proposal("first-course-plan", "讨论后拟行胰十二指肠切除术",
                             source_id="S-B")]
        answers += [_finish("f1"), _finish("f2"), _finish("f3")]
        runner = self._runner(answers)
        runner.run_discovery()
        event = runner.events[0]
        from scripts.cohort_construction.p2_checkpoint_harness.stages.common import (
            event_id,
        )
        self.assertEqual(event["event_id"], event_id(["S-B"], "first-course-plan"))
        self.assertEqual(event["origin_source_ids"], ["S-B"])
        self.assertEqual(event["origin_unit_id"], "DU-B")
        self.assertEqual(event["clock"]["value"], "2026-03-05")
        self.assertEqual(event["clock_basis"], "body_record_time")
        self.assertEqual(event["status"], "ACCEPTED")
        # The discovery assignment remains an audit field, never the object owner.
        self.assertEqual(event["discovery_unit_id"], "DU-A")
        self.assertEqual(event["discovery_source_ids"], ["S-A"])
        self.assertEqual(event["source_ids"], ["S-B"])
        self.assertEqual(event["span"]["source_id"], "S-B")
        self.assertEqual(event["span"]["quote"], "讨论后拟行胰十二指肠切除术")
        self.assertEqual(event["span"]["start"], DOC_B.index("讨论后拟行胰十二指肠切除术"))
        self.assertEqual(event["span"]["end"],
                         DOC_B.index("讨论后拟行胰十二指肠切除术") + len("讨论后拟行胰十二指肠切除术"))
        self.assertEqual(event["attribution_problems"], [])
        self.assertFalse({"QUOTE_OUTSIDE_CURRENT_UNIT", "unit_id", "unit_kind",
                          "document_profile", "unit_source_ids"} & set(event))

    def test_HPL018a_missing_origin_never_borrows_the_assigned_source_or_clock(self):
        runner = self._runner([
            dict(tool_calls=[dict(id="e1", name="propose_candidate", arguments=dict(
                local_id="unanchored", scope="uncertain", record_function="uncertain",
                source_id="", quote="", uncertainty="无法定位"))]),
            _finish("f1"), _finish("f2"), _finish("f3")])
        runner.run_discovery()
        event = runner.events[0]
        self.assertEqual(event["origin_source_ids"], [])
        self.assertEqual(event["attribution_problems"], ["SPAN_ORIGIN_REQUIRED"])
        self.assertIsNone(event["clock"]["value"])
        self.assertIsNone(event["clock_basis"])
        self.assertEqual(event["discovery_source_ids"], ["S-A"])

    def test_HPL019_discovery_does_not_collect_prechoice_or_postchoice_evidence(self):
        answers = [
            dict(tool_calls=[dict(id="e1", name="propose_candidate", arguments=dict(
                local_id="e1", scope="yes", record_function="new_decision",
                action_id="DIAG_PANCREATIC_CONTRAST_CT",
                source_id="S-A", quote="临床团队决定完善增强CT后评估手术"))]),
            _finish("f1"), _finish("f2"), _finish("f3")]
        runner = self._runner(answers)
        runner.run_discovery()
        event = runner.events[0]
        self.assertEqual(event["status"], "ACCEPTED")
        self.assertEqual(event["source_ids"], ["S-A"])
        self.assertEqual(event["origin_source_ids"], ["S-A"])
        self.assertEqual(event["discovery_source_ids"], ["S-A"])
        self.assertEqual(event["clock"]["value"], "2026-03-01")
        self.assertFalse({"management_question", "action", "action_quotes",
                          "prechoice_quotes", "postchoice_quotes", "choice_time",
                          "time_quotes", "evidence_quotes"} & set(event))
        registry = runner.harness.new_registry()
        from scripts.cohort_construction.p2_checkpoint_harness.tools import proposals
        from scripts.cohort_construction.p2_checkpoint_harness.tools.proposals import (
            ProposalStore,
        )
        proposals.register(registry, ProposalStore(source_index=runner.index))
        self.assertNotIn("reject_mention", registry.names())
        self.assertNotIn("propose_event", registry.names())
        with self.assertRaisesRegex(TypeError, "prechoice_quotes"):
            # Legacy evidence-organization fields are not part of this contract.
            registry.get("propose_candidate").run(
                local_id="legacy-evidence", scope="yes", record_function="new_decision",
                action_id="DIAG_PANCREATIC_CONTRAST_CT",
                source_id="S-A", quote="临床团队决定完善增强CT后评估手术",
                prechoice_quotes=[])

    def test_HPL020_event_list_distinguishes_assignment_and_actual_origins(self):
        runner = self._runner([
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"), _finish("f2"), _finish("f3")])
        runner.run_discovery()
        from scripts.cohort_construction.p2_checkpoint_harness.stages.ordering import (
            EventIndex,
        )
        summary = EventIndex(runner.events, source_index=runner.index)._summary(
            runner.events[0])
        self.assertEqual(summary["source_ids"], ["S-A"])
        self.assertEqual(summary["origin_source_ids"], ["S-A"])
        self.assertNotIn("discovery_source_ids", summary)
        self.assertNotIn("discovery_unit_id", summary)
        self.assertEqual(summary["origin_unit_id"], "DU-A")

    def test_HPL021_preview_episodes_is_registered_before_final_assembly(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.episodes import (
            episode_finish_tool,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.stages.episodes import (
            EpisodeBuilder,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.tools import ToolError
        gate = episode_finish_tool(events=[], builder=EpisodeBuilder([], relations=[]))
        with self.assertRaisesRegex(ToolError, "EPISODE_REVIEW_REQUIRES_PREVIEW"):
            gate.run(reviewed_event_ids=["E1"], evidence_checks=[])
        runner = self._runner([
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"), _finish("f2"), _finish("f3")])
        runner.run_discovery()
        runner.harness.transport.answers = [
            _finish("ordering")] + _episode_review_answers(runner=runner)
        runner.run_ordering()
        runner.run_episodes()
        self.assertIn("preview_episodes", runner.harness.sessions()["episodes"].derive_messages()[0]["content"])
        calls = [event["payload"]["name"]
                 for event in runner.harness.sessions()["episodes"].events()
                 if event["type"] == "tool/call"]
        self.assertEqual(calls, ["preview_episodes", "finish"])
        self.assertEqual(runner.artifacts()["episode_review"]["stop"], "answered")

    def test_HPL022_later_no_link_refutes_a_direct_process_edge_without_deleting_history(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages import (
            episodes as E,
            ordering as O,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.tools import (
            ToolError,
            ToolRegistry,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import (
            SourceIndex,
        )
        events = [dict(event_id="E1", clock={}, source_ids=["S-A"],
                       span=dict(source_id="S-A",
                                 quote="临床团队决定完善增强CT后评估手术")),
                  dict(event_id="E2", clock={}, source_ids=["S-B"],
                       span=dict(source_id="S-B",
                                 quote="讨论后拟行胰十二指肠切除术"))]
        registry = ToolRegistry(approve=lambda *args: True)
        recorded = O.relation_tools(registry, events=events,
                                    source_index=SourceIndex(_sources(), units=_units()))
        with self.assertRaisesRegex(ToolError, "REFUTATION_REQUIRES_BASIS"):
            registry.get("record_relation").run(
                source_event_id="E1", target_event_id="E2", relation="none",
                confidence="no_link", explanation="仅同次查房")
        correction = registry.get("record_relation").run(
            source_event_id="E1", target_event_id="E2", relation="none",
            confidence="no_link", explanation="同次查房不等于同一管理目标",
            basis_quotes=[
                dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术"),
                dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")])
        self.assertEqual(correction["relation"]["refutes"],
                         "direct_same_management_process_only")
        old = dict(source_event_id="E1", target_event_id="E2",
                   relation="same_management_process", confidence="supported")
        result = E.EpisodeBuilder(events, relations=[old] + recorded["relations"]).assemble()
        self.assertEqual(len(result["episodes"]), 2)
        self.assertEqual(result["merged_edges"], [])
        self.assertEqual(len(recorded["relations"]), 1)

    def test_HPL023_evidence_reference_does_not_merge_management_processes(self):
        from scripts.cohort_construction.p2_checkpoint_harness.stages.episodes import (
            EpisodeBuilder,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.stages.ordering import (
            relation_tools,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.tools import (
            ToolError, ToolRegistry,
        )
        from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import (
            SourceIndex,
        )
        events = [
            dict(event_id="E1", clock={}, object_kind="evidence_artifact",
                 source_ids=["S-A"], source_inventory=[dict(source_id="S-A")]),
            dict(event_id="E2", clock={}, candidate_kind="management_expression",
                 source_ids=["S-B"], span=dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")),
            dict(event_id="E3", clock={}, candidate_kind="management_expression",
                 source_ids=["S-A"], span=dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术")),
        ]
        registry = ToolRegistry(approve=lambda *args: True)
        recorded = relation_tools(registry, events=events,
                                  source_index=SourceIndex(_sources(), units=_units()))
        relation = registry.get("record_relation")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_ARTIFACT_NOT_MANAGEMENT_ENDPOINT"):
            relation.run(source_event_id="E1", target_event_id="E2",
                         relation="same_management_process", confidence="supported",
                         basis_quotes=[], explanation="证据不是管理过程")
        with self.assertRaisesRegex(ToolError, "EVIDENCE_ARTIFACT_NOT_MANAGEMENT_ENDPOINT"):
            relation.run(source_event_id="E1", target_event_id="E2",
                         relation="same_event", confidence="supported",
                         basis_quotes=[], explanation="证据不是重复管理提案")
        relation.run(
            source_event_id="E1", target_event_id="E2",
            relation="evidence_comparison_previous", confidence="supported",
            basis_quotes=[
                dict(source_id="S-A", quote="复查增强CT示胰头占位"),
                dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")],
            explanation="既往检查所见被后续讨论引用")
        # A missing target fails even when no quotes are supplied.
        with self.assertRaisesRegex(ToolError, "EVIDENCE_REFERENCE_REQUIRES_TARGET"):
            relation.run(source_event_id="E1", target_event_id=None,
                         relation="evidence_comparison_previous", confidence="supported")
        result = EpisodeBuilder(events[:2], relations=recorded["relations"]).assemble()
        self.assertEqual(len(result["episodes"]), 1)
        self.assertEqual(result["evidence_object_ids"], ["E1"])
        self.assertEqual(result["merged_edges"], [])
        self.assertEqual(len(result["evidence_references"]), 1)
        self.assertFalse(result["unresolved_relations"])

    def test_HPL024_episode_transport_recovery_retains_relations_and_is_bounded(self):
        from scripts.cohort_construction.p2_checkpoint_harness.transport import (
            TransportFailed,
        )
        runner = self._runner([
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"),
            _proposal("e2", "讨论后拟行胰十二指肠切除术", "拟行胰十二指肠切除术",
                      source_id="S-B"),
            _finish("f2"), _finish("f3")])
        runner.run_discovery()
        runner.harness.transport.answers = [_finish("ordering")]
        runner.run_ordering()
        first, second = [event["event_id"] for event in runner.events]
        failure = TransportFailed(dict(status="FAILED", attempts=[
            dict(attempt=1, reason_code="TRANSPORT_ERROR")]))
        runner.harness.transport.answers = [
            _preview("initial-preview"),
            dict(tool_calls=[dict(id="correction", name="record_relation", arguments=dict(
                source_event_id=first, target_event_id=second,
                relation="same_management_process", confidence="supported",
                basis_quotes=[
                    dict(source_id="S-A", quote="临床团队决定完善增强CT后评估手术"),
                    dict(source_id="S-B", quote="讨论后拟行胰十二指肠切除术")],
                explanation="术前评估至手术意向"))]),
            failure,
            _preview("recovery-preview"),
            _review_finish("recovery-finish", runner=runner),
        ]
        result = runner.run_episodes()
        review = runner.artifacts()["episode_review"]
        self.assertEqual(result["stop"], "answered")
        self.assertEqual(review["recovery"]["attempts"], 1)
        self.assertTrue(review["recovery"]["recovered"])
        self.assertEqual(review["added_relations"], 1)
        self.assertEqual(len(runner.episodes["episodes"]), 1)
        self.assertIn("episodes-recovery-1", runner.harness.sessions())
        self.assertEqual([event["payload"]["stop"] for event in
                          runner.harness.sessions()["episodes"].events()
                          if event["type"] == "turn/end"], ["request_failed"])

        other = self._runner([
            _proposal("e1", "临床团队决定完善增强CT后评估手术", "决定完善增强CT"),
            _finish("f1"), _finish("f2"), _finish("f3")], name="bounded")
        other.run_discovery()
        other.harness.transport.answers = [_finish("ordering")]
        other.run_ordering()
        other.harness.config.max_transport_recovery_attempts = 1
        other.harness.transport.answers = [_preview("failed-preview"), failure, failure]
        result = other.run_episodes()
        self.assertEqual(result["stop"], "request_failed")
        self.assertTrue(other.artifacts()["episode_review"]["recovery"]["exhausted"])

    def test_HCO007_case_organization_recovers_relations_and_final_state(self):
        from scripts.cohort_construction.p2_checkpoint_harness.transport import (
            TransportFailed,
        )
        runner = self._imaging_runner([
            _proposal("e1", "临床团队决定完善增强CT后评估手术"),
            _finish("f1"), _finish("f2"), _finish("f3"),
            _finish("imaging-finish")])
        runner.run_discovery()
        failure = TransportFailed(dict(status="FAILED", attempts=[
            dict(attempt=1, reason_code="TRANSPORT_ERROR")]))
        runner.harness.transport.answers = [failure, failure, failure]
        result = runner.run_case_organization()
        self.assertEqual(result["stop"], "request_failed")
        self.assertTrue(result["recovery"]["exhausted"])
        self.assertEqual(runner._case_organization["stop"], "request_failed")
        self.assertEqual(len(runner.episodes["episodes"]), 1)

        # A completed recovery can add a directed evidence edge and the final
        # state, rather than a transient failed attempt, controls completion.
        other = self._imaging_runner([
            _proposal("e1", "临床团队决定完善增强CT后评估手术"),
            _finish("f1"),
            _proposal("e2", "讨论后拟行胰十二指肠切除术", source_id="S-B"),
            _finish("f2"), _finish("f3"),
            _finish("imaging-finish")], name="case-recovery-success")
        other.run_discovery()
        target = next(event["event_id"] for event in other.events
                      if event["source_ids"] == ["S-B"])
        evidence = other.evidence_inventory[0]["event_id"]
        other.harness.transport.answers = [failure, dict(tool_calls=[dict(
            id="evidence-link", name="record_relation", arguments=dict(
                source_event_id=evidence, target_event_id=target,
                relation="evidence_used", confidence="uncertain",
                explanation="报告可见性时点早于该文书；不推定实际选择时刻"))]),
            _finish("complete")]
        result = other.run_case_organization()
        self.assertEqual(result["stop"], "answered")
        self.assertEqual(other._case_organization["stop"], "answered")
        self.assertTrue(other._case_organization["recovery"]["recovered"])
        self.assertEqual(len(other.episodes["evidence_references"]), 1)
        self.assertEqual(other.episodes["relation_semantics"], "supported_only")
        self.assertEqual(other.artifacts()["case_organization"]["final"]["note"], "done")
        self.assertIsNone(other.artifacts()["episode_review"])
        self.assertFalse(other.demotions)

    def _imaging_runner(self, answers=None, name="run"):
        sources = _sources()
        units = _units()
        imaging = dict(source_id="S-I", kind="imaging", title="增强CT",
                       text="胰头占位，包绕肠系膜上动脉。",
                       source_sha256="i" * 64,
                       clock=dict(value="2026-03-03", precision="date"),
                       clock_basis="verified_time", clocks={})
        sources.append(imaging)
        units.append(dict(unit_id="TU-I", source_ids=["S-I"],
                          kind="performed_test_report",
                          document_profile="performed_test_report",
                          clock=dict(value="2026-03-03", precision="date"),
                          clock_basis="verified_time",
                          original_texts=[dict(source_id="S-I", text=imaging["text"])]))
        runner = self._runner(answers or [], name=name)
        runner.sources = sources
        runner.units = units
        from scripts.cohort_construction.p2_checkpoint_harness.tools.sources import (
            SourceIndex,
        )
        runner.index = SourceIndex(sources, units=units)
        return runner

    def test_HPL025_program_evidence_inventory_needs_no_fake_management_fields(self):
        runner = self._imaging_runner([_finish("imaging-finish")])
        runner.run_discovery(only_unit_ids={"TU-I"})
        self.assertEqual(runner.events, [])
        event = runner.evidence_inventory[0]
        self.assertEqual(event["status"], "SOURCE_INDEXED")
        self.assertEqual(event["object_kind"], "evidence_artifact")
        self.assertEqual(event["source_ids"], ["S-I"])
        self.assertEqual(event["clock_basis"], "verified_time")
        self.assertEqual(event["evidence_selection_record_status"],
                         "not_assessed_in_discovery")
        self.assertFalse({"management_question", "action", "action_quotes",
                          "evidence_quotes", "report_action_quote_unavailable"}
                         & set(event))
        self.assertEqual(runner.discovery["TU-I"]["summary"]["proposals"], 0)

    def test_HPL026_evidence_coverage_must_be_declared_per_object(self):
        runner = self._imaging_runner([_finish("imaging-finish")])
        runner.run_discovery(only_unit_ids={"TU-I"})
        runner.harness.transport.answers = [_finish("ordering")]
        runner.run_ordering()
        runner.harness.transport.answers = [
            _preview("preview"),
            _review_finish("missing-evidence", runner=runner),
            _review_finish("complete", runner=runner, evidence_checks=[dict(
                event_id=runner.evidence_inventory[0]["event_id"], status="matched",
                source_ids=["S-I"], basis="交付中存在该报告；只核对证据来源，不推定检查选择")])]
        runner.run_episodes()
        errors = [event["payload"]["content"] for event in
                  runner.harness.sessions()["episodes"].events()
                  if event["type"] == "tool/result" and event["payload"]["is_error"]]
        self.assertTrue(any("EPISODE_EVIDENCE_COVERAGE_MISSING" in error for error in errors))
        self.assertEqual(runner.artifacts()["episode_review"]["stop"], "answered")
        self.assertEqual(len(runner.artifacts()["episode_review"]["final"]["evidence_checks"]), 1)
        self.assertEqual(runner.artifacts()["episodes"]["episodes"], [])
        self.assertEqual(runner.artifacts()["episodes"]["evidence_object_ids"],
                         [runner.evidence_inventory[0]["event_id"]])
        self.assertEqual(runner.artifacts()["audit"]["event_count"], 1)

    def test_HPL009_units_share_one_case_budget(self):
        # Each unit gets its own session, but not its own allowance: the
        # authorization names a budget for the case. With a per-agent budget,
        # 37 units would each look compliant while the case spent 37 times the
        # granted amount — a cap that bounds nothing, which is exactly the
        # frozen batch's failure mode.
        answers = [_finish("f1"), _finish("f2"), _finish("f3"), _finish("f4")]
        harness = Harness(case_id="T", run_dir=self.dir / "run",
                          cache_dir=self.dir / "cache",
                          transport=StubTransport(answers),
                          config=HarnessConfig(context_window=1_000_000,
                                               max_requests=2,
                                               max_total_tokens=1_000_000))
        self.addCleanup(harness.close)
        runner = CaseRunner(harness=harness, sources=_sources(), units=_units(),
                            case_id="T")
        summary = runner.run_discovery()
        self.assertEqual(summary["units"], 3)
        # Two requests for the whole case: the third unit is refused, and the
        # refusal is recorded rather than the unit silently vanishing.
        self.assertEqual(harness.spend()["requests"], 2)
        self.assertEqual(summary["units_with_stop"], {"answered": 2,
                                                      "request_budget": 1})
        self.assertEqual(runner.artifacts()["discovery"]["units_run"], 3)

    def test_HPL008_each_unit_has_its_own_session(self):
        answers = [_finish("f1"), _finish("f2"), _finish("f3"), _finish("f4")]
        runner = self._runner(answers)
        runner.run_discovery()
        runner.harness.transport.answers = [_finish("f5")]
        runner.run_ordering()
        names = sorted(runner.harness.sessions())
        self.assertEqual(names, ["discovery-DU-A", "discovery-DU-B",
                                 "discovery-DU-C", "ordering"])
        # Unit B never saw unit A's document in its history.
        second = runner.harness.sessions()["discovery-DU-B"]
        joined = "\n".join(message["content"]
                           for message in second.derive_messages())
        self.assertIn(DOC_B.strip(), joined)
        self.assertNotIn(DOC_A.strip(), joined)


if __name__ == "__main__":
    unittest.main()
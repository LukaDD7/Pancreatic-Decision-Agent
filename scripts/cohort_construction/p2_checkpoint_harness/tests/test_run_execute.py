"""One full executed run against a stub provider: files, counts, and refusals.

Nothing here contacts a provider or reads a credential: the transport class and
the settings loader are replaced before the entry point reaches them.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.cohort_construction.p2_checkpoint_harness import run_case
from scripts.cohort_construction.p2_checkpoint_harness.tests.test_run_case import (
    synthetic_bundle,
)


class StubTransport:
    """Alternates propose-then-finish. Never leaves the process."""

    def __init__(self, cache, settings, *, config=None):
        self.receipts = []
        self._proposing = True
        self._episode_event_ids = None
        self._episode_previewed = False

    def complete(self, messages, *, tools=None, task="agent_step", purpose=None):
        self.receipts.append(dict(task=task, usage=dict(prompt_tokens=7,
                                                        completion_tokens=3)))
        if task == "compaction_summary":
            return dict(summary="压缩"), dict(usage=dict(prompt_tokens=1))
        if any(isinstance(tool, dict) and tool.get("name") == "preview_episodes"
               for tool in tools or ()):
            if self._episode_event_ids is None:
                self._episode_event_ids = []
            if self._episode_previewed:
                self._episode_event_ids = []
                for message in messages:
                    if message.get("role") != "tool":
                        continue
                    try:
                        result = json.loads(message.get("content") or "{}")
                    except ValueError:
                        continue
                    for episode in result.get("episodes", []):
                        self._episode_event_ids.extend(episode.get("event_ids") or [])
                self._episode_event_ids = sorted(set(self._episode_event_ids))
            if not self._episode_previewed:
                self._episode_previewed = True
                return (dict(tool_calls=[dict(
                    id="preview", name="preview_episodes", arguments={})]),
                    dict(usage=dict(prompt_tokens=7, completion_tokens=3)))
            return (dict(tool_calls=[dict(id="f1", name="finish", arguments=dict(
                note="stub review", reviewed_event_ids=self._episode_event_ids,
                evidence_checks=[]))]),
                dict(usage=dict(prompt_tokens=7, completion_tokens=3)))
        if self._proposing:
            self._proposing = False
            return (dict(tool_calls=[dict(
                id="p1", name="propose_candidate", arguments=dict(
                    local_id="e1", scope="yes", record_function="new_decision",
                    action_type="ORDER_DIAGNOSTIC_TEST", target="增强CT",
                    source_id="SYN-1",
                    quote="临床团队决定完善增强CT后评估手术"))]),
                dict(usage=dict(prompt_tokens=7, completion_tokens=3)))
        self._proposing = True
        return (dict(tool_calls=[dict(id="f1", name="finish", arguments={})]),
                dict(usage=dict(prompt_tokens=7, completion_tokens=3)))


class TrajectoryStubTransport(StubTransport):
    def __init__(self, cache, settings, *, config=None):
        super().__init__(cache, settings, config=config)
        self._listed = False
        self._current_read = False
        self._source_read = False
        self._adjudicated = False
        self._checkpoint_id = None
        self._source_id = None

    def complete(self, messages, *, tools=None, task="agent_step", purpose=None):
        if not any(isinstance(tool, dict) and tool.get("name") == "adjudicate_checkpoint"
                   for tool in tools or ()):
            return super().complete(messages, tools=tools, task=task, purpose=purpose)
        self.receipts.append(dict(task=task, usage=dict(prompt_tokens=7, completion_tokens=3)))
        usage = dict(usage=dict(prompt_tokens=7, completion_tokens=3))
        if not self._listed:
            self._listed = True
            return dict(tool_calls=[dict(id="list-cp", name="list_candidate_checkpoints",
                                         arguments=dict(limit=200))]), usage
        if self._checkpoint_id is None:
            for message in reversed(messages):
                if message.get("role") != "tool":
                    continue
                try:
                    result = json.loads(message.get("content") or "{}")
                except ValueError:
                    continue
                management = [item for item in result.get("checkpoints") or []
                              if item.get("kind") == "MANAGEMENT_REASSESSMENT"]
                if management:
                    self._checkpoint_id = management[0]["checkpoint_id"]
                    self._source_id = management[0]["source_ids"][0]
                    break
        assert self._checkpoint_id is not None
        if not self._current_read:
            self._current_read = True
            return dict(tool_calls=[dict(
                id="read-current-cp", name="read_candidate_checkpoint",
                arguments=dict(checkpoint_id=self._checkpoint_id))]), usage
        if not self._source_read:
            self._source_read = True
            return dict(tool_calls=[dict(
                id="read-current-source", name="read_source",
                arguments=dict(source_id=self._source_id))]), usage
        if not self._adjudicated:
            self._adjudicated = True
            return dict(tool_calls=[dict(
                id="adjudicate-cp", name="adjudicate_checkpoint",
                arguments=dict(checkpoint_id=self._checkpoint_id, disposition="RETAIN_CP",
                               evolution="FIRST_VISIBLE", rationale="合成首见锚点。"))]), usage
        return dict(tool_calls=[dict(id="finish-cp", name="finish", arguments={})]), usage


class ExecutedRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.sources = self.dir / "source_fhir_r4_bundle.json"
        self.sources.write_text(json.dumps(synthetic_bundle(), ensure_ascii=False),
                                encoding="utf-8")
        self.output = self.dir / "out"
        self.cache = self.dir / "cache"
        self.authorization = self.dir / "auth.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _authorize(self, stages="legacy"):
        args = type("A", (), dict(sources=self.sources, case_id="SYN-01",
                                  config=None, stages=stages))()
        contract, _ = run_case.run_contract(args, run_case.build_plan(self.sources))
        authorization = dict(run_case.AUTH_REQUIRED, execution_scope=contract["execution_scope"],
                             case_id="SYN-01",
                             sources_sha256=contract["sources_sha256"],
                             config_sha256=contract["config_sha256"],
                             code_sha256=run_case._code_digest(contract["code_hashes"]),
                             limits=dict(max_requests=40, max_total_tokens=100000))
        self.authorization.write_text(json.dumps(authorization), encoding="utf-8")

    def _run(self, stages="legacy", discovery_result=None, resume=False):
        argv = ["--sources", str(self.sources), "--output", str(self.output),
                "--cache", str(self.cache), "--case-id", "SYN-01",
                "--stages", stages, "--authorization", str(self.authorization), "--execute"]
        if discovery_result is not None:
            argv += ["--discovery-result", str(discovery_result)]
        if resume:
            argv += ["--resume"]
        return run_case.main(argv)

    def test_HRN001_executed_run_writes_new_files_and_keeps_events(self):
        self._authorize()
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   StubTransport):
            code = self._run()
        self.assertEqual(code, 0)
        written = {path.name for path in self.output.iterdir()}
        for name in ("run_contract.json", "case_result.json", "discovery.json",
                     "relations.json", "candidate_containers.json", "audit.json",
                     "model_receipts.json", "summary.json"):
            self.assertIn(name, written)
        result = json.loads((self.output / "case_result.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(result["counts"]["events"], 1)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["clinical_accuracy_measured"])
        self.assertFalse(summary["reference_read_by_pipeline"])
        self.assertIn("plan", summary)
        self.assertEqual(summary["status"], "COMPLETE")
        self.assertEqual(summary["unfinished"], [])
        # The spend ledger is on the record, so a later reader can see what the
        # case cost without re-reading every receipt.
        self.assertGreater(summary["budget"]["requests"], 0)
        self.assertLessEqual(summary["budget"]["requests"],
                             summary["budget"]["max_requests"])

    def test_trajectory_run_invokes_global_adjudication_after_discovery(self):
        self._authorize("trajectory")
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   TrajectoryStubTransport):
            code = self._run("trajectory")

        self.assertEqual(code, 0)
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(list(summary["stages"]), ["discovery", "checkpoint_adjudication"])
        self.assertEqual(summary["status"], "COMPLETE")
        self.assertTrue(summary["stages"]["checkpoint_adjudication"]["complete"])
        result = json.loads((self.output / "case_result.json").read_text())
        self.assertEqual(len(result["checkpoint_adjudication"]["items"]), 1)
        self.assertEqual(result["checkpoint_adjudication"]["items"][0]["evolution"],
                         "FIRST_VISIBLE")

    def test_adjudication_only_reuses_bound_discovery_without_rerunning_it(self):
        quote = "临床团队决定完善增强CT后评估手术"
        frozen = self.dir / "fresh-discovery.json"
        frozen.write_text(json.dumps(dict(
            candidate_spans=[dict(
                event_id="EV-FROZEN", status="ACCEPTED", scope="yes", demoted=False,
                record_function="new_decision", clock=dict(value="2026-03-01"),
                candidate_action=dict(action_type="ORDER_DIAGNOSTIC_TEST", target="增强CT"),
                span=dict(source_id="SYN-1", quote=quote, start=0, end=len(quote)))],
            out_of_scope_candidates=[], evidence_inventory=[],
            diagnostic_candidate_reviews=[], diagnostic_candidate_review_revisions=[],
            discovery=dict(units={})), ensure_ascii=False))
        args = type("A", (), dict(
            sources=self.sources, case_id="SYN-01", config=None,
            stages="adjudication", discovery_result=frozen))()
        contract, binding = run_case.run_contract(args, run_case.build_plan(self.sources))
        self.assertIn("discovery_result_sha256", binding)
        self.authorization.write_text(json.dumps(dict(
            run_case.AUTH_REQUIRED, execution_scope=contract["execution_scope"],
            case_id="SYN-01", sources_sha256=contract["sources_sha256"],
            config_sha256=contract["config_sha256"],
            discovery_result_sha256=contract["discovery_result_sha256"],
            code_sha256=run_case._code_digest(contract["code_hashes"]),
            limits=dict(max_requests=40, max_total_tokens=100000))))
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   TrajectoryStubTransport):
            self.assertEqual(self._run("adjudication", frozen), 0)
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(list(summary["stages"]), ["discovery_input", "checkpoint_adjudication"])
        self.assertEqual(summary["status"], "COMPLETE")
        sessions = {path.name for path in (self.output / "session" / "sessions").iterdir()}
        self.assertEqual(sessions, {"checkpoint_adjudication.jsonl"})

    def test_discovery_clock_preserves_raw_precision_evidence(self):
        from scripts.cohort_construction.p2_checkpoint_harness.pipeline import _clock_of
        clock = _clock_of(dict(clock=dict(
            value="2026-03-01 10:00:00", precision="minute", raw="2026-03-01 10:00")))
        self.assertEqual(clock["raw"], "2026-03-01 10:00")

    def test_trajectory_incomplete_adjudication_cannot_be_complete_or_rendered(self):
        self._authorize("trajectory")

        def adjudicate(runner):
            runner.checkpoint_adjudication = dict(
                format="p2-checkpoint-adjudication-0.1.0", case_id=runner.case_id,
                items=[], summary=dict(stop="answered", complete=False,
                                       missing_checkpoint_ids=["CP-M-MISSING"]))
            runner.stage_stops.append(dict(stage="checkpoint_adjudication", stop="answered"))
            return dict(stop="answered", complete=False)

        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   StubTransport), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.pipeline.CaseRunner."
                   "run_checkpoint_adjudication", adjudicate):
            self.assertEqual(self._run("trajectory"), 1)

        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(summary["status"], "INCOMPLETE")
        self.assertEqual(summary["unfinished"][0]["kind"],
                         "checkpoint_adjudication_incomplete")
        from scripts.cohort_construction.p2_checkpoint_harness.render_candidate_materials import (
            render_candidate_materials,
        )
        with self.assertRaisesRegex(ValueError, "CHECKPOINT_ADJUDICATION_INCOMPLETE_OR_INVALID"):
            render_candidate_materials(
                case_result=self.output / "case_result.json", source_bundle=self.sources,
                output=self.dir / "materials")

    def _run_with(self, patch_transport, stages="legacy"):
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   patch_transport):
            return self._run(stages)

    def test_HRN004_discovery_only_writes_no_ordering_or_episode_calls(self):
        self._authorize("discovery")
        self.assertEqual(self._run_with(StubTransport, "discovery"), 0)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(list(summary["stages"]), ["discovery"])
        self.assertEqual(summary["status"], "COMPLETE")
        sessions = {path.name for path in (self.output / "session" / "sessions").iterdir()}
        self.assertFalse(any("ordering" in name or "episode" in name for name in sessions))
        self.assertFalse(any("case_organization" in name for name in sessions))

    def test_HRN005_case_organization_is_one_stage_after_discovery(self):
        self._authorize("case")
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   StubTransport):
            self.assertEqual(self._run_with(StubTransport, "case"), 0)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(list(summary["stages"]), ["discovery", "case_organization"])
        sessions = {path.name for path in (self.output / "session" / "sessions").iterdir()}
        self.assertEqual(sum(name.startswith("case_organization") for name in sessions), 1)
        self.assertFalse(any("ordering" in name or name.startswith("episodes") for name in sessions))

    def test_HRN006_organization_only_reuses_frozen_discovery_without_provider_discovery(self):
        discovery = self.dir / "frozen.json"
        discovery.write_text(json.dumps(dict(
            candidate_spans=[], out_of_scope_candidates=[], evidence_inventory=[],
            diagnostic_candidate_reviews=[dict(review_id="DR-1", disposition="no")],
            diagnostic_candidate_review_revisions=[dict(review_id="DR-1", revision=1)],
            discovery=dict(units={}))))
        args = type("A", (), dict(sources=self.sources, case_id="SYN-01", config=None,
                                  stages="organization", discovery_result=discovery))()
        contract, binding = run_case.run_contract(args, run_case.build_plan(self.sources))
        authorization = dict(run_case.AUTH_REQUIRED, execution_scope=contract["execution_scope"],
                             case_id="SYN-01", sources_sha256=contract["sources_sha256"],
                             config_sha256=contract["config_sha256"],
                             code_sha256=run_case._code_digest(contract["code_hashes"]),
                             discovery_result_sha256=contract["discovery_result_sha256"],
                             limits=dict(max_requests=40, max_total_tokens=100000))
        self.authorization.write_text(json.dumps(authorization), encoding="utf-8")
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   StubTransport):
            self.assertEqual(self._run("organization", discovery), 0)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(list(summary["stages"]), ["discovery_input", "case_organization"])
        self.assertFalse(summary["stages"]["discovery_input"]["discovery_rerun"])
        restored = json.loads((self.output / "case_result.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["diagnostic_candidate_reviews"][0]["review_id"], "DR-1")
        self.assertEqual(restored["diagnostic_candidate_review_revisions"][0]["revision"], 1)
        sessions = {path.name for path in (self.output / "session" / "sessions").iterdir()}
        self.assertEqual(sessions, {"case_organization.jsonl"})

    def test_HRN007_interrupted_run_resume_preserves_history_and_writes_new_attempt(self):
        discovery = self.dir / "frozen.json"
        discovery.write_text(json.dumps(dict(
            candidate_spans=[], out_of_scope_candidates=[], evidence_inventory=[],
            discovery=dict(units={}))))
        args = type("A", (), dict(sources=self.sources, case_id="SYN-01", config=None,
                                  stages="organization", discovery_result=discovery))()
        contract, binding = run_case.run_contract(args, run_case.build_plan(self.sources))
        authorization = dict(run_case.AUTH_REQUIRED, execution_scope=contract["execution_scope"],
                             case_id="SYN-01", sources_sha256=contract["sources_sha256"],
                             config_sha256=contract["config_sha256"],
                             code_sha256=run_case._code_digest(contract["code_hashes"]),
                             discovery_result_sha256=contract["discovery_result_sha256"],
                             limits=dict(max_requests=40, max_total_tokens=100000))
        self.authorization.write_text(json.dumps(authorization), encoding="utf-8")
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   StubTransport):
            self.assertEqual(self._run("organization", discovery), 0)
        original_summary = self.output / "summary.json"
        original = json.loads(original_summary.read_text())
        original["status"] = "INCOMPLETE"
        original_summary.write_text(json.dumps(original), encoding="utf-8")
        original_bytes = original_summary.read_bytes()
        (self.output / "case_result.json").unlink()
        with patch("scripts.cohort_construction.model_api.load_boyu_settings",
                   lambda *a, **k: object()), \
             patch("scripts.cohort_construction.p2_checkpoint_harness.transport.Transport",
                   StubTransport):
            self.assertEqual(self._run("organization", discovery, resume=True), 0)
        resumed = self.output.with_name(self.output.name + "-resumed-01")
        self.assertTrue((resumed / "summary.json").is_file())
        self.assertEqual(original_summary.read_bytes(), original_bytes)
        self.assertTrue((resumed / "resume_audit.json").is_file())


    def test_HRN003_a_case_that_ran_out_of_budget_is_not_complete(self):
        # The dangerous direction: three stages ran, so a status of COMPLETE
        # would look right while units were never judged.
        class ExpensiveTransport(StubTransport):
            def complete(self, messages, *, tools=None, task="agent_step",
                         purpose=None):
                result, receipt = super().complete(
                    messages, tools=tools, task=task, purpose=purpose)
                usage = dict(prompt_tokens=1000, completion_tokens=0)
                self.receipts[-1]["usage"] = usage
                return result, dict(receipt, usage=usage)

        self._authorize()
        auth = json.loads(self.authorization.read_text(encoding="utf-8"))
        auth["limits"] = dict(max_requests=40, max_total_tokens=1000)
        self.authorization.write_text(json.dumps(auth), encoding="utf-8")
        code = self._run_with(ExpensiveTransport)
        self.assertEqual(code, 1)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "INCOMPLETE")
        kinds = [row["kind"] for row in summary["unfinished"]]
        self.assertIn("stage_stop", kinds)
        # Every stage stop here is a budget stop: no unit was judged, and the
        # record says why rather than showing a short but clean result.
        self.assertEqual({row["stop"] for row in summary["unfinished"]
                          if row["kind"] == "stage_stop"}, {"token_budget"})

    def test_HRN002_missing_authorization_files_nothing_but_the_contract(self):
        with self.assertRaises(SystemExit):
            self._run()
        # The contract is written before the authorization check only when an
        # authorization was supplied; without one the run stops before any file.
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
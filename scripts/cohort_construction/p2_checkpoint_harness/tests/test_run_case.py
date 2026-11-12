"""run_case: plan, contract, authorization, and a full stub run into a new dir.

The bundle below is synthetic: invented identifiers, invented text, no patient.
The point is to exercise the entry point's refusals and the end-to-end wiring,
not to say anything about any real record.
"""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import EXT
from scripts.cohort_construction.p2_checkpoint_harness import run_case
from scripts.cohort_construction.p2_checkpoint_harness import pipeline as P


DOC_TEXTS = {
    "SYN-1": "2026-03-01 病程记录\n复查增强CT示胰头占位，临床团队决定完善增强CT后评估手术。\n",
    "SYN-2": "2026-03-05 术前讨论\n讨论后拟行胰十二指肠切除术。\n",
}


def _source_resource(source_id, title, text, clock):
    payload = dict(
        family="synthetic", clocks={}, provenance={"synthetic": True},
        source_id=source_id, source_kind="document", source_title=title,
        source_fields={}, clock=clock, clock_basis="body_record_time",
        time_provenance={}, flags=[], source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        availability_certified=False)
    return dict(
        resourceType="DocumentReference", id=source_id,
        status="current", subject={"reference": "Patient/SYN-PATIENT"},
        type={"text": title},
        content=[{"attachment": {"data": base64.b64encode(text.encode()).decode(),
                                  "contentType": "text/plain"}}],
        extension=[{"url": EXT + "source-clock-and-provenance",
                    "valueString": json.dumps(payload)}])


def synthetic_bundle():
    entries = [{"resource": {"resourceType": "Patient", "id": "SYN-PATIENT"}}]
    for source_id, text in DOC_TEXTS.items():
        title = text.split("\n")[0].split(" ", 1)[1]
        clock = {"value": text[:10], "precision": "date", "raw": text[:10]}
        entries.append({"resource": _source_resource(source_id, title, text, clock)})
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


class RunCaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.sources = self.dir / "source_fhir_r4_bundle.json"
        self.sources.write_text(json.dumps(synthetic_bundle(), ensure_ascii=False),
                                encoding="utf-8")
        self.output = self.dir / "out"
        self.cache = self.dir / "cache"

    def tearDown(self):
        self._tmp.cleanup()

    def _args(self, *extra):
        return ["--sources", str(self.sources), "--output", str(self.output),
                "--cache", str(self.cache), "--case-id", "SYN-01", *extra]

    def test_HRC001_dry_run_prints_a_contract_and_writes_nothing(self):
        code = run_case.main(self._args("--dry-run"))
        self.assertEqual(code, 0)
        self.assertFalse(self.output.exists())

    def test_HRC002_plan_reads_the_bundle_without_a_provider(self):
        plan = run_case.build_plan(self.sources)
        self.assertEqual(plan["sources"], 2)
        self.assertEqual(plan["units"], 2)

    def test_HRC003_code_hashes_cover_tests_independently(self):
        hashes = run_case.code_hashes()
        self.assertIn("scripts/cohort_construction/p2_checkpoint_harness/agent.py", hashes)
        self.assertIn("scripts/cohort_construction/p2_checkpoint_harness/skills/discovery/SKILL.md",
                      hashes)
        self.assertEqual(len({value for value in hashes.values()}), len(hashes))

    def test_HRC004_execute_without_authorization_is_refused_before_provider(self):
        with self.assertRaises(SystemExit) as caught:
            run_case.main(self._args("--execute"))
        self.assertIn("EXECUTION_AUTHORIZATION_REQUIRED", str(caught.exception))

    def test_HRC005_authorization_must_bind_this_exact_run(self):
        contract = run_case.run_contract(_Namespace(self._args()), run_case.build_plan(self.sources))
        bad = dict(run_case.AUTH_REQUIRED, execution_scope=run_case.EXECUTION_SCOPES["legacy"],
                   sources_sha256="0" * 64,
                   config_sha256=None, case_id="SYN-01",
                   code_sha256=run_case._code_digest(contract["code_hashes"]),
                   limits=dict(max_requests=10, max_total_tokens=1000))
        with self.assertRaises(SystemExit) as caught:
            run_case.validate_authorization(bad, contract)
        self.assertIn("BINDING_MISMATCH", str(caught.exception))

    def test_HRC006_authorization_cannot_claim_clinical_release(self):
        contract = run_case.run_contract(_Namespace(self._args()), run_case.build_plan(self.sources))
        authorization = dict(run_case.AUTH_REQUIRED, execution_scope=contract["execution_scope"],
                             sources_sha256=contract["sources_sha256"],
                             config_sha256=contract["config_sha256"],
                             case_id="SYN-01",
                             code_sha256=run_case._code_digest(contract["code_hashes"]),
                             limits=dict(max_requests=10, max_total_tokens=1000),
                             clinical_release_permitted=True)
        with self.assertRaises(SystemExit) as caught:
            run_case.validate_authorization(authorization, contract)
        self.assertIn("GOVERNANCE_MISMATCH", str(caught.exception))

    def test_HRC007_limits_are_required(self):
        contract = run_case.run_contract(_Namespace(self._args()), run_case.build_plan(self.sources))
        authorization = dict(run_case.AUTH_REQUIRED, execution_scope=contract["execution_scope"],
                             sources_sha256=contract["sources_sha256"],
                             config_sha256=contract["config_sha256"],
                             case_id="SYN-01",
                             code_sha256=run_case._code_digest(contract["code_hashes"]))
        with self.assertRaises(SystemExit) as caught:
            run_case.validate_authorization(authorization, contract)
        self.assertIn("LIMITS_REQUIRED", str(caught.exception))

    def test_HRC008_complete_case_receipt_blocks_a_rerun(self):
        receipt = self.dir / "receipt.json"
        receipt.write_text(json.dumps(dict(status="COMPLETE")), encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            run_case.main(self._args("--dry-run", "--case-receipt", str(receipt)))
        self.assertIn("CASE_ALREADY_COMPLETE", str(caught.exception))

    def test_HRC009_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        (self.output / "summary.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            run_case.main(self._args("--dry-run"))
        self.assertIn("OUTPUT_DIRECTORY_NOT_EMPTY", str(caught.exception))

    def test_HRC010_unknown_config_field_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            run_case._harness_config(dict(context_windows=1000),
                                     dict(max_requests=5, max_total_tokens=100))
        self.assertIn("HARNESS_CONFIG_UNKNOWN_FIELDS", str(caught.exception))
    def test_HRC013_scope_is_bound_to_selected_stages_and_wrong_type_cannot_pass(self):
        contract = run_case.run_contract(
            _Namespace(self._args("--stages", "discovery")), run_case.build_plan(self.sources))
        self.assertEqual(contract["execution_scope"], run_case.EXECUTION_SCOPES["discovery"])
        authorization = dict(run_case.AUTH_REQUIRED, execution_scope=contract["execution_scope"],
                             sources_sha256=contract["sources_sha256"], config_sha256=None,
                             case_id="SYN-01", code_sha256=run_case._code_digest(contract["code_hashes"]),
                             limits=dict(max_requests=10, max_total_tokens=1000))
        run_case.validate_authorization(authorization, contract)
        for bad_scope in (True, run_case.EXECUTION_SCOPES["legacy"]):
            with self.assertRaisesRegex(SystemExit, "GOVERNANCE_MISMATCH:execution_scope"):
                run_case.validate_authorization(dict(authorization, execution_scope=bad_scope), contract)

    def test_HRC011_recovery_history_does_not_override_final_unit_state(self):
        runner = type("Runner", (), dict(discovery={
            "DU-RECOVERED": dict(stop="answered"),
            "DU-EXHAUSTED": dict(stop="request_failed")}))()
        recovered = dict(stage="discovery", unit_id="DU-RECOVERED",
                         stop="request_failed", recovery_attempt=1)
        exhausted = dict(stage="discovery", unit_id="DU-EXHAUSTED",
                         stop="request_failed", recovery_attempt=2)
        self.assertTrue(run_case._latest_stage_succeeded(runner, recovered))
        self.assertFalse(run_case._latest_stage_succeeded(runner, exhausted))
        history = [recovered, exhausted,
                   dict(stage="discovery", unit_id="DU-RECOVERED", stop="answered"),
                   dict(stage="ordering", stop="answered"),
                   dict(stage="episodes", stop="assembled")]
        current = [stop for stop in history
                   if not (stop.get("recovery_attempt") is not None
                           and stop.get("stop") not in run_case.STAGE_DONE
                           and run_case._latest_stage_succeeded(runner, stop))]
        artifacts = dict(stage_stops=current, stage_stop_history=history,
                         coverage=dict(units_not_run=0))
        self.assertEqual(run_case._unfinished(artifacts), [
            dict(kind="stage_stop", **exhausted)])
        self.assertEqual(len(history), 5)

    def test_HRC012_ordering_recovery_uses_its_latest_attempt_not_history(self):
        runner = type("Runner", (), dict(discovery={}, _ordering_records=[
            dict(stop="request_failed"), dict(stop="answered")]))()
        recovered = dict(stage="ordering", stop="request_failed", recovery_attempt=1)
        self.assertTrue(run_case._latest_stage_succeeded(runner, recovered))
        runner._ordering_records.append(dict(stop="request_failed"))
        self.assertFalse(run_case._latest_stage_succeeded(runner, recovered))

    def test_HRC014_case_organization_recovery_uses_final_state(self):
        runner = type("Runner", (), dict(discovery={}, _case_organization=dict(stop="answered")))()
        recovered = dict(stage="case_organization", stop="request_failed", recovery_attempt=1)
        self.assertTrue(run_case._latest_stage_succeeded(runner, recovered))
        runner._case_organization["stop"] = "request_failed"
        self.assertFalse(run_case._latest_stage_succeeded(runner, recovered))


class _Namespace:
    """argparse-shaped stand-in for the fields run_contract reads."""

    def __init__(self, argv):
        self.sources = Path(argv[1])
        self.case_id = argv[7]
        self.config = None
        self.stages = argv[argv.index("--stages") + 1] if "--stages" in argv else "legacy"


if __name__ == "__main__":
    unittest.main()
"""Synthetic receipt/lineage controls; no credentials or network requests."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint_v04.evaluate_layers import evaluate_layers
from . import evaluate as module


ENDPOINT = "https://apicz.boyuerichdata.com/v1/chat/completions"


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def cached(cache, task, payload, proposal, model="qwen3.8-max"):
    request = {"model": model, "messages": [
        {"role": "system", "content": "Synthetic source-bound task."},
        {"role": "user", "content": json.dumps({"task": task, "input": payload}, ensure_ascii=False)}]}
    key = digest([ENDPOINT, request])
    result = {"items": [proposal]}
    receipt = {"request_sha256": key, "task": task, "status": "SUCCESS", "model": model}
    write(Path(cache) / (key + ".json"), {"request": request, "result": result,
        "result_sha256": digest(result), "receipt": receipt})
    return key, receipt


def fixture(cache):
    text = "当前状态平稳。决定继续治疗。"
    source = {"source_id": "S", "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
              "text": text, "kind": "document", "patient_uid": "fictional"}
    anchor = {"source_id": "S", "source_sha256": source["source_sha256"],
              "start": 0, "end": 7, "quote": text[:7]}
    block = {**anchor, "text": text[:7], "anchor": anchor}
    pool = [{"block_id": "S:0:7", "source_id": "S", "start": 0, "end": 7, "text": text[:7]}]
    payload = {"events": [{"event": {"event_id": "E1"}, "evidence_context": {"block_ids": ["S:0:7"]}}],
        "evidence_source_pool": pool, "current_full_document": [{"source_id": "S", "text": text}],
        "earlier_event_ledger": []}
    proposal = {"event_id": "E1", "scope": "yes", "record_function": "new_decision", "checkpoint": "new_evidence"}
    original_hash, original_receipt = cached(cache, module.PRIMARY_TASK, payload, proposal)
    original = {"event_id": "E1", "scope": "yes", "record_function": "new_decision",
        "checkpoint_status": "CANDIDATE_NEW_EVIDENCE", "semantic_review": proposal,
        "action_citations": {"valid": [{**anchor, "start": 7, "end": len(text), "quote": text[7:]}]},
        "actual_model_evidence": [block], "evidence_context": {"blocks": [block], "allowed_spans": {"S": [{"start": 0, "end": 7}]}},
        "review_request_sha256": original_hash}
    repair_payload = deepcopy(payload)
    repair_payload["repair_contract"] = {"maximum_semantic_repair_rounds": 1,
        "original_evidence_pool_sha256": digest(pool), "source_context_added": False}
    repair_payload["validation_feedback"] = [{"event_id": "E1", "reason": "Synthetic proof mismatch"}]
    repaired_proposal = dict(proposal, checkpoint="uncertain")
    repair_hash, repair_receipt = cached(cache, module.REPAIR_TASK, repair_payload, repaired_proposal)
    repaired = dict(deepcopy(original), review_request_sha256=repair_hash, semantic_review=repaired_proposal,
        review_refinement={"attempted": True, "rounds": 1, "status": "MODEL_REVISION_VALIDATED",
            "original_review_request_sha256": original_hash, "repair_request_sha256": repair_hash})
    return {"source": source, "original": original, "repaired": repaired, "original_payload": payload,
        "repair_payload": repair_payload, "receipts": [original_receipt, repair_receipt],
        "manifest": {"model": "qwen3.8-max"}}


def authenticate(data, cache, event=None):
    return module.authenticate_event(event or data["repaired"], cache=cache, endpoint=ENDPOINT,
        receipts=data["receipts"], sources={"S": data["source"]}, names=[],
        snapshot={"E1": data["original"]}, manifest=data["manifest"])


class AuthenticatedEvaluationTests(unittest.TestCase):
    def test_A001_verified_repair_accepted_without_task_reclassification(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            result = authenticate(data, folder)
            self.assertEqual(result["status"], "VERIFIED")
            self.assertEqual(result["actual_task"], module.REPAIR_TASK)
            self.assertEqual(result["original_review_request_sha256"], data["original"]["review_request_sha256"])

    def test_A002_invalid_original_chain_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            data["repaired"]["review_refinement"]["original_review_request_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "LINEAGE_INVALID"):
                authenticate(data, folder)

    def test_A003_changed_pool_even_with_valid_new_request_hash_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            data["repair_payload"]["evidence_source_pool"][0]["added_metadata"] = "changed"
            key, receipt = cached(folder, module.REPAIR_TASK, data["repair_payload"], data["repaired"]["semantic_review"])
            data["repaired"]["review_request_sha256"] = key
            data["repaired"]["review_refinement"]["repair_request_sha256"] = key
            data["receipts"].append(receipt)
            with self.assertRaisesRegex(ValueError, "POOL_OR_ONE_PASS_CONTRACT_CHANGED"):
                authenticate(data, folder)

    def test_A004_changed_target_block_ids_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            data["repair_payload"]["events"][0]["evidence_context"]["block_ids"] = []
            key, receipt = cached(folder, module.REPAIR_TASK, data["repair_payload"], data["repaired"]["semantic_review"])
            data["repaired"]["review_request_sha256"] = key
            data["repaired"]["review_refinement"]["repair_request_sha256"] = key
            data["receipts"].append(receipt)
            with self.assertRaisesRegex(ValueError, "BLOCK_IDS_AND_FULL_ACTUAL"):
                authenticate(data, folder)

    def test_A005_tampered_request_or_response_hash_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            path = Path(folder) / (data["repaired"]["review_request_sha256"] + ".json")
            value = module.read(path)
            value["result"]["items"][0]["checkpoint"] = "forged"
            write(path, value)
            with self.assertRaisesRegex(ValueError, "RESULT_HASH_MISMATCH"):
                authenticate(data, folder)
            data = fixture(folder)
            value = module.read(path)
            value["request"]["model"] = "other-model"
            write(path, value)
            with self.assertRaisesRegex(ValueError, "REQUEST_BODY_HASH_MISMATCH"):
                authenticate(data, folder)

    def test_A006_full_actual_text_or_coordinates_change_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            data["repaired"]["actual_model_evidence"][0]["text"] = "fake"
            with self.assertRaisesRegex(ValueError, "TEXT_NOT_EXACT_SOURCE_SPAN"):
                authenticate(data, folder)

    def test_A007_changed_current_full_document_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            data["repair_payload"]["current_full_document"].append({"source_id": "FUTURE", "text": "forbidden"})
            key, receipt = cached(folder, module.REPAIR_TASK, data["repair_payload"], data["repaired"]["semantic_review"])
            data["repaired"]["review_request_sha256"] = key
            data["repaired"]["review_refinement"]["repair_request_sha256"] = key
            data["receipts"].append(receipt)
            with self.assertRaisesRegex(ValueError, "ORIGINAL_CONTEXT_CHANGED:current_full_document"):
                authenticate(data, folder)

    def test_A008_primary_task_itself_is_authenticated(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            result = authenticate(data, folder, data["original"])
            self.assertEqual(result["actual_task"], module.PRIMARY_TASK)

    def test_A009_success_receipt_must_keep_actual_repair_task(self):
        with tempfile.TemporaryDirectory() as folder:
            data = fixture(folder)
            data["receipts"][1]["task"] = module.PRIMARY_TASK
            with self.assertRaisesRegex(ValueError, "RUN_SUCCESS_RECEIPT_MISSING_FOR_ACTUAL_TASK"):
                authenticate(data, folder)

    def test_A010_endpoint_query_or_userinfo_never_read_as_configuration(self):
        for value in ["https://user:secret@example.com/v1", "https://example.com/v1?api_key=secret", "http://example.com/v1"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    module.sanitized_endpoint(value)

    def test_A011_injected_repair_receipt_authenticates_frozen_needed_span_metric(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "cache"
            data = fixture(cache)
            primary, refined = root / "primary", root / "refined"
            case = refined / "fictional"
            source_file = "source_fhir_r4_bundle.json"
            write(primary / "decision_events.json", [data["original"]])
            write(primary / source_file, {})
            write(case / "original_decision_events.json", [data["original"]])
            write(case / source_file, {})
            write(case / "model_receipts.json", data["receipts"])
            manifest = {"chat_completions_endpoint": ENDPOINT, "model": "qwen3.8-max",
                "original_input_directory": str(primary),
                "original_decision_events_sha256": module.file_sha(primary / "decision_events.json"),
                "original_source_fhir_sha256": module.file_sha(primary / source_file),
                "source_fhir_sha256": module.file_sha(case / source_file)}
            write(case / "refinement_manifest.json", manifest)
            event = data["repaired"]
            normalized = {"status": "available", "root": str(refined), "cases": {"fictional": {
                "events": [event], "discovered": [event], "source_hashes": {"S":data["source"]["source_sha256"]},
                "contexts": {"E1": {"blocks": event["actual_model_evidence"], "measured":True}},
                "windows": {}, "raw": [], "final_available":True, "warnings":[], "input_files":{}}}}
            with patch.object(module, "load_run", return_value=normalized), patch.object(module, "import_sources", return_value=([data["source"]], {})):
                loaded = module.load_authenticated_run(refined, cache, ENDPOINT, case_ids={"fictional"})
            context = loaded["cases"]["fictional"]["contexts"]["E1"]
            self.assertTrue(context["measured"])
            self.assertEqual(context["request_sha256"], event["review_request_sha256"])
            self.assertIn("cache_file_sha256", context)
            anchor = event["action_citations"]["valid"][0]
            reference = {"reference_id":"P", "case_id":"fictional", "label":"positive", "anchor":anchor,
                "checkpoint_eligible":"yes", "evidence_expectations":{"needed":[event["actual_model_evidence"][0]["anchor"]]}}
            report = evaluate_layers([reference], loaded)
            self.assertEqual(report["metrics"]["needed_source_span_actual_request_coverage"]["hit"], 1)
            self.assertEqual(loaded["request_authentication"]["events"][0]["actual_task"], module.REPAIR_TASK)
            # A failed re-authentication must clear an earlier direct receipt;
            # the repair task cannot be rescued by the legacy primary-task set.
            path = cache / (event["review_request_sha256"] + ".json")
            corrupted = module.read(path)
            corrupted["result"]["items"][0]["checkpoint"] = "forged"
            write(path, corrupted)
            with patch.object(module, "load_run", return_value=normalized), patch.object(module, "import_sources", return_value=([data["source"]], {})):
                failed = module.load_authenticated_run(refined, cache, ENDPOINT, case_ids={"fictional"}, strict=False)
            failed_context = failed["cases"]["fictional"]["contexts"]["E1"]
            self.assertFalse(failed_context["measured"])
            self.assertNotIn("request_sha256", failed_context)
            result = evaluate_layers([reference], failed)
            self.assertEqual(result["metrics"]["needed_source_span_actual_request_coverage"]["unknown"], 1)


if __name__ == "__main__":
    unittest.main()

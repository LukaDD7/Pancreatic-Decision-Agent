"""CE-REV-001 compact cached-request authentication; synthetic counterexamples only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile

import pytest

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_compact_review_feedback import setup
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import compact_repair as compact
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import evaluate as module


ENDPOINT = "https://example.invalid/v1/chat/completions"
MODEL = "qwen3.8-max"


def cached(cache, task, payload, proposal):
    request = {"model": MODEL, "messages": [
        {"role": "system", "content": "Synthetic source-bound task."},
        {"role": "user", "content": json.dumps(
            {"task": task, "input": payload}, ensure_ascii=False)}]}
    key = digest([ENDPOINT, request])
    result = {"items": [proposal]}
    receipt = {"request_sha256": key, "task": task, "status": "SUCCESS", "model": MODEL}
    path = Path(cache) / (key + ".json")
    path.write_text(json.dumps({"request": request, "result": result,
        "result_sha256": digest(result), "receipt": receipt}, ensure_ascii=False))
    return key, receipt


def fixture(cache):
    event, previous, sources, proposal, parent_payload = setup()
    repaired_proposal = deepcopy(proposal)
    proposal["checkpoint"] = "uncertain"
    original_hash, original_receipt = cached(
        cache, module.PRIMARY_TASK, parent_payload, proposal)
    event["review_request_sha256"] = original_hash
    repair_payload = compact.prepare_compact_payload(
        parent_payload, [event], [], sources, [previous, event])
    repair_hash, repair_receipt = cached(
        cache, module.REPAIR_TASK, repair_payload, repaired_proposal)
    repaired = dict(deepcopy(event), review_request_sha256=repair_hash,
        semantic_review=repaired_proposal,
        review_refinement={"attempted": True, "rounds": 1,
            "status": "MODEL_REVISION_VALIDATED",
            "original_review_request_sha256": original_hash,
            "repair_request_sha256": repair_hash})
    return {"event": event, "repaired": repaired, "previous": previous,
        "sources": {source["source_id"]: source for source in sources},
        "parent_payload": parent_payload, "repair_payload": repair_payload,
        "receipts": [original_receipt, repair_receipt],
        "manifest": {"model": MODEL}}


def authenticate(data, cache):
    return module.authenticate_event(data["repaired"], cache=cache,
        endpoint=ENDPOINT, receipts=data["receipts"], sources=data["sources"],
        names=[], snapshot={"E1": data["event"]}, manifest=data["manifest"],
        all_events={"E0": data["previous"], "E1": data["event"]})


def mutation_fixture(tmp_path, mutate):
    data = fixture(tmp_path)
    mutate(data)
    repair_hash, repair_receipt = cached(tmp_path, module.REPAIR_TASK,
        data["repair_payload"], data["repaired"]["semantic_review"])
    data["repaired"]["review_request_sha256"] = repair_hash
    data["repaired"]["review_refinement"]["repair_request_sha256"] = repair_hash
    data["receipts"].append(repair_receipt)
    return data


def test_CEREV001_authenticates_real_compact_payload_and_current_boundary_window():
    with tempfile.TemporaryDirectory() as folder:
        data = fixture(folder)
        assert any(row["authority_basis"] ==
                   "PARENT_CURRENT_FULL_DOCUMENT_ACTION_OR_CONNECTION_BOUNDARY"
                   for row in data["repair_payload"]["evidence_source_pool"])
        verified = authenticate(data, folder)
        assert verified["status"] == "VERIFIED"
        assert verified["actual_task"] == module.REPAIR_TASK
        assert verified["original_review_request_sha256"] == data["event"]["review_request_sha256"]


@pytest.mark.parametrize("kind", [
    "window_text", "window_coordinate", "selection_coordinate", "selection_role",
    "source_expansion", "window_metadata_expansion", "target_event_expansion",
    "coordinate_index", "original_pool_hash", "original_ledger_hash",
    "original_event_inputs_hash", "target_input", "original_proposals",
    "full_document_hash", "allowed_spans_hash", "omitted_derived_hash",
    "unrendered_ids", "ledger_projection", "submitted_prior", "identity_sibling",
    "selection_role", "authority_basis", "display_role", "submitted_windows_hash",
    "unresolved_feedback", "validation_feedback",
])
def test_CEREV001_rehashed_payload_or_contract_tampering_is_rejected(tmp_path, kind):
    def mutate(data):
        payload = data["repair_payload"]
        contract = payload["repair_contract"]
        if kind == "window_text":
            payload["evidence_source_pool"][0]["text"] = "伪造的显示原文"
        elif kind == "window_coordinate":
            payload["evidence_source_pool"][0]["end"] -= 1
        elif kind == "selection_coordinate":
            contract["selection_receipt"][0]["start"] = 1
        elif kind == "selection_role":
            contract["selection_receipt"][0]["roles"] = [
                "DISCOVERY_ACTION_PRECHOICE_POSTCHOICE_OR_CONNECTION_BOUNDARY"]
        elif kind == "authority_basis":
            payload["evidence_source_pool"][0]["authority_basis"] = (
                "ORIGINAL_PARENT_EVIDENCE_POOL")
        elif kind == "display_role":
            contract["display_roles"]["unrendered_original_blocks"] = "CLINICALLY_ABSENT"
        elif kind == "submitted_windows_hash":
            contract["submitted_evidence_windows_sha256"] = "0" * 64
        elif kind == "unresolved_feedback":
            contract["unresolved_feedback_spans"] = [dict(
                target_event_id="E1", role="LOCALIZED_TECHNICAL_ERROR_SOURCE_SPAN",
                source_id="C", start=0, end=7, status="SPAN_NOT_AUTHENTICATED_AS_RENDERABLE_SOURCE_WINDOW")]
        elif kind == "validation_feedback":
            payload["validation_feedback"][0]["validation_reasons"] = []
        elif kind == "source_expansion":
            contract["selection_receipt"].append({"source_id": "C", "start": 0,
                "end": len(data["sources"]["C"]["text"]) + 1, "roles": []})
        elif kind == "window_metadata_expansion":
            payload["evidence_source_pool"][0]["choice_time"] = "2099-01-01"
        elif kind == "target_event_expansion":
            payload["events"][0]["event"]["event_id"] = "NEW-EVENT"
        elif kind == "coordinate_index":
            payload["source_coordinate_index"][0]["masked_text_sha256"] = "0" * 64
        elif kind == "original_pool_hash":
            contract["original_evidence_pool_sha256"] = "0" * 64
        elif kind == "original_ledger_hash":
            contract["original_earlier_event_ledger_sha256"] = "0" * 64
        elif kind == "original_event_inputs_hash":
            contract["original_event_inputs_sha256"] = "0" * 64
        elif kind == "target_input":
            payload["events"][0]["evidence_context"]["block_ids"] = []
        elif kind == "original_proposals":
            payload["original_proposals"][0]["checkpoint"] = "uncertain"
        elif kind == "full_document_hash":
            contract["original_full_document_sha256"] = "0" * 64
        elif kind == "allowed_spans_hash":
            contract["original_allowed_spans_sha256"] = "0" * 64
        elif kind == "omitted_derived_hash":
            contract["omitted_derived_receipts_sha256"]["comparison_validation"] = "0" * 64
        elif kind == "unrendered_ids":
            contract["unrendered_original_block_ids"] = ["INVENTED:0:1"]
        elif kind == "ledger_projection":
            payload["earlier_event_ledger"][0]["original_row_sha256"] = "0" * 64
        elif kind == "submitted_prior":
            contract["submitted_event_authority"]["E1"]["submitted_eligible_previous_event_ids"] = ["NEW"]
        elif kind == "identity_sibling":
            contract["submitted_event_authority"]["E1"]["identity_event_ids"].append("NEW-SIBLING")
    data = mutation_fixture(tmp_path, mutate)
    with pytest.raises(ValueError, match="COMPACT_REPAIR|TARGET_EVENT_NOT_UNIQUE"):
        authenticate(data, tmp_path)


def test_CEREV001_current_document_substitution_rejected_even_with_new_request_receipt(tmp_path):
    def mutate(data):
        data["repair_payload"]["current_full_document"][0]["text"] += "新增选择后事实"
    data = mutation_fixture(tmp_path, mutate)
    with pytest.raises(ValueError, match="COMPACT_REPAIR_ORIGINAL_CONTEXT_CHANGED"):
        authenticate(data, tmp_path)


def test_CEREV001_stale_parent_lineage_cannot_be_repaired_by_new_compact_receipt(tmp_path):
    data = fixture(tmp_path)
    data["repaired"]["review_refinement"]["original_review_request_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="REPAIR_ORIGINAL_LINEAGE_INVALID"):
        authenticate(data, tmp_path)


def test_CEREV001_parent_payload_substitution_is_rejected_by_cached_transport_hash(tmp_path):
    data = fixture(tmp_path)
    path = Path(tmp_path) / (data["event"]["review_request_sha256"] + ".json")
    saved = json.loads(path.read_text())
    content = json.loads(saved["request"]["messages"][1]["content"])
    content["input"]["evidence_source_pool"][0]["text"] = "替换父请求证据"
    saved["request"]["messages"][1]["content"] = json.dumps(content, ensure_ascii=False)
    path.write_text(json.dumps(saved, ensure_ascii=False))
    with pytest.raises(ValueError, match="CACHED_REQUEST_BODY_HASH_MISMATCH"):
        authenticate(data, tmp_path)


def test_CEREV001_result_substitution_rejected_before_proposal_application(tmp_path):
    data = fixture(tmp_path)
    path = Path(tmp_path) / (data["repaired"]["review_request_sha256"] + ".json")
    saved = json.loads(path.read_text())
    saved["result"]["items"][0]["checkpoint"] = "forged"
    path.write_text(json.dumps(saved, ensure_ascii=False))
    with pytest.raises(ValueError, match="CACHED_RESULT_HASH_MISMATCH"):
        authenticate(data, tmp_path)


def test_CEREV001_applied_proposal_must_be_unique_in_authenticated_response(tmp_path):
    data = fixture(tmp_path)
    path = Path(tmp_path) / (data["repaired"]["review_request_sha256"] + ".json")
    saved = json.loads(path.read_text())
    saved["result"]["items"].append(deepcopy(saved["result"]["items"][0]))
    saved["result_sha256"] = digest(saved["result"])
    path.write_text(json.dumps(saved, ensure_ascii=False))
    with pytest.raises(ValueError, match="APPLIED_REPAIR_PROPOSAL_NOT_UNIQUE_SAVED_RESPONSE"):
        authenticate(data, tmp_path)


def test_CEREV001_transport_and_contract_hash_domains_stay_separate(tmp_path):
    data = fixture(tmp_path)
    contract = data["repair_payload"]["repair_contract"]
    transport = data["repaired"]["review_request_sha256"]
    assert transport == digest([ENDPOINT, module.read(
        Path(tmp_path) / (transport + ".json"))["request"]])
    assert contract["submitted_evidence_windows_sha256"] == digest(
        data["repair_payload"]["evidence_source_pool"])
    assert contract["submitted_evidence_windows_sha256"] != transport
    assert contract["original_evidence_pool_sha256"] == digest(
        data["parent_payload"]["evidence_source_pool"])

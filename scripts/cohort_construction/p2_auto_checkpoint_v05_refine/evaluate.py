"""Authenticate original/one-repair evidence requests before frozen evaluation.

This wrapper changes receipt authentication only. It does not relabel repair
calls as original calls, change a metric definition, edit a run, or load API
credentials. The caller supplies the sanitized endpoint recorded in provenance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
from scripts.cohort_construction.p2_auto_checkpoint.semantic import mask_text, patient_names
from scripts.cohort_construction.p2_auto_checkpoint_v04.evaluate import (
    file_sha, load_reference, load_run, read,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import redact
from scripts.cohort_construction.p2_auto_checkpoint_v04.evaluate_layers import (
    compare_reports, evaluate_layers, load_contract, render_markdown,
)


PRIMARY_TASK = "document_checkpoint_review"
REPAIR_TASK = "document_checkpoint_review_repair"
COMPACT_REPAIR_CONTRACT = "review-compact-repair-0.2.0"
_COMPACT_EVIDENCE_AVAILABILITY = (
    "SELECTED_ORIGINAL_WINDOWS_WITH_ORIGINAL_AUTHORITY_REVALIDATION")
_COMPACT_OMITTED_FIELDS = [
    "observation_validation", "connection_validation", "comparison_validation",
    "availability_validation", "semantic_proof_validation", "field_validation_errors",
    "evidence_relation_ledger",
]
_COMPACT_SELECTION_ROLES = {
    "SELECTED_PREVIOUS_EVENT_ACTION_AND_BOUNDARY",
    "DISCOVERY_ACTION_PRECHOICE_POSTCHOICE_OR_CONNECTION_BOUNDARY",
    "DECLARED_EVIDENCE_OBJECT_DIRECT_QUOTE_AND_CLOCK_BINDING",
    "ORIGINAL_PROPOSAL_OBSERVATION_CONNECTION_COMPARISON_OR_ACTION",
    "LOCALIZED_TECHNICAL_ERROR_SOURCE_SPAN",
}
_COMPACT_DISPLAY_ROLES = {
    "current_full_document": "ACTION_AND_POSTCHOICE_BOUNDARY_ONLY_NOT_PRECHOICE_AUTHORITY",
    "unrendered_original_blocks": "NOT_DISPLAYED_NOT_NEGATIVE_NOT_CLINICALLY_ABSENT",
    "original_coordinate_index": "HASH_BOUND_AUTHORITY_NOT_A_CLAIM_THAT_MODEL_READ_THE_TEXT",
}
_COMPACT_UNRESOLVED_SPAN_STATUS = "SPAN_NOT_AUTHENTICATED_AS_RENDERABLE_SOURCE_WINDOW"
_SOURCE_FILES = {
    "decision_events.json", "source_fhir_r4_bundle.json", "model_receipts.json",
    "discovery.json", "unit_inventory.json", "source_import_receipt.json",
}


def sanitized_endpoint(value):
    if not isinstance(value, str):
        raise ValueError("EXACT_SANITIZED_ENDPOINT_REQUIRED")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("ENDPOINT_MUST_BE_HTTPS_WITHOUT_CREDENTIALS_QUERY_OR_FRAGMENT")
    return value


def _input(saved):
    messages = saved["request"].get("messages", [])
    users = [message for message in messages if message.get("role") == "user"]
    if len(users) != 1 or not isinstance(users[0].get("content"), str):
        raise ValueError("SINGLE_JSON_USER_MESSAGE_REQUIRED")
    content = json.loads(users[0]["content"])
    if not isinstance(content.get("input"), dict):
        raise ValueError("REQUEST_INPUT_OBJECT_REQUIRED")
    from scripts.cohort_construction.p2_auto_checkpoint_v04.payload_codec import decode_payload
    # Authenticate against the canonical expanded pool, whatever wire
    # representation the provider received. Decoding is lossless and every
    # downstream digest is taken over the decoded value.
    return content["task"], decode_payload(content["input"])


def _cached(cache, request_hash, endpoint, memo):
    if not isinstance(request_hash, str) or len(request_hash) != 64 or any(
            char not in "0123456789abcdef" for char in request_hash):
        raise ValueError("INVALID_REQUEST_HASH")
    key = (str(Path(cache).resolve()), request_hash, endpoint)
    if key in memo:
        return memo[key]
    path = Path(cache) / (request_hash + ".json")
    saved = read(path)
    if digest([endpoint, saved["request"]]) != request_hash:
        raise ValueError("CACHED_REQUEST_BODY_HASH_MISMATCH")
    if saved.get("result_sha256") != digest(saved.get("result")):
        raise ValueError("CACHED_RESULT_HASH_MISMATCH")
    receipt = saved.get("receipt", {})
    task, payload = _input(saved)
    if (receipt.get("request_sha256") != request_hash or receipt.get("status") != "SUCCESS"
            or receipt.get("task") != task):
        raise ValueError("CACHED_SUCCESS_RECEIPT_TASK_OR_ID_MISMATCH")
    if task not in {PRIMARY_TASK, REPAIR_TASK}:
        raise ValueError("UNSUPPORTED_ACTUAL_RECEIPT_TASK")
    if receipt.get("model") != saved["request"].get("model"):
        raise ValueError("CACHED_RECEIPT_MODEL_MISMATCH")
    result = {"saved": saved, "path": path, "task": task, "payload": payload,
              "request_sha256": request_hash, "request_body_sha256": digest(saved["request"]),
              "cache_file_sha256": file_sha(path)}
    memo[key] = result
    return result


def _target(payload, event_id):
    targets = [item for item in payload.get("events", [])
               if item.get("event", {}).get("event_id") == event_id]
    if len(targets) != 1:
        raise ValueError("TARGET_EVENT_NOT_UNIQUE_IN_REQUEST")
    return targets[0]


def _pool(payload, sources, names):
    entries = payload.get("evidence_source_pool")
    if not isinstance(entries, list):
        raise ValueError("EVIDENCE_SOURCE_POOL_REQUIRED")
    pool = {}
    for block in entries:
        source_id, start, end = block.get("source_id"), block.get("start"), block.get("end")
        key = block.get("block_id")
        if (source_id not in sources or not isinstance(start, int) or not isinstance(end, int)
                or start < 0 or end <= start or end > len(sources[source_id]["text"])
                or key != f"{source_id}:{start}:{end}" or key in pool):
            raise ValueError("POOL_SOURCE_COORDINATES_OR_ID_INVALID")
        expected = mask_text(sources[source_id]["text"][start:end], names)
        if block.get("text") != expected:
            raise ValueError("POOL_TEXT_NOT_EXACT_MASKED_SOURCE_SPAN")
        pool[key] = block
    return pool


def _allowed(context, source_id, start, end):
    for value in context.get("allowed_spans", {}).get(source_id, []):
        a, b = (value["start"], value["end"]) if isinstance(value, dict) else value
        if a <= start and end <= b:
            return True
    return False


def _material(event, payload, sources, names):
    """Authenticate every actual block and its complete raw/masked text."""
    target = _target(payload, event["event_id"])
    pool = _pool(payload, sources, names)
    block_ids = target.get("evidence_context", {}).get("block_ids")
    actual = event.get("actual_model_evidence")
    if not isinstance(block_ids, list) or not isinstance(actual, list):
        raise ValueError("ACTUAL_MATERIAL_AND_TARGET_BLOCK_IDS_REQUIRED")
    actual_keys = []
    for block in actual:
        source_id, start, end = block.get("source_id"), block.get("start"), block.get("end")
        if source_id not in sources or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("ACTUAL_EVIDENCE_COORDINATES_INVALID")
        key = f"{source_id}:{start}:{end}"
        if key not in pool:
            raise ValueError("ACTUAL_EVIDENCE_BLOCK_NOT_IN_REQUEST_POOL")
        source = sources[source_id]
        raw = source["text"][start:end]
        if block.get("text", block.get("quote")) != raw or (
                "quote" in block and block["quote"] != raw):
            raise ValueError("ACTUAL_EVIDENCE_TEXT_NOT_EXACT_SOURCE_SPAN")
        anchor = block.get("anchor", {})
        if (anchor.get("source_sha256", block.get("source_sha256")) != source["source_sha256"]
                or anchor.get("source_id", source_id) != source_id
                or anchor.get("start", start) != start or anchor.get("end", end) != end
                or ("quote" in anchor and anchor["quote"] != raw)):
            raise ValueError("ACTUAL_EVIDENCE_ANCHOR_HASH_OR_SPAN_MISMATCH")
        if not _allowed(event.get("evidence_context", {}), source_id, start, end):
            raise ValueError("ACTUAL_EVIDENCE_OUTSIDE_SAVED_ALLOWED_SPANS")
        if mask_text(raw, names) != pool[key]["text"]:
            raise ValueError("ACTUAL_EVIDENCE_AND_REQUEST_TEXT_DIFFER")
        actual_keys.append(key)
    if Counter(actual_keys) != Counter(block_ids):
        raise ValueError("TARGET_BLOCK_IDS_AND_FULL_ACTUAL_EVIDENCE_DIFFER")
    if event.get("evidence_context", {}).get("blocks") != actual:
        raise ValueError("COMPILED_CONTEXT_AND_ACTUAL_EVIDENCE_DIFFER")
    return {"event_input_sha256": digest(target), "target_block_ids_sha256": digest(block_ids),
            "evidence_pool_sha256": digest(payload["evidence_source_pool"]),
            "actual_model_evidence_sha256": digest(actual), "block_count": len(actual)}


def _manifest(case_dir, endpoint):
    path = case_dir / "refinement_manifest.json"
    if not path.exists():
        return None
    manifest = read(path)
    if manifest.get("chat_completions_endpoint") != endpoint:
        raise ValueError("REFINEMENT_MANIFEST_ENDPOINT_MISMATCH")
    sanitized_endpoint(manifest["chat_completions_endpoint"])
    original = Path(manifest["original_input_directory"])
    original_events = original / "decision_events.json"
    snapshot = case_dir / "original_decision_events.json"
    expected = manifest["original_decision_events_sha256"]
    if file_sha(original_events) != expected or file_sha(snapshot) != expected:
        raise ValueError("ORIGINAL_EVENT_FILE_OR_SNAPSHOT_HASH_MISMATCH")
    if file_sha(original / "source_fhir_r4_bundle.json") != manifest["original_source_fhir_sha256"]:
        raise ValueError("ORIGINAL_SOURCE_FHIR_HASH_MISMATCH")
    if file_sha(case_dir / "source_fhir_r4_bundle.json") != manifest["source_fhir_sha256"]:
        raise ValueError("REFINED_SOURCE_FHIR_HASH_MISMATCH")
    for name, expected_hash in manifest.get("original_file_sha256", {}).items():
        if name not in _SOURCE_FILES or file_sha(original / name) != expected_hash:
            raise ValueError("ORIGINAL_MANIFEST_FILE_HASH_MISMATCH:" + str(name))
    for name, expected_hash in manifest.get("preserved_file_sha256", {}).items():
        if name not in _SOURCE_FILES or file_sha(case_dir / name) != expected_hash:
            raise ValueError("PRESERVED_MANIFEST_FILE_HASH_MISMATCH:" + str(name))
    manifest = dict(manifest, manifest_path=str(path.resolve()), manifest_sha256=file_sha(path),
                    snapshot_path=str(snapshot.resolve()))
    return manifest


def _successful_receipt(receipts, request_hash, task):
    return any(receipt.get("request_sha256") == request_hash and receipt.get("task") == task
               and receipt.get("status") == "SUCCESS" for receipt in receipts)


def authenticate_event(event, *, cache, endpoint, receipts, sources, names,
                       snapshot=None, manifest=None, memo=None, all_events=None):
    """Pure file/source checks; actual primary/repair task labels stay intact."""
    memo = {} if memo is None else memo
    request_hash = event.get("review_request_sha256")
    current = _cached(cache, request_hash, endpoint, memo)
    if not _successful_receipt(receipts, request_hash, current["task"]):
        raise ValueError("RUN_SUCCESS_RECEIPT_MISSING_FOR_ACTUAL_TASK")
    if manifest and current["saved"]["request"].get("model") != manifest.get("model"):
        raise ValueError("REQUEST_MODEL_DIFFERS_FROM_REFINEMENT_MANIFEST")
    compact = (current["task"] == REPAIR_TASK
               and current["payload"].get("repair_contract", {}).get("version")
               == COMPACT_REPAIR_CONTRACT)
    # Compact target block_ids still refer to the original authority coordinates,
    # not to its shorter rendered window list. Authenticate those coordinates via
    # the cached parent below; do not mistake omission for raw evidence deletion.
    current_material = (_material(event, current["payload"], sources, names)
                        if not compact else None)
    original_hash = None
    if snapshot is not None:
        original_event = snapshot.get(event["event_id"])
        if original_event is None:
            raise ValueError("EVENT_NOT_IN_ORIGINAL_SNAPSHOT")
        if original_event.get("actual_model_evidence") != event.get("actual_model_evidence"):
            raise ValueError("REFINEMENT_CHANGED_FULL_ACTUAL_MODEL_EVIDENCE")
    else:
        original_event = None
    if compact and all_events is None:
        raise ValueError("COMPACT_REPAIR_ORIGINAL_DISCOVERY_EVENTS_REQUIRED")
    if all_events is None:
        all_events = snapshot
    elif not isinstance(all_events, dict):
        all_events = {item["event_id"]: item for item in all_events}
    if current["task"] == REPAIR_TASK:
        lineage = event.get("review_refinement", {})
        original_hash = lineage.get("original_review_request_sha256")
        if (not manifest or original_event is None or lineage.get("attempted") is not True
                or lineage.get("rounds") != 1 or lineage.get("status") != "MODEL_REVISION_VALIDATED"
                or lineage.get("repair_request_sha256") != request_hash
                or original_event.get("review_request_sha256") != original_hash):
            raise ValueError("REPAIR_ORIGINAL_LINEAGE_INVALID")
        original = _cached(cache, original_hash, endpoint, memo)
        if original["task"] != PRIMARY_TASK or not _successful_receipt(receipts, original_hash, PRIMARY_TASK):
            raise ValueError("REPAIR_ORIGIN_IS_NOT_SUCCESSFUL_PRIMARY_REVIEW")
        if original["saved"]["request"].get("model") != current["saved"]["request"].get("model"):
            raise ValueError("REPAIR_CHANGED_MODEL")
        old_payload, repair_payload = original["payload"], current["payload"]
        compact = (repair_payload.get("repair_contract", {}).get("version")
                   == COMPACT_REPAIR_CONTRACT)
        if compact:
            # current_material is measured against the parent after the compact
            # target is proven identical to that parent target.
            if _target(repair_payload, event["event_id"]) != _target(
                    old_payload, event["event_id"]):
                raise ValueError("COMPACT_REPAIR_TARGET_INPUT_OR_BLOCK_IDS_CHANGED")
            current_material = _material(event, old_payload, sources, names)
        if not compact:
            pool_digest = digest(old_payload.get("evidence_source_pool"))
            if (digest(repair_payload.get("evidence_source_pool")) != pool_digest
                    or repair_payload.get("repair_contract", {}).get("original_evidence_pool_sha256") != pool_digest
                    or repair_payload.get("repair_contract", {}).get("maximum_semantic_repair_rounds") != 1
                    or repair_payload.get("repair_contract", {}).get("source_context_added") is not False):
                raise ValueError("REPAIR_EVIDENCE_POOL_OR_ONE_PASS_CONTRACT_CHANGED")
            for field in ("current_full_document", "earlier_event_ledger"):
                if repair_payload.get(field) != old_payload.get(field):
                    raise ValueError("REPAIR_ORIGINAL_CONTEXT_CHANGED:" + field)
            if _target(repair_payload, event["event_id"]) != _target(old_payload, event["event_id"]):
                raise ValueError("REPAIR_TARGET_INPUT_OR_BLOCK_IDS_CHANGED")
            original_ids = {item.get("event", {}).get("event_id") for item in old_payload.get("events", [])}
            repair_ids = [item.get("event", {}).get("event_id") for item in repair_payload.get("events", [])]
            if len(repair_ids) != len(set(repair_ids)) or not set(repair_ids) <= original_ids:
                raise ValueError("REPAIR_TARGET_SET_NOT_ORIGINAL_SUBSET")
        original_material = _material(original_event, old_payload, sources, names)
        if original_material != current_material:
            raise ValueError("REPAIR_MATERIAL_DIGEST_CHANGED")
        if compact:
            # Only the additional compact branch lives here; all preceding
            # legacy task/receipt/lineage/material checks remain unchanged.
            authenticate_compact_event(event, original_event, original, current, sources, names,
                                       target_events=snapshot, all_events=all_events)
        returned = [item for item in current["saved"].get("result", {}).get("items", [])
                    if item.get("event_id") == event["event_id"]]
        if len(returned) != 1 or returned[0] != event.get("semantic_review"):
            raise ValueError("APPLIED_REPAIR_PROPOSAL_NOT_UNIQUE_SAVED_RESPONSE")
    elif original_event and original_event.get("review_request_sha256") != request_hash:
        raise ValueError("UNREPAIRED_EVENT_REVIEW_HASH_CHANGED")
    return {"status": "VERIFIED", "actual_task": current["task"],
            "request_sha256": request_hash, "request_body_sha256": current["request_body_sha256"],
            "cache_file_sha256": current["cache_file_sha256"],
            "cache_path": str(current["path"].resolve()),
            "original_review_request_sha256": original_hash, **current_material}


def _compact_coordinate(block):
    """Recompute the exact 0.2.0 coordinate receipt, including clock bindings."""
    row = {key: deepcopy(block.get(key)) for key in (
        "block_id", "evidence_object_id", "test_execution_anchor_id")}
    row["coordinate_digest"] = digest({key: block.get(key) for key in (
        "block_id", "source_id", "start", "end", "source_title", "clock",
        "execution_time", "publication_time", "publication_status",
        "evidence_object_id", "test_execution_anchor_id")})
    row["masked_text_sha256"] = hashlib.sha256(block["text"].encode()).hexdigest()
    return row


def _compact_current_document(payload, sources):
    documents = payload.get("current_full_document")
    if not isinstance(documents, list):
        raise ValueError("COMPACT_PARENT_CURRENT_DOCUMENT_REQUIRED")
    result = {}
    for row in documents:
        if not isinstance(row, dict):
            raise ValueError("COMPACT_PARENT_CURRENT_DOCUMENT_INVALID")
        sid, text = row.get("source_id"), row.get("text")
        if sid not in sources or text != sources[sid]["text"] or sid in result:
            raise ValueError("COMPACT_PARENT_CURRENT_DOCUMENT_NOT_EXACT_SOURCE")
        result[sid] = row
    return result


def _compact_target_map(payload):
    result = {}
    for item in payload.get("events", []):
        event_id = item.get("event", {}).get("event_id")
        if not isinstance(event_id, str) or event_id in result:
            raise ValueError("COMPACT_REPAIR_TARGET_SET_NOT_ORIGINAL_SUBSET")
        result[event_id] = item
    return result


def _compact_repair_contract(original, repair, original_event, sources, names,
                             target_events=None):
    """Authenticate compact presentation against the independently cached parent.

    The original parent payload and original snapshot remain the authority. The
    compact pool is only a display selection. No compact field is accepted as a
    substitute for raw text, parent coordinates, or the saved original allowlist.
    """
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.compact_repair import (
        COMPACT_INSTRUCTIONS, _feedback,
    )
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.refine import (
        feedback, prepare_payload,
    )

    old_payload, payload = original["payload"], repair["payload"]
    contract = payload.get("repair_contract")
    if not isinstance(contract, dict) or contract.get("version") != COMPACT_REPAIR_CONTRACT:
        raise ValueError("COMPACT_REPAIR_CONTRACT_VERSION_REQUIRED")
    original_targets = _compact_target_map(old_payload)
    repair_targets = _compact_target_map(payload)
    target_events = ({original_event["event_id"]: original_event} if target_events is None
                     else target_events)
    if (not repair_targets or not set(repair_targets) <= set(original_targets)
            or not set(repair_targets) <= set(target_events)):
        raise ValueError("COMPACT_REPAIR_TARGET_SET_NOT_ORIGINAL_SUBSET")
    targets = {event_id: original_targets[event_id] for event_id in repair_targets}
    saved_events = {event_id: target_events[event_id] for event_id in repair_targets}
    for event_id, target in targets.items():
        if target != repair_targets[event_id]:
            raise ValueError("COMPACT_REPAIR_TARGET_INPUT_OR_BLOCK_IDS_CHANGED")
    for field, expected in (
        ("maximum_semantic_repair_rounds", 1),
        ("repair_attempt_semantics", "NEW_IMPLEMENTATION_VERSION_NOT_PROVIDER_SECOND_ROUND"),
        ("source_context_added", False), ("source_authority_expanded", False),
        ("authority_narrowed", False), ("source_coverage_complete", False),
        ("positive_result_required", False),
        ("repair_evidence_availability", _COMPACT_EVIDENCE_AVAILABILITY),
        ("original_evidence_pool_sha256", digest(old_payload.get("evidence_source_pool"))),
        ("original_full_document_sha256", digest(old_payload.get("current_full_document"))),
        ("original_earlier_event_ledger_sha256", digest(old_payload.get("earlier_event_ledger"))),
        ("original_event_inputs_sha256", digest(targets)),
        ("original_allowed_spans_sha256", digest({event_id: event["evidence_context"]["allowed_spans"]
                                                  for event_id, event in saved_events.items()})),
        ("submitted_evidence_windows_sha256", digest(payload.get("evidence_source_pool"))),
        ("display_roles", _COMPACT_DISPLAY_ROLES),
    ):
        if contract.get(field) != expected:
            raise ValueError("COMPACT_REPAIR_ORIGINAL_OR_SELECTION_HASH_MISMATCH:" + field)
    for field in ("unit_id", "document_profile", "type_prior", "current_full_document"):
        if payload.get(field) != old_payload.get(field):
            raise ValueError("COMPACT_REPAIR_ORIGINAL_CONTEXT_CHANGED:" + field)
    if payload.get("reconstruction_only_actual_choices_disclosed") is not True:
        raise ValueError("COMPACT_REPAIR_ACTION_DISCLOSURE_RECEIPT_INVALID")
    _compact_current_document(old_payload, sources)
    _compact_current_document(payload, sources)
    if original_event.get("review_refinement", {}).get("attempted"):
        raise ValueError("COMPACT_REPAIR_SNAPSHOT_ALREADY_REPAIRED")
    for saved in saved_events.values():
        if saved.get("review_refinement", {}).get("attempted"):
            raise ValueError("COMPACT_REPAIR_SNAPSHOT_ALREADY_REPAIRED")

    # Re-run strict parent checks and derive every submitted target's feedback.
    parent = prepare_payload(old_payload, list(saved_events.values()), names)
    if payload.get("original_proposals") != parent.get("original_proposals"):
        raise ValueError("COMPACT_REPAIR_PARENT_DERIVED_CONTENT_CHANGED:original_proposals")
    expected_feedback = []
    for event_id, event in saved_events.items():
        submitted = targets[event_id]["evidence_context"].get("eligible_previous_event_ids", [])
        recovered, _ = _compact_authority(contract, event, submitted)
        expected_feedback.append(_feedback(event, recovered))
    if payload.get("validation_feedback") != redact(expected_feedback, names):
        raise ValueError("COMPACT_REPAIR_FEEDBACK_NOT_AUTHENTICATED_COMPACT_PROJECTION")
    expected_instruction = old_payload.get("instruction", "") + "\n" + COMPACT_INSTRUCTIONS
    if payload.get("instruction") != expected_instruction:
        raise ValueError("COMPACT_REPAIR_PARENT_INSTRUCTION_CHANGED")

    pool_rows = old_payload.get("evidence_source_pool")
    if not isinstance(pool_rows, list):
        raise ValueError("COMPACT_REPAIR_PARENT_POOL_REQUIRED")
    pool = {row.get("block_id"): row for row in pool_rows}
    if len(pool) != len(pool_rows):
        raise ValueError("COMPACT_REPAIR_DUPLICATE_PARENT_BLOCK_ID")
    index_rows = payload.get("source_coordinate_index")
    if not isinstance(index_rows, list) or len(index_rows) != len(pool):
        raise ValueError("COMPACT_REPAIR_COORDINATE_INDEX_INVALID")
    coordinates = {}
    for row in index_rows:
        key = row.get("block_id")
        if key not in pool or key in coordinates or row != _compact_coordinate(pool[key]):
            raise ValueError("COMPACT_REPAIR_COORDINATE_INDEX_OR_HASH_MISMATCH")
        coordinates[key] = row
    if list(coordinates) != sorted(pool):
        raise ValueError("COMPACT_REPAIR_COORDINATE_INDEX_INVALID")

    # Full parent receipts remain in their own hash domain, never transport or
    # result hashes. Recompute all targets using the original saved allowlists.
    if contract.get("omitted_derived_receipt_fields") != _COMPACT_OMITTED_FIELDS:
        raise ValueError("COMPACT_REPAIR_OMITTED_FIELD_LIST_CHANGED")
    if contract.get("omitted_derived_receipts_sha256") != {
            field: digest({event_id: feedback(event).get(field)
                           for event_id, event in saved_events.items()})
            for field in _COMPACT_OMITTED_FIELDS}:
        raise ValueError("COMPACT_REPAIR_OMITTED_DERIVED_HASH_MISMATCH")
    return contract, pool


def _compact_authority(contract, original_event, submitted):
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.compact_repair import (
        unmask_submitted_id,
    )

    saved = sorted(original_event.get("provided_prior_event_ids", []))
    submitted = sorted(submitted)
    original_submitted = list(submitted)
    recovered = [unmask_submitted_id(value, saved) for value in submitted]
    if None in recovered or sorted(recovered) != saved:
        raise ValueError("COMPACT_REPAIR_SUBMITTED_PRIOR_AUTHORITY_MISMATCH")
    submitted = sorted(recovered)
    identity = sorted(set(saved) | set(original_event.get("provided_identity_event_ids", []))
                      | {original_event["event_id"]})
    expected = {original_event["event_id"]: dict(
        submitted_eligible_previous_event_ids=submitted,
        original_submitted_eligible_previous_event_ids=original_submitted,
        saved_provided_prior_event_ids=saved, identity_event_ids=identity)}
    authority = contract.get("submitted_event_authority")
    if not isinstance(authority, dict) or authority.get(original_event["event_id"]) != expected[
            original_event["event_id"]]:
        raise ValueError("COMPACT_REPAIR_EVENT_OR_IDENTITY_AUTHORITY_CHANGED")
    return submitted, identity


def _compact_ledger_row(row):
    return dict(event_id=row.get("event_id"), original_row_sha256=digest(row))


def _compact_ledger(repair_payload, original_payload, original_event, contract):
    """Bind compact ledger IDs and each full original row's exact hash."""
    submitted = _target(original_payload, original_event["event_id"])["evidence_context"].get(
        "eligible_previous_event_ids", [])
    submitted, identity = _compact_authority(contract, original_event, submitted)
    parent_rows = original_payload.get("earlier_event_ledger")
    rows = repair_payload.get("earlier_event_ledger")
    if not isinstance(parent_rows, list) or not isinstance(rows, list):
        raise ValueError("COMPACT_REPAIR_LEDGER_REQUIRED")
    expected_ids = {row.get("event_id") for row in parent_rows
                    if row.get("event_id") in set(submitted) | set(identity)}
    if len({row.get("event_id") for row in parent_rows
            if row.get("event_id") in expected_ids}) != len([row for row in parent_rows
            if row.get("event_id") in expected_ids]):
        raise ValueError("COMPACT_REPAIR_LEDGER_ID_OR_DUPLICATE_INVALID")
    seen = set()
    for row in rows:
        event_id = row.get("event_id")
        if event_id not in expected_ids or event_id in seen:
            raise ValueError("COMPACT_REPAIR_LEDGER_ID_OR_DUPLICATE_INVALID")
        parents = [parent for parent in parent_rows if parent.get("event_id") == event_id]
        if len(parents) != 1 or row != _compact_ledger_row(parents[0]):
            raise ValueError("COMPACT_REPAIR_LEDGER_NOT_AUTHENTICATED_PARENT_PROJECTION")
        seen.add(event_id)
    if seen != expected_ids:
        raise ValueError("COMPACT_REPAIR_LEDGER_PARENT_ROW_OMITTED")
    return rows


def _current_boundary_ranges(event, sources):
    from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import _allowed_connection_ranges
    return _allowed_connection_ranges(event, event["evidence_context"], list(sources.values()))


def _compact_merge_windows(windows):
    result = []
    for span in sorted(set(windows)):
        if (result and span[0] == result[-1][0]
                and span[1] <= result[-1][2] + 16):
            # Merged text is new display coverage. Retain provenance separately
            # instead of pretending the smaller selected coordinate was rendered.
            result[-1] = (span[0], min(result[-1][1], span[1]),
                          max(result[-1][2], span[2]))
        else:
            result.append(span)
    return result


def _compact_expected_selection(saved_events, all_events, sources, names):
    """Reconstruct selection/role receipts from original citations and proofs."""
    from scripts.cohort_construction.p2_auto_checkpoint_v04.context import (
        build_evidence_context,
    )
    from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import (
        _allowed_connection_ranges, resolve_model_quotes,
    )
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.compact_repair import (
        _citation_spans, _group_citations, _proposal_spans, _ranges, _referenced_eos,
    )
    from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.refine import (
        technical_validation_errors,
    )

    source_list = list(sources.values())
    all_by_id = {event["event_id"]: event for event in all_events.values()}
    missing = [event_id for event_id, event in saved_events.items()
               if event["semantic_review"].get("previous_event_id") is not None
               and event["semantic_review"].get("previous_event_id") not in all_by_id]
    if missing:
        raise ValueError("COMPACT_REPAIR_ORIGINAL_DISCOVERY_EVENTS_REQUIRED")
    selected, unresolved = {}, []

    def select(span, role, event_id):
        sid, start, end = span
        valid = (isinstance(sid, str) and sid in sources and type(start) is int
                 and type(end) is int and 0 <= start < end <= len(sources[sid]["text"]))
        if not valid:
            unresolved.append(dict(target_event_id=event_id, role=role,
                source_id=sid if isinstance(sid, str) else None,
                start=start if type(start) is int else None,
                end=end if type(end) is int else None,
                status=_COMPACT_UNRESOLVED_SPAN_STATUS))
            return
        selected.setdefault((sid, start, end), set()).add(role)

    for event_id, event in saved_events.items():
        event_source_ids = set(event.get("source_ids") or [event.get("source_id")])
        previous = all_by_id.get(event["semantic_review"].get("previous_event_id"))
        if previous is not None:
            rebuilt = build_evidence_context(previous, source_list, list(all_events.values()),
                budget=150000, preserve_current_context=True)
            previous_ranges = _ranges(_allowed_connection_ranges(
                previous, rebuilt, source_list))
            for row in _group_citations(previous):
                for span in _citation_spans(row):
                    if any(type(span[1]) is int and type(span[2]) is int
                           and candidate["source_id"] == span[0]
                           and candidate["start"] <= span[1] and span[2] <= candidate["end"]
                           for candidate in previous_ranges):
                        select(span, "SELECTED_PREVIOUS_EVENT_ACTION_AND_BOUNDARY", event_id)
        for row in _group_citations(event):
            if row.get("source_id") not in event_source_ids:
                continue
            for span in _citation_spans(row):
                select(span, "DISCOVERY_ACTION_PRECHOICE_POSTCHOICE_OR_CONNECTION_BOUNDARY", event_id)
        referenced_eos = _referenced_eos(event)
        for block in event["actual_model_evidence"]:
            if (block.get("evidence_object_id") in referenced_eos
                    or block.get("test_execution_anchor_id") in referenced_eos):
                select((block["source_id"], block["start"], block["end"]),
                    "DECLARED_EVIDENCE_OBJECT_DIRECT_QUOTE_AND_CLOCK_BINDING", event_id)
        for row in _proposal_spans(event["semantic_review"]):
            resolved = resolve_model_quotes([row], source_list, names)
            for status in ("valid", "ambiguous"):
                for citation in resolved[status]:
                    for span in _citation_spans(citation):
                        select(span, "ORIGINAL_PROPOSAL_OBSERVATION_CONNECTION_COMPARISON_OR_ACTION",
                               event_id)
        for error in technical_validation_errors(event):
            span = error.get("span")
            if isinstance(span, dict):
                select((error.get("source_id"), span.get("start"), span.get("end")),
                    "LOCALIZED_TECHNICAL_ERROR_SOURCE_SPAN", event_id)
    receipt = [dict(source_id=sid, start=start, end=end, roles=sorted(roles))
               for (sid, start, end), roles in sorted(selected.items())]
    return receipt, unresolved


def _compact_unresolved_feedback(payload, sources, expected=None):
    """Unresolved spans are receipts only; they never grant display authority."""
    rows = payload.get("repair_contract", {}).get("unresolved_feedback_spans")
    if not isinstance(rows, list):
        raise ValueError("COMPACT_REPAIR_UNRESOLVED_FEEDBACK_INVALID")
    if expected is not None:
        if rows != expected:
            raise ValueError("COMPACT_REPAIR_UNRESOLVED_FEEDBACK_INVALID")
        return rows
    seen = set()
    for row in rows:
        if set(row) != {"target_event_id", "role", "source_id", "start", "end", "status"}:
            raise ValueError("COMPACT_REPAIR_UNRESOLVED_FEEDBACK_INVALID")
        sid, start, end = row.get("source_id"), row.get("start"), row.get("end")
        valid = (isinstance(sid, str) and sid in sources and type(start) is int
                 and type(end) is int and 0 <= start < end <= len(sources[sid]["text"]))
        valid_shape = (row["status"] == _COMPACT_UNRESOLVED_SPAN_STATUS
                       and isinstance(row["target_event_id"], str)
                       and row["target_event_id"] in {item.get("event", {}).get("event_id")
                           for item in payload.get("events", [])}
                       and row["role"] in _COMPACT_SELECTION_ROLES
                       and (sid is None or isinstance(sid, str))
                       and (start is None or type(start) is int)
                       and (end is None or type(end) is int) and not valid)
        key = (row["target_event_id"], row["role"], sid, start, end)
        if not valid_shape or key in seen:
            raise ValueError("COMPACT_REPAIR_UNRESOLVED_FEEDBACK_INVALID")
        seen.add(key)
    return rows


def _compact_windows(payload, pool, original_event, sources, names, saved_events, all_events):
    """Validate selected text and membership without expanding its authority."""
    selected = payload.get("repair_contract", {}).get("selection_receipt")
    rendered = payload.get("evidence_source_pool")
    if not isinstance(selected, list) or not isinstance(rendered, list) or not selected:
        raise ValueError("COMPACT_REPAIR_SELECTION_OR_WINDOWS_REQUIRED")
    expected_selection, expected_unresolved = _compact_expected_selection(
        saved_events, all_events, sources, names)
    if selected != expected_selection:
        raise ValueError("COMPACT_REPAIR_SELECTION_ROLE_OR_COORDINATE_CHANGED")
    _compact_unresolved_feedback(payload, sources, expected_unresolved)
    # This synthetic event object must still contain the full original action and
    # context citations. _allowed_connection_ranges does not use model proposals.
    allowed_connection = _current_boundary_ranges(original_event, sources)
    current_rows = _compact_current_document(payload, sources)
    selection, seen = {}, set()
    for row in selected:
        sid, start, end = row.get("source_id"), row.get("start"), row.get("end")
        if (sid not in sources or type(start) is not int or type(end) is not int
                or start < 0 or end <= start or end > len(sources[sid]["text"])
                or (sid, start, end) in seen or set(row) != {
                    "source_id", "start", "end", "roles"}
                or not isinstance(row["roles"], list) or not row["roles"]
                or row["roles"] != sorted(set(row["roles"]))
                or not set(row["roles"]) <= _COMPACT_SELECTION_ROLES
                or not all(isinstance(role, str) and role for role in row["roles"])):
            raise ValueError("COMPACT_REPAIR_SELECTION_SPAN_INVALID")
        raw = sources[sid]["text"][start:end]
        containing = sorted(block["block_id"] for block in pool.values()
            if block["source_id"] == sid and block["start"] <= start and end <= block["end"])
        overlapping = sorted(block["block_id"] for block in pool.values()
            if block["source_id"] == sid and block["start"] < end and start < block["end"])
        # Exact original-pool subspans need no new authority. Current-document
        # boundary windows are admitted only from the same authenticated parent
        # current document and the same original event's action/connection bound.
        in_pool = bool(containing)
        in_current = (sid in current_rows and any(
            span["start"] <= start and end <= span["end"]
            for span in allowed_connection.get(sid, [])))
        if not in_pool and not in_current:
            raise ValueError("COMPACT_REPAIR_WINDOW_OUTSIDE_PARENT_OR_CURRENT_BOUNDARY")
        key = (sid, start, end)
        selection[key] = dict(row, raw=raw, containing=containing, overlapping=overlapping)
        seen.add(key)

    # Recompute the 0.2.0 sixteen-character adjacency merge, then bind each
    # rendered window to its exact original source and declared basis.
    merged = _compact_merge_windows(list(selection))
    if len(merged) != len(rendered):
        raise ValueError("COMPACT_REPAIR_RENDERED_WINDOW_COUNT_OR_MERGE_MISMATCH")
    overlapping_rendered = set()
    for block, span in zip(rendered, merged):
        sid, start, end = span
        raw = sources[sid]["text"][start:end]
        containing = sorted(block_id for block_id, original in pool.items()
            if original["source_id"] == sid and original["start"] <= start and end <= original["end"])
        basis = ("ORIGINAL_PARENT_EVIDENCE_POOL" if containing else
                 "PARENT_CURRENT_FULL_DOCUMENT_ACTION_OR_CONNECTION_BOUNDARY")
        if (set(block) != {"block_id", "source_id", "start", "end", "text",
                           "authority_basis", "presentation_role"}
                or block.get("block_id") != f"{sid}:{start}:{end}"
                or block.get("source_id") != sid or block.get("start") != start
                or block.get("end") != end
                or block.get("text") != mask_text(raw, names)
                or not block.get("text")
                or block.get("authority_basis") != basis
                or block.get("presentation_role") != "EXACT_ORIGINAL_SOURCE_WINDOW_NOT_NEW_SOURCE_AUTHORITY"):
            raise ValueError("COMPACT_REPAIR_WINDOW_TEXT_COORDINATE_OR_AUTHORITY_CHANGED")
        overlapping_rendered.update(block_id for block_id, original in pool.items()
            if original["source_id"] == sid and original["start"] < end and start < original["end"])
    unrendered = sorted(set(pool) - overlapping_rendered)
    if payload["repair_contract"].get("unrendered_original_block_ids") != unrendered:
        raise ValueError("COMPACT_REPAIR_UNRENDERED_BLOCK_IDS_CHANGED")
    return rendered


def authenticate_compact_event(event, original_event, original, repair, sources, names,
                               target_events=None, all_events=None):
    target_events = ({original_event["event_id"]: original_event} if target_events is None
                     else target_events)
    all_events = target_events if all_events is None else all_events
    saved_events = {item["event"]["event_id"]: target_events[item["event"]["event_id"]]
                    for item in repair["payload"].get("events", [])}
    if any(event_id not in all_events for event_id in saved_events):
        raise ValueError("COMPACT_REPAIR_ORIGINAL_DISCOVERY_EVENTS_REQUIRED")
    contract, pool = _compact_repair_contract(
        original, repair, original_event, sources, names, target_events=target_events)
    # The compact ledger may only contain IDs authorized by original submitted
    # priors or the same-identity set; every full source row is hash-bound.
    for event_id, saved in saved_events.items():
        _compact_ledger(repair["payload"], original["payload"], saved, contract)
        _compact_windows(repair["payload"], pool, saved, sources, names,
                         saved_events, all_events)
    return repair["payload"]["evidence_source_pool"]


def load_authenticated_run(root, cache, endpoint_from_refinement_manifest, *, case_ids=None, strict=True):
    """Old load_run + independent cache/lineage verification + direct receipts."""
    endpoint = sanitized_endpoint(endpoint_from_refinement_manifest)
    root = Path(root)
    if case_ids is None:
        case_ids = {path.parent.name for path in root.glob("SCALE-*/decision_events.json")}
    run = load_run(root, case_ids)
    audit, memo = [], {}
    for case_id, case in run.get("cases", {}).items():
        case_dir = root / case_id
        source_list, _ = import_sources(read(case_dir / "source_fhir_r4_bundle.json"))
        sources = {source["source_id"]: source for source in source_list}
        names = patient_names(source_list)
        receipts = read(case_dir / "model_receipts.json")
        manifest = _manifest(case_dir, endpoint)
        snapshot = ({event["event_id"]: event for event in read(manifest["snapshot_path"])}
                    if manifest else None)
        original_discovery = (Path(manifest["original_input_directory"]) / "discovery.json"
                              if manifest else None)
        original_all_events = ({event["event_id"]: event
            for event in read(original_discovery)["events"]}
            if original_discovery is not None and original_discovery.exists() else None)
        if manifest:
            original_sources, _ = import_sources(read(Path(manifest["original_input_directory"]) / "source_fhir_r4_bundle.json"))
            original_index = {source["source_id"]: (source["source_sha256"], source["text"]) for source in original_sources}
            if original_index != {key: (source["source_sha256"], source["text"]) for key, source in sources.items()}:
                raise ValueError("REFINEMENT_CHANGED_RAW_SOURCE_SET")
        for event in case.get("events", []):
            event_id = event["event_id"]
            context = deepcopy(case.get("contexts", {}).get(event_id, {"blocks": []}))
            for key in ("request_sha256", "request_body_sha256", "cache_file_sha256"):
                context.pop(key, None)
            context.update(measured=False, origin="cache_authentication_not_verified")
            case.setdefault("contexts", {})[event_id] = context
            try:
                verified = authenticate_event(event, cache=cache, endpoint=endpoint, receipts=receipts,
                    sources=sources, names=names, snapshot=snapshot, manifest=manifest, memo=memo,
                    all_events=original_all_events)
            except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
                failure = {"case_id": case_id, "event_id": event_id, "status": "FAILED",
                           "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
                audit.append(failure)
                case["warnings"].append("ACTUAL_REQUEST_AUTHENTICATION_FAILED:" + event_id)
                if strict:
                    raise ValueError("ACTUAL_REQUEST_AUTHENTICATION_FAILED:" + case_id + ":" + event_id + ":" + failure["error"]) from exc
                continue
            context.update({key: verified[key] for key in
                ("request_sha256", "request_body_sha256", "cache_file_sha256")})
            context.update(measured=True, origin="independently_authenticated_cached_" + verified["actual_task"])
            case["input_files"][verified["cache_path"]] = verified["cache_file_sha256"]
            audit.append({"case_id": case_id, "event_id": event_id, **verified})
    run["request_authentication"] = {"status": "VERIFIED" if audit and all(item["status"] == "VERIFIED" for item in audit)
        else "PARTIAL_OR_NOT_MEASURED", "endpoint": endpoint, "cache": str(Path(cache).resolve()),
        "run_case_count": len(list(root.glob("SCALE-*/decision_events.json"))),
        "loaded_case_count": len(run.get("cases", {})), "requested_case_count": len(case_ids),
        "verified_events": sum(item["status"] == "VERIFIED" for item in audit),
        "failed_events": sum(item["status"] == "FAILED" for item in audit), "events": audit,
        "actual_task_labels_preserved": True, "metric_definitions_changed": False}
    return run


def compare_authenticated(reference, contract_path, runs, caches, endpoint, *, strict=True):
    frozen = load_reference(reference)
    contract, receipt = load_contract(contract_path)
    if {item["sha256"] for item in contract["input_reference_manifests"]} != {
            item["sha256"] for item in frozen["frozen_references"]}:
        raise ValueError("AUTHENTICATED_EVALUATION_REFERENCE_CONTRACT_MISMATCH")
    case_ids = {item["case_id"] for item in frozen["items"]}
    reports, authentication = {}, {}
    for name, root in runs.items():
        loaded = load_authenticated_run(root, caches[name], endpoint, case_ids=case_ids, strict=strict)
        # Identical discovery/rejection adapter to the frozen compare(), without
        # changing the frozen loader or relabelling any request task.
        for case_id, case in loaded.get("cases", {}).items():
            path = Path(root) / case_id / "discovery.json"
            case["discovery_available"] = path.exists()
            rejected = []
            if path.exists():
                for unit in read(path).get("units", []):
                    for index, mention in enumerate(unit.get("rejected_mentions", [])):
                        rejected.append({"event_id": f"rejection:{unit['unit_id']}:{index}",
                            "action_citations": mention.get("validated_quotes", {}), "scope": "uncertain",
                            "record_function": "generic", "mention_origin": "semantic_rejection_trace",
                            "reason": mention.get("reason")})
            case["rejected_mention_traces"] = rejected
        reports[name] = evaluate_layers(frozen["items"], loaded)
        authentication[name] = loaded["request_authentication"]
    differences = compare_reports(reports)
    names = list(reports)
    for index, name in enumerate(names[1:], 1):
        for later in names[index + 1:]:
            differences.update(compare_reports({name: reports[name], later: reports[later]}))
    return {"schema_version": "p2-authenticated-layered-comparison-v1",
        "expert_gold": False, "clinical_validation": False, "metric_contract_receipt": receipt,
        "reference_frozen_receipts": frozen["frozen_references"], "runs": reports,
        "differences": differences, "request_authentication": authentication,
        "run_case_counts": {name: {key: audit[key] for key in
            ("run_case_count", "loaded_case_count", "requested_case_count")}
            for name, audit in authentication.items()},
        "evaluated_reference_case_count": len(case_ids),
        "authentication_change_only": True, "frozen_metric_definition_changes": False,
        "measurement_scope": "Same seven reference-covered cases as frozen evaluate_layers.compare; not an implicit expansion of completeness denominators to all ten."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--run", action="append", required=True, help="NAME=ROOT; repeat for primary/refined/baseline")
    parser.add_argument("--cache", action="append", required=True, help="NAME=CACHE; one per named run")
    parser.add_argument("--endpoint-manifest", required=True,
                        help="Sanitized refinement_manifest.json; no API settings or credentials are read")
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-auth-failures", action="store_true",
                        help="Diagnostic only: failed contexts remain unmeasured; never a verified release")
    args = parser.parse_args(argv)
    def named(values):
        result = {}
        for value in values:
            if "=" not in value:
                parser.error("named arguments require NAME=PATH")
            name, path = value.split("=", 1)
            if not name or name in result:
                parser.error("empty or duplicate name")
            result[name] = path
        return result
    runs, caches = named(args.run), named(args.cache)
    if set(runs) != set(caches):
        parser.error("--run and --cache names must match exactly")
    manifest = read(args.endpoint_manifest)
    report = compare_authenticated(args.reference, args.contract, runs, caches,
        manifest["chat_completions_endpoint"], strict=not args.allow_auth_failures)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "evaluation_layers.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (output / "evaluation_layers.md").write_text(render_markdown(report))
    print(json.dumps({"output": str(output.resolve()), "authentication": {
        key: {field: value[field] for field in ("status", "verified_events", "failed_events")}
        for key, value in report["request_authentication"].items()}, "metric_definitions_changed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()

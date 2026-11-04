"""Authenticate original/one-repair evidence requests before frozen evaluation.

This wrapper changes receipt authentication only. It does not relabel repair
calls as original calls, change a metric definition, edit a run, or load API
credentials. The caller supplies the sanitized endpoint recorded in provenance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import import_sources
from scripts.cohort_construction.p2_auto_checkpoint.semantic import mask_text, patient_names
from scripts.cohort_construction.p2_auto_checkpoint_v04.evaluate import (
    file_sha, load_reference, load_run, read,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.evaluate_layers import (
    compare_reports, evaluate_layers, load_contract, render_markdown,
)


PRIMARY_TASK = "document_checkpoint_review"
REPAIR_TASK = "document_checkpoint_review_repair"
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
    return content["task"], content["input"]


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
                       snapshot=None, manifest=None, memo=None):
    """Pure file/source checks; actual primary/repair task labels stay intact."""
    memo = {} if memo is None else memo
    request_hash = event.get("review_request_sha256")
    current = _cached(cache, request_hash, endpoint, memo)
    if not _successful_receipt(receipts, request_hash, current["task"]):
        raise ValueError("RUN_SUCCESS_RECEIPT_MISSING_FOR_ACTUAL_TASK")
    if manifest and current["saved"]["request"].get("model") != manifest.get("model"):
        raise ValueError("REQUEST_MODEL_DIFFERS_FROM_REFINEMENT_MANIFEST")
    current_material = _material(event, current["payload"], sources, names)
    original_hash = None
    if snapshot is not None:
        original_event = snapshot.get(event["event_id"])
        if original_event is None:
            raise ValueError("EVENT_NOT_IN_ORIGINAL_SNAPSHOT")
        if original_event.get("actual_model_evidence") != event.get("actual_model_evidence"):
            raise ValueError("REFINEMENT_CHANGED_FULL_ACTUAL_MODEL_EVIDENCE")
    else:
        original_event = None
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
                    sources=sources, names=names, snapshot=snapshot, manifest=manifest, memo=memo)
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

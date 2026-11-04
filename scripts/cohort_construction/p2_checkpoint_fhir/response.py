"""Expert response-only FHIR exchange; no clinical source text is returned."""
from __future__ import annotations
import copy
import json
from scripts.cohort_construction.p2_checkpoint_packet import text_sha256
from .common import (BASE, EXT, SYSTEM, FhirContractError, entries_by_url,
                     extension, fail, full_url, parse_json_unique, reference, resolve,
                     resource_id, single_ext, validate_fhir_r4)
from .doctor import _json_semantically_equal, verify_packet

RESPONSE_KEYS = [
    "schema-version", "submission-id", "reviewer-assignment-id", "submitted-at",
    "packet-id", "packet-version", "packet-sha256", "clinical-content-sha256",
    "checkpoint-id", "decision-task-id", "visibility-policy-version",
    "questionnaire-canonical", "questionnaire-version", "questionnaire-sha256",
]


def _values(s):
    p, q = s["packet_binding"], s["questionnaire_binding"]
    return [s["schema_version"], s["submission_id"], s["reviewer_assignment_id"], s["submitted_at"],
            p["packet_id"], p["packet_version"], p["packet_sha256"], p["clinical_content_sha256"],
            p["target"]["checkpoint_id"], p["target"]["decision_task_id"], p["target"]["visibility_policy_version"],
            q["canonical"], q["version"], q["sha256"]]


def _encode(submission: dict, response_json: str | None = None) -> dict:
    response = submission["questionnaire_response"]
    if response_json is None:
        response_json = json.dumps(response, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
    response_hash = text_sha256(response_json)
    binding = submission["packet_binding"]["packet_sha256"] + ":" + submission["submission_id"] + ":" + response_hash
    response_url = full_url("response", binding)
    basic = {"resourceType": "Basic", "id": resource_id("submission", binding),
             "identifier": [{"system": BASE + "/identifier/submission", "value": submission["submission_id"]}],
             "code": {"coding": [{"system": SYSTEM, "code": "research-review-submission"}]},
             "extension": [extension("submission-binding", [
                 {"url": key, "valueString": value} for key, value in zip(RESPONSE_KEYS, _values(submission))], "extension"),
                 reference("response", response_url),
                 extension("response-canonical-json", response_json),
                 extension("response-content-sha256", response_hash)]}
    return {"resourceType": "Bundle", "id": resource_id("responsebundle", binding), "type": "collection",
            "identifier": {"system": BASE + "/identifier/response-bundle", "value": response_hash},
            "entry": [{"fullUrl": full_url("submission", binding), "resource": basic},
                      {"fullUrl": response_url, "resource": copy.deepcopy(response)}]}


def response_bundle_from_submission(submission: dict, packet: dict) -> dict:
    """Export only after answer semantics and trusted packet bindings pass."""
    from scripts.cohort_construction.p2_checkpoint_review import validate_submission
    verify_packet(packet)
    validate_submission(submission, packet)
    bundle = _encode(submission)
    validate_fhir_r4(bundle)
    return bundle


def submission_from_response_bundle(bundle: dict, packet: dict, *,
                                    expected_reviewer_assignment_id: str | None = None) -> dict:
    """Reconstruct the existing ReviewSubmission for the existing review ledger.

    The exact response JSON helper can be serialized by Python or by a browser;
    its digest binds those bytes, and its parsed value must equal the FHIR QR.
    Packet identity is independently validated against the supplied trusted packet.
    """
    from scripts.cohort_construction.p2_checkpoint_review import validate_submission
    verify_packet(packet)
    try:
        index = entries_by_url(bundle)
        basics = [r for r in index.values() if r.get("resourceType") == "Basic"]
        if len(basics) != 1 or len(index) != 2:
            fail("FHIR_RESPONSE_EXACT_RESOURCE_SET_REQUIRED")
        basic = basics[0]
        children = single_ext(basic, "submission-binding", "extension")
        if (not isinstance(children, list) or len(children) != len(RESPONSE_KEYS)
                or any(not isinstance(e, dict) or set(e) != {"url", "valueString"}
                       or not isinstance(e["valueString"], str) for e in children)):
            fail("FHIR_SUBMISSION_BINDING_SHAPE")
        b = {e["url"]: e["valueString"] for e in children}
        if set(b) != set(RESPONSE_KEYS):
            fail("FHIR_SUBMISSION_BINDING_KEYS")
        response = resolve(index, single_ext(basic, "response", "valueReference"), "QuestionnaireResponse")
        response_json = single_ext(basic, "response-canonical-json")
        response_hash = single_ext(basic, "response-content-sha256")
        if text_sha256(response_json) != response_hash:
            fail("FHIR_RESPONSE_HASH_MISMATCH")
        try:
            original = parse_json_unique(response_json)
        except (ValueError, TypeError) as exc:
            raise FhirContractError("FHIR_RESPONSE_JSON_INVALID") from exc
        if not isinstance(original, dict) or not _json_semantically_equal(original, response):
            fail("FHIR_RESPONSE_JSON_MISMATCH")
        submission = {"schema_version": b["schema-version"], "submission_id": b["submission-id"],
            "reviewer_assignment_id": b["reviewer-assignment-id"], "submitted_at": b["submitted-at"],
            "packet_binding": {"packet_id": b["packet-id"], "packet_version": b["packet-version"],
                "packet_sha256": b["packet-sha256"], "clinical_content_sha256": b["clinical-content-sha256"],
                "target": {"checkpoint_id": b["checkpoint-id"], "decision_task_id": b["decision-task-id"],
                           "visibility_policy_version": b["visibility-policy-version"]}},
            "questionnaire_binding": {"canonical": b["questionnaire-canonical"],
                "version": b["questionnaire-version"], "sha256": b["questionnaire-sha256"]},
            "questionnaire_response": original}
        validate_submission(submission, packet)
        if (expected_reviewer_assignment_id is not None
                and submission["reviewer_assignment_id"] != expected_reviewer_assignment_id):
            fail("FHIR_TRUSTED_ASSIGNMENT_BINDING_MISMATCH")
        if bundle != _encode(submission, response_json):
            fail("FHIR_NONCANONICAL_OR_EXTRA_RESPONSE_CONTENT")
        validate_fhir_r4(bundle)
        return submission
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise FhirContractError("FHIR_MALFORMED_RESPONSE_BUNDLE") from exc

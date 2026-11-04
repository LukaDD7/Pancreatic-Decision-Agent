"""Doctor-only exchange. Never accept a researcher graph at this entry point."""
from __future__ import annotations
import base64
import binascii
import copy
import json
import math
from scripts.cohort_construction.p2_checkpoint_packet import (
    canonical_sha256, text_sha256, validate_contract,
)
from .common import (
    BASE, EXT, SYSTEM, FhirContractError, entries_by_url, extension, fail,
    full_url, parse_json_unique, reference, resolve, resource_id, single_ext, validate_fhir_r4,
)

BINDING_KEYS = [
    "schema-version", "packet-id", "packet-version", "state", "fhir-version",
    "checkpoint-id", "decision-task-id", "visibility-policy-version",
    "clinical-content-sha256", "packet-sha256", "compiler-version",
    "compiled-partition-sha256", "semantic-audit-id",
]


def verify_packet(packet: dict) -> None:
    """Validate exact packet/form bindings, independently of the FHIR projection."""
    from scripts.cohort_construction.p2_checkpoint_review import validate_questionnaire
    validate_contract("FrozenCheckpointPacket", packet)
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if canonical_sha256(body) != packet["packet_sha256"]:
        fail("FHIR_PACKET_HASH_MISMATCH")
    if canonical_sha256(packet["materials"]) != packet["clinical_content_sha256"]:
        fail("FHIR_CLINICAL_CONTENT_HASH_MISMATCH")
    ids = [m["material_id"] for m in packet["materials"]]
    if len(set(ids)) != len(ids):
        fail("FHIR_DUPLICATE_MATERIAL_ID")
    for material in packet["materials"]:
        if text_sha256(material["text"]) != material["sha256"]:
            fail("FHIR_MATERIAL_HASH_MISMATCH")
    q = packet["questionnaire"]
    if (q["resource"].get("url") != q["canonical"]
            or q["resource"].get("version") != q["version"]
            or q["resource"].get("status") != "active"
            or canonical_sha256(q["resource"]) != q["sha256"]):
        fail("FHIR_QUESTIONNAIRE_BINDING_MISMATCH")
    validate_questionnaire(q["resource"])


def _binding_values(packet: dict) -> list:
    return [packet["schema_version"], packet["packet_id"], packet["packet_version"],
            packet["state"], packet["fhir_version"], packet["target"]["checkpoint_id"],
            packet["target"]["decision_task_id"], packet["target"]["visibility_policy_version"],
            packet["clinical_content_sha256"], packet["packet_sha256"],
            packet["provenance"]["compiler_version"], packet["provenance"]["compiled_partition_sha256"],
            packet["provenance"]["semantic_audit_id"]]


def _encode(packet: dict, episode_id: str | None = None) -> dict:
    digest = packet["packet_sha256"]
    q = packet["questionnaire"]
    qurl = full_url("questionnaire", q["sha256"])
    basic = {"resourceType": "Basic", "id": resource_id("cp", digest),
             "identifier": [{"system": BASE + "/identifier/packet", "value": packet["packet_id"]}],
             "code": {"coding": [{"system": SYSTEM, "code": "research-checkpoint"}]},
             "extension": [extension("packet-binding", [
                 {"url": key, "valueString": value}
                 for key, value in zip(BINDING_KEYS, _binding_values(packet))], "extension"),
                 reference("questionnaire", qurl), extension("questionnaire-sha256", q["sha256"]),
                 extension("questionnaire-frozen", True, "valueBoolean"),
                 extension("questionnaire-canonical-json", json.dumps(q["resource"], ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"), allow_nan=False))]}
    docs = []
    for material in packet["materials"]:
        binding = canonical_sha256(material)
        url = full_url("material", binding)
        basic["extension"].append(reference("material", url))
        attachment = {"contentType": "text/plain;charset=utf-8"}
        if material["text"]:
            attachment["data"] = base64.b64encode(material["text"].encode("utf-8")).decode("ascii")
        else:
            # FHIR primitives cannot be empty strings. An explicit zero-byte
            # attachment represents an empty permitted view without empty data.
            attachment["size"] = 0
        doc = {"resourceType": "DocumentReference", "id": resource_id("doc", binding),
               "identifier": [{"system": BASE + "/identifier/material", "value": material["material_id"]}],
               "status": "current", "extension": [extension("source-id", material["source_id"]),
                   extension("source-version", material["source_version"]),
                   extension("content-sha256", material["sha256"])],
               "content": [{"attachment": attachment}]}
        docs.append({"fullUrl": url, "resource": doc})
    if episode_id is not None:
        if not isinstance(episode_id, str) or not episode_id:
            fail("FHIR_EPISODE_ID_MUST_BE_NONEMPTY_STRING")
        basic["extension"].append(extension("episode-id", episode_id))
    return {"resourceType": "Bundle", "id": resource_id("doctor", digest), "type": "collection",
            "identifier": {"system": BASE + "/identifier/doctor-bundle", "value": digest},
            "entry": [{"fullUrl": full_url("checkpoint", digest), "resource": basic},
                      *docs, {"fullUrl": qurl, "resource": copy.deepcopy(q["resource"])}]}


def doctor_bundle_from_packet(packet: dict, episode_id: str | None = None) -> dict:
    """Only a frozen, independently audited doctor packet can be projected.

    The optional episode_id must already be assigned/approved by the caller; this
    adapter does not invent an Episode or claim an algorithm candidate is a fact.
    """
    verify_packet(packet)
    bundle = _encode(packet, episode_id)
    validate_fhir_r4(bundle)
    return bundle


def _json_semantically_equal(left, right):
    """JSON number 1.0/1 may roundtrip through JS, but boolean true is distinct."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return ((not isinstance(left, float) or math.isfinite(left))
                and (not isinstance(right, float) or math.isfinite(right)) and left == right)
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (set(left) == set(right)
                and all(_json_semantically_equal(left[k], right[k]) for k in left))
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_semantically_equal(a, b) for a, b in zip(left, right))
    return left == right


def _decode(bundle: dict) -> tuple[dict, str | None]:
    index = entries_by_url(bundle)
    basics = [r for r in index.values() if r.get("resourceType") == "Basic"]
    if len(basics) != 1:
        fail("FHIR_EXACTLY_ONE_CURRENT_CHECKPOINT_REQUIRED")
    basic = basics[0]
    children = single_ext(basic, "packet-binding", "extension")
    if (not isinstance(children, list) or len(children) != len(BINDING_KEYS)
            or any(not isinstance(e, dict) or set(e) != {"url", "valueString"}
                   or not isinstance(e["valueString"], str) for e in children)):
        fail("FHIR_PACKET_BINDING_SHAPE")
    bindings = {e["url"]: e["valueString"] for e in children}
    if set(bindings) != set(BINDING_KEYS):
        fail("FHIR_PACKET_BINDING_KEYS")
    qresource = resolve(index, single_ext(basic, "questionnaire", "valueReference"), "Questionnaire")
    qcanonical = single_ext(basic, "questionnaire-canonical-json")
    try:
        qoriginal = parse_json_unique(qcanonical)
    except (ValueError, TypeError) as exc:
        raise FhirContractError("FHIR_QUESTIONNAIRE_CANONICAL_JSON") from exc
    if (not isinstance(qoriginal, dict) or not _json_semantically_equal(qoriginal, qresource)
            or json.dumps(qoriginal, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) != qcanonical):
        fail("FHIR_QUESTIONNAIRE_CANONICAL_JSON_MISMATCH")
    qresource = qoriginal
    qhash = single_ext(basic, "questionnaire-sha256")
    qfrozen = single_ext(basic, "questionnaire-frozen", "valueBoolean")
    materials, seen = [], set()
    for ext in basic.get("extension", []):
        if ext.get("url") != EXT + "material":
            continue
        if set(ext) != {"url", "valueReference"}:
            fail("FHIR_MATERIAL_REFERENCE_SHAPE")
        ref = ext["valueReference"]
        doc = resolve(index, ref, "DocumentReference")
        if ref["reference"] in seen:
            fail("FHIR_DUPLICATE_MATERIAL_REFERENCE")
        seen.add(ref["reference"])
        content = doc.get("content")
        if not isinstance(content, list) or len(content) != 1 or set(content[0]) != {"attachment"}:
            fail("FHIR_MATERIAL_CONTENT_SHAPE")
        attachment = content[0]["attachment"]
        if not isinstance(attachment, dict) or attachment.get("contentType") != "text/plain;charset=utf-8":
            fail("FHIR_INLINE_UTF8_TEXT_ONLY")
        if set(attachment) == {"contentType", "size"} and type(attachment["size"]) is int and attachment["size"] == 0:
            text = ""
        elif set(attachment) == {"contentType", "data"} and attachment["data"]:
            try:
                text = base64.b64decode(attachment["data"], validate=True).decode("utf-8", errors="strict")
            except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
                raise FhirContractError("FHIR_INVALID_BASE64_UTF8") from exc
        else:
            fail("FHIR_INLINE_UTF8_TEXT_ONLY")
        identifiers = doc.get("identifier")
        if not isinstance(identifiers, list) or len(identifiers) != 1:
            fail("FHIR_MATERIAL_IDENTIFIER_SHAPE")
        materials.append({"material_id": identifiers[0].get("value"),
                          "source_id": single_ext(doc, "source-id"),
                          "source_version": single_ext(doc, "source-version"),
                          "text": text, "sha256": single_ext(doc, "content-sha256")})
    if not materials:
        fail("FHIR_PERMITTED_MATERIALS_REQUIRED")
    p = {"schema_version": bindings["schema-version"], "packet_id": bindings["packet-id"],
         "packet_version": bindings["packet-version"], "state": bindings["state"],
         "fhir_version": bindings["fhir-version"],
         "target": {"checkpoint_id": bindings["checkpoint-id"], "decision_task_id": bindings["decision-task-id"],
                    "visibility_policy_version": bindings["visibility-policy-version"]},
         "materials": materials, "clinical_content_sha256": bindings["clinical-content-sha256"],
         "questionnaire": {"canonical": qresource.get("url"), "version": qresource.get("version"),
                           "sha256": qhash, "frozen": qfrozen, "resource": copy.deepcopy(qresource)},
         "provenance": {"compiler_version": bindings["compiler-version"],
                        "compiled_partition_sha256": bindings["compiled-partition-sha256"],
                        "semantic_audit_id": bindings["semantic-audit-id"]},
         "packet_sha256": bindings["packet-sha256"]}
    episode_ext = [e for e in basic.get("extension", []) if e.get("url") == EXT + "episode-id"]
    episode_id = single_ext(basic, "episode-id") if episode_ext else None
    return p, episode_id


def packet_from_doctor_bundle(bundle: dict, *, expected_packet_sha256: str | None = None) -> dict:
    """Reject extra resources/fields and mismatches before any doctor rendering.

    expected_packet_sha256 is the trusted assignment/ledger binding. Without it,
    hashes establish self-consistency only, not who authorized the packet.
    """
    try:
        packet, episode_id = _decode(bundle)
        verify_packet(packet)
        if expected_packet_sha256 is not None and packet["packet_sha256"] != expected_packet_sha256:
            fail("FHIR_TRUSTED_PACKET_BINDING_MISMATCH")
        if bundle != _encode(packet, episode_id):
            fail("FHIR_NONCANONICAL_OR_EXTRA_DOCTOR_CONTENT")
        validate_fhir_r4(bundle)
        return packet
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise FhirContractError("FHIR_MALFORMED_DOCTOR_BUNDLE") from exc

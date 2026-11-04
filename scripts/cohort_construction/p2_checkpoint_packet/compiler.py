"""Compile exact source partitions without inferring clinical semantics.

All offsets are Unicode code-point, zero-based, half-open intervals.  UTF-8
hashing preserves CR/LF and performs no Unicode normalization.  Researcher
results contain all source text; doctor packets are separate allowlist-built
objects containing only KEEP text and neutral placeholders.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "0.1.0"
COMPILER_VERSION = "0.1.0"
PLACEHOLDER = "[资料按方案遮蔽]"
SCHEMA_DIR = Path(__file__).with_name("schemas")


class ContractError(ValueError):
    """A malformed, mismatched, or unreleasable contract."""


def text_sha256(text: str) -> str:
    """Hash the exact Unicode string as UTF-8; never normalize line endings."""
    try:
        return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()
    except (AttributeError, UnicodeError) as exc:
        raise ContractError("Source text must be a valid Unicode string") from exc


def canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("Object cannot be canonically serialized") from exc
    return text_sha256(encoded)


def _validate(value: Any, schema: dict, root: dict, path: str) -> None:
    """Validate only the deliberately small vocabulary used by local schemas.

    This is not a general JSON Schema or FHIR validator.  Schema files are
    Draft 2020-12 and may also be checked with an independent standard validator.
    """
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ContractError("Only local schema references are supported")
        resolved = root
        for part in ref[2:].split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        _validate(value, resolved, root, path)
    types = {"object": lambda x: isinstance(x, dict),
             "array": lambda x: isinstance(x, list),
             "string": lambda x: isinstance(x, str),
             "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
             "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
             "boolean": lambda x: isinstance(x, bool),
             "null": lambda x: x is None}
    if "type" in schema:
        allowed = schema["type"]
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if not any(types[name](value) for name in allowed):
            raise ContractError(f"{path}: expected {allowed}")
    if "const" in schema and (value != schema["const"] or
                              isinstance(schema["const"], bool) != isinstance(value, bool)):
        raise ContractError(f"{path}: wrong constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path}: value is not allowed")
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _validate(value, branch, root, path)
            except ContractError:
                continue
            matches += 1
        if matches != 1:
            raise ContractError(f"{path}: exactly one locator/variant is required")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ContractError(f"{path}: string is too short")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ContractError(f"{path}: string does not match contract pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"{path}: below minimum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ContractError(f"{path}: too few items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], root, f"{path}[{index}]")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ContractError(f"{path}.{name}: required")
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], root, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise ContractError(f"{path}.{name}: unexpected property")


def validate_contract(name: str, value: dict) -> None:
    """Validate one of this package's fixed local JSON contracts."""
    allowed = {p.stem.removesuffix(".schema") for p in SCHEMA_DIR.glob("*.schema.json")}
    if name not in allowed:
        raise ContractError(f"Unknown local contract: {name}")
    schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    _validate(value, schema, schema, name)


def build_source_manifest(manifest_id: str, sources: list[dict]) -> dict:
    """Create a researcher-only manifest from exact source_id/version/text."""
    result = {"schema_version": CONTRACT_VERSION, "manifest_id": manifest_id, "sources": []}
    for source in sources:
        text = source["text"]
        result["sources"].append({"source_id": source["source_id"],
                                  "source_version": source["source_version"], "text": text,
                                  "sha256": text_sha256(text), "length_codepoints": len(text),
                                  "coordinate_unit": "unicode_codepoint", "newline_policy": "preserve"})
    _verified_sources(result)
    return result


def _verified_sources(manifest: dict) -> dict[str, dict]:
    validate_contract("SourceManifest", manifest)
    source_map = {}
    for source in manifest["sources"]:
        source_id = source["source_id"]
        if source_id in source_map:
            raise ContractError(f"DUPLICATE_SOURCE_ID: {source_id}")
        if text_sha256(source["text"]) != source["sha256"]:
            raise ContractError(f"SOURCE_HASH_MISMATCH: {source_id}")
        if len(source["text"]) != source["length_codepoints"]:
            raise ContractError(f"SOURCE_LENGTH_MISMATCH: {source_id}")
        source_map[source_id] = source
    return source_map


def _anchor_id(source: dict, start: int, end: int) -> str:
    return "A-" + canonical_sha256({"source_id": source["source_id"],
                                    "source_version": source["source_version"],
                                    "source_sha256": source["sha256"], "start": start, "end": end})


def locate_exact_quote(manifest: dict, source_id: str, exact_quote: str,
                       prefix: str | None = None, suffix: str | None = None) -> dict:
    """Return every exact match (including overlapping matches), never first-only.

    Optional prefix/suffix must be immediately adjacent original characters.
    Offsets supplied by an LLM are unnecessary.  Returned tool anchors remain
    bound to the exact source version and hash.
    """
    sources = _verified_sources(manifest)
    if source_id not in sources:
        raise ContractError(f"UNKNOWN_SOURCE_ID: {source_id}")
    if not isinstance(exact_quote, str) or not exact_quote:
        raise ContractError("EMPTY_EXACT_QUOTE")
    if prefix is not None and not isinstance(prefix, str):
        raise ContractError("prefix must be text")
    if suffix is not None and not isinstance(suffix, str):
        raise ContractError("suffix must be text")
    source = sources[source_id]
    return _locate_in_verified_source(source, exact_quote, prefix, suffix)


def _locate_in_verified_source(source: dict, exact_quote: str,
                               prefix: str | None, suffix: str | None) -> dict:
    """Avoid rehashing every source for each segment during one compile."""
    source_id = source["source_id"]
    text = source["text"]
    anchors, cursor = [], 0
    while True:
        start = text.find(exact_quote, cursor)
        if start < 0:
            break
        end, cursor = start + len(exact_quote), start + 1
        if prefix is not None and text[max(0, start - len(prefix)):start] != prefix:
            continue
        if suffix is not None and text[end:end + len(suffix)] != suffix:
            continue
        anchors.append({"anchor_id": _anchor_id(source, start, end), "source_id": source_id,
                        "source_version": source["source_version"], "source_sha256": source["sha256"],
                        "start": start, "end": end, "exact_quote": exact_quote})
    return {"schema_version": CONTRACT_VERSION, "source_id": source_id,
            "source_sha256": source["sha256"], "coordinate_unit": "unicode_codepoint",
            "status": "UNIQUE" if len(anchors) == 1 else "AMBIGUOUS" if anchors else "NOT_FOUND",
            "anchors": anchors}


def _issue(issues: list, code: str, *, source_id: str | None = None,
           segment_id: str | None = None, start: int | None = None,
           end: int | None = None) -> None:
    issue = {"code": code}
    for name, value in (("source_id", source_id), ("segment_id", segment_id), ("start", start), ("end", end)):
        if value is not None:
            issue[name] = value
    issues.append(issue)


def _resolve_segment(segment: dict, source: dict, anchors: dict[str, dict],
                     issues: list) -> tuple[int, int] | None:
    sid, segid, text = source["source_id"], segment["segment_id"], source["text"]
    if "quote_locator" in segment:
        locator = segment["quote_locator"]
        match = _locate_in_verified_source(source, locator["exact_quote"],
                                           locator.get("prefix"), locator.get("suffix"))
        if match["status"] != "UNIQUE":
            _issue(issues, "QUOTE_NOT_FOUND" if not match["anchors"] else "QUOTE_AMBIGUOUS",
                   source_id=sid, segment_id=segid)
            return None
        anchor = match["anchors"][0]
        return anchor["start"], anchor["end"]
    if "resolved_span" in segment:
        span = segment["resolved_span"]
        start, end = span["start"], span["end"]
        if not 0 <= start < end <= len(text):
            _issue(issues, "INVALID_SOURCE_SPAN", source_id=sid, segment_id=segid)
            return None
        if text[start:end] != span["exact_quote"]:
            _issue(issues, "SPAN_QUOTE_MISMATCH", source_id=sid, segment_id=segid)
            return None
        return start, end
    anchor = anchors.get(segment["anchor_id"])
    if anchor is None:
        _issue(issues, "UNKNOWN_ANCHOR_ID", source_id=sid, segment_id=segid)
        return None
    start, end = anchor["start"], anchor["end"]
    if (anchor["source_id"] != sid or anchor["source_version"] != source["source_version"]
            or anchor["source_sha256"] != source["sha256"]
            or not 0 <= start < end <= len(text)
            or text[start:end] != anchor["exact_quote"]
            or anchor["anchor_id"] != _anchor_id(source, start, end)):
        _issue(issues, "ANCHOR_SOURCE_BINDING_MISMATCH", source_id=sid, segment_id=segid)
        return None
    return start, end


def compile_partition(manifest: dict, proposal: dict, *, anchor_catalog: list[dict] | None = None) -> dict:
    """Compile complete, mutually exclusive KEEP/MASK/REVIEW partitions.

    Malformed contracts or stale hashes are rejected without output. Semantic
    input defects (ambiguous quotes, missing coverage, overlapping proposals)
    are retained in an audit and rendered REVIEW, never inferred as KEEP.
    """
    sources = _verified_sources(manifest)
    validate_contract("SplitProposal", proposal)
    if proposal["manifest_id"] != manifest["manifest_id"]:
        raise ContractError("MANIFEST_ID_MISMATCH")
    bindings = {}
    for binding in proposal["source_bindings"]:
        sid = binding["source_id"]
        if sid in bindings:
            raise ContractError(f"DUPLICATE_SOURCE_BINDING: {sid}")
        bindings[sid] = binding
    if set(bindings) != set(sources):
        raise ContractError("SOURCE_BINDING_SET_MISMATCH")
    for sid, source in sources.items():
        if bindings[sid] != {"source_id": sid, "source_version": source["source_version"],
                             "sha256": source["sha256"]}:
            raise ContractError(f"SOURCE_BINDING_MISMATCH: {sid}")
    anchors = {}
    for anchor in anchor_catalog or []:
        validate_contract("SourceAnchor", anchor)
        if anchor["anchor_id"] in anchors:
            raise ContractError("DUPLICATE_ANCHOR_ID")
        anchors[anchor["anchor_id"]] = copy.deepcopy(anchor)
    issues, resolved, segment_ids = [], {sid: [] for sid in sources}, set()
    for segment in proposal["segments"]:
        segid, sid = segment["segment_id"], segment["source_id"]
        if segid in segment_ids:
            raise ContractError(f"DUPLICATE_SEGMENT_ID: {segid}")
        segment_ids.add(segid)
        if sid not in sources:
            _issue(issues, "UNKNOWN_SEGMENT_SOURCE", source_id=sid, segment_id=segid)
            continue
        span = _resolve_segment(segment, sources[sid], anchors, issues)
        if span is not None:
            resolved[sid].append({"start": span[0], "end": span[1],
                                  "disposition": segment["disposition"], "segment_id": segid})
    partitions = []
    for sid, source in sources.items():
        source_segments = resolved[sid]
        boundaries = sorted({0, len(source["text"])} |
                            {p[k] for p in source_segments for k in ("start", "end")})
        partition = []
        for start, end in zip(boundaries, boundaries[1:]):
            if start == end:
                continue
            covering = [p for p in source_segments if p["start"] <= start and end <= p["end"]]
            reason = None
            if not covering:
                disposition, reason = "REVIEW", "UNASSIGNED_GAP"
                _issue(issues, reason, source_id=sid, start=start, end=end)
            elif len(covering) > 1:
                disposition, reason = "REVIEW", "OVERLAPPING_PROPOSALS"
                _issue(issues, reason, source_id=sid, start=start, end=end)
            else:
                disposition = covering[0]["disposition"]
            partition.append({"start": start, "end": end, "disposition": disposition,
                              "text": source["text"][start:end],
                              "proposal_segment_ids": [p["segment_id"] for p in covering],
                              "generated_reason": reason})
        partitions.append({"source_id": sid, "source_version": source["source_version"],
                           "source_sha256": source["sha256"], "length_codepoints": len(source["text"]),
                           "segments": partition})
    review_count = sum(p["disposition"] == "REVIEW" for s in partitions for p in s["segments"])
    result = {"schema_version": CONTRACT_VERSION, "compiler_version": COMPILER_VERSION,
              "manifest_id": manifest["manifest_id"], "proposal_id": proposal["proposal_id"],
              "proposal_version": proposal["proposal_version"], "proposal_sha256": canonical_sha256(proposal),
              "target": copy.deepcopy(proposal["target"]), "coordinate_unit": "unicode_codepoint",
              "sources": partitions,
              "mechanical_audit": {"status": "PASS" if not issues else "FAIL", "issues": issues,
                                   "review_segment_count": review_count, "full_coverage": True,
                                   "mutually_exclusive": True},
              "semantic_audit_status": "NOT_ASSESSED"}
    result["compiled_sha256"] = canonical_sha256(result)
    validate_contract("CompiledPartition", result)
    return result


def _verify_compiled(compiled: dict) -> None:
    validate_contract("CompiledPartition", compiled)
    material = {k: v for k, v in compiled.items() if k != "compiled_sha256"}
    if canonical_sha256(material) != compiled["compiled_sha256"]:
        raise ContractError("COMPILED_HASH_MISMATCH")
    if (compiled["mechanical_audit"]["status"] != "PASS"
            or compiled["mechanical_audit"]["issues"]):
        raise ContractError("MECHANICAL_AUDIT_NOT_PASSED")
    for source in compiled["sources"]:
        cursor, original = 0, []
        for span in source["segments"]:
            if span["disposition"] == "REVIEW":
                raise ContractError("UNRESOLVED_REVIEW_SEGMENT")
            if span["start"] != cursor or span["end"] <= cursor:
                raise ContractError("PARTITION_IS_NOT_COMPLETE_AND_EXCLUSIVE")
            if len(span["text"]) != span["end"] - span["start"]:
                raise ContractError("PARTITION_TEXT_LENGTH_MISMATCH")
            original.append(span["text"])
            cursor = span["end"]
        if cursor != source["length_codepoints"] or text_sha256("".join(original)) != source["source_sha256"]:
            raise ContractError("PARTITION_SOURCE_HASH_MISMATCH")


def project_doctor_materials(compiled: dict) -> list[dict]:
    """Project KEEP text and one neutral placeholder per concealed span.

    REVIEW is withheld exactly like MASK: this projection is also used by the
    automated reviewer, so a draft cannot be reviewed as if it were releasable.
    It does not grant semantic, human-sampling or clinical release permission.
    """
    materials = []
    for source in compiled["sources"]:
        pieces, masking = [], False
        for span in source["segments"]:
            if span["disposition"] in ("MASK", "REVIEW"):
                if not masking:
                    pieces.append(PLACEHOLDER)
                masking = True
            else:
                pieces.append(span["text"])
                masking = False
        visible_text = "".join(pieces)
        materials.append({"material_id": "DVM-" + canonical_sha256({"source_id": source["source_id"],
                                                                     "text": visible_text})[:24],
                          "source_id": source["source_id"], "source_version": source["source_version"],
                          "text": visible_text, "sha256": text_sha256(visible_text)})
    return materials


def freeze_checkpoint_packet(compiled: dict, semantic_audit: dict, questionnaire: dict, *,
                             packet_id: str, packet_version: str) -> dict:
    """Project a separately audited partition into a doctor-only packet.

    The semantic assessment is supplied by an independent review process; this
    function verifies its binding/attestation, not its clinical truth. It never
    creates a semantic PASS from a mechanical PASS.
    """
    from .semantic_audit import validate_semantic_audit
    _verify_compiled(compiled)
    if semantic_audit["status"] != "PASS" or not semantic_audit["independence_attested"]:
        raise ContractError("INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED")
    validate_semantic_audit(compiled, semantic_audit)
    validate_contract("FrozenQuestionnaire", questionnaire)
    resource = questionnaire["resource"]
    if (resource.get("resourceType") != "Questionnaire" or resource.get("status") != "active"
            or resource.get("url") != questionnaire["canonical"]
            or resource.get("version") != questionnaire["version"]
            or canonical_sha256(resource) != questionnaire["sha256"]):
        raise ContractError("QUESTIONNAIRE_BINDING_MISMATCH")
    materials = project_doctor_materials(compiled)
    packet = {"schema_version": CONTRACT_VERSION, "packet_id": packet_id, "packet_version": packet_version,
              "state": "frozen_for_review", "fhir_version": "4.0.1",
              "target": copy.deepcopy(compiled["target"]), "materials": materials,
              "clinical_content_sha256": canonical_sha256(materials),
              "questionnaire": copy.deepcopy(questionnaire),
              "provenance": {"compiler_version": COMPILER_VERSION,
                             "compiled_partition_sha256": compiled["compiled_sha256"],
                             "semantic_audit_id": semantic_audit["assessment_id"]}}
    packet["packet_sha256"] = canonical_sha256(packet)
    validate_contract("FrozenCheckpointPacket", packet)
    return packet


def _verify_packet(packet: dict) -> None:
    validate_contract("FrozenCheckpointPacket", packet)
    if canonical_sha256({k: v for k, v in packet.items() if k != "packet_sha256"}) != packet["packet_sha256"]:
        raise ContractError("PACKET_HASH_MISMATCH")
    if canonical_sha256(packet["materials"]) != packet["clinical_content_sha256"]:
        raise ContractError("CLINICAL_CONTENT_HASH_MISMATCH")
    if any(text_sha256(m["text"]) != m["sha256"] for m in packet["materials"]):
        raise ContractError("VISIBLE_MATERIAL_HASH_MISMATCH")
    q = packet["questionnaire"]
    if canonical_sha256(q["resource"]) != q["sha256"]:
        raise ContractError("QUESTIONNAIRE_HASH_MISMATCH")


def make_review_submission(packet: dict, questionnaire_response: dict, *, submission_id: str,
                           reviewer_assignment_id: str, submitted_at: str) -> dict:
    """Bind actual expert answers to the exact packet and fixed-version form."""
    _verify_packet(packet)
    result = {"schema_version": CONTRACT_VERSION, "submission_id": submission_id,
              "reviewer_assignment_id": reviewer_assignment_id, "submitted_at": submitted_at,
              "packet_binding": {key: copy.deepcopy(packet[key]) for key in
                                 ("packet_id", "packet_version", "packet_sha256", "clinical_content_sha256", "target")},
              "questionnaire_binding": {key: packet["questionnaire"][key] for key in ("canonical", "version", "sha256")},
              "questionnaire_response": copy.deepcopy(questionnaire_response)}
    validate_review_submission(result, packet)
    return result


def validate_review_submission(submission: dict, packet: dict) -> None:
    """Check structural/binding integrity, not expert identity or FHIR validity."""
    _verify_packet(packet)
    validate_contract("ReviewSubmission", submission)
    expected_packet = {key: packet[key] for key in
                       ("packet_id", "packet_version", "packet_sha256", "clinical_content_sha256", "target")}
    expected_q = {key: packet["questionnaire"][key] for key in ("canonical", "version", "sha256")}
    if submission["packet_binding"] != expected_packet:
        raise ContractError("SUBMISSION_PACKET_BINDING_MISMATCH")
    if submission["questionnaire_binding"] != expected_q:
        raise ContractError("SUBMISSION_QUESTIONNAIRE_BINDING_MISMATCH")
    response = submission["questionnaire_response"]
    if (response.get("resourceType") != "QuestionnaireResponse"
            or response.get("questionnaire") != f"{expected_q['canonical']}|{expected_q['version']}"):
        raise ContractError("RESPONSE_QUESTIONNAIRE_BINDING_MISMATCH")

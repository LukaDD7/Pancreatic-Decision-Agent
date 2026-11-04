"""Shared FHIR wire-format helpers; no clinical classification or data discovery."""
from __future__ import annotations
import copy
import json
import math
import uuid
from functools import lru_cache
from pathlib import Path
from scripts.cohort_construction.p2_checkpoint_packet import canonical_sha256

BASE = "https://checkpoint-research.invalid/fhir"
EXT = BASE + "/StructureDefinition/"
SYSTEM = BASE + "/CodeSystem/research-object"
ADAPTER_VERSION = "0.1.0-draft"


class FhirContractError(ValueError):
    """An unsafe, inconsistent, or unsupported local draft exchange object."""

    @property
    def code(self):
        return str(self).split(":", 1)[0]



def fail(code: str):
    raise FhirContractError(code)


def full_url(kind: str, binding: str) -> str:
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, BASE + "/" + kind + "/" + binding))


def resource_id(kind: str, binding: str) -> str:
    return kind + "-" + canonical_sha256(binding)[:32]


def extension(name: str, value, kind: str = "valueString") -> dict:
    return {"url": EXT + name, kind: copy.deepcopy(value)}


def reference(name: str, url: str) -> dict:
    return extension(name, {"reference": url}, "valueReference")


def single_ext(resource: dict, name: str, kind: str = "valueString"):
    matches = [e for e in resource.get("extension", []) if e.get("url") == EXT + name]
    if len(matches) != 1 or set(matches[0]) != {"url", kind}:
        fail("FHIR_EXTENSION_CARDINALITY_OR_TYPE:" + name)
    return copy.deepcopy(matches[0][kind])


def entries_by_url(bundle: dict) -> dict:
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle" or bundle.get("type") != "collection":
        fail("FHIR_COLLECTION_BUNDLE_REQUIRED")
    entries = bundle.get("entry")
    if not isinstance(entries, list) or not entries:
        fail("FHIR_ENTRIES_REQUIRED")
    index = {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"fullUrl", "resource"}
                or not isinstance(entry["fullUrl"], str) or not entry["fullUrl"].startswith("urn:uuid:")
                or not isinstance(entry["resource"], dict)):
            fail("FHIR_ENTRY_NOT_LOCAL_RESOURCE")
        if entry["fullUrl"] in index:
            fail("FHIR_DUPLICATE_FULLURL")
        index[entry["fullUrl"]] = entry["resource"]
    return index


def resolve(index: dict, ref: dict, resource_type: str) -> dict:
    if not isinstance(ref, dict) or set(ref) != {"reference"}:
        fail("FHIR_REFERENCE_SHAPE")
    target = index.get(ref["reference"])
    if target is None or target.get("resourceType") != resource_type:
        fail("FHIR_UNRESOLVED_OR_WRONG_TYPE_REFERENCE")
    return target


def parse_json_unique(text: str):
    """A JSON helper must not smuggle discarded duplicate keys or NaN values."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("FHIR_DUPLICATE_JSON_HELPER_KEY")
            result[key] = value
        return result
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            fail("FHIR_NONFINITE_JSON_HELPER_NUMBER")
        return result
    result = json.loads(text, object_pairs_hook=pairs, parse_float=finite_float,
                        parse_constant=lambda _: fail("FHIR_NONFINITE_JSON_HELPER_NUMBER"))
    def unicode_values(value):
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise FhirContractError("FHIR_UNPAIRED_JSON_SURROGATE") from exc
        elif isinstance(value, dict):
            for key, item in value.items():
                unicode_values(key)
                unicode_values(item)
        elif isinstance(value, list):
            for item in value:
                unicode_values(item)
    unicode_values(result)
    return result


@lru_cache(maxsize=1)
def _schema_validator():
    import jsonschema
    path = Path(__file__).resolve().parents[3] / "private/p2_auto_checkpoint_v0.1_20261024/standards/fhir.schema.json"
    if not path.is_file():
        fail("FHIR_R4_SCHEMA_UNAVAILABLE")
    schema = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft6Validator.check_schema(schema)
    return jsonschema.Draft6Validator(schema)


def validate_fhir_r4(resource: dict) -> None:
    """Validate actual instances against the repository's complete official R4 JSON Schema.

    This checks R4 JSON structure, not the terminology server, FHIRPath invariants,
    clinical truth, independently trusted signatures, or a published local profile.
    """
    errors = sorted(_schema_validator().iter_errors(resource), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        fail("FHIR_R4_SCHEMA:" + "/".join(map(str, error.absolute_path)) + ":" + str(error.validator))

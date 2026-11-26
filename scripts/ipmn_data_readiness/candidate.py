import hashlib
import json
from pathlib import Path

import jsonschema

from .measurement import failure, longitudinal_rate, validate_measurement_rows

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts/ipmn/v0.1/candidate_manifest.schema.json"

SUPPORTED_FORMULAS = {
    "signed_size_change / observation_days": "cyst_size_mm",
    "signed_mpd_change / observation_days": "mpd_mm",
}


def canonical_sha256(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def engine_sha256():
    return canonical_sha256({
        "candidate.py": file_sha256(Path(__file__)),
        "measurement.py": file_sha256(Path(__file__).with_name("measurement.py")),
    })


def validate_candidate_manifest(manifest, expected_summary_sha256=None, require_frozen=True):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(schema).validate(manifest)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path)
        raise ValueError(f"Invalid candidate manifest at {location}: {exc.validator}") from exc
    errors = []
    if require_frozen and manifest["status"] != "frozen":
        errors.append("manifest is not frozen")
    if expected_summary_sha256 and manifest["readiness_summary_sha256"] != expected_summary_sha256:
        errors.append("manifest belongs to a different readiness summary")
    candidates = manifest["candidates"]
    hypothesis_ids = [candidate["hypothesis_id"] for candidate in candidates]
    if len(hypothesis_ids) != len(set(hypothesis_ids)):
        errors.append("duplicate hypothesis_id")
    by_id = {candidate["hypothesis_id"]: candidate for candidate in candidates}
    primary_id = manifest["primary_hypothesis_id"]
    selected_primary = [candidate for candidate in candidates if candidate["track"] == "primary" and candidate["selection_status"] == "selected"]
    if len(selected_primary) != 1 or selected_primary[0]["hypothesis_id"] != primary_id:
        errors.append("exactly one selected primary candidate must match primary_hypothesis_id")
    elif selected_primary[0]["known_status"] == "known":
        errors.append("a known rediscovery cannot be the primary new phenotype")
    if len(candidates) > manifest["selection"]["candidate_budget"]:
        errors.append("candidate budget exceeded")
    for candidate in candidates:
        hypothesis_id = candidate["hypothesis_id"]
        window = candidate["time_window"]
        if window["start_relative_to_entry_days"] > window["end_relative_to_entry_days"]:
            errors.append(f"{hypothesis_id}: invalid time window")
        if candidate["selection_status"] != "selected" and not candidate.get("failure_reason"):
            errors.append(f"{hypothesis_id}: non-selected candidate lacks failure_reason")
        parent = candidate.get("parent_hypothesis_id")
        if parent and (parent == hypothesis_id or parent not in by_id):
            errors.append(f"{hypothesis_id}: invalid parent_hypothesis_id")
        if candidate["formula"] in SUPPORTED_FORMULAS and candidate["code_sha256"] != engine_sha256():
            errors.append(f"{hypothesis_id}: built-in engine hash mismatch")
        for field in candidate["source_fields"]:
            lowered = field.lower()
            if any(term in lowered for term in ("postlandmark", "outcome", "target_event", "hgd", "cpdac")):
                errors.append(f"{hypothesis_id}: outcome or post-landmark source field is prohibited")
    if errors:
        raise ValueError("; ".join(errors))


def compute_candidate_values(manifest, rows, patient_ids=None):
    validate_candidate_manifest(manifest)
    validate_measurement_rows(rows)
    candidate = next(candidate for candidate in manifest["candidates"] if candidate["hypothesis_id"] == manifest["primary_hypothesis_id"])
    formula = candidate["formula"]
    if formula not in SUPPORTED_FORMULAS:
        raise ValueError(f"Unsupported frozen formula: {formula}")
    if candidate["unit"] != "mm/day":
        raise ValueError("Longitudinal rate candidates must use mm/day")
    variable = SUPPORTED_FORMULAS[formula]
    if variable not in candidate["source_fields"]:
        raise ValueError(f"Frozen source_fields do not include {variable}")
    if patient_ids is None:
        patient_ids = sorted({row["research_id"] for row in rows})
    else:
        if not isinstance(patient_ids, list) or any(not isinstance(patient_id, str) or not patient_id.strip() for patient_id in patient_ids):
            raise ValueError("patient_ids must be a list of non-empty strings")
        if len(patient_ids) != len(set(patient_ids)):
            raise ValueError("patient_ids must be unique")
        outside = sorted({row["research_id"] for row in rows} - set(patient_ids))
        if outside:
            raise ValueError(f"Measurement rows outside the requested patient set: {outside}")
        patient_ids = sorted(patient_ids)
    by_patient = {patient_id: [] for patient_id in patient_ids}
    for row in rows:
        by_patient[row["research_id"]].append(row)
    manifest_sha256 = canonical_sha256(manifest)
    window = candidate["time_window"]
    results = []
    for patient_id in patient_ids:
        patient_rows = by_patient[patient_id]
        if patient_rows:
            computed = longitudinal_rate(
                patient_rows,
                variable,
                window["start_relative_to_entry_days"],
                window["end_relative_to_entry_days"],
                candidate["minimum_observations"],
            )
        else:
            computed = failure("NO_VISIBLE_MEASUREMENT")
        results.append({
            "research_id": patient_id,
            "hypothesis_id": candidate["hypothesis_id"],
            "manifest_sha256": manifest_sha256,
            "unit": candidate["unit"],
            **computed,
        })
    return results

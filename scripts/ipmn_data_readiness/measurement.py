import math
from datetime import date

MEASUREMENT_FIELDS = {
    "research_id",
    "measurement_id",
    "entry_date",
    "object_id",
    "exam_date",
    "available_date",
    "availability_basis",
    "modality",
    "protocol_id",
    "variable",
    "value",
    "unit",
    "source_ref",
    "measurement_version",
    "review",
}

REVIEW_FIELDS = {
    "identity_status",
    "protocol_status",
    "reviewer_role",
    "reviewed_at",
    "evidence_refs",
}


def iso_date(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def validate_measurement_rows(rows):
    if not isinstance(rows, list):
        raise ValueError("measurement rows must be a list")
    measurement_ids = set()
    patient_entries = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"measurement row {index} must be an object")
        missing = MEASUREMENT_FIELDS - set(row)
        unknown = set(row) - MEASUREMENT_FIELDS
        if missing or unknown:
            raise ValueError(f"measurement row {index} has missing fields {sorted(missing)} or unknown fields {sorted(unknown)}")
        for field in ("research_id", "measurement_id", "object_id", "modality", "protocol_id", "source_ref", "measurement_version"):
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError(f"measurement row {index} has invalid {field}")
        if row["measurement_id"] in measurement_ids:
            raise ValueError(f"duplicate measurement_id: {row['measurement_id']}")
        measurement_ids.add(row["measurement_id"])
        entry = iso_date(row["entry_date"], "entry_date")
        exam = iso_date(row["exam_date"], "exam_date")
        available = iso_date(row["available_date"], "available_date")
        if available < exam:
            raise ValueError(f"measurement row {index} has available_date before exam_date")
        if row["research_id"] in patient_entries and patient_entries[row["research_id"]] != entry:
            raise ValueError(f"inconsistent entry_date for {row['research_id']}")
        patient_entries[row["research_id"]] = entry
        if row["availability_basis"] not in {"reported_available_time", "reviewed_exam_date_proxy"}:
            raise ValueError(f"measurement row {index} has invalid availability_basis")
        if row["availability_basis"] == "reviewed_exam_date_proxy" and available != exam:
            raise ValueError(f"measurement row {index} has an inconsistent exam-date availability proxy")
        if row["variable"] not in {"cyst_size_mm", "mpd_mm"} or row["unit"] != "mm":
            raise ValueError(f"measurement row {index} has an unsupported variable or unit")
        value = row["value"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise ValueError(f"measurement row {index} has an invalid numeric value")
        review = row["review"]
        if not isinstance(review, dict) or set(review) != REVIEW_FIELDS:
            raise ValueError(f"measurement row {index} has an invalid review object")
        for field in ("identity_status", "protocol_status"):
            if review[field] not in {"pending", "passed", "failed"}:
                raise ValueError(f"measurement row {index} has invalid {field}")
        if "passed" in (review["identity_status"], review["protocol_status"]):
            if not isinstance(review["reviewer_role"], str) or not review["reviewer_role"].strip() or not isinstance(review["reviewed_at"], str) or not review["reviewed_at"].strip():
                raise ValueError(f"measurement row {index} passed review lacks role or date")
            if not isinstance(review["evidence_refs"], list) or not review["evidence_refs"] or any(not isinstance(ref, str) or not ref.strip() for ref in review["evidence_refs"]):
                raise ValueError(f"measurement row {index} passed review lacks evidence_refs")


def failure(reason, eligible_count=0):
    return {"status": "not_computable", "value": None, "reason": reason, "eligible_observation_count": eligible_count}


def longitudinal_rate(rows, variable, start_days, end_days, minimum_observations):
    eligible = []
    for row in rows:
        if row["variable"] != variable:
            continue
        entry = date.fromisoformat(row["entry_date"])
        exam_offset = (date.fromisoformat(row["exam_date"]) - entry).days
        available_offset = (date.fromisoformat(row["available_date"]) - entry).days
        if start_days <= exam_offset <= end_days and start_days <= available_offset <= end_days:
            eligible.append(row)
    if not eligible:
        return failure("NO_VISIBLE_MEASUREMENT")
    if len({row["object_id"] for row in eligible}) != 1:
        return failure("AMBIGUOUS_MEASUREMENT_OBJECT", len(eligible))
    if len({row["modality"] for row in eligible}) != 1:
        return failure("CROSS_MODALITY_MEASUREMENT", len(eligible))
    if len({row["protocol_id"] for row in eligible}) != 1:
        return failure("INCOMPARABLE_PROTOCOL", len(eligible))
    if len({row["measurement_version"] for row in eligible}) != 1:
        return failure("AMBIGUOUS_MEASUREMENT_VERSION", len(eligible))
    statuses = {(row["review"]["identity_status"], row["review"]["protocol_status"]) for row in eligible}
    if any("failed" in status for status in statuses):
        return failure("MEASUREMENT_REVIEW_FAILED", len(eligible))
    if any(status != ("passed", "passed") for status in statuses):
        return failure("MEASUREMENT_REVIEW_REQUIRED", len(eligible))
    numeric = [row for row in eligible if row["value"] is not None]
    by_exam_day = {}
    for row in numeric:
        previous = by_exam_day.get(row["exam_date"])
        if previous and previous["value"] != row["value"]:
            return failure("CONFLICTING_SAME_DAY_MEASUREMENT", len(eligible))
        if previous is None or row["measurement_id"] < previous["measurement_id"]:
            by_exam_day[row["exam_date"]] = row
    series = sorted(by_exam_day.values(), key=lambda row: (row["exam_date"], row["measurement_id"]))
    if len(series) < minimum_observations:
        return failure("INSUFFICIENT_NUMERIC_OBSERVATIONS", len(series))
    first, last = series[0], series[-1]
    observation_days = (date.fromisoformat(last["exam_date"]) - date.fromisoformat(first["exam_date"])).days
    if observation_days <= 0:
        return failure("NO_POSITIVE_OBSERVATION_INTERVAL", len(series))
    signed_change = last["value"] - first["value"]
    return {
        "status": "computable",
        "value": signed_change / observation_days,
        "reason": "",
        "eligible_observation_count": len(series),
        "signed_change_mm": signed_change,
        "observation_days": observation_days,
        "first_measurement_id": first["measurement_id"],
        "last_measurement_id": last["measurement_id"],
        "first_exam_date": first["exam_date"],
        "last_exam_date": last["exam_date"],
    }

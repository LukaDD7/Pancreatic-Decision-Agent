"""PyArrow contracts and deterministic identifiers for the Stage 7 day view."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import pyarrow as pa


RULE_VERSION = "stage7_day_timeline_canary_v1"
DAY_NAMESPACE = uuid.UUID("2d77b451-a55f-5dfb-a0df-7e28516b4d2e")
EVENT_NAMESPACE = uuid.UUID("7f7e15cd-5a5e-5dd3-ae1c-bd16cf52c2c1")
ORDER_NAMESPACE = uuid.UUID("c7ff9c11-7a6f-5ff0-9ce1-8d8db16f84db")


def stable_uuid(namespace: uuid.UUID, *values: Any) -> str:
    material = "|".join("" if value is None else str(value) for value in values)
    return str(uuid.uuid5(namespace, material))


def patient_uid_hash(patient_uid: str) -> str:
    return hashlib.sha256(patient_uid.encode("utf-8")).hexdigest()


def day_id(patient_uid: str, event_date: Any) -> str:
    return stable_uuid(DAY_NAMESPACE, patient_uid, event_date)


def event_id(event_category: str, source_record_key: str, event_type: str) -> str:
    return stable_uuid(EVENT_NAMESPACE, event_category, source_record_key, event_type)


def lab_order_id(patient_uid: str, order_no: Any, specimen: Any, sample_time: Any, report_time: Any) -> str:
    return stable_uuid(ORDER_NAMESPACE, patient_uid, order_no, specimen, sample_time, report_time)


def _canonical_type(data_type: pa.DataType) -> object:
    if pa.types.is_list(data_type):
        return ["list", _canonical_type(data_type.value_type)]
    if pa.types.is_large_list(data_type):
        return ["large_list", _canonical_type(data_type.value_type)]
    if pa.types.is_fixed_size_list(data_type):
        return ["fixed_size_list", data_type.list_size, _canonical_type(data_type.value_type)]
    if pa.types.is_struct(data_type):
        return ["struct", [_canonical_field(field) for field in data_type]]
    return str(data_type)


def _canonical_field(field: pa.Field) -> list[object]:
    return [field.name, field.nullable, _canonical_type(field.type)]


def schema_hash(schema: pa.Schema) -> str:
    """Hash the logical schema, ignoring Parquet list child-name rewrites."""
    payload = [_canonical_field(field) for field in schema]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


_TS = pa.timestamp("us")
_DATE = pa.date32()
_STR = pa.string()
_LSTR = pa.large_string()
_FLAGS = pa.list_(_STR)


TIMELINE_EVENT_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("event_date", _DATE),
    pa.field("event_category", _STR),
    pa.field("event_type", _STR),
    pa.field("sort_time", _TS),
    pa.field("clinical_time", _TS),
    pa.field("available_time", _TS),
    pa.field("time_precision", _STR),
    pa.field("fallback_rule", _STR),
    pa.field("source_system", _STR),
    pa.field("source_record_key", _STR),
    pa.field("event_label", _LSTR),
    pa.field("narrative_eligible", pa.bool_()),
    pa.field("anchor_eligible", pa.bool_()),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


EVENT_TIME_EVIDENCE_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("source_record_key", _STR),
    pa.field("time_field", _STR),
    pa.field("raw_time_value", _LSTR),
    pa.field("parsed_time", _TS),
    pa.field("time_role", _STR),
    pa.field("is_selected_for_sort", pa.bool_()),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


EVENT_SOURCE_MAP_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("source_system", _STR),
    pa.field("source_record_key", _STR),
    pa.field("source_file", _LSTR),
    pa.field("source_row", pa.int64()),
    pa.field("source_record_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("encounter_uid", _STR),
    pa.field("source_disposition", _STR),
    pa.field("event_eligible", pa.bool_()),
    pa.field("rule_version", _STR),
])


ENCOUNTER_INTERVAL_SCHEMA = pa.schema([
    pa.field("encounter_uid", _STR),
    pa.field("patient_uid", _STR),
    pa.field("visit_id_normalized", _STR),
    pa.field("admission_time", _TS),
    pa.field("discharge_time", _TS),
    pa.field("admission_evidence_json", _LSTR),
    pa.field("discharge_evidence_json", _LSTR),
    pa.field("encounter_interval_status", _STR),
    pa.field("encounter_quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


ENCOUNTER_LINK_CANDIDATE_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("encounter_uid", _STR),
    pa.field("candidate_status", _STR),
    pa.field("candidate_reason", _STR),
    pa.field("event_date", _DATE),
    pa.field("source_record_key", _STR),
    pa.field("rule_version", _STR),
])


TIMELINE_QUALITY_FLAGS_SCHEMA = pa.schema([
    pa.field("patient_uid", _STR),
    pa.field("event_date", _DATE),
    pa.field("quality_flag", _STR),
    pa.field("flag_count", pa.int64()),
    pa.field("evidence_json", _LSTR),
    pa.field("rule_version", _STR),
])


PATIENT_DAY_SCHEMA = pa.schema([
    pa.field("day_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("event_date", _DATE),
    pa.field("encounter_count", pa.int64()),
    pa.field("lab_order_count", pa.int64()),
    pa.field("lab_item_count", pa.int64()),
    pa.field("document_count", pa.int64()),
    pa.field("imaging_count", pa.int64()),
    pa.field("pathology_count", pa.int64()),
    pa.field("admission_flag", pa.bool_()),
    pa.field("discharge_flag", pa.bool_()),
    pa.field("procedure_candidate_count", pa.int64()),
    pa.field("narrative_eligible", pa.bool_()),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


DAY_EVENT_RELATION_SCHEMA = pa.schema([
    pa.field("day_id", _STR),
    pa.field("event_id", _STR),
    pa.field("event_category", _STR),
    pa.field("relationship_type", _STR),
    pa.field("source_record_key", _STR),
])


LAB_DAY_SUMMARY_SCHEMA = pa.schema([
    pa.field("day_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("event_date", _DATE),
    pa.field("encounter_uid", _STR),
    pa.field("lab_order_count", pa.int64()),
    pa.field("lab_item_count", pa.int64()),
    pa.field("specimen_type_count", pa.int64()),
    pa.field("earliest_sample_time", _TS),
    pa.field("latest_sample_time", _TS),
    pa.field("earliest_report_time", _TS),
    pa.field("latest_report_time", _TS),
    pa.field("same_item_multiple_test_flag", pa.bool_()),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


LAB_ORDER_SCHEMA = pa.schema([
    pa.field("lab_order_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("encounter_uid", _STR),
    pa.field("order_no", _LSTR),
    pa.field("specimen", _LSTR),
    pa.field("sample_time", _TS),
    pa.field("report_time", _TS),
    pa.field("event_date", _DATE),
    pa.field("item_count", pa.int64()),
    pa.field("distinct_item_count", pa.int64()),
    pa.field("same_item_multiple_test_flag", pa.bool_()),
    pa.field("time_fallback_rule", _STR),
    pa.field("source_record_count", pa.int64()),
    pa.field("source_record_key_first", _STR),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


LAB_RESULT_DETAIL_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("lab_order_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("encounter_uid", _STR),
    pa.field("source_record_key", _STR),
    pa.field("item_name", _LSTR),
    pa.field("specimen", _LSTR),
    pa.field("result_raw", _LSTR),
    pa.field("result_type", _STR),
    pa.field("result_operator", _STR),
    pa.field("result_numeric_value", pa.float64()),
    pa.field("result_qualitative_value", _LSTR),
    pa.field("result_text_value", _LSTR),
    pa.field("result_parse_status", _STR),
    pa.field("unit_raw", _LSTR),
    pa.field("unit_normalized", _LSTR),
    pa.field("reference_raw", _LSTR),
    pa.field("reference_type", _STR),
    pa.field("reference_rule_json", _LSTR),
    pa.field("sample_time", _TS),
    pa.field("report_time", _TS),
    pa.field("event_time_used", _TS),
    pa.field("available_time", _TS),
    pa.field("event_date", _DATE),
    pa.field("time_fallback_rule", _STR),
    pa.field("source_abnormal_flag", _LSTR),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


DOCUMENT_DAY_DETAIL_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("encounter_uid", _STR),
    pa.field("source_record_key", _STR),
    pa.field("source_file", _LSTR),
    pa.field("source_row", pa.int64()),
    pa.field("source_record_id", _STR),
    pa.field("document_type", _LSTR),
    pa.field("create_time", _TS),
    pa.field("admission_time", _TS),
    pa.field("discharge_time", _TS),
    pa.field("event_time_used", _TS),
    pa.field("event_date", _DATE),
    pa.field("content_length", pa.int64()),
    pa.field("content_sha256", _STR),
    pa.field("parser_status", _STR),
    pa.field("time_fallback_rule", _STR),
    pa.field("procedure_candidate", pa.bool_()),
    pa.field("anchor_eligible", pa.bool_()),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


IMAGING_EVENT_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("encounter_uid", _STR),
    pa.field("source_record_key", _STR),
    pa.field("source_file", _LSTR),
    pa.field("record_cluster_id", pa.int64()),
    pa.field("exam_date_raw", _LSTR),
    pa.field("exam_time_raw", _LSTR),
    pa.field("exam_time", _TS),
    pa.field("event_date", _DATE),
    pa.field("exam_method", _LSTR),
    pa.field("cluster_status", _STR),
    pa.field("fragment_count", pa.int64()),
    pa.field("parsed_column_counts_json", _LSTR),
    pa.field("time_fallback_rule", _STR),
    pa.field("narrative_eligible", pa.bool_()),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


PATHOLOGY_EVENT_SCHEMA = pa.schema([
    pa.field("event_id", _STR),
    pa.field("patient_uid", _STR),
    pa.field("encounter_uid", _STR),
    pa.field("pathology_record_uid", _STR),
    pa.field("source_record_key", _STR),
    pa.field("received_time", _TS),
    pa.field("specimen_time", _TS),
    pa.field("report_time", _TS),
    pa.field("event_time_used", _TS),
    pa.field("event_date", _DATE),
    pa.field("time_fallback_rule", _STR),
    pa.field("pathology_time_sequence_conflict", pa.bool_()),
    pa.field("event_eligible", pa.bool_()),
    pa.field("quality_flags", _FLAGS),
    pa.field("rule_version", _STR),
])


SCHEMAS = {
    "timeline_event": TIMELINE_EVENT_SCHEMA,
    "event_time_evidence": EVENT_TIME_EVIDENCE_SCHEMA,
    "event_source_map": EVENT_SOURCE_MAP_SCHEMA,
    "encounter_interval": ENCOUNTER_INTERVAL_SCHEMA,
    "encounter_link_candidate": ENCOUNTER_LINK_CANDIDATE_SCHEMA,
    "timeline_quality_flags": TIMELINE_QUALITY_FLAGS_SCHEMA,
    "patient_day": PATIENT_DAY_SCHEMA,
    "day_event_relation": DAY_EVENT_RELATION_SCHEMA,
    "lab_day_summary": LAB_DAY_SUMMARY_SCHEMA,
    "lab_order": LAB_ORDER_SCHEMA,
    "lab_result_detail": LAB_RESULT_DETAIL_SCHEMA,
    "document_day_detail": DOCUMENT_DAY_DETAIL_SCHEMA,
    "imaging_event": IMAGING_EVENT_SCHEMA,
    "pathology_event": PATHOLOGY_EVENT_SCHEMA,
}

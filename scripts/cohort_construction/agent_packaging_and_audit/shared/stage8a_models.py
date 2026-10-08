"""Stage 8A data contracts and deterministic identity helpers.

This module defines the report/event/fact contract and the de-duplication
rules used by the Stage 8A preparation run.  It deliberately does not perform
medical fact extraction.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import OrderedDict
from typing import Any, Iterable


CONTRACT_VERSION = "stage8a_contract_v1"
RULE_VERSION = "stage8a_rules_v1"

REPORT_NAMESPACE = uuid.UUID("f4f0e11a-90f4-5e26-a0dc-2c58a1e9b1f4")
EVENT_NAMESPACE = uuid.UUID("f12ca70a-12ae-5e0e-8b5d-9c3c2f3c2201")
FACT_NAMESPACE = uuid.UUID("4d1a2dc0-2eb9-5c37-a553-1ddf1d1ec8ce")
SAMPLE_NAMESPACE = uuid.UUID("f8a3b4aa-654e-5fcf-9b0f-15b9a41f70c4")


REPORT_FIELDS = [
    "report_uid",
    "patient_uid",
    "event_uid",
    "source_system",
    "source_record_key",
    "source_file",
    "source_sheet",
    "source_row",
    "source_record_id",
    "content_hash",
    "report_type",
    "source_count",
    "dedup_scope",
    "rule_version",
]

EVENT_FIELDS = [
    "event_uid",
    "patient_uid",
    "event_type",
    "event_date",
    "clinical_time",
    "available_time",
    "source_system",
    "source_record_key",
    "report_uid",
    "rule_version",
]

FACT_FIELDS = [
    "fact_uid",
    "report_uid",
    "event_uid",
    "patient_uid",
    "fact_key",
    "fact_text",
    "assertion_polarity",
    "certainty",
    "body_site",
    "normalized_term",
    "terminology_system",
    "terminology_code",
    "terminology_status",
    "source_span",
    "rule_version",
]


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def normalized_key(value: Any) -> str:
    value = text(value).strip()
    value = re.sub(r"\s+", " ", value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: Any) -> str:
    return hashlib.sha256(text(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_uuid(namespace: uuid.UUID, *parts: Any) -> str:
    material = "|".join(normalized_key(part) for part in parts)
    return str(uuid.uuid5(namespace, material))


def build_event_uid(patient_uid: Any, source_record_key: Any, event_type: Any, event_date: Any = "") -> str:
    return stable_uuid(EVENT_NAMESPACE, patient_uid, source_record_key, event_type, event_date)


def build_report_uid(
    patient_uid: Any,
    event_uid: Any,
    content_hash: Any,
    source_system: Any,
) -> str:
    # Patient and event are part of the scope.  Equal content is never merged
    # across patients or clinical events.
    return stable_uuid(REPORT_NAMESPACE, patient_uid, event_uid, content_hash, source_system)


def build_fact_uid(report_uid: Any, fact_key: Any, assertion_polarity: Any = "", certainty: Any = "") -> str:
    # The assertion fields remain in the identity so positive/negative or
    # uncertain statements are retained as distinct facts.
    return stable_uuid(FACT_NAMESPACE, report_uid, fact_key, assertion_polarity, certainty)


def build_sample_uid(sample_type: Any, source_record_key: Any) -> str:
    return stable_uuid(SAMPLE_NAMESPACE, sample_type, source_record_key)


def contract_definition() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "rule_version": RULE_VERSION,
        "medical_fact_extraction_started": False,
        "objects": {
            "report": {
                "grain": "原始报告实例",
                "fields": REPORT_FIELDS,
                "dedup_rule": "同一患者、同一事件、同一来源类型下按内容哈希合并；跨患者或跨事件不合并",
            },
            "event": {
                "grain": "检查、取材、手术等临床事件",
                "fields": EVENT_FIELDS,
                "id_rule": "patient_uid + source_record_key + event_type + event_date 的UUIDv5",
            },
            "fact": {
                "grain": "报告中的单条医学事实",
                "fields": FACT_FIELDS,
                "id_rule": "report_uid + fact_key + assertion_polarity + certainty 的UUIDv5",
                "status": "schema_only_no_batch_extraction",
            },
        },
        "provenance": {
            "required": [
                "source_file",
                "source_sheet",
                "source_row",
                "source_record_id",
                "source_record_key",
            ],
            "content_cache_rule": "模型缓存可按内容哈希复用，但报告实例仍按患者和事件分别建溯源关系",
        },
    }


def deduplicate_reports(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return report instances and all source mappings.

    Records are expected to contain patient_uid, event_uid, source_system,
    content_hash and source_record_key.  The implementation is intentionally
    independent of medical semantics.
    """

    groups: OrderedDict[tuple[str, str, str, str], dict[str, Any]] = OrderedDict()
    source_map: list[dict[str, Any]] = []
    for row in records:
        patient_uid = normalized_key(row.get("patient_uid"))
        event_uid = normalized_key(row.get("event_uid"))
        source_system = normalized_key(row.get("source_system"))
        content_hash = normalized_key(row.get("content_hash"))
        key = (patient_uid, event_uid, source_system, content_hash)
        report_uid = build_report_uid(patient_uid, event_uid, content_hash, source_system)
        if key not in groups:
            groups[key] = {
                "report_uid": report_uid,
                "patient_uid": patient_uid,
                "event_uid": event_uid,
                "source_system": source_system,
                "content_hash": content_hash,
                "report_type": normalized_key(row.get("report_type")),
                "source_count": 0,
                "dedup_scope": "patient_event_content",
                "rule_version": RULE_VERSION,
            }
        groups[key]["source_count"] += 1
        source_map.append(
            {
                "report_uid": report_uid,
                "patient_uid": patient_uid,
                "event_uid": event_uid,
                "source_system": source_system,
                "source_record_key": normalized_key(row.get("source_record_key")),
                "source_file": normalized_key(row.get("source_file")),
                "source_sheet": normalized_key(row.get("source_sheet")),
                "source_row": row.get("source_row"),
                "source_record_id": normalized_key(row.get("source_record_id")),
                "content_hash": content_hash,
                "rule_version": RULE_VERSION,
            }
        )
    return list(groups.values()), source_map


def deduplicate_facts(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-duplicate equal facts while retaining assertion conflicts."""

    seen: OrderedDict[tuple[str, str, str, str], dict[str, Any]] = OrderedDict()
    for row in records:
        report_uid = normalized_key(row.get("report_uid"))
        fact_key = normalized_key(row.get("fact_key"))
        polarity = normalized_key(row.get("assertion_polarity"))
        certainty = normalized_key(row.get("certainty"))
        key = (report_uid, fact_key, polarity, certainty)
        if key in seen:
            continue
        result = dict(row)
        result["fact_uid"] = build_fact_uid(report_uid, fact_key, polarity, certainty)
        result["rule_version"] = RULE_VERSION
        seen[key] = result
    return list(seen.values())

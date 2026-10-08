"""Stage 7 simplified day-level timeline Canary.

The implementation intentionally has an explicit data root and reads only the
already accepted Stage 5/6/Stage 7-0 artifacts.  It never imports the moved
Stage 6 module, never reads document正文, and never starts a formal timeline.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

try:
    import orjson
except ImportError:  # pragma: no cover - the bundled runtime provides orjson
    orjson = None

from data_pipeline.paths import data_root
from .stage7_models import (
    DOCUMENT_DAY_DETAIL_SCHEMA,
    ENCOUNTER_INTERVAL_SCHEMA,
    ENCOUNTER_LINK_CANDIDATE_SCHEMA,
    EVENT_SOURCE_MAP_SCHEMA,
    EVENT_TIME_EVIDENCE_SCHEMA,
    IMAGING_EVENT_SCHEMA,
    LAB_DAY_SUMMARY_SCHEMA,
    LAB_ORDER_SCHEMA,
    LAB_RESULT_DETAIL_SCHEMA,
    PATHOLOGY_EVENT_SCHEMA,
    PATIENT_DAY_SCHEMA,
    DAY_EVENT_RELATION_SCHEMA,
    TIMELINE_EVENT_SCHEMA,
    TIMELINE_QUALITY_FLAGS_SCHEMA,
    RULE_VERSION,
    day_id,
    event_id,
    lab_order_id,
    patient_uid_hash,
    schema_hash,
)


DATA_ROOT = data_root()
CODE_ROOT = DATA_ROOT / "code"
STAGE5_ROOT = DATA_ROOT / "pipeline_outputs_stage5_v1" / "restricted"
STAGE6_ROOT = DATA_ROOT / "pipeline_outputs_stage6_v2" / "restricted"
PATHOLOGY_ROOT = STAGE6_ROOT / "increment" / "pathology_v1" / "full"
IMAGING_ROOT = DATA_ROOT / "pipeline_outputs_v2" / "patient_l1"
FREEZE_MANIFEST = CODE_ROOT / "stage7_0_freeze_manifest_v1.json"
STAGE7_0_SUMMARY = DATA_ROOT / "pipeline_outputs_stage7_0_v1" / "stage7_0_acceptance_summary.json"
STAGE6_MANIFEST = CODE_ROOT / "stage6_alignment_manifest_v2.json"
STAGE6_STATE = STAGE6_ROOT / "state" / "stage6_full_state_v2.sqlite3"

OUTPUT_ROOT = DATA_ROOT / "pipeline_outputs_stage7_v1"
CANARY_ROOT = OUTPUT_ROOT / "canary_day_timeline"
RESTRICTED_ROOT = OUTPUT_ROOT / "restricted" / "canary_day_timeline"
AUDIT_ROOT = OUTPUT_ROOT / "audit"
STATE_PATH = OUTPUT_ROOT / "state" / "stage7_day_canary_state.sqlite3"
STAGING_ROOT = OUTPUT_ROOT / "staging"
PARTIAL_ARCHIVE = OUTPUT_ROOT / "partial_archive"
SELECTION_PATH = RESTRICTED_ROOT / "selection" / "patient_selection_v1.json"
SELECTION_AUDIT_PATH = AUDIT_ROOT / "patient_selection_report.json"
CANARY_REPORT_PATH = AUDIT_ROOT / "stage7_day_timeline_canary_report.json"
PUBLIC_REPORT_PATH = CANARY_ROOT / "stage7_day_timeline_canary_report.json"

SEED = 20260826
TASK_ID = "stage7_day_timeline_canary_0001"
MAX_SHARD_ROWS = 50_000

LINK_ROOTS = {
    "document": STAGE6_ROOT / "full" / "record_links" / "document",
    "lab_l1": STAGE6_ROOT / "full" / "record_links" / "lab_l1",
    "lab_quarantine": STAGE6_ROOT / "full" / "record_links" / "lab_quarantine",
    "imaging": STAGE6_ROOT / "full" / "record_links" / "imaging",
    "pathology": PATHOLOGY_ROOT / "record_links_pathology",
}

OUTPUT_SCHEMAS = {
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


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    with partial.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def stable_source_key(source_system: str, source_file: Any, source_record_id: Any) -> str:
    material = "|".join((source_system, _text(source_file), _text(source_record_id)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_datetime(value: Any) -> datetime | None:
    if value is None or _text(value) == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return None
        try:
            from datetime import timedelta
            return datetime(1899, 12, 30) + timedelta(days=float(value))
        except (OverflowError, ValueError):
            return None
    raw = _text(value).replace("年", "-").replace("月", "-").replace("日", "")
    raw = raw.replace("/", "-").replace(".", "-").replace("T", " ")
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _precision(value: datetime | None) -> str:
    if value is None:
        return "UNKNOWN"
    return "MINUTE" if value.hour or value.minute or value.second or value.microsecond else "DAY"


def _unique_sorted(values: Iterable[Any]) -> list[str]:
    return sorted({_text(value) for value in values if _text(value)})


def iter_parquet_rows(
    root: Path,
    columns: list[str],
    batch_size: int = 100_000,
    filter_expression: Any | None = None,
) -> Iterator[dict[str, Any]]:
    dataset = ds.dataset(root, format="parquet")
    scanner = dataset.scanner(columns=columns, batch_size=batch_size, filter=filter_expression, use_threads=False)
    for batch in scanner.to_batches():
        yield from batch.to_pylist()


def _json_loads(raw: bytes) -> Any:
    if orjson is not None:
        return orjson.loads(raw)
    return json.loads(raw.decode("utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_input_baseline() -> dict[str, Any]:
    freeze = load_json(FREEZE_MANIFEST)
    if freeze.get("manifest_version") != "stage7_0_freeze_manifest_v1":
        raise RuntimeError("invalid_stage7_0_freeze_manifest")
    if not STAGE7_0_SUMMARY.is_file() or not load_json(STAGE7_0_SUMMARY)["validation"]["validation_passed"]:
        raise RuntimeError("stage7_0_acceptance_not_passed")
    if not STAGE6_MANIFEST.is_file():
        raise FileNotFoundError(STAGE6_MANIFEST)
    stage6_manifest = load_json(STAGE6_MANIFEST)
    if stage6_manifest.get("manifest_version") != "stage6_alignment_manifest_v2":
        raise RuntimeError("invalid_stage6_manifest")
    sidecars = [Path(str(STAGE6_STATE) + suffix) for suffix in ("-journal", "-wal", "-shm") if Path(str(STAGE6_STATE) + suffix).exists()]
    if sidecars:
        raise RuntimeError("stage6_state_hot_sidecar")
    uri = f"file:{STAGE6_STATE.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        status_rows = db.execute("SELECT status, COUNT(*) FROM task_state GROUP BY status").fetchall()
        task_count = db.execute("SELECT COUNT(*) FROM task_state").fetchone()[0]
    status_counts = {str(status): int(count) for status, count in status_rows}
    if task_count != 520 or status_counts.get("SUCCEEDED", 0) != 520:
        raise RuntimeError("stage6_tasks_not_520_succeeded")
    imaging_tasks = [task for task in stage6_manifest.get("tasks", []) if task.get("source_system") == "imaging"]
    if len(imaging_tasks) != 251:
        raise RuntimeError("imaging_cluster_task_count_not_251")
    return {
        "freeze_manifest_sha256": sha256_file(FREEZE_MANIFEST),
        "stage6_manifest_sha256": sha256_file(STAGE6_MANIFEST),
        "stage6_task_count": task_count,
        "stage6_status_counts": status_counts,
        "imaging_tasks": sorted(imaging_tasks, key=lambda item: item["task_id"]),
    }


def _scan_link_rows(root: Path, columns: list[str]) -> Iterator[dict[str, Any]]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    yield from iter_parquet_rows(root, columns)


def candidate_patient_pool() -> set[str]:
    pool: set[str] = set()
    document_columns = ["patient_uid", "disposition_class"]
    for row in _scan_link_rows(LINK_ROOTS["document"], document_columns):
        patient = _text(row.get("patient_uid"))
        if patient and _text(row.get("disposition_class")) == "hard":
            pool.add(patient)
    for row in _scan_link_rows(LINK_ROOTS["pathology"], document_columns):
        patient = _text(row.get("patient_uid"))
        if patient and _text(row.get("disposition_class")) == "hard":
            pool.add(patient)
    if len(pool) < 100:
        raise RuntimeError(f"candidate_patient_pool_too_small:{len(pool)}")
    return pool


def patient_activity_stats(candidates: set[str]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {
        patient: {
            "total_events": 0,
            "eligible_events": 0,
            "ineligible_events": 0,
            "encounters": set(),
            "source_counts": Counter(),
            "pathology_conflict_count": 0,
        }
        for patient in candidates
    }
    link_columns = ["patient_uid", "encounter_uid", "disposition_class", "event_eligible"]
    for source_system in ("document", "lab_l1", "imaging", "pathology"):
        for row in _scan_link_rows(LINK_ROOTS[source_system], link_columns):
            patient = _text(row.get("patient_uid"))
            if patient not in candidates:
                continue
            disposition = _text(row.get("disposition_class"))
            if source_system == "pathology" and disposition == "conflict":
                stats[patient]["pathology_conflict_count"] += 1
            if disposition not in {"hard", "conflict"}:
                continue
            stats[patient]["total_events"] += int(disposition == "hard")
            stats[patient]["source_counts"][source_system] += int(disposition == "hard")
            encounter = _text(row.get("encounter_uid"))
            if encounter:
                stats[patient]["encounters"].add(encounter)
            if bool(row.get("event_eligible")) and disposition == "hard":
                stats[patient]["eligible_events"] += 1
            elif disposition == "hard":
                stats[patient]["ineligible_events"] += 1
    return stats


def _ranked(items: Iterable[str], stats: dict[str, dict[str, Any]], label: str) -> list[str]:
    return sorted(
        items,
        key=lambda patient: hashlib.sha256(f"{SEED}|{label}|{patient}".encode("utf-8")).hexdigest(),
    )


def _selection_payload(strata: dict[str, list[str]], stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    patients = []
    for stratum, values in strata.items():
        for patient in values:
            patients.append({
                "patient_uid": patient,
                "stratum": stratum,
                "activity_event_count": int(stats[patient]["total_events"]),
                "encounter_count": len(stats[patient]["encounters"]),
            })
    patients.sort(key=lambda item: (item["stratum"], item["patient_uid"]))
    return {"selection_version": "stage7_patient_selection_v1", "seed": SEED, "patients": patients}


def _selection_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def select_or_load_patients(candidates: set[str], stats: dict[str, dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    if SELECTION_PATH.is_file():
        selection = load_json(SELECTION_PATH)
        payload = {key: selection[key] for key in ("selection_version", "seed", "patients")}
        if selection.get("selection_sha256") != _selection_hash(payload):
            raise RuntimeError("patient_selection_hash_invalid")
        if len(payload["patients"]) != 100:
            raise RuntimeError("patient_selection_count_not_100")
        return [item["patient_uid"] for item in payload["patients"]], selection

    boundary_rank = sorted(
        candidates,
        key=lambda patient: (
            -(
                stats[patient]["pathology_conflict_count"] * 100
                + stats[patient]["ineligible_events"] * 10
                + int(len(stats[patient]["encounters"]) > 1) * 5
            ),
            hashlib.sha256(f"{SEED}|boundary|{patient}".encode("utf-8")).hexdigest(),
        ),
    )
    boundary = boundary_rank[:10]
    remaining = set(candidates) - set(boundary)
    simple = sorted(
        remaining,
        key=lambda patient: (
            stats[patient]["total_events"],
            len(stats[patient]["encounters"]),
            hashlib.sha256(f"{SEED}|simple|{patient}".encode("utf-8")).hexdigest(),
        ),
    )[:40]
    remaining -= set(simple)
    complex_patients = sorted(
        remaining,
        key=lambda patient: (
            -stats[patient]["total_events"],
            -len(stats[patient]["encounters"]),
            hashlib.sha256(f"{SEED}|complex|{patient}".encode("utf-8")).hexdigest(),
        ),
    )[:20]
    remaining -= set(complex_patients)
    medium = _ranked(remaining, stats, "medium")[:30]
    strata = {
        "simple": simple,
        "medium": medium,
        "complex": complex_patients,
        "boundary": boundary,
    }
    if sum(len(values) for values in strata.values()) != 100:
        raise RuntimeError("patient_selection_strata_not_100")
    payload = _selection_payload(strata, stats)
    selection = {**payload, "selection_sha256": _selection_hash(payload)}
    atomic_json(SELECTION_PATH, selection)
    audit = {
        "selection_version": selection["selection_version"],
        "seed": SEED,
        "patient_count": 100,
        "candidate_pool_count": len(candidates),
        "stratum_counts": {key: len(value) for key, value in strata.items()},
        "selection_sha256": selection["selection_sha256"],
        "patient_uid_hashes": [
            {"stratum": item["stratum"], "patient_uid_hash": patient_uid_hash(item["patient_uid"])}
            for item in payload["patients"]
        ],
        "pii_in_report": False,
    }
    atomic_json(SELECTION_AUDIT_PATH, audit)
    return [item["patient_uid"] for item in payload["patients"]], selection


def selected_link_maps(selected: set[str], include_quarantine: bool = True) -> dict[str, dict[str, dict[str, Any]]]:
    common_columns = [
        "source_record_key", "source_file", "source_row", "source_record_id",
        "patient_uid", "encounter_uid", "disposition_class", "event_eligible",
        "timeline_candidate_eligible",
    ]
    result: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in LINK_ROOTS}
    for source_system, root in LINK_ROOTS.items():
        if source_system == "lab_quarantine" and not include_quarantine:
            continue
        if source_system == "pathology":
            columns = common_columns + ["pathology_record_uid"]
        else:
            columns = common_columns + ["source_status", "quarantine_reason"]
        for row in iter_parquet_rows(
            root,
            columns,
            filter_expression=pc.field("patient_uid").isin(sorted(selected)),
        ):
            patient = _text(row.get("patient_uid"))
            if patient not in selected:
                continue
            disposition = _text(row.get("disposition_class"))
            if disposition not in {"hard", "conflict"}:
                continue
            key = _text(row.get("source_record_key"))
            if not key:
                raise RuntimeError(f"source_record_key_missing:{source_system}")
            prior = result[source_system].get(key)
            compact = {
                "source_record_key": key,
                "source_file": _text(row.get("source_file")),
                "source_row": row.get("source_row"),
                "source_record_id": _text(row.get("source_record_id")),
                "pathology_record_uid": _text(row.get("pathology_record_uid")) or None,
                "patient_uid": patient,
                "encounter_uid": _text(row.get("encounter_uid")) or None,
                "disposition_class": disposition,
                "event_eligible": bool(row.get("event_eligible")),
                "timeline_candidate_eligible": bool(row.get("timeline_candidate_eligible")),
                "source_status": _text(row.get("source_status")),
                "quarantine_reason": _text(row.get("quarantine_reason")),
            }
            if prior is not None and prior != compact:
                raise RuntimeError(f"duplicate_source_key_conflict:{source_system}")
            result[source_system][key] = compact
    return result


def _read_encounter_intervals(selected: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    uri = f"file:{STAGE6_STATE.as_posix()}?mode=ro"
    registry: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(uri, uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        for encounter_key, encounter_uid, patient_uid, visit_id in db.execute(
            "SELECT encounter_key, encounter_uid, patient_uid, visit_id_normalized FROM encounter_registry"
        ):
            if patient_uid in selected:
                registry[encounter_key] = {
                    "encounter_uid": encounter_uid,
                    "patient_uid": patient_uid,
                    "visit_id_normalized": visit_id,
                    "admission": [],
                    "discharge": [],
                }
        for encounter_key, kind, value in db.execute(
            "SELECT encounter_key, kind, value FROM encounter_window"
        ):
            item = registry.get(encounter_key)
            if item is not None and kind in {"admission", "discharge"}:
                item[kind].append(_text(value))
    intervals: list[dict[str, Any]] = []
    boundary_events: list[dict[str, Any]] = []
    for item in registry.values():
        admissions = _unique_sorted(item["admission"])
        discharges = _unique_sorted(item["discharge"])
        admission_times = [parse_datetime(value) for value in admissions]
        discharge_times = [parse_datetime(value) for value in discharges]
        admission_times = [value for value in admission_times if value is not None]
        discharge_times = [value for value in discharge_times if value is not None]
        admission_time = min(admission_times) if admission_times else None
        discharge_time = max(discharge_times) if discharge_times else None
        flags: list[str] = []
        if len(admissions) > 1:
            flags.append("MULTIPLE_ADMISSION_EVIDENCE")
        if len(discharges) > 1:
            flags.append("MULTIPLE_DISCHARGE_EVIDENCE")
        if admission_time and discharge_time and discharge_time < admission_time:
            flags.append("INVALID_INTERVAL_ORDER")
        if admission_time and discharge_time:
            status = "COMPLETE_INTERVAL"
        elif admission_time:
            status = "OPEN_INTERVAL"
        elif discharge_time:
            status = "NO_ADMISSION_EVIDENCE"
        else:
            status = "NO_INTERVAL_EVIDENCE"
        intervals.append({
            "encounter_uid": item["encounter_uid"],
            "patient_uid": item["patient_uid"],
            "visit_id_normalized": item["visit_id_normalized"],
            "admission_time": admission_time,
            "discharge_time": discharge_time,
            "admission_evidence_json": json.dumps(admissions, ensure_ascii=False),
            "discharge_evidence_json": json.dumps(discharges, ensure_ascii=False),
            "encounter_interval_status": status,
            "encounter_quality_flags": sorted(flags),
            "_admission_raw": admissions,
            "_discharge_raw": discharges,
        })
        for kind, values in (("ADMISSION", admissions), ("DISCHARGE", discharges)):
            for raw in values:
                parsed = parse_datetime(raw)
                if parsed is None:
                    continue
                source_key = hashlib.sha256(
                    f"stage6|encounter_window|{item['encounter_uid']}|{kind}|{raw}".encode("utf-8")
                ).hexdigest()
                boundary_events.append({
                    "patient_uid": item["patient_uid"],
                    "encounter_uid": item["encounter_uid"],
                    "source_record_key": source_key,
                    "source_record_id": item["encounter_uid"],
                    "source_row": None,
                    "source_file": str(STAGE6_STATE),
                    "event_type": kind,
                    "event_time": parsed,
                    "raw_time": raw,
                    "quality_flags": sorted(flags),
                })
    intervals.sort(key=lambda row: (row["patient_uid"], row["encounter_uid"]))
    return intervals, boundary_events


def _base_event(
    patient_uid: str,
    category: str,
    event_type: str,
    source_key: str,
    source_system: str,
    event_time: datetime | None,
    available_time: datetime | None,
    label: str,
    narrative_eligible: bool,
    anchor_eligible: bool,
    flags: Iterable[str],
) -> dict[str, Any]:
    sort_time = event_time or available_time
    return {
        "event_id": event_id(category, source_key, event_type),
        "patient_uid": patient_uid,
        "event_date": sort_time.date() if sort_time else None,
        "event_category": category,
        "event_type": event_type,
        "sort_time": sort_time,
        "clinical_time": event_time,
        "available_time": available_time,
        "time_precision": _precision(sort_time),
        "fallback_rule": "NONE" if event_time else ("AVAILABLE_TIME_FALLBACK" if available_time else "NO_TIME"),
        "source_system": source_system,
        "source_record_key": source_key,
        "event_label": label,
        "narrative_eligible": narrative_eligible,
        "anchor_eligible": anchor_eligible,
        "quality_flags": sorted(set(flags)),
        "rule_version": RULE_VERSION,
    }


def _time_evidence(event: dict[str, Any], field: str, raw: Any, parsed: datetime | None, role: str, selected: bool) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "source_record_key": event["source_record_key"],
        "time_field": field,
        "raw_time_value": _text(raw),
        "parsed_time": parsed,
        "time_role": role,
        "is_selected_for_sort": selected,
        "quality_flags": event["quality_flags"],
        "rule_version": RULE_VERSION,
    }


def _source_map(event: dict[str, Any], link: dict[str, Any], source_system: str) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "source_system": source_system,
        "source_record_key": event["source_record_key"],
        "source_file": link.get("source_file", ""),
        "source_row": link.get("source_row"),
        "source_record_id": link.get("source_record_id", ""),
        "patient_uid": event["patient_uid"],
        "encounter_uid": link.get("encounter_uid"),
        "source_disposition": link.get("disposition_class", "hard"),
        "event_eligible": bool(link.get("event_eligible", True)),
        "rule_version": RULE_VERSION,
    }


def collect_timeline_data(selected: set[str], links: dict[str, dict[str, dict[str, Any]]], baseline: dict[str, Any]) -> dict[str, list[dict[str, Any]] | dict[str, Any]]:
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in OUTPUT_SCHEMAS}
    event_records: list[dict[str, Any]] = []
    lab_detail_rows: list[dict[str, Any]] = []
    lab_orders: dict[tuple[Any, ...], dict[str, Any]] = {}
    seen: dict[str, set[str]] = {name: set() for name in ("document", "lab_l1", "imaging", "pathology")}
    counters: Counter[str] = Counter()
    boundary_flags: Counter[tuple[str, date, str]] = Counter()

    intervals, boundary_events = _read_encounter_intervals(selected)
    for interval in intervals:
        rows["encounter_interval"].append({key: value for key, value in interval.items() if not key.startswith("_")})
    for item in boundary_events:
        event = _base_event(
            item["patient_uid"], "encounter", item["event_type"], item["source_record_key"],
            "stage6", item["event_time"], item["event_time"], item["event_type"], True, False, item["quality_flags"],
        )
        rows["timeline_event"].append(event)
        rows["event_time_evidence"].append(_time_evidence(event, item["event_type"], item["raw_time"], item["event_time"], "BOUNDARY", True))
        rows["event_source_map"].append(_source_map(event, item, "stage6"))
        event["_encounter_uid"] = item["encounter_uid"]
        event_records.append(event)
        counters[f"encounter_{item['event_type'].lower()}_event_count"] += 1

    # Documents: only structured metadata is read; 文书内容 is intentionally not selected.
    document_columns = [
        "PATIENT_ID", "VISIT_ID", "ADMISSION_DATE_TIME", "DISCHARGE_DATE_TIME",
        "文书名称", "CREATE_DATE_TIME", "admission_time", "discharge_time", "create_time",
        "content_length", "content_sha256", "source_file", "source_row", "source_record_id", "parser_status",
    ]
    selected_document_ids = sorted({row["source_record_id"] for row in links["document"].values() if row.get("source_record_id")})
    for row in iter_parquet_rows(
        STAGE5_ROOT / "document_l1",
        document_columns,
        filter_expression=pc.field("source_record_id").isin(pa.array(selected_document_ids, type=pa.string())),
    ):
        key = stable_source_key("document", row.get("source_file"), row.get("source_record_id"))
        link = links["document"].get(key)
        if link is None or link.get("disposition_class") != "hard" or not link.get("event_eligible"):
            continue
        seen["document"].add(key)
        create_raw = row.get("create_time") or row.get("CREATE_DATE_TIME")
        admission_raw = row.get("admission_time") or row.get("ADMISSION_DATE_TIME")
        discharge_raw = row.get("discharge_time") or row.get("DISCHARGE_DATE_TIME")
        create_time = parse_datetime(create_raw)
        admission_time = parse_datetime(admission_raw)
        discharge_time = parse_datetime(discharge_raw)
        flags: list[str] = []
        event_time = create_time
        fallback = "NONE"
        if event_time is None and admission_time is not None:
            event_time = admission_time
            fallback = "ADMISSION_TIME_FALLBACK"
            flags.append(fallback)
        if event_time is None:
            flags.append("DOCUMENT_TIME_MISSING")
            fallback = "NO_TIME"
        event = _base_event(
            link["patient_uid"], "document", "DOCUMENT", key, "document", event_time, create_time,
            _text(row.get("文书名称")), True, False, flags,
        )
        event["fallback_rule"] = fallback
        rows["timeline_event"].append(event)
        rows["event_source_map"].append(_source_map(event, link, "document"))
        rows["event_time_evidence"].extend([
            _time_evidence(event, "CREATE_DATE_TIME", create_raw, create_time, "CLINICAL", create_time is not None),
            _time_evidence(event, "ADMISSION_DATE_TIME", admission_raw, admission_time, "INTERVAL", False),
            _time_evidence(event, "DISCHARGE_DATE_TIME", discharge_raw, discharge_time, "INTERVAL", False),
        ])
        rows["document_day_detail"].append({
            "event_id": event["event_id"], "patient_uid": link["patient_uid"], "encounter_uid": link.get("encounter_uid"),
            "source_record_key": key, "source_file": _text(row.get("source_file")), "source_row": row.get("source_row"),
            "source_record_id": _text(row.get("source_record_id")), "document_type": _text(row.get("文书名称")),
            "create_time": create_time, "admission_time": admission_time, "discharge_time": discharge_time,
            "event_time_used": event_time, "event_date": event["event_date"], "content_length": row.get("content_length"),
            "content_sha256": _text(row.get("content_sha256")), "parser_status": _text(row.get("parser_status")),
            "time_fallback_rule": fallback, "procedure_candidate": False, "anchor_eligible": False,
            "quality_flags": event["quality_flags"], "rule_version": RULE_VERSION,
        })
        event["_encounter_uid"] = link.get("encounter_uid")
        event_records.append(event)
        counters["document_event_count"] += 1
        if event["event_date"] is None:
            counters["document_untimed_count"] += 1

    # Labs: one detail row per item, orders are keyed by order/sample/report evidence.
    lab_columns = [
        "检验项目名称", "检验结果值", "检验结果单位", "结果正常标志", "送检时间", "报告时间",
        "检验参考值", "检验单号", "标本", "source_workbook", "source_sheet", "source_row", "source_record_id",
        "result_raw", "result_type", "result_operator", "result_numeric_value", "result_qualitative_value",
        "result_text_value", "result_parse_status", "unit_raw", "unit_normalized", "reference_raw", "reference_type",
        "reference_rule_json", "event_time", "available_time", "time_parse_status",
    ]
    selected_lab_ids = sorted({row["source_record_id"] for row in links["lab_l1"].values() if row.get("source_record_id")})
    for row in iter_parquet_rows(
        STAGE5_ROOT / "lab_l1",
        lab_columns,
        filter_expression=pc.field("source_record_id").isin(pa.array(selected_lab_ids, type=pa.string())),
    ):
        source_file = f"{_text(row.get('source_workbook'))}|{_text(row.get('source_sheet'))}"
        key = stable_source_key("lab_l1", source_file, row.get("source_record_id"))
        link = links["lab_l1"].get(key)
        if link is None or link.get("disposition_class") != "hard" or not link.get("event_eligible"):
            continue
        seen["lab_l1"].add(key)
        sample_raw = row.get("event_time") or row.get("送检时间")
        report_raw = row.get("available_time") or row.get("报告时间")
        sample_time = parse_datetime(sample_raw)
        report_time = parse_datetime(report_raw)
        flags: list[str] = []
        fallback = "NONE"
        event_time = sample_time
        if sample_time is None and report_time is not None:
            event_time = report_time
            fallback = "REPORT_TIME_FALLBACK"
            flags.append(fallback)
            counters["lab_missing_sample_time_count"] += 1
        if sample_time is None and report_time is None:
            fallback = "NO_TIME"
            flags.append("LAB_TIME_MISSING")
            counters["lab_untimed_count"] += 1
        if sample_time and report_time and sample_time.date() != report_time.date():
            flags.append("REPORT_CROSS_DAY")
            counters["lab_report_cross_day_count"] += 1
        if _text(row.get("time_parse_status")).startswith("INVALID"):
            flags.append("INVALID_TIME_STATUS")
        event = _base_event(
            link["patient_uid"], "lab", "LAB_RESULT", key, "lab_l1", sample_time, report_time,
            _text(row.get("检验项目名称")), False, False, flags,
        )
        event["fallback_rule"] = fallback
        rows["timeline_event"].append(event)
        rows["event_source_map"].append(_source_map(event, link, "lab_l1"))
        rows["event_time_evidence"].extend([
            _time_evidence(event, "送检时间", sample_raw, sample_time, "CLINICAL", sample_time is not None),
            _time_evidence(event, "报告时间", report_raw, report_time, "AVAILABLE", sample_time is None and report_time is not None),
        ])
        order_key = (
            link["patient_uid"], _text(row.get("检验单号")), _text(row.get("标本")),
            sample_time, report_time, link.get("encounter_uid"),
        )
        if order_key not in lab_orders:
            lab_orders[order_key] = {
                "patient_uid": link["patient_uid"], "encounter_uid": link.get("encounter_uid"),
                "order_no": _text(row.get("检验单号")), "specimen": _text(row.get("标本")),
                "sample_time": sample_time, "report_time": report_time, "event_date": event["event_date"],
                "source_keys": [], "item_names": [], "flags": set(), "detail_event_ids": [],
            }
        order = lab_orders[order_key]
        order["source_keys"].append(key)
        order["item_names"].append(_text(row.get("检验项目名称")))
        order["flags"].update(flags)
        order_id = lab_order_id(order["patient_uid"], order["order_no"], order["specimen"], order["sample_time"], order["report_time"])
        order["detail_event_ids"].append(event["event_id"])
        detail = {
            "event_id": event["event_id"], "lab_order_id": order_id, "patient_uid": link["patient_uid"],
            "encounter_uid": link.get("encounter_uid"), "source_record_key": key,
            "item_name": _text(row.get("检验项目名称")), "specimen": _text(row.get("标本")),
            "result_raw": _text(row.get("result_raw") or row.get("检验结果值")),
            "result_type": _text(row.get("result_type")), "result_operator": _text(row.get("result_operator")),
            "result_numeric_value": row.get("result_numeric_value"),
            "result_qualitative_value": _text(row.get("result_qualitative_value")),
            "result_text_value": _text(row.get("result_text_value")), "result_parse_status": _text(row.get("result_parse_status")),
            "unit_raw": _text(row.get("unit_raw") or row.get("检验结果单位")), "unit_normalized": _text(row.get("unit_normalized")),
            "reference_raw": _text(row.get("reference_raw") or row.get("检验参考值")), "reference_type": _text(row.get("reference_type")),
            "reference_rule_json": _text(row.get("reference_rule_json")), "sample_time": sample_time,
            "report_time": report_time, "event_time_used": event_time, "available_time": report_time,
            "event_date": event["event_date"], "time_fallback_rule": fallback,
            "source_abnormal_flag": _text(row.get("结果正常标志")), "quality_flags": event["quality_flags"],
            "rule_version": RULE_VERSION,
        }
        lab_detail_rows.append(detail)
        event["_encounter_uid"] = link.get("encounter_uid")
        event_records.append(event)
        counters["lab_item_count"] += 1

    for order in lab_orders.values():
        order_id = lab_order_id(order["patient_uid"], order["order_no"], order["specimen"], order["sample_time"], order["report_time"])
        repeated = len(order["item_names"]) != len(set(order["item_names"]))
        if repeated:
            order["flags"].add("SAME_ITEM_MULTIPLE_TEST")
        rows["lab_order"].append({
            "lab_order_id": order_id, "patient_uid": order["patient_uid"], "encounter_uid": order["encounter_uid"],
            "order_no": order["order_no"], "specimen": order["specimen"], "sample_time": order["sample_time"],
            "report_time": order["report_time"], "event_date": order["event_date"], "item_count": len(order["item_names"]),
            "distinct_item_count": len(set(order["item_names"])), "same_item_multiple_test_flag": repeated,
            "time_fallback_rule": "REPORT_TIME_FALLBACK" if order["sample_time"] is None and order["report_time"] else "NONE",
            "source_record_count": len(order["source_keys"]), "source_record_key_first": order["source_keys"][0],
            "quality_flags": sorted(order["flags"]), "rule_version": RULE_VERSION,
        })
    rows["lab_result_detail"] = lab_detail_rows

    # Imaging clusters: only selected patient-linked cluster rows are retained.
    selected_imaging_files = {
        Path(row["source_file"]).name
        for row in links["imaging"].values()
        if row.get("source_file") and row.get("disposition_class") == "hard" and row.get("event_eligible")
    }
    for task in baseline["imaging_tasks"]:
        cluster_path = Path(task["source_file"])
        original_name = cluster_path.name.removesuffix(".record_clusters.jsonl")
        if original_name not in selected_imaging_files:
            continue
        if not cluster_path.is_file():
            raise FileNotFoundError(cluster_path)
        digest = hashlib.sha256()
        line_count = 0
        with cluster_path.open("rb") as stream:
            for raw_line in stream:
                digest.update(raw_line)
                line_count += 1
                try:
                    record = _json_loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise RuntimeError(f"imaging_cluster_parse_error:{cluster_path.name}:{line_count}") from exc
                key = stable_source_key("imaging", cluster_path.name, record.get("record_cluster_id"))
                link = links["imaging"].get(key)
                if link is None or not link.get("event_eligible") or link.get("disposition_class") != "hard":
                    continue
                seen["imaging"].add(key)
                fragments = record.get("fragments") or []
                tokens = record.get("repaired_tokens")
                if not isinstance(tokens, list) or len(tokens) < 8:
                    tokens = (fragments[0].get("raw_token_array") if fragments and isinstance(fragments[0], dict) else None) or []
                exam_date_raw = tokens[5] if len(tokens) > 5 else ""
                method = tokens[6] if len(tokens) > 6 else ""
                exam_time_raw = tokens[7] if len(tokens) > 7 else ""
                exam_time = parse_datetime(f"{_text(exam_date_raw)} {_text(exam_time_raw)}".strip()) or parse_datetime(exam_date_raw)
                flags: list[str] = []
                if exam_time is None:
                    flags.append("IMAGING_DATE_MISSING_OR_INVALID")
                event = _base_event(
                    link["patient_uid"], "imaging", "IMAGING_EXAM", key, "imaging", exam_time, exam_time,
                    _text(method), False, False, flags,
                )
                rows["timeline_event"].append(event)
                rows["event_source_map"].append(_source_map(event, link, "imaging"))
                rows["event_time_evidence"].append(_time_evidence(event, "检查日期+检查时间", f"{_text(exam_date_raw)} {_text(exam_time_raw)}", exam_time, "CLINICAL", exam_time is not None))
                rows["imaging_event"].append({
                    "event_id": event["event_id"], "patient_uid": link["patient_uid"], "encounter_uid": link.get("encounter_uid"),
                    "source_record_key": key, "source_file": _text(record.get("source_file")),
                    "record_cluster_id": record.get("record_cluster_id"), "exam_date_raw": _text(exam_date_raw),
                    "exam_time_raw": _text(exam_time_raw), "exam_time": exam_time, "event_date": event["event_date"],
                    "exam_method": _text(method), "cluster_status": _text(record.get("status")),
                    "fragment_count": record.get("fragment_count"), "parsed_column_counts_json": json.dumps(record.get("parsed_column_counts") or [], ensure_ascii=False),
                    "time_fallback_rule": event["fallback_rule"], "narrative_eligible": False,
                    "quality_flags": event["quality_flags"], "rule_version": RULE_VERSION,
                })
                event["_encounter_uid"] = link.get("encounter_uid")
                event_records.append(event)
                counters["imaging_event_count"] += 1
        if line_count != int(task["expected_row_count"]) or digest.hexdigest() != task["source_sha256"]:
            raise RuntimeError(f"imaging_input_changed:{cluster_path.name}")

    # Pathology unique reports: conflict/unmatched links are deliberately excluded.
    pathology_record_columns = [
        "pathology_record_uid", "canonical_source_record_key", "received_date_parsed", "specimen_date_parsed",
        "report_date_parsed", "received_date_parse_status", "specimen_date_parse_status", "report_date_parse_status",
        "pathology_time_sequence_conflict",
    ]
    selected_pathology_uids = {row.get("pathology_record_uid") for row in links["pathology"].values() if row.get("disposition_class") == "hard"}
    for row in iter_parquet_rows(
        PATHOLOGY_ROOT / "pathology_record",
        pathology_record_columns,
        filter_expression=pc.field("pathology_record_uid").isin(
            pa.array(sorted(selected_pathology_uids), type=pa.string())
        ),
    ):
        uid = _text(row.get("pathology_record_uid"))
        if uid not in selected_pathology_uids:
            continue
        key = _text(row.get("canonical_source_record_key"))
        link = links["pathology"].get(key)
        if link is None or link.get("disposition_class") != "hard" or not link.get("event_eligible"):
            continue
        seen["pathology"].add(key)
        received = parse_datetime(row.get("received_date_parsed"))
        specimen = parse_datetime(row.get("specimen_date_parsed"))
        report = parse_datetime(row.get("report_date_parsed"))
        flags: list[str] = []
        event_time = report
        fallback = "NONE"
        if event_time is None and specimen is not None:
            event_time = specimen
            fallback = "SPECIMEN_DATE_FALLBACK"
            flags.append(fallback)
            counters["pathology_report_missing_fallback_count"] += 1
        if bool(row.get("pathology_time_sequence_conflict")):
            flags.append("PATHOLOGY_TIME_SEQUENCE_CONFLICT")
        event = _base_event(
            link["patient_uid"], "pathology", "PATHOLOGY_REPORT", key, "pathology", event_time, report,
            "PATHOLOGY_REPORT", False, False, flags,
        )
        event["fallback_rule"] = fallback
        rows["timeline_event"].append(event)
        rows["event_source_map"].append(_source_map(event, link, "pathology"))
        rows["event_time_evidence"].extend([
            _time_evidence(event, "收到日期", row.get("received_date_parsed"), received, "RECEIVED", False),
            _time_evidence(event, "取材日期", row.get("specimen_date_parsed"), specimen, "CLINICAL", report is None and specimen is not None),
            _time_evidence(event, "报告日期", row.get("report_date_parsed"), report, "AVAILABLE", report is not None),
        ])
        rows["pathology_event"].append({
            "event_id": event["event_id"], "patient_uid": link["patient_uid"], "encounter_uid": link.get("encounter_uid"),
            "pathology_record_uid": uid, "source_record_key": key, "received_time": received,
            "specimen_time": specimen, "report_time": report, "event_time_used": event_time,
            "event_date": event["event_date"], "time_fallback_rule": fallback,
            "pathology_time_sequence_conflict": bool(row.get("pathology_time_sequence_conflict")),
            "event_eligible": True, "quality_flags": event["quality_flags"], "rule_version": RULE_VERSION,
        })
        event["_encounter_uid"] = link.get("encounter_uid")
        event_records.append(event)
        counters["pathology_event_count"] += 1

    # Source conservation for selected link rows, without reading unavailable quarantine正文.
    for source_system in ("document", "lab_l1", "imaging", "pathology"):
        expected = {key for key, row in links[source_system].items() if row.get("disposition_class") == "hard" and row.get("event_eligible")}
        missing = expected - seen[source_system]
        if missing:
            raise RuntimeError(f"selected_source_rows_not_materialized:{source_system}:{len(missing)}")
    counters["timeline_event_count"] = len(rows["timeline_event"])

    # Day model and relations.
    days: dict[tuple[str, date], dict[str, Any]] = {}
    for event in event_records:
        if event["event_date"] is None:
            continue
        key = (event["patient_uid"], event["event_date"])
        day = days.setdefault(key, {"events": [], "encounters": set(), "flags": Counter(), "categories": Counter()})
        day["events"].append(event)
        if event.get("_encounter_uid"):
            day["encounters"].add(event["_encounter_uid"])
        day["categories"][event["event_category"]] += 1
        day["flags"].update(event.get("quality_flags", []))
    order_by_day: defaultdict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    order_by_day_encounter: defaultdict[tuple[str, date, str | None], list[dict[str, Any]]] = defaultdict(list)
    for order in rows["lab_order"]:
        if order["event_date"] is not None:
            order_by_day[(order["patient_uid"], order["event_date"])].append(order)
            order_by_day_encounter[(order["patient_uid"], order["event_date"], order["encounter_uid"])].append(order)
    for day_key, orders in order_by_day.items():
        day = days.setdefault(day_key, {"events": [], "encounters": set(), "flags": Counter(), "categories": Counter()})
        day["flags"].update(flag for order in orders for flag in order["quality_flags"])
    detail_by_day_encounter: defaultdict[tuple[str, date, str | None], list[dict[str, Any]]] = defaultdict(list)
    detail_count_by_day: Counter[tuple[str, date]] = Counter()
    for detail in lab_detail_rows:
        if detail["event_date"] is not None:
            detail_by_day_encounter[(detail["patient_uid"], detail["event_date"], detail["encounter_uid"])].append(detail)
            detail_count_by_day[(detail["patient_uid"], detail["event_date"])] += 1
    for (patient, event_date, encounter), details in detail_by_day_encounter.items():
        orders = order_by_day_encounter[(patient, event_date, encounter)]
        samples = [detail["sample_time"] for detail in details if detail["sample_time"] is not None]
        reports = [detail["report_time"] for detail in details if detail["report_time"] is not None]
        item_names = [detail["item_name"] for detail in details]
        flags = set(flag for detail in details for flag in detail["quality_flags"])
        repeated = len(item_names) != len(set(item_names))
        if repeated:
            flags.add("SAME_ITEM_MULTIPLE_TEST")
        rows["lab_day_summary"].append({
            "day_id": day_id(patient, event_date), "patient_uid": patient, "event_date": event_date,
            "encounter_uid": encounter, "lab_order_count": len(orders), "lab_item_count": len(details),
            "specimen_type_count": len({detail.get("specimen") for detail in details if detail.get("specimen")}),
            "earliest_sample_time": min(samples) if samples else None, "latest_sample_time": max(samples) if samples else None,
            "earliest_report_time": min(reports) if reports else None, "latest_report_time": max(reports) if reports else None,
            "same_item_multiple_test_flag": repeated, "quality_flags": sorted(flags), "rule_version": RULE_VERSION,
        })
        days.setdefault((patient, event_date), {"events": [], "encounters": set(), "flags": Counter(), "categories": Counter()})["flags"].update(flags)
    for (patient, event_date), info in sorted(days.items()):
        category_counts = info["categories"]
        flags = set(info["flags"])
        if len(info["encounters"]) > 1:
            flags.add("MULTIPLE_ENCOUNTERS_SAME_DAY")
            counters["multiple_encounters_same_day_count"] += 1
        admission = any(event["event_type"] == "ADMISSION" for event in info["events"])
        discharge = any(event["event_type"] == "DISCHARGE" for event in info["events"])
        if admission and sum(category_counts.values()) >= 10:
            counters["admission_day_high_density_count"] += 1
        if discharge and sum(category_counts.values()) >= 10:
            counters["discharge_day_high_density_count"] += 1
        rows["patient_day"].append({
            "day_id": day_id(patient, event_date), "patient_uid": patient, "event_date": event_date,
            "encounter_count": len(info["encounters"]), "lab_order_count": len(order_by_day.get((patient, event_date), [])),
            "lab_item_count": detail_count_by_day[(patient, event_date)],
            "document_count": category_counts.get("document", 0), "imaging_count": category_counts.get("imaging", 0),
            "pathology_count": category_counts.get("pathology", 0), "admission_flag": admission, "discharge_flag": discharge,
            "procedure_candidate_count": 0, "narrative_eligible": category_counts.get("document", 0) > 0,
            "quality_flags": sorted(flags), "rule_version": RULE_VERSION,
        })
        for event in info["events"]:
            rows["day_event_relation"].append({
                "day_id": day_id(patient, event_date), "event_id": event["event_id"],
                "event_category": event["event_category"], "relationship_type": "OCCURRED_ON_DAY",
                "source_record_key": event["source_record_key"],
            })
            for flag in event.get("quality_flags", []):
                boundary_flags[(patient, event_date, flag)] += 1
        for order in order_by_day.get((patient, event_date), []):
            for flag in order["quality_flags"]:
                boundary_flags[(patient, event_date, flag)] += 1
    for detail in lab_detail_rows:
        if detail["event_date"] is None:
            continue
        if detail["encounter_uid"] is None:
            rows["encounter_link_candidate"].append({
                "event_id": detail["event_id"], "patient_uid": detail["patient_uid"], "encounter_uid": None,
                "candidate_status": "NO_ENCOUNTER", "candidate_reason": "no_exact_encounter_uid",
                "event_date": detail["event_date"], "source_record_key": detail["source_record_key"], "rule_version": RULE_VERSION,
            })
    for (patient, event_date, flag), count in sorted(boundary_flags.items()):
        rows["timeline_quality_flags"].append({
            "patient_uid": patient, "event_date": event_date, "quality_flag": flag, "flag_count": count,
            "evidence_json": json.dumps({"count": count}, ensure_ascii=False, sort_keys=True), "rule_version": RULE_VERSION,
        })
    for kind, output_rows in rows.items():
        if not output_rows:
            counters[f"{kind}_row_count"] = 0
        else:
            counters[f"{kind}_row_count"] = len(output_rows)
    rows["_counters"] = dict(counters)
    rows["_seen"] = {key: len(value) for key, value in seen.items()}
    rows["_selected_patient_count"] = len(selected)
    return rows


def _write_sharded_table(root: Path, kind: str, values: list[dict[str, Any]], schema: pa.Schema) -> list[dict[str, Any]]:
    output_dir = root / kind
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks = [values[index:index + MAX_SHARD_ROWS] for index in range(0, len(values), MAX_SHARD_ROWS)] or [[]]
    results = []
    for index, chunk in enumerate(chunks, 1):
        path = output_dir / f"part-{index:05d}.parquet"
        partial = path.with_name(path.name + ".partial")
        table = pa.Table.from_pylist(chunk, schema=schema)
        pq.write_table(table, partial, compression="zstd")
        check = pq.ParquetFile(partial)
        if check.metadata.num_rows != len(chunk) or check.schema_arrow != schema:
            raise RuntimeError(f"output_schema_or_row_count_invalid:{kind}:{index}")
        del check
        gc.collect()
        os.replace(partial, path)
        results.append({
            "output_kind": kind, "shard_index": index, "output_file": str(path), "row_count": len(chunk),
            "schema_sha256": schema_hash(schema), "sha256": sha256_file(path), "status": "SUCCEEDED",
        })
    return results


def _validate_published_outputs(task_dir: Path, expected: list[dict[str, Any]] | None = None) -> bool:
    if not task_dir.is_dir() or any(path.name.endswith(".partial") for path in task_dir.rglob("*")):
        return False
    files = sorted(path for path in task_dir.rglob("*.parquet") if path.is_file())
    if not files:
        return False
    expected_by_path: dict[str, dict[str, Any]] = {}
    if expected is not None:
        expected_by_path = {item["output_file"]: item for item in expected}
        expected_paths = set(expected_by_path)
        if {str(path) for path in files} != expected_paths:
            return False
    for path in files:
        try:
            parquet = pq.ParquetFile(path)
            kind = path.parent.name
            contract = OUTPUT_SCHEMAS.get(kind)
            if contract is None or parquet.schema_arrow != contract:
                return False
            if expected is not None:
                item = expected_by_path[str(path)]
                if parquet.metadata.num_rows != int(item["row_count"]):
                    return False
                if item["schema_sha256"] != schema_hash(contract):
                    return False
                if item["sha256"] != sha256_file(path):
                    return False
        except Exception:
            return False
    return True


def _connect_state(path: Path | None = None) -> sqlite3.Connection:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_state(
            task_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, output_row_count INTEGER NOT NULL DEFAULT 0,
            error_reason TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_output(
            task_id TEXT NOT NULL, output_kind TEXT NOT NULL, shard_index INTEGER NOT NULL,
            output_file TEXT NOT NULL, row_count INTEGER NOT NULL, schema_sha256 TEXT NOT NULL,
            sha256 TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(task_id, output_kind, shard_index)
        );
        """
    )
    db.commit()
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _input_hash(baseline: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({
        "freeze_manifest_sha256": baseline["freeze_manifest_sha256"],
        "stage6_manifest_sha256": baseline["stage6_manifest_sha256"],
        "stage6_task_count": baseline["stage6_task_count"],
    }, sort_keys=True).encode("utf-8")).hexdigest()


def initialize_task_state(input_sha: str) -> None:
    db = _connect_state()
    try:
        prior = db.execute("SELECT value FROM metadata WHERE key='input_sha256'").fetchone()
        if prior and prior[0] != input_sha:
            raise RuntimeError("canary_input_hash_changed")
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('input_sha256',?)", (input_sha,))
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('rule_version',?)", (RULE_VERSION,))
        db.execute(
            "INSERT OR IGNORE INTO task_state(task_id,input_sha256,status,attempts,output_row_count,error_reason,updated_at) VALUES(?,?,?,?,0,NULL,?)",
            (TASK_ID, input_sha, "PENDING", 0, _now()),
        )
        db.commit()
    finally:
        db.close()


def reset_failed_task_for_retry() -> bool:
    db = _connect_state()
    try:
        row = db.execute("SELECT status FROM task_state WHERE task_id=?", (TASK_ID,)).fetchone()
        if not row or row[0] != "FAILED":
            return False
        _archive_incomplete_artifacts()
        db.execute("DELETE FROM task_output WHERE task_id=?", (TASK_ID,))
        db.execute(
            "UPDATE task_state SET status='PENDING', output_row_count=0, error_reason=?, updated_at=? WHERE task_id=?",
            ("retry_after_implementation_fix", _now(), TASK_ID),
        )
        db.commit()
        return True
    finally:
        db.close()


def _archive_incomplete_artifacts() -> int:
    paths = [STAGING_ROOT / TASK_ID, RESTRICTED_ROOT / TASK_ID]
    paths.extend(sorted(OUTPUT_ROOT.glob("stage7_day_replay_*")))
    existing = [path for path in paths if path.exists()]
    if not existing:
        return 0
    PARTIAL_ARCHIVE.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time())}.{os.getpid()}"
    for index, path in enumerate(existing, 1):
        target = PARTIAL_ARCHIVE / f"{TASK_ID}.{stamp}.{index}.{path.name}"
        shutil.move(str(path), str(target))
    return len(existing)


def _acceptance_report_complete() -> bool:
    if not CANARY_REPORT_PATH.is_file() or not PUBLIC_REPORT_PATH.is_file():
        return False
    try:
        report = load_json(CANARY_REPORT_PATH)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        report.get("status") == "SUCCEEDED"
        and report.get("patient_count") == 100
        and report.get("repeat_check", {}).get("passed") is True
    )


def recover_running_task() -> dict[str, int]:
    db = _connect_state()
    counts = Counter()
    try:
        row = db.execute("SELECT status FROM task_state WHERE task_id=?", (TASK_ID,)).fetchone()
        if not row or row[0] not in {"RUNNING", "SUCCEEDED"}:
            return dict(counts)
        status = row[0]
        final_task = RESTRICTED_ROOT / TASK_ID
        output_rows = db.execute(
            "SELECT output_file,row_count,schema_sha256,sha256,status FROM task_output WHERE task_id=?",
            (TASK_ID,),
        ).fetchall()
        expected = [
            {"output_file": item[0], "row_count": item[1], "schema_sha256": item[2], "sha256": item[3], "status": item[4]}
            for item in output_rows
        ]
        if expected and _validate_published_outputs(final_task, expected) and _acceptance_report_complete():
            total = db.execute("SELECT COALESCE(SUM(row_count),0) FROM task_output WHERE task_id=? AND output_kind='timeline_event'", (TASK_ID,)).fetchone()[0]
            db.execute("UPDATE task_state SET status='SUCCEEDED',output_row_count=?,updated_at=? WHERE task_id=?", (total, _now(), TASK_ID))
            db.commit()
            counts[f"{status}_CONFIRMED_SUCCEEDED"] += 1
            return dict(counts)
        counts["ARCHIVED_INCOMPLETE_PATHS"] += _archive_incomplete_artifacts()
        db.execute("DELETE FROM task_output WHERE task_id=?", (TASK_ID,))
        db.execute(
            "UPDATE task_state SET status='PENDING', output_row_count=0, error_reason=?, updated_at=? WHERE task_id=?",
            (f"incomplete_{status.lower()}_task_reset", _now(), TASK_ID),
        )
        db.commit()
        counts[f"{status}_TO_PENDING"] += 1
        return dict(counts)
    finally:
        db.close()


def _claim_task(input_sha: str) -> bool:
    db = _connect_state()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,input_sha256 FROM task_state WHERE task_id=?", (TASK_ID,)).fetchone()
        if not row or row[1] != input_sha or row[0] != "PENDING":
            db.rollback()
            return False
        db.execute("UPDATE task_state SET status='RUNNING',attempts=attempts+1,error_reason=NULL,updated_at=? WHERE task_id=?", (_now(), TASK_ID))
        db.commit()
        return True
    finally:
        db.close()


def _publish_task(staging_task: Path, output_meta: list[dict[str, Any]]) -> None:
    final_task = RESTRICTED_ROOT / TASK_ID
    if final_task.exists():
        raise FileExistsError(f"refusing_to_overwrite_existing_canary_output:{final_task}")
    RESTRICTED_ROOT.mkdir(parents=True, exist_ok=True)
    os.replace(staging_task, final_task)
    db = _connect_state()
    try:
        db.executemany(
            "INSERT INTO task_output(task_id,output_kind,shard_index,output_file,row_count,schema_sha256,sha256,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (TASK_ID, item["output_kind"], item["shard_index"], str(final_task / Path(item["output_file"]).relative_to(staging_task)), item["row_count"], item["schema_sha256"], sha256_file(final_task / Path(item["output_file"]).relative_to(staging_task)), "SUCCEEDED", _now())
                for item in output_meta
            ],
        )
        db.commit()
    finally:
        db.close()


def _mark_task_succeeded(input_sha: str, timeline_event_count: int) -> None:
    db = _connect_state()
    try:
        cursor = db.execute(
            "UPDATE task_state SET status='SUCCEEDED',output_row_count=?,updated_at=? "
            "WHERE task_id=? AND input_sha256=? AND status='RUNNING'",
            (timeline_event_count, _now(), TASK_ID, input_sha),
        )
        if cursor.rowcount != 1:
            db.rollback()
            raise RuntimeError("task_not_running_at_final_commit")
        db.commit()
    finally:
        db.close()


def _output_hash_manifest(root: Path) -> list[dict[str, Any]]:
    values = []
    for path in sorted(root.rglob("*.parquet")):
        table = pq.ParquetFile(path)
        values.append({
            "relative_path": path.relative_to(root).as_posix(), "row_count": table.metadata.num_rows,
            "schema_sha256": schema_hash(table.schema_arrow), "sha256": sha256_file(path),
        })
    return values


def write_previews(rows: dict[str, Any], selected: list[str], task_root: Path | None = None) -> None:
    previews_root = (task_root or (RESTRICTED_ROOT / TASK_ID)) / "previews"
    day_rows = rows["patient_day"]
    by_patient: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in day_rows:
        by_patient[row["patient_uid"]].append(row)
    for patient in selected[:10]:
        path = previews_root / f"patient_{patient_uid_hash(patient)}.md"
        lines = [
            f"# Day timeline preview {patient_uid_hash(patient)}",
            "",
            "This restricted preview contains day-level counts only; source正文 is omitted.",
            "",
            "| event_date | lab_orders | lab_items | documents | imaging | pathology | admissions | discharges | flags |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for row in sorted(by_patient.get(patient, []), key=lambda value: value["event_date"]):
            lines.append(
                f"| {row['event_date']} | {row['lab_order_count']} | {row['lab_item_count']} | {row['document_count']} | "
                f"{row['imaging_count']} | {row['pathology_count']} | {row['admission_flag']} | {row['discharge_flag']} | "
                f"{','.join(row['quality_flags'])} |"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_fixed_selection() -> tuple[list[str], dict[str, Any], int]:
    if not SELECTION_PATH.is_file():
        raise FileNotFoundError(SELECTION_PATH)
    selection = load_json(SELECTION_PATH)
    payload = {key: selection[key] for key in ("selection_version", "seed", "patients")}
    if selection.get("selection_sha256") != _selection_hash(payload) or len(payload["patients"]) != 100:
        raise RuntimeError("patient_selection_hash_or_count_invalid")
    audit = load_json(SELECTION_AUDIT_PATH) if SELECTION_AUDIT_PATH.is_file() else {}
    return [item["patient_uid"] for item in payload["patients"]], selection, int(audit.get("candidate_pool_count", 0))


def _replay_and_compare(
    data: dict[str, Any],
    first_hashes: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="stage7_day_replay_", dir=str(OUTPUT_ROOT)) as temporary:
        replay_root = Path(temporary)
        for kind, schema in OUTPUT_SCHEMAS.items():
            _write_sharded_table(replay_root, kind, data[kind], schema)
        second_hashes = _output_hash_manifest(replay_root)
        repeat_result = {
            "performed": True,
            "passed": first_hashes == second_hashes,
            "first_output_hashes": first_hashes,
            "second_output_hashes": second_hashes,
        }
        counters = data["_counters"]
        if not repeat_result["passed"]:
            raise RuntimeError("deterministic_replay_mismatch")
        return counters, repeat_result


def _make_report(
    baseline: dict[str, Any],
    selection: dict[str, Any],
    candidate_pool_count: int,
    links: dict[str, dict[str, dict[str, Any]]],
    counters: dict[str, Any],
    output_hashes: list[dict[str, Any]],
    repeat_result: dict[str, Any],
    recovery: dict[str, int],
) -> dict[str, Any]:
    return {
        "report_version": "stage7_day_timeline_canary_v1",
        "rule_version": RULE_VERSION,
        "status": "SUCCEEDED",
        "task_id": TASK_ID,
        "seed": SEED,
        "patient_selection_sha256": selection["selection_sha256"],
        "patient_count": 100,
        "stratum_counts": Counter(item["stratum"] for item in selection["patients"]),
        "candidate_pool_count": candidate_pool_count,
        "source_link_counts": {key: len(value) for key, value in links.items()},
        "counts": counters,
        "output_hashes": output_hashes,
        "repeat_check": repeat_result,
        "soft_linking_enabled": False,
        "procedure_anchor_eligible": False,
        "pii_in_report": False,
        "timeline_started": True,
        "formal_full_run_started": False,
        "medical_semantic_extraction_started": False,
        "recovery": recovery,
        "input_baseline": {
            "freeze_manifest_sha256": baseline["freeze_manifest_sha256"],
            "stage6_manifest_sha256": baseline["stage6_manifest_sha256"],
            "stage6_task_count": baseline["stage6_task_count"],
            "stage6_status_counts": baseline["stage6_status_counts"],
        },
    }


def run_canary(repeat_check: bool = True) -> dict[str, Any]:
    baseline = load_input_baseline()
    input_sha = _input_hash(baseline)
    initialize_task_state(input_sha)
    recovery = recover_running_task()
    reset_failed_task_for_retry()
    db = _connect_state()
    try:
        status = db.execute("SELECT status FROM task_state WHERE task_id=?", (TASK_ID,)).fetchone()[0]
        if status == "SUCCEEDED":
            output_rows = db.execute("SELECT output_file,row_count,schema_sha256,sha256,status FROM task_output WHERE task_id=?", (TASK_ID,)).fetchall()
            expected = [{"output_file": row[0], "row_count": row[1], "schema_sha256": row[2], "sha256": row[3], "status": row[4]} for row in output_rows]
            if not _validate_published_outputs(RESTRICTED_ROOT / TASK_ID, expected):
                raise RuntimeError("succeeded_output_validation_failed")
            if _acceptance_report_complete():
                report = load_json(CANARY_REPORT_PATH)
                report["reused_succeeded_task"] = True
                return report
    finally:
        db.close()
    if status == "SUCCEEDED":
        raise RuntimeError("succeeded_task_missing_complete_acceptance")
    if not _claim_task(input_sha):
        raise RuntimeError("canary_task_not_claimed")
    if SELECTION_PATH.is_file():
        selected, selection, candidate_pool_count = _load_fixed_selection()
    else:
        candidates = candidate_patient_pool()
        stats = patient_activity_stats(candidates)
        selected, selection = select_or_load_patients(candidates, stats)
        candidate_pool_count = len(candidates)
        del stats, candidates
        gc.collect()
    staging_task = STAGING_ROOT / TASK_ID
    try:
        selected_set = set(selected)
        links = selected_link_maps(selected_set)
        data = collect_timeline_data(selected_set, links, baseline)
        if staging_task.exists():
            raise RuntimeError("staging_task_already_exists")
        output_meta: list[dict[str, Any]] = []
        for kind, schema in OUTPUT_SCHEMAS.items():
            output_meta.extend(_write_sharded_table(staging_task, kind, data[kind], schema))
        if not _validate_published_outputs(staging_task):
            raise RuntimeError("staging_output_validation_failed")
        first_hashes = _output_hash_manifest(staging_task)
        write_previews(data, selected, staging_task)
        repeat_result: dict[str, Any] = {"performed": False, "passed": True}
        counters = data["_counters"]
        if repeat_check:
            counters, repeat_result = _replay_and_compare(data, first_hashes)
        _publish_task(staging_task, output_meta)
        first_hashes = _output_hash_manifest(RESTRICTED_ROOT / TASK_ID)
        report = _make_report(baseline, selection, candidate_pool_count, links, counters, first_hashes, repeat_result, recovery)
        atomic_json(CANARY_REPORT_PATH, report)
        atomic_json(PUBLIC_REPORT_PATH, {
            key: value for key, value in report.items()
            if key not in {"output_hashes", "repeat_check"}
        })
        _mark_task_succeeded(input_sha, int(counters["timeline_event_count"]))
    except Exception as exc:
        db = _connect_state()
        try:
            _archive_incomplete_artifacts()
            db.execute("DELETE FROM task_output WHERE task_id=?", (TASK_ID,))
            db.execute("UPDATE task_state SET status='FAILED',error_reason=?,updated_at=? WHERE task_id=?", (type(exc).__name__, _now(), TASK_ID))
            db.commit()
        finally:
            db.close()
        raise
    return report


def validate_canary() -> dict[str, Any]:
    baseline = load_input_baseline()
    db = _connect_state()
    try:
        status = db.execute("SELECT status FROM task_state WHERE task_id=?", (TASK_ID,)).fetchone()
        outputs = db.execute("SELECT output_file,row_count,schema_sha256,sha256,status FROM task_output WHERE task_id=? ORDER BY output_kind,shard_index", (TASK_ID,)).fetchall()
        state = {
            "status": status[0] if status else None,
            "output_count": len(outputs),
            "integrity_check": db.execute("PRAGMA integrity_check").fetchone()[0],
            "partial_files": sum(
                1
                for root in (STAGING_ROOT, RESTRICTED_ROOT / TASK_ID)
                if root.exists()
                for path in root.rglob("*.partial")
            ),
        }
    finally:
        db.close()
    report = load_json(CANARY_REPORT_PATH) if CANARY_REPORT_PATH.is_file() else {}
    result = {
        "validation_passed": bool(
            state["status"] == "SUCCEEDED" and state["integrity_check"] == "ok" and state["partial_files"] == 0
            and report.get("repeat_check", {}).get("passed", False)
            and report.get("patient_count") == 100
            and baseline["stage6_task_count"] == 520
            and baseline["stage6_status_counts"].get("SUCCEEDED") == 520
            and report.get("formal_full_run_started") is False
            and report.get("medical_semantic_extraction_started") is False
        ),
        "state": state,
        "report_status": report.get("status"),
        "patient_count": report.get("patient_count"),
        "repeat_check": report.get("repeat_check", {}),
        "formal_full_run_started": report.get("formal_full_run_started"),
        "timeline_started": report.get("timeline_started"),
        "medical_semantic_extraction_started": report.get("medical_semantic_extraction_started"),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 7 simplified day-level timeline Canary")
    parser.add_argument("command", choices=("preflight", "canary", "validate"))
    parser.add_argument("--no-repeat", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "preflight":
        baseline = load_input_baseline()
        candidates = candidate_patient_pool()
        print(json.dumps({
            "data_root": str(DATA_ROOT), "stage6_task_count": baseline["stage6_task_count"],
            "stage6_status_counts": baseline["stage6_status_counts"], "imaging_task_count": len(baseline["imaging_tasks"]),
            "candidate_pool_count": len(candidates), "formal_full_run_started": False,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "canary":
        result = run_canary(repeat_check=not args.no_repeat)
        print(json.dumps({
            "status": result.get("status"), "patient_count": result.get("patient_count"),
            "stratum_counts": result.get("stratum_counts"), "repeat_passed": result.get("repeat_check", {}).get("passed"),
        }, ensure_ascii=False, indent=2))
        return 0
    result = validate_canary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

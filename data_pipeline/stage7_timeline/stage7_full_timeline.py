"""Resumable full-cohort runner for the accepted Stage 7 day timeline."""

from __future__ import annotations

import argparse
import gc
import hashlib
import heapq
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from data_pipeline.paths import data_root
from . import stage7_timeline as base
from .stage7_models import RULE_VERSION, schema_hash


DATA_ROOT = data_root()
OUTPUT_ROOT = DATA_ROOT / "pipeline_outputs_stage7_v1"
FULL_ROOT = OUTPUT_ROOT / "restricted" / "full_day_timeline"
TASK_ROOT = FULL_ROOT / "tasks"
STAGING_ROOT = OUTPUT_ROOT / "staging_full_day_timeline"
PARTIAL_ARCHIVE = OUTPUT_ROOT / "partial_archive_full_day_timeline"
AUDIT_ROOT = OUTPUT_ROOT / "audit" / "full_day_timeline"
PLAN_PATH = FULL_ROOT / "full_plan_v1.json"
PLAN_AUDIT_PATH = AUDIT_ROOT / "full_plan_audit_v1.json"
BOUNDARY_AUDIT_PATH = AUDIT_ROOT / "boundary_inventory_report_v1.json"
PROGRESS_PATH = AUDIT_ROOT / "full_progress_v1.json"
FINAL_REPORT_PATH = AUDIT_ROOT / "full_acceptance_report_v1.json"
STATE_PATH = OUTPUT_ROOT / "state" / "stage7_full_day_timeline_state_v1.sqlite3"
CANARY_REPORT = OUTPUT_ROOT / "audit" / "stage7_day_timeline_canary_report.json"
RUNNER_PATH = Path(__file__).resolve()

PLAN_VERSION = "stage7_full_day_timeline_plan_v1"
RUN_VERSION = "stage7_full_day_timeline_v1"
TARGET_SOURCE_ROWS = 125_000
MAX_PATIENTS_PER_TASK = 100
BOUNDARY_SAMPLE_PER_REASON = 50


BOUNDARY_LINK_SCHEMA = pa.schema([
    pa.field("boundary_id", pa.string()),
    pa.field("source_system", pa.string()),
    pa.field("source_record_key", pa.string()),
    pa.field("source_file", pa.large_string()),
    pa.field("source_row", pa.int64()),
    pa.field("source_record_id", pa.string()),
    pa.field("patient_uid", pa.string()),
    pa.field("encounter_uid", pa.string()),
    pa.field("disposition_class", pa.string()),
    pa.field("event_eligible", pa.bool_()),
    pa.field("timeline_candidate_eligible", pa.bool_()),
    pa.field("source_status", pa.string()),
    pa.field("quarantine_reason", pa.large_string()),
    pa.field("boundary_reasons", pa.list_(pa.string())),
    pa.field("severity", pa.string()),
    pa.field("included_in_timeline", pa.bool_()),
    pa.field("default_query_eligible", pa.bool_()),
    pa.field("requires_manual_review", pa.bool_()),
    pa.field("sample_reason", pa.string()),
    pa.field("sample_rank", pa.int32()),
    pa.field("rule_version", pa.string()),
])

EVENT_BOUNDARY_SCHEMA = pa.schema([
    pa.field("boundary_id", pa.string()),
    pa.field("event_id", pa.string()),
    pa.field("patient_uid", pa.string()),
    pa.field("encounter_uid", pa.string()),
    pa.field("event_date", pa.date32()),
    pa.field("source_system", pa.string()),
    pa.field("source_record_key", pa.string()),
    pa.field("boundary_reasons", pa.list_(pa.string())),
    pa.field("severity", pa.string()),
    pa.field("included_in_timeline", pa.bool_()),
    pa.field("default_query_eligible", pa.bool_()),
    pa.field("narrative_eligible", pa.bool_()),
    pa.field("rule_version", pa.string()),
])

FULL_SCHEMAS = {**base.OUTPUT_SCHEMAS, "boundary_link": BOUNDARY_LINK_SCHEMA,
                "boundary_review_sample": BOUNDARY_LINK_SCHEMA, "event_boundary": EVENT_BOUNDARY_SCHEMA}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _link_batches(root: Path, columns: list[str]) -> Iterable[pa.RecordBatch]:
    files = sorted(root.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(root)
    dataset = ds.dataset(files, format="parquet")
    available = set(dataset.schema.names)
    selected = [name for name in columns if name in available]
    yield from dataset.scanner(columns=selected, batch_size=200_000, use_threads=True).to_batches()


def _input_evidence() -> dict[str, Any]:
    baseline = base.load_input_baseline()
    if not CANARY_REPORT.is_file():
        raise FileNotFoundError(CANARY_REPORT)
    canary = base.load_json(CANARY_REPORT)
    if canary.get("status") != "SUCCEEDED" or not canary.get("repeat_check", {}).get("passed"):
        raise RuntimeError("accepted_canary_required")
    return {
        "freeze_manifest_sha256": baseline["freeze_manifest_sha256"],
        "stage6_manifest_sha256": baseline["stage6_manifest_sha256"],
        "canary_report_sha256": base.sha256_file(CANARY_REPORT),
        "timeline_code_sha256": base.sha256_file(Path(base.__file__)),
        "models_code_sha256": base.sha256_file(Path(__file__).with_name("stage7_models.py")),
        "runner_code_sha256": base.sha256_file(RUNNER_PATH),
        "stage6_task_count": baseline["stage6_task_count"],
    }


def _build_plan() -> dict[str, Any]:
    cohort = base.candidate_patient_pool()
    activity: Counter[str] = Counter({patient: 0 for patient in cohort})
    source_counts: Counter[str] = Counter()
    boundary_counts: Counter[str] = Counter()
    columns = ["patient_uid", "disposition_class", "event_eligible", "timeline_candidate_eligible"]
    for source_system, root in base.LINK_ROOTS.items():
        for batch in _link_batches(root, columns):
            values = batch.to_pydict()
            count = batch.num_rows
            patients = values.get("patient_uid", [None] * count)
            dispositions = values.get("disposition_class", [None] * count)
            event_eligible = values.get("event_eligible", [False] * count)
            timeline_eligible = values.get("timeline_candidate_eligible", [False] * count)
            for patient, disposition, event_ok, timeline_ok in zip(patients, dispositions, event_eligible, timeline_eligible):
                in_cohort = bool(patient) and patient in cohort
                accepted = in_cohort and disposition == "hard" and bool(event_ok) and source_system != "lab_quarantine"
                if accepted:
                    activity[patient] += 1
                    source_counts[source_system] += 1
                if in_cohort and not accepted:
                    boundary_counts[f"{source_system}:EXCLUDED"] += 1
                if in_cohort and not bool(timeline_ok):
                    boundary_counts[f"{source_system}:TIMELINE_CANDIDATE_FALSE"] += 1
    ordered = sorted(activity.items(), key=lambda item: (-item[1], item[0]))
    tasks: list[dict[str, Any]] = []
    patients: list[str] = []
    source_rows = 0
    for patient, count in ordered:
        if patients and (len(patients) >= MAX_PATIENTS_PER_TASK or source_rows + count > TARGET_SOURCE_ROWS):
            tasks.append({"patient_uids": patients, "expected_source_event_count": source_rows})
            patients, source_rows = [], 0
        patients.append(patient)
        source_rows += count
    if patients:
        tasks.append({"patient_uids": patients, "expected_source_event_count": source_rows})
    for index, task in enumerate(tasks, 1):
        task["task_id"] = f"patient_{index:06d}"
        task["priority"] = index
    evidence = _input_evidence()
    payload = {
        "plan_version": PLAN_VERSION,
        "run_version": RUN_VERSION,
        "rule_version": RULE_VERSION,
        "cohort_rule": "hard-linked document OR hard-linked pathology patient_uid",
        "cohort_patient_count": len(cohort),
        "eligible_source_event_count": sum(activity.values()),
        "source_event_counts": dict(sorted(source_counts.items())),
        "boundary_counts": dict(sorted(boundary_counts.items())),
        "target_source_rows": TARGET_SOURCE_ROWS,
        "max_patients_per_task": MAX_PATIENTS_PER_TASK,
        "input_evidence": evidence,
        "tasks": tasks,
    }
    return {**payload, "plan_sha256": _json_hash(payload)}


def build_or_load_plan() -> dict[str, Any]:
    if PLAN_PATH.is_file():
        plan = base.load_json(PLAN_PATH)
        payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if plan.get("plan_sha256") != _json_hash(payload):
            raise RuntimeError("full_plan_hash_invalid")
        if plan.get("input_evidence") != _input_evidence():
            raise RuntimeError("full_plan_input_or_code_changed")
        return plan
    plan = _build_plan()
    base.atomic_json(PLAN_PATH, plan)
    base.atomic_json(PLAN_AUDIT_PATH, {
        "plan_version": plan["plan_version"], "plan_sha256": plan["plan_sha256"],
        "cohort_rule": plan["cohort_rule"], "cohort_patient_count": plan["cohort_patient_count"],
        "eligible_source_event_count": plan["eligible_source_event_count"],
        "source_event_counts": plan["source_event_counts"], "boundary_counts": plan["boundary_counts"],
        "patient_task_count": len(plan["tasks"]),
        "task_patient_counts": [len(task["patient_uids"]) for task in plan["tasks"]],
        "task_expected_source_event_counts": [task["expected_source_event_count"] for task in plan["tasks"]],
        "contains_patient_uid": False,
    })
    return plan


def _connect_state() -> sqlite3.Connection:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_PATH, timeout=30)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_state(
            task_id TEXT PRIMARY KEY, task_kind TEXT NOT NULL, priority INTEGER NOT NULL,
            patient_count INTEGER NOT NULL, expected_source_event_count INTEGER NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, output_row_count INTEGER NOT NULL DEFAULT 0,
            error_reason TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_output(
            task_id TEXT NOT NULL, output_kind TEXT NOT NULL, shard_index INTEGER NOT NULL,
            output_file TEXT NOT NULL, row_count INTEGER NOT NULL, schema_sha256 TEXT NOT NULL,
            sha256 TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(task_id, output_kind, shard_index)
        );
    """)
    db.commit()
    return db


def initialize_state(plan: dict[str, Any]) -> None:
    db = _connect_state()
    try:
        prior = db.execute("SELECT value FROM metadata WHERE key='plan_sha256'").fetchone()
        if prior and prior[0] != plan["plan_sha256"]:
            raise RuntimeError("state_plan_hash_changed")
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('plan_sha256',?)", (plan["plan_sha256"],))
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('run_version',?)", (RUN_VERSION,))
        db.execute(
            "INSERT OR IGNORE INTO task_state VALUES(?,?,?,?,?,'PENDING',0,0,NULL,?)",
            ("boundary_000001", "boundary", 0, 0, 0, _now()),
        )
        for task in plan["tasks"]:
            db.execute(
                "INSERT OR IGNORE INTO task_state VALUES(?,?,?,?,?,'PENDING',0,0,NULL,?)",
                (task["task_id"], "patient", task["priority"], len(task["patient_uids"]),
                 task["expected_source_event_count"], _now()),
            )
        db.commit()
    finally:
        db.close()


def _task_manifest(task_dir: Path) -> dict[str, Any]:
    return base.load_json(task_dir / "task_manifest.json")


def _validate_task_dir(task_dir: Path) -> bool:
    if not task_dir.is_dir() or any(path.name.endswith(".partial") for path in task_dir.rglob("*")):
        return False
    try:
        manifest = _task_manifest(task_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    expected = manifest.get("outputs", [])
    files = sorted(task_dir.rglob("*.parquet"))
    if {path.relative_to(task_dir).as_posix() for path in files} != {item["relative_path"] for item in expected}:
        return False
    for item in expected:
        path = task_dir / item["relative_path"]
        kind = path.parent.name
        contract = FULL_SCHEMAS.get(kind)
        if contract is None:
            return False
        try:
            parquet = pq.ParquetFile(path)
            if parquet.metadata.num_rows != item["row_count"] or parquet.schema_arrow != contract:
                return False
            if schema_hash(contract) != item["schema_sha256"] or base.sha256_file(path) != item["sha256"]:
                return False
        except Exception:
            return False
    return True


def _archive(path: Path, task_id: str) -> None:
    if not path.exists():
        return
    PARTIAL_ARCHIVE.mkdir(parents=True, exist_ok=True)
    target = PARTIAL_ARCHIVE / f"{task_id}.{int(time.time())}.{os.getpid()}.{path.name}"
    shutil.move(str(path), str(target))


def recover_interrupted() -> dict[str, int]:
    db = _connect_state()
    counts: Counter[str] = Counter()
    try:
        running = db.execute("SELECT task_id FROM task_state WHERE status='RUNNING' ORDER BY priority").fetchall()
        for (task_id,) in running:
            final = TASK_ROOT / task_id
            staging = STAGING_ROOT / task_id
            if _validate_task_dir(final):
                _commit_task_from_manifest(db, task_id, final)
                counts["RUNNING_TO_SUCCEEDED"] += 1
            else:
                _archive(staging, task_id)
                _archive(final, task_id)
                db.execute("DELETE FROM task_output WHERE task_id=?", (task_id,))
                db.execute(
                    "UPDATE task_state SET status='PENDING',output_row_count=0,error_reason=?,updated_at=? WHERE task_id=?",
                    ("interrupted_task_reset", _now(), task_id),
                )
                counts["RUNNING_TO_PENDING"] += 1
        db.commit()
        return dict(counts)
    finally:
        db.close()


def _claim_next() -> tuple[str, str] | None:
    db = _connect_state()
    try:
        db.execute("BEGIN IMMEDIATE")
        failed = db.execute("SELECT task_id,error_reason FROM task_state WHERE status='FAILED' ORDER BY priority LIMIT 1").fetchone()
        if failed:
            db.rollback()
            raise RuntimeError(f"failed_task_requires_review:{failed[0]}:{failed[1]}")
        row = db.execute("SELECT task_id,task_kind FROM task_state WHERE status='PENDING' ORDER BY priority LIMIT 1").fetchone()
        if row is None:
            db.rollback()
            return None
        cursor = db.execute(
            "UPDATE task_state SET status='RUNNING',attempts=attempts+1,error_reason=NULL,updated_at=? WHERE task_id=? AND status='PENDING'",
            (_now(), row[0]),
        )
        if cursor.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        return str(row[0]), str(row[1])
    finally:
        db.close()


def _manifest_outputs(root: Path, metas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "output_kind": item["output_kind"], "shard_index": item["shard_index"],
        "relative_path": Path(item["output_file"]).relative_to(root).as_posix(),
        "row_count": item["row_count"], "schema_sha256": item["schema_sha256"],
        "sha256": item["sha256"],
    } for item in metas]


def _write_task_manifest(staging: Path, task_id: str, task_kind: str, metas: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    base.atomic_json(staging / "task_manifest.json", {
        "manifest_version": RUN_VERSION, "task_id": task_id, "task_kind": task_kind,
        "plan_sha256": plan["plan_sha256"], "outputs": _manifest_outputs(staging, metas),
    })


def _commit_task_from_manifest(db: sqlite3.Connection, task_id: str, final: Path) -> None:
    manifest = _task_manifest(final)
    db.execute("DELETE FROM task_output WHERE task_id=?", (task_id,))
    output_rows = []
    for item in manifest["outputs"]:
        output_rows.append((
            task_id, item["output_kind"], item["shard_index"], str(final / item["relative_path"]),
            item["row_count"], item["schema_sha256"], item["sha256"], "SUCCEEDED", _now(),
        ))
    db.executemany("INSERT INTO task_output VALUES(?,?,?,?,?,?,?,?,?)", output_rows)
    total = sum(item["row_count"] for item in manifest["outputs"] if item["output_kind"] == "timeline_event")
    db.execute(
        "UPDATE task_state SET status='SUCCEEDED',output_row_count=?,error_reason=NULL,updated_at=? WHERE task_id=?",
        (total, _now(), task_id),
    )


def _publish_task(staging: Path, task_id: str) -> None:
    final = TASK_ROOT / task_id
    if final.exists():
        raise FileExistsError(final)
    TASK_ROOT.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final)
    if not _validate_task_dir(final):
        raise RuntimeError("published_task_validation_failed")
    db = _connect_state()
    try:
        db.execute("BEGIN IMMEDIATE")
        _commit_task_from_manifest(db, task_id, final)
        db.commit()
    finally:
        db.close()


def _link_boundary_reasons(source: str, disposition: str, event_ok: bool, timeline_ok: bool) -> list[str]:
    reasons: list[str] = []
    if source == "lab_quarantine":
        reasons.append("SOURCE_QUARANTINE")
    if disposition == "conflict":
        reasons.append("LINK_CONFLICT")
    elif disposition == "soft":
        reasons.append("SOFT_LINK_EXCLUDED")
    elif disposition == "unmatched":
        reasons.append("UNMATCHED_EXCLUDED")
    elif disposition != "hard":
        reasons.append("NON_HARD_LINK_EXCLUDED")
    if not event_ok:
        reasons.append("EVENT_INELIGIBLE")
    if not timeline_ok:
        reasons.append("TIMELINE_CANDIDATE_FALSE")
    return sorted(set(reasons))


def _severity(reasons: Iterable[str]) -> str:
    values = set(reasons)
    if values & {"LINK_CONFLICT", "EVENT_TIME_MISSING", "INVALID_INTERVAL_ORDER"}:
        return "HIGH"
    if values & {"SOFT_LINK_EXCLUDED", "UNMATCHED_EXCLUDED", "SOURCE_QUARANTINE", "EVENT_INELIGIBLE"}:
        return "MEDIUM"
    return "LOW"


def _boundary_row(source: str, row: dict[str, Any], reasons: list[str], included: bool) -> dict[str, Any]:
    key = str(row.get("source_record_key") or "")
    identity = hashlib.sha256(f"{source}|{key}|{'|'.join(reasons)}".encode("utf-8")).hexdigest()
    return {
        "boundary_id": identity, "source_system": source, "source_record_key": key,
        "source_file": str(row.get("source_file") or ""), "source_row": row.get("source_row"),
        "source_record_id": str(row.get("source_record_id") or ""),
        "patient_uid": str(row.get("patient_uid") or "") or None,
        "encounter_uid": str(row.get("encounter_uid") or "") or None,
        "disposition_class": str(row.get("disposition_class") or ""),
        "event_eligible": bool(row.get("event_eligible")),
        "timeline_candidate_eligible": bool(row.get("timeline_candidate_eligible")),
        "source_status": str(row.get("source_status") or ""),
        "quarantine_reason": str(row.get("quarantine_reason") or ""),
        "boundary_reasons": reasons, "severity": _severity(reasons),
        "included_in_timeline": included, "default_query_eligible": False,
        "requires_manual_review": _severity(reasons) in {"HIGH", "MEDIUM"},
        "sample_reason": None, "sample_rank": None, "rule_version": RULE_VERSION,
    }


def process_boundary_task(staging: Path, plan: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cohort = {patient for task in plan["tasks"] for patient in task["patient_uids"]}
    inventory: list[dict[str, Any]] = []
    sample_heaps: dict[tuple[str, str], list[tuple[int, str, dict[str, Any]]]] = {}
    counts: Counter[str] = Counter()
    columns = ["source_record_key", "source_file", "source_row", "source_record_id", "patient_uid",
               "encounter_uid", "disposition_class", "event_eligible", "timeline_candidate_eligible",
               "source_status", "quarantine_reason"]
    for source, root in base.LINK_ROOTS.items():
        for batch in _link_batches(root, columns):
            for row in batch.to_pylist():
                patient = str(row.get("patient_uid") or "")
                in_cohort = bool(patient) and patient in cohort
                disposition = str(row.get("disposition_class") or "")
                event_ok = bool(row.get("event_eligible"))
                timeline_ok = bool(row.get("timeline_candidate_eligible"))
                reasons = _link_boundary_reasons(source, disposition, event_ok, timeline_ok)
                accepted = in_cohort and disposition == "hard" and event_ok and source != "lab_quarantine"
                if reasons:
                    for reason in reasons:
                        counts[f"global:{source}:{reason}"] += 1
                        candidate = _boundary_row(source, row, reasons, accepted)
                        rank = int(hashlib.sha256(f"20260826|{source}|{reason}|{candidate['boundary_id']}".encode()).hexdigest(), 16)
                        heap = sample_heaps.setdefault((source, reason), [])
                        item = (-rank, candidate["boundary_id"], candidate)
                        if len(heap) < BOUNDARY_SAMPLE_PER_REASON:
                            heapq.heappush(heap, item)
                        elif item[0] > heap[0][0]:
                            heapq.heapreplace(heap, item)
                if in_cohort and reasons:
                    inventory.append(_boundary_row(source, row, reasons, accepted))
                    for reason in reasons:
                        counts[f"cohort:{source}:{reason}"] += 1
    inventory.sort(key=lambda row: (row["source_system"], row["source_record_key"], row["boundary_id"]))
    samples: list[dict[str, Any]] = []
    for (source, reason), heap in sorted(sample_heaps.items()):
        ranked = sorted(((-item[0], item[2]) for item in heap), key=lambda item: (item[0], item[1]["boundary_id"]))
        for index, (_, row) in enumerate(ranked, 1):
            samples.append({**row, "sample_reason": reason, "sample_rank": index})
    metas = []
    metas.extend(base._write_sharded_table(staging, "boundary_link", inventory, BOUNDARY_LINK_SCHEMA))
    metas.extend(base._write_sharded_table(staging, "boundary_review_sample", samples, BOUNDARY_LINK_SCHEMA))
    report = {
        "report_version": RUN_VERSION, "cohort_patient_count": len(cohort),
        "boundary_inventory_count": len(inventory), "boundary_review_sample_count": len(samples),
        "counts": dict(sorted(counts.items())), "default_query_eligible": False,
        "automatic_patient_merge_performed": False, "automatic_time_repair_performed": False,
        "contains_patient_uid": False,
    }
    return metas, report


def _sort_rows(values: list[dict[str, Any]]) -> None:
    keys = ("patient_uid", "event_date", "event_id", "lab_order_id", "day_id", "source_record_key", "quality_flag")
    values.sort(key=lambda row: tuple("" if row.get(key) is None else str(row.get(key)) for key in keys))


def _event_boundaries(data: dict[str, Any]) -> list[dict[str, Any]]:
    encounter_by_event = {row["event_id"]: row.get("encounter_uid") for row in data["event_source_map"]}
    result = []
    for event in data["timeline_event"]:
        reasons = list(event.get("quality_flags") or [])
        if event.get("event_date") is None:
            reasons.append("EVENT_TIME_MISSING")
            event["narrative_eligible"] = False
        encounter = encounter_by_event.get(event["event_id"])
        if encounter is None:
            reasons.append("ENCOUNTER_UID_MISSING")
        reasons = sorted(set(reasons))
        if not reasons:
            continue
        boundary_id = hashlib.sha256(f"event|{event['event_id']}|{'|'.join(reasons)}".encode()).hexdigest()
        result.append({
            "boundary_id": boundary_id, "event_id": event["event_id"], "patient_uid": event["patient_uid"],
            "encounter_uid": encounter, "event_date": event.get("event_date"),
            "source_system": event["source_system"], "source_record_key": event["source_record_key"],
            "boundary_reasons": reasons, "severity": _severity(reasons), "included_in_timeline": True,
            "default_query_eligible": False, "narrative_eligible": bool(event.get("narrative_eligible")),
            "rule_version": RULE_VERSION,
        })
    return result


def process_patient_task(staging: Path, patients: list[str]) -> list[dict[str, Any]]:
    selected = set(patients)
    links = base.selected_link_maps(selected, include_quarantine=False)
    data = base.collect_timeline_data(selected, links, base.load_input_baseline())
    data["event_boundary"] = _event_boundaries(data)
    metas: list[dict[str, Any]] = []
    for kind, schema in {**base.OUTPUT_SCHEMAS, "event_boundary": EVENT_BOUNDARY_SCHEMA}.items():
        _sort_rows(data[kind])
        metas.extend(base._write_sharded_table(staging, kind, data[kind], schema))
    del data, links
    gc.collect()
    return metas


def _plan_task(plan: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in plan["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise KeyError(task_id)


def _write_progress(plan: dict[str, Any]) -> dict[str, Any]:
    db = _connect_state()
    try:
        statuses = dict(db.execute("SELECT status,COUNT(*) FROM task_state GROUP BY status").fetchall())
        completed_patients = db.execute("SELECT COALESCE(SUM(patient_count),0) FROM task_state WHERE status='SUCCEEDED'").fetchone()[0]
        completed_events = db.execute("SELECT COALESCE(SUM(output_row_count),0) FROM task_state WHERE status='SUCCEEDED'").fetchone()[0]
        attempts = db.execute("SELECT COALESCE(SUM(attempts),0) FROM task_state").fetchone()[0]
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        db.close()
    report = {
        "report_version": RUN_VERSION, "plan_sha256": plan["plan_sha256"], "status_counts": statuses,
        "task_count": len(plan["tasks"]) + 1, "cohort_patient_count": plan["cohort_patient_count"],
        "completed_patient_count": completed_patients, "completed_timeline_event_count": completed_events,
        "attempt_sum": attempts, "integrity_check": integrity, "formal_full_run_started": True,
        "medical_semantic_extraction_started": False, "contains_patient_uid": False,
    }
    base.atomic_json(PROGRESS_PATH, report)
    return report


def run_full(max_tasks: int | None = None) -> dict[str, Any]:
    plan = build_or_load_plan()
    initialize_state(plan)
    recovery = recover_interrupted()
    processed = 0
    while max_tasks is None or processed < max_tasks:
        claimed = _claim_next()
        if claimed is None:
            break
        task_id, task_kind = claimed
        staging = STAGING_ROOT / task_id
        try:
            if staging.exists():
                raise RuntimeError("staging_task_exists_before_run")
            if task_kind == "boundary":
                metas, boundary_report = process_boundary_task(staging, plan)
            else:
                task = _plan_task(plan, task_id)
                metas = process_patient_task(staging, task["patient_uids"])
                boundary_report = None
            _write_task_manifest(staging, task_id, task_kind, metas, plan)
            if not _validate_task_dir(staging):
                raise RuntimeError("staging_task_validation_failed")
            _publish_task(staging, task_id)
            if boundary_report is not None:
                base.atomic_json(BOUNDARY_AUDIT_PATH, boundary_report)
            processed += 1
            _write_progress(plan)
        except Exception as exc:
            _archive(staging, task_id)
            db = _connect_state()
            try:
                db.execute("UPDATE task_state SET status='FAILED',error_reason=?,updated_at=? WHERE task_id=?",
                           (f"{type(exc).__name__}:{exc}", _now(), task_id))
                db.commit()
            finally:
                db.close()
            _write_progress(plan)
            raise
    progress = _write_progress(plan)
    if progress["status_counts"].get("SUCCEEDED") == progress["task_count"]:
        final = {**progress, "status": "SUCCEEDED", "validation_pending": True,
                 "boundary_report_sha256": base.sha256_file(BOUNDARY_AUDIT_PATH)}
        base.atomic_json(FINAL_REPORT_PATH, final)
    return {**progress, "processed_this_run": processed, "recovery": recovery}


def run_probe() -> dict[str, Any]:
    plan = build_or_load_plan()
    task = plan["tasks"][0]
    with tempfile.TemporaryDirectory(prefix="stage7_full_probe_", dir=str(OUTPUT_ROOT)) as temporary:
        root = Path(temporary)
        metas = process_patient_task(root, task["patient_uids"])
        _write_task_manifest(root, "probe", "patient", metas, plan)
        if not _validate_task_dir(root):
            raise RuntimeError("full_probe_validation_failed")
        peak_rows = sum(item["row_count"] for item in metas)
    return {"probe_passed": True, "patient_count": len(task["patient_uids"]),
            "expected_source_event_count": task["expected_source_event_count"], "output_row_count": peak_rows}


def status() -> dict[str, Any]:
    plan = build_or_load_plan()
    initialize_state(plan)
    return _write_progress(plan)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 7 full day-timeline runner")
    parser.add_argument("command", choices=("initialize", "probe", "run", "status"))
    parser.add_argument("--max-tasks", type=int)
    args = parser.parse_args(argv)
    if args.command == "initialize":
        plan = build_or_load_plan()
        initialize_state(plan)
        result = {"initialized": True, "cohort_patient_count": plan["cohort_patient_count"],
                  "eligible_source_event_count": plan["eligible_source_event_count"],
                  "patient_task_count": len(plan["tasks"])}
    elif args.command == "probe":
        result = run_probe()
    elif args.command == "run":
        result = run_full(args.max_tasks)
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

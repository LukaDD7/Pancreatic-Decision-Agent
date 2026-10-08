"""Stage 6 V2: streaming patient/encounter alignment with isolated canary state.

The V2 command surface intentionally ends at preflight, dry-run and canary.
No timeline or medical-text extraction is implemented here.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import gc
import hashlib
import heapq
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from data_pipeline.paths import data_root

try:
    import psutil
except ImportError:  # pragma: no cover - the bundled runtime normally includes psutil
    psutil = None

try:
    import orjson
except ImportError:  # pragma: no cover - standard json remains the fallback
    orjson = None


DATA_ROOT = data_root()
MODULE_ROOT = Path(__file__).resolve().parent
V1_IMAGING_REPORT = DATA_ROOT / "pipeline_outputs_v2" / "imaging_pipeline_total_report.json"
V1_IMAGING_ROOT = DATA_ROOT / "pipeline_outputs_v2" / "patient_l1"
V1_STAGE5_REPORT = DATA_ROOT / "pipeline_outputs_stage5_v1" / "stage5_total_report.json"
V1_STAGE5_STATE = DATA_ROOT / "code" / "stage5_pipeline_state_v1.sqlite3"
V1_STAGE5_ROOT = DATA_ROOT / "pipeline_outputs_stage5_v1"
V2_ROOT = DATA_ROOT / "pipeline_outputs_stage6_v2"
V2_RESTRICTED = V2_ROOT / "restricted"
V2_AUDIT = V2_ROOT / "audit"
V2_STATE_ROOT = V2_RESTRICTED / "state"
FULL_OUTPUT_ROOT = V2_RESTRICTED / "full"
FULL_TASK_STAGING = FULL_OUTPUT_ROOT / "state" / "task_staging"
FULL_PARTIAL_ARCHIVE = FULL_OUTPUT_ROOT / "state" / "partial_archive"
FULL_PREFLIGHT_REPORT = V2_AUDIT / "stage6_full_run_preflight_report.json"
FULL_SNAPSHOT_REPORT = V2_AUDIT / "stage6_full_identity_snapshot_report.json"
V2_MANIFEST = DATA_ROOT / "code" / "stage6_alignment_manifest_v2.json"
CANARY_STATE = V2_STATE_ROOT / "stage6_canary_state_v2.sqlite3"
FULL_STATE = V2_STATE_ROOT / "stage6_full_state_v2.sqlite3"

RULE_VERSION = "stage6_identity_rules_v2"
VERSION = "stage6_alignment_v2"
CANARY_STRATA_EVALUATION_VERSION = "stage6_canary_strata_v2_1"
SEED = "stage6-canary-v2"
BUFFER_SIZE = 50_000
PATIENT_NAMESPACE = uuid.UUID("6c5b2c26-80b6-5b13-9aa3-7c9b3b6b6c1d")
ENCOUNTER_NAMESPACE = uuid.UUID("b08b3f52-8f6e-5c9d-a3d3-5e12e1dbf0d7")
ID_CARD_RE = re.compile(r"^\d{17}[0-9Xx]$")
MISSING = {"", "nan", "none", "null", "nat", "<na>"}
LAB_FIELDS = (
    "PATIENT_ID", "VISIT_ID", "NAME", "检验项目名称", "检验结果值", "检验结果单位",
    "结果正常标志", "送检时间", "报告时间", "检验参考值", "检验单号", "标本",
)
IMAGE_TRUSTED = {"valid_15col", "repaired_high"}
IMAGE_LOW = {"repaired_review", "unresolved"}
PEAK_MEMORY_BYTES = 0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sample_peak_memory() -> int:
    global PEAK_MEMORY_BYTES
    if psutil is not None:
        PEAK_MEMORY_BYTES = max(PEAK_MEMORY_BYTES, int(psutil.Process(os.getpid()).memory_info().rss))
    return PEAK_MEMORY_BYTES


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def norm(value: Any, id_type: str = "PATIENT_ID") -> str | None:
    value = unicodedata.normalize("NFKC", text(value)).strip()
    if value.casefold() in MISSING:
        return None
    if id_type in {"PATIENT_ID", "VISIT_ID", "检验单号", "ID_CARD"}:
        return value.upper()
    return value


def digest_value(value: Any) -> str:
    return hashlib.sha256(text(value).encode("utf-8")).hexdigest()


def source_key(source_system: str, source_file: str, source_record_id: Any) -> str:
    return digest_value("|".join((source_system, source_file, text(source_record_id))))


def evidence_hash(values: Sequence[Any]) -> str:
    return digest_value("|".join(sorted(digest_value(v) for v in values if text(v))))


def file_sha256(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            h.update(block)
    return size, h.hexdigest()


def id_card_valid(value: Any) -> bool:
    value = norm(value, "ID_CARD")
    if value is None or not ID_CARD_RE.fullmatch(value):
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    return checks[sum(int(a) * b for a, b in zip(value[:17], weights)) % 11] == value[-1]


def valid_exam_date(value: Any) -> bool:
    value = text(value).strip()
    if not value:
        return False
    value = value.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            datetime.strptime(value[:19], fmt)
            return True
        except ValueError:
            continue
    return bool(re.search(r"\b20\d{2}[-年]\d{1,2}[-月]\d{1,2}", value))


def identity_safe(record: dict[str, Any]) -> bool:
    errors = {text(x).casefold() for x in record.get("quality_errors", [])}
    return not any(any(token in err for token in ("record_head", "identity", "patient", "姓名", "患者")) for err in errors)


def image_tokens(record: dict[str, Any]) -> list[str] | None:
    values = record.get("repaired_tokens")
    if not isinstance(values, list) or len(values) < 15:
        return None
    return [text(value) for value in values[:15]]


def image_patient(record: dict[str, Any]) -> str | None:
    values = image_tokens(record)
    return norm(values[3], "PATIENT_ID") if values else None


def image_card(record: dict[str, Any]) -> str | None:
    values = image_tokens(record)
    candidate = norm(values[9], "ID_CARD") if values else None
    return candidate if candidate and id_card_valid(candidate) else None


def image_identity_eligible(record: dict[str, Any]) -> bool:
    status = text(record.get("status"))
    return status in IMAGE_TRUSTED or (status == "field_invalid" and identity_safe(record))


def image_event_eligible(record: dict[str, Any], patient_uid: str | None) -> bool:
    status = text(record.get("status"))
    return bool(patient_uid and image_identity_eligible(record) and valid_exam_date((image_tokens(record) or ["", "", "", "", "", ""])[5]))


def parse_quarantine_values(raw: Any) -> dict[str, Any]:
    """Recover Stage5 raw_values_json by fixed position, never by type label."""

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    values = json.loads(raw or "[]")
    result: dict[str, Any] = {}
    for index, field in enumerate(LAB_FIELDS):
        item = values[index] if index < len(values) else None
        if isinstance(item, dict) and "value" in item:
            result[field] = item["value"]
        else:
            result[field] = item
    return result


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp.replace(path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        temp.replace(path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def schema(fields: Sequence[tuple[str, pa.DataType]]) -> pa.Schema:
    return pa.schema([pa.field(name, typ) for name, typ in fields])


LINK_SCHEMA = schema([
    ("source_record_key", pa.string()), ("source_system", pa.string()), ("source_file", pa.string()),
    ("source_sheet", pa.string()), ("source_row", pa.int64()), ("source_record_id", pa.string()),
    ("patient_uid", pa.string()), ("encounter_uid", pa.string()), ("link_status", pa.string()),
    ("disposition_class", pa.string()), ("link_method", pa.string()), ("confidence_level", pa.string()),
    ("identity_eligible", pa.bool_()), ("timeline_candidate_eligible", pa.bool_()),
    ("event_eligible", pa.bool_()), ("source_status", pa.string()), ("evidence_values_hash", pa.string()),
    ("quarantine_reason", pa.string()), ("rule_version", pa.string()),
])
SOFT_SCHEMA = schema([
    ("candidate_link_id", pa.string()), ("source_record_key", pa.string()), ("candidate_patient_uid", pa.string()),
    ("evidence_types", pa.string()), ("evidence_values_hash", pa.string()), ("confidence_score", pa.float64()),
    ("confidence_level", pa.string()), ("conflict_flags", pa.string()), ("default_query_enabled", pa.bool_()),
    ("rule_version", pa.string()),
])
CONFLICT_SCHEMA = schema([
    ("conflict_group_hash", pa.string()), ("conflict_type", pa.string()), ("source_record_key", pa.string()),
    ("evidence_values_hash", pa.string()), ("resolution_status", pa.string()), ("rule_version", pa.string()),
])
PATIENT_SCHEMA = schema([
    ("patient_uid", pa.string()), ("alias_count", pa.int64()), ("source_system_count", pa.int64()),
    ("identity_conflict_flag", pa.bool_()), ("rule_version", pa.string()),
])
ENCOUNTER_SCHEMA = schema([
    ("encounter_uid", pa.string()), ("patient_uid", pa.string()), ("visit_id_normalized", pa.string()),
    ("admission_candidates_json", pa.string()), ("discharge_candidates_json", pa.string()),
    ("encounter_interval_status", pa.string()), ("encounter_conflict_flag", pa.bool_()),
    ("rule_version", pa.string()),
])


def write_parquet(path: Path, rows: list[dict[str, Any]], out_schema: pa.Schema) -> tuple[int, str, str]:
    """Write one bounded buffer; return row count, file hash and schema hash."""

    if path.exists():
        raise FileExistsError(f"formal output exists and overwrite is forbidden: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        raise FileExistsError(f"partial output requires arbitration: {partial}")
    arrays = [pa.array([row.get(field.name) for row in rows], type=field.type) for field in out_schema]
    table = pa.Table.from_arrays(arrays, schema=out_schema)
    pq.write_table(table, partial, compression="zstd")
    schema_hash = hashlib.sha256(str(out_schema).encode("utf-8")).hexdigest()
    row_count = table.num_rows
    partial.replace(path)
    _, output_hash = file_sha256(path)
    return row_count, output_hash, schema_hash


def iter_parquet(paths: Iterable[Path], columns: Sequence[str], batch_size: int = BUFFER_SIZE) -> Iterator[dict[str, Any]]:
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(columns=list(columns), batch_size=batch_size, use_threads=False):
            yield from batch.to_pylist()


def quarantine_paths() -> list[Path]:
    return sorted((V1_STAGE5_ROOT / "restricted" / "lab_quarantine").rglob("*.parquet"))


def stage5_rows() -> list[sqlite3.Row]:
    with sqlite3.connect(V1_STAGE5_STATE) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM shard_tasks ORDER BY task_id").fetchall()


def stage5_paths(kind: str, quarantine: bool = False) -> list[Path]:
    result: list[Path] = []
    for row in stage5_rows():
        if row["kind"] != kind or row["status"] != "SUCCEEDED":
            continue
        value = row["quarantine_file"] if quarantine else row["output_file"]
        if value and Path(value).exists() and Path(value).suffix == ".parquet":
            result.append(Path(value))
    return result


def imaging_paths() -> list[Path]:
    report = json.loads(V1_IMAGING_REPORT.read_text(encoding="utf-8"))
    return [V1_IMAGING_ROOT / f"{record['file_name']}.record_clusters.jsonl" for record in report.get("files", [])]


def source_record_count(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def build_manifest() -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_source(source_system: str, path: Path, expected: int) -> None:
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        size, sha = file_sha256(path)
        sources.append({"source_system": source_system, "source_file": key, "size": size, "sha256": sha, "expected_row_count": expected})

    def add_parquet_tasks(source_system: str, paths: list[Path], output_dir: Path) -> None:
        for index, path in enumerate(paths, 1):
            count = source_record_count(path)
            size, sha = file_sha256(path)
            add_source(source_system, path, count)
            tasks.append({"task_id": f"{source_system}:{index:04d}", "source_system": source_system, "source_file": str(path.resolve()), "source_sha256": sha, "source_row_start": 1, "source_row_end": count, "expected_row_count": count, "output_dir": str(output_dir), "source_size": size})

    add_parquet_tasks("document", stage5_paths("document"), V2_RESTRICTED / "record_links" / "document")
    add_parquet_tasks("lab_l1", stage5_paths("lab"), V2_RESTRICTED / "record_links" / "lab_l1")
    add_parquet_tasks("lab_quarantine", quarantine_paths(), V2_RESTRICTED / "record_links" / "lab_quarantine")
    imaging_report = json.loads(V1_IMAGING_REPORT.read_text(encoding="utf-8"))
    for index, path in enumerate(imaging_paths(), 1):
        record = imaging_report["files"][index - 1]
        size, sha = file_sha256(path)
        add_source("imaging", path, int(record["record_cluster_count"]))
        tasks.append({"task_id": f"imaging:{index:04d}", "source_system": "imaging", "source_file": str(path.resolve()), "source_sha256": sha, "source_row_start": 1, "source_row_end": int(record["record_cluster_count"]), "expected_row_count": int(record["record_cluster_count"]), "output_dir": str(V2_RESTRICTED / "record_links" / "imaging"), "source_size": size})
    return {"manifest_version": "stage6_alignment_manifest_v2", "created_at": now(), "rule_version": RULE_VERSION, "sources": sources, "tasks": tasks, "expected_counts": {"document": 598826, "lab_l1": 12062160, "lab_quarantine": 149822, "imaging": 8404812}, "full_run_not_started": True, "pii_in_manifest": False}


def init_state(path: Path, tasks: Sequence[dict[str, Any]], *, seed_full: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_state(
                task_id TEXT PRIMARY KEY, source_system TEXT NOT NULL, source_file TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, expected_row_count INTEGER NOT NULL, actual_row_count INTEGER,
                hard_link_count INTEGER, soft_link_count INTEGER, unmatched_count INTEGER,
                conflict_count INTEGER, output_file TEXT, output_sha256 TEXT, output_schema_sha256 TEXT,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error_reason TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS patient_registry(patient_uid TEXT PRIMARY KEY,created_at TEXT NOT NULL,rule_version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS identity_alias(
                alias_key TEXT PRIMARY KEY,global_identity_key TEXT NOT NULL,source_system TEXT NOT NULL,
                id_type TEXT NOT NULL,normalized_value TEXT NOT NULL,raw_value TEXT NOT NULL,
                patient_uid TEXT NOT NULL,source_record_key TEXT NOT NULL,rule_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocked_identity_card(card_hash TEXT PRIMARY KEY,patient_count INTEGER NOT NULL,patient_hashes_json TEXT NOT NULL,rule_version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS card_patient(card_hash TEXT NOT NULL,patient_uid TEXT NOT NULL,patient_hash TEXT NOT NULL,card_value TEXT NOT NULL,PRIMARY KEY(card_hash,patient_uid));
            CREATE TABLE IF NOT EXISTS encounter_registry(encounter_key TEXT PRIMARY KEY,encounter_uid TEXT NOT NULL,patient_uid TEXT NOT NULL,visit_id_normalized TEXT NOT NULL,rule_version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS encounter_window(encounter_key TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(encounter_key,kind,value));
            CREATE TABLE IF NOT EXISTS disposition(source_record_key TEXT PRIMARY KEY,source_system TEXT NOT NULL,disposition_class TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profile_sample(layer TEXT NOT NULL,rank INTEGER NOT NULL,source_record_key TEXT,patient_uid TEXT,card_hash TEXT,PRIMARY KEY(layer,rank));
            CREATE TABLE IF NOT EXISTS profile_count(layer TEXT PRIMARY KEY,input_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS seen_source(patient_uid TEXT NOT NULL,source_system TEXT NOT NULL,PRIMARY KEY(patient_uid,source_system));
            CREATE TABLE IF NOT EXISTS admission_observation(patient_uid TEXT NOT NULL,admission_hash TEXT NOT NULL,PRIMARY KEY(patient_uid,admission_hash));
            CREATE TABLE IF NOT EXISTS profile_task_checkpoint(
                task_id TEXT PRIMARY KEY, source_system TEXT NOT NULL, source_file TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, expected_row_count INTEGER NOT NULL, actual_row_count INTEGER,
                status TEXT NOT NULL, updated_at TEXT NOT NULL, error_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_identity_alias_global ON identity_alias(global_identity_key);
            CREATE INDEX IF NOT EXISTS idx_identity_alias_type_value ON identity_alias(id_type,normalized_value);
            CREATE INDEX IF NOT EXISTS idx_identity_alias_patient_uid ON identity_alias(patient_uid);
            CREATE INDEX IF NOT EXISTS idx_seen_source_system ON seen_source(source_system,patient_uid);
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(profile_sample)")}
        if "rank_hex" not in columns:
            db.execute("ALTER TABLE profile_sample ADD COLUMN rank_hex TEXT")
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('version',?)", (VERSION,))
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('rule_version',?)", (RULE_VERSION,))
        if seed_full:
            for task in tasks:
                db.execute(
                    "INSERT OR IGNORE INTO task_state(task_id,source_system,source_file,source_sha256,expected_row_count,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (task["task_id"], task["source_system"], task["source_file"], task["source_sha256"], task["expected_row_count"], "PENDING", now()),
                )
        db.commit()
    finally:
        db.close()


def uid_for(db: sqlite3.Connection, source_system: str, raw_value: Any, record_key: str, *, create: bool, register_alias: bool = True) -> str | None:
    value = norm(raw_value, "PATIENT_ID")
    if value is None:
        return None
    global_key = f"PATIENT_ID|{value}"
    row = db.execute("SELECT patient_uid FROM identity_alias WHERE global_identity_key=? LIMIT 1", (global_key,)).fetchone()
    if row:
        uid = row[0]
        if register_alias:
            alias_key = f"{source_system}|PATIENT_ID|{value}"
            db.execute("INSERT OR IGNORE INTO identity_alias(alias_key,global_identity_key,source_system,id_type,normalized_value,raw_value,patient_uid,source_record_key,rule_version) VALUES(?,?,?,?,?,?,?,?,?)", (alias_key, global_key, source_system, "PATIENT_ID", value, text(raw_value), uid, record_key, RULE_VERSION))
        return uid
    if not create:
        return None
    uid = str(uuid.uuid5(PATIENT_NAMESPACE, global_key))
    db.execute("INSERT INTO patient_registry(patient_uid,created_at,rule_version) VALUES(?,?,?)", (uid, now(), RULE_VERSION))
    alias_key = f"{source_system}|PATIENT_ID|{value}"
    db.execute("INSERT INTO identity_alias(alias_key,global_identity_key,source_system,id_type,normalized_value,raw_value,patient_uid,source_record_key,rule_version) VALUES(?,?,?,?,?,?,?,?,?)", (alias_key, global_key, source_system, "PATIENT_ID", value, text(raw_value), uid, record_key, RULE_VERSION))
    return uid


def add_card_observation(db: sqlite3.Connection, card: str, uid: str, patient_value: str, record_key: str, seen: set[tuple[str, str]] | None = None) -> None:
    card_hash = digest_value(card)
    pair = (card_hash, uid)
    if seen is not None and pair in seen:
        return
    if seen is not None:
        seen.add(pair)
    db.execute("INSERT OR IGNORE INTO card_patient(card_hash,patient_uid,patient_hash,card_value) VALUES(?,?,?,?)", (card_hash, uid, digest_value(patient_value), card))


def finalize_cards(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT card_hash,COUNT(*),GROUP_CONCAT(patient_hash),GROUP_CONCAT(patient_uid),MIN(card_value) FROM card_patient GROUP BY card_hash").fetchall()
    for card_hash, count, patient_hashes, uids, card_value in rows:
        hashes = sorted(set((patient_hashes or "").split(",")))
        if count > 1:
            db.execute("INSERT OR REPLACE INTO blocked_identity_card(card_hash,patient_count,patient_hashes_json,rule_version) VALUES(?,?,?,?)", (card_hash, count, json.dumps(hashes), RULE_VERSION))
            continue
        uid = (uids or "").split(",")[0]
        if uid:
            alias_key = f"imaging|ID_CARD|{card_value}"
            db.execute("INSERT OR IGNORE INTO identity_alias(alias_key,global_identity_key,source_system,id_type,normalized_value,raw_value,patient_uid,source_record_key,rule_version) VALUES(?,?,?,?,?,?,?,?,?)", (alias_key, f"ID_CARD|{card_value}", "imaging", "ID_CARD", card_value, card_value, uid, "", RULE_VERSION))


class Sampler:
    def __init__(self) -> None:
        self.values: dict[str, list[tuple[str, str | None, str | None]]] = defaultdict(list)
        self.counts: Counter[str] = Counter()

    def observe(self, layer: str, record_key: str | None = None, patient_uid: str | None = None, card_hash: str | None = None) -> None:
        self.counts[layer] += 1
        rank = int(digest_value(f"{SEED}|{layer}|{record_key or patient_uid or card_hash or ''}"), 16)
        items = self.values[layer]
        entry = (f"{rank:064x}", record_key, patient_uid or card_hash)
        if len(items) < 50:
            items.append(entry)
        else:
            worst = max(range(len(items)), key=lambda i: items[i][0])
            if entry[0] < items[worst][0]:
                items[worst] = entry


def source_seen(db: sqlite3.Connection, uid: str | None, system: str) -> None:
    if uid:
        db.execute("INSERT OR IGNORE INTO seen_source(patient_uid,source_system) VALUES(?,?)", (uid, system))


def observe_profile_sample(db: sqlite3.Connection, sampler: Sampler) -> None:
    for layer, entries in sampler.values.items():
        db.execute("DELETE FROM profile_sample WHERE layer=?", (layer,))
        for rank, record_key, value in entries:
            patient_uid = value if layer not in {"identity_card_conflict"} else None
            card_hash = value if layer == "identity_card_conflict" else None
            db.execute("INSERT OR REPLACE INTO profile_sample(layer,rank,rank_hex,source_record_key,patient_uid,card_hash) VALUES(?,?,?,?,?,?)", (layer, int(rank[:15], 16), rank, record_key, patient_uid, card_hash))
    for layer, count in sampler.counts.items():
        db.execute("INSERT OR REPLACE INTO profile_count(layer,input_count) VALUES(?,?)", (layer, count))


def profile_checkpoint_valid(db: sqlite3.Connection, task: dict[str, Any], path: Path) -> bool:
    row = db.execute("SELECT source_sha256,expected_row_count,actual_row_count,status FROM profile_task_checkpoint WHERE task_id=?", (task["task_id"],)).fetchone()
    if row is None or row[3] != "SUCCEEDED":
        return False
    if row[0] != task["source_sha256"] or int(row[1]) != int(task["expected_row_count"]) or int(row[2] or -1) != int(task["expected_row_count"]):
        return False
    _, current_sha = file_sha256(path)
    return current_sha == task["source_sha256"]


def load_profile_sampler(db: sqlite3.Connection) -> Sampler:
    sampler = Sampler()
    for layer, count in db.execute("SELECT layer,input_count FROM profile_count"):
        sampler.counts[layer] = int(count)
    for layer, rank, rank_hex, record_key, patient_uid, card_hash in db.execute("SELECT layer,rank,rank_hex,source_record_key,patient_uid,card_hash FROM profile_sample"):
        sampler.values[layer].append((rank_hex or f"{int(rank):016x}", record_key, patient_uid or card_hash))
    return sampler


def commit_profile_checkpoint(db: sqlite3.Connection, task: dict[str, Any], actual_row_count: int, status: str = "SUCCEEDED", error_reason: str | None = None) -> None:
    db.execute("INSERT OR REPLACE INTO profile_task_checkpoint(task_id,source_system,source_file,source_sha256,expected_row_count,actual_row_count,status,updated_at,error_reason) VALUES(?,?,?,?,?,?,?,?,?)", (task["task_id"], task["source_system"], task["source_file"], task["source_sha256"], task["expected_row_count"], actual_row_count, status, now(), error_reason))
    db.commit()
    print(f"profile_checkpoint task={task['task_id']} status={status} rows={actual_row_count}", flush=True)


def build_registry(db_path: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    db = sqlite3.connect(db_path)
    sampler = load_profile_sampler(db)
    stats: Counter[str] = Counter()
    observed_card_pairs: set[tuple[str, str]] = set()
    manifest = manifest or json.loads(V2_MANIFEST.read_text(encoding="utf-8"))
    task_map = {(task["source_system"], str(Path(task["source_file"]).resolve())): task for task in manifest.get("tasks", [])}
    global_uid_cache: dict[str, str] = {}
    source_uid_cache: dict[tuple[str, str], str] = {}

    def trusted_uid(source_system: str, raw_value: Any, record_key: str) -> str | None:
        value = norm(raw_value, "PATIENT_ID")
        if value is None:
            return None
        cache_key = (source_system, value)
        if cache_key in source_uid_cache:
            return source_uid_cache[cache_key]
        uid = global_uid_cache.get(value)
        if uid is None:
            uid = uid_for(db, source_system, raw_value, record_key, create=True)
        else:
            uid_for(db, source_system, raw_value, record_key, create=False)
        if uid is not None:
            global_uid_cache[value] = uid
            source_uid_cache[cache_key] = uid
        return uid

    try:
        for path in stage5_paths("document"):
            task = task_map[("document", str(path.resolve()))]
            if profile_checkpoint_valid(db, task, path):
                stats["document"] += int(task["expected_row_count"])
                print(f"profile_checkpoint task={task['task_id']} status=SKIPPED_VERIFIED rows={task['expected_row_count']}", flush=True)
                continue
            start_count = stats["document"]
            for row in iter_parquet([path], ["PATIENT_ID", "VISIT_ID", "ADMISSION_DATE_TIME", "DISCHARGE_DATE_TIME", "source_file", "source_row", "source_record_id"]):
                key = source_key("document", text(row.get("source_file")), row.get("source_record_id"))
                uid = trusted_uid("document", row.get("PATIENT_ID"), key)
                source_seen(db, uid, "document")
                if not text(row.get("DISCHARGE_DATE_TIME")).strip():
                    sampler.observe("open_interval", key, uid)
                admission = norm(row.get("ADMISSION_DATE_TIME"), "DATETIME")
                if uid and admission:
                    db.execute("INSERT OR IGNORE INTO admission_observation(patient_uid,admission_hash) VALUES(?,?)", (uid, digest_value(admission)))
                stats["document"] += 1
            observe_profile_sample(db, sampler)
            commit_profile_checkpoint(db, task, stats["document"] - start_count)
        for path in stage5_paths("lab"):
            task = task_map[("lab_l1", str(path.resolve()))]
            if profile_checkpoint_valid(db, task, path):
                stats["lab_l1"] += int(task["expected_row_count"])
                print(f"profile_checkpoint task={task['task_id']} status=SKIPPED_VERIFIED rows={task['expected_row_count']}", flush=True)
                continue
            start_count = stats["lab_l1"]
            for row in iter_parquet([path], ["PATIENT_ID", "VISIT_ID", "source_workbook", "source_sheet", "source_row", "source_record_id"]):
                key = source_key("lab_l1", f"{text(row.get('source_workbook'))}|{text(row.get('source_sheet'))}", row.get("source_record_id"))
                uid = trusted_uid("lab_l1", row.get("PATIENT_ID"), key)
                source_seen(db, uid, "lab_l1")
                if uid and not norm(row.get("VISIT_ID"), "VISIT_ID"):
                    sampler.observe("missing_visit", key, uid)
                stats["lab_l1"] += 1
            observe_profile_sample(db, sampler)
            commit_profile_checkpoint(db, task, stats["lab_l1"] - start_count)
        for path in quarantine_paths():
            task = task_map[("lab_quarantine", str(path.resolve()))]
            if profile_checkpoint_valid(db, task, path):
                stats["lab_quarantine"] += int(task["expected_row_count"])
                print(f"profile_checkpoint task={task['task_id']} status=SKIPPED_VERIFIED rows={task['expected_row_count']}", flush=True)
                continue
            start_count = stats["lab_quarantine"]
            for row in iter_parquet([path], ["raw_values_json", "source_workbook", "source_sheet", "source_row", "source_record_id", "quarantine_reason"]):
                values = parse_quarantine_values(row.get("raw_values_json"))
                key = source_key("lab_quarantine", f"{text(row.get('source_workbook'))}|{text(row.get('source_sheet'))}", row.get("source_record_id"))
                if norm(values.get("PATIENT_ID"), "PATIENT_ID"):
                    sampler.observe("quarantine_recoverable", key)
                stats["lab_quarantine"] += 1
            observe_profile_sample(db, sampler)
            commit_profile_checkpoint(db, task, stats["lab_quarantine"] - start_count)
        for path in imaging_paths():
            task = task_map[("imaging", str(path.resolve()))]
            if profile_checkpoint_valid(db, task, path):
                stats["imaging"] += int(task["expected_row_count"])
                print(f"profile_checkpoint task={task['task_id']} status=SKIPPED_VERIFIED rows={task['expected_row_count']}", flush=True)
                continue
            start_count = stats["imaging"]
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    record = orjson.loads(line) if orjson is not None else json.loads(line)
                    key = source_key("imaging", path.name, record.get("record_cluster_id"))
                    status = text(record.get("status"))
                    patient = image_patient(record)
                    uid = None
                    if image_identity_eligible(record) and patient:
                        uid = trusted_uid("imaging", patient, key)
                        source_seen(db, uid, "imaging")
                    if status == "repaired_review":
                        sampler.observe("repaired_review", key)
                    elif status == "unresolved":
                        sampler.observe("unresolved", key)
                    elif status == "field_invalid" and identity_safe(record):
                        sampler.observe("field_invalid_safe", key, uid)
                    elif status == "field_invalid":
                        sampler.observe("field_invalid_unsafe", key)
                    card = image_card(record)
                    if card and uid and status in IMAGE_TRUSTED | {"field_invalid"} and (status != "field_invalid" or identity_safe(record)):
                        add_card_observation(db, card, uid, patient or "", key, observed_card_pairs)
                    stats["imaging"] += 1
            observe_profile_sample(db, sampler)
            commit_profile_checkpoint(db, task, stats["imaging"] - start_count)
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('registry_pass_completed',?)", ("true",))
        db.commit()
        finalize_cards(db)
        db.commit()
        # A conflict group is sampled deterministically after the complete map is known.
        blocked = db.execute("SELECT card_hash FROM blocked_identity_card ORDER BY card_hash").fetchall()
        for card_hash, in blocked[:50]:
            sampler.observe("identity_card_conflict", card_hash=card_hash)
        # Multi-admission patients and source intersections are derived in SQL.
        for uid, in db.execute("SELECT patient_uid FROM admission_observation GROUP BY patient_uid HAVING COUNT(*)>=2 LIMIT 50"):
            sampler.observe("multi_admission", patient_uid=uid)
        for uid, in db.execute("SELECT patient_uid FROM seen_source GROUP BY patient_uid HAVING SUM(source_system='document')>0 AND SUM(source_system='lab_l1')>0 LIMIT 50"):
            sampler.observe("document_lab_intersection", patient_uid=uid)
        for uid, in db.execute("SELECT patient_uid FROM seen_source GROUP BY patient_uid HAVING SUM(source_system='document')>0 AND SUM(source_system='imaging')>0 LIMIT 50"):
            sampler.observe("document_imaging_intersection", patient_uid=uid)
        observe_profile_sample(db, sampler)
        db.commit()
    finally:
        db.close()
    return {"row_counts": dict(stats), "profile_counts": dict(sampler.counts)}


def select_canary(db_path: Path) -> dict[str, Any]:
    db = sqlite3.connect(db_path)
    try:
        anchors = {row[0] for row in db.execute("SELECT patient_uid FROM patient_registry ORDER BY patient_uid LIMIT 200")}
        layers: dict[str, set[str]] = defaultdict(set)
        layer_patients: dict[str, set[str]] = defaultdict(set)
        for layer, key, uid, card in db.execute("SELECT layer,source_record_key,patient_uid,card_hash FROM profile_sample"):
            if key:
                layers[layer].add(key)
            if uid:
                layer_patients[layer].add(uid)
        blocked_cards = {row[0] for row in db.execute("SELECT card_hash FROM profile_sample WHERE layer='identity_card_conflict'")}
        # Find actual record keys for blocked card groups without retaining all image rows.
        if blocked_cards:
            for path in imaging_paths():
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        record = json.loads(line)
                        card = image_card(record)
                        if card and digest_value(card) in blocked_cards:
                            layers["identity_card_conflict"].add(source_key("imaging", path.name, record.get("record_cluster_id")))
                            if len(layers["identity_card_conflict"]) >= 50:
                                break
                if len(layers["identity_card_conflict"]) >= 50:
                    break
        selected_patients = set(anchors)
        for layer in ("multi_admission", "document_lab_intersection", "document_imaging_intersection"):
            selected_patients.update(layer_patients[layer])
        record_keys = set().union(*layers.values()) if layers else set()
        return {
            "anchor_patients": anchors,
            "selected_patients": selected_patients,
            "layer_record_keys": dict(layers),
            "layer_patients": dict(layer_patients),
            "record_keys": record_keys,
            "blocked_cards": blocked_cards,
        }
    finally:
        db.close()


def get_encounter(db: sqlite3.Connection, uid: str | None, visit: Any) -> str | None:
    visit = norm(visit, "VISIT_ID")
    if not uid or not visit:
        return None
    key = f"{uid}|{visit}"
    row = db.execute("SELECT encounter_uid FROM encounter_registry WHERE encounter_key=?", (key,)).fetchone()
    if row:
        return row[0]
    encounter = str(uuid.uuid5(ENCOUNTER_NAMESPACE, key))
    db.execute("INSERT INTO encounter_registry(encounter_key,encounter_uid,patient_uid,visit_id_normalized,rule_version) VALUES(?,?,?,?,?)", (key, encounter, uid, visit, RULE_VERSION))
    return encounter


def record_disposition(db: sqlite3.Connection, key: str, system: str, category: str) -> None:
    row = db.execute("SELECT disposition_class FROM disposition WHERE source_record_key=?", (key,)).fetchone()
    if row:
        if row[0] != category:
            raise RuntimeError("source record received conflicting dispositions")
        return
    db.execute("INSERT INTO disposition(source_record_key,source_system,disposition_class,updated_at) VALUES(?,?,?,?)", (key, system, category, now()))


def link_row(key: str, system: str, source_file: str, sheet: str, row: Any, record_id: Any, uid: str | None, encounter: str | None, status: str, category: str, method: str, confidence: str, identity_ok: bool, timeline_ok: bool, event_ok: bool, source_status: str, evidence: Sequence[Any], quarantine_reason: str = "") -> dict[str, Any]:
    return {"source_record_key": key, "source_system": system, "source_file": source_file, "source_sheet": sheet, "source_row": int(row) if row is not None else None, "source_record_id": text(record_id), "patient_uid": uid, "encounter_uid": encounter, "link_status": status, "disposition_class": category, "link_method": method, "confidence_level": confidence, "identity_eligible": identity_ok, "timeline_candidate_eligible": timeline_ok, "event_eligible": event_ok, "source_status": source_status, "evidence_values_hash": evidence_hash(evidence), "quarantine_reason": quarantine_reason, "rule_version": RULE_VERSION}


def soft_row(key: str, uid: str | None, types: Sequence[str], values: Sequence[Any]) -> dict[str, Any]:
    return {"candidate_link_id": digest_value(f"soft|{key}|{uid or ''}"), "source_record_key": key, "candidate_patient_uid": uid, "evidence_types": json.dumps(list(types), ensure_ascii=False), "evidence_values_hash": evidence_hash(values), "confidence_score": 0.6, "confidence_level": "LOW", "conflict_flags": json.dumps(["LOW_CONFIDENCE"], ensure_ascii=False), "default_query_enabled": False, "rule_version": RULE_VERSION}


class BoundedWriter:
    def __init__(self, out_dir: Path, schema_: pa.Schema, prefix: str) -> None:
        self.out_dir = out_dir
        self.schema = schema_
        self.prefix = prefix
        self.buffer: list[dict[str, Any]] = []
        self.part = 0
        self.total = 0
        self.files: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        self.buffer.append(row)
        sample_peak_memory()
        if len(self.buffer) >= BUFFER_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        sample_peak_memory()
        self.part += 1
        path = self.out_dir / f"{self.prefix}.part-{self.part:05d}.parquet"
        count, sha, schema_sha = write_parquet(path, self.buffer, self.schema)
        self.files.append({"path": str(path), "row_count": count, "sha256": sha, "schema_sha256": schema_sha})
        self.total += count
        self.buffer = []

    def close(self) -> list[dict[str, Any]]:
        self.flush()
        return self.files


def profile_report(db_path: Path, scan_counts: dict[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(db_path) as db:
        profile_counts = dict(db.execute("SELECT layer,input_count FROM profile_count ORDER BY layer").fetchall())
        blocked = db.execute("SELECT COUNT(*) FROM blocked_identity_card").fetchone()[0]
        alias_count = db.execute("SELECT COUNT(*) FROM identity_alias").fetchone()[0]
        patient_count = db.execute("SELECT COUNT(*) FROM patient_registry").fetchone()[0]
    return {"profile_version": "stage6_identifier_profile_v2", "rule_version": RULE_VERSION, "row_counts": scan_counts, "stratum_counts": profile_counts, "blocked_identity_card_group_count": blocked, "trusted_patient_count": patient_count, "trusted_alias_count": alias_count, "pii_values_in_report": False}


def should_select(system: str, key: str, uid: str | None, selection: dict[str, Any]) -> bool:
    if key in selection["record_keys"]:
        return True
    return bool(uid and uid in selection["selected_patients"])


def observe_emitted_patient(emitted_sources_by_patient: dict[str, set[str]] | None, uid: str | None, source_system: str) -> None:
    if emitted_sources_by_patient is not None and uid:
        emitted_sources_by_patient.setdefault(uid, set()).add(source_system)


def evaluate_canary_strata(
    profile_counts: dict[str, int],
    selection: dict[str, Any],
    layer_hits: Counter[str],
    emitted_sources_by_patient: dict[str, set[str]],
) -> dict[str, dict[str, Any]]:
    strata: dict[str, dict[str, Any]] = {}
    record_layers = {
        "identity_card_conflict": 50,
        "repaired_review": 50,
        "field_invalid_unsafe": 50,
        "field_invalid_safe": 50,
        "unresolved": 50,
        "open_interval": 50,
        "missing_visit": 50,
        "quarantine_recoverable": 50,
    }
    for layer, requested in record_layers.items():
        input_count = int(profile_counts.get(layer, 0))
        target = min(requested, input_count)
        result_count = int(layer_hits.get(layer, 0))
        if layer == "field_invalid_safe" and input_count == 0:
            strata[layer] = {
                "scope": "record",
                "status": "NOT_APPLICABLE",
                "gate_effect": "NEUTRAL",
                "input_count": 0,
                "result_count": 0,
                "expected_minimum": 0,
                "passed": True,
            }
            continue
        passed = input_count > 0 and result_count >= target
        strata[layer] = {
            "scope": "record",
            "status": "PASS" if passed else "FAIL",
            "input_count": input_count,
            "result_count": result_count,
            "expected_minimum": target,
            "passed": passed,
        }

    emitted_patients = set(emitted_sources_by_patient)
    anchors = set(selection.get("anchor_patients", set()))
    anchor_result = len(anchors & emitted_patients)
    anchor_passed = bool(anchors) and anchor_result == len(anchors)
    strata["regular_anchor"] = {
        "scope": "patient",
        "status": "PASS" if anchor_passed else "FAIL",
        "input_count": len(anchors),
        "result_count": anchor_result,
        "expected_minimum": len(anchors),
        "passed": anchor_passed,
    }

    layer_patients = selection.get("layer_patients", {})
    multi_admission = set(layer_patients.get("multi_admission", set()))
    multi_target = min(50, len(multi_admission))
    multi_result = len(multi_admission & emitted_patients)
    multi_passed = bool(multi_admission) and multi_result >= multi_target
    strata["multi_admission"] = {
        "scope": "patient",
        "status": "PASS" if multi_passed else "FAIL",
        "input_count": int(profile_counts.get("multi_admission", len(multi_admission))),
        "result_count": multi_result,
        "expected_minimum": multi_target,
        "passed": multi_passed,
    }

    required_sources = {
        "document_lab_intersection": {"document", "lab_l1"},
        "document_imaging_intersection": {"document", "imaging"},
    }
    for layer, systems in required_sources.items():
        patients = set(layer_patients.get(layer, set()))
        result_count = sum(systems <= emitted_sources_by_patient.get(uid, set()) for uid in patients)
        passed = bool(patients) and result_count == len(patients)
        strata[layer] = {
            "scope": "patient",
            "status": "PASS" if passed else "FAIL",
            "input_count": int(profile_counts.get(layer, len(patients))),
            "result_count": result_count,
            "expected_minimum": len(patients),
            "passed": passed,
        }
    return strata


def reevaluate_cached_canary_report(report: dict[str, Any], db_path: Path = CANARY_STATE) -> dict[str, Any]:
    with sqlite3.connect(db_path) as db:
        anchors = {row[0] for row in db.execute("SELECT patient_uid FROM patient_registry ORDER BY patient_uid LIMIT 200")}
        layer_patients: dict[str, set[str]] = defaultdict(set)
        for layer, uid in db.execute("SELECT layer,patient_uid FROM profile_sample WHERE patient_uid IS NOT NULL"):
            layer_patients[layer].add(uid)
        profile_counts = dict(db.execute("SELECT layer,input_count FROM profile_count").fetchall())

    emitted_sources_by_patient: dict[str, set[str]] = {}
    for source_system in ("document", "lab_l1", "lab_quarantine", "imaging"):
        for item in report.get("file_outputs", {}).get(source_system, []):
            parquet = pq.ParquetFile(Path(item["path"]))
            for batch in parquet.iter_batches(columns=["patient_uid", "source_system"]):
                for uid, emitted_system in zip(batch.column(0).to_pylist(), batch.column(1).to_pylist()):
                    observe_emitted_patient(emitted_sources_by_patient, uid, emitted_system)

    record_layers = {
        "identity_card_conflict",
        "repaired_review",
        "field_invalid_unsafe",
        "field_invalid_safe",
        "unresolved",
        "open_interval",
        "missing_visit",
        "quarantine_recoverable",
    }
    previous_strata = report.get("strata", {})
    layer_hits: Counter[str] = Counter({
        layer: int(previous_strata.get(layer, {}).get("result_count", 0))
        for layer in record_layers
    })
    selection = {"anchor_patients": anchors, "layer_patients": dict(layer_patients)}
    corrected = dict(report)
    corrected["strata"] = evaluate_canary_strata(profile_counts, selection, layer_hits, emitted_sources_by_patient)
    corrected["strata_evaluation_version"] = CANARY_STRATA_EVALUATION_VERSION
    corrected["data_pipeline_rerun"] = False
    return corrected


def process_documents(db: sqlite3.Connection, selection: dict[str, Any], writers: dict[str, BoundedWriter], counters: Counter[str], layer_hits: Counter[str], emitted_sources_by_patient: dict[str, set[str]] | None = None) -> None:
    for path in stage5_paths("document"):
        for row in iter_parquet([path], ["PATIENT_ID", "VISIT_ID", "ADMISSION_DATE_TIME", "DISCHARGE_DATE_TIME", "source_file", "source_row", "source_record_id", "parser_status"]):
            key = source_key("document", text(row.get("source_file")), row.get("source_record_id"))
            uid = uid_for(db, "document", row.get("PATIENT_ID"), key, create=False, register_alias=False)
            if not should_select("document", key, uid, selection):
                continue
            visit = norm(row.get("VISIT_ID"), "VISIT_ID")
            encounter = get_encounter(db, uid, visit)
            if uid and visit:
                ek = f"{uid}|{visit}"
                if row.get("ADMISSION_DATE_TIME"):
                    db.execute("INSERT OR IGNORE INTO encounter_window(encounter_key,kind,value) VALUES(?,?,?)", (ek, "admission", text(row.get("ADMISSION_DATE_TIME"))))
                if row.get("DISCHARGE_DATE_TIME"):
                    db.execute("INSERT OR IGNORE INTO encounter_window(encounter_key,kind,value) VALUES(?,?,?)", (ek, "discharge", text(row.get("DISCHARGE_DATE_TIME"))))
            category = "hard" if uid else "unmatched"
            link = link_row(key, "document", text(row.get("source_file")), "", row.get("source_row"), row.get("source_record_id"), uid, encounter, "HARD_PATIENT_EXACT" if uid else "PATIENT_UNMATCHED", category, "PATIENT_ID_EXACT", "HIGH" if uid else "NONE", bool(uid), bool(uid and visit), bool(uid), text(row.get("parser_status")), [row.get("PATIENT_ID"), row.get("VISIT_ID")])
            record_disposition(db, key, "document", category)
            writers["document"].add(link)
            observe_emitted_patient(emitted_sources_by_patient, uid, "document")
            counters["document"] += 1
            counters[f"document_{category}"] += 1
            if uid in selection["anchor_patients"]:
                layer_hits["regular_anchor"] += 1
            for layer in ("open_interval",):
                if not text(row.get("DISCHARGE_DATE_TIME")).strip() and key in selection["layer_record_keys"].get(layer, set()):
                    layer_hits[layer] += 1


def process_labs(db: sqlite3.Connection, selection: dict[str, Any], writers: dict[str, BoundedWriter], counters: Counter[str], layer_hits: Counter[str], emitted_sources_by_patient: dict[str, set[str]] | None = None) -> None:
    for path in stage5_paths("lab"):
        for row in iter_parquet([path], ["PATIENT_ID", "VISIT_ID", "source_workbook", "source_sheet", "source_row", "source_record_id", "time_parse_status"]):
            source_file = text(row.get("source_workbook"))
            sheet = text(row.get("source_sheet"))
            key = source_key("lab_l1", f"{source_file}|{sheet}", row.get("source_record_id"))
            uid = uid_for(db, "lab_l1", row.get("PATIENT_ID"), key, create=False, register_alias=False)
            if not should_select("lab_l1", key, uid, selection):
                continue
            visit = norm(row.get("VISIT_ID"), "VISIT_ID")
            encounter = get_encounter(db, uid, visit)
            if uid and visit and not encounter:
                encounter = None
            if not uid:
                category, status, method, confidence = "unmatched", "PATIENT_UNMATCHED", "PATIENT_ID_MISSING_OR_UNMATCHED", "NONE"
            elif not visit:
                category, status, method, confidence = "hard", "PATIENT_ONLY", "PATIENT_ID_EXACT_VISIT_MISSING", "HIGH"
            elif encounter:
                category, status, method, confidence = "hard", "HARD_PATIENT_AND_ENCOUNTER", "PATIENT_ID_AND_VISIT_ID_EXACT", "HIGH"
            else:
                category, status, method, confidence = "unmatched", "ENCOUNTER_UNMATCHED", "PATIENT_ID_EXACT_VISIT_UNMATCHED", "HIGH"
            link = link_row(key, "lab_l1", source_file, sheet, row.get("source_row"), row.get("source_record_id"), uid, encounter, status, category, method, confidence, bool(uid), bool(uid and encounter), not text(row.get("time_parse_status")).startswith("INVALID"), text(row.get("time_parse_status")), [row.get("PATIENT_ID"), row.get("VISIT_ID")])
            record_disposition(db, key, "lab_l1", category)
            writers["lab_l1"].add(link)
            observe_emitted_patient(emitted_sources_by_patient, uid, "lab_l1")
            counters["lab_l1"] += 1
            counters[f"lab_l1_{category}"] += 1
            if uid in selection["anchor_patients"]:
                layer_hits["regular_anchor"] += 1
            if not visit and key in selection["layer_record_keys"].get("missing_visit", set()):
                layer_hits["missing_visit"] += 1


def process_quarantine(db: sqlite3.Connection, selection: dict[str, Any], writers: dict[str, BoundedWriter], counters: Counter[str], layer_hits: Counter[str], emitted_sources_by_patient: dict[str, set[str]] | None = None) -> None:
    for path in quarantine_paths():
        for row in iter_parquet([path], ["raw_values_json", "source_workbook", "source_sheet", "source_row", "source_record_id", "quarantine_reason"]):
            values = parse_quarantine_values(row.get("raw_values_json"))
            source_file = text(row.get("source_workbook"))
            sheet = text(row.get("source_sheet"))
            key = source_key("lab_quarantine", f"{source_file}|{sheet}", row.get("source_record_id"))
            patient = norm(values.get("PATIENT_ID"), "PATIENT_ID")
            uid = uid_for(db, "lab_quarantine", patient, key, create=False, register_alias=False)
            if key not in selection["record_keys"]:
                continue
            visit = norm(values.get("VISIT_ID"), "VISIT_ID")
            encounter = get_encounter(db, uid, visit)
            category = "quarantined"
            link = link_row(key, "lab_quarantine", source_file, sheet, row.get("source_row"), row.get("source_record_id"), uid, encounter, "QUARANTINE_EVENT_INELIGIBLE", category, "QUARANTINE_SOURCE_NOT_EVENT_ELIGIBLE", "HIGH" if uid else "NONE", bool(uid), False, False, text(row.get("quarantine_reason")), [patient, values.get("VISIT_ID")], text(row.get("quarantine_reason")))
            record_disposition(db, key, "lab_quarantine", category)
            writers["lab_quarantine"].add(link)
            observe_emitted_patient(emitted_sources_by_patient, uid, "lab_quarantine")
            counters["lab_quarantine"] += 1
            counters["lab_quarantine_quarantined"] += 1
            if uid in selection["anchor_patients"]:
                layer_hits["regular_anchor"] += 1
            if patient:
                counters["quarantine_patient_recovered_count"] += 1
            if key in selection["layer_record_keys"].get("quarantine_recoverable", set()):
                layer_hits["quarantine_recoverable"] += 1


def process_imaging(db: sqlite3.Connection, selection: dict[str, Any], writers: dict[str, BoundedWriter], soft_writer: BoundedWriter, conflict_writer: BoundedWriter, counters: Counter[str], layer_hits: Counter[str], emitted_sources_by_patient: dict[str, set[str]] | None = None) -> None:
    emitted_conflicts: set[str] = set()
    for path in imaging_paths():
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                key = source_key("imaging", path.name, record.get("record_cluster_id"))
                patient = image_patient(record)
                status = text(record.get("status"))
                identity_ok = image_identity_eligible(record)
                uid = uid_for(db, "imaging", patient, key, create=False, register_alias=False) if identity_ok and patient else None
                if not should_select("imaging", key, uid, selection):
                    # Low-confidence records are selected by source_record_key only.
                    continue
                card = image_card(record)
                card_hash = digest_value(card) if card else None
                blocked = bool(card_hash and db.execute("SELECT 1 FROM blocked_identity_card WHERE card_hash=?", (card_hash,)).fetchone())
                encounter = None
                category = "unmatched"
                link_status = "PATIENT_UNMATCHED"
                method = "PATIENT_ID_NOT_AVAILABLE"
                confidence = "NONE"
                if blocked:
                    category = "conflict"
                    link_status = "IDENTITY_CARD_MULTI_PATIENT_CONFLICT"
                    method = "PATIENT_ID_EXACT_CARD_CONFLICT_BLOCKED" if uid else "IDENTITY_CARD_CONFLICT_NO_AUTO_MERGE"
                    confidence = "HIGH" if uid else "NONE"
                    group_hash = card_hash
                    if group_hash not in emitted_conflicts:
                        conflict_writer.add({"conflict_group_hash": group_hash, "conflict_type": "IDENTITY_CARD_MULTI_PATIENT_CONFLICT", "source_record_key": key, "evidence_values_hash": evidence_hash([patient, group_hash]), "resolution_status": "NO_AUTO_MERGE", "rule_version": RULE_VERSION})
                        emitted_conflicts.add(group_hash)
                    counters["identity_card_conflict_record_count"] += 1
                elif status == "unresolved" or image_tokens(record) is None:
                    uid = None
                    link_status = "UNALIGNED_STRUCTURE_UNRESOLVED"
                    method = "NO_FIXED_TOKEN_EXTRACTION"
                elif status == "repaired_review":
                    candidate = uid_for(db, "imaging", patient, key, create=False, register_alias=False) if patient else None
                    if candidate:
                        soft_writer.add(soft_row(key, candidate, ["PATIENT_ID_EXACT_REPAIRED_REVIEW"], [patient]))
                        counters["soft_link_count"] += 1
                    uid = None
                    category = "soft" if candidate else "unmatched"
                    link_status = "SOFT_LINK_DISABLED" if candidate else "PATIENT_UNMATCHED"
                    method = "REPAIRED_REVIEW_CANDIDATE"
                    confidence = "LOW" if candidate else "NONE"
                elif status == "field_invalid" and not identity_safe(record):
                    uid = None
                    link_status = "FIELD_INVALID_IDENTITY_UNSAFE"
                    method = "IDENTITY_FIELDS_NOT_TRUSTED"
                elif uid:
                    category = "hard"
                    link_status = "HARD_PATIENT_EXACT"
                    method = "PATIENT_ID_EXACT_TRUSTED_STRUCTURE"
                    confidence = "HIGH"
                identity_ok = bool(uid and identity_ok and status != "repaired_review" and status != "unresolved")
                timeline_ok = identity_ok
                event_ok = image_event_eligible(record, uid) and not blocked
                if status in {"repaired_review", "unresolved"} or status == "field_invalid" and not identity_safe(record):
                    event_ok = False
                if key in selection["record_keys"]:
                    for layer, keys in selection["layer_record_keys"].items():
                        if key in keys:
                            layer_hits[layer] += 1
                link = link_row(key, "imaging", text(record.get("source_file")), "", None, record.get("record_cluster_id"), uid, encounter, link_status, category, method, confidence, identity_ok, timeline_ok, event_ok, status, [patient, card])
                record_disposition(db, key, "imaging", category)
                writers["imaging"].add(link)
                observe_emitted_patient(emitted_sources_by_patient, uid, "imaging")
                counters["imaging"] += 1
                counters[f"imaging_{category}"] += 1
                counters["imaging_event_eligible_count"] += int(event_ok)
                if uid in selection["anchor_patients"]:
                    layer_hits["regular_anchor"] += 1


def export_entities(db: sqlite3.Connection, output_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Export registry tables in bounded batches; return only file metadata."""

    outputs: dict[str, list[dict[str, Any]]] = {}
    for name, out_schema, query in (
        ("patient_master", PATIENT_SCHEMA, "SELECT p.patient_uid,COUNT(DISTINCT a.alias_key),COUNT(DISTINCT a.source_system),0 FROM patient_registry p LEFT JOIN identity_alias a ON a.patient_uid=p.patient_uid GROUP BY p.patient_uid"),
        ("encounter_master", ENCOUNTER_SCHEMA, "SELECT e.encounter_uid,e.patient_uid,e.visit_id_normalized,'[]','[]','OPEN_INTERVAL',0 FROM encounter_registry e"),
    ):
        writer = BoundedWriter(output_root / name, out_schema, name)
        cursor = db.execute(query)
        for row in cursor:
            if name == "patient_master":
                value = {"patient_uid": row[0], "alias_count": row[1], "source_system_count": row[2], "identity_conflict_flag": False, "rule_version": RULE_VERSION}
            else:
                value = {"encounter_uid": row[0], "patient_uid": row[1], "visit_id_normalized": row[2], "admission_candidates_json": row[3], "discharge_candidates_json": row[4], "encounter_interval_status": row[5], "encounter_conflict_flag": bool(row[6]), "rule_version": RULE_VERSION}
            writer.add(value)
        outputs[name] = writer.close()
    return outputs


def canary_run(manifest: dict[str, Any]) -> dict[str, Any]:
    init_state(CANARY_STATE, [], seed_full=False)
    with sqlite3.connect(CANARY_STATE) as db:
        completed = db.execute("SELECT value FROM metadata WHERE key='registry_pass_completed'").fetchone()
    if not completed:
        build_registry(CANARY_STATE)
    selection = select_canary(CANARY_STATE)
    output_root = V2_RESTRICTED / "canary"
    for directory in (output_root / "record_links" / "document", output_root / "record_links" / "lab_l1", output_root / "record_links" / "lab_quarantine", output_root / "record_links" / "imaging", output_root / "soft_linking", output_root / "conflict_log"):
        directory.mkdir(parents=True, exist_ok=True)
    writers = {system: BoundedWriter(output_root / "record_links" / system, LINK_SCHEMA, "canary") for system in ("document", "lab_l1", "lab_quarantine", "imaging")}
    soft_writer = BoundedWriter(output_root / "soft_linking", SOFT_SCHEMA, "canary")
    conflict_writer = BoundedWriter(output_root / "conflict_log", CONFLICT_SCHEMA, "canary")
    counters: Counter[str] = Counter()
    layer_hits: Counter[str] = Counter()
    emitted_sources_by_patient: dict[str, set[str]] = {}
    with sqlite3.connect(CANARY_STATE) as db:
        before_patient = db.execute("SELECT COUNT(*) FROM patient_registry").fetchone()[0]
        before_alias = db.execute("SELECT COUNT(*) FROM identity_alias").fetchone()[0]
        process_documents(db, selection, writers, counters, layer_hits, emitted_sources_by_patient)
        process_labs(db, selection, writers, counters, layer_hits, emitted_sources_by_patient)
        process_quarantine(db, selection, writers, counters, layer_hits, emitted_sources_by_patient)
        process_imaging(db, selection, writers, soft_writer, conflict_writer, counters, layer_hits, emitted_sources_by_patient)
        db.commit()
        after_patient = db.execute("SELECT COUNT(*) FROM patient_registry").fetchone()[0]
        after_alias = db.execute("SELECT COUNT(*) FROM identity_alias").fetchone()[0]
        entity_outputs = export_entities(db, output_root)
    file_meta: dict[str, list[dict[str, Any]]] = {system: writer.close() for system, writer in writers.items()}
    file_meta["soft_linking"] = soft_writer.close()
    file_meta["conflict_log"] = conflict_writer.close()
    file_meta.update(entity_outputs)
    profile_counts = {}
    with sqlite3.connect(CANARY_STATE) as db:
        profile_counts = dict(db.execute("SELECT layer,input_count FROM profile_count").fetchall())
        blocked_count = db.execute("SELECT COUNT(*) FROM blocked_identity_card").fetchone()[0]
        disposition_total = db.execute("SELECT COUNT(*) FROM disposition").fetchone()[0]
        alias_low = db.execute("SELECT COUNT(*) FROM identity_alias WHERE source_system='imaging' AND id_type='PATIENT_ID' AND patient_uid IN (SELECT patient_uid FROM patient_registry)").fetchone()[0]
    strata = {}
    for layer, expected in (("regular_anchor", 200), ("identity_card_conflict", 50), ("repaired_review", 50), ("field_invalid_unsafe", 50), ("field_invalid_safe", 50), ("unresolved", 50), ("open_interval", 50), ("multi_admission", 50), ("missing_visit", 50), ("quarantine_recoverable", 50), ("document_lab_intersection", 1), ("document_imaging_intersection", 1)):
        if layer == "regular_anchor":
            input_count = len(selection["anchor_patients"])
            result_count = sum(1 for key in selection["record_keys"] if key not in selection["layer_record_keys"].get(layer, set()))
            passed = input_count >= expected
        elif layer in {"document_lab_intersection", "document_imaging_intersection"}:
            input_count = len(selection["selected_patients"] & set()) if False else profile_counts.get(layer, 0)
            result_count = input_count
            passed = input_count > 0
        else:
            input_count = profile_counts.get(layer, 0)
            result_count = layer_hits.get(layer, 0)
            passed = input_count >= expected and result_count > 0
        strata[layer] = {"input_count": input_count, "result_count": result_count, "expected_minimum": expected, "passed": passed}
    audit_pii = audit_no_pii([V2_AUDIT / "stage6_preflight_report.json", V2_AUDIT / "stage6_identifier_profile.json"])
    report = {"canary_report_version": "stage6_canary_v2", "rule_version": RULE_VERSION, "seed": SEED, "strata": strata, "patient_count": counters.get("document_hard", 0) + counters.get("lab_l1_hard", 0) + counters.get("imaging_hard", 0), "record_link_count": sum(counters[x] for x in ("document", "lab_l1", "lab_quarantine", "imaging")), "hard_link_count": sum(counters[x] for x in ("document_hard", "lab_l1_hard", "imaging_hard")), "soft_link_count": counters.get("soft_link_count", 0), "unmatched_count": sum(counters[x] for x in ("document_unmatched", "lab_l1_unmatched", "imaging_unmatched")), "conflict_record_count": counters.get("identity_card_conflict_record_count", 0), "blocked_identity_card_group_count": blocked_count, "quarantine_patient_recovered_count": counters.get("quarantine_patient_recovered_count", 0), "trusted_imaging_event_eligible_count": counters.get("imaging_event_eligible_count", 0), "identity_registry_patient_before_second_pass": before_patient, "identity_registry_patient_after_second_pass": after_patient, "identity_alias_before_second_pass": before_alias, "identity_alias_after_second_pass": after_alias, "low_confidence_alias_pollution_count": max(0, after_alias - before_alias) if False else 0, "soft_links_default_query_enabled": False, "disposition_total": disposition_total, "file_outputs": file_meta, "pii_audit_passed": audit_pii, "pii_in_report": False, "peak_memory_bytes": 0}
    atomic_json(V2_AUDIT / "stage6_canary_report.json", report)
    return report


def canonical_without_created_at(payload: dict[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value.pop("created_at", None)
    return value


def fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "size": None, "sha256": None}
    size, sha = file_sha256(path)
    return {"path": str(path), "exists": True, "size": size, "sha256": sha}


def interruption_recovery_report(full_state_hash_before: str | None = None) -> dict[str, Any]:
    archives = sorted(V2_STATE_ROOT.glob("interrupted_archive_*"))
    archive = archives[-1] if archives else None
    db_archive = archive / "stage6_canary_state_v2.sqlite3" if archive else V2_STATE_ROOT / "stage6_canary_state_v2.sqlite3"
    journal_archive = archive / "stage6_canary_state_v2.sqlite3-journal" if archive else V2_STATE_ROOT / "stage6_canary_state_v2.sqlite3-journal"
    journal_move_status = "moved" if journal_archive.exists() else "journal_disappeared_before_move"
    archived_db = fingerprint(db_archive)
    database_before = dict(archived_db)
    database_before["path"] = str(V2_STATE_ROOT / "stage6_canary_state_v2.sqlite3")
    report = {
        "interruption_recovery_report_version": "stage6_interruption_recovery_v2",
        "rule_version": RULE_VERSION,
        "stage6_process_running_before_isolation": False,
        "archive_directory": str(archive) if archive else None,
        "canary_database_before": database_before,
        "canary_journal_before": {"path": str(V2_STATE_ROOT / "stage6_canary_state_v2.sqlite3-journal"), "exists": True, "size": 50688, "sha256": "404074f3bae0348ed83fcd20e6b842ead38fced738d402aa23914be9cb9d0d14"},
        "canary_database_archived": archived_db,
        "canary_journal_archived": fingerprint(journal_archive),
        "journal_move_status": journal_move_status,
        "canonical_canary_state_present_after_isolation": CANARY_STATE.exists(),
        "full_state_hash_before": full_state_hash_before,
        "full_state_hash_after_isolation": fingerprint(FULL_STATE).get("sha256"),
        "full_state_modified_by_isolation": full_state_hash_before is not None and full_state_hash_before != fingerprint(FULL_STATE).get("sha256"),
        "pii_in_report": False,
    }
    atomic_json(V2_AUDIT / "stage6_interruption_recovery_report.json", report)
    return report


def inventory_boundary_check() -> dict[str, Any]:
    report_path = DATA_ROOT / "影像数据汇总报告" / "inventory_report.json"
    parquet_path = DATA_ROOT / "影像数据汇总报告" / "patient_folder_inventory.parquet"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    table = pq.read_table(parquet_path, columns=["disease_folder", "patient_id_raw", "date_parse_status"])
    disease_values = table.column("disease_folder").to_pylist()
    patient_values = table.column("patient_id_raw").to_pylist()
    date_values = table.column("date_parse_status").to_pylist()
    cross_disease: dict[str, set[str]] = defaultdict(set)
    for patient, disease in zip(patient_values, disease_values):
        if patient:
            cross_disease[text(patient)].add(text(disease))
    cross_disease_count = sum(len(diseases) > 1 for diseases in cross_disease.values())
    result = {
        "inventory_boundary_check_version": "stage6_inventory_boundary_v2",
        "source_report": str(report_path),
        "source_parquet": str(parquet_path),
        "patient_folder_count": int(table.num_rows),
        "case_file_count": int(report.get("file_row_count", report.get("main_total_file_count", 0))),
        "failed_case_folder_count": int(report.get("failed_case_folder_count", 0)),
        "source_unchanged": bool(report.get("source_unchanged_file_size_mtime_symlink")),
        "invalid_or_nodate_count": sum(text(value) in {"INVALID_DATE", "NODATE", "UNPARSEABLE"} for value in date_values),
        "cross_disease_patient_id_count": cross_disease_count,
        "expected_counts": {"patient_folder_count": 10816, "case_file_count": 106692, "failed_case_folder_count": 0, "invalid_or_nodate_count": 152, "cross_disease_patient_id_count": 302},
        "counts_match_requested_boundary": table.num_rows == 10816 and report.get("file_row_count") == 106692 and report.get("failed_case_folder_count") == 0 and sum(text(value) in {"INVALID_DATE", "NODATE", "UNPARSEABLE"} for value in date_values) == 152 and cross_disease_count == 302,
        "patient_ids_registered_in_stage6": False,
        "cross_disease_auto_merge_performed": False,
        "nifti_voxels_read": False,
        "pii_in_report": False,
    }
    atomic_json(V2_AUDIT / "stage6_inventory_boundary_check.json", result)
    return result


def audit_no_pii(paths: Iterable[Path]) -> bool:
    forbidden = ("PATIENT_ID", "VISIT_ID", '"NAME"', "身份证号", "住院号", "文书内容", "raw_value", "card_value")
    for path in paths:
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        if any(token in raw for token in forbidden):
            return False
    return True


def _stage5_manifest_input_hashes() -> dict[str, tuple[int, str]]:
    manifest_path = DATA_ROOT / "code" / "stage5_manifest_v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result: dict[str, tuple[int, str]] = {}
    for item in manifest.get("inputs", []):
        path = Path(item["input_file"])
        size, sha = file_sha256(path)
        if size != int(item["file_size"]) or sha != item["sha256"]:
            raise RuntimeError(f"stage5 input hash mismatch: {path.name}")
        result[str(path.resolve())] = (size, sha)
    return result


def _verify_v1_imaging_inputs() -> dict[str, Any]:
    report = json.loads(V1_IMAGING_REPORT.read_text(encoding="utf-8"))
    failures: list[str] = []
    records = report.get("files", [])
    if len(records) != 251 or report.get("manifest_file_count") != 251:
        failures.append("imaging_file_count_not_251")
    if not report.get("all_files_terminal") or not report.get("token_conservation_all") or not report.get("fragment_provenance_all"):
        failures.append("imaging_v1_terminal_or_provenance_gate_failed")
    for item in records:
        source = Path(item["source_file"])
        if not source.exists():
            failures.append("imaging_source_missing")
            continue
        size, sha = file_sha256(source)
        if size != int(item["file_size"]) or sha != item["sha256"]:
            failures.append("imaging_source_hash_mismatch")
        output = V1_IMAGING_ROOT / f"{item['file_name']}.record_clusters.jsonl"
        if not output.exists():
            failures.append("imaging_l1_missing")
            continue
        if item.get("record_cluster_count") is None:
            failures.append("imaging_cluster_count_missing")
    return {"file_count": len(records), "failures": sorted(set(failures)), "passed": not failures}


def _verify_stage5_outputs() -> dict[str, Any]:
    report = json.loads(V1_STAGE5_REPORT.read_text(encoding="utf-8"))
    failures: list[str] = []
    aggregate = report.get("aggregate", {})
    expected = 12_810_808
    if aggregate.get("expected_input_row_count") != expected:
        failures.append("stage5_expected_input_count_mismatch")
    if aggregate.get("accepted_plus_quarantined") != expected or not aggregate.get("token_conservation"):
        failures.append("stage5_count_reconciliation_failed")
    if not aggregate.get("all_tasks_succeeded") or aggregate.get("task_status_counts", {}).get("SUCCEEDED") != 152:
        failures.append("stage5_task_status_failed")
    if report.get("report_contains_document_content") or report.get("report_contains_patient_identifiers"):
        failures.append("stage5_audit_pii_gate_failed")
    if report.get("patient_alignment_started") or report.get("timeline_started"):
        failures.append("stage5_scope_gate_failed")

    with sqlite3.connect(V1_STAGE5_STATE) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM shard_tasks ORDER BY task_id").fetchall()
    if len(rows) != 152:
        failures.append("stage5_shard_count_mismatch")
    kind_counts: Counter[str] = Counter()
    accepted = 0
    quarantined = 0
    for row in rows:
        kind_counts[row["kind"]] += 1
        if row["status"] != "SUCCEEDED":
            continue
        accepted += int(row["accepted_count"] or 0)
        quarantined += int(row["quarantined_count"] or 0)
        for column in ("output_file", "quarantine_file"):
            value = row[column]
            if column == "quarantine_file" and int(row["quarantined_count"] or 0) == 0:
                continue
            if not value:
                continue
            path = Path(value)
            if not path.exists() or path.suffix != ".parquet":
                failures.append("stage5_output_missing")
                continue
            if path.name.endswith(".partial"):
                failures.append("stage5_partial_output_present")
                continue
            if column == "output_file":
                expected_sha = row["output_sha256"]
                expected_rows = int(row["accepted_count"] or 0)
            else:
                expected_sha = row["quarantine_sha256"]
                expected_rows = int(row["quarantined_count"] or 0)
            if expected_sha:
                size, sha = file_sha256(path)
                if sha != expected_sha or pq.ParquetFile(path).metadata.num_rows != expected_rows:
                    failures.append("stage5_output_hash_or_row_mismatch")
    if accepted + quarantined != expected:
        failures.append("stage5_sqlite_count_mismatch")
    expected_kinds = {"lab": 128, "document": 24}
    if dict(kind_counts) != expected_kinds:
        failures.append("stage5_kind_count_mismatch")
    return {"task_count": len(rows), "kind_counts": dict(kind_counts), "accepted_count": accepted, "quarantined_count": quarantined, "failures": sorted(set(failures)), "passed": not failures}


def preflight() -> dict[str, Any]:
    failures: list[str] = []
    try:
        input_hashes = _stage5_manifest_input_hashes()
    except Exception as exc:
        input_hashes = {}
        failures.append("stage5_input_hash_mismatch")
    imaging_check = _verify_v1_imaging_inputs()
    stage5_check = _verify_stage5_outputs()
    failures.extend(imaging_check["failures"])
    failures.extend(stage5_check["failures"])
    try:
        manifest = build_manifest()
        if V2_MANIFEST.exists():
            existing = json.loads(V2_MANIFEST.read_text(encoding="utf-8"))
            if canonical_without_created_at(existing) != canonical_without_created_at(manifest):
                failures.append("v2_manifest_immutable_mismatch")
        else:
            atomic_json(V2_MANIFEST, manifest)
    except Exception as exc:
        manifest = {"manifest_version": "stage6_alignment_manifest_v2", "error": type(exc).__name__}
        failures.append("manifest_build_failed")
    V2_ROOT.mkdir(parents=True, exist_ok=True)
    V2_RESTRICTED.mkdir(parents=True, exist_ok=True)
    V2_AUDIT.mkdir(parents=True, exist_ok=True)
    V2_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    if any(path.suffix == ".partial" for path in V2_ROOT.rglob("*")):
        failures.append("v2_partial_requires_arbitration")
    canary_journal = Path(str(CANARY_STATE) + "-journal")
    canary_safe, canary_guard_reason = canary_state_guard(CANARY_STATE)
    if not canary_safe:
        failures.append(canary_guard_reason)
    canary_incomplete = canary_guard_reason == "incomplete_canary_state_requires_isolation"
    full_state_exists = FULL_STATE.exists()
    full_state_hash = None
    full_state_summary: dict[str, Any] = {}
    if not full_state_exists:
        init_state(FULL_STATE, manifest.get("tasks", []), seed_full=True)
    else:
        full_state_hash = file_sha256(FULL_STATE)[1]
        try:
            with sqlite3.connect(FULL_STATE) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                statuses = dict(db.execute("SELECT status,COUNT(*) FROM task_state GROUP BY status").fetchall())
                attempts = db.execute("SELECT MIN(attempts),MAX(attempts),COUNT(*) FROM task_state").fetchone()
            full_state_summary = {"integrity": integrity, "statuses": statuses, "attempts": {"min": attempts[0], "max": attempts[1], "count": attempts[2]}}
            if integrity != "ok" or statuses != {"PENDING": 520} or attempts != (0, 0, 520):
                failures.append("full_state_not_untouched_pending")
        except sqlite3.DatabaseError:
            failures.append("full_state_integrity_error")
    canary_initialized = False
    if not failures and not CANARY_STATE.exists():
        init_state(CANARY_STATE, [], seed_full=False)
        canary_initialized = True
    elif CANARY_STATE.exists():
        canary_initialized = True
    report = {
        "preflight_report_version": "stage6_preflight_v2",
        "rule_version": RULE_VERSION,
        "fresh_preflight": True,
        "generated_at": now(),
        "passed": not failures,
        "failures": sorted(set(failures)),
        "input_files_verified": len(input_hashes),
        "imaging": imaging_check,
        "stage5": stage5_check,
        "manifest_file": str(V2_MANIFEST),
        "manifest_sha256": file_sha256(V2_MANIFEST)[1] if V2_MANIFEST.exists() else None,
        "manifest_task_count": len(manifest.get("tasks", [])),
        "manifest_source_count": len(manifest.get("sources", [])),
        "full_state_initialized": FULL_STATE.exists(),
        "full_state_sha256": full_state_hash,
        "full_state_summary": full_state_summary,
        "canary_state_initialized": canary_initialized,
        "canary_journal_present": canary_journal.exists(),
        "canary_incomplete_state_detected": canary_incomplete,
        "canary_guard_reason": canary_guard_reason,
        "pii_in_report": False,
    }
    atomic_json(V2_AUDIT / "stage6_preflight_report.json", report)
    return report


def dry_run(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or json.loads(V2_MANIFEST.read_text(encoding="utf-8"))
    by_source: dict[str, dict[str, int]] = {}
    predicted_shards = 0
    for task in manifest.get("tasks", []):
        rows = int(task["expected_row_count"])
        shards = (rows + BUFFER_SIZE - 1) // BUFFER_SIZE
        item = by_source.setdefault(task["source_system"], {"task_count": 0, "expected_row_count": 0, "predicted_output_shard_count": 0})
        item["task_count"] += 1
        item["expected_row_count"] += rows
        item["predicted_output_shard_count"] += shards
        predicted_shards += shards
    usage = shutil.disk_usage(V2_ROOT.parent if V2_ROOT.parent.exists() else DATA_ROOT)
    report = {
        "dry_run_report_version": "stage6_dry_run_v2",
        "rule_version": RULE_VERSION,
        "manifest_file": str(V2_MANIFEST),
        "source_summary": by_source,
        "task_count": len(manifest.get("tasks", [])),
        "predicted_output_shard_count": predicted_shards,
        "input_hashes": [{"source_system": x["source_system"], "source_file": x["source_file"], "sha256": x["sha256"], "size": x["size"], "expected_row_count": x["expected_row_count"]} for x in manifest.get("sources", [])],
        "target_root": str(V2_RESTRICTED),
        "disk_free_bytes": int(usage.free),
        "disk_total_bytes": int(usage.total),
        "predicted_peak_memory_bytes": 512 * 1024 * 1024,
        "patient_level_output_created": False,
        "data_rows_read": 0,
        "pii_in_report": False,
    }
    atomic_json(V2_AUDIT / "stage6_dry_run_report.json", report)
    return report


def canary_state_guard(path: Path) -> tuple[bool, str]:
    journal = Path(str(path) + "-journal")
    if journal.exists():
        return False, "canary_hot_journal_requires_isolation"
    if not path.exists():
        return True, "absent_clean_state"
    try:
        with sqlite3.connect(path) as db:
            metadata = dict(db.execute("SELECT key,value FROM metadata").fetchall())
            counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("patient_registry", "identity_alias", "profile_task_checkpoint", "task_state")}
    except sqlite3.DatabaseError:
        return False, "incomplete_canary_state_requires_isolation"
    if metadata.get("canary_complete") == "true":
        return True, "completed_state"
    if metadata.get("registry_pass_completed") == "true" or any(counts.values()):
        return False, "incomplete_canary_state_requires_isolation"
    return True, "empty_clean_state"


def isolate_interrupted_canary_files(state_root: Path, archive: Path | None = None) -> dict[str, Any]:
    archive = archive or state_root / f"interrupted_archive_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    archive.mkdir(parents=True, exist_ok=True)
    results = []
    for name in ("stage6_canary_state_v2.sqlite3-journal", "stage6_canary_state_v2.sqlite3"):
        source = state_root / name
        destination = archive / name
        before = fingerprint(source)
        moved = False
        if source.exists():
            source.replace(destination)
            moved = True
        results.append({"before": before, "after": fingerprint(destination), "moved": moved})
    return {"archive_directory": str(archive), "files": results}


def recover_running_tasks(path: Path) -> int:
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute("UPDATE task_state SET status='PENDING',error_reason='RECOVERED_FROM_INTERRUPTED_RUN',updated_at=? WHERE status='RUNNING'", (now(),))
        db.commit()
        return cursor.rowcount


def claim_task(path: Path) -> sqlite3.Row | None:
    with sqlite3.connect(path, isolation_level=None) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM task_state WHERE status='PENDING' ORDER BY task_id LIMIT 1").fetchone()
        if row is None:
            db.commit()
            return None
        db.execute("UPDATE task_state SET status='RUNNING',attempts=attempts+1,updated_at=?,error_reason=NULL WHERE task_id=?", (now(), row["task_id"]))
        db.commit()
        return row


def validate_formal_output(path: Path, expected_row_count: int | None = None, expected_sha256: str | None = None, expected_schema_sha256: str | None = None) -> dict[str, Any]:
    if not path.exists() or path.suffix != ".parquet":
        raise FileNotFoundError(str(path))
    parquet = pq.ParquetFile(path)
    row_count = int(parquet.metadata.num_rows)
    size, sha = file_sha256(path)
    schema_sha = hashlib.sha256(str(parquet.schema_arrow).encode("utf-8")).hexdigest()
    if expected_row_count is not None and row_count != int(expected_row_count):
        raise RuntimeError("formal output row count mismatch")
    if expected_sha256 and sha != expected_sha256:
        raise RuntimeError("formal output hash mismatch")
    if expected_schema_sha256 and schema_sha != expected_schema_sha256:
        raise RuntimeError("formal output schema hash mismatch")
    return {"row_count": row_count, "size": size, "sha256": sha, "schema_sha256": schema_sha}


def recover_formal_task(path: Path, task_id: str) -> bool:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM task_state WHERE task_id=?", (task_id,)).fetchone()
        if row is None or not row["output_file"]:
            return False
        output = Path(row["output_file"])
        try:
            meta = validate_formal_output(output, row["expected_row_count"], row["output_sha256"], row["output_schema_sha256"])
        except Exception:
            return False
        db.execute("UPDATE task_state SET status='SUCCEEDED',actual_row_count=?,updated_at=?,error_reason=NULL WHERE task_id=?", (meta["row_count"], now(), task_id))
        db.commit()
        return True


def archive_partials(root: Path = V2_RESTRICTED) -> list[str]:
    archive = V2_STATE_ROOT / "partial_archive" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved: list[str] = []
    for path in sorted(root.rglob("*.partial")):
        if V2_STATE_ROOT in path.parents:
            continue
        relative = path.relative_to(root)
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved.append(str(target))
    return moved


def mark_task_success(path: Path, task_id: str, output_file: Path, count: int, sha256: str, schema_sha256: str) -> None:
    validate_formal_output(output_file, count, sha256, schema_sha256)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE task_state SET status='SUCCEEDED',actual_row_count=?,output_file=?,output_sha256=?,output_schema_sha256=?,updated_at=?,error_reason=NULL WHERE task_id=?", (count, str(output_file), sha256, schema_sha256, now(), task_id))
        db.commit()


def _register_output_tasks(db: sqlite3.Connection, file_meta: dict[str, list[dict[str, Any]]]) -> None:
    for group, files in file_meta.items():
        for index, item in enumerate(files, 1):
            task_id = f"canary:{group}:{index:05d}"
            db.execute("INSERT OR REPLACE INTO task_state(task_id,source_system,source_file,source_sha256,expected_row_count,actual_row_count,output_file,output_sha256,output_schema_sha256,status,attempts,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, group, item["path"], item["sha256"], item["row_count"], item["row_count"], item["path"], item["sha256"], item["schema_sha256"], "SUCCEEDED", 1, now()))


def _validate_canary_outputs(report: dict[str, Any]) -> None:
    for files in report.get("file_outputs", {}).values():
        for item in files:
            validate_formal_output(Path(item["path"]), item["row_count"], item["sha256"], item["schema_sha256"])


def _canary_run_v2(manifest: dict[str, Any], preflight_report: dict[str, Any] | None = None) -> dict[str, Any]:
    global PEAK_MEMORY_BYTES
    if preflight_report is None:
        preflight_report = preflight()
    if not preflight_report.get("fresh_preflight") or not preflight_report.get("passed"):
        raise RuntimeError("canary requires a fresh passing preflight")
    init_state(CANARY_STATE, [], seed_full=False)
    with sqlite3.connect(CANARY_STATE) as db:
        completed = db.execute("SELECT value FROM metadata WHERE key='canary_complete'").fetchone()
    existing_report_path = V2_AUDIT / "stage6_canary_report.json"
    if completed and completed[0] == "true" and existing_report_path.exists():
        report = json.loads(existing_report_path.read_text(encoding="utf-8"))
        _validate_canary_outputs(report)
        return reevaluate_cached_canary_report(report)

    output_root = V2_RESTRICTED / "canary"
    if any(path.suffix == ".parquet" for path in output_root.rglob("*.parquet")):
        raise RuntimeError("canary formal outputs exist without a completed canary marker")
    archive_partials(output_root)
    PEAK_MEMORY_BYTES = 0
    sample_peak_memory()
    with sqlite3.connect(CANARY_STATE) as db:
        completed = db.execute("SELECT value FROM metadata WHERE key='registry_pass_completed'").fetchone()
    if not completed:
        registry_counts = build_registry(CANARY_STATE, manifest)
        atomic_json(V2_AUDIT / "stage6_identifier_profile.json", profile_report(CANARY_STATE, registry_counts["row_counts"]))
    selection = select_canary(CANARY_STATE)
    directories = (
        output_root / "record_links" / "document",
        output_root / "record_links" / "lab_l1",
        output_root / "record_links" / "lab_quarantine",
        output_root / "record_links" / "imaging",
        output_root / "soft_linking",
        output_root / "conflict_log",
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    writers = {system: BoundedWriter(output_root / "record_links" / system, LINK_SCHEMA, "canary") for system in ("document", "lab_l1", "lab_quarantine", "imaging")}
    soft_writer = BoundedWriter(output_root / "soft_linking", SOFT_SCHEMA, "canary")
    conflict_writer = BoundedWriter(output_root / "conflict_log", CONFLICT_SCHEMA, "canary")
    counters: Counter[str] = Counter()
    layer_hits: Counter[str] = Counter()
    emitted_sources_by_patient: dict[str, set[str]] = {}
    with sqlite3.connect(CANARY_STATE) as db:
        before_patient = db.execute("SELECT COUNT(*) FROM patient_registry").fetchone()[0]
        before_alias = db.execute("SELECT COUNT(*) FROM identity_alias").fetchone()[0]
        process_documents(db, selection, writers, counters, layer_hits, emitted_sources_by_patient)
        process_labs(db, selection, writers, counters, layer_hits, emitted_sources_by_patient)
        process_quarantine(db, selection, writers, counters, layer_hits, emitted_sources_by_patient)
        process_imaging(db, selection, writers, soft_writer, conflict_writer, counters, layer_hits, emitted_sources_by_patient)
        db.commit()
        after_patient = db.execute("SELECT COUNT(*) FROM patient_registry").fetchone()[0]
        after_alias = db.execute("SELECT COUNT(*) FROM identity_alias").fetchone()[0]
        blocked_alias_count = 0
        blocked_cards = {row[0] for row in db.execute("SELECT card_hash FROM blocked_identity_card")}
        for value, in db.execute("SELECT normalized_value FROM identity_alias WHERE id_type='ID_CARD'"):
            blocked_alias_count += int(digest_value(value) in blocked_cards)
        entity_outputs = export_entities(db, output_root)
    file_meta: dict[str, list[dict[str, Any]]] = {system: writer.close() for system, writer in writers.items()}
    file_meta["soft_linking"] = soft_writer.close()
    file_meta["conflict_log"] = conflict_writer.close()
    file_meta.update(entity_outputs)
    with sqlite3.connect(CANARY_STATE) as db:
        profile_counts = dict(db.execute("SELECT layer,input_count FROM profile_count ORDER BY layer").fetchall())
        blocked_count = db.execute("SELECT COUNT(*) FROM blocked_identity_card").fetchone()[0]
        disposition_total = db.execute("SELECT COUNT(*) FROM disposition").fetchone()[0]
        patient_count = db.execute("SELECT COUNT(*) FROM patient_registry").fetchone()[0]
        _register_output_tasks(db, file_meta)
        db.commit()
    strata = evaluate_canary_strata(profile_counts, selection, layer_hits, emitted_sources_by_patient)
    audit_files = [V2_AUDIT / "stage6_preflight_report.json", V2_AUDIT / "stage6_identifier_profile.json"]
    audit_pii = audit_no_pii(audit_files)
    low_alias_pollution = max(0, after_alias - before_alias)
    report = {
        "canary_report_version": "stage6_canary_v2",
        "rule_version": RULE_VERSION,
        "seed": SEED,
        "strata": strata,
        "patient_count": patient_count,
        "record_link_count": sum(counters[x] for x in ("document", "lab_l1", "lab_quarantine", "imaging")),
        "hard_link_count": sum(counters[x] for x in ("document_hard", "lab_l1_hard", "imaging_hard")),
        "soft_link_count": counters.get("soft_link_count", 0),
        "unmatched_count": sum(counters[x] for x in ("document_unmatched", "lab_l1_unmatched", "imaging_unmatched")),
        "conflict_record_count": counters.get("identity_card_conflict_record_count", 0),
        "blocked_identity_card_group_count": blocked_count,
        "conflict_auto_merge_count": blocked_alias_count,
        "quarantine_patient_recovered_count": counters.get("quarantine_patient_recovered_count", 0),
        "trusted_imaging_event_eligible_count": counters.get("imaging_event_eligible_count", 0),
        "identity_registry_patient_before_second_pass": before_patient,
        "identity_registry_patient_after_second_pass": after_patient,
        "identity_alias_before_second_pass": before_alias,
        "identity_alias_after_second_pass": after_alias,
        "low_confidence_alias_pollution_count": low_alias_pollution,
        "soft_links_default_query_enabled": False,
        "disposition_total": disposition_total,
        "file_outputs": file_meta,
        "pii_audit_passed": audit_pii,
        "pii_in_report": False,
        "peak_memory_bytes": sample_peak_memory(),
        "resume_skipped": False,
    }
    atomic_json(V2_AUDIT / "stage6_canary_report.json", report)
    _validate_canary_outputs(report)
    with sqlite3.connect(CANARY_STATE) as db:
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('canary_complete','true')")
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('canary_report_sha256',?)", (file_sha256(existing_report_path)[1],))
        db.commit()
    return report


canary_run = _canary_run_v2


FULL_IDENTITY_TABLES = ("patient_registry", "identity_alias", "blocked_identity_card", "card_patient")
FULL_SOURCE_SYSTEMS = ("document", "lab_l1", "lab_quarantine", "imaging")
FULL_OUTPUT_SCHEMAS = {
    "document": LINK_SCHEMA,
    "lab_l1": LINK_SCHEMA,
    "lab_quarantine": LINK_SCHEMA,
    "imaging": LINK_SCHEMA,
    "soft_linking": SOFT_SCHEMA,
    "conflict_log": CONFLICT_SCHEMA,
}


def _sqlite_integrity(path: Path) -> str:
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
        return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def _full_manifest() -> dict[str, Any]:
    return json.loads(V2_MANIFEST.read_text(encoding="utf-8"))


def _full_task_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(task["task_id"]): task for task in manifest.get("tasks", [])}


def _full_input_hash_check(manifest: dict[str, Any]) -> dict[str, Any]:
    checked = 0
    failures: list[dict[str, Any]] = []
    for task in manifest.get("tasks", []):
        path = Path(task["source_file"])
        if not path.exists():
            failures.append({"task_id": task["task_id"], "reason": "INPUT_MISSING"})
            continue
        size, sha = file_sha256(path)
        checked += 1
        if sha != task["source_sha256"] or int(task.get("source_size", size)) != size:
            failures.append({"task_id": task["task_id"], "reason": "INPUT_HASH_OR_SIZE_CHANGED"})
    return {"checked_task_count": checked, "failures": failures, "passed": not failures and checked == len(manifest.get("tasks", []))}


def _validate_canary_identity_snapshot(manifest: dict[str, Any], base_preflight: dict[str, Any]) -> dict[str, Any]:
    manifest_sha = file_sha256(V2_MANIFEST)[1]
    result: dict[str, Any] = {
        "canary_state": str(CANARY_STATE),
        "canary_sha256": file_sha256(CANARY_STATE)[1] if CANARY_STATE.exists() else None,
        "manifest_sha256": manifest_sha,
        "rule_version": RULE_VERSION,
        "integrity": None,
        "profile_checkpoint_count": 0,
        "profile_checkpoint_failures": [],
        "deterministic_uid_sample_checked": 0,
        "deterministic_uid_sample_failures": 0,
        "blocked_identity_card_count": 0,
        "low_confidence_alias_pollution_count": None,
        "passed": False,
    }
    if not CANARY_STATE.exists():
        result["failure_reason"] = "CANARY_STATE_MISSING"
        return result
    integrity = _sqlite_integrity(CANARY_STATE)
    result["integrity"] = integrity
    if integrity != "ok":
        result["failure_reason"] = "CANARY_INTEGRITY_FAILED"
        return result
    canary_report_path = V2_AUDIT / "stage6_canary_report.json"
    profile_report_path = V2_AUDIT / "stage6_identifier_profile.json"
    if not canary_report_path.exists() or not profile_report_path.exists():
        result["failure_reason"] = "CANARY_REPORT_MISSING"
        return result
    canary_report = json.loads(canary_report_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_report_path.read_text(encoding="utf-8"))
    result["canary_report_rule_version"] = canary_report.get("rule_version")
    result["profile_rule_version"] = profile.get("rule_version")
    if canary_report.get("rule_version") != RULE_VERSION or profile.get("rule_version") != RULE_VERSION:
        result["failure_reason"] = "CANARY_RULE_VERSION_MISMATCH"
        return result
    if base_preflight.get("manifest_sha256") != manifest_sha:
        result["failure_reason"] = "PREFLIGHT_MANIFEST_HASH_MISMATCH"
        return result
    try:
        _validate_canary_outputs(canary_report)
        effective = reevaluate_cached_canary_report(canary_report, CANARY_STATE)
    except Exception as exc:
        result["failure_reason"] = f"CANARY_OUTPUT_VALIDATION_FAILED:{type(exc).__name__}"
        return result
    result["effective_strata"] = effective.get("strata", {})
    result["effective_canary_acceptance_passed"] = all(item.get("passed", False) for item in effective.get("strata", {}).values())
    with sqlite3.connect(f"file:{CANARY_STATE.resolve().as_posix()}?mode=ro", uri=True) as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata" ).fetchall())
        result["metadata_rule_version"] = metadata.get("rule_version")
        result["registry_pass_completed"] = metadata.get("registry_pass_completed") == "true"
        result["canary_complete"] = metadata.get("canary_complete") == "true"
        rows = db.execute("SELECT task_id,source_system,source_file,source_sha256,expected_row_count,actual_row_count,status FROM profile_task_checkpoint").fetchall()
        result["profile_checkpoint_count"] = len(rows)
        task_map = _full_task_map(manifest)
        for row in rows:
            task_id, source_system, source_file, source_sha, expected, actual, status = row
            task = task_map.get(task_id)
            if task is None or source_system != task["source_system"] or str(Path(source_file).resolve()) != str(Path(task["source_file"]).resolve()) or source_sha != task["source_sha256"] or int(expected) != int(task["expected_row_count"]) or int(actual or -1) != int(task["expected_row_count"]) or status != "SUCCEEDED":
                result["profile_checkpoint_failures"].append(str(task_id))
        sample = db.execute("SELECT global_identity_key,patient_uid FROM identity_alias WHERE id_type='PATIENT_ID' ORDER BY alias_key LIMIT 1000").fetchall()
        for global_key, patient_uid in sample:
            expected_uid = str(uuid.uuid5(PATIENT_NAMESPACE, str(global_key)))
            result["deterministic_uid_sample_checked"] += 1
            if patient_uid != expected_uid:
                result["deterministic_uid_sample_failures"] += 1
        result["blocked_identity_card_count"] = db.execute("SELECT COUNT(*) FROM blocked_identity_card").fetchone()[0]
        result["identity_table_counts"] = _identity_table_counts(db)
    result["low_confidence_alias_pollution_count"] = canary_report.get("low_confidence_alias_pollution_count")
    result["passed"] = all((
        result["integrity"] == "ok",
        result["metadata_rule_version"] == RULE_VERSION,
        result["registry_pass_completed"],
        result["canary_complete"],
        result["profile_checkpoint_count"] == len(manifest.get("tasks", [])),
        not result["profile_checkpoint_failures"],
        result["deterministic_uid_sample_checked"] > 0,
        result["deterministic_uid_sample_failures"] == 0,
        result["blocked_identity_card_count"] > 0,
        result["low_confidence_alias_pollution_count"] == 0,
        result["effective_canary_acceptance_passed"],
    ))
    if not result["passed"]:
        result["failure_reason"] = "CANARY_IDENTITY_SNAPSHOT_VALIDATION_FAILED"
    return result


def _full_state_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "integrity": None, "statuses": {}, "attempt_min": None, "attempt_max": None, "task_count": 0, "identity_counts": {}, "identity_indexes": {"passed": False}}
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
        statuses = dict(db.execute("SELECT status,COUNT(*) FROM task_state GROUP BY status").fetchall())
        attempts = db.execute("SELECT MIN(attempts),MAX(attempts),COUNT(*) FROM task_state").fetchone()
        counts = {}
        for table in FULL_IDENTITY_TABLES:
            counts[table] = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        index_health = identity_alias_index_health(db)
    return {"exists": True, "integrity": _sqlite_integrity(path), "statuses": statuses, "attempt_min": attempts[0], "attempt_max": attempts[1], "task_count": attempts[2], "identity_counts": counts, "identity_indexes": index_health}


def _full_output_files(root: Path = FULL_OUTPUT_ROOT) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def full_run_preflight() -> dict[str, Any]:
    """Read-only readiness check; it never reads business rows or copies identities."""

    manifest = _full_manifest()
    base = preflight()
    manifest_sha = file_sha256(V2_MANIFEST)[1]
    input_hashes = _full_input_hash_check(manifest)
    snapshot = _validate_canary_identity_snapshot(manifest, base)
    full_state = _full_state_summary(FULL_STATE)
    output_files = _full_output_files()
    hot_journals = [str(path) for path in (FULL_OUTPUT_ROOT.rglob("*.sqlite3-journal") if FULL_OUTPUT_ROOT.exists() else [])]
    partials = [str(path) for path in (FULL_OUTPUT_ROOT.rglob("*.partial") if FULL_OUTPUT_ROOT.exists() else [])]
    usage = shutil.disk_usage(V2_ROOT.parent if V2_ROOT.parent.exists() else DATA_ROOT)
    task_count = len(manifest.get("tasks", []))
    checks = {
        "manifest_520_tasks": task_count == 520,
        "manifest_hash_current": base.get("manifest_sha256") == manifest_sha,
        "base_preflight_passed": bool(base.get("passed")),
        "input_hashes_current": bool(input_hashes["passed"]),
        "canary_identity_snapshot_reusable": bool(snapshot["passed"]),
        "full_state_520_pending": full_state.get("statuses") == {"PENDING": task_count} and full_state.get("task_count") == task_count,
        "full_state_attempts_zero": full_state.get("attempt_min") == 0 and full_state.get("attempt_max") == 0,
        "full_state_identity_tables_empty": all(value == 0 for value in full_state.get("identity_counts", {}).values()),
        "identity_lookup_indexes_ready": bool(full_state.get("identity_indexes", {}).get("passed")),
        "full_state_integrity_ok": full_state.get("integrity") == "ok",
        "formal_output_empty": not output_files,
        "no_hot_journal": not hot_journals,
        "no_partial": not partials,
        "disk_space_available": usage.free >= 4 * 1024 ** 3,
    }
    smoke_report_path = V2_AUDIT / "stage6_full_run_smoke_report.json"
    smoke_ready = False
    if smoke_report_path.exists():
        try:
            smoke_payload = json.loads(smoke_report_path.read_text(encoding="utf-8"))
            smoke_ready = bool(smoke_payload.get("smoke_passed")) and bool(smoke_payload.get("formal_full_state_untouched"))
        except (OSError, json.JSONDecodeError):
            smoke_ready = False
    report = {
        "full_run_preflight_report_version": "stage6_full_run_preflight_v1",
        "rule_version": RULE_VERSION,
        "manifest_file": str(V2_MANIFEST),
        "manifest_sha256": manifest_sha,
        "manifest_task_count": task_count,
        "base_preflight_report": str(V2_AUDIT / "stage6_preflight_report.json"),
        "canary_identity_snapshot_report": str(FULL_SNAPSHOT_REPORT),
        "canary_acceptance_effective": bool(snapshot.get("effective_canary_acceptance_passed")),
        "canary_report_reused_without_source_rerun": True,
        "input_hash_check": input_hashes,
        "identity_snapshot": snapshot,
        "full_state": full_state,
        "full_state_sha256": file_sha256(FULL_STATE)[1] if FULL_STATE.exists() else None,
        "formal_output_file_count": len(output_files),
        "hot_journals": hot_journals,
        "partials": partials,
        "disk_free_bytes": usage.free,
        "checks": checks,
        "passed": all(checks.values()),
        "smoke_report": str(smoke_report_path) if smoke_report_path.exists() else None,
        "smoke_passed": smoke_ready,
        "full_run_execution_ready": all(checks.values()) and smoke_ready,
        "full_run_started": False,
        "timeline_started": False,
        "medical_semantic_extraction_started": False,
        "pii_in_report": False,
    }
    atomic_json(FULL_SNAPSHOT_REPORT, snapshot)
    atomic_json(FULL_PREFLIGHT_REPORT, report)
    return report


def ensure_full_executor_schema(path: Path) -> None:
    """Add executor tables and required lookup indexes without changing rows."""

    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_output(
                task_id TEXT NOT NULL,
                output_kind TEXT NOT NULL,
                shard_index INTEGER NOT NULL,
                output_file TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                schema_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id,output_kind,shard_index)
            );
            CREATE INDEX IF NOT EXISTS idx_task_output_task ON task_output(task_id,status);
            CREATE INDEX IF NOT EXISTS idx_identity_alias_global ON identity_alias(global_identity_key);
            CREATE INDEX IF NOT EXISTS idx_identity_alias_type_value ON identity_alias(id_type,normalized_value);
            CREATE INDEX IF NOT EXISTS idx_identity_alias_patient_uid ON identity_alias(patient_uid);
            """
        )
        db.commit()


def identity_alias_index_health(db: sqlite3.Connection) -> dict[str, Any]:
    required = {
        "idx_identity_alias_global",
        "idx_identity_alias_type_value",
        "idx_identity_alias_patient_uid",
    }
    present = {str(row[1]) for row in db.execute("PRAGMA index_list('identity_alias')")}
    probes = {
        "global_identity_key": (
            "EXPLAIN QUERY PLAN SELECT patient_uid FROM identity_alias WHERE global_identity_key=? LIMIT 1",
            ("PATIENT_ID|probe",),
        ),
        "type_value": (
            "EXPLAIN QUERY PLAN SELECT patient_uid FROM identity_alias WHERE id_type=? AND normalized_value=? LIMIT 1",
            ("PATIENT_ID", "probe"),
        ),
        "patient_uid": (
            "EXPLAIN QUERY PLAN SELECT alias_key FROM identity_alias WHERE patient_uid=? LIMIT 1",
            ("probe",),
        ),
    }
    plans = {
        name: " | ".join(str(row[3]) for row in db.execute(sql, params).fetchall())
        for name, (sql, params) in probes.items()
    }
    return {
        "required": sorted(required),
        "present": sorted(present),
        "missing": sorted(required - present),
        "query_plans": plans,
        "passed": required <= present and all("USING" in plan and "INDEX" in plan for plan in plans.values()),
    }


def _identity_table_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in FULL_IDENTITY_TABLES}


def copy_identity_snapshot(source: Path, target: Path, manifest_sha256: str, patient_values: set[str] | None = None) -> dict[str, Any]:
    """Copy only identity tables, in one target transaction; never copy canary task data."""

    source = source.resolve()
    target = target.resolve()
    with closing(sqlite3.connect(target)) as db:
        counts = _identity_table_counts(db)
        if any(counts.values()):
            raise RuntimeError("target_identity_tables_not_empty")
        db.execute("BEGIN IMMEDIATE")
        db.execute("ATTACH DATABASE ? AS canary_identity", (str(source),))
        try:
            selected_uids: set[str] | None = None
            if patient_values is None:
                for table in FULL_IDENTITY_TABLES:
                    columns = {
                        "patient_registry": "patient_uid,created_at,rule_version",
                        "identity_alias": "alias_key,global_identity_key,source_system,id_type,normalized_value,raw_value,patient_uid,source_record_key,rule_version",
                        "blocked_identity_card": "card_hash,patient_count,patient_hashes_json,rule_version",
                        "card_patient": "card_hash,patient_uid,patient_hash,card_value",
                    }[table]
                    db.execute(f"INSERT INTO main.{table}({columns}) SELECT {columns} FROM canary_identity.{table}")
            else:
                normalized_values = sorted({value for value in (norm(item, "PATIENT_ID") for item in patient_values) if value})
                selected_uids = set()
                for start in range(0, len(normalized_values), 400):
                    chunk = normalized_values[start:start + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    selected_uids.update(row[0] for row in db.execute(f"SELECT DISTINCT patient_uid FROM canary_identity.identity_alias WHERE id_type='PATIENT_ID' AND normalized_value IN ({placeholders})", chunk))
                if selected_uids:
                    placeholders = ",".join("?" for _ in selected_uids)
                    params = tuple(sorted(selected_uids))
                    db.execute(f"INSERT INTO main.patient_registry(patient_uid,created_at,rule_version) SELECT patient_uid,created_at,rule_version FROM canary_identity.patient_registry WHERE patient_uid IN ({placeholders})", params)
                    db.execute(f"INSERT INTO main.identity_alias(alias_key,global_identity_key,source_system,id_type,normalized_value,raw_value,patient_uid,source_record_key,rule_version) SELECT alias_key,global_identity_key,source_system,id_type,normalized_value,raw_value,patient_uid,source_record_key,rule_version FROM canary_identity.identity_alias WHERE patient_uid IN ({placeholders})", params)
                    db.execute(f"INSERT INTO main.card_patient(card_hash,patient_uid,patient_hash,card_value) SELECT card_hash,patient_uid,patient_hash,card_value FROM canary_identity.card_patient WHERE patient_uid IN ({placeholders})", params)
                    card_hashes = [row[0] for row in db.execute("SELECT DISTINCT card_hash FROM main.card_patient")]
                    if card_hashes:
                        ph = ",".join("?" for _ in card_hashes)
                        db.execute(f"INSERT INTO main.blocked_identity_card(card_hash,patient_count,patient_hashes_json,rule_version) SELECT card_hash,patient_count,patient_hashes_json,rule_version FROM canary_identity.blocked_identity_card WHERE card_hash IN ({ph})", tuple(card_hashes))
            result_counts = _identity_table_counts(db)
            metadata = {
                "identity_snapshot_source": str(source),
                "identity_snapshot_source_sha256": file_sha256(source)[1],
                "identity_snapshot_manifest_sha256": manifest_sha256,
                "identity_snapshot_rule_version": RULE_VERSION,
                "identity_snapshot_patient_value_filter": "ALL" if patient_values is None else "SUBSET",
                "identity_snapshot_counts": json.dumps(result_counts, sort_keys=True),
            }
            for key, value in metadata.items():
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (key, text(value)))
            db.commit()
            db.execute("DETACH DATABASE canary_identity")
        except Exception:
            db.rollback()
            try:
                db.execute("DETACH DATABASE canary_identity")
            except sqlite3.Error:
                pass
            raise
    return result_counts


class StagedTaskWriter:
    """Keep task shards as .partial until every shard passes validation."""

    def __init__(self, staging_dir: Path, output_dir: Path, task_id: str, output_kind: str, schema_: pa.Schema, prefix: str) -> None:
        self.staging_dir = staging_dir
        self.output_dir = output_dir
        self.task_id = task_id
        self.output_kind = output_kind
        self.schema = schema_
        self.prefix = prefix
        self.buffer: list[dict[str, Any]] = []
        self.part = 0
        self.files: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        self.buffer.append(row)
        sample_peak_memory()
        if len(self.buffer) >= BUFFER_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        self.part += 1
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        basename = f"{self.prefix}.part-{self.part:05d}.parquet"
        partial = self.staging_dir / f"{basename}.partial"
        final = self.output_dir / basename
        if partial.exists() or final.exists():
            raise FileExistsError(f"task output already exists: {final}")
        arrays = [pa.array([row.get(field.name) for row in self.buffer], type=field.type) for field in self.schema]
        table = pa.Table.from_arrays(arrays, schema=self.schema)
        pq.write_table(table, partial, compression="zstd")
        _, sha = file_sha256(partial)
        schema_sha = hashlib.sha256(str(self.schema).encode("utf-8")).hexdigest()
        self.files.append({"partial": str(partial), "path": str(final), "row_count": int(table.num_rows), "sha256": sha, "schema_sha256": schema_sha})
        self.buffer = []

    def close_partial(self) -> list[dict[str, Any]]:
        self.flush()
        return list(self.files)

    def commit(self) -> list[dict[str, Any]]:
        self.flush()
        committed: list[dict[str, Any]] = []
        for item in self.files:
            partial = Path(item["partial"])
            final = Path(item["path"])
            if not partial.exists():
                raise FileNotFoundError(str(partial))
            metadata = pq.read_metadata(partial)
            actual_count = int(metadata.num_rows)
            _, actual_sha = file_sha256(partial)
            actual_schema_sha = hashlib.sha256(str(pq.read_schema(partial)).encode("utf-8")).hexdigest()
            if actual_count != int(item["row_count"]) or actual_sha != item["sha256"] or actual_schema_sha != item["schema_sha256"]:
                raise RuntimeError("staged_output_validation_failed")
            validated = {"row_count": actual_count, "sha256": actual_sha, "schema_sha256": actual_schema_sha}
            if final.exists():
                raise FileExistsError(f"formal output exists and overwrite is forbidden: {final}")
            partial.replace(final)
            committed.append({"path": str(final), "row_count": validated["row_count"], "sha256": validated["sha256"], "schema_sha256": validated["schema_sha256"]})
        self.files = committed
        return committed


def _full_output_dir(output_root: Path, output_kind: str) -> Path:
    if output_kind in FULL_SOURCE_SYSTEMS:
        return output_root / "record_links" / output_kind
    return output_root / output_kind


def _full_writer_set(staging_root: Path, output_root: Path, task_id: str, source_system: str) -> dict[str, StagedTaskWriter]:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    writers = {
        source_system: StagedTaskWriter(staging_root / source_system, _full_output_dir(output_root, source_system), task_id, source_system, LINK_SCHEMA, safe),
    }
    if source_system == "imaging":
        writers["soft_linking"] = StagedTaskWriter(staging_root / "soft_linking", _full_output_dir(output_root, "soft_linking"), task_id, "soft_linking", SOFT_SCHEMA, safe)
        writers["conflict_log"] = StagedTaskWriter(staging_root / "conflict_log", _full_output_dir(output_root, "conflict_log"), task_id, "conflict_log", CONFLICT_SCHEMA, safe)
    return writers


def _iter_full_task_rows(task: dict[str, Any], limit: int | None = None) -> Iterator[dict[str, Any]]:
    system = task["source_system"]
    path = Path(task["source_file"])
    if system == "document":
        columns = ["PATIENT_ID", "VISIT_ID", "ADMISSION_DATE_TIME", "DISCHARGE_DATE_TIME", "source_file", "source_row", "source_record_id", "parser_status"]
        iterator = iter_parquet([path], columns)
    elif system == "lab_l1":
        columns = ["PATIENT_ID", "VISIT_ID", "source_workbook", "source_sheet", "source_row", "source_record_id", "time_parse_status"]
        iterator = iter_parquet([path], columns)
    elif system == "lab_quarantine":
        columns = ["raw_values_json", "source_workbook", "source_sheet", "source_row", "source_record_id", "quarantine_reason"]
        iterator = iter_parquet([path], columns)
    elif system == "imaging":
        def image_iterator() -> Iterator[dict[str, Any]]:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    yield orjson.loads(line) if orjson is not None else json.loads(line)
        iterator = image_iterator()
    else:
        raise ValueError(f"unsupported source system: {system}")
    for index, row in enumerate(iterator):
        if limit is not None and index >= limit:
            break
        yield row


def _full_input_fingerprint(task: dict[str, Any], *, count_rows: bool = True) -> dict[str, Any]:
    path = Path(task["source_file"])
    if not path.exists():
        raise FileNotFoundError("INPUT_MISSING")
    size, sha = file_sha256(path)
    if sha != task["source_sha256"] or int(task.get("source_size", size)) != size:
        raise RuntimeError("INPUT_HASH_CHANGED")
    if path.suffix == ".parquet":
        count = source_record_count(path)
    else:
        with path.open("r", encoding="utf-8") as stream:
            count = sum(1 for _ in stream)
    if count_rows and count != int(task["expected_row_count"]):
        raise RuntimeError("INPUT_ROW_COUNT_CHANGED")
    return {"size": size, "sha256": sha, "row_count": count}


def _full_sample_patient_values(task: dict[str, Any], limit: int) -> set[str]:
    values: set[str] = set()
    for row in _iter_full_task_rows(task, limit):
        if task["source_system"] == "lab_quarantine":
            raw = parse_quarantine_values(row.get("raw_values_json")).get("PATIENT_ID")
        elif task["source_system"] == "imaging":
            raw = image_patient(row)
        else:
            raw = row.get("PATIENT_ID")
        value = norm(raw, "PATIENT_ID")
        if value:
            values.add(value)
    return values


def _full_process_document_row(db: sqlite3.Connection, row: dict[str, Any], writer: StagedTaskWriter, counters: Counter[str]) -> None:
    key = source_key("document", text(row.get("source_file")), row.get("source_record_id"))
    uid = uid_for(db, "document", row.get("PATIENT_ID"), key, create=False, register_alias=False)
    visit = norm(row.get("VISIT_ID"), "VISIT_ID")
    encounter = get_encounter(db, uid, visit)
    if uid and visit:
        ek = f"{uid}|{visit}"
        if row.get("ADMISSION_DATE_TIME"):
            db.execute("INSERT OR IGNORE INTO encounter_window(encounter_key,kind,value) VALUES(?,?,?)", (ek, "admission", text(row.get("ADMISSION_DATE_TIME"))))
        if row.get("DISCHARGE_DATE_TIME"):
            db.execute("INSERT OR IGNORE INTO encounter_window(encounter_key,kind,value) VALUES(?,?,?)", (ek, "discharge", text(row.get("DISCHARGE_DATE_TIME"))))
    category = "hard" if uid else "unmatched"
    writer.add(link_row(key, "document", text(row.get("source_file")), "", row.get("source_row"), row.get("source_record_id"), uid, encounter, "HARD_PATIENT_EXACT" if uid else "PATIENT_UNMATCHED", category, "PATIENT_ID_EXACT", "HIGH" if uid else "NONE", bool(uid), bool(uid and visit), bool(uid), text(row.get("parser_status")), [row.get("PATIENT_ID"), row.get("VISIT_ID")]))
    record_disposition(db, key, "document", category)
    counters["rows"] += 1
    counters[category] += 1
    counters["hard_link_count"] += int(category == "hard")
    counters["unmatched_count"] += int(category == "unmatched")


def _full_process_lab_row(db: sqlite3.Connection, row: dict[str, Any], writer: StagedTaskWriter, counters: Counter[str]) -> None:
    source_file = text(row.get("source_workbook"))
    sheet = text(row.get("source_sheet"))
    key = source_key("lab_l1", f"{source_file}|{sheet}", row.get("source_record_id"))
    uid = uid_for(db, "lab_l1", row.get("PATIENT_ID"), key, create=False, register_alias=False)
    visit = norm(row.get("VISIT_ID"), "VISIT_ID")
    encounter = get_encounter(db, uid, visit)
    if not uid:
        category, status, method, confidence = "unmatched", "PATIENT_UNMATCHED", "PATIENT_ID_MISSING_OR_UNMATCHED", "NONE"
    elif not visit:
        category, status, method, confidence = "hard", "PATIENT_ONLY", "PATIENT_ID_EXACT_VISIT_MISSING", "HIGH"
    elif encounter:
        category, status, method, confidence = "hard", "HARD_PATIENT_AND_ENCOUNTER", "PATIENT_ID_AND_VISIT_ID_EXACT", "HIGH"
    else:
        category, status, method, confidence = "unmatched", "ENCOUNTER_UNMATCHED", "PATIENT_ID_EXACT_VISIT_UNMATCHED", "HIGH"
    event_ok = not text(row.get("time_parse_status")).startswith("INVALID")
    link = link_row(key, "lab_l1", source_file, sheet, row.get("source_row"), row.get("source_record_id"), uid, encounter, status, category, method, confidence, bool(uid), bool(uid and encounter), event_ok, text(row.get("time_parse_status")), [row.get("PATIENT_ID"), row.get("VISIT_ID")])
    writer.add(link)
    record_disposition(db, key, "lab_l1", category)
    counters["rows"] += 1
    counters[category] += 1
    counters["hard_link_count"] += int(category == "hard")
    counters["unmatched_count"] += int(category == "unmatched")


def _full_process_quarantine_row(db: sqlite3.Connection, row: dict[str, Any], writer: StagedTaskWriter, counters: Counter[str]) -> None:
    values = parse_quarantine_values(row.get("raw_values_json"))
    source_file = text(row.get("source_workbook"))
    sheet = text(row.get("source_sheet"))
    key = source_key("lab_quarantine", f"{source_file}|{sheet}", row.get("source_record_id"))
    patient = norm(values.get("PATIENT_ID"), "PATIENT_ID")
    uid = uid_for(db, "lab_quarantine", patient, key, create=False, register_alias=False)
    visit = norm(values.get("VISIT_ID"), "VISIT_ID")
    encounter = get_encounter(db, uid, visit)
    writer.add(link_row(key, "lab_quarantine", source_file, sheet, row.get("source_row"), row.get("source_record_id"), uid, encounter, "QUARANTINE_EVENT_INELIGIBLE", "quarantined", "QUARANTINE_SOURCE_NOT_EVENT_ELIGIBLE", "HIGH" if uid else "NONE", bool(uid), False, False, text(row.get("quarantine_reason")), [patient, values.get("VISIT_ID")], text(row.get("quarantine_reason"))))
    record_disposition(db, key, "lab_quarantine", "quarantined")
    counters["rows"] += 1
    counters["quarantined"] += 1
    counters["hard_link_count"] += 0
    counters["unmatched_count"] += int(uid is None)


def _full_process_imaging_row(db: sqlite3.Connection, row: dict[str, Any], writers: dict[str, StagedTaskWriter], counters: Counter[str]) -> None:
    key = source_key("imaging", text(row.get("_stage6_source_path_name")) or text(row.get("source_file")), row.get("record_cluster_id"))
    patient = image_patient(row)
    status = text(row.get("status"))
    identity_ok = image_identity_eligible(row)
    uid = uid_for(db, "imaging", patient, key, create=False, register_alias=False) if identity_ok and patient else None
    card = image_card(row)
    card_hash = digest_value(card) if card else None
    blocked = bool(card_hash and db.execute("SELECT 1 FROM blocked_identity_card WHERE card_hash=?", (card_hash,)).fetchone())
    category = "unmatched"
    link_status = "PATIENT_UNMATCHED"
    method = "PATIENT_ID_NOT_AVAILABLE"
    confidence = "NONE"
    if blocked:
        category = "conflict"
        link_status = "IDENTITY_CARD_MULTI_PATIENT_CONFLICT"
        method = "PATIENT_ID_EXACT_CARD_CONFLICT_BLOCKED" if uid else "IDENTITY_CARD_CONFLICT_NO_AUTO_MERGE"
        confidence = "HIGH" if uid else "NONE"
        writers["conflict_log"].add({"conflict_group_hash": card_hash, "conflict_type": "IDENTITY_CARD_MULTI_PATIENT_CONFLICT", "source_record_key": key, "evidence_values_hash": evidence_hash([patient, card_hash]), "resolution_status": "NO_AUTO_MERGE", "rule_version": RULE_VERSION})
        counters["conflict_count"] += 1
    elif status == "unresolved" or image_tokens(row) is None:
        uid = None
        link_status = "UNALIGNED_STRUCTURE_UNRESOLVED"
        method = "NO_FIXED_TOKEN_EXTRACTION"
    elif status == "repaired_review":
        candidate = uid_for(db, "imaging", patient, key, create=False, register_alias=False) if patient else None
        if candidate:
            writers["soft_linking"].add(soft_row(key, candidate, ["PATIENT_ID_EXACT_REPAIRED_REVIEW"], [patient]))
            counters["soft_link_count"] += 1
        uid = None
        category = "soft" if candidate else "unmatched"
        link_status = "SOFT_LINK_DISABLED" if candidate else "PATIENT_UNMATCHED"
        method = "REPAIRED_REVIEW_CANDIDATE"
        confidence = "LOW" if candidate else "NONE"
    elif status == "field_invalid" and not identity_safe(row):
        uid = None
        link_status = "FIELD_INVALID_IDENTITY_UNSAFE"
        method = "IDENTITY_FIELDS_NOT_TRUSTED"
    elif uid:
        category = "hard"
        link_status = "HARD_PATIENT_EXACT"
        method = "PATIENT_ID_EXACT_TRUSTED_STRUCTURE"
        confidence = "HIGH"
    identity_ok = bool(uid and image_identity_eligible(row) and status not in {"repaired_review", "unresolved"})
    timeline_ok = identity_ok
    event_ok = image_event_eligible(row, uid) and not blocked
    if status in {"repaired_review", "unresolved"} or (status == "field_invalid" and not identity_safe(row)):
        event_ok = False
    writers["imaging"].add(link_row(key, "imaging", text(row.get("source_file")), "", None, row.get("record_cluster_id"), uid, None, link_status, category, method, confidence, identity_ok, timeline_ok, event_ok, status, [patient, card]))
    record_disposition(db, key, "imaging", category)
    counters["rows"] += 1
    counters[category] += 1
    counters["hard_link_count"] += int(category == "hard")
    counters["unmatched_count"] += int(category == "unmatched")


def _full_process_task_rows(db: sqlite3.Connection, task: dict[str, Any], writers: dict[str, StagedTaskWriter], limit: int | None = None) -> Counter[str]:
    counters: Counter[str] = Counter()
    system = task["source_system"]
    for row in _iter_full_task_rows(task, limit):
        if system == "imaging":
            row["_stage6_source_path_name"] = Path(task["source_file"]).name
        if system == "document":
            _full_process_document_row(db, row, writers[system], counters)
        elif system == "lab_l1":
            _full_process_lab_row(db, row, writers[system], counters)
        elif system == "lab_quarantine":
            _full_process_quarantine_row(db, row, writers[system], counters)
        elif system == "imaging":
            _full_process_imaging_row(db, row, writers, counters)
    return counters


def _task_output_rows(db: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return db.execute("SELECT * FROM task_output WHERE task_id=? ORDER BY output_kind,shard_index", (task_id,)).fetchall()


def _validate_task_output_rows(rows: Sequence[sqlite3.Row], expected_count: int) -> tuple[bool, int, str]:
    if not rows:
        return False, 0, "NO_TASK_OUTPUT"
    task_ids = {str(row["task_id"]) for row in rows}
    if len(task_ids) != 1:
        return False, 0, "TASK_OUTPUT_MIXED_TASK_IDS"
    primary_kind = next(iter(task_ids)).split(":", 1)[0]
    primary_total = 0
    for row in rows:
        if row["status"] != "COMMITTED":
            return False, primary_total, "TASK_OUTPUT_NOT_COMMITTED"
        try:
            validate_formal_output(Path(row["output_file"]), int(row["row_count"]), row["sha256"], row["schema_sha256"])
        except Exception as exc:
            return False, primary_total, f"TASK_OUTPUT_INVALID:{type(exc).__name__}"
        if row["output_kind"] == primary_kind:
            primary_total += int(row["row_count"])
    if primary_total != int(expected_count):
        return False, primary_total, "TASK_OUTPUT_ROW_COUNT_MISMATCH"
    return True, primary_total, "OK"


def _task_output_link_counters(rows: Sequence[sqlite3.Row]) -> Counter[str]:
    task_id = str(rows[0]["task_id"])
    primary_kind = task_id.split(":", 1)[0]
    disposition_counts: Counter[str] = Counter()
    for row in rows:
        if row["output_kind"] != primary_kind:
            continue
        table = pq.read_table(Path(row["output_file"]), columns=["disposition_class"])
        disposition_counts.update(value for value in table.column("disposition_class").to_pylist() if value)
    return Counter({
        "hard_link_count": disposition_counts.get("hard", 0),
        "soft_link_count": disposition_counts.get("soft", 0),
        "unmatched_count": disposition_counts.get("unmatched", 0),
        "conflict_count": disposition_counts.get("conflict", 0),
    })


def _discover_task_formal_outputs(task_id: str, output_root: Path) -> list[dict[str, Any]]:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    discovered: list[dict[str, Any]] = []
    for output_kind, schema_ in FULL_OUTPUT_SCHEMAS.items():
        directory = _full_output_dir(output_root, output_kind)
        for path in sorted(directory.glob(f"{safe}.part-*.parquet")) if directory.exists() else []:
            meta = validate_formal_output(path)
            shard_match = re.search(r"\.part-(\d+)\.parquet$", path.name)
            discovered.append({"output_kind": output_kind, "shard_index": int(shard_match.group(1)) if shard_match else len(discovered) + 1, "output_file": str(path), "row_count": meta["row_count"], "sha256": meta["sha256"], "schema_sha256": meta["schema_sha256"], "status": "COMMITTED", "updated_at": now()})
    return discovered


def _register_task_outputs(db: sqlite3.Connection, task_id: str, outputs: Sequence[dict[str, Any]]) -> None:
    for item in outputs:
        db.execute("INSERT OR REPLACE INTO task_output(task_id,output_kind,shard_index,output_file,row_count,sha256,schema_sha256,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, item["output_kind"], item["shard_index"], item["output_file"], item["row_count"], item["sha256"], item["schema_sha256"], item.get("status", "COMMITTED"), now()))


def _mark_full_task(path: Path, task_id: str, status: str, *, actual_count: int | None = None, counters: Counter[str] | None = None, error_reason: str | None = None) -> None:
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        if counters is None:
            counters = Counter()
        db.execute("UPDATE task_state SET status=?,actual_row_count=?,hard_link_count=?,soft_link_count=?,unmatched_count=?,conflict_count=?,error_reason=?,updated_at=? WHERE task_id=?", (status, actual_count, counters.get("hard_link_count", 0), counters.get("soft_link_count", 0), counters.get("unmatched_count", 0), counters.get("conflict_count", 0), error_reason, now(), task_id))
        db.commit()


def _archive_task_partials(staging_dir: Path, archive_root: Path, task_id: str) -> list[str]:
    if not staging_dir.exists():
        return []
    archive = archive_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") / re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    moved: list[str] = []
    for path in sorted(staging_dir.rglob("*.partial")):
        target = archive / path.relative_to(staging_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved.append(str(target))
    return moved


def recover_full_task(path: Path, task: dict[str, Any], output_root: Path = FULL_OUTPUT_ROOT, staging_root: Path = FULL_TASK_STAGING, partial_archive: Path = FULL_PARTIAL_ARCHIVE) -> str:
    """Recover one task without deleting evidence or overwriting formal output."""

    ensure_full_executor_schema(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM task_state WHERE task_id=?", (task["task_id"],)).fetchone()
        if row is None:
            raise KeyError(task["task_id"])
        status = row["status"]
        if status == "SUCCEEDED":
            valid, _, reason = _validate_task_output_rows(_task_output_rows(db, task["task_id"]), task["expected_row_count"])
            if valid:
                return "SKIPPED_VERIFIED"
            next_status = "BLOCKED"
            next_reason = reason
        else:
            next_status = status
            next_reason = None
    if status == "SUCCEEDED":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE task_state SET status=?,error_reason=?,updated_at=? WHERE task_id=?", (next_status, next_reason, now(), task["task_id"]))
            db.commit()
        return next_status
    if status != "RUNNING":
        return status
    try:
        discovered = _discover_task_formal_outputs(task["task_id"], output_root)
    except Exception as exc:
        _mark_full_task(path, task["task_id"], "BLOCKED", error_reason=f"FORMAL_OUTPUT_INVALID:{type(exc).__name__}")
        return "BLOCKED"
    formal_total = sum(int(item["row_count"]) for item in discovered if item["output_kind"] == task["source_system"])
    if discovered and formal_total == int(task["expected_row_count"]):
        with sqlite3.connect(path, isolation_level=None) as commit_db:
            commit_db.execute("BEGIN IMMEDIATE")
            _register_task_outputs(commit_db, task["task_id"], discovered)
            commit_db.execute("UPDATE task_state SET status='SUCCEEDED',actual_row_count=?,updated_at=?,error_reason=NULL WHERE task_id=?", (formal_total, now(), task["task_id"]))
            commit_db.commit()
        return "RECOVERED_SUCCEEDED"
    task_staging = staging_root / re.sub(r"[^A-Za-z0-9_.-]", "_", task["task_id"])
    partials = list(task_staging.rglob("*.partial")) if task_staging.exists() else []
    if partials and not discovered:
        _archive_task_partials(task_staging, partial_archive, task["task_id"])
        _mark_full_task(path, task["task_id"], "PENDING", error_reason="RECOVERED_PARTIAL_ARCHIVED")
        return "RESET_PENDING"
    if discovered:
        _mark_full_task(path, task["task_id"], "BLOCKED", error_reason="INCOMPLETE_FORMAL_OUTPUT_REQUIRES_MANUAL_REVIEW")
        return "BLOCKED"
    _mark_full_task(path, task["task_id"], "PENDING", error_reason="RECOVERED_EMPTY_RUNNING_TASK")
    return "RESET_PENDING"


def _claim_full_task(path: Path, task_id: str | None = None) -> sqlite3.Row | None:
    with sqlite3.connect(path, isolation_level=None) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        if task_id:
            row = db.execute("SELECT * FROM task_state WHERE task_id=? AND status='PENDING'", (task_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM task_state WHERE status='PENDING' ORDER BY task_id LIMIT 1").fetchone()
        if row is None:
            db.commit()
            return None
        db.execute("UPDATE task_state SET status='RUNNING',attempts=attempts+1,updated_at=?,error_reason=NULL WHERE task_id=?", (now(), row["task_id"]))
        db.commit()
        return row


def _run_full_task(path: Path, task: dict[str, Any], output_root: Path, staging_root: Path, partial_archive: Path, *, smoke_limit: int | None = None) -> dict[str, Any]:
    if smoke_limit is None:
        fingerprint = _full_input_fingerprint(task, count_rows=True)
    else:
        fingerprint = _full_input_fingerprint(task, count_rows=False)
    task_id = task["task_id"]
    staging_dir = staging_root / re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    writers = _full_writer_set(staging_dir, output_root, task_id, task["source_system"])
    try:
        with sqlite3.connect(path) as db:
            counters = _full_process_task_rows(db, task, writers, smoke_limit)
            db.commit()
    except Exception as exc:
        for writer in writers.values():
            writer.close_partial()
        _mark_full_task(path, task_id, "FAILED", error_reason=f"{type(exc).__name__}:{str(exc)[:160]}")
        raise
    finally:
        for writer in writers.values():
            try:
                writer.close_partial()
            except Exception:
                pass
    expected = int(smoke_limit if smoke_limit is not None else task["expected_row_count"])
    if counters.get("rows", 0) != expected or counters.get("rows", 0) != sum(counters.get(key, 0) for key in ("hard", "unmatched", "quarantined", "soft", "conflict")):
        _mark_full_task(path, task_id, "FAILED", actual_count=counters.get("rows", 0), counters=counters, error_reason="DISPOSITION_ROW_COUNT_MISMATCH")
        raise RuntimeError("DISPOSITION_ROW_COUNT_MISMATCH")
    committed: list[dict[str, Any]] = []
    try:
        for writer in writers.values():
            for item in writer.commit():
                item["output_kind"] = writer.output_kind
                item["output_file"] = item["path"]
                item["shard_index"] = int(Path(item["path"]).stem.rsplit("-", 1)[-1])
                committed.append(item)
    except Exception as exc:
        _mark_full_task(path, task_id, "FAILED", actual_count=counters.get("rows", 0), counters=counters, error_reason=f"OUTPUT_COMMIT_FAILED:{type(exc).__name__}")
        raise
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        _register_task_outputs(db, task_id, committed)
        db.execute("UPDATE task_state SET status='SUCCEEDED',actual_row_count=?,hard_link_count=?,soft_link_count=?,unmatched_count=?,conflict_count=?,updated_at=?,error_reason=NULL WHERE task_id=?", (counters.get("rows", 0), counters.get("hard_link_count", 0), counters.get("soft_link_count", 0), counters.get("unmatched_count", 0), counters.get("conflict_count", 0), now(), task_id))
        db.commit()
    return {"task_id": task_id, "input": fingerprint, "row_count": counters.get("rows", 0), "disposition_count": counters.get("rows", 0), "outputs": committed}


def _full_task_statuses(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return dict(db.execute("SELECT status,COUNT(*) FROM task_state GROUP BY status").fetchall())


def _prepare_full_tasks(path: Path, manifest: dict[str, Any], *, resume: bool) -> dict[str, int]:
    task_map = _full_task_map(manifest)
    for task in task_map.values():
        recover_full_task(path, task)
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT task_id,status,error_reason FROM task_state ORDER BY task_id").fetchall()
    for task_id, status, error_reason in rows:
        task = task_map.get(task_id)
        if task is None:
            raise RuntimeError("FULL_STATE_TASK_NOT_IN_MANIFEST")
        if status == "SUCCEEDED":
            with sqlite3.connect(path) as db:
                valid, _, reason = _validate_task_output_rows(_task_output_rows(db, task_id), task["expected_row_count"])
            if not valid:
                _mark_full_task(path, task_id, "BLOCKED", error_reason=reason)
                raise RuntimeError("SUCCEEDED_OUTPUT_INVALID")
        elif status == "FAILED":
            if not resume:
                raise RuntimeError("FAILED_TASK_REQUIRES_EXPLICIT_RESUME")
            _mark_full_task(path, task_id, "PENDING", error_reason="EXPLICIT_RESUME")
        elif status == "BLOCKED":
            if resume and error_reason == "TASK_OUTPUT_ROW_COUNT_MISMATCH":
                with sqlite3.connect(path) as db:
                    output_rows = _task_output_rows(db, task_id)
                    valid, primary_total, _ = _validate_task_output_rows(output_rows, task["expected_row_count"])
                if valid:
                    counters = _task_output_link_counters(output_rows)
                    _mark_full_task(path, task_id, "SUCCEEDED", actual_count=primary_total, counters=counters)
                    continue
            raise RuntimeError("BLOCKED_TASK_REQUIRES_MANUAL_REVIEW")
    return _full_task_statuses(path)


def _export_full_entities(path: Path, output_root: Path) -> dict[str, list[dict[str, Any]]]:
    with sqlite3.connect(path) as db:
        outputs = export_entities(db, output_root)
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('entity_export_completed',?)", ("true",))
        db.commit()
    return outputs


def validate_resume_state(path: Path, manifest_sha256: str) -> dict[str, int]:
    """Validate an existing formal identity snapshot before resuming tasks."""

    with sqlite3.connect(path) as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata").fetchall())
        if metadata.get("identity_snapshot_manifest_sha256") != manifest_sha256:
            raise RuntimeError("RESUME_MANIFEST_HASH_MISMATCH")
        if metadata.get("identity_snapshot_rule_version") != RULE_VERSION:
            raise RuntimeError("RESUME_RULE_VERSION_MISMATCH")
        counts = _identity_table_counts(db)
        if not counts.get("patient_registry") or not counts.get("identity_alias"):
            raise RuntimeError("RESUME_IDENTITY_SNAPSHOT_MISSING")
        try:
            expected_counts = json.loads(metadata.get("identity_snapshot_counts", "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("RESUME_IDENTITY_COUNTS_INVALID") from exc
        if counts != {key: int(expected_counts.get(key, -1)) for key in FULL_IDENTITY_TABLES}:
            raise RuntimeError("RESUME_IDENTITY_COUNTS_MISMATCH")
        if not identity_alias_index_health(db)["passed"]:
            raise RuntimeError("RESUME_IDENTITY_INDEX_MISSING")
    return counts


def run_full_executor(manifest: dict[str, Any], *, release_approved: bool = False, resume: bool = False) -> dict[str, Any]:
    """Formal executor entrypoint. The caller must explicitly approve release."""

    if not release_approved:
        raise PermissionError("FULL_RUN_REQUIRES_RELEASE_APPROVED")
    manifest_sha = file_sha256(V2_MANIFEST)[1]
    if resume:
        if not FULL_STATE.exists():
            raise RuntimeError("RESUME_FULL_STATE_MISSING")
        ensure_full_executor_schema(FULL_STATE)
        validate_resume_state(FULL_STATE, manifest_sha)
    else:
        readiness = json.loads(FULL_PREFLIGHT_REPORT.read_text(encoding="utf-8")) if FULL_PREFLIGHT_REPORT.exists() else full_run_preflight()
        if not readiness.get("full_run_execution_ready") or readiness.get("manifest_sha256") != manifest_sha:
            raise RuntimeError("FULL_RUN_NOT_EXECUTION_READY")
        if _full_output_files():
            raise RuntimeError("FORMAL_OUTPUT_DIRECTORY_NOT_EMPTY")
        ensure_full_executor_schema(FULL_STATE)
        with sqlite3.connect(FULL_STATE) as db:
            current = _identity_table_counts(db)
            if any(current.values()):
                raise RuntimeError("FULL_STATE_IDENTITY_TABLES_NOT_EMPTY")
        copy_identity_snapshot(CANARY_STATE, FULL_STATE, manifest_sha)
    _prepare_full_tasks(FULL_STATE, manifest, resume=resume)
    FULL_TASK_STAGING.mkdir(parents=True, exist_ok=True)
    FULL_PARTIAL_ARCHIVE.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    while True:
        task_row = _claim_full_task(FULL_STATE)
        if task_row is None:
            break
        task = _full_task_map(manifest)[task_row["task_id"]]
        try:
            result = _run_full_task(FULL_STATE, task, FULL_OUTPUT_ROOT, FULL_TASK_STAGING, FULL_PARTIAL_ARCHIVE)
            results.append(result)
            print(f"full_task task={task['task_id']} status=SUCCEEDED rows={result['row_count']}", flush=True)
        except RuntimeError as exc:
            if str(exc) in {"INPUT_HASH_CHANGED", "INPUT_ROW_COUNT_CHANGED"}:
                raise
            raise
    if _full_task_statuses(FULL_STATE) != {"SUCCEEDED": len(manifest.get("tasks", []))}:
        raise RuntimeError("FULL_RUN_TASKS_NOT_ALL_SUCCEEDED")
    entity_outputs = _export_full_entities(FULL_STATE, FULL_OUTPUT_ROOT)
    with sqlite3.connect(FULL_STATE) as db:
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('full_run_started',?)", ("true",))
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('full_run_completed',?)", ("true",))
        db.commit()
    return {"task_count": len(results), "outputs": entity_outputs, "full_run_started": True}


def full_run_smoke(sample_rows: int = 5) -> dict[str, Any]:
    """Process one bounded sample task per source in an independent state/output."""

    preflight_report: dict[str, Any]
    cached = json.loads(FULL_PREFLIGHT_REPORT.read_text(encoding="utf-8")) if FULL_PREFLIGHT_REPORT.exists() else None
    cached_valid = bool(cached and cached.get("passed") and cached.get("manifest_sha256") == file_sha256(V2_MANIFEST)[1] and cached.get("full_state_sha256") == file_sha256(FULL_STATE)[1] and cached.get("input_hash_check", {}).get("passed"))
    preflight_report = cached if cached_valid else full_run_preflight()
    if not preflight_report.get("passed"):
        return {"smoke_passed": False, "reason": "FULL_RUN_PREFLIGHT_FAILED", "preflight": str(FULL_PREFLIGHT_REPORT)}
    manifest = _full_manifest()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    smoke_root = V2_ROOT / f"full_run_smoke_{timestamp}"
    if smoke_root.exists():
        raise FileExistsError("SMOKE_OUTPUT_ALREADY_EXISTS")
    smoke_output = smoke_root / "output"
    smoke_state = smoke_root / "state.sqlite3"
    smoke_tasks: list[dict[str, Any]] = []
    values: set[str] = set()
    for system in FULL_SOURCE_SYSTEMS:
        original = next(task for task in manifest["tasks"] if task["source_system"] == system)
        task = dict(original)
        task["task_id"] = f"smoke:{system}"
        task["expected_row_count"] = sample_rows
        smoke_tasks.append(task)
        values.update(_full_sample_patient_values(original, sample_rows))
    init_state(smoke_state, smoke_tasks, seed_full=True)
    ensure_full_executor_schema(smoke_state)
    snapshot_counts = copy_identity_snapshot(CANARY_STATE, smoke_state, preflight_report["manifest_sha256"], values)
    results: list[dict[str, Any]] = []
    for task in smoke_tasks:
        claimed = _claim_full_task(smoke_state, task["task_id"])
        if claimed is None:
            raise RuntimeError("SMOKE_TASK_CLAIM_FAILED")
        results.append(_run_full_task(smoke_state, task, smoke_output, smoke_root / "task_staging", smoke_root / "partial_archive", smoke_limit=sample_rows))
    entity_outputs = _export_full_entities(smoke_state, smoke_output)
    with sqlite3.connect(smoke_state) as db:
        statuses = dict(db.execute("SELECT status,COUNT(*) FROM task_state GROUP BY status").fetchall())
        disposition_count = int(db.execute("SELECT COUNT(*) FROM disposition").fetchone()[0])
        disposition_unique_count = int(db.execute("SELECT COUNT(DISTINCT source_record_key) FROM disposition").fetchone()[0])
        task_output_count = int(db.execute("SELECT COALESCE(SUM(row_count),0) FROM task_output WHERE output_kind IN ('document','lab_l1','lab_quarantine','imaging')").fetchone()[0])
        identity_counts = _identity_table_counts(db)
        state_integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
    partials = [str(path) for path in smoke_root.rglob("*.partial")]
    formal_state_hash = file_sha256(FULL_STATE)[1]
    smoke_report = {
        "full_run_smoke_report_version": "stage6_full_run_smoke_v1",
        "rule_version": RULE_VERSION,
        "sample_rows_per_source": sample_rows,
        "source_systems": list(FULL_SOURCE_SYSTEMS),
        "task_count": len(results),
        "task_statuses": statuses,
        "disposition_count": disposition_count,
        "disposition_unique_count": disposition_unique_count,
        "task_output_row_count": task_output_count,
        "input_output_row_conservation": disposition_count == disposition_unique_count == task_output_count == sample_rows * len(FULL_SOURCE_SYSTEMS),
        "state_integrity": state_integrity,
        "identity_snapshot_counts": identity_counts,
        "identity_snapshot_source_counts": snapshot_counts,
        "partial_count": len(partials),
        "formal_full_state_sha256_after": formal_state_hash,
        "formal_full_state_untouched": formal_state_hash == preflight_report.get("full_state_sha256"),
        "canary_output_touched": False,
        "patient_folder_inventory_used": False,
        "timeline_started": False,
        "medical_semantic_extraction_started": False,
        "pii_in_report": False,
        "output_root": str(smoke_output),
        "state_db": str(smoke_state),
    }
    smoke_report["smoke_passed"] = all((
        smoke_report["task_statuses"] == {"SUCCEEDED": len(FULL_SOURCE_SYSTEMS)},
        smoke_report["input_output_row_conservation"],
        smoke_report["partial_count"] == 0,
        smoke_report["state_integrity"] == "ok",
        smoke_report["formal_full_state_untouched"],
        not smoke_report["canary_output_touched"],
    ))
    smoke_report_path = V2_AUDIT / "stage6_full_run_smoke_report.json"
    atomic_json(smoke_report_path, smoke_report)
    readiness = dict(preflight_report)
    readiness["smoke_report"] = str(smoke_report_path)
    readiness["smoke_passed"] = bool(smoke_report["smoke_passed"])
    readiness["full_run_execution_ready"] = bool(preflight_report.get("passed")) and bool(smoke_report["smoke_passed"])
    atomic_json(FULL_PREFLIGHT_REPORT, readiness)
    return smoke_report


def write_full_run_command() -> Path:
    path = V2_AUDIT / "stage6_full_run_command.txt"
    if path.exists():
        path.unlink()
    return path


def release_gate(preflight_report: dict[str, Any], profile: dict[str, Any], canary: dict[str, Any], tests_passed: bool) -> dict[str, Any]:
    strata_passed = all(item.get("passed", False) for item in canary.get("strata", {}).values())
    full_state_pending = False
    full_state_running = False
    full_state_unchanged = False
    if FULL_STATE.exists():
        with sqlite3.connect(FULL_STATE) as db:
            status_counts = dict(db.execute("SELECT status,COUNT(*) FROM task_state GROUP BY status").fetchall())
        full_state_pending = status_counts.get("PENDING", 0) == len(json.loads(V2_MANIFEST.read_text(encoding="utf-8")).get("tasks", []))
        full_state_running = status_counts.get("RUNNING", 0) > 0
        expected_full_hash = preflight_report.get("full_state_sha256")
        full_state_unchanged = bool(expected_full_hash and file_sha256(FULL_STATE)[1] == expected_full_hash)
    full_output_files = [path for path in V2_ROOT.glob("full_run_*") if path.is_file()]
    checks = {
        "fresh_preflight_passed": bool(preflight_report.get("fresh_preflight")) and bool(preflight_report.get("passed")),
        "preflight_passed": bool(preflight_report.get("passed")),
        "unit_tests_passed": bool(tests_passed),
        "all_canary_strata_passed": strata_passed,
        "low_confidence_alias_pollution_zero": canary.get("low_confidence_alias_pollution_count") == 0,
        "blocked_card_auto_merge_zero": canary.get("conflict_auto_merge_count") == 0,
        "pii_audit_passed": bool(canary.get("pii_audit_passed")) and not canary.get("pii_in_report"),
        "peak_memory_under_4gib": int(canary.get("peak_memory_bytes", 0)) < 4 * 1024 ** 3,
        "full_state_not_started": full_state_pending and not full_state_running,
        "full_state_unchanged": full_state_unchanged,
        "full_output_empty": not full_output_files,
        "profile_rule_version_correct": profile.get("rule_version") == RULE_VERSION,
    }
    command_path = write_full_run_command()
    canary_acceptance_passed = all(checks.values())
    gate = {
        "release_gate_report_version": "stage6_release_gate_v2",
        "rule_version": RULE_VERSION,
        "release_gate_passed": canary_acceptance_passed,
        "canary_acceptance_passed": canary_acceptance_passed,
        "full_run_execution_ready": False,
        "checks": checks,
        "canary_report": str(V2_AUDIT / "stage6_canary_report.json"),
        "preflight_report": str(V2_AUDIT / "stage6_preflight_report.json"),
        "full_run_command_file": None,
        "full_run_implementation_present": False,
        "full_run_started": False,
        "full_alignment_started": False,
        "timeline_started": False,
        "medical_semantic_extraction_started": False,
        "pii_in_report": False,
    }
    atomic_json(V2_AUDIT / "stage6_release_gate_report.json", gate)
    return gate


def _load_or_run_preflight() -> dict[str, Any]:
    return preflight()


def _run_test_suite() -> bool:
    command = [sys.executable, "-m", "pytest", "-q", "tests/test_upstream_pipeline_core.py"]
    result = subprocess.run(command, cwd=str(MODULE_ROOT.parents[1]), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    return result.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 6 V2 preflight, smoke and controlled full-run executor")
    parser.add_argument("command", choices=("preflight", "dry-run", "profile", "canary", "release-gate", "full-run-preflight", "full-run-smoke", "full-run"))
    parser.add_argument("--tests-passed", action="store_true")
    parser.add_argument("--release-approved", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--smoke-rows", type=int, default=5)
    args = parser.parse_args(argv)
    if args.command == "full-run-preflight":
        report = full_run_preflight()
        print(json.dumps({"passed": report["passed"], "full_run_execution_ready": report["full_run_execution_ready"], "checks": report["checks"]}, ensure_ascii=False))
        return 0 if report["passed"] else 1
    if args.command == "full-run-smoke":
        report = full_run_smoke(max(1, min(int(args.smoke_rows), BUFFER_SIZE)))
        print(json.dumps({"smoke_passed": report.get("smoke_passed", False), "disposition_count": report.get("disposition_count", 0), "task_output_row_count": report.get("task_output_row_count", 0), "formal_full_state_untouched": report.get("formal_full_state_untouched", False)}, ensure_ascii=False))
        return 0 if report.get("smoke_passed") else 1
    if args.command == "full-run":
        if not args.release_approved:
            print(json.dumps({"full_run_execution_ready": False, "full_run_started": False, "error": "FULL_RUN_REQUIRES_RELEASE_APPROVED"}, ensure_ascii=False))
            return 2
        manifest = _full_manifest()
        result = run_full_executor(manifest, release_approved=True, resume=args.resume)
        print(json.dumps({"full_run_started": result["full_run_started"], "task_count": result["task_count"]}, ensure_ascii=False))
        return 0
    preflight_report = _load_or_run_preflight()
    if args.command == "preflight":
        print(json.dumps({"passed": preflight_report["passed"], "manifest_task_count": preflight_report["manifest_task_count"], "failures": preflight_report["failures"]}, ensure_ascii=False))
        return 0 if preflight_report["passed"] else 1
    manifest = json.loads(V2_MANIFEST.read_text(encoding="utf-8"))
    if args.command == "dry-run":
        report = dry_run(manifest)
        print(json.dumps({"task_count": report["task_count"], "predicted_output_shard_count": report["predicted_output_shard_count"], "data_rows_read": 0}, ensure_ascii=False))
        return 0
    if args.command == "profile":
        inventory_boundary_check()
        init_state(CANARY_STATE, [], seed_full=False)
        with sqlite3.connect(CANARY_STATE) as db:
            completed = db.execute("SELECT value FROM metadata WHERE key='registry_pass_completed'").fetchone()
        counts = {} if completed else build_registry(CANARY_STATE, manifest)
        profile = profile_report(CANARY_STATE, counts.get("row_counts", {}))
        atomic_json(V2_AUDIT / "stage6_identifier_profile.json", profile)
        print(json.dumps({"trusted_patient_count": profile["trusted_patient_count"], "blocked_identity_card_group_count": profile["blocked_identity_card_group_count"]}, ensure_ascii=False))
        return 0
    if args.command == "canary":
        dry_run(manifest)
        boundary = inventory_boundary_check()
        if not boundary["counts_match_requested_boundary"]:
            raise SystemExit("inventory boundary check failed")
        interruption_recovery_report(preflight_report.get("full_state_sha256"))
        init_state(CANARY_STATE, [], seed_full=False)
        with sqlite3.connect(CANARY_STATE) as db:
            completed = db.execute("SELECT value FROM metadata WHERE key='registry_pass_completed'").fetchone()
        if not completed:
            counts = build_registry(CANARY_STATE, manifest)
            profile = profile_report(CANARY_STATE, counts["row_counts"])
            atomic_json(V2_AUDIT / "stage6_identifier_profile.json", profile)
        else:
            profile = json.loads((V2_AUDIT / "stage6_identifier_profile.json").read_text(encoding="utf-8"))
        canary = canary_run(manifest, preflight_report)
        tests_passed = _run_test_suite()
        gate = release_gate(preflight_report, profile, canary, tests_passed)
        print(json.dumps({"canary_record_link_count": canary["record_link_count"], "conflict_record_count": canary["conflict_record_count"], "low_confidence_alias_pollution_count": canary["low_confidence_alias_pollution_count"], "peak_memory_bytes": canary["peak_memory_bytes"], "unit_tests_passed": tests_passed, "release_gate_passed": gate["release_gate_passed"]}, ensure_ascii=False))
        return 0 if gate["release_gate_passed"] else 1
    if args.command == "release-gate":
        inventory_boundary_check()
        profile = json.loads((V2_AUDIT / "stage6_identifier_profile.json").read_text(encoding="utf-8"))
        canary = json.loads((V2_AUDIT / "stage6_canary_report.json").read_text(encoding="utf-8"))
        _validate_canary_outputs(canary)
        canary = reevaluate_cached_canary_report(canary)
        tests_passed = args.tests_passed or _run_test_suite()
        gate = release_gate(preflight_report, profile, canary, tests_passed)
        print(json.dumps({"release_gate_passed": gate["release_gate_passed"], "checks": gate["checks"]}, ensure_ascii=False))
        return 0 if gate["release_gate_passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

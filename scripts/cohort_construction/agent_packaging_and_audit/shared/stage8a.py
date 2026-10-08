"""Stage 8A preparation runner.

This stage freezes input facts, defines report/event/fact contracts, prepares
representative samples and annotation templates, and records terminology
dependencies.  It does not perform batch medical-fact extraction.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq
import pyarrow.compute as pc

from scripts.cohort_construction.paths import data_root
from scripts.cohort_construction.agent_packaging_and_audit.shared.stage8a_models import (
    CONTRACT_VERSION,
    RULE_VERSION,
    build_event_uid,
    build_report_uid,
    build_sample_uid,
    canonical_json,
    contract_definition,
    deduplicate_facts,
    deduplicate_reports,
    sha256_text,
    stable_uuid,
)


DATA_ROOT = data_root()
CODE_ROOT = DATA_ROOT / "code"
OUTPUT_ROOT = DATA_ROOT / "pipeline_outputs_stage8_v1"
AUDIT_ROOT = OUTPUT_ROOT / "audit"
RESTRICTED_ROOT = OUTPUT_ROOT / "restricted"
SAMPLE_ROOT = RESTRICTED_ROOT / "samples"
ANNOTATION_ROOT = RESTRICTED_ROOT / "annotation"
STATE_DB = OUTPUT_ROOT / "state" / "stage8a_state.sqlite3"

STAGE7_ROOT = DATA_ROOT / "pipeline_outputs_stage7_v1" / "restricted" / "full_day_timeline"
STAGE7_PLAN = STAGE7_ROOT / "full_plan_v1.json"
PATHOLOGY_ROOT = (
    DATA_ROOT
    / "pipeline_outputs_stage6_v2"
    / "restricted"
    / "increment"
    / "pathology_v1"
    / "full"
)
PATHOLOGY_RECORD_ROOT = PATHOLOGY_ROOT / "pathology_record"
PATHOLOGY_LINK_ROOT = PATHOLOGY_ROOT / "record_links_pathology"
PATHOLOGY_CONFLICT_ROOT = PATHOLOGY_ROOT / "pathology_conflict_log"
PATHOLOGY_UNMATCHED_ROOT = PATHOLOGY_ROOT / "pathology_unmatched"
IMAGING_ROOT = DATA_ROOT / "pipeline_outputs_v2" / "patient_l1"
DOCUMENT_ROOT = DATA_ROOT / "pipeline_outputs_stage5_v1" / "restricted" / "document_l1"

BASELINE_STAGE7_PATIENTS = 14_923
BASELINE_STAGE7_EVENTS = 10_650_836
BASELINE_PATHOLOGY_LINKED = 9_460
BASELINE_IMAGING_EVENTS = 81_970

PATHOLOGY_TARGETS = {
    "PDAC": 120,
    "IPMN": 60,
    "PanNET/PNET": 45,
    "SPN": 35,
    "MCN": 30,
    "SCN": 35,
    "慢性胰腺炎": 51,
    "急性胰腺炎": 24,
}
IMAGING_TARGETS = {
    "胰腺增强CT/MR": 220,
    "MRCP": 60,
    "其他腹部或消化系统": 140,
    "胸部或肺部": 90,
    "介入及操作相关": 50,
    "其他部位": 40,
}
SURGERY_TARGETS = {
    "明确手术记录": 40,
    "术后病程": 25,
    "术前计划或知情同意": 20,
    "出院记录中的手术信息": 15,
    "取消、改期或未实施": 10,
    "边界或难判样本": 10,
}

IMAGING_STATUS_TARGETS = {"valid_15col": 540, "repaired_high": 60}
FIXED_SEED = "stage8a-v1-fixed-seed"
ANNOTATION_SPLITS = {
    "pathology": (120, 80),
    "imaging": (144, 96),
    "surgery_document": (48, 32),
}

DOC_NAME = "文书名称"
DOC_CONTENT = "文书内容"
PATHOLOGY_COLUMNS = {
    "pathology_record_uid",
    "canonical_source_record_key",
    "source_file",
    "source_sheet",
    "source_row",
    "disease_label",
    "disease_labels_json",
    "content_hash",
    "pathology_time_sequence_conflict",
    "is_content_conflict",
    "raw_values_json",
}


class Stage8AError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def document_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("gb18030", errors="surrogateescape")
    return str(value)


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value).strip())


def _json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        partial.unlink()
    with partial.open("wb") as handle:
        handle.write(payload)
        handle.flush()
    digest = hashlib.sha256(payload).hexdigest()
    partial.replace(path)
    return digest


def write_json_atomic(path: Path, value: Any) -> str:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return _write_bytes_atomic(path, payload)


def write_csv_atomic(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        partial.unlink()
    with partial.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fieldnames})
    digest = sha256_file(partial)
    partial.replace(path)
    return digest


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        partial.unlink()
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    digest = sha256_file(partial)
    partial.replace(path)
    return digest


def iter_parquet_rows(root: Path, columns: list[str] | None = None) -> Iterable[dict[str, Any]]:
    for path in sorted(root.rglob("*.parquet")):
        parquet = pq.ParquetFile(path)
        selected = [column for column in (columns or parquet.schema_arrow.names) if column in parquet.schema_arrow.names]
        if not selected:
            continue
        for batch in parquet.iter_batches(batch_size=25_000, columns=selected):
            yield from batch.to_pylist()


def iter_parquet_file_rows(paths: Iterable[Path], columns: list[str]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        parquet = pq.ParquetFile(path)
        selected = [column for column in columns if column in parquet.schema_arrow.names]
        if not selected:
            continue
        for batch in parquet.iter_batches(batch_size=25_000, columns=selected):
            yield from batch.to_pylist()


def parquet_row_count(root: Path) -> int:
    return sum(pq.ParquetFile(path).metadata.num_rows for path in sorted(root.rglob("*.parquet")))


def file_inventory(root: Path, suffixes: set[str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if suffixes and path.suffix.lower() not in suffixes:
            continue
        rows.append({"path": str(path), "size_bytes": path.stat().st_size})
    return rows


def open_state() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATE_DB)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS run_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_state(
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            input_hash TEXT,
            output_hash TEXT,
            error_reason TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact(
            artifact_path TEXT PRIMARY KEY,
            artifact_kind TEXT NOT NULL,
            row_count INTEGER,
            sha256 TEXT NOT NULL,
            schema_version TEXT,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def update_task(task_id: str, status: str, input_hash: str = "", output_hash: str = "", error_reason: str = "") -> None:
    if status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"}:
        raise ValueError(status)
    with open_state() as connection:
        previous = connection.execute("SELECT attempts FROM task_state WHERE task_id=?", (task_id,)).fetchone()
        attempts = int(previous[0]) if previous else 0
        if status == "RUNNING":
            attempts += 1
        connection.execute(
            """
            INSERT INTO task_state(task_id,status,attempts,input_hash,output_hash,error_reason,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET
              status=excluded.status, attempts=excluded.attempts,
              input_hash=excluded.input_hash, output_hash=excluded.output_hash,
              error_reason=excluded.error_reason, updated_at=excluded.updated_at
            """,
            (task_id, status, attempts, input_hash, output_hash, error_reason, _now()),
        )
        connection.commit()


def register_artifact(path: Path, kind: str, row_count: int | None, digest: str, schema_version: str = CONTRACT_VERSION) -> None:
    with open_state() as connection:
        connection.execute(
            """
            INSERT INTO artifact(artifact_path,artifact_kind,row_count,sha256,schema_version,status,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(artifact_path) DO UPDATE SET
              artifact_kind=excluded.artifact_kind,row_count=excluded.row_count,
              sha256=excluded.sha256,schema_version=excluded.schema_version,
              status=excluded.status,updated_at=excluded.updated_at
            """,
            (str(path), kind, row_count, digest, schema_version, "PUBLISHED", _now()),
        )
        connection.commit()


def set_meta(key: str, value: Any) -> None:
    with open_state() as connection:
        connection.execute(
            "INSERT INTO run_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, canonical_json(value) if not isinstance(value, str) else value),
        )
        connection.commit()


def stable_source_key(source_system: str, source_file: Any, source_record_id: Any) -> str:
    value = "|".join((_normalized(source_system), _normalized(source_file), _normalized(source_record_id)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact_text(value: Any, direct_values: Iterable[Any] = (), limit: int = 4_000) -> str:
    result = _text(value)
    values = sorted(
        {
            _text(item).strip()
            for item in direct_values
            if _text(item).strip()
            and (
                len(_text(item).strip()) >= 5
                or bool(re.fullmatch(r"[\u4e00-\u9fff·]{2,6}", _text(item).strip()))
            )
        },
        key=len,
        reverse=True,
    )
    for item in values:
        result = result.replace(item, "[REDACTED]")
    result = re.sub(r"\b\d{17}[\dXx]\b", "[REDACTED_ID]", result)
    result = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[REDACTED_PHONE]", result)
    result = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", result)
    result = re.sub(
        r"(患者姓名|姓名|身份证号|身份证|联系电话|联系方式|住院号|病案号|门诊号|床号|检查号|影像号|患者编号)"
        r"\s*[:：]?\s*[^\s,，。；;]{1,40}",
        r"\1：[REDACTED]",
        result,
    )
    result = re.sub(
        r"(现住址|户籍地址|家庭住址|住址|地址|家住)\s*[:：]?\s*[^\n，,。；;]{1,100}",
        r"\1：[REDACTED]",
        result,
    )
    result = re.sub(
        r"(?<=患者)([\u4e00-\u9fff·]{2,6})(?=[，,]\s*(?:男|女)(?:[，,]|\s))",
        "[REDACTED_NAME]",
        result,
    )
    result = re.sub(
        r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff·]{2,6})(?=[，,]\s*(?:男|女)[，,]\s*\d{1,3}岁)",
        "[REDACTED_NAME]",
        result,
    )
    result = re.sub(
        r"((?:经治|主治|住院|手术|麻醉|质控)?(?:医师|医生|护士)\s*[:：])\s*[\u4e00-\u9fff·]{2,6}",
        r"\1[REDACTED_STAFF]",
        result,
    )
    return result[:limit]


def terminology_inventory() -> dict[str, Any]:
    search_roots = [CODE_ROOT, DATA_ROOT / "指南摘要", DATA_ROOT / "terminology", DATA_ROOT / "术语", DATA_ROOT / "reference"]
    matches: dict[str, list[str]] = {"RadLex": [], "ICD-O": [], "TNM": []}
    patterns = {
        "RadLex": re.compile(r"radlex", re.I),
        "ICD-O": re.compile(r"icd[ _-]?o|icdo", re.I),
        "TNM": re.compile(r"tnm", re.I),
    }
    seen: set[str] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or str(path) in seen:
                continue
            seen.add(str(path))
            name = path.name
            for system, pattern in patterns.items():
                if pattern.search(name):
                    matches[system].append(str(path))
    missing = [system for system, paths in matches.items() if not paths]
    return {
        "terminology_status": "terminology_dependency_missing" if missing else "TERMINOLOGY_RESOURCES_FOUND",
        "resources": {key: sorted(value) for key, value in matches.items()},
        "missing_dependencies": missing,
        "radlex_mapping": "not_performed_without_authoritative_resource",
        "icdo_mapping": "not_performed_without_authoritative_resource",
        "tnm_mapping": "not_performed; only explicit source staging is allowed",
        "codes_invented": False,
        "rule_version": RULE_VERSION,
    }


def preflight() -> dict[str, Any]:
    if not STAGE7_PLAN.is_file():
        raise Stage8AError(f"missing_stage7_plan:{STAGE7_PLAN}")
    if not PATHOLOGY_RECORD_ROOT.is_dir():
        raise Stage8AError(f"missing_pathology_record:{PATHOLOGY_RECORD_ROOT}")
    if not IMAGING_ROOT.is_dir():
        raise Stage8AError(f"missing_imaging_clusters:{IMAGING_ROOT}")
    if not DOCUMENT_ROOT.is_dir():
        raise Stage8AError(f"missing_document_l1:{DOCUMENT_ROOT}")

    plan = _json_load(STAGE7_PLAN)
    source_event_counts = {key: int(value) for key, value in plan.get("source_event_counts", {}).items()}
    actual_stage7_events = int(plan.get("eligible_source_event_count", sum(source_event_counts.values())))
    pathology_count = parquet_row_count(PATHOLOGY_RECORD_ROOT)
    document_count = parquet_row_count(DOCUMENT_ROOT)
    actual_imaging_events = sum(
        pq.ParquetFile(path).metadata.num_rows
        for path in sorted(STAGE7_ROOT.glob("tasks/*/imaging_event/*.parquet"))
    )
    actual_patient_count = int(plan.get("cohort_patient_count", 0))
    differences = {
        "stage7_patient_count": {
            "baseline": BASELINE_STAGE7_PATIENTS,
            "actual": actual_patient_count,
            "delta": actual_patient_count - BASELINE_STAGE7_PATIENTS,
        },
        "stage7_event_count": {
            "baseline": BASELINE_STAGE7_EVENTS,
            "actual": actual_stage7_events,
            "delta": actual_stage7_events - BASELINE_STAGE7_EVENTS,
            "source": "full_plan_v1.json eligible_source_event_count and source_event_counts",
        },
        "pathology_linked_count": {
            "baseline": BASELINE_PATHOLOGY_LINKED,
            "actual": int(source_event_counts.get("pathology", 0)),
            "delta": int(source_event_counts.get("pathology", 0)) - BASELINE_PATHOLOGY_LINKED,
        },
        "imaging_event_count": {
            "baseline": BASELINE_IMAGING_EVENTS,
            "actual": actual_imaging_events,
            "delta": actual_imaging_events - BASELINE_IMAGING_EVENTS,
        },
    }
    terminology = terminology_inventory()
    manifest = {
        "manifest_version": "stage8a_input_manifest_v1",
        "contract_version": CONTRACT_VERSION,
        "rule_version": RULE_VERSION,
        "fixed_seed": FIXED_SEED,
        "sources": {
            "stage7_full_day_timeline": {
                "root": str(STAGE7_ROOT),
                "plan_file": str(STAGE7_PLAN),
                "plan_sha256": sha256_file(STAGE7_PLAN),
                "patient_count": actual_patient_count,
                "eligible_source_event_count": actual_stage7_events,
                "source_event_counts": source_event_counts,
            },
            "pathology_record": {
                "root": str(PATHOLOGY_RECORD_ROOT),
                "row_count": pathology_count,
                "files": file_inventory(PATHOLOGY_RECORD_ROOT),
            },
            "imaging_cluster_jsonl": {
                "root": str(IMAGING_ROOT),
                "file_count": len(list(IMAGING_ROOT.glob("*.jsonl"))),
                "files": file_inventory(IMAGING_ROOT, {".jsonl"}),
                "stage7_imaging_event_count": actual_imaging_events,
            },
            "document_l1": {
                "root": str(DOCUMENT_ROOT),
                "row_count": document_count,
                "files": file_inventory(DOCUMENT_ROOT, {".parquet"}),
            },
        },
        "baseline_differences": differences,
        "terminology_status": terminology["terminology_status"],
        "medical_fact_extraction_started": False,
    }
    terminology_path = AUDIT_ROOT / "stage8a_terminology_inventory.json"
    manifest_path = AUDIT_ROOT / "stage8a_input_manifest.json"
    contract_path = RESTRICTED_ROOT / "contracts" / "stage8a_contract.json"
    write_json_atomic(terminology_path, terminology)
    write_json_atomic(manifest_path, manifest)
    write_json_atomic(contract_path, contract_definition())
    for path, kind in [(terminology_path, "terminology"), (manifest_path, "input_manifest"), (contract_path, "contract")]:
        register_artifact(path, kind, None, sha256_file(path))
    set_meta("preflight", manifest)
    return {"manifest": manifest, "terminology": terminology}


def load_imaging_index() -> dict[str, dict[str, Any]]:
    columns = [
        "event_id",
        "patient_uid",
        "encounter_uid",
        "source_record_key",
        "source_file",
        "record_cluster_id",
        "cluster_status",
        "exam_method",
        "event_date",
    ]
    result = {}
    for row in iter_parquet_file_rows(STAGE7_ROOT.glob("tasks/*/imaging_event/*.parquet"), columns):
        key = _normalized(row.get("source_record_key"))
        if key:
            result[key] = row
    return result


def _imaging_text(tokens: list[Any]) -> str:
    selected = []
    for index in (6, 8, 13, 14):
        if index < len(tokens):
            selected.append(_text(tokens[index]))
    return "\n".join(selected)


def imaging_tags(method: str, body: str) -> tuple[str, list[str]]:
    text_value = f"{method}\n{body}"
    method_value = method.upper()
    if re.search(r"MRCP|胰胆管造影|磁共振胰胆管", text_value, re.I):
        category = "MRCP"
    elif re.search(r"介入|穿刺|引流|置管|消融|ERCP|EUS|内镜下", text_value, re.I):
        category = "介入及操作相关"
    elif re.search(r"胸部|肺部|肺CT|胸CT|肺MR", method_value + "\n" + body, re.I):
        category = "胸部或肺部"
    elif re.search(r"增强|CTA|增强CT|增强MR", method_value, re.I) and re.search(r"胰腺|胰头|胰体|胰尾|胰管|胰周", text_value):
        category = "胰腺增强CT/MR"
    elif re.search(r"腹部|上腹|肝胆|肝脏|胆道|胃|肠|消化|肾", method_value + "\n" + body, re.I):
        category = "其他腹部或消化系统"
    else:
        category = "其他部位"
    tags = []
    tag_patterns = {
        "术后": r"术后|术后改变|切除术后|手术后",
        "随访比较": r"复查|随访|较前|对比|比较",
        "否定": r"未见|未发现|无明显|未显示|未提示|未检出",
        "不确定": r"考虑|可能|不除外|待排|疑似|可疑|建议结合",
        "疑似转移": r"转移|肝转移|肺转移|骨转移|淋巴结转移",
        "血管侵犯": r"侵犯血管|血管受侵|包绕|累及.{0,8}(动脉|静脉)|门静脉|肠系膜上",
    }
    for tag, pattern in tag_patterns.items():
        if re.search(pattern, text_value, re.I):
            tags.append(tag)
    if not re.search(r"胰腺|胰头|胰体|胰尾|胰管|胰周|胰", text_value):
        tags.append("未直接提及胰腺")
    return category, tags


def scan_imaging_candidates() -> list[dict[str, Any]]:
    index = load_imaging_index()
    candidates: list[dict[str, Any]] = []
    for cluster_path in sorted(IMAGING_ROOT.glob("*.jsonl")):
        with cluster_path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise Stage8AError(f"imaging_jsonl_parse_error:{cluster_path.name}:{line_number}") from exc
                cluster_id = record.get("record_cluster_id")
                key = stable_source_key("imaging", cluster_path.name, cluster_id)
                indexed = index.get(key)
                if not indexed:
                    continue
                fragments = record.get("fragments") or []
                tokens = record.get("repaired_tokens")
                if not isinstance(tokens, list) or len(tokens) < 8:
                    tokens = fragments[0].get("raw_token_array") if fragments and isinstance(fragments[0], dict) else []
                tokens = list(tokens or [])
                method = _text(tokens[6]) if len(tokens) > 6 else _text(indexed.get("exam_method"))
                body = _imaging_text(tokens)
                category, tags = imaging_tags(method, body)
                status = _normalized(record.get("status") or indexed.get("cluster_status"))
                content_hash = sha256_text(canonical_json(tokens))
                patient_uid = _normalized(indexed.get("patient_uid"))
                event_uid = _normalized(indexed.get("event_id")) or build_event_uid(
                    patient_uid, key, "IMAGING_EXAM", indexed.get("event_date")
                )
                candidates.append(
                    {
                        "sample_type": "imaging",
                        "sample_uid": build_sample_uid("imaging", key),
                        "source_system": "imaging",
                        "source_record_key": key,
                        "source_file": str(record.get("source_file") or cluster_path),
                        "source_file_name": cluster_path.name,
                        "source_row": line_number,
                        "source_record_id": _text(cluster_id),
                        "record_cluster_id": cluster_id,
                        "patient_uid": patient_uid,
                        "event_uid": event_uid,
                        "encounter_uid": _normalized(indexed.get("encounter_uid")),
                        "report_uid": build_report_uid(patient_uid, event_uid, content_hash, "imaging"),
                        "content_hash": content_hash,
                        "report_type": "IMAGING_REPORT",
                        "status": status,
                        "category": category,
                        "coverage_tags": tags,
                        "event_date": _text(indexed.get("event_date")),
                        "exam_method": method,
                        "_annotation_text": redact_text(
                            "\n".join(_text(tokens[index]) for index in (8, 13, 14) if index < len(tokens)),
                            [tokens[index] for index in (0, 3, 4, 9, 10) if index < len(tokens)],
                        ),
                    }
                )
    return candidates


def _rank(seed: str, sample_uid: str) -> str:
    return hashlib.sha256(f"{seed}|{sample_uid}".encode("utf-8")).hexdigest()


def _select_by_rank(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: _rank(FIXED_SEED, row["sample_uid"]))[:count]


def _allocate_status_targets(rows: list[dict[str, Any]], category_targets: dict[str, int], status_targets: dict[str, int]) -> dict[tuple[str, str], int]:
    allocations: dict[tuple[str, str], int] = {}
    repaired_left = status_targets.get("repaired_high", 0)
    ordered_categories = list(category_targets)
    for category in ordered_categories:
        target = category_targets[category]
        available = len([row for row in rows if row["category"] == category and row["status"] == "repaired_high"])
        requested = min(available, round(target * status_targets.get("repaired_high", 0) / sum(category_targets.values())))
        allocations[(category, "repaired_high")] = requested
        repaired_left -= requested
    while repaired_left > 0:
        possible = [
            category
            for category in ordered_categories
            if allocations.get((category, "repaired_high"), 0)
            < len([row for row in rows if row["category"] == category and row["status"] == "repaired_high"])
        ]
        if not possible:
            break
        category = max(possible, key=lambda value: category_targets[value] - allocations.get((value, "repaired_high"), 0))
        allocations[(category, "repaired_high")] = allocations.get((category, "repaired_high"), 0) + 1
        repaired_left -= 1
    for category, target in category_targets.items():
        allocations[(category, "valid_15col")] = target - allocations.get((category, "repaired_high"), 0)
    return allocations


def select_imaging(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    allocations = _allocate_status_targets(rows, IMAGING_TARGETS, IMAGING_STATUS_TARGETS)
    selected: list[dict[str, Any]] = []
    shortages = []
    for (category, status), count in allocations.items():
        available = [row for row in rows if row["category"] == category and row["status"] == status]
        if len(available) < count:
            shortages.append({"category": category, "status": status, "required": count, "available": len(available)})
            continue
        selected.extend(_select_by_rank(available, count))
    if shortages:
        raise Stage8AError(f"imaging_sample_shortage:{json.dumps(shortages, ensure_ascii=False, sort_keys=True)}")
    selected_keys = {row["sample_uid"] for row in selected}
    for tag in ["术后", "随访比较", "否定", "不确定", "疑似转移", "血管侵犯", "未直接提及胰腺"]:
        if any(tag in row["coverage_tags"] for row in selected):
            continue
        candidates = [row for row in rows if tag in row["coverage_tags"] and row["sample_uid"] not in selected_keys]
        swapped = False
        for candidate in sorted(candidates, key=lambda row: _rank(FIXED_SEED + tag, row["sample_uid"])):
            replacements = [
                row
                for row in selected
                if row["category"] == candidate["category"]
                and row["status"] == candidate["status"]
                and tag not in row["coverage_tags"]
            ]
            if not replacements:
                replacements = [
                    row
                    for row in selected
                    if row["status"] == candidate["status"] and tag not in row["coverage_tags"]
                ]
            if replacements:
                old = sorted(replacements, key=lambda row: _rank(FIXED_SEED + "replace", row["sample_uid"]))[-1]
                selected.remove(old)
                selected.append(candidate)
                selected_keys.remove(old["sample_uid"])
                selected_keys.add(candidate["sample_uid"])
                swapped = True
                break
        if not swapped:
            raise Stage8AError(f"imaging_coverage_missing:{tag}")
    selected.sort(key=lambda row: (row["category"], row["status"], row["event_date"], row["sample_uid"]))
    return selected, {
        "category_counts": dict(Counter(row["category"] for row in selected)),
        "status_counts": dict(Counter(row["status"] for row in selected)),
        "coverage_counts": {
            tag: sum(tag in row["coverage_tags"] for row in selected)
            for tag in ["术后", "随访比较", "否定", "不确定", "疑似转移", "血管侵犯", "未直接提及胰腺"]
        },
    }


def pathology_candidates() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    columns = sorted(PATHOLOGY_COLUMNS)
    event_map = {
        _normalized(row.get("source_record_key")): row
        for row in iter_parquet_file_rows(
            STAGE7_ROOT.glob("tasks/*/pathology_event/*.parquet"),
            ["event_id", "patient_uid", "encounter_uid", "source_record_key", "event_date"],
        )
        if _normalized(row.get("source_record_key"))
    }
    linked: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for row in iter_parquet_rows(PATHOLOGY_RECORD_ROOT, columns):
        pathology_uid = _normalized(row.get("pathology_record_uid"))
        source_key = _normalized(row.get("canonical_source_record_key"))
        event = event_map.get(source_key)
        item = {
            "sample_type": "pathology",
            "sample_uid": build_sample_uid("pathology", source_key or pathology_uid),
            "source_system": "pathology",
            "source_record_key": source_key,
            "source_file": _text(row.get("source_file")),
            "source_file_name": Path(_text(row.get("source_file"))).name,
            "source_sheet": _text(row.get("source_sheet")),
            "source_row": row.get("source_row"),
            "source_record_id": pathology_uid,
            "pathology_record_uid": pathology_uid,
            "patient_uid": _normalized(event.get("patient_uid")) if event else "",
            "event_uid": _normalized(event.get("event_id")) if event else "",
            "encounter_uid": _normalized(event.get("encounter_uid")) if event else "",
            "event_date": _text(event.get("event_date")) if event else "",
            "report_uid": build_report_uid(
                _normalized(event.get("patient_uid")) if event else "",
                _normalized(event.get("event_id")) if event else pathology_uid,
                _text(row.get("content_hash")),
                "pathology",
            ),
            "content_hash": _text(row.get("content_hash")),
            "report_type": "PATHOLOGY_REPORT",
            "category": _text(row.get("disease_label")),
            "disease_labels": _text(row.get("disease_labels_json")),
            "pathology_time_sequence_conflict": bool(row.get("pathology_time_sequence_conflict")),
            "is_content_conflict": bool(row.get("is_content_conflict")),
            "_raw_values_json": _text(row.get("raw_values_json")),
        }
        all_rows.append(item)
        if event:
            linked.append(item)
    return linked, all_rows


def select_pathology(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = []
    shortages = []
    for category, count in PATHOLOGY_TARGETS.items():
        available = [row for row in rows if row["category"] == category]
        if len(available) < count:
            shortages.append({"category": category, "required": count, "available": len(available)})
        else:
            selected.extend(_select_by_rank(available, count))
    if shortages:
        raise Stage8AError(f"pathology_sample_shortage:{json.dumps(shortages, ensure_ascii=False, sort_keys=True)}")
    selected.sort(key=lambda row: (row["category"], row["event_date"], row["sample_uid"]))
    return selected, {"category_counts": dict(Counter(row["category"] for row in selected))}


def pathology_annotation_text(row: dict[str, Any]) -> str:
    try:
        values = json.loads(row.get("_raw_values_json") or "[]")
    except json.JSONDecodeError:
        values = []
    fields = [values[index] for index in (3, 8, 9, 22, 25, 26) if index < len(values)]
    direct = [values[index] for index in (0, 1, 11, 13, 18, 19) if index < len(values)]
    return redact_text("\n".join(_text(value) for value in fields), direct)


def load_document_index() -> dict[str, dict[str, Any]]:
    result = {}
    columns = ["event_id", "patient_uid", "encounter_uid", "source_record_key", "event_date"]
    for row in iter_parquet_file_rows(STAGE7_ROOT.glob("tasks/*/document_day_detail/*.parquet"), columns):
        key = _normalized(row.get("source_record_key"))
        if key:
            result[key] = row
    return result


def classify_document(name: str, content: str) -> str | None:
    text_value = f"{name}\n{content}"
    if re.search(r"取消|改期|延期|暂缓|未行|未实施|未做|拒绝|放弃|不愿|未予|未进行|手术未|未安排|撤销", text_value):
        return "取消、改期或未实施"
    if re.search(r"术后病程|术后第|术后首次|术后日常", name):
        return "术后病程"
    if re.search(r"术前|知情同意", name) and re.search(r"手术|胰腺|开腹|切除|ERCP|EUS", text_value):
        return "术前计划或知情同意"
    if "出院" in name and "手术" in text_value:
        return "出院记录中的手术信息"
    if re.search(r"手术记录|手术经过|手术名称|手术患者", name):
        return "明确手术记录"
    if "手术" in text_value:
        return "边界或难判样本"
    return None


def scan_document_candidates() -> list[dict[str, Any]]:
    document_index = load_document_index()
    candidates = []
    columns = ["source_file", "source_row", "source_record_id", "PATIENT_ID", "VISIT_ID", DOC_NAME, DOC_CONTENT]
    candidate_pattern = r"手术|术后|术前|知情同意|取消|改期|延期|暂缓|未行|未实施|未做|拒绝|放弃|不愿|未予|未进行|手术未|未安排|撤销"
    for parquet_path in sorted(DOCUMENT_ROOT.rglob("*.parquet")):
        parquet = pq.ParquetFile(parquet_path)
        for batch in parquet.iter_batches(batch_size=50_000, columns=columns):
            names = batch.column(batch.schema.get_field_index(DOC_NAME))
            contents = batch.column(batch.schema.get_field_index(DOC_CONTENT))
            name_hits = pc.match_substring_regex(names, pattern=candidate_pattern)
            content_hits = pc.match_substring_regex(contents, pattern=candidate_pattern)
            hit_flags = [left or right for left, right in zip(name_hits.to_pylist(), content_hits.to_pylist())]
            if not any(hit_flags):
                continue
            values = batch.to_pydict()
            for index, hit in enumerate(hit_flags):
                if not hit:
                    continue
                name = _text(values[DOC_NAME][index])
                content = document_text(values[DOC_CONTENT][index])
                category = classify_document(name, content)
                if not category:
                    continue
                source_file = _text(values["source_file"][index])
                source_record_id = _text(values["source_record_id"][index])
                source_key = stable_source_key("document", source_file, source_record_id)
                event = document_index.get(source_key)
                if not event:
                    continue
                content_hash = sha256_text(content)
                patient_uid = _normalized(event.get("patient_uid"))
                event_uid = _normalized(event.get("event_id"))
                candidates.append(
                    {
                        "sample_type": "surgery_document",
                        "sample_uid": build_sample_uid("surgery_document", source_key),
                        "source_system": "document",
                        "source_record_key": source_key,
                        "source_file": source_file,
                        "source_file_name": Path(source_file).name,
                        "source_row": values["source_row"][index],
                        "source_record_id": source_record_id,
                        "patient_uid": patient_uid,
                        "event_uid": event_uid,
                        "encounter_uid": _normalized(event.get("encounter_uid")),
                        "event_date": _text(event.get("event_date")),
                        "report_uid": build_report_uid(patient_uid, event_uid, content_hash, "document"),
                        "content_hash": content_hash,
                        "report_type": "SURGERY_RELATED_DOCUMENT",
                        "category": category,
                        "document_type": name,
                        "_annotation_text": redact_text(
                            content,
                            [name, values["PATIENT_ID"][index], values["VISIT_ID"][index]],
                        ),
                    }
                )
    return candidates


def select_documents(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = []
    shortages = []
    for category, count in SURGERY_TARGETS.items():
        available = [row for row in rows if row["category"] == category]
        if len(available) < count:
            shortages.append({"category": category, "required": count, "available": len(available)})
        else:
            selected.extend(_select_by_rank(available, count))
    if shortages:
        raise Stage8AError(f"surgery_document_sample_shortage:{json.dumps(shortages, ensure_ascii=False, sort_keys=True)}")
    selected.sort(key=lambda row: (row["category"], row["event_date"], row["sample_uid"]))
    return selected, {"category_counts": dict(Counter(row["category"] for row in selected))}


def public_sample_row(row: dict[str, Any], split: str = "") -> dict[str, Any]:
    result = {key: value for key, value in row.items() if not key.startswith("_")}
    result["split"] = split
    result["coverage_tags"] = ";".join(row.get("coverage_tags") or [])
    return result


SAMPLE_FIELDS = [
    "sample_uid",
    "sample_type",
    "category",
    "status",
    "split",
    "patient_uid",
    "event_uid",
    "encounter_uid",
    "report_uid",
    "source_system",
    "source_record_key",
    "source_file",
    "source_file_name",
    "source_sheet",
    "source_row",
    "source_record_id",
    "record_cluster_id",
    "pathology_record_uid",
    "event_date",
    "exam_method",
    "document_type",
    "disease_labels",
    "coverage_tags",
    "content_hash",
    "report_type",
]


def annotate_split(rows: list[dict[str, Any]], sample_type: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = _select_by_rank(rows, len(rows))
    development_count, lock_count = ANNOTATION_SPLITS[sample_type]
    development = ordered[:development_count]
    locked = ordered[development_count : development_count + lock_count]
    if len(locked) != lock_count:
        raise Stage8AError(f"annotation_split_shortage:{sample_type}")
    return development, locked


def annotation_rows(rows: list[dict[str, Any]], split: str) -> list[dict[str, Any]]:
    return [
        {
            "sample_uid": row["sample_uid"],
            "split": split,
            "sample_type": row["sample_type"],
            "category": row["category"],
            "patient_uid": row.get("patient_uid"),
            "event_uid": row.get("event_uid"),
            "report_uid": row.get("report_uid"),
            "source_record_key": row.get("source_record_key"),
            "content_hash": row.get("content_hash"),
            "label_status": "UNLABELED",
            "annotator_1_label": "",
            "annotator_2_label": "",
            "adjudication_status": "PENDING",
        }
        for row in rows
    ]


def write_annotation_material(sample_sets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    counts = {}
    lock_hashes = {}
    for sample_type, rows in sample_sets.items():
        development, locked = annotate_split(rows, sample_type)
        all_annotation = annotation_rows(development, "development") + annotation_rows(locked, "lock")
        annotation_path = ANNOTATION_ROOT / f"{sample_type}_double_blind.csv"
        digest = write_csv_atomic(
            annotation_path,
            [
                "sample_uid",
                "split",
                "sample_type",
                "category",
                "patient_uid",
                "event_uid",
                "report_uid",
                "source_record_key",
                "content_hash",
                "label_status",
                "annotator_1_label",
                "annotator_2_label",
                "adjudication_status",
            ],
            sorted(all_annotation, key=lambda row: (row["split"], row["sample_uid"])),
        )
        lock_rows = sorted(annotation_rows(locked, "lock"), key=lambda row: row["sample_uid"])
        lock_hash = sha256_text(canonical_json(lock_rows))
        counts[sample_type] = {"development": len(development), "lock": len(locked), "total": len(all_annotation), "file_sha256": digest}
        lock_hashes[sample_type] = lock_hash
        register_artifact(annotation_path, "annotation_table", len(all_annotation), digest)

        material_rows = []
        for row, split in [(item, "development") for item in development] + [(item, "lock") for item in locked]:
            material_rows.append(
                {
                    "sample_uid": row["sample_uid"],
                    "split": split,
                    "sample_type": row["sample_type"],
                    "category": row["category"],
                    "patient_uid": row.get("patient_uid"),
                    "event_uid": row.get("event_uid"),
                    "report_uid": row.get("report_uid"),
                    "source_record_key": row.get("source_record_key"),
                    "source_file": row.get("source_file"),
                    "source_row": row.get("source_row"),
                    "content_hash": row.get("content_hash"),
                    "deidentified_text_excerpt": row.get("_annotation_text") or pathology_annotation_text(row),
                }
            )
        material_path = ANNOTATION_ROOT / f"{sample_type}_annotation_material.jsonl"
        material_digest = write_jsonl_atomic(material_path, sorted(material_rows, key=lambda row: (row["split"], row["sample_uid"])))
        register_artifact(material_path, "restricted_annotation_material", len(material_rows), material_digest)
    instructions = """# Stage 8A双标说明

本材料用于建立后续医学事实抽取的人工金标准，不代表当前已经完成事实抽取。

标注时分别判断报告或文书中明确出现的事实、否定、疑问和不确定表述；不要根据常识补写原文没有明确表达的分期、部位、手术或转移信息。TNM只有在原文明确给出时才记录。不同标注者对同一事实的冲突保留在分歧仲裁表中。

锁定验收集只用于最终验收，不用于规则调参。正文材料位于受限目录，审计报告不包含正文。
"""
    instructions_path = ANNOTATION_ROOT / "annotation_instructions.md"
    instruction_digest = _write_bytes_atomic(instructions_path, instructions.encode("utf-8"))
    register_artifact(instructions_path, "annotation_instructions", None, instruction_digest)
    arbitration_path = ANNOTATION_ROOT / "disagreement_adjudication.csv"
    arbitration_digest = write_csv_atomic(
        arbitration_path,
        ["sample_uid", "fact_key", "annotator_1_position", "annotator_2_position", "adjudication_decision", "adjudicator_note"],
        [],
    )
    register_artifact(arbitration_path, "arbitration_template", 0, arbitration_digest)
    return {"counts": counts, "lock_hashes": lock_hashes}


def sample_run() -> dict[str, Any]:
    preflight_result = preflight()
    update_task("sample_pathology", "RUNNING")
    pathology_linked, pathology_all = pathology_candidates()
    selected_pathology, pathology_stats = select_pathology(pathology_linked)
    pathology_path = SAMPLE_ROOT / "pathology_sample.csv"
    pathology_digest = write_csv_atomic(pathology_path, SAMPLE_FIELDS, [public_sample_row(row) for row in selected_pathology])
    register_artifact(pathology_path, "pathology_sample", len(selected_pathology), pathology_digest)
    update_task("sample_pathology", "SUCCEEDED", output_hash=pathology_digest)

    update_task("sample_imaging", "RUNNING")
    imaging_candidates = scan_imaging_candidates()
    selected_imaging, imaging_stats = select_imaging(imaging_candidates)
    imaging_path = SAMPLE_ROOT / "imaging_sample.csv"
    imaging_digest = write_csv_atomic(imaging_path, SAMPLE_FIELDS, [public_sample_row(row) for row in selected_imaging])
    register_artifact(imaging_path, "imaging_sample", len(selected_imaging), imaging_digest)
    update_task("sample_imaging", "SUCCEEDED", output_hash=imaging_digest)

    update_task("sample_surgery_document", "RUNNING")
    document_candidates = scan_document_candidates()
    selected_documents, document_stats = select_documents(document_candidates)
    document_path = SAMPLE_ROOT / "surgery_document_sample.csv"
    document_digest = write_csv_atomic(document_path, SAMPLE_FIELDS, [public_sample_row(row) for row in selected_documents])
    register_artifact(document_path, "surgery_document_sample", len(selected_documents), document_digest)
    update_task("sample_surgery_document", "SUCCEEDED", output_hash=document_digest)

    boundary_rows = [row for row in pathology_all if not row.get("event_uid")]
    boundary_path = SAMPLE_ROOT / "pathology_boundary_pressure.csv"
    boundary_digest = write_csv_atomic(boundary_path, SAMPLE_FIELDS, [public_sample_row(row) for row in boundary_rows])
    register_artifact(boundary_path, "pathology_boundary_pressure", len(boundary_rows), boundary_digest)

    sample_sets = {
        "pathology": selected_pathology,
        "imaging": selected_imaging,
        "surgery_document": selected_documents,
    }
    annotation = write_annotation_material(sample_sets)
    all_sample_rows = [public_sample_row(row) for rows in sample_sets.values() for row in rows]
    sample_index_path = SAMPLE_ROOT / "stage8a_sample_index.csv"
    sample_index_digest = write_csv_atomic(
        sample_index_path,
        SAMPLE_FIELDS,
        sorted(all_sample_rows, key=lambda row: (row["sample_type"], row["category"], row["sample_uid"])),
    )
    register_artifact(sample_index_path, "sample_index", len(all_sample_rows), sample_index_digest)

    report = {
        "report_version": "stage8a_preparation_report_v1",
        "contract_version": CONTRACT_VERSION,
        "rule_version": RULE_VERSION,
        "fixed_seed": FIXED_SEED,
        "stage8a_preparation_completed": True,
        "medical_fact_extraction_started": False,
        "sample_counts": {
            "pathology": len(selected_pathology),
            "imaging": len(selected_imaging),
            "surgery_document": len(selected_documents),
            "pathology_boundary_pressure": len(boundary_rows),
        },
        "sample_stats": {
            "pathology": pathology_stats,
            "imaging": imaging_stats,
            "surgery_document": document_stats,
        },
        "annotation": annotation,
        "artifact_sha256": {
            "sample_index": sample_index_digest,
            "pathology_sample": pathology_digest,
            "imaging_sample": imaging_digest,
            "surgery_document_sample": document_digest,
            "pathology_boundary_pressure": boundary_digest,
        },
        "baseline_differences": preflight_result["manifest"]["baseline_differences"],
        "terminology_status": preflight_result["terminology"]["terminology_status"],
        "no_patient_fact_output": True,
    }
    report_path = AUDIT_ROOT / "stage8a_preparation_report.json"
    report_digest = write_json_atomic(report_path, report)
    register_artifact(report_path, "preparation_report", None, report_digest)
    update_task("annotation_material", "SUCCEEDED", output_hash=annotation["lock_hashes"].get("pathology", ""))
    return report


def validate() -> dict[str, Any]:
    expected = {"pathology": 400, "imaging": 600, "surgery_document": 120}
    sample_files = {
        key: SAMPLE_ROOT / filename
        for key, filename in {
            "pathology": "pathology_sample.csv",
            "imaging": "imaging_sample.csv",
            "surgery_document": "surgery_document_sample.csv",
        }.items()
    }
    all_rows = []
    errors = []
    parsed: dict[str, list[dict[str, Any]]] = {}
    for kind, path in sample_files.items():
        if not path.is_file():
            errors.append(f"missing_sample:{kind}")
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        parsed[kind] = rows
        all_rows.extend(rows)
        if len(rows) != expected[kind]:
            errors.append(f"sample_count:{kind}:{len(rows)}!={expected[kind]}")
    sample_uids = [row.get("sample_uid", "") for row in all_rows]
    if len(sample_uids) != len(set(sample_uids)):
        errors.append("duplicate_sample_uid")
    for kind, rows in parsed.items():
        required = PATHOLOGY_TARGETS if kind == "pathology" else IMAGING_TARGETS if kind == "imaging" else SURGERY_TARGETS
        counts = Counter(row.get("category", "") for row in rows)
        for category, count in required.items():
            if counts.get(category, 0) != count:
                errors.append(f"stratum_count:{kind}:{category}:{counts.get(category, 0)}!={count}")
    imaging_status = Counter(row.get("status", "") for row in parsed.get("imaging", []))
    for status, count in IMAGING_STATUS_TARGETS.items():
        if imaging_status.get(status, 0) != count:
            errors.append(f"imaging_status_count:{status}:{imaging_status.get(status, 0)}!={count}")
    split_sets: dict[str, set[str]] = {}
    lock_hashes = {}
    for kind, filename in {
        "pathology": "pathology_double_blind.csv",
        "imaging": "imaging_double_blind.csv",
        "surgery_document": "surgery_document_double_blind.csv",
    }.items():
        path = ANNOTATION_ROOT / filename
        if not path.is_file():
            errors.append(f"missing_annotation:{kind}")
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        development = {row["sample_uid"] for row in rows if row.get("split") == "development"}
        locked = {row["sample_uid"] for row in rows if row.get("split") == "lock"}
        if development & locked:
            errors.append(f"annotation_overlap:{kind}")
        split_sets[kind] = development | locked
        lock_rows = sorted((row for row in rows if row.get("split") == "lock"), key=lambda row: row["sample_uid"])
        lock_hashes[kind] = sha256_text(canonical_json(lock_rows))
        expected_dev, expected_lock = ANNOTATION_SPLITS[kind]
        if len(development) != expected_dev or len(locked) != expected_lock:
            errors.append(f"annotation_count:{kind}:{len(development)}/{len(locked)}")
    if any(split_sets.values()):
        for first, values in split_sets.items():
            for second, other in split_sets.items():
                if first < second and values & other:
                    errors.append(f"cross_type_annotation_overlap:{first}:{second}")
    terminology = _json_load(AUDIT_ROOT / "stage8a_terminology_inventory.json")
    report = {
        "validation_version": "stage8a_validation_report_v1",
        "passed": not errors,
        "errors": errors,
        "sample_counts": {kind: len(rows) for kind, rows in parsed.items()},
        "annotation_lock_hashes": lock_hashes,
        "terminology_status": terminology.get("terminology_status"),
        "medical_fact_extraction_started": False,
        "partial_files": [str(path) for path in OUTPUT_ROOT.rglob("*.partial")],
        "audit_contains_pii": False,
    }
    path = AUDIT_ROOT / "stage8a_validation_report.json"
    digest = write_json_atomic(path, report)
    register_artifact(path, "validation_report", None, digest)
    return report


def finalize_report(preflight_result: dict[str, Any], sample_report: dict[str, Any], validation_report: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    if not validation_report.get("passed"):
        blockers.append("sample_or_annotation_validation_failed")
    if preflight_result["terminology"].get("missing_dependencies"):
        blockers.append("terminology_dependency_missing")
    final = {
        "report_version": "stage8a_final_acceptance_v1",
        "stage8a_status": "8A_GO" if not blockers else "8A_NO_GO",
        "blocking_reasons": blockers,
        "contract_version": CONTRACT_VERSION,
        "rule_version": RULE_VERSION,
        "fixed_seed": FIXED_SEED,
        "sample_counts": sample_report.get("sample_counts"),
        "sample_stats": sample_report.get("sample_stats"),
        "annotation": sample_report.get("annotation"),
        "baseline_differences": preflight_result["manifest"].get("baseline_differences"),
        "terminology_status": preflight_result["terminology"],
        "validation": validation_report,
        "medical_fact_extraction_started": False,
        "stage8_canary_started": False,
        "full_extraction_started": False,
        "patient_fact_output_generated": False,
    }
    path = AUDIT_ROOT / "stage8a_final_acceptance.json"
    digest = write_json_atomic(path, final)
    register_artifact(path, "final_acceptance", None, digest)
    update_task("validation", "SUCCEEDED" if validation_report.get("passed") else "FAILED", output_hash=digest)
    return final


def run_all() -> dict[str, Any]:
    preflight_result = preflight()
    sample_report = sample_run()
    validation_report = validate()
    return finalize_report(preflight_result, sample_report, validation_report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 8A preparation only; no batch fact extraction")
    parser.add_argument("command", choices=["preflight", "sample", "validate", "run-all"])
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight()
        elif args.command == "sample":
            result = sample_run()
        elif args.command == "validate":
            result = validate()
        else:
            result = run_all()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if not isinstance(result, dict) or result.get("passed", True) or result.get("stage8a_status") == "8A_NO_GO" else 1
    except Exception as exc:
        print(f"stage8a_error:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

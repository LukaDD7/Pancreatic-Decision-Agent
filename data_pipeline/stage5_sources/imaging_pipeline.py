"""Manifest-driven orchestration for the imaging clustering pipeline.

The first dry-run may scan ``*.csv`` to create an immutable manifest.  All
later processing uses only paths stored in that manifest.  Patient-level JSONL
is opt-in and is always kept in a directory separate from audit JSON.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .imaging_field_quality import apply_field_quality
from .imaging_overlong_repair import repair_route
from .imaging_record_cluster import is_valid_record_head, iter_imaging_record_clusters
from .imaging_safe_split import (
    REPAIRED_HIGH,
    REPAIRED_REVIEW,
    UNRESOLVED,
    VALID_15COL,
    iter_safe_routes,
)


MANIFEST_VERSION = "imaging_manifest_v1"
SUPPORTED_MANIFEST_VERSIONS = {MANIFEST_VERSION, "imaging_manifest_v2"}
RULES_VERSION = "imaging_rules_v2"
PILOT_FILE_NAMES = {
    "A001.csv",
    "A002.csv",
    "A019.csv",
    "A031.csv",
    "A045.csv",
    "A056.csv",
    "A371.csv",
}
PIPELINE_STAGES = [
    "record_cluster",
    "safe_15col_split",
    "overlong_fragment_repair",
    "field_value_quality",
]
PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
QUARANTINED = "QUARANTINED"
STATE_VALUES = {PENDING, RUNNING, SUCCEEDED, FAILED, QUARANTINED}
CSV_NAME_RE = re.compile(r"^A\d{3}\.csv$")


class PipelineStop(RuntimeError):
    """A condition that pauses the whole pipeline."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return size, digest.hexdigest()


def _token_hash_update(digest, tokens: Sequence[str]) -> None:
    digest.update(len(tokens).to_bytes(8, "little"))
    for token in tokens:
        encoded = token.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
    digest.update(b"\xff")


def _canonical(path: str | Path) -> str:
    return str(Path(path).resolve(strict=False))


def scan_csv_manifest(
    input_dir: str | Path,
    *,
    expected_count: int = 251,
    validate_pilot_shape: bool = True,
    validate_number_bounds: bool = True,
    manifest_version: str = MANIFEST_VERSION,
) -> dict[str, object]:
    """Scan actual CSV entries once and build an immutable inventory.

    The discovered set is used directly.  Numeric ranges are only reported as
    diagnostics and are never used to synthesize missing filenames.
    """

    root = Path(input_dir).resolve()
    paths = sorted(root.glob("*.csv"), key=lambda path: path.name)
    names = [path.name for path in paths]
    if len(paths) != expected_count:
        raise ValueError(f"expected exactly {expected_count} CSV files, found {len(paths)}")
    if len(set(names)) != len(names):
        raise ValueError("duplicate CSV filenames discovered")
    if validate_pilot_shape and not all(CSV_NAME_RE.fullmatch(name) for name in names):
        raise ValueError("discovered CSV filename outside A###.csv convention")

    numeric_ids = {int(name[1:4]) for name in names if CSV_NAME_RE.fullmatch(name)}
    if validate_pilot_shape and validate_number_bounds and (
        not numeric_ids or min(numeric_ids) != 1 or max(numeric_ids) != 371
    ):
        raise ValueError("discovered A###.csv set must span the actual A001.csv to A371.csv bounds")
    missing_numeric_ids = sorted(set(range(1, 372)) - numeric_ids)
    records = []
    for path in paths:
        size, sha256 = file_sha256(path)
        records.append(
            {
                "file_name": path.name,
                "absolute_path": str(path.resolve()),
                "size": size,
                "sha256": sha256,
                "is_pilot": path.name in PILOT_FILE_NAMES,
                "current_processing_status": PENDING,
                "completed_stages": [],
                "output_file": None,
                "error_reason": None,
            }
        )
    return {
        "manifest_version": manifest_version,
        "created_at": utc_now(),
        "input_directory": str(root),
        "file_count": len(records),
        "file_names_are_discovered_set": True,
        "numeric_range_used_to_generate_names": False,
        "missing_numeric_ids_diagnostic": missing_numeric_ids,
        "files": records,
    }


def write_immutable_manifest(manifest: dict[str, object], manifest_path: str | Path) -> Path:
    path = Path(manifest_path).resolve()
    if path.exists():
        raise FileExistsError(f"immutable manifest already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n", closefd=True) as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp_path.replace(path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    return path


def load_manifest(manifest_path: str | Path) -> dict[str, object]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("manifest_version") not in SUPPORTED_MANIFEST_VERSIONS:
        raise ValueError("unsupported or mutable manifest version")
    files = manifest.get("files")
    if not isinstance(files, list) or manifest.get("file_count") != len(files):
        raise ValueError("manifest file count is inconsistent")
    if len({record["file_name"] for record in files}) != len(files):
        raise ValueError("manifest contains duplicate file names")
    return manifest


def init_state_db(state_db: str | Path, manifest: dict[str, object]) -> Path:
    path = Path(state_db).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS file_state (
                file_name TEXT PRIMARY KEY,
                absolute_path TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                is_pilot INTEGER NOT NULL,
                status TEXT NOT NULL,
                completed_stages TEXT NOT NULL,
                output_file TEXT,
                audit_output_file TEXT,
                error_reason TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_event (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                event_type TEXT NOT NULL,
                file_name TEXT,
                detail TEXT
            )
            """
        )
        existing = {
            row[0]
            for row in connection.execute("SELECT file_name FROM file_state").fetchall()
        }
        for record in manifest["files"]:
            if record["file_name"] in existing:
                continue
            connection.execute(
                """
                INSERT INTO file_state
                (file_name, absolute_path, manifest_sha256, file_size, is_pilot,
                 status, completed_stages, output_file, audit_output_file,
                 error_reason, attempts, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?)
                """,
                (
                    record["file_name"],
                    record["absolute_path"],
                    record["sha256"],
                    record["size"],
                    int(record["is_pilot"]),
                    PENDING,
                    json.dumps([], ensure_ascii=False),
                    utc_now(),
                ),
            )
        manifest_names = {record["file_name"] for record in manifest["files"]}
        if existing - manifest_names:
            raise ValueError("state database contains files outside immutable manifest")
    return path


def recover_interrupted(state_db: str | Path) -> int:
    with sqlite3.connect(str(Path(state_db))) as connection:
        cursor = connection.execute(
            "UPDATE file_state SET status=?, error_reason=?, updated_at=? WHERE status=?",
            (PENDING, "recovered from interrupted RUNNING state", utc_now(), RUNNING),
        )
        connection.execute(
            "INSERT INTO pipeline_event(event_time,event_type,detail) VALUES(?,?,?)",
            (utc_now(), "RECOVER", f"reset {cursor.rowcount} RUNNING files to PENDING"),
        )
        return cursor.rowcount


def seed_pilot_state_from_report(
    state_db: str | Path,
    manifest: dict[str, object],
    pilot_report: str | Path | None,
) -> int:
    """Mark only SHA-verified prior pilot outputs as already completed."""

    if pilot_report is None:
        return 0
    report_path = Path(pilot_report).resolve()
    if not report_path.exists():
        return 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_files = {
        item["source_csv_name"]: item
        for item in report.get("files", [])
        if isinstance(item, dict) and item.get("source_csv_name")
    }
    manifest_by_name = {item["file_name"]: item for item in manifest["files"]}
    pilot_names = {name for name in manifest_by_name if manifest_by_name[name]["is_pilot"]}
    if pilot_names != set(PILOT_FILE_NAMES) or not pilot_names.issubset(report_files):
        raise ValueError("pilot report does not cover exactly the seven configured pilot files")
    for name in pilot_names:
        if report_files[name].get("sha256") != manifest_by_name[name]["sha256"]:
            raise ValueError(f"pilot report SHA-256 does not match manifest for {name}")

    seeded = 0
    with sqlite3.connect(str(Path(state_db))) as connection:
        for name in sorted(pilot_names):
            cursor = connection.execute(
                """
                UPDATE file_state
                SET status=?, completed_stages=?, output_file=?, audit_output_file=?, error_reason=NULL, updated_at=?
                WHERE file_name=? AND status IN (?,?)
                """,
                (
                    SUCCEEDED,
                    json.dumps(PIPELINE_STAGES, ensure_ascii=False),
                    str(report_path),
                    str(report_path),
                    utc_now(),
                    name,
                    PENDING,
                    FAILED,
                ),
            )
            seeded += cursor.rowcount
        connection.execute(
            "INSERT INTO pipeline_event(event_time,event_type,detail) VALUES(?,?,?)",
            (utc_now(), "SEED_PILOT", f"seeded {seeded} SHA-verified pilot files from {report_path}"),
        )
    return seeded


def build_size_batches(
    manifest: dict[str, object],
    *,
    min_bytes: int = 250 * 1024 * 1024,
    max_bytes: int = 350 * 1024 * 1024,
) -> list[dict[str, object]]:
    if min_bytes <= 0 or max_bytes < min_bytes:
        raise ValueError("invalid batch size bounds")
    batches: list[dict[str, object]] = []
    current: list[dict[str, object]] = []
    current_size = 0
    for record in manifest["files"]:
        size = int(record["size"])
        if current and current_size + size > max_bytes:
            batches.append(
                {
                    "batch_id": len(batches) + 1,
                    "file_names": [item["file_name"] for item in current],
                    "total_size": current_size,
                    "within_target_range": min_bytes <= current_size <= max_bytes,
                }
            )
            current = []
            current_size = 0
        current.append(record)
        current_size += size
    if current:
        batches.append(
            {
                "batch_id": len(batches) + 1,
                "file_names": [item["file_name"] for item in current],
                "total_size": current_size,
                "within_target_range": min_bytes <= current_size <= max_bytes,
            }
        )
    return batches


def _state_row(state_db: Path, file_name: str) -> tuple:
    with sqlite3.connect(str(state_db)) as connection:
        row = connection.execute(
            "SELECT file_name, absolute_path, manifest_sha256, file_size, is_pilot, status, completed_stages, output_file, audit_output_file, error_reason, attempts FROM file_state WHERE file_name=?",
            (file_name,),
        ).fetchone()
    if row is None:
        raise ValueError(f"file is not in state database: {file_name}")
    return row


def _set_running(state_db: Path, record: dict[str, object]) -> None:
    with sqlite3.connect(str(state_db)) as connection:
        connection.execute(
            "UPDATE file_state SET status=?, error_reason=NULL, attempts=attempts+1, updated_at=? WHERE file_name=? AND status IN (?,?)",
            (RUNNING, utc_now(), record["file_name"], PENDING, FAILED),
        )


def _set_finished(
    state_db: Path,
    file_name: str,
    status: str,
    completed_stages: list[str],
    output_file: Optional[str],
    audit_output_file: Optional[str],
    error_reason: Optional[str],
) -> None:
    if status not in STATE_VALUES:
        raise ValueError(status)
    with sqlite3.connect(str(state_db)) as connection:
        connection.execute(
            "UPDATE file_state SET status=?, completed_stages=?, output_file=?, audit_output_file=?, error_reason=?, updated_at=? WHERE file_name=?",
            (
                status,
                json.dumps(completed_stages, ensure_ascii=False),
                output_file,
                audit_output_file,
                error_reason,
                utc_now(),
                file_name,
            ),
        )


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n", closefd=True) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp_path.replace(path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def _output_record(route, quality) -> dict[str, object]:
    return {
        "source_file": route.source_file,
        "record_cluster_id": route.record_cluster_id,
        "status": quality.status,
        "fragment_count": route.fragment_count,
        "parsed_column_counts": route.parsed_column_counts,
        "fragments": [fragment.to_dict() for fragment in route.fragments],
        "quality_errors": quality.quality_errors,
        "repaired_tokens": quality.final_tokens,
    }


def _validate_jsonl_output(path: Path, expected_records: int, expected_token_hash: str) -> None:
    count = 0
    digest = hashlib.blake2b(digest_size=32)
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            count += 1
            for fragment in record["fragments"]:
                _token_hash_update(digest, fragment["raw_token_array"])
    if count != expected_records or digest.hexdigest() != expected_token_hash:
        raise PipelineStop("patient-level output record count or token hash validation failed")


def _process_file(
    record: dict[str, object],
    *,
    state_db: Path,
    audit_dir: Path,
    patient_dir: Optional[Path],
) -> dict[str, object]:
    path = Path(record["absolute_path"])
    audit_path = audit_dir / f"{path.name}.audit.json"
    patient_path = patient_dir / f"{path.name}.record_clusters.jsonl" if patient_dir else None
    completed: list[str] = []
    audit_temp: Optional[Path] = None
    patient_temp: Optional[Path] = None
    _set_running(state_db, record)
    try:
        size, sha256 = file_sha256(path)
        if size != record["size"] or sha256 != record["sha256"]:
            raise PipelineStop("source file size or SHA-256 differs from immutable manifest")

        counts = Counter()
        route_counts = Counter()
        fragment_count = 0
        cluster_count = 0
        max_fragment_count = 0
        multi_fragment_count = 0
        quality_error_count = 0
        valid_15col_false_head_negative_count = 0
        digest = hashlib.blake2b(digest_size=32)
        patient_stream = None
        if patient_path is not None:
            patient_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=patient_path.name + ".", suffix=".tmp", dir=str(patient_path.parent)
            )
            patient_temp = Path(temp_name)
            patient_stream = open(fd, "w", encoding="utf-8", newline="\n", closefd=True)

        for cluster in iter_imaging_record_clusters(path):
            cluster_count += 1
            route = next(iter_safe_routes([cluster]))
            proposal = repair_route(route)
            quality = apply_field_quality(proposal)
            route_counts[route.status] += 1
            if route.status == VALID_15COL and not is_valid_record_head(route.fragments[0].raw_token_array):
                valid_15col_false_head_negative_count += 1
            counts[quality.status] += 1
            quality_error_count += int(bool(quality.quality_errors))
            fragment_count += route.fragment_count
            max_fragment_count = max(max_fragment_count, route.fragment_count)
            multi_fragment_count += int(route.fragment_count > 1)
            head_count = sum(is_valid_record_head(fragment.raw_token_array) for fragment in route.fragments)
            if head_count > 1:
                raise PipelineStop("two valid record heads were merged into one cluster")
            for fragment in route.fragments:
                if fragment.parsed_column_count != len(fragment.raw_token_array):
                    raise PipelineStop("parsed column count and raw token count differ")
                _token_hash_update(digest, fragment.raw_token_array)
            if patient_stream is not None:
                json.dump(_output_record(route, quality), patient_stream, ensure_ascii=False, separators=(",", ":"))
                patient_stream.write("\n")

        if patient_stream is not None:
            patient_stream.close()
            patient_stream = None
            assert patient_temp is not None and patient_path is not None
            _validate_jsonl_output(patient_temp, cluster_count, digest.hexdigest())
            patient_temp.replace(patient_path)
            patient_temp = None

        audit_payload = {
            "source_file": str(path),
            "source_csv_name": path.name,
            "file_size": size,
            "sha256": sha256,
            "status_counts": dict(counts),
            "route_status_counts": dict(route_counts),
            "parsed_fragment_count": fragment_count,
            "record_cluster_count": cluster_count,
            "fragment_count_sum": fragment_count,
            "multi_fragment_cluster_count": multi_fragment_count,
            "max_fragment_count": max_fragment_count,
            "quality_error_count": quality_error_count,
            "valid_15col_false_head_negative_count": valid_15col_false_head_negative_count,
            "rules_version": RULES_VERSION,
            "token_conservation": True,
            "fragment_provenance": True,
            "completed_stages": PIPELINE_STAGES,
        }
        _write_json_atomic(audit_path, audit_payload)
        completed = list(PIPELINE_STAGES)
        output_file = str(patient_path) if patient_path is not None else str(audit_path)
        _set_finished(state_db, path.name, SUCCEEDED, completed, output_file, str(audit_path), None)
        return {"file_name": path.name, "status": SUCCEEDED, "audit": audit_payload}
    except PipelineStop as exc:
        if patient_stream is not None:
            patient_stream.close()
        if patient_temp is not None and patient_temp.exists():
            patient_temp.unlink()
        _set_finished(state_db, path.name, QUARANTINED, completed, None, None, str(exc))
        return {"file_name": path.name, "status": QUARANTINED, "error": str(exc), "system_stop": True}
    except Exception as exc:
        if patient_stream is not None:
            patient_stream.close()
        if patient_temp is not None and patient_temp.exists():
            patient_temp.unlink()
        is_system_error = isinstance(exc, (UnicodeDecodeError, ValueError, sqlite3.Error))
        status = QUARANTINED if is_system_error else FAILED
        _set_finished(state_db, path.name, status, completed, None, None, f"{type(exc).__name__}: {exc}")
        return {
            "file_name": path.name,
            "status": status,
            "error": f"{type(exc).__name__}: {exc}",
            "system_stop": is_system_error,
        }


def run_pipeline(
    manifest_path: str | Path,
    state_db: str | Path,
    output_root: str | Path,
    *,
    workers: int = 1,
    patient_output_dir: str | Path | None = None,
    pilot_report: str | Path | None = None,
    retry_failed: bool = False,
    failure_ratio_threshold: float = 0.10,
    max_consecutive_system_errors: int = 3,
) -> dict[str, object]:
    if workers not in (1, 2):
        raise ValueError("workers must be 1 or 2")
    if not 0 < failure_ratio_threshold <= 1:
        raise ValueError("failure_ratio_threshold must be in (0, 1]")
    manifest = load_manifest(manifest_path)
    state_path = init_state_db(state_db, manifest)
    if pilot_report is None and manifest.get("manifest_version") == MANIFEST_VERSION:
        default_pilot_report = Path(manifest_path).resolve().parent / "pilot_artifacts_v1" / "pilot_repair_report.json"
        pilot_report = default_pilot_report if default_pilot_report.exists() else None
    seed_pilot_state_from_report(state_path, manifest, pilot_report)
    recover_interrupted(state_path)
    audit_dir = Path(output_root).resolve() / "audit"
    patient_dir = Path(patient_output_dir).resolve() if patient_output_dir else None
    if patient_dir is not None and patient_dir == audit_dir.resolve():
        raise ValueError("audit and patient-level output directories must be separate")
    audit_dir.mkdir(parents=True, exist_ok=True)

    records = list(manifest["files"])
    batches = build_size_batches(manifest)
    results: list[dict[str, object]] = []
    processed = 0
    failures = 0
    consecutive_system_errors = 0
    stopped = False

    for batch in batches:
        batch_records = [record for record in records if record["file_name"] in batch["file_names"]]
        pending_records = []
        for record in batch_records:
            state = _state_row(state_path, record["file_name"])
            if state[5] == SUCCEEDED:
                continue
            if state[5] in (FAILED, QUARANTINED) and not retry_failed:
                continue
            pending_records.append(record)
        if workers == 1:
            batch_results = []
            for record in pending_records:
                result = _process_file(
                    record,
                    state_db=state_path,
                    audit_dir=audit_dir,
                    patient_dir=patient_dir,
                )
                batch_results.append(result)
                if result.get("system_stop"):
                    break
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _process_file,
                        record,
                        state_db=state_path,
                        audit_dir=audit_dir,
                        patient_dir=patient_dir,
                    )
                    for record in pending_records
                ]
                batch_results = [future.result() for future in futures]
        for result in batch_results:
            results.append(result)
            processed += 1
            if result["status"] != SUCCEEDED:
                failures += 1
            if result.get("system_stop"):
                consecutive_system_errors += 1
                stopped = True
            else:
                consecutive_system_errors = 0
        write_total_report(manifest_path, state_path, output_root, pilot_report)
        if stopped or consecutive_system_errors >= max_consecutive_system_errors:
            break
        if processed and failures / processed > failure_ratio_threshold:
            stopped = True
            break

    total_report = write_total_report(manifest_path, state_path, output_root, pilot_report)
    return {
        "manifest_version": manifest["manifest_version"],
        "batches": batches,
        "processed_result_count": len(results),
        "skipped_succeeded_count": len(records) - processed,
        "stopped": stopped,
        "results": results,
        "state_db": str(state_path),
        "audit_output_dir": str(audit_dir),
        "patient_output_dir": str(patient_dir) if patient_dir else None,
        "total_report": str(total_report),
    }


def status_report(state_db: str | Path) -> dict[str, object]:
    with sqlite3.connect(str(Path(state_db))) as connection:
        rows = connection.execute(
            "SELECT file_name, status, completed_stages, output_file, audit_output_file, error_reason, attempts FROM file_state ORDER BY file_name"
        ).fetchall()
    return {
        "file_count": len(rows),
        "status_counts": dict(Counter(row[1] for row in rows)),
        "files": [
            {
                "file_name": row[0],
                "status": row[1],
                "completed_stages": json.loads(row[2]),
                "output_file": row[3],
                "audit_output_file": row[4],
                "error_reason": row[5],
                "attempts": row[6],
            }
            for row in rows
        ],
    }


def build_total_report(
    manifest_path: str | Path,
    state_db: str | Path,
    output_root: str | Path,
    pilot_report: str | Path | None = None,
) -> dict[str, object]:
    """Build a patient-free, manifest/state/audit reconciliation report."""

    manifest = load_manifest(manifest_path)
    states = status_report(state_db)
    state_by_name = {item["file_name"]: item for item in states["files"]}
    pilot_by_name: dict[str, dict[str, object]] = {}
    if pilot_report is not None and Path(pilot_report).exists():
        pilot_payload = json.loads(Path(pilot_report).read_text(encoding="utf-8"))
        pilot_by_name = {item["source_csv_name"]: item for item in pilot_payload.get("files", [])}

    audit_dir = Path(output_root).resolve() / "audit"
    file_summaries: list[dict[str, object]] = []
    final_status_counts: Counter[str] = Counter()
    route_status_counts: Counter[str] = Counter()
    audit_missing: list[str] = []
    hash_mismatches: list[str] = []

    for record in manifest["files"]:
        name = record["file_name"]
        state = state_by_name.get(name)
        if state is None:
            file_summaries.append({"file_name": name, "processing_status": "MISSING_STATE"})
            continue
        summary: dict[str, object] = {
            "file_name": name,
            "processing_status": state["status"],
            "completed_stages": state["completed_stages"],
            "error_reason": state["error_reason"],
        }
        if state["status"] == SUCCEEDED:
            if name in pilot_by_name:
                item = pilot_by_name[name]
                summary.update(
                    {
                        "file_size": item["file_size"],
                        "sha256": item["sha256"],
                        "status_counts": item["final_status_counts"],
                        "route_status_counts": item["route_status_counts"],
                        "parsed_fragment_count": item["fragment_count"],
                        "record_cluster_count": item["record_cluster_count"],
                        "fragment_count_sum": item["fragment_count"],
                        "multi_fragment_cluster_count": item["multi_fragment_cluster_count"],
                        "max_fragment_count": item["max_fragment_count"],
                        "quality_error_count": item["quality_error_count"],
                        "token_conservation": item["fragment_count_conservation"],
                        "fragment_provenance": item["token_and_fragment_traceable"],
                        "normal_15col_zero_modification": item["normal_15col_zero_modification"],
                        "valid_15col_false_head_negative_count": item.get(
                            "valid_15col_false_head_negative_count", 0
                        ),
                    }
                )
            else:
                audit_path = Path(state["audit_output_file"] or audit_dir / f"{name}.audit.json")
                if not audit_path.exists():
                    audit_missing.append(name)
                else:
                    item = json.loads(audit_path.read_text(encoding="utf-8"))
                    summary.update(item)
                    summary["normal_15col_zero_modification"] = True
                    if item.get("sha256") != record["sha256"]:
                        hash_mismatches.append(name)
            for key, value in summary.get("status_counts", {}).items():
                final_status_counts[key] += value
            for key, value in summary.get("route_status_counts", {}).items():
                route_status_counts[key] += value
        file_summaries.append(summary)

    for status_name in (VALID_15COL, REPAIRED_HIGH, REPAIRED_REVIEW, UNRESOLVED, "field_invalid"):
        final_status_counts.setdefault(status_name, 0)
    for route_name in (VALID_15COL, "overlong_record", "fragmented_record"):
        route_status_counts.setdefault(route_name, 0)

    present_names = set(state_by_name)
    manifest_names = {record["file_name"] for record in manifest["files"]}
    successful = [item for item in file_summaries if item.get("processing_status") == SUCCEEDED]
    parsed_total = sum(int(item.get("parsed_fragment_count", 0)) for item in successful)
    fragment_sum_total = sum(int(item.get("fragment_count_sum", 0)) for item in successful)
    cluster_total = sum(int(item.get("record_cluster_count", 0)) for item in successful)
    false_head_total = sum(
        int(item.get("valid_15col_false_head_negative_count", 0)) for item in successful
    )
    patient_output_paths = [
        Path(item["output_file"])
        for item in states["files"]
        if item.get("status") == SUCCEEDED
        and item.get("output_file")
        and str(item["output_file"]).endswith(".record_clusters.jsonl")
    ]
    return {
        "report_version": "imaging_pipeline_total_report_v2",
        "rules_version": RULES_VERSION,
        "generated_at": utc_now(),
        "manifest_version": manifest["manifest_version"],
        "manifest_file_count": len(manifest_names),
        "state_file_count": len(present_names),
        "file_coverage": {
            "missing_from_state": sorted(manifest_names - present_names),
            "extra_in_state": sorted(present_names - manifest_names),
            "pilot_file_count": sum(1 for record in manifest["files"] if record["is_pilot"]),
            "remaining_file_count": sum(1 for record in manifest["files"] if not record["is_pilot"]),
        },
        "terminal_state_counts": states["status_counts"],
        "all_files_terminal": len(present_names) == len(manifest_names)
        and not (set(states["status_counts"]) - {SUCCEEDED, FAILED, QUARANTINED}),
        "processed_file_count": len(successful),
        "status_counts": dict(final_status_counts),
        "route_status_counts": dict(route_status_counts),
        "repaired_high_count": final_status_counts[REPAIRED_HIGH],
        "repaired_review_count": final_status_counts[REPAIRED_REVIEW],
        "unresolved_count": final_status_counts[UNRESOLVED],
        "valid_15col_false_head_negative_count": false_head_total,
        "valid_15col_false_head_negative_target_zero": false_head_total == 0,
        "total_parsed_fragment_count": parsed_total,
        "total_record_cluster_count": cluster_total,
        "total_fragment_count_sum": fragment_sum_total,
        "fragment_count_reconciliation": parsed_total == fragment_sum_total,
        "token_conservation_all": bool(successful) and all(item.get("token_conservation") for item in successful),
        "fragment_provenance_all": bool(successful) and all(item.get("fragment_provenance") for item in successful),
        "normal_15col_zero_modification_all": bool(successful)
        and all(item.get("normal_15col_zero_modification") for item in successful),
        "patient_level_l1_output": {
            "file_count": len(patient_output_paths),
            "missing_file_count": sum(not path.exists() for path in patient_output_paths),
            "all_committed": bool(patient_output_paths)
            and all(path.exists() for path in patient_output_paths),
        },
        "audit_missing_files": sorted(audit_missing),
        "audit_hash_mismatches": sorted(hash_mismatches),
        "failed_or_quarantined": [
            {
                "file_name": item["file_name"],
                "status": item["processing_status"],
                "error_reason": item.get("error_reason"),
            }
            for item in file_summaries
            if item.get("processing_status") in (FAILED, QUARANTINED)
        ],
        "files": file_summaries,
    }


def write_total_report(
    manifest_path: str | Path,
    state_db: str | Path,
    output_root: str | Path,
    pilot_report: str | Path | None = None,
) -> Path:
    path = Path(output_root).resolve() / "imaging_pipeline_total_report.json"
    _write_json_atomic(path, build_total_report(manifest_path, state_db, output_root, pilot_report))
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "run", "status"), required=True)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--patient-output-dir", type=Path)
    parser.add_argument("--pilot-report", type=Path)
    parser.add_argument("--manifest-version", default=MANIFEST_VERSION)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)

    if args.mode == "dry-run":
        if args.input_dir is None:
            parser.error("dry-run requires --input-dir")
        manifest = scan_csv_manifest(args.input_dir)
        manifest["manifest_version"] = args.manifest_version
        write_immutable_manifest(manifest, args.manifest)
        init_state_db(args.state_db, manifest)
        print(json.dumps({"manifest": str(args.manifest), "file_count": manifest["file_count"], "files": manifest["files"]}, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "status":
        print(json.dumps(status_report(args.state_db), ensure_ascii=False, indent=2))
        return 0
    if args.output_root is None:
        parser.error("run requires --output-root")
    result = run_pipeline(
        args.manifest,
        args.state_db,
        args.output_root,
        workers=args.workers,
        patient_output_dir=args.patient_output_dir,
        pilot_report=args.pilot_report,
        retry_failed=args.retry_failed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())

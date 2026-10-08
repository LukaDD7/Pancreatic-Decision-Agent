"""Stream the controlled admission/discharge document CSV into restricted L1 Parquet."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from .document_l1_schema import (
    AUDIT_FIELDS,
    DEFAULT_BATCH_SIZE,
    INGESTION_VERSION,
    PARSED_TIME_FIELDS,
    PILOT_ROW_LIMIT,
    RAW_FIELDS,
    RAW_TIME_FIELDS,
    SOURCE_ENCODING,
)


_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d",
)
_SURROGATE_RE = re.compile(r"[\udc80-\udcff]")


@dataclass(frozen=True)
class ParquetShardSummary:
    path: str
    source_row_start: int
    source_row_end: int
    row_count: int
    byte_size: int
    sha256: str


def _memory_rss_bytes() -> int:
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return 0


def _source_bytes(value: str) -> bytes:
    """Keep GB18030 text and surrogate-escaped invalid bytes reversible."""

    return value.encode(SOURCE_ENCODING, errors="surrogateescape")


def _hash_raw_fields(raw_values: Sequence[str]) -> str:
    payload = json.dumps(
        list(raw_values),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode(SOURCE_ENCODING, errors="surrogateescape")
    return hashlib.sha256(payload).hexdigest()


def _content_sha256(content: str) -> str:
    return hashlib.sha256(_source_bytes(content)).hexdigest()


def _physical_line_count(content: str) -> int:
    if not content:
        return 0
    crlf_count = content.count("\r\n")
    line_break_count = crlf_count + (content.count("\r") - crlf_count) + (content.count("\n") - crlf_count)
    return line_break_count + 1


def parse_datetime(raw_value: str) -> tuple[datetime | None, str]:
    """Parse a document time without changing the stored raw value."""

    if raw_value == "":
        return None, "MISSING"
    candidate = raw_value.strip()
    if not candidate:
        return None, "MISSING"
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(candidate, fmt), "PARSED"
        except ValueError:
            continue
    return None, "INVALID_DATETIME"


def _encounter_interval_status(
    admission_raw: str,
    discharge_raw: str,
    admission_time: datetime | None,
    discharge_time: datetime | None,
) -> str:
    if discharge_raw.strip() == "":
        return "OPEN_INTERVAL"
    if admission_raw.strip() == "" or admission_time is None or discharge_time is None:
        return "INVALID_INTERVAL_DATE"
    if discharge_time < admission_time:
        return "INVALID_INTERVAL_ORDER"
    return "CLOSED_INTERVAL"


def _parser_status(
    raw_values: Sequence[str],
    time_statuses: Sequence[str],
    encounter_status: str,
) -> str:
    statuses: list[str] = []
    if raw_values[6] == "":
        statuses.append("CONTENT_NOT_RECORDED")
    if raw_values[4] == "":
        statuses.append("DOCUMENT_TYPE_NOT_RECORDED")
    if any(status == "INVALID_DATETIME" for status in time_statuses):
        statuses.append("DATE_PARSE_FAILED")
    if encounter_status in {"INVALID_INTERVAL_DATE", "INVALID_INTERVAL_ORDER"}:
        statuses.append(encounter_status)
    if any(_SURROGATE_RE.search(value) for value in raw_values):
        statuses.append("DECODED_WITH_SURROGATEESCAPE")
    return "OK" if not statuses else "|".join(statuses)


def build_record(
    raw_values: Sequence[str],
    *,
    source_file: str,
    source_row: int,
) -> dict[str, Any]:
    if len(raw_values) != len(RAW_FIELDS):
        raise ValueError(f"expected {len(RAW_FIELDS)} fields, got {len(raw_values)}")
    values = tuple(raw_values)
    parsed_times: list[datetime | None] = []
    time_statuses: list[str] = []
    for field in RAW_TIME_FIELDS:
        parsed, status = parse_datetime(values[RAW_FIELDS.index(field)])
        parsed_times.append(parsed)
        time_statuses.append(status)
    encounter_status = _encounter_interval_status(
        values[2], values[3], parsed_times[0], parsed_times[1]
    )
    return {
        **dict(zip(RAW_FIELDS, values)),
        "admission_time": parsed_times[0],
        "discharge_time": parsed_times[1],
        "create_time": parsed_times[2],
        "encounter_interval_status": encounter_status,
        "content_length": len(values[6]),
        "physical_line_count": _physical_line_count(values[6]),
        "content_sha256": _content_sha256(values[6]),
        "source_file": source_file,
        "source_row": source_row,
        "source_record_id": f"{Path(source_file).name}:{source_row}",
        "row_hash": _hash_raw_fields(values),
        "parser_status": _parser_status(values, time_statuses, encounter_status),
        "ingestion_version": INGESTION_VERSION,
    }


def _parquet_schema() -> pa.Schema:
    fields: list[pa.Field] = []
    for field in RAW_FIELDS:
        if field in {"PATIENT_ID", "VISIT_ID"}:
            field_type = pa.string()
        elif field == "文书内容":
            # The source contains an invalid GB18030 byte in the pilot range.
            # Binary storage preserves that byte instead of replacing it.
            field_type = pa.large_binary()
        else:
            field_type = pa.large_string()
        fields.append(pa.field(field, field_type))
    fields.extend(pa.field(field, pa.timestamp("us")) for field in PARSED_TIME_FIELDS)
    fields.extend(
        [
            pa.field("encounter_interval_status", pa.string()),
            pa.field("content_length", pa.int64()),
            pa.field("physical_line_count", pa.int64()),
            pa.field("content_sha256", pa.string()),
            pa.field("source_file", pa.string()),
            pa.field("source_row", pa.int64()),
            pa.field("source_record_id", pa.string()),
            pa.field("row_hash", pa.string()),
            pa.field("parser_status", pa.string()),
            pa.field("ingestion_version", pa.string()),
        ]
    )
    return pa.schema(fields)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_shard(output_path: Path, rows: Sequence[dict[str, Any]]) -> ParquetShardSummary:
    schema = _parquet_schema()
    columns: dict[str, list[Any]] = {}
    for field in schema:
        if field.name == "文书内容":
            columns[field.name] = [_source_bytes(row[field.name]) for row in rows]
        else:
            columns[field.name] = [row[field.name] for row in rows]
    table = pa.Table.from_pydict(columns, schema=schema)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output_path.name + ".", suffix=".tmp", dir=str(output_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        pq.write_table(table, temp_path, compression="zstd")
        temp_path.replace(output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return ParquetShardSummary(
        path=str(output_path),
        source_row_start=rows[0]["source_row"],
        source_row_end=rows[-1]["source_row"],
        row_count=len(rows),
        byte_size=output_path.stat().st_size,
        sha256=_sha256_file(output_path),
    )


def ingest_document_csv(
    input_path: str | Path,
    output_root: str | Path,
    *,
    row_limit: int = PILOT_ROW_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    if row_limit <= 0 or row_limit > PILOT_ROW_LIMIT:
        raise ValueError(f"row_limit must be in 1..{PILOT_ROW_LIMIT}")
    if batch_size <= 0 or batch_size > row_limit:
        raise ValueError("batch_size must be positive and no greater than row_limit")

    source_path = Path(input_path).resolve()
    output_path = Path(output_root).resolve()
    report_target = Path(report_path).resolve() if report_path else output_path.parent / "document_ingestion_report.json"
    if output_path.exists() and any(output_path.iterdir()):
        raise FileExistsError(f"refusing to use non-empty output directory: {output_path}")
    output_path.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    peak_memory = _memory_rss_bytes()
    parser_status_counts: Counter[str] = Counter()
    interval_status_counts: Counter[str] = Counter()
    null_counts: Counter[str] = Counter()
    date_status_counts: dict[str, Counter[str]] = {field: Counter() for field in RAW_TIME_FIELDS}
    row_hash_counts: Counter[str] = Counter()
    shard_summaries: list[ParquetShardSummary] = []
    batch: list[dict[str, Any]] = []
    emitted = 0
    previous_physical_end = 1
    first_source_row: int | None = None
    last_source_row: int | None = None
    min_content_length: int | None = None
    max_content_length = 0
    max_physical_line_count = 0
    surrogateescape_record_count = 0
    header: tuple[str, ...]

    with source_path.open("r", encoding=SOURCE_ENCODING, errors="surrogateescape", newline="") as stream:
        reader = csv.reader(stream)
        try:
            header = tuple(next(reader))
        except StopIteration as exc:
            raise ValueError("source CSV is empty") from exc
        if header != RAW_FIELDS:
            raise ValueError(f"unexpected CSV header with {len(header)} fields")

        for source_row in range(2, 2 + row_limit):
            try:
                raw_row = next(reader)
            except StopIteration:
                break
            if len(raw_row) != len(RAW_FIELDS):
                raise ValueError(f"source row {source_row} has {len(raw_row)} fields; expected 7")
            physical_end = reader.line_num
            physical_start = previous_physical_end + 1
            previous_physical_end = physical_end
            record = build_record(raw_row, source_file=str(source_path), source_row=source_row)
            record["physical_line_count"] = _physical_line_count(raw_row[6])
            record["_physical_line_start"] = physical_start
            record["_physical_line_end"] = physical_end
            batch.append(record)
            emitted += 1
            first_source_row = source_row if first_source_row is None else first_source_row
            last_source_row = source_row
            parser_status_counts[record["parser_status"]] += 1
            interval_status_counts[record["encounter_interval_status"]] += 1
            row_hash_counts[record["row_hash"]] += 1
            min_content_length = (
                record["content_length"]
                if min_content_length is None
                else min(min_content_length, record["content_length"])
            )
            max_content_length = max(max_content_length, record["content_length"])
            max_physical_line_count = max(max_physical_line_count, record["physical_line_count"])
            if "DECODED_WITH_SURROGATEESCAPE" in record["parser_status"]:
                surrogateescape_record_count += 1
            for field in RAW_FIELDS:
                if record[field] == "":
                    null_counts[field] += 1
            for field, status in zip(RAW_TIME_FIELDS, [parse_datetime(raw_row[index])[1] for index in (2, 3, 5)]):
                date_status_counts[field][status] += 1
            if len(batch) >= batch_size:
                shard_summaries.append(_write_shard(output_path / f"part-{len(shard_summaries) + 1:05d}.parquet", batch))
                batch.clear()
                peak_memory = max(peak_memory, _memory_rss_bytes())

    if batch:
        shard_summaries.append(_write_shard(output_path / f"part-{len(shard_summaries) + 1:05d}.parquet", batch))
        batch.clear()
        peak_memory = max(peak_memory, _memory_rss_bytes())
    peak_memory = max(peak_memory, _memory_rss_bytes())

    report: dict[str, Any] = {
        "report_version": "document_ingestion_report_v1",
        "ingestion_version": INGESTION_VERSION,
        "source_file": str(source_path),
        "source_file_size": source_path.stat().st_size,
        "source_encoding": SOURCE_ENCODING,
        "csv_reader": {"standard_csv_reader": True, "newline_empty": True, "errors": "surrogateescape"},
        "row_limit_excludes_header": True,
        "row_limit": row_limit,
        "batch_size": batch_size,
        "header_field_count": len(header),
        "raw_field_count_per_record": len(RAW_FIELDS),
        "emitted_row_count": emitted,
        "reached_row_limit": emitted == row_limit,
        "source_row_start": first_source_row,
        "source_row_end": last_source_row,
        "parser_status_counts": dict(sorted(parser_status_counts.items())),
        "encounter_interval_status_counts": dict(sorted(interval_status_counts.items())),
        "null_counts": dict(sorted(null_counts.items())),
        "date_status_counts": {field: dict(sorted(counts.items())) for field, counts in date_status_counts.items()},
        "duplicate_row_hash_count": sum(count - 1 for count in row_hash_counts.values() if count > 1),
        "surrogateescape_record_count": surrogateescape_record_count,
        "content_length_min": min_content_length,
        "content_length_max": max_content_length,
        "physical_line_count_max": max_physical_line_count,
        "restricted_output": True,
        "report_contains_document_content": False,
        "report_contains_patient_direct_identifiers": False,
        "nlp_extraction": False,
        "patient_alignment": False,
        "hash_encoding": "gb18030 with surrogateescape",
        "document_content_storage": "large_binary containing original GB18030 bytes",
        "document_content_roundtrip": "decode with gb18030 and surrogateescape",
        "peak_memory_bytes": peak_memory,
        "peak_memory_gib": peak_memory / (1024**3),
        "elapsed_seconds": time.perf_counter() - started,
        "shards": [asdict(summary) for summary in shard_summaries],
    }
    report_target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=report_target.name + ".", suffix=".tmp", dir=str(report_target.parent))
    os.close(fd)
    temp_report = Path(temp_name)
    try:
        temp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_report.replace(report_target)
    except Exception:
        temp_report.unlink(missing_ok=True)
        raise
    report["report_path"] = str(report_target)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--row-limit", type=int, default=PILOT_ROW_LIMIT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()
    report = ingest_document_csv(
        args.csv,
        args.output_root,
        row_limit=args.row_limit,
        batch_size=args.batch_size,
        report_path=args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

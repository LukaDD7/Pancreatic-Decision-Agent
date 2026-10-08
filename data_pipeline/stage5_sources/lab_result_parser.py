"""Laboratory result parsing and the controlled 5A Parquet enrichment pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from .lab_l1_schema import PILOT_SHEETS, RAW_FIELDS
from .lab_reference_parser import parse_reference
from .lab_time_normalizer import TIME_RULE_VERSION, normalize_time_pair
from .lab_unit_normalizer import normalize_unit


RESULT_TYPES = (
    "NUMERIC",
    "SCIENTIFIC_NUMERIC",
    "QUALIFIED_NUMERIC",
    "PERCENT_NUMERIC",
    "QUALITATIVE",
    "QUALITATIVE_WITH_NUMERIC",
    "BELOW_DETECTION_LIMIT",
    "LONG_TEXT_RESULT",
    "EMPTY",
    "UNPARSED",
)
RESULT_PARSE_STATUSES = ("PARSED", "EMPTY", "UNPARSED")
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUMERIC_RE = re.compile(rf"^{_NUMBER}$")
_SCIENTIFIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+$")
_QUALIFIED_RE = re.compile(rf"^(<=|>=|<|>|≤|≥)\s*({_NUMBER})$")
_PERCENT_RE = re.compile(rf"^({_NUMBER})\s*%$")
_QUALITATIVE_NUMERIC_RE = re.compile(
    rf"^(阴性|阳性|弱阴性|弱阳性|未检出|未见|正常|异常)\s*({_NUMBER})$"
)
_QUALITATIVE_VALUES = frozenset(
    {"阴性", "阳性", "弱阴性", "弱阳性", "未检出", "未见", "正常", "异常", "无", "有"}
)
_BELOW_LIMIT_RE = re.compile(r"^(?:<\s*)?(?:最低)?检出限$|^低于(?:最低)?检出限$")
LONG_TEXT_THRESHOLD = 64


def _to_number(value: str) -> float | None:
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return float(number)


def parse_result(value: Any) -> dict[str, Any]:
    """Parse one result expression without assigning clinical meaning."""

    result_raw = value
    if value is None:
        text = ""
    else:
        text = str(value).strip()
    base = {
        "result_raw": result_raw,
        "result_type": "EMPTY",
        "result_operator": None,
        "result_numeric_value": None,
        "result_qualitative_value": None,
        "result_text_value": None,
        "result_parse_status": "EMPTY",
    }
    if not text:
        return base

    if _BELOW_LIMIT_RE.fullmatch(text):
        base.update(
            result_type="BELOW_DETECTION_LIMIT",
            result_operator="<",
            result_text_value=text,
            result_parse_status="PARSED",
        )
        return base

    percent = _PERCENT_RE.fullmatch(text)
    if percent:
        base.update(
            result_type="PERCENT_NUMERIC",
            result_numeric_value=_to_number(percent.group(1)),
            result_parse_status="PARSED",
        )
        return base

    qualified = _QUALIFIED_RE.fullmatch(text)
    if qualified:
        base.update(
            result_type="QUALIFIED_NUMERIC",
            result_operator=qualified.group(1),
            result_numeric_value=_to_number(qualified.group(2)),
            result_parse_status="PARSED",
        )
        return base

    qualitative_numeric = _QUALITATIVE_NUMERIC_RE.fullmatch(text)
    if qualitative_numeric:
        base.update(
            result_type="QUALITATIVE_WITH_NUMERIC",
            result_qualitative_value=qualitative_numeric.group(1),
            result_numeric_value=_to_number(qualitative_numeric.group(2)),
            result_parse_status="PARSED",
        )
        return base

    if _SCIENTIFIC_RE.fullmatch(text):
        base.update(
            result_type="SCIENTIFIC_NUMERIC",
            result_numeric_value=_to_number(text),
            result_parse_status="PARSED",
        )
        return base

    if _NUMERIC_RE.fullmatch(text):
        base.update(
            result_type="NUMERIC",
            result_numeric_value=_to_number(text),
            result_parse_status="PARSED",
        )
        return base

    if text in _QUALITATIVE_VALUES:
        base.update(
            result_type="QUALITATIVE",
            result_qualitative_value=text,
            result_parse_status="PARSED",
        )
        return base

    if len(text) >= LONG_TEXT_THRESHOLD or "\n" in text or "\r" in text:
        base.update(result_type="LONG_TEXT_RESULT", result_text_value=text, result_parse_status="PARSED")
        return base

    base.update(result_type="UNPARSED", result_text_value=text, result_parse_status="UNPARSED")
    return base


def _memory_rss_bytes() -> int:
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _enriched_schema(input_schema: pa.Schema) -> pa.Schema:
    additions = (
        ("result_raw", pa.large_string()),
        ("result_type", pa.string()),
        ("result_operator", pa.string()),
        ("result_numeric_value", pa.float64()),
        ("result_qualitative_value", pa.large_string()),
        ("result_text_value", pa.large_string()),
        ("result_parse_status", pa.string()),
        ("reference_raw", pa.large_string()),
        ("reference_type", pa.string()),
        ("reference_rule_json", pa.large_string()),
        ("reference_parse_status", pa.string()),
        ("unit_raw", pa.large_string()),
        ("unit_normalized", pa.large_string()),
        ("unit_mapping_status", pa.string()),
        ("raw_excel_serial", pa.large_string()),
        ("event_time", pa.timestamp("us")),
        ("available_time", pa.timestamp("us")),
        ("time_parse_status", pa.string()),
        ("time_rule_version", pa.string()),
        ("source_abnormal_flag", pa.large_string()),
    )
    names = set(input_schema.names)
    if names.intersection(name for name, _ in additions):
        raise ValueError("input already contains one or more enrichment columns")
    schema = input_schema
    for name, data_type in additions:
        schema = schema.append(pa.field(name, data_type))
    return schema


def _atomic_write_table(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        pq.write_table(table, temp_path, compression="zstd")
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _parse_rows(rows: list[dict[str, Any]], counters: dict[str, Counter[str]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        result = parse_result(row.get("检验结果值"))
        reference = parse_reference(row.get("检验参考值"))
        unit = normalize_unit(row.get("检验结果单位"))
        timing = normalize_time_pair(row.get("送检时间"), row.get("报告时间"))
        output = dict(row)
        output.update(result)
        output.update(reference)
        output.update(unit)
        output.update(timing)
        output["source_abnormal_flag"] = row.get("结果正常标志")
        enriched.append(output)
        counters["result_type"][result["result_type"]] += 1
        counters["result_parse_status"][result["result_parse_status"]] += 1
        counters["reference_type"][reference["reference_type"]] += 1
        counters["reference_parse_status"][reference["reference_parse_status"]] += 1
        counters["unit_mapping_status"][unit["unit_mapping_status"]] += 1
        counters["time_parse_status"][timing["time_parse_status"]] += 1
        counters["source_abnormal_flag_missing"][str(row.get("结果正常标志") is None)] += 1
    return enriched


def enrich_pilot_parquet(
    input_root: str | Path,
    output_root: str | Path,
    report_path: str | Path,
    *,
    batch_size: int = 25_000,
) -> dict[str, Any]:
    """Enrich exactly the 5A pilot shards without cross-sheet concatenation."""

    input_path = Path(input_root).resolve()
    output_path = Path(output_root).resolve()
    report_target = Path(report_path).resolve()
    if output_path.exists() and any(output_path.iterdir()):
        raise FileExistsError(f"refusing to use non-empty enrichment directory: {output_path}")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    output_path.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    peak_memory = _memory_rss_bytes()
    counters = {key: Counter() for key in (
        "result_type", "result_parse_status", "reference_type", "reference_parse_status",
        "unit_mapping_status", "time_parse_status", "source_abnormal_flag_missing",
    )}
    sheet_summaries: list[dict[str, Any]] = []
    input_total = 0
    output_total = 0

    for sheet_name in PILOT_SHEETS:
        input_sheet = input_path / sheet_name
        output_sheet = output_path / sheet_name
        input_shards = sorted(input_sheet.glob("*.parquet"))
        if not input_shards:
            raise FileNotFoundError(f"no Parquet shards for {sheet_name}: {input_sheet}")
        sheet_input = 0
        sheet_output = 0
        shard_summaries: list[dict[str, Any]] = []
        for input_shard in input_shards:
            output_shard = output_sheet / input_shard.name
            pf = pq.ParquetFile(input_shard)
            input_schema = pf.schema_arrow
            schema = _enriched_schema(input_schema)
            shard_rows = 0
            for record_batch in pf.iter_batches(batch_size=batch_size):
                rows = record_batch.to_pylist()
                enriched_rows = _parse_rows(rows, counters)
                table = pa.Table.from_pylist(enriched_rows, schema=schema)
                if output_shard.exists():
                    raise FileExistsError(f"refusing to overwrite output shard: {output_shard}")
                _atomic_write_table(table, output_shard)
                shard_rows += table.num_rows
                peak_memory = max(peak_memory, _memory_rss_bytes())
            if shard_rows != pf.metadata.num_rows:
                raise ValueError(f"row count changed for {input_shard.name}")
            input_size = input_shard.stat().st_size
            shard_summaries.append(
                {
                    "input_path": str(input_shard),
                    "input_row_count": pf.metadata.num_rows,
                    "input_byte_size": input_size,
                    "input_sha256": _sha256_file(input_shard),
                    "output_path": str(output_shard),
                    "output_row_count": shard_rows,
                    "output_byte_size": output_shard.stat().st_size,
                    "output_sha256": _sha256_file(output_shard),
                }
            )
            sheet_input += pf.metadata.num_rows
            sheet_output += shard_rows
        input_total += sheet_input
        output_total += sheet_output
        sheet_summaries.append(
            {
                "source_sheet": sheet_name,
                "input_row_count": sheet_input,
                "output_row_count": sheet_output,
                "shard_count": len(shard_summaries),
                "shards": shard_summaries,
            }
        )

    report: dict[str, Any] = {
        "report_version": "lab_enriched_report_v1",
        "result_parser_version": "lab_result_parser_v1",
        "reference_parser_version": "lab_reference_parser_v1",
        "unit_normalizer_version": "lab_unit_normalizer_v1",
        "time_rule_version": TIME_RULE_VERSION,
        "input_root": str(input_path),
        "output_root": str(output_path),
        "requested_sheets": list(PILOT_SHEETS),
        "input_record_count": input_total,
        "output_record_count": output_total,
        "record_count_reconciliation": input_total == output_total,
        "result_type_counts": dict(sorted(counters["result_type"].items())),
        "result_parse_status_counts": dict(sorted(counters["result_parse_status"].items())),
        "reference_type_counts": dict(sorted(counters["reference_type"].items())),
        "reference_parse_status_counts": dict(sorted(counters["reference_parse_status"].items())),
        "unit_mapping_status_counts": dict(sorted(counters["unit_mapping_status"].items())),
        "time_parse_status_counts": dict(sorted(counters["time_parse_status"].items())),
        "source_abnormal_flag_missing_count": counters["source_abnormal_flag_missing"]["True"],
        "derived_abnormal_flag_generated": False,
        "source_columns_preserved": True,
        "peak_memory_bytes": peak_memory,
        "peak_memory_gib": peak_memory / (1024**3),
        "elapsed_seconds": time.perf_counter() - started,
        "sheets": sheet_summaries,
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
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=25_000)
    args = parser.parse_args()
    report = enrich_pilot_parquet(args.input_root, args.output_root, args.report, batch_size=args.batch_size)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

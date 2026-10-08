"""Rule-filter pancreas-related imaging reports using existing Stage 5/6 links."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from data_pipeline.paths import data_root
from data_pipeline.report_index.imaging_fields import (
    RULE_VERSION as FIELD_RULE_VERSION,
    split_imaging_source_fields,
)
from scripts.cohort_construction.agent_packaging_and_audit.shared import stage8a


ROOT = data_root()
SOURCE_ROOT = ROOT / "pipeline_outputs_v2" / "patient_l1"
LINK_ROOT = ROOT / "pipeline_outputs_stage6_v2" / "restricted" / "full" / "record_links" / "imaging"
INVENTORY_PATH = ROOT / "影像数据汇总报告" / "patient_folder_inventory.parquet"
OUTPUT_ROOT = ROOT / "pipeline_outputs_stage8_v1" / "pancreas_imaging_report_index_v3"
FINAL_ROOT = OUTPUT_ROOT / "restricted" / "index"
AUDIT_PATH = OUTPUT_ROOT / "audit" / "acceptance.json"
SOURCE_REPORT_PATH = ROOT / "pipeline_outputs_v2" / "imaging_pipeline_total_report.json"
PANCREAS_RE = re.compile(r"胰(?!岛素)|MRCP", re.IGNORECASE)
PII_PATTERNS = (
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"(?<!\d)1\d{10}(?!\d)"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?:患者姓名|姓名|身份证号|联系电话|住院号|病案号|门诊号|床号|住址|地址|家住)\s*[:：]\s*(?!\[REDACTED)[^\s,，。；;]+"),
    re.compile(r"(?<=患者)[\u4e00-\u9fff·]{2,6}(?=[，,]\s*(?:男|女))"),
)
RULE_VERSION = f"pancreas_imaging_report_rule_v3+{FIELD_RULE_VERSION}"

SCHEMA = pa.schema([
    ("source_record_key", pa.string()), ("report_uid", pa.string()),
    ("source_file_name", pa.string()), ("record_cluster_id", pa.string()),
    ("patient_id", pa.string()), ("patient_uid", pa.string()),
    ("exam_date", pa.string()), ("exam_time_raw", pa.string()),
    ("exam_datetime", pa.string()), ("imaging_number_raw", pa.string()),
    ("exam_method", pa.large_string()),
    ("findings_deidentified", pa.large_string()),
    ("impression_deidentified", pa.large_string()),
    ("source_result_class", pa.string()),
    ("source_result_class_column", pa.string()),
    ("source_result_class_is_model_generated", pa.bool_()),
    ("description_deidentified", pa.large_string()),
    ("diagnosis_deidentified", pa.large_string()),
    ("report_deidentified", pa.large_string()),
    ("rule_hit", pa.string()), ("source_parse_status", pa.string()),
    ("stage6_link_status", pa.string()), ("stage6_event_eligible", pa.bool_()),
    ("inventory_match_status", pa.string()), ("inventory_case_count", pa.int32()),
    ("inventory_storage_keys_json", pa.large_string()),
    ("inventory_phase_patterns_json", pa.large_string()),
    ("index_status", pa.string()), ("review_reason", pa.string()),
    ("rule_version", pa.string()),
])


def clean_id(value: object) -> str:
    return str(value or "").strip()


def normalize_date(value: object) -> str:
    raw = clean_id(value)
    if not raw:
        return ""
    compact = re.sub(r"[^0-9]", "", raw)
    if len(compact) != 8:
        return ""
    try:
        return datetime.strptime(compact, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def possible_pii(text: str) -> list[str]:
    return [f"pii_pattern_{index}" for index, pattern in enumerate(PII_PATTERNS) if pattern.search(text)]


def link_paths_by_source() -> dict[str, Path]:
    result = {}
    for path in sorted(LINK_ROOT.glob("*.parquet")):
        batch = next(pq.ParquetFile(path).iter_batches(batch_size=1, columns=["source_file"]), None)
        if batch is None or batch.num_rows == 0:
            continue
        source_name = Path(str(batch.column(0)[0].as_py())).name + ".record_clusters.jsonl"
        if source_name in result:
            raise RuntimeError(f"duplicate_stage6_source:{source_name}")
        result[source_name] = path
    return result


def inventory_by_patient_date() -> dict[tuple[str, str], list[tuple[str, str]]]:
    result: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    table = pq.read_table(
        INVENTORY_PATH,
        columns=["patient_id_raw", "exam_date_raw", "date_parse_status", "storage_case_key", "image_phase_pattern"],
    )
    for row in table.to_pylist():
        if row["date_parse_status"] != "VALID_YYYYMMDD":
            continue
        patient_id = clean_id(row["patient_id_raw"])
        exam_date = normalize_date(row["exam_date_raw"])
        if patient_id and exam_date:
            result[(patient_id, exam_date)].append((row["storage_case_key"], row["image_phase_pattern"]))
    return result


def normalize_time(date: str, raw: str) -> str:
    if not date:
        return ""
    value = raw.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return f"{date} {parsed.strftime('%H:%M:%S')}"
        except ValueError:
            pass
    return ""


def load_links(path: Path) -> dict[str, dict]:
    columns = ["source_record_key", "patient_uid", "link_status", "event_eligible", "source_status"]
    table = pq.read_table(path, columns=columns)
    return {row["source_record_key"]: row for row in table.to_pylist()}


def make_row(record: dict, source_name: str, link: dict, inventory: dict) -> dict | None:
    tokens = record.get("repaired_tokens")
    if not isinstance(tokens, list) or len(tokens) < 15:
        return None
    tokens = [str(value or "") for value in tokens[:15]]
    fields = split_imaging_source_fields(tokens)
    method = fields["exam_method"]
    findings = fields["findings_text"]
    impression = fields["impression_text"]
    result_class = fields["source_result_class"]
    method_hit = bool(PANCREAS_RE.search(method))
    body_hit = bool(PANCREAS_RE.search(fields["report_text"]))
    if not method_hit and not body_hit:
        return None
    source_key = stage8a.stable_source_key("imaging", source_name, record.get("record_cluster_id"))
    if link is None:
        raise RuntimeError(f"stage6_link_missing:{source_name}:{record.get('record_cluster_id')}")
    if source_key != link["source_record_key"]:
        raise RuntimeError(f"stage6_link_key_mismatch:{source_name}:{record.get('record_cluster_id')}")
    patient_id = clean_id(tokens[3])
    exam_date = normalize_date(tokens[5])
    is_ct = "CT" in method.upper() and "PET" not in method.upper()
    matches = inventory.get((patient_id, exam_date), []) if is_ct else []
    if not is_ct:
        match_status = "NOT_CT_REPORT"
    elif not exam_date:
        match_status = "INVALID_REPORT_DATE"
    elif len(matches) == 1:
        match_status = "PATIENT_DATE_ONE_CT_FOLDER_UNVERIFIED_EXAM"
    elif len(matches) > 1:
        match_status = "PATIENT_DATE_MULTIPLE_CT_FOLDERS"
    else:
        match_status = "NO_SAME_DAY_CT_FOLDER"
    direct_values = [tokens[index] for index in (0, 3, 4, 9, 10)]
    findings_safe = stage8a.redact_text(findings, direct_values, limit=len(findings) + 100)
    impression_safe = stage8a.redact_text(impression, direct_values, limit=len(impression) + 100)
    report_safe = "\n".join(part for part in (findings_safe, impression_safe) if part)
    review_reasons = []
    if not patient_id or not link["patient_uid"] or link["link_status"] != "HARD_PATIENT_EXACT":
        review_reasons.append("no_stage6_hard_patient_link")
    if not exam_date or not link["event_eligible"]:
        review_reasons.append("invalid_or_ineligible_exam_time")
    if not report_safe:
        review_reasons.append("report_text_empty")
    if possible_pii(report_safe):
        review_reasons.append("pii_after_redaction")
    status = "INDEXED" if not review_reasons else "REVIEW"
    return {
        "source_record_key": source_key,
        "report_uid": stage8a.build_report_uid(link["patient_uid"] or patient_id, source_key, stage8a.sha256_text(report_safe), "imaging"),
        "source_file_name": source_name,
        "record_cluster_id": str(record.get("record_cluster_id") or ""),
        "patient_id": patient_id,
        "patient_uid": link["patient_uid"] or "",
        "exam_date": exam_date,
        "exam_time_raw": tokens[7],
        "exam_datetime": normalize_time(exam_date, tokens[7]),
        "imaging_number_raw": tokens[4],
        "exam_method": method,
        "findings_deidentified": findings_safe if status == "INDEXED" else "",
        "impression_deidentified": impression_safe if status == "INDEXED" else "",
        "source_result_class": result_class,
        "source_result_class_column": fields["source_result_class_column"],
        "source_result_class_is_model_generated": fields["source_result_class_is_model_generated"],
        "description_deidentified": findings_safe if status == "INDEXED" else "",
        "diagnosis_deidentified": impression_safe if status == "INDEXED" else "",
        "report_deidentified": report_safe if status == "INDEXED" else "",
        "rule_hit": "method_and_body" if method_hit and body_hit else "method" if method_hit else "body",
        "source_parse_status": str(record.get("status") or ""),
        "stage6_link_status": str(link["link_status"] or ""),
        "stage6_event_eligible": bool(link["event_eligible"]),
        "inventory_match_status": match_status,
        "inventory_case_count": len(matches),
        "inventory_storage_keys_json": json.dumps([item[0] for item in matches], ensure_ascii=False),
        "inventory_phase_patterns_json": json.dumps([item[1] for item in matches], ensure_ascii=False),
        "index_status": status,
        "review_reason": "|".join(review_reasons),
        "rule_version": RULE_VERSION,
    }


def run(max_files: int | None = None) -> dict:
    if FINAL_ROOT.exists():
        raise RuntimeError("final_index_already_exists")
    staging = FINAL_ROOT.with_name(FINAL_ROOT.name + ".staging")
    if staging.exists():
        raise RuntimeError("index_staging_already_exists")
    staging.mkdir(parents=True)
    links_by_source = link_paths_by_source()
    sources = sorted(SOURCE_ROOT.glob("*.record_clusters.jsonl"))
    if max_files is not None:
        sources = sources[:max_files]
    if len(sources) != len(links_by_source) and max_files is None:
        raise RuntimeError(f"source_link_file_count_mismatch:{len(sources)}:{len(links_by_source)}")
    inventory = inventory_by_patient_date()
    writers = {
        "report_index": pq.ParquetWriter(staging / "report_index.parquet", SCHEMA, compression="zstd"),
        "review_candidates": pq.ParquetWriter(staging / "review_candidates.parquet", SCHEMA, compression="zstd"),
    }
    buffers: dict[str, list[dict]] = {name: [] for name in writers}
    counts = Counter()
    source_file_counts = {}
    for file_index, source in enumerate(sources, 1):
        link_path = links_by_source.get(source.name)
        if link_path is None:
            raise RuntimeError(f"stage6_link_file_missing:{source.name}")
        links = load_links(link_path)
        file_total = 0
        file_hit = 0
        with source.open("r", encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                file_total += 1
                source_key = stage8a.stable_source_key("imaging", source.name, record.get("record_cluster_id"))
                row = make_row(record, source.name, links.get(source_key), inventory)
                if row is None:
                    continue
                file_hit += 1
                name = "report_index" if row["index_status"] == "INDEXED" else "review_candidates"
                buffers[name].append(row)
                counts[f"status_{row['index_status']}"] += 1
                counts[f"rule_{row['rule_hit']}"] += 1
                counts[f"inventory_{row['inventory_match_status']}"] += 1
                counts[f"source_result_{row['source_result_class'] or 'BLANK'}"] += 1
                if row["review_reason"]:
                    for reason in row["review_reason"].split("|"):
                        counts[f"review_{reason}"] += 1
                if len(buffers[name]) >= 2_000:
                    writers[name].write_table(pa.Table.from_pylist(buffers[name], schema=SCHEMA))
                    buffers[name].clear()
        if file_total != len(links):
            raise RuntimeError(f"stage5_stage6_row_count_mismatch:{source.name}:{file_total}:{len(links)}")
        counts["scanned_records"] += file_total
        source_file_counts[source.name] = {"scanned": file_total, "rule_hit": file_hit}
        print(json.dumps({"files_done": file_index, "files_total": len(sources), "scanned": counts["scanned_records"], "rule_hits": counts["status_INDEXED"] + counts["status_REVIEW"]}), flush=True)
    for name, writer in writers.items():
        if buffers[name]:
            writer.write_table(pa.Table.from_pylist(buffers[name], schema=SCHEMA))
        writer.close()
    if max_files is None:
        expected = json.loads(SOURCE_REPORT_PATH.read_text(encoding="utf-8"))["total_record_cluster_count"]
        if counts["scanned_records"] != expected:
            raise RuntimeError(f"source_total_mismatch:{counts['scanned_records']}:{expected}")
    os.replace(staging, FINAL_ROOT)
    audit = {
        "status": "SUCCEEDED",
        "rule_version": RULE_VERSION,
        "source_files": len(sources),
        "source_records_scanned": counts["scanned_records"],
        "indexed_reports": counts["status_INDEXED"],
        "review_candidates": counts["status_REVIEW"],
        "counts": dict(sorted(counts.items())),
        "stage6_exact_source_key_reused": True,
        "inventory_match_is_same_patient_date_not_proven_same_exam": True,
        "source_result_class_is_source_provided_not_model_generated": True,
        "agent_report_excludes_source_result_class": True,
        "model_calls": 0,
        "source_file_counts": source_file_counts,
        "final_root": str(FINAL_ROOT),
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-files", type=int)
    args = parser.parse_args()
    result = run(max_files=args.max_files)
    print(json.dumps({key: value for key, value in result.items() if key != "source_file_counts"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

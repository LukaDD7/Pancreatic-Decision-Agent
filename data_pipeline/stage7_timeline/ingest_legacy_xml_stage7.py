from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from lxml import etree

from data_pipeline.paths import data_root
from .stage7_models import (
    DAY_EVENT_RELATION_SCHEMA,
    DOCUMENT_DAY_DETAIL_SCHEMA,
    EVENT_SOURCE_MAP_SCHEMA,
    EVENT_TIME_EVIDENCE_SCHEMA,
    PATIENT_DAY_SCHEMA,
    TIMELINE_EVENT_SCHEMA,
    day_id,
    event_id,
    schema_hash,
)


DATA_ROOT = data_root()
SOURCE_ROOT = DATA_ROOT / "21年以前检验文书"
STAGE5_ROOT = DATA_ROOT / "pipeline_outputs_stage5_v1"
STAGE6_DB = DATA_ROOT / "pipeline_outputs_stage6_v2" / "restricted" / "state" / "stage6_full_state_v2.sqlite3"
STAGE7_ROOT = DATA_ROOT / "pipeline_outputs_stage7_v1"
VERSION = "legacy_xml_document_increment_v1"
RULE_VERSION = "stage7_legacy_xml_document_v1"
CUTOFF = datetime(2021, 1, 1)
L1_FINAL = STAGE5_ROOT / "restricted" / "legacy_xml_document_l1_v1"
OUTPUT_FINAL = STAGE7_ROOT / VERSION
REGISTRY = STAGE7_ROOT / "restricted" / "full_day_timeline" / "increment_registry" / f"{VERSION}.json"


L1_SCHEMA = pa.schema([
    ("PATIENT_ID", pa.string()), ("VISIT_ID", pa.string()), ("LEGACY_INPATIENT_NO", pa.string()),
    ("ADMISSION_DATE_TIME", pa.large_string()), ("DISCHARGE_DATE_TIME", pa.large_string()),
    ("文书名称", pa.large_string()), ("CREATE_DATE_TIME", pa.large_string()),
    ("文书内容", pa.large_binary()), ("admission_time", pa.timestamp("us")),
    ("discharge_time", pa.timestamp("us")), ("create_time", pa.timestamp("us")),
    ("encounter_interval_status", pa.string()), ("content_length", pa.int64()),
    ("physical_line_count", pa.int64()), ("content_sha256", pa.string()),
    ("source_file", pa.large_string()), ("source_row", pa.int64()),
    ("source_record_id", pa.string()), ("row_hash", pa.string()),
    ("parser_status", pa.string()), ("ingestion_version", pa.string()),
    ("xml_recovery_error_count", pa.int64()), ("source_relative_path", pa.large_string()),
    ("source_folder", pa.string()), ("source_file_sha256", pa.string()),
])

QUARANTINE_SCHEMA = pa.schema([
    ("source_relative_path", pa.large_string()), ("source_record_id", pa.string()),
    ("patient_id", pa.string()), ("visit_id", pa.string()), ("reason", pa.string()),
    ("detail", pa.large_string()), ("rule_version", pa.string()),
])

LINK_SCHEMA = pa.schema([
    ("source_record_key", pa.string()), ("source_record_id", pa.string()),
    ("source_relative_path", pa.large_string()), ("patient_id", pa.string()),
    ("visit_id", pa.string()), ("patient_uid", pa.string()), ("encounter_uid", pa.string()),
    ("link_status", pa.string()), ("event_eligible", pa.bool_()),
    ("quality_flags", pa.list_(pa.string())), ("rule_version", pa.string()),
])


def clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_id(value: Any) -> str:
    return re.sub(r"\s+", "", clean(value).upper())


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_source_key(source_file: Any, source_record_id: Any) -> str:
    material = "|".join(("legacy_xml_document_v1", clean(source_file), clean(source_record_id)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def commit_directory(staging: Path, final: Path) -> None:
    try:
        os.replace(staging, final)
    except PermissionError:
        shutil.move(str(staging), str(final))


def parse_filename(path: Path) -> dict[str, str] | None:
    parts = path.stem.split("_", 3)
    if len(parts) != 4:
        return None
    patient_id, visit_id, inpatient_no, remainder = parts
    for prefix, group in (("入院记录", "admission"), ("病程记录", "progress"), ("手术记录", "surgery")):
        if remainder.startswith(prefix):
            return {
                "patient_id": normalize_id(patient_id), "visit_id": normalize_id(visit_id),
                "inpatient_no": normalize_id(inpatient_no), "file_document_type": prefix,
                "document_group": group, "legacy_file_record_id": remainder[len(prefix):],
            }
    return None


def parse_xml(path: Path) -> tuple[etree._Element | None, str, int, list[str]]:
    flags: list[str] = []
    data = path.read_bytes()
    try:
        decoded = data.decode("gb18030")
    except UnicodeDecodeError:
        decoded = data.decode("gb18030", errors="replace")
        flags.append("XML_DECODE_REPLACEMENT")
    decoded = re.sub(r"^\s*<\?xml[^>]*\?>", "", decoded, count=1, flags=re.I)
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(decoded.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return None, decoded, len(parser.error_log), flags + ["XML_UNRECOVERABLE"]
    errors = len(parser.error_log)
    if errors:
        flags.append("XML_RECOVERED")
    return root, decoded, errors, flags


def terminal_texts(element: etree._Element) -> list[str]:
    values = []
    for item in element.iter():
        if not isinstance(item.tag, str):
            continue
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in {"text", "fieldelem"}:
            continue
        value = clean(item.text)
        if value:
            values.append(value)
    return values


def document_text(element: etree._Element) -> str:
    value = "".join(terminal_texts(element))
    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\t\f\v ]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def field_values(element: etree._Element) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for item in element.iter():
        if not isinstance(item.tag, str) or item.tag.rsplit("}", 1)[-1] != "fieldelem":
            continue
        name = clean(item.get("name"))
        value = clean(item.text)
        if name and value:
            result[name].append(value)
    return result


DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?"
    r"(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def parse_datetime(value: Any) -> datetime | None:
    match = DATE_RE.search(clean(value))
    if not match:
        return None
    parts = [int(match.group(index)) for index in (1, 2, 3)]
    time_parts = [int(match.group(index) or 0) for index in (4, 5, 6)]
    try:
        return datetime(*parts, *time_parts)
    except ValueError:
        return None


def first_named_time(fields: dict[str, list[str]], names: Iterable[str]) -> tuple[str, datetime | None]:
    for name in names:
        for raw in fields.get(name, []):
            parsed = parse_datetime(raw)
            if parsed:
                return raw, parsed
    return "", None


def progress_units(root: etree._Element) -> list[tuple[int, etree._Element]]:
    result = []
    for index, child in enumerate(list(root), 1):
        if not isinstance(child.tag, str) or child.tag.rsplit("}", 1)[-1] != "section":
            continue
        head = "".join(terminal_texts(child)[:3])[:160]
        if parse_datetime(head):
            result.append((index, child))
    return result or [(1, root)]


def unit_title(group: str, unit: etree._Element) -> str:
    if group == "admission":
        return "入院记录"
    if group == "surgery":
        return "手术记录"
    for section in unit.iter():
        if not isinstance(section.tag, str) or section.tag.rsplit("}", 1)[-1] != "section":
            continue
        if clean(section.get("name")) == "标题":
            values = terminal_texts(section)
            title = values[0].strip() if values else ""
            if title:
                return title[:160]
    return "病程记录"


def unit_create_time(group: str, unit: etree._Element, root_fields: dict[str, list[str]]) -> tuple[str, datetime | None, str]:
    fields = field_values(unit)
    if group == "surgery":
        raw, parsed = first_named_time(fields, ("手术日期", "手术时间", "日期", "时间"))
        return raw, parsed, "SURGERY_TIME" if parsed else "NO_TIME"
    if group == "admission":
        raw, parsed = first_named_time(root_fields, ("入院时间", "入院日期", "记录时间", "记录日期"))
        return raw, parsed, "ADMISSION_TIME_FALLBACK" if parsed else "NO_TIME"
    head = "".join(terminal_texts(unit)[:3])[:200]
    parsed = parse_datetime(head)
    if parsed:
        match = DATE_RE.search(head)
        return match.group(0) if match else head, parsed, "PROGRESS_SECTION_TIME"
    raw, parsed = first_named_time(fields, ("记录时间", "记录日期", "时间", "日期"))
    return raw, parsed, "NAMED_FIELD_TIME" if parsed else "NO_TIME"


def existing_pre2021_keys() -> set[tuple[str, str, str, str]]:
    source = STAGE5_ROOT / "restricted" / "document_l1"
    dataset = ds.dataset(source, format="parquet")
    table = dataset.to_table(
        columns=["PATIENT_ID", "VISIT_ID", "文书名称", "create_time"],
        filter=pc.field("create_time") < pa.scalar(CUTOFF),
    )
    return {
        (normalize_id(row["PATIENT_ID"]), normalize_id(row["VISIT_ID"]), clean(row["文书名称"]), str(row["create_time"]))
        for row in table.to_pylist() if row.get("create_time")
    }


def identity_and_encounter_maps(patient_ids: set[str]) -> tuple[dict[str, str], set[str], dict[tuple[str, str], str]]:
    connection = sqlite3.connect(f"file:{STAGE6_DB}?mode=ro", uri=True)
    connection.execute("CREATE TEMP TABLE wanted(value TEXT PRIMARY KEY)")
    connection.executemany("INSERT INTO wanted VALUES (?)", ((value,) for value in sorted(patient_ids)))
    grouped: dict[str, set[str]] = defaultdict(set)
    for patient_id, uid in connection.execute(
        "SELECT a.normalized_value,a.patient_uid FROM identity_alias a JOIN wanted w "
        "ON w.value=a.normalized_value WHERE a.id_type='PATIENT_ID'"
    ):
        grouped[normalize_id(patient_id)].add(uid)
    conflicts = {key for key, values in grouped.items() if len(values) > 1}
    identities = {key: next(iter(values)) for key, values in grouped.items() if len(values) == 1}
    wanted_uids = set(identities.values())
    encounters = {
        (patient_uid, normalize_id(visit_id)): encounter_uid
        for encounter_uid, patient_uid, visit_id in connection.execute(
            "SELECT encounter_uid,patient_uid,visit_id_normalized FROM encounter_registry"
        ) if patient_uid in wanted_uids
    }
    connection.close()
    return identities, conflicts, encounters


class Writer:
    def __init__(self, path: Path, schema: pa.Schema, batch_size: int = 1000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.schema = schema
        self.writer = pq.ParquetWriter(path, schema, compression="zstd")
        self.rows: list[dict[str, Any]] = []
        self.batch_size = batch_size
        self.count = 0
        self.closed = False

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        self.count += 1
        if len(self.rows) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if self.rows:
            self.writer.write_table(pa.Table.from_pylist(self.rows, schema=self.schema))
            self.rows.clear()

    def close(self) -> None:
        if self.closed:
            return
        self.flush()
        self.writer.close()
        self.closed = True


def main() -> int:
    if not SOURCE_ROOT.is_dir():
        raise FileNotFoundError(SOURCE_ROOT)
    if OUTPUT_FINAL.exists() or L1_FINAL.exists():
        raise RuntimeError("legacy_xml_increment_output_already_exists")
    xml_files = sorted(SOURCE_ROOT.rglob("*.xml"))
    bak_files = sum(1 for _ in SOURCE_ROOT.rglob("*.bak"))
    parsed_names = {path: parse_filename(path) for path in xml_files}
    patient_ids = {value["patient_id"] for value in parsed_names.values() if value}
    identities, identity_conflicts, encounters = identity_and_encounter_maps(patient_ids)
    existing = existing_pre2021_keys()

    output_staging = OUTPUT_FINAL.with_name(OUTPUT_FINAL.name + ".staging")
    l1_staging = L1_FINAL.with_name(L1_FINAL.name + ".staging")
    if output_staging.exists() or l1_staging.exists():
        raise RuntimeError("legacy_xml_increment_staging_exists")
    delta = output_staging / "restricted" / "delta"
    writers = {
        "l1": Writer(l1_staging / "part-00001.parquet", L1_SCHEMA),
        "quarantine": Writer(output_staging / "restricted" / "quarantine" / "part-00001.parquet", QUARANTINE_SCHEMA),
        "link_audit": Writer(output_staging / "restricted" / "link_audit" / "part-00001.parquet", LINK_SCHEMA),
        "timeline_event": Writer(delta / "timeline_event" / "part-00001.parquet", TIMELINE_EVENT_SCHEMA),
        "event_time_evidence": Writer(delta / "event_time_evidence" / "part-00001.parquet", EVENT_TIME_EVIDENCE_SCHEMA),
        "event_source_map": Writer(delta / "event_source_map" / "part-00001.parquet", EVENT_SOURCE_MAP_SCHEMA),
        "document_day_detail": Writer(delta / "document_day_detail" / "part-00001.parquet", DOCUMENT_DAY_DETAIL_SCHEMA),
        "day_event_relation": Writer(delta / "day_event_relation" / "part-00001.parquet", DAY_EVENT_RELATION_SCHEMA),
    }
    counters: Counter[str] = Counter()
    by_year: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    day_stats: dict[tuple[str, date], dict[str, Any]] = defaultdict(lambda: {"documents": 0, "encounters": set(), "flags": Counter(), "procedures": 0})
    seen: set[tuple[str, str, str, str, str]] = set()

    try:
        for file_index, path in enumerate(xml_files, 1):
            counters["xml_files_scanned"] += 1
            metadata = parsed_names[path]
            relative = path.relative_to(SOURCE_ROOT).as_posix()
            if metadata is None:
                counters["filename_unparsed"] += 1
                writers["quarantine"].add({
                    "source_relative_path": relative, "source_record_id": relative,
                    "patient_id": "", "visit_id": "", "reason": "FILENAME_UNPARSED",
                    "detail": "", "rule_version": RULE_VERSION,
                })
                continue
            root, decoded, recovery_errors, parse_flags = parse_xml(path)
            if root is None:
                counters["xml_unrecoverable"] += 1
                writers["quarantine"].add({
                    "source_relative_path": relative, "source_record_id": relative,
                    "patient_id": metadata["patient_id"], "visit_id": metadata["visit_id"],
                    "reason": "XML_UNRECOVERABLE", "detail": "",
                    "rule_version": RULE_VERSION,
                })
                continue
            counters["xml_recovered_files"] += int(bool(recovery_errors))
            root_fields = field_values(root)
            admission_raw, admission_time = first_named_time(root_fields, ("入院时间", "入院日期"))
            discharge_raw, discharge_time = first_named_time(root_fields, ("出院时间", "出院日期"))
            units = progress_units(root) if metadata["document_group"] == "progress" else [(1, root)]
            counters["source_units"] += len(units)
            source_bytes = path.read_bytes()
            source_file_sha = sha256_bytes(source_bytes)
            for unit_index, unit in units:
                title = unit_title(metadata["document_group"], unit)
                content = document_text(unit)
                source_record_id = f"{relative}#unit={unit_index}"
                if not content:
                    counters["empty_content"] += 1
                    writers["quarantine"].add({
                        "source_relative_path": relative, "source_record_id": source_record_id,
                        "patient_id": metadata["patient_id"], "visit_id": metadata["visit_id"],
                        "reason": "EMPTY_CONTENT", "detail": "", "rule_version": RULE_VERSION,
                    })
                    continue
                create_raw, create_time, time_rule = unit_create_time(metadata["document_group"], unit, root_fields)
                if create_time is None:
                    counters["time_missing"] += 1
                    writers["quarantine"].add({
                        "source_relative_path": relative, "source_record_id": source_record_id,
                        "patient_id": metadata["patient_id"], "visit_id": metadata["visit_id"],
                        "reason": "DOCUMENT_TIME_MISSING", "detail": title, "rule_version": RULE_VERSION,
                    })
                    continue
                if create_time >= CUTOFF:
                    counters["post_2020_excluded"] += 1
                    continue
                content_bytes = content.encode("gb18030", errors="replace")
                content_sha = sha256_bytes(content_bytes)
                fingerprint = (
                    metadata["patient_id"], metadata["visit_id"], title, str(create_time), content_sha,
                )
                if fingerprint in seen:
                    counters["internal_duplicate"] += 1
                    continue
                seen.add(fingerprint)
                existing_key = (metadata["patient_id"], metadata["visit_id"], title, str(create_time))
                if existing_key in existing:
                    counters["existing_stage5_duplicate"] += 1
                    continue

                patient_uid = identities.get(metadata["patient_id"], "")
                encounter_uid = encounters.get((patient_uid, metadata["visit_id"]), "") if patient_uid else ""
                flags = list(parse_flags)
                if not encounter_uid:
                    flags.append("ENCOUNTER_UNMATCHED")
                if create_time.hour == 0 and create_time.minute == 0 and ":" not in create_raw:
                    flags.append("DATE_ONLY_TIME")
                if metadata["patient_id"] in identity_conflicts:
                    link_status = "PATIENT_ID_CONFLICT"
                elif not patient_uid:
                    link_status = "PATIENT_UNMATCHED"
                elif encounter_uid:
                    link_status = "HARD_PATIENT_AND_ENCOUNTER"
                else:
                    link_status = "HARD_PATIENT_ONLY"
                event_eligible = bool(patient_uid)
                source_key = stable_source_key(str(path), source_record_id)
                row_hash = sha256_bytes("|".join((
                    metadata["patient_id"], metadata["visit_id"], title, str(create_time), content_sha, source_record_id,
                )).encode("utf-8"))
                parser_status = "XML_RECOVERED" if recovery_errors else "OK"
                interval_status = "COMPLETE_INTERVAL" if admission_time and discharge_time else "OPEN_INTERVAL" if admission_time else "NO_INTERVAL_EVIDENCE"
                writers["l1"].add({
                    "PATIENT_ID": metadata["patient_id"], "VISIT_ID": metadata["visit_id"],
                    "LEGACY_INPATIENT_NO": metadata["inpatient_no"],
                    "ADMISSION_DATE_TIME": admission_raw, "DISCHARGE_DATE_TIME": discharge_raw,
                    "文书名称": title, "CREATE_DATE_TIME": create_raw, "文书内容": content_bytes,
                    "admission_time": admission_time, "discharge_time": discharge_time, "create_time": create_time,
                    "encounter_interval_status": interval_status, "content_length": len(content),
                    "physical_line_count": decoded.count("\n") + 1, "content_sha256": content_sha,
                    "source_file": str(path), "source_row": unit_index, "source_record_id": source_record_id,
                    "row_hash": row_hash, "parser_status": parser_status, "ingestion_version": VERSION,
                    "xml_recovery_error_count": recovery_errors, "source_relative_path": relative,
                    "source_folder": path.parent.name, "source_file_sha256": source_file_sha,
                })
                writers["link_audit"].add({
                    "source_record_key": source_key, "source_record_id": source_record_id,
                    "source_relative_path": relative, "patient_id": metadata["patient_id"],
                    "visit_id": metadata["visit_id"], "patient_uid": patient_uid,
                    "encounter_uid": encounter_uid, "link_status": link_status,
                    "event_eligible": event_eligible, "quality_flags": sorted(set(flags)),
                    "rule_version": RULE_VERSION,
                })
                counters["clean_l1_rows"] += 1
                counters[f"link_{link_status}"] += 1
                by_year[str(create_time.year)] += 1
                by_type[title] += 1
                if not event_eligible:
                    counters["stage7_ineligible_unlinked"] += 1
                    continue

                procedure = metadata["document_group"] == "surgery"
                event = {
                    "event_id": event_id("document", source_key, "DOCUMENT"),
                    "patient_uid": patient_uid, "event_date": create_time.date(),
                    "event_category": "document", "event_type": "DOCUMENT",
                    "sort_time": create_time, "clinical_time": create_time, "available_time": create_time,
                    "time_precision": "DAY" if "DATE_ONLY_TIME" in flags else "MINUTE",
                    "fallback_rule": time_rule, "source_system": "legacy_xml_document_v1",
                    "source_record_key": source_key, "event_label": title,
                    "narrative_eligible": True, "anchor_eligible": False,
                    "quality_flags": sorted(set(flags)), "rule_version": RULE_VERSION,
                }
                writers["timeline_event"].add(event)
                writers["event_time_evidence"].add({
                    "event_id": event["event_id"], "source_record_key": source_key,
                    "time_field": time_rule, "raw_time_value": create_raw, "parsed_time": create_time,
                    "time_role": "CLINICAL", "is_selected_for_sort": True,
                    "quality_flags": event["quality_flags"], "rule_version": RULE_VERSION,
                })
                writers["event_source_map"].add({
                    "event_id": event["event_id"], "source_system": "legacy_xml_document_v1",
                    "source_record_key": source_key, "source_file": str(path), "source_row": unit_index,
                    "source_record_id": source_record_id, "patient_uid": patient_uid,
                    "encounter_uid": encounter_uid, "source_disposition": "hard",
                    "event_eligible": True, "rule_version": RULE_VERSION,
                })
                writers["document_day_detail"].add({
                    "event_id": event["event_id"], "patient_uid": patient_uid,
                    "encounter_uid": encounter_uid, "source_record_key": source_key,
                    "source_file": str(path), "source_row": unit_index,
                    "source_record_id": source_record_id, "document_type": title,
                    "create_time": create_time, "admission_time": admission_time,
                    "discharge_time": discharge_time, "event_time_used": create_time,
                    "event_date": create_time.date(), "content_length": len(content),
                    "content_sha256": content_sha, "parser_status": parser_status,
                    "time_fallback_rule": time_rule, "procedure_candidate": procedure,
                    "anchor_eligible": False, "quality_flags": event["quality_flags"],
                    "rule_version": RULE_VERSION,
                })
                current_day_id = day_id(patient_uid, create_time.date())
                writers["day_event_relation"].add({
                    "day_id": current_day_id, "event_id": event["event_id"],
                    "event_category": "document", "relationship_type": "OCCURRED_ON_DAY",
                    "source_record_key": source_key,
                })
                day = day_stats[(patient_uid, create_time.date())]
                day["documents"] += 1
                if encounter_uid:
                    day["encounters"].add(encounter_uid)
                day["flags"].update(flags)
                day["procedures"] += int(procedure)
                counters["stage7_document_events"] += 1
            if file_index % 2500 == 0:
                print(json.dumps({"files": file_index, "total": len(xml_files), "l1_rows": counters["clean_l1_rows"], "events": counters["stage7_document_events"]}), flush=True)

        patient_day_writer = Writer(delta / "patient_day_delta" / "part-00001.parquet", PATIENT_DAY_SCHEMA)
        for (patient_uid, event_date), info in sorted(day_stats.items()):
            patient_day_writer.add({
                "day_id": day_id(patient_uid, event_date), "patient_uid": patient_uid,
                "event_date": event_date, "encounter_count": len(info["encounters"]),
                "lab_order_count": 0, "lab_item_count": 0, "document_count": info["documents"],
                "imaging_count": 0, "pathology_count": 0, "admission_flag": False,
                "discharge_flag": False, "procedure_candidate_count": info["procedures"],
                "narrative_eligible": True, "quality_flags": sorted(info["flags"]),
                "rule_version": RULE_VERSION,
            })
        patient_day_writer.close()
        writers["patient_day_delta"] = patient_day_writer
    finally:
        for writer in writers.values():
            try:
                writer.close()
            except Exception:
                pass

    manifests = {}
    for path in sorted(output_staging.rglob("*.parquet")):
        parquet = pq.ParquetFile(path)
        manifests[path.relative_to(output_staging).as_posix()] = {
            "rows": parquet.metadata.num_rows,
            "schema_sha256": schema_hash(parquet.schema_arrow),
            "sha256": sha256_bytes(path.read_bytes()),
        }
    l1_path = l1_staging / "part-00001.parquet"
    manifests[f"stage5/{l1_path.name}"] = {
        "rows": pq.ParquetFile(l1_path).metadata.num_rows,
        "schema_sha256": schema_hash(pq.read_schema(l1_path)),
        "sha256": sha256_bytes(l1_path.read_bytes()),
    }
    audit = {
        "status": "PASSED",
        "version": VERSION,
        "source_root": str(SOURCE_ROOT),
        "cutoff_exclusive": CUTOFF.isoformat(),
        "source_xml_files": len(xml_files),
        "source_bak_files_ignored": bak_files,
        "unique_filename_patient_ids": len(patient_ids),
        "unique_linked_patient_uids": len({row[0] for row in day_stats}),
        "patient_days_added": len(day_stats),
        "counters": dict(sorted(counters.items())),
        "rows_by_event_year": dict(sorted(by_year.items())),
        "top_document_types": dict(by_type.most_common(50)),
        "identity_conflict_patient_ids": len(identity_conflicts),
        "outputs": manifests,
        "merge_semantics": {
            "append_union": ["timeline_event", "event_time_evidence", "event_source_map", "document_day_detail", "day_event_relation"],
            "patient_day_delta": "reaggregate base and increment by day_id; document_count is additive, encounter_count is distinct-union",
            "existing_stage7_mutated": False,
        },
    }
    atomic_json(output_staging / "audit" / "acceptance.json", audit)
    commit_directory(l1_staging, L1_FINAL)
    commit_directory(output_staging, OUTPUT_FINAL)
    registry = {
        "status": "ACTIVE", "version": VERSION, "registered_at": datetime.now().isoformat(timespec="seconds"),
        "stage5_l1_root": str(L1_FINAL),
        "stage7_increment_root": str(OUTPUT_FINAL / "restricted" / "delta"),
        "audit_path": str(OUTPUT_FINAL / "audit" / "acceptance.json"),
        "merge_semantics": audit["merge_semantics"],
    }
    atomic_json(REGISTRY, registry)
    print(json.dumps({
        "status": audit["status"], "source_xml_files": len(xml_files),
        "clean_l1_rows": counters["clean_l1_rows"],
        "stage7_document_events": counters["stage7_document_events"],
        "unique_linked_patient_uids": audit["unique_linked_patient_uids"],
        "patient_days_added": audit["patient_days_added"],
        "post_2020_excluded": counters["post_2020_excluded"],
        "quarantine_rows": writers["quarantine"].count,
        "registry": str(REGISTRY),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

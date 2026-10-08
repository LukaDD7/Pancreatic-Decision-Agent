from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from scripts.cohort_construction.paths import data_root
from scripts.cohort_construction.source_semantics import time_precision
from scripts.cohort_construction.agent_packaging_and_audit.shared import stage8a

DATA_ROOT = data_root()


PROJECT_ROOT = DATA_ROOT / "pipeline_outputs_stage8_v1" / "pancreas_decision_window_enriched_v1"
RESTRICTED_ROOT = PROJECT_ROOT / "restricted"
COHORT_PATH = RESTRICTED_ROOT / "four_pathway_cohort_100_pre_t0.parquet"
IMAGING_INDEX_PATH = (
    DATA_ROOT
    / "pipeline_outputs_stage8_v1"
    / "pancreas_imaging_report_index_v2"
    / "restricted"
    / "index"
    / "report_index.parquet"
)
TIMELINE_PLAN_PATH = (
    DATA_ROOT / "pipeline_outputs_stage7_v1" / "restricted" / "full_day_timeline" / "full_plan_v1.json"
)
TIMELINE_TASK_ROOT = (
    DATA_ROOT / "pipeline_outputs_stage7_v1" / "restricted" / "full_day_timeline" / "tasks"
)
PATHOLOGY_ROOT = (
    DATA_ROOT
    / "pipeline_outputs_stage6_v2"
    / "restricted"
    / "increment"
    / "pathology_total_v2"
    / "full"
    / "pathology_record"
)
DEFAULT_OUT = RESTRICTED_ROOT / "patient_state_100_v1" / "outputs" / "state_100_20261003"

VERSION = "patient_state_100_v1_pre_t0"
LOOKBACK_DAYS = 180
IAP_REFERENCE = "Isaji et al. Pancreatology 2018;18:2-11. DOI:10.1016/j.pan.2017.11.011"


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {"nan", "NaT", "None", "<NA>"} else text


def parse_dt(value: Any) -> datetime | None:
    text = clean(value).replace("T", " ")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        parsed = pd.to_datetime(text, errors="coerce")
        return None if pd.isna(parsed) else parsed.to_pydatetime()


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(sep=" ", timespec="seconds") if dt else None


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def source(
    source_type: str,
    source_id: str,
    date_value: str | None,
    quote: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "type": source_type,
        "id": source_id,
        "date": date_value,
        "raw_text": clean(quote)[:1200],
    }
    if provenance:
        item["provenance"] = {key: clean(value) for key, value in provenance.items() if clean(value)}
    return item


def unknown(note: str = "T0 前可用资料未见该字段") -> dict[str, Any]:
    return {"value": None, "status": "unknown", "source": [], "note": note}


def fact(value: Any, sources: list[dict[str, Any]], status: str = "present", note: str | None = None, **extra: Any) -> dict[str, Any]:
    item = {"value": value, "status": status, "source": sources}
    if note:
        item["note"] = note
    item.update(extra)
    return item


def conflict(candidates: list[dict[str, Any]], note: str) -> dict[str, Any]:
    return {"value": "conflict", "status": "conflict", "candidates": candidates, "source": [], "note": note}


def decision_time(row: pd.Series) -> tuple[datetime, str]:
    citation = clean(row["信号证据引用"])
    match = re.search(r"@(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", citation)
    if match:
        return datetime.fromisoformat(f"{match.group(1)} {match.group(2)}"), "exam_datetime_proxy"
    day = pd.to_datetime(row["首次信号日期_检查日期暂代"]).to_pydatetime()
    return day.replace(hour=23, minute=59, second=59), "date_only_end_of_day_proxy"


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def filter_batches(paths: Iterable[Path], columns: list[str], patient_uids: set[str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    value_set = pa.array(sorted(patient_uids))
    for path in sorted(paths):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000, columns=columns):
            index = batch.schema.get_field_index("patient_uid")
            mask = pc.is_in(batch.column(index), value_set=value_set)
            filtered = batch.filter(mask)
            output.extend(filtered.to_pylist())
    return output


def pathology_files() -> list[Path]:
    files = sorted(PATHOLOGY_ROOT.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no pathology parquet shards under {PATHOLOGY_ROOT}")
    return files


def pathology_schema_names() -> list[str]:
    return pq.ParquetFile(pathology_files()[0]).schema_arrow.names


def read_pathology_table(columns: list[str]) -> pa.Table:
    tables = [pq.read_table(path, columns=columns) for path in pathology_files()]
    return tables[0] if len(tables) == 1 else pa.concat_tables(tables, promote_options="default")


def load_cohort_and_imaging() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    cohort = pd.read_parquet(COHORT_PATH).copy()
    cohort["患者ID"] = cohort["患者ID"].astype(str).str.strip()
    decision_values = cohort.apply(decision_time, axis=1)
    cohort["decision_time"] = [value[0] for value in decision_values]
    cohort["decision_time_basis"] = [value[1] for value in decision_values]
    ids = set(cohort["患者ID"])
    imaging_columns = [
        "index_report_uid",
        "source_record_key",
        "source_file_name",
        "patient_id",
        "patient_uid",
        "exam_date",
        "exam_time_raw",
        "exam_datetime",
        "exam_method",
        "description_deidentified",
        "diagnosis_deidentified",
        "report_deidentified",
    ]
    table = pq.read_table(IMAGING_INDEX_PATH, columns=imaging_columns)
    imaging = table.to_pandas()
    imaging["patient_id"] = imaging["patient_id"].astype(str).str.strip()
    mapping_rows = imaging[imaging["patient_id"].isin(ids)][["patient_id", "patient_uid"]].drop_duplicates()
    counts = mapping_rows.groupby("patient_id")["patient_uid"].nunique()
    if set(counts.index) != ids or (counts != 1).any():
        raise RuntimeError("patient_id_to_uid_mapping_not_unique_or_complete")
    id_to_uid = dict(zip(mapping_rows["patient_id"], mapping_rows["patient_uid"].astype(str)))
    imaging = imaging[imaging["patient_id"].isin(ids)].copy()
    imaging["exam_dt"] = pd.to_datetime(imaging["exam_datetime"], errors="coerce")
    return cohort, imaging, id_to_uid


def selected_tasks(patient_uids: set[str]) -> list[tuple[str, set[str]]]:
    plan = json.loads(TIMELINE_PLAN_PATH.read_text(encoding="utf-8"))
    output: list[tuple[str, set[str]]] = []
    covered: set[str] = set()
    for task in plan["tasks"]:
        overlap = patient_uids & set(map(str, task["patient_uids"]))
        if overlap:
            output.append((str(task["task_id"]), overlap))
            covered |= overlap
    if covered != patient_uids:
        raise RuntimeError("timeline_plan_missing_patient")
    return output


def load_timeline_rows(patient_uids: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    doc_columns = [
        "patient_uid",
        "source_record_key",
        "source_file",
        "source_record_id",
        "document_type",
        "create_time",
        "event_time_used",
        "event_date",
    ]
    lab_columns = [
        "patient_uid",
        "source_record_key",
        "item_name",
        "result_raw",
        "result_numeric_value",
        "result_qualitative_value",
        "result_text_value",
        "unit_raw",
        "unit_normalized",
        "sample_time",
        "report_time",
        "available_time",
        "event_time_used",
        "event_date",
        "source_abnormal_flag",
    ]
    pathology_columns = [
        "patient_uid",
        "pathology_record_uid",
        "source_record_key",
        "report_time",
        "event_time_used",
        "event_date",
        "pathology_time_sequence_conflict",
        "event_eligible",
    ]
    documents: list[dict[str, Any]] = []
    labs: list[dict[str, Any]] = []
    pathology: list[dict[str, Any]] = []
    for task_id, overlap in selected_tasks(patient_uids):
        root = TIMELINE_TASK_ROOT / task_id
        documents.extend(filter_batches((root / "document_day_detail").glob("*.parquet"), doc_columns, overlap))
        labs.extend(filter_batches((root / "lab_result_detail").glob("*.parquet"), lab_columns, overlap))
        pathology.extend(filter_batches((root / "pathology_event").glob("*.parquet"), pathology_columns, overlap))
    return documents, labs, pathology


def in_window(event_time: Any, t0: datetime, allow_same_time: bool = True) -> bool:
    dt = parse_dt(event_time)
    if dt is None:
        return False
    lower = t0 - timedelta(days=LOOKBACK_DAYS)
    return lower <= dt <= t0 if allow_same_time else lower <= dt < t0


def select_pre_t0_rows(
    rows: list[dict[str, Any]], t0_by_uid: dict[str, datetime], time_fields: list[str], date_only_same_day_excluded: bool = False
) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        uid = clean(row.get("patient_uid"))
        t0 = t0_by_uid[uid]
        selected_field = next((field for field in time_fields if parse_dt(row.get(field))), None)
        dt = parse_dt(row.get(selected_field)) if selected_field else None
        if dt is None:
            continue
        if date_only_same_day_excluded and dt.time() == datetime.min.time() and dt.date() == t0.date():
            continue
        if in_window(dt, t0):
            item = dict(row)
            item["_available_dt"] = dt
            item["_time_basis"] = selected_field
            item["_time_precision"] = time_precision(row.get(selected_field))
            output.append(item)
    return output


RELEVANT_DOC_TITLE = re.compile(
    r"入院|首次病程|病程记录|查房|会诊|MDT|多学科|术前讨论|术前小结|营养|NRS|评估|"
    r"ERCP|PTCD|PTBD|ENBD|支架|引流|穿刺|活检|化疗|放疗|新辅助|出院记录|出院小结",
    re.I,
)


def retrieve_document_text(rows: list[dict[str, Any]]) -> dict[str, str]:
    needed = {clean(row.get("source_record_key")) for row in rows}
    output: dict[str, str] = {}
    columns = ["source_file", "source_record_id", "PATIENT_ID", "VISIT_ID", stage8a.DOC_CONTENT]
    for parquet_path in sorted(stage8a.DOCUMENT_ROOT.rglob("*.parquet")):
        if not needed:
            break
        for batch in pq.ParquetFile(parquet_path).iter_batches(batch_size=10_000, columns=columns):
            values = batch.to_pydict()
            for index in range(batch.num_rows):
                key = stage8a.stable_source_key(
                    "document", clean(values["source_file"][index]), clean(values["source_record_id"][index])
                )
                if key not in needed:
                    continue
                raw = stage8a.document_text(values[stage8a.DOC_CONTENT][index])
                text = stage8a.redact_text(raw, [values["PATIENT_ID"][index], values["VISIT_ID"][index]], limit=50_000)
                output[key] = clean(text)
                needed.remove(key)
    for key in needed:
        output[key] = ""
    return output


def attach_document_text(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [row for row in rows if RELEVANT_DOC_TITLE.search(clean(row.get("document_type")))]
    text_map = retrieve_document_text(rows)
    output = []
    for row in rows:
        item = dict(row)
        item["text"] = text_map.get(clean(row.get("source_record_key")), "")
        if item["text"]:
            output.append(item)
    return output


def load_pathology_details(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    needed = {clean(row.get("source_record_key")) for row in events}
    if not needed:
        return []
    event_by_key = {clean(row.get("source_record_key")): row for row in events}
    columns = [
        "source_record_key",
        "病人编号",
        "性别",
        "年龄",
        "标本类型",
        "标本名称",
        "临床诊断",
        "病理诊断",
        "镜下所见",
        "收到日期",
        "报告日期",
        "pathology_record_uid",
    ]
    output = []
    value_set = pa.array(sorted(needed))
    for path in pathology_files():
        for batch in pq.ParquetFile(path).iter_batches(batch_size=20_000, columns=columns):
            index = batch.schema.get_field_index("source_record_key")
            filtered = batch.filter(pc.is_in(batch.column(index), value_set=value_set))
            for row in filtered.to_pylist():
                event = event_by_key.get(clean(row.get("source_record_key")))
                if not event:
                    continue
                item = dict(row)
                item.update({"patient_uid": event["patient_uid"], "_available_dt": event["_available_dt"]})
                output.append(item)
    return output


def imaging_text(row: pd.Series | dict[str, Any]) -> str:
    parts = [clean(row.get("description_deidentified")), clean(row.get("diagnosis_deidentified"))]
    text = "。".join(part for part in parts if part)
    return text or clean(row.get("report_deidentified"))


def quote_sentences(text: str, pattern: re.Pattern[str], limit: int = 3) -> list[str]:
    sentences = [part.strip() for part in re.split(r"[。；;\n\r]+", clean(text)) if part.strip()]
    return [sentence for sentence in sentences if pattern.search(sentence)][:limit]


def doc_source(row: dict[str, Any], quote: str) -> dict[str, Any]:
    dt = row.get("_available_dt")
    return source(
        "document",
        f"DOC:{clean(row.get('source_record_key'))[:16]}:{clean(row.get('document_type'))}",
        iso(dt),
        quote,
        {
            "source_record_key": row.get("source_record_key"),
            "source_file": row.get("source_file"),
            "source_record_id": row.get("source_record_id"),
            "document_type": row.get("document_type"),
        },
    )


def imaging_source(row: dict[str, Any] | pd.Series, quote: str) -> dict[str, Any]:
    dt = parse_dt(row.get("exam_dt") or row.get("exam_datetime"))
    return source(
        "imaging",
        f"IMG:{clean(row.get('index_report_uid'))}",
        iso(dt),
        quote,
        {
            "source_record_key": row.get("source_record_key"),
            "source_file": row.get("source_file_name"),
            "index_report_uid": row.get("index_report_uid"),
        },
    )


def lab_source(row: dict[str, Any]) -> dict[str, Any]:
    value = clean(row.get("result_raw") or row.get("result_numeric_value") or row.get("result_qualitative_value") or row.get("result_text_value"))
    unit = clean(row.get("unit_normalized") or row.get("unit_raw"))
    quote = f"{clean(row.get('item_name'))}={value}{unit}"
    return source(
        "laboratory",
        f"LAB:{clean(row.get('source_record_key'))[:16]}:{clean(row.get('item_name'))}",
        iso(row.get("_available_dt")),
        quote,
        {
            "source_record_key": row.get("source_record_key"),
            "item_name": row.get("item_name"),
            "available_time": row.get("available_time"),
            "report_time": row.get("report_time"),
            "sample_time": row.get("sample_time"),
        },
    )


def pathology_source(row: dict[str, Any], quote: str) -> dict[str, Any]:
    return source(
        "pathology",
        f"PATH:{clean(row.get('pathology_record_uid'))}",
        iso(row.get("_available_dt")),
        quote,
        {
            "source_record_key": row.get("source_record_key"),
            "pathology_record_uid": row.get("pathology_record_uid"),
            "report_date": row.get("report_date_parsed") or row.get("报告日期"),
            "specimen_date": row.get("specimen_date_parsed") or row.get("取材日期"),
        },
    )


def pick_demographic(docs: list[dict[str, Any]], pathology: list[dict[str, Any]], field: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for row in docs:
        text = clean(row.get("text"))[:1500]
        patterns = (
            [r"性别[：:]?\s*(男|女)", r"患者\[REDACTED_NAME\][，, ]+(男|女)[，, ]"]
            if field == "sex"
            else [r"年龄[：:]?\s*(\d{1,3})\s*岁", r"患者\[REDACTED_NAME\][，, ]+(?:男|女)[，, ]+(\d{1,3})\s*岁"]
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value: Any = match.group(1) if field == "sex" else int(match.group(1))
                candidates.append({"value": value, "source": doc_source(row, match.group(0))})
                break
    for row in pathology:
        raw = clean(row.get("性别" if field == "sex" else "年龄"))
        match = re.search(r"(男|女)", raw) if field == "sex" else re.search(r"(\d{1,3})", raw)
        if match:
            value = match.group(1) if field == "sex" else int(match.group(1))
            candidates.append({"value": value, "source": pathology_source(row, raw)})
    unique = sorted({item["value"] for item in candidates}, key=str)
    if not unique:
        return unknown()
    if len(unique) > 1:
        return conflict(candidates, f"T0 前不同来源记录了多个{field}值")
    selected = max(candidates, key=lambda item: item["source"].get("date") or "")
    return fact(selected["value"], [selected["source"]])


def latest_pancreas_reports(imaging: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relevant = [row for row in imaging if re.search(r"胰|pancre", imaging_text(row), re.I)]
    return sorted(relevant, key=lambda row: parse_dt(row.get("exam_dt") or row.get("exam_datetime")) or datetime.min, reverse=True)


def extract_tumor_location(imaging: list[dict[str, Any]]) -> dict[str, Any]:
    pattern = re.compile(r"胰(?:腺)?(头|钩突|颈|体尾|体|尾)部?")
    for row in latest_pancreas_reports(imaging):
        sentences = quote_sentences(imaging_text(row), pattern)
        if not sentences:
            continue
        matches = pattern.findall(sentences[0])
        if "体" in matches and "尾" in matches:
            return fact("胰体尾", [imaging_source(row, sentences[0])])
        match = pattern.search(sentences[0])
        value_map = {"头": "胰头", "钩突": "胰钩突", "颈": "胰颈", "体尾": "胰体尾", "体": "胰体", "尾": "胰尾"}
        return fact(value_map[match.group(1)], [imaging_source(row, sentences[0])])
    return unknown()


def extract_tumor_size(imaging: list[dict[str, Any]]) -> dict[str, Any]:
    lesion = re.compile(r"胰[^。；\n]{0,120}(?:癌|占位|病灶|肿物|肿块|结节)[^。；\n]{0,120}", re.I)
    dimension = re.compile(r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)(?:\s*[×xX*]\s*(\d+(?:\.\d+)?))?\s*(mm|cm|毫米|厘米)", re.I)
    for row in latest_pancreas_reports(imaging):
        for sentence in quote_sentences(imaging_text(row), lesion, limit=8):
            match = dimension.search(sentence)
            if not match:
                continue
            values = [float(value) for value in match.groups()[:3] if value]
            multiplier = 10.0 if match.group(4).lower() in {"cm", "厘米"} else 1.0
            return fact(round(max(values) * multiplier, 1), [imaging_source(row, sentence)], unit="mm", dimensions_raw=match.group(0))
    return unknown()


VESSELS = {
    "sma": re.compile(r"SMA|肠系膜上动脉", re.I),
    "celiac_axis": re.compile(r"腹腔干(?:动脉)?|腹腔动脉|\bCA\b", re.I),
    "smv_pv": re.compile(r"SMV|PV|肠系膜上静脉|门静脉", re.I),
    "cha": re.compile(r"CHA|肝总动脉", re.I),
}
NEGATIVE_VASCULAR = re.compile(r"未见[^。；]{0,18}(?:侵犯|受侵|累及|包绕|接触)|(?:分界|界限)清|无[^。；]{0,12}(?:侵犯|受侵|累及|接触)")
LT180 = re.compile(r"(?:<|＜|小于|不足)\s*180\s*[°度]|180\s*[°度]\s*(?:以下|以内)")
LE180 = re.compile(r"(?:<=|≤|小于等于)\s*180\s*[°度]")
GE180 = re.compile(r"(?:>=|≥|大于等于|超过|>|＞)\s*180\s*[°度]|180\s*[°度]\s*(?:以上|及以上)")
POSITIVE_VASCULAR = re.compile(r"关系密切|紧贴|相贴|接触|侵犯|受侵|累及|包绕|环绕|狭窄|闭塞|变形")


def vascular_value(sentence: str) -> tuple[str, bool | None]:
    if NEGATIVE_VASCULAR.search(sentence):
        return "未见明确接触或侵犯", True
    if LT180.search(sentence):
        return "接触<180°", True
    if LE180.search(sentence):
        return "接触≤180°", True
    if GE180.search(sentence):
        return "接触≥180°", True
    match = POSITIVE_VASCULAR.search(sentence)
    if match:
        return match.group(0), None
    return "提及但关系未量化", None


def extract_vascular(imaging: list[dict[str, Any]], vessel_pattern: re.Pattern[str]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for row in latest_pancreas_reports(imaging)[:6]:
        for sentence in quote_sentences(imaging_text(row), vessel_pattern, limit=4):
            clauses = [part.strip() for part in re.split(r"[，,、]", sentence) if part.strip()]
            target_clause = next((part for part in clauses if vessel_pattern.search(part)), sentence)
            value, quantified = vascular_value(target_clause)
            candidates.append({"value": value, "quantified": quantified, "source": imaging_source(row, target_clause)})
    if not candidates:
        return unknown()
    polarity = {"negative" if item["value"].startswith("未见") else "positive" for item in candidates}
    if len(polarity) > 1:
        return conflict(candidates, "T0 前不同影像对该血管关系描述不一致")
    selected = max(candidates, key=lambda item: item["source"].get("date") or "")
    return fact(selected["value"], [selected["source"]], quantified=selected["quantified"], candidates=candidates)


def map_resectability(vascular: dict[str, dict[str, Any]], disease_stratum: str) -> dict[str, Any]:
    if disease_stratum != "PDAC":
        return unknown("IAP 2017 PDAC 可切除性规则未用于非 PDAC 或病种待定病例")
    values = {key: clean(item.get("value")) for key, item in vascular.items()}
    arterial = values["sma"] + " " + values["celiac_axis"]
    if re.search(r"≥180|包绕|环绕|闭塞|狭窄|变形", arterial):
        return fact(
            "locally_advanced",
            [],
            note="保守规则映射：SMA/腹腔干存在≥180°或包绕/闭塞/狭窄/变形描述",
            rule_reference=IAP_REFERENCE,
        )
    if re.search(r"<180", arterial):
        return fact("borderline_resectable", [], note="IAP 2017 解剖学规则映射", rule_reference=IAP_REFERENCE)
    if values["cha"] == "接触<180°":
        return fact("borderline_resectable", [], note="IAP 2017 解剖学规则映射", rule_reference=IAP_REFERENCE)
    if all(value.startswith("未见") for value in values.values()):
        return fact("resectable", [], note="四组关键血管均有明确阴性描述", rule_reference=IAP_REFERENCE)
    return unknown("关键血管关系未完整量化，未自动推断可切除性")


LAB_PATTERNS = {
    "ca19_9": re.compile(r"CA\s*19[-－]?9|糖类抗原\s*19[-－]?9", re.I),
    "cea": re.compile(r"癌胚抗原|\bCEA\b", re.I),
    "ca125": re.compile(r"CA\s*125|糖类抗原\s*125", re.I),
    "albumin": re.compile(r"^(?:血清)?白蛋白$|^ALB$", re.I),
    "total_bilirubin": re.compile(r"总胆红素|TBIL", re.I),
    "alp": re.compile(r"碱性磷酸酶|\bALP\b", re.I),
    "ggt": re.compile(r"谷氨酰转移酶|谷氨酰转肽酶|\bGGT\b", re.I),
}


def numeric_value(row: dict[str, Any]) -> float | None:
    value = row.get("result_numeric_value")
    if value is not None and not (isinstance(value, float) and math.isnan(value)):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    match = re.search(r"[-+]?\d+(?:\.\d+)?", clean(row.get("result_raw")))
    return float(match.group(0)) if match else None


def matching_labs(labs: list[dict[str, Any]], pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    return sorted(
        [row for row in labs if pattern.search(clean(row.get("item_name")))],
        key=lambda row: row.get("_available_dt") or datetime.min,
    )


def latest_lab(labs: list[dict[str, Any]], pattern: re.Pattern[str]) -> dict[str, Any]:
    rows = matching_labs(labs, pattern)
    if not rows:
        return unknown()
    row = rows[-1]
    value = numeric_value(row)
    if value is None:
        value = clean(row.get("result_raw") or row.get("result_qualitative_value") or row.get("result_text_value"))
    return fact(
        value,
        [lab_source(row)],
        unit=clean(row.get("unit_normalized") or row.get("unit_raw")) or None,
        date=iso(row.get("_available_dt")),
        abnormal_flag=clean(row.get("source_abnormal_flag")) or None,
    )


def ca19_trend(labs: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in matching_labs(labs, LAB_PATTERNS["ca19_9"]) if numeric_value(row) is not None]
    if len(rows) < 2:
        return unknown("T0 前少于两次可解析 CA19-9，无法评估趋势")
    observations = [
        {"value": numeric_value(row), "unit": clean(row.get("unit_normalized") or row.get("unit_raw")), "date": iso(row.get("_available_dt")), "source": lab_source(row)}
        for row in rows
    ]
    latest, previous = observations[-1], observations[-2]
    direction = "升高" if latest["value"] > previous["value"] else "降低" if latest["value"] < previous["value"] else "持平"
    return fact(direction, [previous["source"], latest["source"]], observations=observations, note="仅描述最近两次实测值方向，不设置临床显著性阈值")


def extract_ecog(docs: list[dict[str, Any]]) -> dict[str, Any]:
    pattern = re.compile(r"(?:ECOG(?:\s*PS)?|体力状况评分|PS评分)\s*[：:]?\s*([0-4])(?:\s*分)?", re.I)
    candidates = []
    for row in docs:
        for match in pattern.finditer(clean(row.get("text"))):
            candidates.append({"value": int(match.group(1)), "source": doc_source(row, match.group(0))})
    values = sorted({item["value"] for item in candidates})
    if not values:
        return unknown()
    if len(values) > 1:
        return conflict(candidates, "T0 前文书记录了多个 ECOG/PS 数值，未自动仲裁")
    return fact(values[0], [max(candidates, key=lambda item: item["source"].get("date") or "")["source"]])


def extract_weight_loss(docs: list[dict[str, Any]]) -> dict[str, Any]:
    pattern = re.compile(r"(?:体重|消瘦)[^。；\n]{0,35}(?:下降|减轻|减少)[^。；\n]{0,20}(?:\d+(?:\.\d+)?\s*(?:kg|公斤|斤|%))", re.I)
    for row in sorted(docs, key=lambda item: item.get("_available_dt") or datetime.min, reverse=True):
        match = pattern.search(clean(row.get("text")))
        if match:
            return fact(match.group(0), [doc_source(row, match.group(0))])
    return unknown()


def extract_biliary_drainage(docs: list[dict[str, Any]]) -> dict[str, Any]:
    pattern = re.compile(r"(?:PTCD|PTBD|ENBD|ERCP|胆道支架|胆管支架|胆道引流|胆管引流)[^。；\n]{0,80}", re.I)
    candidates = []
    for row in docs:
        for match in pattern.finditer(clean(row.get("text"))):
            candidates.append({"value": match.group(0), "source": doc_source(row, match.group(0))})
    if not candidates:
        return unknown()
    selected = max(candidates, key=lambda item: item["source"].get("date") or "")
    return fact(selected["value"], [selected["source"]], candidates=candidates[-5:])


def extract_nrs(docs: list[dict[str, Any]]) -> dict[str, Any]:
    pattern = re.compile(r"NRS\s*[-_ ]?2002[^。；\n]{0,20}?([0-7])\s*分?", re.I)
    candidates = []
    for row in docs:
        for match in pattern.finditer(clean(row.get("text"))):
            candidates.append({"value": int(match.group(1)), "source": doc_source(row, match.group(0))})
    values = sorted({item["value"] for item in candidates})
    if not values:
        return unknown()
    if len(values) > 1:
        return conflict(candidates, "T0 前 NRS 2002 记录不一致")
    return fact(values[0], [max(candidates, key=lambda item: item["source"].get("date") or "")["source"]])


COMORBIDITIES = {
    "高血压": re.compile(r"高血压"),
    "糖尿病": re.compile(r"(?:2型|II型|Ⅱ型)?糖尿病"),
    "冠心病": re.compile(r"冠心病|冠状动脉粥样硬化性心脏病"),
    "心房颤动": re.compile(r"房颤|心房颤动"),
    "脑卒中史": re.compile(r"脑梗死|脑卒中|脑出血"),
    "慢性肾病": re.compile(r"慢性肾功能不全|慢性肾病|尿毒症"),
    "慢性肝病": re.compile(r"肝硬化|慢性肝炎"),
    "慢阻肺": re.compile(r"慢性阻塞性肺疾病|慢阻肺"),
}


def extract_comorbidities(docs: list[dict[str, Any]]) -> dict[str, Any]:
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in docs:
        text = clean(row.get("text"))
        for label, pattern in COMORBIDITIES.items():
            match = pattern.search(text)
            if match:
                snippet = text[max(0, match.start() - 30) : min(len(text), match.end() + 50)]
                if re.search(r"否认[^。；]{0,20}" + re.escape(match.group(0)), snippet):
                    continue
                found[label].append(doc_source(row, snippet))
    if not found:
        return unknown()
    sources = [items[-1] for items in found.values()]
    return fact(sorted(found), sources)


def extract_ln_imaging(imaging: list[dict[str, Any]]) -> dict[str, Any]:
    suspicious = re.compile(r"淋巴结[^。；\n]{0,45}(?:转移|肿大|增大|异常|可疑)|(?:转移|肿大|增大|异常|可疑)[^。；\n]{0,45}淋巴结")
    negative = re.compile(r"(?:未见|无)[^。；\n]{0,30}(?:肿大|异常|转移)淋巴结|淋巴结[^。；\n]{0,30}(?:未见肿大|未见异常|无转移)")
    for row in latest_pancreas_reports(imaging):
        text = imaging_text(row)
        quotes = quote_sentences(text, negative)
        if quotes:
            return fact("未见影像学可疑淋巴结", [imaging_source(row, quotes[0])], status="absent")
        quotes = [quote for quote in quote_sentences(text, suspicious) if not re.search(r"未见|无", quote)]
        if quotes:
            return fact("可疑", [imaging_source(row, quotes[0])])
    return unknown()


def extract_ln_pathology(pathology: list[dict[str, Any]]) -> dict[str, Any]:
    positive = re.compile(r"淋巴结[^。；\n]{0,45}(?:转移|癌)|(?:转移|癌)[^。；\n]{0,45}淋巴结")
    negative = re.compile(r"淋巴结[^。；\n]{0,45}(?:未见|无)[^。；\n]{0,15}(?:转移|癌)")
    for row in sorted(pathology, key=lambda item: item.get("_available_dt") or datetime.min, reverse=True):
        text = clean(row.get("病理诊断")) + "。" + clean(row.get("镜下所见"))
        match = positive.search(text)
        if match:
            return fact("病理证实阳性", [pathology_source(row, match.group(0))])
        match = negative.search(text)
        if match:
            return fact("病理未见转移", [pathology_source(row, match.group(0))], status="absent")
    return unknown("T0 前无区域淋巴结病理确认")


def extract_histology(pathology: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    for row in sorted(pathology, key=lambda item: item.get("_available_dt") or datetime.min):
        diagnosis = clean(row.get("病理诊断"))
        if diagnosis:
            candidates.append({"value": diagnosis, "source": pathology_source(row, diagnosis)})
    if not candidates:
        return unknown("T0 前无可用病理诊断")
    selected = candidates[-1]
    return fact(selected["value"], [selected["source"]], candidates=candidates)


def extract_distant_signal(case: pd.Series, imaging_rows: list[dict[str, Any]]) -> dict[str, Any]:
    signal_uid = clean(case["信号证据引用"]).split("@", 1)[0]
    row = next((item for item in imaging_rows if clean(item.get("index_report_uid")) == signal_uid), None)
    quote = clean(case["信号原文"])
    src = imaging_source(row, quote) if row else source("cohort_signal", f"SIGNAL:{signal_uid}", iso(case["decision_time"]), quote)
    return fact(
        {"assessment": "suspicious_distant_metastasis", "site": clean(case["部位"]), "signal_level": clean(case["信号等级"]), "raw_text": quote},
        [src],
        note="决策触发信号；不等同于已证实 M1",
    )


def cholestasis_context(labs: list[dict[str, Any]], docs: list[dict[str, Any]]) -> dict[str, Any]:
    bilirubin = latest_lab(labs, LAB_PATTERNS["total_bilirubin"])
    alp = latest_lab(labs, LAB_PATTERNS["alp"])
    ggt = latest_lab(labs, LAB_PATTERNS["ggt"])
    doc_candidates = []
    pattern = re.compile(r"梗阻性黄疸|胆道梗阻|胆管梗阻|黄疸")
    for row in docs:
        match = pattern.search(clean(row.get("text")))
        if match:
            doc_candidates.append(doc_source(row, match.group(0)))
    if all(item["status"] == "unknown" for item in [bilirubin, alp, ggt]) and not doc_candidates:
        return unknown("无可用胆汁淤积相关检验或明确文书描述")
    return fact(
        {"total_bilirubin": bilirubin, "alp": alp, "ggt": ggt, "documented_jaundice_or_obstruction": bool(doc_candidates)},
        doc_candidates,
        note="仅呈现原始背景，不对 CA19-9 作数值校正",
    )


def status_at(root: dict[str, Any], path: str) -> str:
    node: Any = root
    for part in path.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return clean(node.get("status")) or "unknown"


AUDIT_FIELDS = {
    "patient_demographics.age": "medium",
    "patient_demographics.sex": "medium",
    "A_anatomy.tumor_location": "high",
    "A_anatomy.tumor_size_mm": "medium",
    "A_anatomy.vascular_contact.sma": "high",
    "A_anatomy.vascular_contact.celiac_axis": "high",
    "A_anatomy.vascular_contact.smv_pv": "high",
    "A_anatomy.vascular_contact.cha": "high",
    "A_anatomy.resectability_class": "high",
    "A_anatomy.distant_metastasis": "high",
    "B_biology.histology": "high",
    "B_biology.ca19_9": "high",
    "B_biology.ca19_9_trend": "medium",
    "B_biology.lymph_node_status.imaging_suspicion": "medium",
    "B_biology.lymph_node_status.pathology_confirmation": "medium",
    "C_condition.ecog_ps": "high",
    "C_condition.weight_loss": "medium",
    "C_condition.biliary_drainage": "medium",
    "C_condition.nutritional_status.albumin": "medium",
    "C_condition.nutritional_status.nrs_2002": "medium",
    "C_condition.comorbidity_burden.specific_comorbidities": "medium",
}


def get_node(root: dict[str, Any], path: str) -> dict[str, Any]:
    node: Any = root
    for part in path.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def build_uncertainty(state: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for path, impact in AUDIT_FIELDS.items():
        node = get_node(state, path)
        status = clean(node.get("status")) or "unknown"
        item = {"field": path, "status": status, "decision_impact": impact}
        if status in {"unknown", "conflict"}:
            item["reason"] = clean(node.get("note")) or ("资料冲突" if status == "conflict" else "T0 前资料未见")
        elif node.get("source"):
            item["source"] = node["source"]
        output.append(item)
    return output


def completeness(state: dict[str, Any]) -> dict[str, Any]:
    scores = {"present": 1.0, "absent": 1.0, "conflict": 0.5, "unknown": 0.0}
    values = [scores.get(status_at(state, path), 0.0) for path in AUDIT_FIELDS]
    overall = sum(values) / len(values)
    high_paths = [path for path, impact in AUDIT_FIELDS.items() if impact == "high"]
    high_unknown = sum(status_at(state, path) == "unknown" for path in high_paths)
    grade = "A" if overall >= 0.75 and high_unknown <= 2 else "B" if overall >= 0.50 else "C"
    return {
        "score": round(overall, 3),
        "grade": grade,
        "known_or_explicit_absent_fields": sum(status_at(state, path) in {"present", "absent"} for path in AUDIT_FIELDS),
        "conflict_fields": sum(status_at(state, path) == "conflict" for path in AUDIT_FIELDS),
        "unknown_fields": sum(status_at(state, path) == "unknown" for path in AUDIT_FIELDS),
        "high_impact_unknown_fields": high_unknown,
    }


def build_timeline(imaging: list[dict[str, Any]], docs: list[dict[str, Any]], pathology: list[dict[str, Any]], labs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for row in imaging:
        events.append({"date": iso(parse_dt(row.get("exam_dt") or row.get("exam_datetime"))), "type": "imaging", "source_id": f"IMG:{clean(row.get('index_report_uid'))}", "summary": imaging_text(row)[:500]})
    for row in docs:
        text = clean(row.get("text"))
        events.append({"date": iso(row.get("_available_dt")), "type": "document", "source_id": f"DOC:{clean(row.get('source_record_key'))[:16]}", "summary": f"{clean(row.get('document_type'))}: {text[:400]}"})
    for row in pathology:
        events.append({"date": iso(row.get("_available_dt")), "type": "pathology", "source_id": f"PATH:{clean(row.get('pathology_record_uid'))}", "summary": clean(row.get("病理诊断"))[:500]})
    key_labs = [row for row in labs if any(pattern.search(clean(row.get("item_name"))) for pattern in LAB_PATTERNS.values())]
    for row in key_labs:
        events.append({"date": iso(row.get("_available_dt")), "type": "laboratory", "source_id": lab_source(row)["id"], "summary": lab_source(row)["raw_text"]})
    events.sort(key=lambda item: item["date"] or "")
    return events[-20:]


def readable_field(label: str, node: dict[str, Any]) -> str:
    status = clean(node.get("status")) or "unknown"
    if status == "unknown":
        return f"- {label}：未知（{clean(node.get('note')) or 'T0 前资料未见'}）"
    if status == "conflict":
        values = [clean(item.get("value")) for item in node.get("candidates", [])]
        return f"- {label}：冲突（{'；'.join(values[:4])}）"
    value = node.get("value")
    if isinstance(value, (dict, list)):
        value = json_text(value)
    unit = f" {node.get('unit')}" if node.get("unit") else ""
    source_text = ""
    if node.get("source"):
        src = node["source"][0]
        source_text = f"（来源：{src.get('id')}@{src.get('date')}）"
    return f"- {label}：{value}{unit}{source_text}"


def render_state(state: dict[str, Any]) -> str:
    a = state["A_anatomy"]
    b = state["B_biology"]
    c = state["C_condition"]
    lines = [
        f"【病例】{state['case_id']}",
        f"【决策时点】{state['decision_timepoint']}，首次强可疑远处转移信号",
        "",
        "【A 解剖学】",
        readable_field("肿瘤位置", a["tumor_location"]),
        readable_field("肿瘤最大径", a["tumor_size_mm"]),
        readable_field("SMA", a["vascular_contact"]["sma"]),
        readable_field("腹腔干", a["vascular_contact"]["celiac_axis"]),
        readable_field("SMV/PV", a["vascular_contact"]["smv_pv"]),
        readable_field("肝总动脉", a["vascular_contact"]["cha"]),
        readable_field("解剖学可切除性", a["resectability_class"]),
        readable_field("远处转移信号", a["distant_metastasis"]),
        "",
        "【B 生物学】",
        readable_field("T0 前病理", b["histology"]),
        readable_field("CA19-9", b["ca19_9"]),
        readable_field("CA19-9趋势", b["ca19_9_trend"]),
        readable_field("影像淋巴结", b["lymph_node_status"]["imaging_suspicion"]),
        readable_field("病理淋巴结", b["lymph_node_status"]["pathology_confirmation"]),
        "",
        "【C 条件】",
        readable_field("ECOG PS", c["ecog_ps"]),
        readable_field("体重变化", c["weight_loss"]),
        readable_field("胆道引流", c["biliary_drainage"]),
        readable_field("白蛋白", c["nutritional_status"]["albumin"]),
        readable_field("NRS 2002", c["nutritional_status"]["nrs_2002"]),
        readable_field("合并症", c["comorbidity_burden"]["specific_comorbidities"]),
        "",
        f"【完整性】{state['state_completeness']['grade']}，得分 {state['state_completeness']['score']:.3f}",
        "【决策相关不确定性】",
    ]
    uncertain = [item for item in state["uncertainty_ledger"] if item["status"] in {"unknown", "conflict"}]
    lines.extend(f"{index}. {item['field']}：{item['status']}；{item.get('reason', '')}；影响={item['decision_impact']}" for index, item in enumerate(uncertain, 1))
    return "\n".join(lines)


def build_states(output_dir: Path) -> dict[str, Any]:
    cohort, imaging_df, id_to_uid = load_cohort_and_imaging()
    cohort["patient_uid"] = cohort["患者ID"].map(id_to_uid)
    t0_by_uid = dict(zip(cohort["patient_uid"], cohort["decision_time"]))
    documents_raw, labs_raw, pathology_events_raw = load_timeline_rows(set(t0_by_uid))
    documents_selected = select_pre_t0_rows(documents_raw, t0_by_uid, ["create_time", "event_time_used"])
    documents = attach_document_text(documents_selected)
    labs = select_pre_t0_rows(labs_raw, t0_by_uid, ["available_time", "report_time", "event_time_used"])
    pathology_events = select_pre_t0_rows(
        pathology_events_raw, t0_by_uid, ["report_time", "event_time_used"], date_only_same_day_excluded=True
    )
    pathology = load_pathology_details(pathology_events)

    imaging_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in imaging_df.to_dict("records"):
        patient_id = clean(row.get("patient_id"))
        t0 = cohort.loc[cohort["患者ID"] == patient_id, "decision_time"].iloc[0]
        dt = parse_dt(row.get("exam_dt") or row.get("exam_datetime"))
        if dt and in_window(dt, t0):
            imaging_by_id[patient_id].append(row)
    docs_by_uid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labs_by_uid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    path_by_uid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in documents:
        docs_by_uid[clean(row.get("patient_uid"))].append(row)
    for row in labs:
        labs_by_uid[clean(row.get("patient_uid"))].append(row)
    for row in pathology:
        path_by_uid[clean(row.get("patient_uid"))].append(row)

    states: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    flat_rows: list[dict[str, Any]] = []
    field_rows: list[dict[str, Any]] = []
    uncertainty_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []

    for _, case in cohort.iterrows():
        patient_id = clean(case["患者ID"])
        uid = clean(case["patient_uid"])
        case_imaging = imaging_by_id[patient_id]
        case_docs = docs_by_uid[uid]
        case_labs = labs_by_uid[uid]
        case_pathology = path_by_uid[uid]
        age = pick_demographic(case_docs, case_pathology, "age")
        sex = pick_demographic(case_docs, case_pathology, "sex")
        ecog = extract_ecog(case_docs)
        comorbidities = extract_comorbidities(case_docs)
        vascular = {key: extract_vascular(case_imaging, pattern) for key, pattern in VESSELS.items()}
        stratum = clean(case["病种"]) or "病种待定"
        ca19 = latest_lab(case_labs, LAB_PATTERNS["ca19_9"])
        if ca19["status"] == "present" and isinstance(ca19.get("value"), (int, float)):
            ca19["iap_b_threshold_gt_500"] = ca19["value"] > 500
            ca19["rule_reference"] = IAP_REFERENCE
        state: dict[str, Any] = {
            "case_id": patient_id,
            "patient_uid": uid,
            "decision_timepoint": iso(case["decision_time"]),
            "decision_timepoint_basis": clean(case["decision_time_basis"]),
            "snapshot_type": "first_strong_suspicious_distant_metastasis_signal",
            "patient_demographics": {"age": age, "sex": sex, "ecog_ps": ecog, "comorbidities": comorbidities},
            "A_anatomy": {
                "tumor_location": extract_tumor_location(case_imaging),
                "tumor_size_mm": extract_tumor_size(case_imaging),
                "vascular_contact": vascular,
                "resectability_class": map_resectability(vascular, stratum),
                "distant_metastasis": extract_distant_signal(case, case_imaging),
            },
            "B_biology": {
                "histology": extract_histology(case_pathology),
                "ca19_9": ca19,
                "ca19_9_trend": ca19_trend(case_labs),
                "cholestasis_context": cholestasis_context(case_labs, case_docs),
                "lymph_node_status": {
                    "imaging_suspicion": extract_ln_imaging(case_imaging),
                    "pathology_confirmation": extract_ln_pathology(case_pathology),
                },
                "tumor_markers_other": {
                    "cea": latest_lab(case_labs, LAB_PATTERNS["cea"]),
                    "ca125": latest_lab(case_labs, LAB_PATTERNS["ca125"]),
                },
            },
            "C_condition": {
                "ecog_ps": ecog,
                "weight_loss": extract_weight_loss(case_docs),
                "biliary_drainage": extract_biliary_drainage(case_docs),
                "nutritional_status": {"albumin": latest_lab(case_labs, LAB_PATTERNS["albumin"]), "nrs_2002": extract_nrs(case_docs)},
                "comorbidity_burden": {"charlson_index": unknown("不从自由文本自动计算 CCI"), "specific_comorbidities": comorbidities},
            },
            "timeline": build_timeline(case_imaging, case_docs, case_pathology, case_labs),
            "temporal_guard": {
                "lookback_days": LOOKBACK_DAYS,
                "future_data_in_state": False,
                "same_day_policy": "仅纳入时间戳明确不晚于T0的资料；日期粒度病理同日排除",
                "imaging_time_limitation": "影像源无签发/审核时间，T0使用检查时间代理",
            },
            "rule_version": VERSION,
        }
        state["uncertainty_ledger"] = build_uncertainty(state)
        state["state_completeness"] = completeness(state)
        states.append(state)

        outcome_fields = [
            "最终远处转移状态",
            "M状态证据",
            "计划术式",
            "计划证据引用",
            "实际处置日期",
            "实际术式/处置",
            "切除性质",
            "实际处置证据引用",
            "术中发现分类",
            "术中发现证据引用",
            "明确取消/终止语义",
            "取消/终止证据引用",
            "四类路径",
            "根治切除实施状态",
            "决策链状态",
        ]
        outcomes.append({"case_id": patient_id, "decision_timepoint": iso(case["decision_time"]), **{field: clean(case[field]) for field in outcome_fields}})

        flat = {
            "患者ID": patient_id,
            "分析病种分层": stratum,
            "决策时点": iso(case["decision_time"]),
            "T0时间依据": clean(case["decision_time_basis"]),
            "年龄": age.get("value"),
            "年龄状态": age["status"],
            "性别": sex.get("value"),
            "性别状态": sex["status"],
            "肿瘤位置": state["A_anatomy"]["tumor_location"].get("value"),
            "肿瘤位置状态": state["A_anatomy"]["tumor_location"]["status"],
            "肿瘤最大径mm": state["A_anatomy"]["tumor_size_mm"].get("value"),
            "肿瘤大小状态": state["A_anatomy"]["tumor_size_mm"]["status"],
            "SMA关系": vascular["sma"].get("value"),
            "SMA状态": vascular["sma"]["status"],
            "腹腔干关系": vascular["celiac_axis"].get("value"),
            "腹腔干状态": vascular["celiac_axis"]["status"],
            "SMV_PV关系": vascular["smv_pv"].get("value"),
            "SMV_PV状态": vascular["smv_pv"]["status"],
            "肝总动脉关系": vascular["cha"].get("value"),
            "肝总动脉状态": vascular["cha"]["status"],
            "解剖学可切除性": state["A_anatomy"]["resectability_class"].get("value"),
            "可切除性状态": state["A_anatomy"]["resectability_class"]["status"],
            "可疑远处部位": clean(case["部位"]),
            "可疑信号原文": clean(case["信号原文"]),
            "T0前病理": state["B_biology"]["histology"].get("value"),
            "病理状态": state["B_biology"]["histology"]["status"],
            "CA19_9": ca19.get("value"),
            "CA19_9单位": ca19.get("unit"),
            "CA19_9状态": ca19["status"],
            "CA19_9趋势": state["B_biology"]["ca19_9_trend"].get("value"),
            "趋势状态": state["B_biology"]["ca19_9_trend"]["status"],
            "影像淋巴结": state["B_biology"]["lymph_node_status"]["imaging_suspicion"].get("value"),
            "影像淋巴结状态": state["B_biology"]["lymph_node_status"]["imaging_suspicion"]["status"],
            "病理淋巴结": state["B_biology"]["lymph_node_status"]["pathology_confirmation"].get("value"),
            "病理淋巴结状态": state["B_biology"]["lymph_node_status"]["pathology_confirmation"]["status"],
            "ECOG_PS": ecog.get("value"),
            "ECOG状态": ecog["status"],
            "体重变化": state["C_condition"]["weight_loss"].get("value"),
            "体重变化状态": state["C_condition"]["weight_loss"]["status"],
            "胆道引流": state["C_condition"]["biliary_drainage"].get("value"),
            "胆道引流状态": state["C_condition"]["biliary_drainage"]["status"],
            "白蛋白": state["C_condition"]["nutritional_status"]["albumin"].get("value"),
            "白蛋白单位": state["C_condition"]["nutritional_status"]["albumin"].get("unit"),
            "白蛋白状态": state["C_condition"]["nutritional_status"]["albumin"]["status"],
            "NRS2002": state["C_condition"]["nutritional_status"]["nrs_2002"].get("value"),
            "NRS状态": state["C_condition"]["nutritional_status"]["nrs_2002"]["status"],
            "合并症": json_text(comorbidities.get("value")) if comorbidities.get("value") is not None else None,
            "合并症状态": comorbidities["status"],
            "完整性得分": state["state_completeness"]["score"],
            "完整性等级": state["state_completeness"]["grade"],
            "高影响未知字段数": state["state_completeness"]["high_impact_unknown_fields"],
            "影像证据条数": len(case_imaging),
            "文书证据条数": len(case_docs),
            "检验证据条数": len(case_labs),
            "病理证据条数": len(case_pathology),
        }
        flat_rows.append(flat)
        for path, impact in AUDIT_FIELDS.items():
            node = get_node(state, path)
            field_rows.append(
                {
                    "患者ID": patient_id,
                    "字段": path,
                    "状态": clean(node.get("status")) or "unknown",
                    "值": json_text(node.get("value")),
                    "决策影响": impact,
                    "来源": json_text(node.get("source", [])),
                    "候选冲突值": json_text(node.get("candidates", [])),
                    "说明": clean(node.get("note")),
                }
            )
        for item in state["uncertainty_ledger"]:
            uncertainty_rows.append({"患者ID": patient_id, **item, "source": json_text(item.get("source", []))})
        source_rows.append(
            {
                "患者ID": patient_id,
                "影像": len(case_imaging),
                "文书": len(case_docs),
                "检验": len(case_labs),
                "病理": len(case_pathology),
                "T0未来资料混入": "否",
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "patient_states_100_pre_t0.jsonl"
    state_path.write_text("".join(json_text(row) + "\n" for row in states), encoding="utf-8")
    outcome_path = output_dir / "patient_outcomes_100_post_t0.jsonl"
    outcome_path.write_text("".join(json_text(row) + "\n" for row in outcomes), encoding="utf-8")
    readable_path = output_dir / "patient_states_100_readable.txt"
    readable_path.write_text("\n\n" + ("\n\n" + "=" * 88 + "\n\n").join(render_state(row) for row in states), encoding="utf-8")
    pd.DataFrame(flat_rows).to_csv(output_dir / "state_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(field_rows).to_csv(output_dir / "field_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(uncertainty_rows).to_csv(output_dir / "uncertainty_ledger.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(source_rows).to_csv(output_dir / "source_counts.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        [{"患者ID": row["患者ID"], "分析病种分层": row["分析病种分层"], "用途": "仅分析分层，不作为agent输入"} for row in flat_rows]
    ).to_csv(output_dir / "analysis_strata_100.csv", index=False, encoding="utf-8-sig")

    grade_counts = Counter(row["state_completeness"]["grade"] for row in states)
    status_counts = Counter(item["status"] for row in states for item in row["uncertainty_ledger"])
    stratum_counts = Counter(row["分析病种分层"] for row in flat_rows)
    metrics = {
        "case_count": len(states),
        "decision_time_basis": dict(Counter(row["decision_timepoint_basis"] for row in states)),
        "disease_strata": dict(stratum_counts),
        "completeness_grades": dict(grade_counts),
        "field_status_counts": dict(status_counts),
        "mean_completeness_score": round(sum(row["state_completeness"]["score"] for row in states) / len(states), 3),
        "cases_with_zero_documents": sum(not row["文书证据条数"] for row in flat_rows),
        "cases_with_zero_labs": sum(not row["检验证据条数"] for row in flat_rows),
        "cases_with_zero_pathology": sum(not row["病理证据条数"] for row in flat_rows),
        "future_data_in_state": 0,
        "rule_version": VERSION,
        "iap_reference": IAP_REFERENCE,
    }
    (output_dir / "state_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    workbook_payload = {
        "summary": flat_rows,
        "field_audit": field_rows,
        "uncertainty": [row for row in uncertainty_rows if row["status"] in {"unknown", "conflict"}],
        "source_counts": source_rows,
        "outcomes": outcomes,
        "metrics": metrics,
    }
    (output_dir / "workbook_payload.json").write_text(
        json.dumps(workbook_payload, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8"
    )
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": VERSION,
        "source_cohort": str(COHORT_PATH),
        "output_dir": str(output_dir),
        "lookback_days": LOOKBACK_DAYS,
        "case_count": len(states),
        "outputs": [state_path.name, outcome_path.name, readable_path.name, "state_summary.csv", "field_audit.csv", "uncertainty_ledger.csv", "source_counts.csv", "analysis_strata_100.csv", "state_metrics.json", "workbook_payload.json"],
        "temporal_policy": "T0前资料；同日仅纳入明确时间不晚于T0者；日期粒度病理同日排除",
        "known_limitation": "影像索引无签发/审核时间，检查时间作为T0代理",
    }
    (output_dir / "state_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    metrics = build_states(args.output_dir)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

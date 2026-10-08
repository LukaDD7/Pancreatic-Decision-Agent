from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from scripts.cohort_construction.paths import data_root
from scripts.cohort_construction.agent_packaging_and_audit.shared import stage8a
from scripts.cohort_construction.agent_packaging_and_audit.cohort_100 import (
    step01_build_patient_states_100 as state_builder,
)

DATA_ROOT = data_root()
PROJECT_ROOT = DATA_ROOT / "pipeline_outputs_stage8_v1" / "pancreas_decision_window_enriched_v1"
RESTRICTED = PROJECT_ROOT / "restricted"
STATE_DIR = RESTRICTED / "patient_state_100_v1" / "outputs" / "state_100_20261003"
T1_DIR = RESTRICTED / "t1_candidate_audit_v1" / "outputs" / "t1_candidates_20261005"
COHORT_100 = RESTRICTED / "four_pathway_cohort_100_pre_t0.parquet"
COHORT_139 = RESTRICTED / "all_closed_four_pathway_cases_pre_t0.parquet"
STRONG_POOL = RESTRICTED / "strong_pool_pre_t0.parquet"
FOLLOWUP_ROOT = DATA_ROOT / "随访数据"
LEGACY_ROOT = DATA_ROOT / "21年以前检验文书"
DEFAULT_OUT = DATA_ROOT / "outputs" / "100例"

CLINICAL_TITLE = re.compile(
    r"入院|病程|查房|会诊|MDT|多学科|术前|手术|术后|出院|病理|穿刺|活检|EUS|FNA|"
    r"化疗|放疗|影像|CT|MR|PET|评估|营养|NRS|ERCP|PTCD|PTBD|ENBD|支架|引流|探查",
    re.I,
)


def clean(value: Any) -> str:
    return state_builder.clean(value)


def parse_dt(value: Any) -> datetime | None:
    return state_builder.parse_dt(value)


def jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if pd.isna(value) if not isinstance(value, (str, list, dict)) else False:
        return None
    return value


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(jsonable(row), ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def normalize_id(value: Any) -> str:
    text = re.sub(r"\.0$", "", clean(value).upper())
    return re.sub(r"[^A-Z0-9]", "", text)


def temporal_bucket(value: Any, t0: datetime) -> str:
    dt = parse_dt(value)
    if dt is None:
        return "unknown_time"
    if dt <= t0:
        return "pre_or_at_T0"
    if dt <= t0 + timedelta(days=90):
        return "post_T0_0_90d"
    return "post_T0_after_90d"


def source_record_key(row: dict[str, Any]) -> str:
    return stage8a.stable_source_key("document", clean(row.get("source_file")), clean(row.get("source_record_id")))


def extract_documents(
    selected_ids: set[str], all_closed_ids: set[str], output_raw: Path, output_structured: Path, t0_by_case: dict[str, datetime]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    selected_value_set = pa.array(sorted(selected_ids))
    all_value_set = pa.array(sorted(all_closed_ids))
    selected_tables: list[pa.Table] = []
    counts_139: Counter[str] = Counter()
    files = sorted(stage8a.DOCUMENT_ROOT.rglob("*.parquet"))
    for path in files:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=25_000):
            patient_index = batch.schema.get_field_index("PATIENT_ID")
            all_rows = batch.filter(pc.is_in(batch.column(patient_index), value_set=all_value_set))
            if all_rows.num_rows:
                counts_139.update(clean(value) for value in all_rows.column("PATIENT_ID").to_pylist())
            selected_rows = batch.filter(pc.is_in(batch.column(patient_index), value_set=selected_value_set))
            if selected_rows.num_rows:
                selected_tables.append(pa.Table.from_batches([selected_rows]))
    if not selected_tables:
        raise RuntimeError("no_documents_matched_selected_cohort")
    raw_table = pa.concat_tables(selected_tables)
    pq.write_table(raw_table, output_raw / "documents_100_raw.parquet", compression="zstd")

    rows: list[dict[str, Any]] = []
    raw_json_rows: list[dict[str, Any]] = []
    patient_handles: dict[str, Any] = {}
    by_patient_dir = output_structured / "documents_by_patient"
    by_patient_dir.mkdir(parents=True, exist_ok=True)
    try:
        for row in raw_table.to_pylist():
            case_id = clean(row.get("PATIENT_ID"))
            raw_text = stage8a.document_text(row.get(stage8a.DOC_CONTENT))
            deidentified = stage8a.redact_text(raw_text, [row.get("PATIENT_ID"), row.get("VISIT_ID")], limit=200_000)
            dt = parse_dt(row.get("create_time") or row.get("CREATE_DATE_TIME"))
            item = {
                "case_id": case_id,
                "visit_id": clean(row.get("VISIT_ID")),
                "document_id": source_record_key(row),
                "document_title": clean(row.get("文书名称")),
                "create_time": state_builder.iso(dt),
                "admission_time": state_builder.iso(parse_dt(row.get("admission_time") or row.get("ADMISSION_DATE_TIME"))),
                "discharge_time": state_builder.iso(parse_dt(row.get("discharge_time") or row.get("DISCHARGE_DATE_TIME"))),
                "temporal_bucket": temporal_bucket(dt, t0_by_case[case_id]),
                "clinical_relevance": "clinical" if CLINICAL_TITLE.search(clean(row.get("文书名称"))) else "supportive_or_administrative",
                "source_file": clean(row.get("source_file")),
                "source_record_id": clean(row.get("source_record_id")),
                "source_row": row.get("source_row"),
                "content_sha256": clean(row.get("content_sha256")) or hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                "document_text": deidentified,
            }
            rows.append(item)
            raw_json_rows.append(
                {
                    "case_id": case_id,
                    "visit_id": clean(row.get("VISIT_ID")),
                    "document_id": item["document_id"],
                    "document_title": item["document_title"],
                    "create_time": item["create_time"],
                    "source_file": item["source_file"],
                    "source_record_id": item["source_record_id"],
                    "source_row": item["source_row"],
                    "document_text_raw": raw_text,
                }
            )
            handle = patient_handles.get(case_id)
            if handle is None:
                handle = (by_patient_dir / f"{case_id}.jsonl").open("w", encoding="utf-8")
                patient_handles[case_id] = handle
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    finally:
        for handle in patient_handles.values():
            handle.close()

    write_jsonl(output_raw / "documents_100_raw.jsonl", raw_json_rows)
    write_jsonl(output_structured / "documents_100_deidentified.jsonl", rows)
    index = pd.DataFrame([{key: value for key, value in row.items() if key != "document_text"} for row in rows])
    index.to_csv(output_structured / "document_index_100.csv", index=False, encoding="utf-8-sig")
    return rows, dict(counts_139)


def extract_timeline_and_sources(
    cohort: pd.DataFrame,
    id_to_uid: dict[str, str],
    imaging: pd.DataFrame,
    output_raw: Path,
    output_structured: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    uids = set(id_to_uid.values())
    docs, labs, pathology_events = state_builder.load_timeline_rows(uids)
    pd.DataFrame(docs).to_parquet(output_raw / "timeline_document_index_100.parquet", index=False)
    pd.DataFrame(labs).to_parquet(output_raw / "laboratory_results_100_raw.parquet", index=False)
    pd.DataFrame(pathology_events).to_parquet(output_raw / "pathology_events_100_raw.parquet", index=False)
    imaging.to_parquet(output_raw / "imaging_reports_100_raw.parquet", index=False)

    uid_to_case = {uid: case for case, uid in id_to_uid.items()}
    lab_rows = [{"case_id": uid_to_case.get(clean(row.get("patient_uid"))), **row} for row in labs]
    pathology_event_rows = [{"case_id": uid_to_case.get(clean(row.get("patient_uid"))), **row} for row in pathology_events]
    imaging_rows = imaging.to_dict("records")
    write_jsonl(output_structured / "laboratory_results_100.jsonl", lab_rows)
    write_jsonl(output_structured / "pathology_events_100.jsonl", pathology_event_rows)
    write_jsonl(output_structured / "imaging_reports_100.jsonl", imaging_rows)

    pathology_columns = state_builder.pathology_schema_names()
    pathology_table = state_builder.read_pathology_table(pathology_columns)
    patient_column = pathology_table["病人编号"]
    filtered = pathology_table.filter(pc.is_in(patient_column, value_set=pa.array(sorted(set(cohort["患者ID"].astype(str))))))
    pq.write_table(filtered, output_raw / "pathology_records_100_raw.parquet", compression="zstd")
    structured_pathology = []
    for row in filtered.to_pylist():
        structured_pathology.append(
            {
                "case_id": clean(row.get("病人编号")),
                "pathology_record_uid": clean(row.get("pathology_record_uid")),
                "pathology_no": clean(row.get("pathology_no_normalized") or row.get("病理号")),
                "specimen_type": clean(row.get("标本类型")),
                "specimen_name": clean(row.get("标本名称")),
                "clinical_diagnosis": clean(row.get("临床诊断")),
                "pathology_diagnosis": clean(row.get("病理诊断")),
                "microscopy": clean(row.get("镜下所见")),
                "received_date": clean(row.get("received_date_parsed") or row.get("收到日期")),
                "specimen_date": clean(row.get("specimen_date_parsed") or row.get("取材日期")),
                "report_date": clean(row.get("report_date_parsed") or row.get("报告日期")),
                "disease_label": clean(row.get("disease_label")),
                "source_record_key": clean(row.get("source_record_key")),
            }
        )
    write_jsonl(output_structured / "pathology_records_100.jsonl", structured_pathology)
    return lab_rows, pathology_event_rows, structured_pathology


def copy_existing_outputs(output_structured: Path) -> None:
    destination = output_structured / "existing_structured_outputs"
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(STATE_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() in {".jsonl", ".json", ".csv", ".txt", ".xlsx"}:
            shutil.copy2(path, destination / path.name)
    for path in sorted(T1_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() in {".jsonl", ".json", ".csv", ".md"}:
            shutil.copy2(path, destination / path.name)


def followup_matches(
    cohort_ids: set[str], all_closed_ids: set[str], pathology_raw_path: Path, output_raw: Path, output_audit: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_dir = output_raw / "supplemental_followup_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(FOLLOWUP_ROOT.iterdir()):
        if source.is_file():
            shutil.copy2(source, source_dir / source.name)

    pathology = pd.read_parquet(pathology_raw_path).fillna("")
    pathology["_case_id"] = pathology["病人编号"].map(clean)
    pathno_to_cases: dict[str, set[str]] = defaultdict(set)
    for _, row in pathology.iterrows():
        for value in (row.get("pathology_no_normalized"), row.get("病理号")):
            normalized = normalize_id(value)
            if normalized:
                pathno_to_cases[normalized].add(clean(row["_case_id"]))

    all_path = state_builder.read_pathology_table(
        ["病人编号", "病理号", "pathology_no_normalized"]
    ).to_pandas().fillna("")
    all_pathno_to_cases: dict[str, set[str]] = defaultdict(set)
    for _, row in all_path.iterrows():
        for value in (row.get("pathology_no_normalized"), row.get("病理号")):
            normalized = normalize_id(value)
            if normalized:
                all_pathno_to_cases[normalized].add(clean(row["病人编号"]))

    datasets: list[tuple[str, pd.DataFrame, str, str]] = []
    net = pd.read_csv(FOLLOWUP_ROOT / "神经内分泌随访.csv", dtype=str).fillna("")
    datasets.append(("PanNET_followup", net, "patient_id", "pathology_no"))
    pdac = pd.read_excel(FOLLOWUP_ROOT / "PDAC各模态生存统计.xlsx", dtype=str).fillna("")
    datasets.append(("PDAC_followup", pdac, "ID", "pathology_no"))
    ipmn = pd.read_excel(FOLLOWUP_ROOT / "IPMN表格修订版.xlsx", dtype=str).fillna("")
    datasets.append(("IPMN_followup", ipmn, "患者编号1", "case_id_prefix"))

    current_matches: list[dict[str, Any]] = []
    all_closed_matches: list[dict[str, Any]] = []
    for dataset_name, frame, id_column, basis in datasets:
        for source_row, row in frame.iterrows():
            source_value = clean(row.get(id_column))
            if basis == "pathology_no":
                cases = all_pathno_to_cases.get(normalize_id(source_value), set())
            else:
                cases = {source_value.split("_", 1)[0]}
            for case_id in sorted(cases):
                item = {
                    "case_id": case_id,
                    "dataset": dataset_name,
                    "match_basis": basis,
                    "source_id": source_value,
                    "source_row": int(source_row) + 2,
                    "data": row.to_dict(),
                }
                if case_id in cohort_ids:
                    current_matches.append(item)
                if case_id in all_closed_ids:
                    all_closed_matches.append(item)
    write_jsonl(output_audit / "followup_matches_current_100.jsonl", current_matches)
    write_jsonl(output_audit / "followup_matches_all_closed_139.jsonl", all_closed_matches)
    pd.DataFrame([{key: value for key, value in row.items() if key != "data"} for row in current_matches]).to_csv(
        output_audit / "followup_matches_current_100.csv", index=False, encoding="utf-8-sig"
    )
    return current_matches, all_closed_matches


def decode_xml(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("gb18030", "gb2312", "utf-8"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace"), "gb18030_replace"


def xml_text(text: str) -> str:
    try:
        root = ET.fromstring(text)
        return "\n".join(part.strip() for part in root.itertext() if part and part.strip())
    except ET.ParseError:
        return re.sub(r"<[^>]+>", " ", text)


def legacy_audit(
    cohort_ids: set[str], strong_pool: pd.DataFrame, output_raw: Path, output_structured: Path, output_audit: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    strong_ids = set(strong_pool["患者ID"].astype(str).str.strip())
    current_rows: list[dict[str, Any]] = []
    rescreen_rows: list[dict[str, Any]] = []
    rescreen_raw = output_raw / "legacy_xml_rescreen_candidates"
    rescreen_raw.mkdir(parents=True, exist_ok=True)
    for path in sorted(LEGACY_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".xml", ".bak"}:
            continue
        case_id = path.name.split("_", 1)[0]
        if case_id not in cohort_ids and case_id not in strong_ids:
            continue
        text, encoding = decode_xml(path)
        item = {
            "case_id": case_id,
            "relative_path": str(path.relative_to(LEGACY_ROOT)),
            "extension": path.suffix.lower(),
            "encoding": encoding,
            "size_bytes": path.stat().st_size,
            "document_text": xml_text(text),
        }
        if case_id in cohort_ids:
            current_rows.append(item)
        if case_id in strong_ids:
            rescreen_rows.append(item)
            target = rescreen_raw / path.relative_to(LEGACY_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    write_jsonl(output_structured / "legacy_xml_current_100.jsonl", current_rows)
    write_jsonl(output_audit / "legacy_xml_strong_pool_rescreen.jsonl", rescreen_rows)

    grouped = Counter(row["case_id"] for row in rescreen_rows if row["extension"] == ".xml")
    strong_lookup = strong_pool.drop_duplicates("患者ID").set_index("患者ID").to_dict("index")
    candidates = []
    for case_id, count in grouped.most_common():
        source = strong_lookup.get(case_id, {})
        candidates.append(
            {
                "case_id": case_id,
                "xml_document_count": count,
                "signal_date": clean(source.get("首次信号日期_检查日期暂代")),
                "signal_site": clean(source.get("部位")),
                "signal_text": clean(source.get("原文")),
                "disease": clean(source.get("病种")),
                "decision_chain_status": clean(source.get("决策链状态")),
                "review_status": "需要重新核实决策链；不能仅因文书更多直接替换",
            }
        )
    pd.DataFrame(candidates).to_csv(output_audit / "legacy_strong_pool_rescreen_candidates.csv", index=False, encoding="utf-8-sig")
    return current_rows, rescreen_rows


def source_richness_for_139(
    cohort_139: pd.DataFrame,
    current_ids: set[str],
    document_counts: dict[str, int],
    output_audit: Path,
) -> pd.DataFrame:
    cohort = cohort_139.copy()
    cohort["患者ID"] = cohort["患者ID"].astype(str).str.strip()
    decision_values = cohort.apply(state_builder.decision_time, axis=1)
    cohort["decision_time"] = [value[0] for value in decision_values]
    cohort["decision_time_basis"] = [value[1] for value in decision_values]
    ids = set(cohort["患者ID"])

    imaging_columns = [
        "patient_id",
        "patient_uid",
        "exam_datetime",
        "exam_method",
        "description_deidentified",
        "diagnosis_deidentified",
    ]
    imaging = pq.read_table(state_builder.IMAGING_INDEX_PATH, columns=imaging_columns).to_pandas()
    imaging["patient_id"] = imaging["patient_id"].astype(str).str.strip()
    imaging = imaging[imaging["patient_id"].isin(ids)].copy()
    mappings = imaging[["patient_id", "patient_uid"]].drop_duplicates()
    counts = mappings.groupby("patient_id")["patient_uid"].nunique()
    valid_ids = set(counts[counts.eq(1)].index)
    id_to_uid = dict(zip(mappings[mappings.patient_id.isin(valid_ids)].patient_id, mappings[mappings.patient_id.isin(valid_ids)].patient_uid.astype(str)))
    cohort = cohort[cohort["患者ID"].isin(valid_ids)].copy()
    t0_by_case = dict(zip(cohort["患者ID"], cohort["decision_time"]))
    t0_by_uid = {id_to_uid[case]: value for case, value in t0_by_case.items()}
    uid_to_case = {uid: case for case, uid in id_to_uid.items()}
    documents, labs, pathology = state_builder.load_timeline_rows(set(t0_by_uid))

    metrics: dict[str, Counter[str]] = {
        "pre_documents_180d": Counter(),
        "post_documents_90d": Counter(),
        "pre_labs_180d": Counter(),
        "post_pathology_90d": Counter(),
        "pre_imaging_180d": Counter(),
        "post_imaging_90d": Counter(),
    }
    for row in documents:
        uid = clean(row.get("patient_uid"))
        if uid not in t0_by_uid:
            continue
        dt = parse_dt(row.get("create_time") or row.get("event_time_used"))
        if not dt:
            continue
        case = uid_to_case[uid]
        if t0_by_uid[uid] - timedelta(days=180) <= dt <= t0_by_uid[uid]:
            metrics["pre_documents_180d"][case] += 1
        elif t0_by_uid[uid] < dt <= t0_by_uid[uid] + timedelta(days=90):
            metrics["post_documents_90d"][case] += 1
    for row in labs:
        uid = clean(row.get("patient_uid"))
        if uid not in t0_by_uid:
            continue
        dt = parse_dt(row.get("available_time") or row.get("report_time") or row.get("event_time_used"))
        if dt and t0_by_uid[uid] - timedelta(days=180) <= dt <= t0_by_uid[uid]:
            metrics["pre_labs_180d"][uid_to_case[uid]] += 1
    for row in pathology:
        uid = clean(row.get("patient_uid"))
        if uid not in t0_by_uid:
            continue
        dt = parse_dt(row.get("report_time") or row.get("event_time_used"))
        if dt and t0_by_uid[uid] < dt <= t0_by_uid[uid] + timedelta(days=90):
            metrics["post_pathology_90d"][uid_to_case[uid]] += 1
    for row in imaging.to_dict("records"):
        case = clean(row.get("patient_id"))
        dt = parse_dt(row.get("exam_datetime"))
        if not dt or case not in t0_by_case:
            continue
        if t0_by_case[case] - timedelta(days=180) <= dt <= t0_by_case[case]:
            metrics["pre_imaging_180d"][case] += 1
        elif t0_by_case[case] < dt <= t0_by_case[case] + timedelta(days=90):
            metrics["post_imaging_90d"][case] += 1

    rows = []
    for _, row in cohort.iterrows():
        case = clean(row["患者ID"])
        values = {name: int(counter[case]) for name, counter in metrics.items()}
        score = (
            (3 if row["decision_time_basis"] == "exam_datetime_proxy" else 0)
            + min(values["pre_documents_180d"], 5)
            + min(values["pre_labs_180d"], 5)
            + min(values["pre_imaging_180d"], 3)
            + min(values["post_documents_90d"], 3)
            + min(values["post_imaging_90d"], 3)
            + min(values["post_pathology_90d"], 2)
        )
        rows.append(
            {
                "case_id": case,
                "in_current_100": case in current_ids,
                "pathway": clean(row.get("四类路径")),
                "disease": clean(row.get("病种")),
                "T0": state_builder.iso(row["decision_time"]),
                "T0_time_basis": clean(row["decision_time_basis"]),
                "all_document_count": int(document_counts.get(case, 0)),
                **values,
                "source_richness_score": score,
            }
        )
    frame = pd.DataFrame(rows).sort_values(["in_current_100", "source_richness_score"], ascending=[True, False])
    frame.to_csv(output_audit / "all_closed_139_source_richness.csv", index=False, encoding="utf-8-sig")

    suggestions = []
    used_outgoing: set[str] = set()
    for pathway, reserve in frame[~frame.in_current_100].groupby("pathway"):
        incoming_rows = reserve.sort_values(["source_richness_score", "all_document_count"], ascending=False)
        outgoing_rows = frame[(frame.in_current_100) & frame.pathway.eq(pathway)].sort_values(
            ["source_richness_score", "all_document_count"], ascending=True
        )
        for _, incoming in incoming_rows.iterrows():
            outgoing = next((row for _, row in outgoing_rows.iterrows() if row.case_id not in used_outgoing), None)
            if outgoing is None or incoming.source_richness_score < outgoing.source_richness_score + 3:
                continue
            used_outgoing.add(outgoing.case_id)
            suggestions.append(
                {
                    "outgoing_case_id": outgoing.case_id,
                    "incoming_case_id": incoming.case_id,
                    "pathway": pathway,
                    "outgoing_disease": outgoing.disease,
                    "incoming_disease": incoming.disease,
                    "outgoing_score": outgoing.source_richness_score,
                    "incoming_score": incoming.source_richness_score,
                    "score_gain": incoming.source_richness_score - outgoing.source_richness_score,
                    "outgoing_T0_basis": outgoing.T0_time_basis,
                    "incoming_T0_basis": incoming.T0_time_basis,
                    "incoming_pre_documents": incoming.pre_documents_180d,
                    "incoming_pre_labs": incoming.pre_labs_180d,
                    "incoming_post_imaging": incoming.post_imaging_90d,
                    "incoming_post_pathology": incoming.post_pathology_90d,
                    "recommended_for_v2_after_source_review": outgoing.disease == incoming.disease,
                    "status": (
                        "同路径同病种分层候选；核实T0语义和T1原文后可替换"
                        if outgoing.disease == incoming.disease
                        else "病种分层不一致；仅保留候选，不建议直接替换"
                    ),
                }
            )
            if len(suggestions) >= 5:
                break
        if len(suggestions) >= 5:
            break
    pd.DataFrame(suggestions).to_csv(output_audit / "replacement_suggestions_max5.csv", index=False, encoding="utf-8-sig")
    return frame


def package_replacement_candidate_preview(cohort_139: pd.DataFrame, output_audit: Path) -> int:
    suggestions_path = output_audit / "replacement_suggestions_max5.csv"
    if not suggestions_path.exists() or suggestions_path.stat().st_size == 0:
        return 0
    suggestions = pd.read_csv(suggestions_path, dtype=str).fillna("")
    if suggestions.empty:
        return 0
    candidate_ids = set(suggestions["incoming_case_id"].map(clean))
    destination = output_audit / "replacement_candidate_preview"
    destination.mkdir(parents=True, exist_ok=True)

    value_set = pa.array(sorted(candidate_ids))
    document_tables: list[pa.Table] = []
    for path in sorted(stage8a.DOCUMENT_ROOT.rglob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=25_000):
            patient_index = batch.schema.get_field_index("PATIENT_ID")
            filtered = batch.filter(pc.is_in(batch.column(patient_index), value_set=value_set))
            if filtered.num_rows:
                document_tables.append(pa.Table.from_batches([filtered]))
    raw_documents = pa.concat_tables(document_tables)
    pq.write_table(raw_documents, destination / "candidate_documents_raw.parquet", compression="zstd")

    documents_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_documents.to_pylist():
        case_id = clean(row.get("PATIENT_ID"))
        raw_text = stage8a.document_text(row.get(stage8a.DOC_CONTENT))
        documents_by_case[case_id].append(
            {
                "document_id": source_record_key(row),
                "document_title": clean(row.get("文书名称")),
                "create_time": state_builder.iso(parse_dt(row.get("create_time") or row.get("CREATE_DATE_TIME"))),
                "source_file": clean(row.get("source_file")),
                "source_record_id": clean(row.get("source_record_id")),
                "document_text": stage8a.redact_text(raw_text, [row.get("PATIENT_ID"), row.get("VISIT_ID")], limit=200_000),
            }
        )

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
    imaging = pq.read_table(state_builder.IMAGING_INDEX_PATH, columns=imaging_columns).to_pandas()
    imaging["patient_id"] = imaging["patient_id"].astype(str).str.strip()
    imaging = imaging[imaging["patient_id"].isin(candidate_ids)].copy()
    imaging.to_parquet(destination / "candidate_imaging.parquet", index=False)
    mappings = imaging[["patient_id", "patient_uid"]].drop_duplicates()
    id_to_uid = dict(zip(mappings.patient_id, mappings.patient_uid.astype(str)))
    documents, labs, pathology_events = state_builder.load_timeline_rows(set(id_to_uid.values()))
    pd.DataFrame(labs).to_parquet(destination / "candidate_laboratory.parquet", index=False)
    pd.DataFrame(pathology_events).to_parquet(destination / "candidate_pathology_events.parquet", index=False)
    uid_to_case = {uid: case for case, uid in id_to_uid.items()}
    labs_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pathology_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in labs:
        labs_by_case[uid_to_case[clean(row.get("patient_uid"))]].append(jsonable(row))
    for row in pathology_events:
        pathology_by_case[uid_to_case[clean(row.get("patient_uid"))]].append(jsonable(row))
    imaging_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in imaging.to_dict("records"):
        imaging_by_case[clean(row.get("patient_id"))].append(jsonable(row))

    cohort = cohort_139[cohort_139["患者ID"].astype(str).str.strip().isin(candidate_ids)].copy()
    bundles = []
    for _, row in cohort.iterrows():
        case_id = clean(row["患者ID"])
        dt, basis = state_builder.decision_time(row)
        bundles.append(
            {
                "case_id": case_id,
                "T0": state_builder.iso(dt),
                "T0_time_basis": basis,
                "cohort_metadata": row.to_dict(),
                "documents_all": documents_by_case[case_id],
                "imaging_all": imaging_by_case[case_id],
                "laboratory_all": labs_by_case[case_id],
                "pathology_events_all": pathology_by_case[case_id],
                "warning": "仅供替换候选复核；尚未构建正式T0 State，也未执行替换",
            }
        )
    write_jsonl(destination / "replacement_candidate_bundles.jsonl", bundles)
    return len(bundles)


def build_patient_bundles(
    cohort: pd.DataFrame,
    states: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    imaging: pd.DataFrame,
    labs: list[dict[str, Any]],
    pathology: list[dict[str, Any]],
    followup: list[dict[str, Any]],
    output_structured: Path,
) -> None:
    t1_events_path = T1_DIR / "t1_candidate_events_100.jsonl"
    t1_events = load_jsonl(t1_events_path) if t1_events_path.exists() else []
    state_by_case = {clean(row.get("case_id")): row for row in states}
    outcome_by_case = {clean(row.get("case_id")): row for row in outcomes}
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in documents:
        grouped[clean(row.get("case_id"))]["documents"].append(row)
    for row in imaging.to_dict("records"):
        grouped[clean(row.get("patient_id"))]["imaging"].append(jsonable(row))
    for row in labs:
        grouped[clean(row.get("case_id"))]["labs"].append(jsonable(row))
    for row in pathology:
        grouped[clean(row.get("case_id"))]["pathology"].append(row)
    for row in followup:
        grouped[clean(row.get("case_id"))]["followup"].append(row)
    for row in t1_events:
        grouped[clean(row.get("case_id"))]["t1_candidates"].append(row)

    cohort_lookup = cohort.set_index("患者ID").to_dict("index")
    bundles = []
    for case_id in sorted(cohort_lookup):
        bundle = {
            "case_id": case_id,
            "cohort_metadata": cohort_lookup[case_id],
            "state_pre_T0": state_by_case.get(case_id),
            "outcome_post_T0_isolated": outcome_by_case.get(case_id),
            "documents_all": grouped[case_id]["documents"],
            "imaging_all": grouped[case_id]["imaging"],
            "laboratory_all": grouped[case_id]["labs"],
            "pathology_all": grouped[case_id]["pathology"],
            "followup_analysis_only": grouped[case_id]["followup"],
            "T1_candidates_not_best_test_labels": grouped[case_id]["t1_candidates"],
        }
        bundles.append(bundle)
    write_jsonl(output_structured / "patient_source_bundles_100.jsonl", bundles)


def write_readme(output_dir: Path, metrics: dict[str, Any]) -> None:
    text = f"""# 100例Agent评估数据包

生成日期：{datetime.now().date().isoformat()}

## 目录

- `raw`：按100例患者ID抽取的原始文书、影像、检验和病理数据，以及补充随访源文件副本。
- `structured`：去标识化文书、结构化索引、患者级整合包及既有T0/T1输出。
- `audit`：遗漏资料、补充随访匹配、旧XML命中、139例数据充分度和替换建议。

## 重要边界

- `patient_source_bundles_100.jsonl`包含T0后资料，只能作为受控实验源，不可整体直接输入T0模型。
- T0模型输入继续使用`existing_structured_outputs/patient_states_100_pre_t0.jsonl`。
- 随访数据和后验病理仅供分析，不进入T0输入。
- T1候选是实际观察到的新增证据，不是最佳检查标签。
- 替换建议仅是数据充分度筛查，尚未执行病例替换。

## 数量

- 病例：{metrics['case_count']}
- 主文书：{metrics['document_count']}
- 有主文书病例：{metrics['cases_with_documents']}
- 检验记录：{metrics['lab_count']}
- 影像记录：{metrics['imaging_count']}
- 病理记录：{metrics['pathology_record_count']}
- 当前100例匹配随访专表：{metrics['followup_match_cases']}例
- 当前100例直接匹配旧XML：{metrics['legacy_current_case_count']}例
- 旧XML命中的强信号重筛病例：{metrics['legacy_rescreen_case_count']}例
"""
    (output_dir / "README.md").write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a complete raw and structured agent-evaluation package for the 100-case cohort.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    output_dir = args.output_dir
    output_raw = output_dir / "raw"
    output_structured = output_dir / "structured"
    output_audit = output_dir / "audit"
    for path in (output_dir, output_raw, output_structured, output_audit):
        path.mkdir(parents=True, exist_ok=True)

    cohort_100, imaging_100, id_to_uid = state_builder.load_cohort_and_imaging()
    cohort_100["患者ID"] = cohort_100["患者ID"].astype(str).str.strip()
    cohort_139 = pd.read_parquet(COHORT_139)
    cohort_139["患者ID"] = cohort_139["患者ID"].astype(str).str.strip()
    states = load_jsonl(STATE_DIR / "patient_states_100_pre_t0.jsonl")
    outcomes = load_jsonl(STATE_DIR / "patient_outcomes_100_post_t0.jsonl")
    t0_by_case = {clean(row["case_id"]): parse_dt(row["decision_timepoint"]) for row in states}

    documents, document_counts_139 = extract_documents(
        set(cohort_100["患者ID"]), set(cohort_139["患者ID"]), output_raw, output_structured, t0_by_case
    )
    labs, pathology_events, pathology_records = extract_timeline_and_sources(
        cohort_100, id_to_uid, imaging_100, output_raw, output_structured
    )
    copy_existing_outputs(output_structured)
    followup_current, followup_all = followup_matches(
        set(cohort_100["患者ID"]),
        set(cohort_139["患者ID"]),
        output_raw / "pathology_records_100_raw.parquet",
        output_raw,
        output_audit,
    )
    strong_pool = pd.read_parquet(STRONG_POOL)
    legacy_current, legacy_rescreen = legacy_audit(
        set(cohort_100["患者ID"]), strong_pool, output_raw, output_structured, output_audit
    )
    richness = source_richness_for_139(
        cohort_139, set(cohort_100["患者ID"]), document_counts_139, output_audit
    )
    replacement_preview_count = package_replacement_candidate_preview(cohort_139, output_audit)
    build_patient_bundles(
        cohort_100, states, outcomes, documents, imaging_100, labs, pathology_records, followup_current, output_structured
    )

    metrics = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "case_count": len(cohort_100),
        "document_count": len(documents),
        "cases_with_documents": len({row["case_id"] for row in documents}),
        "documents_by_temporal_bucket": dict(Counter(row["temporal_bucket"] for row in documents)),
        "clinical_document_count": sum(row["clinical_relevance"] == "clinical" for row in documents),
        "lab_count": len(labs),
        "imaging_count": len(imaging_100),
        "pathology_event_count": len(pathology_events),
        "pathology_record_count": len(pathology_records),
        "followup_match_rows": len(followup_current),
        "followup_match_cases": len({row["case_id"] for row in followup_current}),
        "followup_all_closed_match_cases": len({row["case_id"] for row in followup_all}),
        "legacy_current_file_count": len(legacy_current),
        "legacy_current_case_count": len({row["case_id"] for row in legacy_current}),
        "legacy_rescreen_file_count": len(legacy_rescreen),
        "legacy_rescreen_case_count": len({row["case_id"] for row in legacy_rescreen}),
        "all_closed_richness_cases": len(richness),
        "replacement_candidate_preview_count": replacement_preview_count,
    }
    (output_dir / "manifest.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    write_readme(output_dir, metrics)
    print(output_dir)


if __name__ == "__main__":
    main()

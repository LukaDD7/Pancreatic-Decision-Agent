from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from scripts.cohort_construction.paths import data_root

ROOT = data_root()
SOURCE = ROOT / "outputs" / "continuous_decision_safd_851_20261006"
OUT = ROOT / "outputs" / "preop_decision_cohort_180_20261007"
DOCUMENT_ROOTS = [
    ROOT / "pipeline_outputs_stage5_v1" / "restricted" / "document_l1",
    ROOT / "pipeline_outputs_stage5_v1" / "restricted" / "legacy_xml_document_l1_v1",
]
IMAGING = (
    ROOT
    / "pipeline_outputs_stage8_v1"
    / "pancreas_imaging_report_index_v2"
    / "restricted"
    / "index"
    / "report_index.parquet"
)
OLD100 = (
    ROOT
    / "pipeline_outputs_stage8_v1"
    / "pancreas_decision_window_enriched_v1"
    / "restricted"
    / "four_pathway_cohort_100_pre_t0.parquet"
)

TARGET_N = 180
PLAN_TITLE = re.compile(r"术前讨论|术前小结|手术同意|知情同意|MDT|多学科|治疗计划|手术计划", re.I)
POST_TITLE = re.compile(r"手术记录|术后|出院|转入记录|转出记录|护理记录", re.I)
STATE_TITLE = re.compile(r"入院|首次病程|病程记录|查房|会诊|MDT|多学科|影像|病理|穿刺|活检", re.I)
PLAN_MARKER = re.compile(
    r"拟实施手术|拟施手术|拟行|拟定手术|手术方案|定于.{0,30}(?:行|实施)|经讨论.{0,40}(?:行|手术)|"
    r"(?:限期|择期|准备|决定|考虑|建议|安排)[^。；\n]{0,35}(?:行|实施)[^。；\n]{0,50}(?:手术|切除|活检|ERCP|化疗|放疗)",
    re.I | re.S,
)
PLAN_CAPTURE = [
    re.compile(r"(?:拟实施手术名称|拟施手术名称和手术方式|拟施手术|拟行|拟定手术)\s*[:：]?\s*([^。；\n]{2,180})"),
    re.compile(r"定于[^。；\n]{0,40}(?:行|实施)\s*([^。；\n]{2,160})"),
    re.compile(r"(?:限期|择期|准备|决定|考虑|建议|安排)[^。；\n]{0,35}(?:行|实施)\s*([^。；\n]{2,160})"),
    re.compile(
        r"(?:拟|计划|准备|考虑|决定|安排)\s*[“\"']?\s*"
        r"([^。；\n]{0,100}(?:胰十二指肠切除|Whipple|远端胰腺切除|胰体尾[^。；\n]{0,30}切除|"
        r"全胰切除|胰腺[^。；\n]{0,30}切除|胰[^。；\n]{0,20}癌根治术|手术治疗))"
    ),
]
LEAK_SENTENCE = re.compile(
    r"拟实施手术|拟施手术|拟行|拟定手术|手术方案|知情同意|定于.{0,30}(?:行|实施)|"
    r"实施手术名称|手术经过|术中诊断|术后诊断|"
    r"(?:限期|择期|准备|决定|考虑|建议|安排)[^。；\n]{0,35}(?:行|实施)[^。；\n]{0,50}(?:手术|切除|活检|ERCP|化疗|放疗)",
    re.I | re.S,
)
NAMED_PLAN = re.compile(
    r"(?:拟|计划|限期|择期|准备|决定|考虑|建议|安排|同意)[^。；\n]{0,100}"
    r"(?:胰十二指肠切除|Whipple|远端胰腺切除|胰体尾[^。；\n]{0,30}切除|全胰切除|"
    r"胰腺[^。；\n]{0,30}切除|胰[^。；\n]{0,20}癌根治术)",
    re.I,
)
MET_SIGNAL = re.compile(
    r"(?:肝|腹膜|网膜|肺|骨|肾上腺)[^。；\n]{0,35}(?:转移瘤|转移灶|转移可能|转移待排|可疑转移|多发转移)|"
    r"远处转移\s*[:：][^。；\n]{0,80}(?:转移|可疑|可能|待排)",
    re.I,
)
IMPORTANT_LAB = re.compile(
    r"CA\s*19[-－]?9|糖类抗原\s*19[-－]?9|CA\s*125|CEA|癌胚抗原|胆红素|白蛋白|"
    r"血红蛋白|ALT|AST|丙氨酸|天门冬氨酸|肌酐|INR|凝血酶原",
    re.I,
)
NON_SURGICAL_PLAN_RULES = [
    (
        "补充分期影像",
        re.compile(
            r"(?:(?:建议|需要|需|拟|计划|申请|待)(?:进一步)?(?:行|完善|复查|检查|评估)?|"
            r"(?:进一步完善|完善|复查)(?:胰腺|肝脏|上腹部|腹部))"
            r"[^。；\n]{0,60}(?:肝脏)?(?:MR|MRI|磁共振|增强CT|PET)",
            re.I,
        ),
    ),
    (
        "穿刺或病理确认",
        re.compile(r"(?:拟|计划|建议|考虑|决定|定于)[^。；\n]{0,80}(?:穿刺|活检|EUS|FNA)", re.I),
    ),
    (
        "胆道或介入处理",
        re.compile(
            r"(?:拟|计划|建议|考虑|决定|定于)[^。；\n]{0,60}"
            r"(?:ERCP|PTCD|PTBD|ENBD|胆道支架|胆管支架|胆汁引流|经皮[^。；\n]{0,20}引流)",
            re.I,
        ),
    ),
    (
        "系统治疗",
        re.compile(
            r"(?:拟|计划|建议|考虑|决定|选择)(?:予以?|行|接受|采用|开始)?[^。；\n]{0,30}"
            r"(?:新辅助|化疗|放疗|免疫治疗|靶向治疗)",
            re.I,
        ),
    ),
]
PANCREAS_CONTEXT = re.compile(r"胰|胆|肝|壶腹|十二指肠|腹膜|腹腔|腹部|后腹膜|转移", re.I)
NON_SURGICAL_ANCHOR_EXCLUDE = re.compile(
    r"手术患者交接|交接核查|安全核查|远端胰腺切除|胰十二指肠切除|Whipple|全胰切除|手术记录|术后",
    re.I,
)
ROOT_SURGERY = re.compile(r"胰十二指肠切除|Whipple|远端胰腺切除|胰体尾[^。；\n]{0,20}切除|全胰切除|根治性?切除", re.I)
SURGERY_TIME_PATTERNS = [
    re.compile(r"手术时间\s*[:：]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s+(\d{1,2})[:：](\d{2})"),
    re.compile(r"开始时间\s*[:：]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s+(\d{1,2})[:：](\d{2})"),
]
DOCUMENT_HEADER_TIME = re.compile(
    r"^\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s+(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?"
)
PRIOR_PANCREAS_OPERATION = re.compile(
    r"(?:胰十二指肠|Whipple|胰体尾|胰尾|远端胰腺|全胰)[^。；\n]{0,25}(?:切除)?术后|"
    r"(?:行|接受)[^。；\n]{0,30}(?:胰十二指肠切除|Whipple|胰体尾切除|远端胰腺切除|全胰切除)|"
    r"胰(?:腺)?(?:癌|肿瘤)?术后|胰腺手术史",
    re.I,
)
LOW_VALUE_STATE_TITLE = re.compile(
    r"护理|介绍表|物品清单|健康教育|风险告知|知情同意|交接核查|申请单|承诺书|患者告知|VTE",
    re.I,
)


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                pass
        return value.decode("utf-8", errors="replace")
    return str(value).strip()


def parse_dt(value: Any) -> datetime | None:
    if value is None or clean(value) in {"", "NaT", "None"}:
        return None
    stamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(stamp):
        return None
    return stamp.to_pydatetime()


def iso(value: datetime | None) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def decode_document(row: dict[str, Any]) -> str:
    return clean(row.get("文书内容"))


def document_header_time(text: str) -> datetime | None:
    match = DOCUMENT_HEADER_TIME.search(text)
    if not match:
        return None
    values = [int(value) if value else 0 for value in match.groups()]
    try:
        return datetime(*values)
    except ValueError:
        return None


def action_category(text: str) -> str:
    value = clean(text)
    if re.search(r"胰十二指肠|Whipple|胰头十二指肠", value, re.I):
        return "胰十二指肠切除"
    if re.search(r"远端胰|胰体尾|胰尾|胰体癌根治", value, re.I):
        return "远端胰腺切除"
    if "全胰" in value:
        return "全胰切除"
    if re.search(r"次全胰|胰腺次全|胰腺根治性大部|胰腺大部|胰腺根治", value, re.I):
        return "其他胰腺根治切除"
    if re.search(r"分期腹腔镜", value, re.I):
        return "分期腹腔镜"
    if re.search(r"探查|活检|穿刺|冰冻", value, re.I):
        return "探查或病理确认"
    if re.search(r"ERCP|PTCD|PTBD|ENBD|支架|引流", value, re.I):
        return "胆道或介入处理"
    if re.search(r"化疗|FOLFIRINOX|吉西他滨|白蛋白紫杉醇|奥沙利铂|替吉奥", value, re.I):
        return "系统治疗"
    if re.search(r"手术|切除", value, re.I):
        return "其他手术"
    return "其他明确动作"


def extract_plan(text: str) -> str:
    for pattern in PLAN_CAPTURE:
        match = pattern.search(text)
        if match:
            value = re.sub(r"\s+", "", match.group(1))
            return value[:240]
    return ""


def safe_state_text(text: str, limit: int = 1800) -> str:
    pieces = re.split(r"(?<=[。；;\n])", text)
    kept = [
        piece.strip()
        for piece in pieces
        if piece.strip() and not LEAK_SENTENCE.search(piece) and not NAMED_PLAN.search(piece)
    ]
    return "".join(kept)[:limit]


def safe_anchor_state(text: str, forbidden: list[str], limit: int = 5000) -> str:
    state = safe_state_text(text, limit=limit)
    has_leak = (
        any(value and value in state for value in forbidden)
        or PLAN_MARKER.search(state)
        or NAMED_PLAN.search(state)
    )
    if not has_leak:
        return state
    cut_points = [text.find(value) for value in forbidden if value and text.find(value) >= 0]
    for pattern in (NAMED_PLAN, PLAN_MARKER):
        match = pattern.search(text)
        if match:
            cut_points.append(match.start())
    if not cut_points:
        return ""
    prefix = text[: min(cut_points)]
    state = safe_state_text(prefix, limit=limit)
    if any(value and value in state for value in forbidden) or PLAN_MARKER.search(state) or NAMED_PLAN.search(state):
        return ""
    return state


def load_bundles() -> list[dict[str, Any]]:
    path = SOURCE / "agent_patient_bundle_SAFD.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def complete_decisions(bundles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for bundle in bundles:
        for window in bundle["windows"]:
            if not (
                window["window_type"] == "decision"
                and window["state"]["evidence_status"] == "Y"
                and window["available_actions"]["status"] == "Y"
                and window["actual_action"]["status"] == "Y"
                and window["feedback"]["status"] == "Y"
                and window.get("evidence_quotes")
            ):
                continue
            rows.append(
                {
                    "patient_id": clean(bundle["patient"]["patient_id"]),
                    "patient_uid": clean(bundle["patient"]["patient_uid"]),
                    "disease_labels": clean(bundle["patient"].get("disease_labels")),
                    "window_id": window["window_id"],
                    "window_date": window["window_date"],
                    "plan": window["available_actions"],
                    "actual": window["actual_action"],
                    "feedback": window["feedback"],
                    "full_window": window,
                }
            )
    return rows


def load_documents(patient_ids: set[str]) -> list[dict[str, Any]]:
    value_set = pa.array(sorted(patient_ids))
    rows: list[dict[str, Any]] = []
    for root in DOCUMENT_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.parquet")):
            parquet = pq.ParquetFile(path)
            for batch in parquet.iter_batches(batch_size=20_000):
                index = batch.schema.get_field_index("PATIENT_ID")
                if index < 0:
                    continue
                selected = batch.filter(pc.is_in(batch.column(index), value_set=value_set))
                if not selected.num_rows:
                    continue
                for row in pa.Table.from_batches([selected]).to_pylist():
                    index_dt = parse_dt(row.get("create_time") or row.get("CREATE_DATE_TIME"))
                    text_value = decode_document(row)
                    header_dt = document_header_time(text_value)
                    dt = header_dt or index_dt
                    if not dt:
                        continue
                    rows.append(
                        {
                            "patient_id": clean(row.get("PATIENT_ID")),
                            "visit_id": clean(row.get("VISIT_ID")),
                            "title": clean(row.get("文书名称")),
                            "create_time": dt,
                            "index_create_time": index_dt,
                            "embedded_document_time": header_dt,
                            "time_basis": "embedded_document_header_time" if header_dt else "index_create_time",
                            "text": text_value,
                            "source_record_id": clean(row.get("source_record_id")),
                            "source_file": clean(row.get("source_file")),
                            "source_row": row.get("source_row"),
                        }
                    )
    return rows


def load_imaging(patient_ids: set[str]) -> list[dict[str, Any]]:
    columns = [
        "patient_id",
        "patient_uid",
        "exam_datetime",
        "exam_method",
        "description_deidentified",
        "diagnosis_deidentified",
        "report_deidentified",
        "source_record_key",
        "index_report_uid",
        "rule_hit",
    ]
    table = pq.read_table(IMAGING, columns=columns)
    table = table.filter(pc.is_in(table["patient_id"], value_set=pa.array(sorted(patient_ids))))
    rows = []
    seen = set()
    for row in table.to_pylist():
        dt = parse_dt(row.get("exam_datetime"))
        if not dt:
            continue
        report = clean(row.get("report_deidentified"))
        if not report:
            report = "描述：" + clean(row.get("description_deidentified")) + "\n诊断：" + clean(row.get("diagnosis_deidentified"))
        dedup_key = (clean(row.get("patient_id")), dt, clean(row.get("exam_method")), report)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        rows.append(
            {
                "patient_id": clean(row.get("patient_id")),
                "patient_uid": clean(row.get("patient_uid")),
                "exam_time": dt,
                "exam_method": clean(row.get("exam_method")),
                "report": report[:12_000],
                "description": clean(row.get("description_deidentified")),
                "diagnosis": clean(row.get("diagnosis_deidentified")),
                "source_record_key": clean(row.get("source_record_key")),
                "index_report_uid": clean(row.get("index_report_uid")),
                "rule_hit": bool(row.get("rule_hit")),
            }
        )
    return rows


def category_matches(expected: list[str], candidate: str) -> bool:
    normalized = {action_category(value) for value in expected}
    if candidate in normalized:
        return True
    surgical = {"胰十二指肠切除", "远端胰腺切除", "全胰切除", "其他胰腺根治切除", "其他手术"}
    return candidate in surgical and bool(normalized & surgical)


def find_actual_time(decision: dict[str, Any], documents: list[dict[str, Any]]) -> tuple[datetime | None, dict[str, Any] | None]:
    operation_day = datetime.fromisoformat(decision["window_date"])
    actual_values = [value for value in decision["actual"].get("exact_actions", []) if not value.startswith("手术已实施")]
    candidates = []
    for row in documents:
        if abs((row["create_time"].date() - operation_day.date()).days) > 1:
            continue
        text = row["text"]
        if not ("手术时间" in text or "开始时间" in text):
            continue
        if actual_values and not any(value in text for value in actual_values):
            if not ("实施手术名称" in text and action_category(text) in {action_category(value) for value in actual_values}):
                continue
        for pattern in SURGERY_TIME_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            value = datetime(*(int(part) for part in match.groups()))
            candidates.append((value, row))
            break
    if not candidates:
        return None, None
    return sorted(candidates, key=lambda item: item[0])[0]


def find_anchor(
    decision: dict[str, Any], documents: list[dict[str, Any]], actual_time: datetime
) -> tuple[dict[str, Any] | None, str]:
    expected = decision["plan"].get("explicit_options", [])
    candidates = []
    for row in documents:
        dt = row["create_time"]
        if not (actual_time - timedelta(days=90) <= dt < actual_time):
            continue
        if POST_TITLE.search(row["title"]):
            continue
        if not (PLAN_TITLE.search(row["title"]) or STATE_TITLE.search(row["title"])):
            continue
        if not (PLAN_MARKER.search(row["text"]) or NAMED_PLAN.search(row["text"])):
            continue
        planned = extract_plan(row["text"])
        if not planned:
            planned = next((option for option in expected if option and option in row["text"]), "")
        if not planned:
            continue
        category = action_category(planned)
        if not category_matches(expected, category):
            continue
        candidates.append({**row, "planned_text": planned, "planned_category": category})
    if not candidates:
        return None, "no_precise_preop_plan_document"
    candidates.sort(key=lambda row: row["create_time"])
    return candidates[0], ""


def old100_metadata() -> tuple[set[str], dict[str, dict[str, Any]]]:
    frame = pd.read_parquet(OLD100)
    ids = set(frame.iloc[:, 0].astype(str))
    meta = {}
    for _, row in frame.iterrows():
        case_id = clean(row.iloc[0])
        meta[case_id] = {
            "signal_time_date": clean(row.iloc[1]),
            "signal_level": clean(row.iloc[2]),
            "signal_site": clean(row.iloc[3]),
            "signal_text": clean(row.iloc[4]),
            "signal_citation": clean(row.iloc[5]),
            "pathway": clean(row.iloc[23]),
        }
    return ids, meta


def find_non_surgical_actual(
    category: str,
    anchor_time: datetime,
    plan_text: str,
    documents: list[dict[str, Any]],
    imaging: list[dict[str, Any]],
) -> tuple[datetime, str, str, str] | None:
    end = anchor_time + timedelta(days=30)
    if category == "补充分期影像":
        requested = "MR" if re.search(r"MR|MRI|磁共振", plan_text, re.I) else "PET" if re.search(r"PET", plan_text, re.I) else "CT"
        rows = [
            row
            for row in imaging
            if anchor_time < row["exam_time"] <= end
            and re.search(requested, row["exam_method"], re.I)
            and PANCREAS_CONTEXT.search(row["report"])
        ]
        if not rows:
            return None
        row = sorted(rows, key=lambda item: item["exam_time"])[0]
        return row["exam_time"], "imaging_exam_time", row["source_record_key"], row["exam_method"]
    terms = {
        "穿刺或病理确认": re.compile(r"(?:行|完成|接受)[^。；\n]{0,50}(?:穿刺|活检|EUS|FNA)|穿刺病理|活检病理", re.I),
        "胆道或介入处理": re.compile(
            r"(?:行|完成|接受)[^。；\n]{0,35}(?:ERCP|PTCD|PTBD|ENBD|胆道支架|胆管支架|胆汁引流|经皮[^。；\n]{0,20}引流)",
            re.I,
        ),
        "系统治疗": re.compile(
            r"(?:今日|今予|本次|现予|开始|继续|完成|接受|行|予)[^。；\n]{0,90}"
            r"(?:新辅助|化疗|放疗|免疫治疗|靶向治疗)|第\s*\d+\s*周期",
            re.I,
        ),
    }[category]
    actual_title = {
        "穿刺或病理确认": re.compile(r"手术记录|术后首次病程|操作记录|穿刺|活检|病理", re.I),
        "胆道或介入处理": re.compile(r"手术记录|术后首次病程|操作记录|介入|ERCP|PTCD|PTBD|ENBD", re.I),
        "系统治疗": re.compile(r"实施肿瘤化疗|化疗记录|放疗记录|抗肿瘤治疗记录|系统治疗记录", re.I),
    }[category]
    rows = [
        row
        for row in documents
        if anchor_time < row["create_time"] <= end
        and actual_title.search(row["title"])
        and terms.search(row["title"] + " " + row["text"])
        and not PLAN_TITLE.search(row["title"])
    ]
    if not rows:
        return None
    for row in sorted(rows, key=lambda item: item["create_time"]):
        match = terms.search(row["title"] + " " + row["text"])
        snippet = match.group(0)[:500] if match else row["title"]
        if category == "胆道或介入处理" and re.search(r"切除|横断|吻合|分离", snippet, re.I):
            continue
        return row["create_time"], "document_create_time_proxy", row["source_record_id"], snippet
    return None


def build_non_surgical_candidates(
    bundles: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    imaging: list[dict[str, Any]],
    old_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    patient_meta = {clean(bundle["patient"]["patient_id"]): bundle["patient"] for bundle in bundles}
    docs_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    images_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in documents:
        docs_by[row["patient_id"]].append(row)
    for row in imaging:
        images_by[row["patient_id"]].append(row)
    candidates = []
    exclusions = []
    seen = set()
    for patient_id in sorted(set(patient_meta) - old_ids):
        docs = sorted(docs_by.get(patient_id, []), key=lambda item: item["create_time"])
        images = sorted(images_by.get(patient_id, []), key=lambda item: item["exam_time"])
        for anchor in docs:
            if POST_TITLE.search(anchor["title"]) or NON_SURGICAL_ANCHOR_EXCLUDE.search(anchor["title"]):
                continue
            for category, pattern in NON_SURGICAL_PLAN_RULES:
                if (patient_id, category) in seen:
                    continue
                match = pattern.search(anchor["text"])
                if not match:
                    continue
                plan_text = match.group(0)[:500]
                if not PANCREAS_CONTEXT.search(plan_text) or ROOT_SURGERY.search(plan_text):
                    continue
                if re.search(r"已|曾|既往|完成|术后|治疗后|复查乳腺", plan_text[:40], re.I):
                    continue
                if category == "系统治疗" and re.search(r"20\d{2}|后于|此前|既往|已行|曾行", plan_text, re.I):
                    continue
                pre_images = [
                    row
                    for row in images
                    if anchor["create_time"] - timedelta(days=180) <= row["exam_time"] < anchor["create_time"]
                ]
                if not pre_images:
                    continue
                actual = find_non_surgical_actual(category, anchor["create_time"], plan_text, docs, images)
                if not actual:
                    continue
                actual_time, actual_basis, actual_source, actual_text = actual
                pre_docs = [
                    row
                    for row in docs
                    if anchor["create_time"] - timedelta(days=180) <= row["create_time"] < anchor["create_time"]
                    and STATE_TITLE.search(row["title"])
                    and not LOW_VALUE_STATE_TITLE.search(row["title"])
                    and not PLAN_TITLE.search(row["title"])
                    and not POST_TITLE.search(row["title"])
                ]
                context_text = "\n".join(
                    [row["report"] for row in pre_images] + [row["text"] for row in pre_docs]
                )
                if PRIOR_PANCREAS_OPERATION.search(context_text):
                    exclusions.append(
                        {"patient_id": patient_id, "window_id": f"NS-{category}", "reason": "non_surgical_prior_pancreas_operation"}
                    )
                    continue
                safe_docs = []
                for row in pre_docs[-12:]:
                    safe_text = safe_state_text(row["text"])
                    if safe_text:
                        safe_docs.append(
                            {
                                "time": iso(row["create_time"]),
                                "title": row["title"],
                                "text": safe_text,
                                "source_record_id": row["source_record_id"],
                            }
                        )
                anchor_state = safe_anchor_state(anchor["text"], [plan_text, actual_text])
                input_payload = {
                    "decision_time": iso(anchor["create_time"]),
                    "time_basis": f"first_explicit_non_surgical_plan_{anchor['time_basis']}",
                    "imaging_before_decision": [
                        {
                            "exam_time": iso(row["exam_time"]),
                            "exam_method": row["exam_method"],
                            "report": row["report"],
                            "source_record_key": row["source_record_key"],
                            "time_basis": "exam_datetime_proxy",
                        }
                        for row in pre_images[-8:]
                    ],
                    "clinical_state_documents": safe_docs,
                    "sanitized_state_from_decision_document": {
                        "time": iso(anchor["create_time"]),
                        "text": anchor_state,
                        "source_record_id": anchor["source_record_id"],
                        "redaction": "计划与实施动作句已删除",
                    }
                    if anchor_state
                    else None,
                }
                if anchor_state and (
                    plan_text in anchor_state
                    or PLAN_MARKER.search(anchor_state)
                    or NAMED_PLAN.search(anchor_state)
                    or ROOT_SURGERY.search(anchor_state)
                ):
                    input_payload["sanitized_state_from_decision_document"] = None
                serialized = json.dumps(input_payload, ensure_ascii=False)
                if plan_text in serialized or actual_text in serialized or NAMED_PLAN.search(serialized):
                    exclusions.append(
                        {"patient_id": patient_id, "window_id": f"NS-{category}", "reason": "non_surgical_action_leak_in_input"}
                    )
                    continue
                meta = patient_meta[patient_id]
                candidates.append(
                    {
                        "patient_id": patient_id,
                        "patient_uid": clean(meta["patient_uid"]),
                        "disease_labels": clean(meta.get("disease_labels")),
                        "window_id": f"NS-{category}",
                        "window_date": actual_time.date().isoformat(),
                        "plan": {
                            "status": "Y",
                            "explicit_options": [plan_text],
                            "action_categories": [category],
                            "note": "原文明确记录的下一步计划。",
                        },
                        "actual": {
                            "status": "Y",
                            "exact_actions": [actual_text],
                            "action_categories": [category],
                            "note": "后续影像或文书证实已实施；文书类使用创建时间代理实施时间。",
                        },
                        "feedback": {
                            "status": "Y",
                            "next_window_id": "",
                            "next_window_date": actual_time.date().isoformat(),
                            "observed_next_window_type": "implemented_action",
                            "observed_evidence": [{"source_record_key": actual_source}],
                            "causal_interpretation": "not_inferred",
                        },
                        "anchor": {
                            **anchor,
                            "planned_text": plan_text,
                            "planned_category": category,
                        },
                        "actual_time": actual_time,
                        "actual_time_basis": actual_basis,
                        "actual_time_source": {"source_record_id": actual_source},
                        "agent_input": input_payload,
                        "old100_exception": False,
                        "preop_metastasis_signal": None,
                        "source_score": min(len(pre_images), 8) * 3 + min(len(safe_docs), 8) * 2,
                        "pre_imaging_count": len(pre_images),
                        "pre_state_document_count": len(safe_docs),
                        "reference_plan_category": category,
                    }
                )
                seen.add((patient_id, category))
    return candidates, exclusions


def build_candidates(
    decisions: list[dict[str, Any]], documents: list[dict[str, Any]], imaging: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    docs_by_patient: dict[str, list[dict[str, Any]]] = defaultdict(list)
    image_by_patient: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in documents:
        docs_by_patient[row["patient_id"]].append(row)
    for row in imaging:
        image_by_patient[row["patient_id"]].append(row)
    old_ids, old_meta = old100_metadata()
    candidates = []
    exclusions = []
    for decision in decisions:
        patient_id = decision["patient_id"]
        actual_time, actual_source = find_actual_time(decision, docs_by_patient.get(patient_id, []))
        if not actual_time:
            exclusions.append(
                {"patient_id": patient_id, "window_id": decision["window_id"], "reason": "no_precise_actual_action_time"}
            )
            continue
        anchor, reason = find_anchor(decision, docs_by_patient.get(patient_id, []), actual_time)
        if not anchor:
            exclusions.append({"patient_id": patient_id, "window_id": decision["window_id"], "reason": reason})
            continue
        if actual_time - anchor["create_time"] > timedelta(days=30):
            exclusions.append(
                {"patient_id": patient_id, "window_id": decision["window_id"], "reason": "plan_to_action_gap_over_30d"}
            )
            continue
        t0 = anchor["create_time"]
        pre_images = sorted(
            [
                row
                for row in image_by_patient.get(patient_id, [])
                if t0 - timedelta(days=180) <= row["exam_time"] < t0
            ],
            key=lambda row: row["exam_time"],
        )
        if not pre_images:
            exclusions.append({"patient_id": patient_id, "window_id": decision["window_id"], "reason": "no_pre_anchor_imaging_180d"})
            continue
        pre_docs = sorted(
            [
                row
                for row in docs_by_patient.get(patient_id, [])
                if t0 - timedelta(days=180) <= row["create_time"] < t0
                and STATE_TITLE.search(row["title"])
                and not LOW_VALUE_STATE_TITLE.search(row["title"])
                and not PLAN_TITLE.search(row["title"])
                and not POST_TITLE.search(row["title"])
            ],
            key=lambda row: row["create_time"],
        )
        safe_docs = []
        for row in pre_docs[-12:]:
            safe_text = safe_state_text(row["text"])
            if safe_text:
                safe_docs.append(
                    {
                        "time": iso(row["create_time"]),
                        "title": row["title"],
                        "text": safe_text,
                        "source_record_id": row["source_record_id"],
                    }
                )
        plan_options = decision["plan"].get("explicit_options", [])
        actual_actions = decision["actual"].get("exact_actions", [])
        input_payload = {
            "decision_time": iso(t0),
            "time_basis": f"first_explicit_plan_{anchor['time_basis']}",
            "imaging_before_decision": [
                {
                    "exam_time": iso(row["exam_time"]),
                    "exam_method": row["exam_method"],
                    "report": row["report"],
                    "source_record_key": row["source_record_key"],
                    "time_basis": "exam_datetime_proxy",
                }
                for row in pre_images[-8:]
            ],
            "clinical_state_documents": safe_docs,
            "sanitized_state_from_decision_document": {
                "time": iso(t0),
                "text": safe_anchor_state(anchor["text"], plan_options + actual_actions),
                "source_record_id": anchor["source_record_id"],
                "redaction": "计划与实施动作句已删除",
            },
        }
        anchor_state = input_payload["sanitized_state_from_decision_document"]["text"]
        if anchor_state and (
            any(value and value in anchor_state for value in plan_options + actual_actions)
            or PLAN_MARKER.search(anchor_state)
            or NAMED_PLAN.search(anchor_state)
        ):
            input_payload["sanitized_state_from_decision_document"] = None
        serialized_input = json.dumps(input_payload, ensure_ascii=False)
        leaking = [value for value in plan_options + actual_actions if value and value in serialized_input]
        if leaking or PLAN_MARKER.search(serialized_input) or NAMED_PLAN.search(serialized_input):
            exclusions.append(
                {
                    "patient_id": patient_id,
                    "window_id": decision["window_id"],
                    "reason": "planned_or_actual_action_leak_in_input",
                    "details": ";".join(leaking[:3]),
                }
            )
            continue
        old = patient_id in old_ids
        exception = False
        preop_signal = None
        if old:
            meta = old_meta[patient_id]
            signal_candidates = [row for row in pre_images if MET_SIGNAL.search(row["diagnosis"] + " " + row["description"])]
            if meta["signal_level"] == "明确" and signal_candidates:
                signal = signal_candidates[0]
                exception = True
                preop_signal = {
                    "exam_time": iso(signal["exam_time"]),
                    "exam_method": signal["exam_method"],
                    "text": signal["report"][:1200],
                    "source_record_key": signal["source_record_key"],
                    "signal_level": meta["signal_level"],
                    "pathway": meta["pathway"],
                }
            else:
                exclusions.append(
                    {
                        "patient_id": patient_id,
                        "window_id": decision["window_id"],
                        "reason": "old100_excluded_no_explicit_preop_imaging_metastasis",
                    }
                )
                continue
        score = min(len(pre_images), 8) * 3 + min(len(safe_docs), 8) * 2 + (4 if preop_signal else 0)
        candidates.append(
            {
                **decision,
                "anchor": anchor,
                "actual_time": actual_time,
                "actual_time_basis": "surgery_start_time",
                "actual_time_source": actual_source,
                "agent_input": input_payload,
                "old100_exception": exception,
                "preop_metastasis_signal": preop_signal,
                "source_score": score,
                "pre_imaging_count": len(pre_images),
                "pre_state_document_count": len(safe_docs),
                "reference_plan_category": action_category(";".join(plan_options)),
            }
        )
    return candidates, exclusions


def choose_cohort(candidates: list[dict[str, Any]], target_n: int) -> list[dict[str, Any]]:
    best_by_patient: dict[str, dict[str, Any]] = {}
    for row in sorted(candidates, key=lambda value: (-value["source_score"], value["anchor"]["create_time"])):
        best_by_patient.setdefault(row["patient_id"], row)
    rows = list(best_by_patient.values())
    exceptions = sorted([row for row in rows if row["old100_exception"]], key=lambda row: -row["source_score"])
    ordinary = sorted([row for row in rows if not row["old100_exception"]], key=lambda row: -row["source_score"])

    selected = exceptions[:]
    counts = Counter(row["reference_plan_category"] for row in selected)
    soft_caps = {
        "远端胰腺切除": 70,
        "胰十二指肠切除": 55,
        "其他胰腺根治切除": 35,
        "其他手术": 25,
        "全胰切除": 8,
        "探查或病理确认": 8,
        "胆道或介入处理": 8,
        "系统治疗": 8,
        "其他明确动作": 20,
    }
    deferred = []
    for row in ordinary:
        category = row["reference_plan_category"]
        if counts[category] < soft_caps.get(category, 20) and len(selected) < target_n:
            selected.append(row)
            counts[category] += 1
        else:
            deferred.append(row)
    for row in deferred:
        if len(selected) >= target_n:
            break
        selected.append(row)
    return selected[:target_n]


def choose_non_surgical(candidates: list[dict[str, Any]], per_category: int = 15) -> list[dict[str, Any]]:
    selected = []
    used_patients: set[str] = set()
    for category, _ in NON_SURGICAL_PLAN_RULES:
        if category == "系统治疗":
            continue
        rows = sorted(
            [row for row in candidates if row["reference_plan_category"] == category],
            key=lambda row: (-row["source_score"], row["anchor"]["create_time"]),
        )
        for row in rows:
            if row["patient_id"] in used_patients:
                continue
            selected.append(row)
            used_patients.add(row["patient_id"])
            if sum(item["reference_plan_category"] == category for item in selected) >= per_category:
                break
    return selected


def enrich_timeline(selected: list[dict[str, Any]]) -> None:
    import sys

    project = ROOT / "pipeline_outputs_stage8_v1" / "pancreas_decision_window_enriched_v1"
    sys.path.insert(0, str(project))
    from scripts.cohort_construction.agent_packaging_and_audit.cohort_100 import (
        step01_build_patient_states_100 as state_builder,
    )

    by_uid = {row["patient_uid"]: row for row in selected}
    _, labs, pathology = state_builder.load_timeline_rows(set(by_uid))
    for row in selected:
        row["agent_input"]["important_laboratory_before_decision"] = []
        row["agent_input"]["pathology_before_decision"] = []
    for lab in labs:
        uid = clean(lab.get("patient_uid"))
        target = by_uid.get(uid)
        if not target:
            continue
        t0 = target["anchor"]["create_time"]
        dt = parse_dt(lab.get("available_time") or lab.get("report_time") or lab.get("event_time_used"))
        name = clean(lab.get("item_name") or lab.get("检验项目") or lab.get("项目名称"))
        if not dt or not (t0 - timedelta(days=90) <= dt < t0) or not IMPORTANT_LAB.search(name):
            continue
        target["agent_input"]["important_laboratory_before_decision"].append(
            {
                "available_time": iso(dt),
                "item_name": name,
                "result": clean(
                    lab.get("result_raw")
                    or lab.get("result_text_value")
                    or lab.get("result_qualitative_value")
                    or lab.get("result_numeric_value")
                ),
                "unit": clean(lab.get("unit_normalized") or lab.get("unit_raw") or lab.get("unit") or lab.get("单位")),
                "abnormal_flag": clean(lab.get("source_abnormal_flag") or lab.get("abnormal_flag")),
                "source_record_key": clean(lab.get("source_record_key")),
            }
        )
    tumor_marker = re.compile(r"CA\s*19|CA199|CA\s*125|CA125|CEA|癌胚抗原|肿瘤标", re.I)
    for row in selected:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in row["agent_input"]["important_laboratory_before_decision"]:
            grouped[item["item_name"]].append(item)
        compact = []
        for name, items in grouped.items():
            items.sort(key=lambda item: item["available_time"])
            compact.extend(items[-3:] if tumor_marker.search(name) else items[-1:])
        compact.sort(key=lambda item: item["available_time"])
        row["agent_input"]["important_laboratory_before_decision"] = compact[-40:]
    for event in pathology:
        uid = clean(event.get("patient_uid"))
        target = by_uid.get(uid)
        if not target:
            continue
        t0 = target["anchor"]["create_time"]
        dt = parse_dt(event.get("report_time") or event.get("available_time") or event.get("event_time_used") or event.get("event_date"))
        if not dt or not (t0 - timedelta(days=180) <= dt < t0):
            continue
        target["agent_input"]["pathology_before_decision"].append(
            {
                "available_time": iso(dt),
                "source_record_key": clean(event.get("source_record_key")),
                "event_type": clean(event.get("event_type") or event.get("pathology_type") or "pathology"),
            }
        )

    pathology_columns = [
        "病人编号",
        "标本类型",
        "标本名称",
        "临床诊断",
        "病理诊断",
        "取材日期",
        "报告日期",
        "specimen_date_parsed",
        "report_date_parsed",
        "source_record_key",
    ]
    pathology_table = state_builder.read_pathology_table(pathology_columns)
    patient_ids = {row["patient_id"] for row in selected}
    pathology_table = pathology_table.filter(
        pc.is_in(pathology_table["病人编号"], value_set=pa.array(sorted(patient_ids)))
    )
    selected_by_id = {row["patient_id"]: row for row in selected}
    pathology_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in pathology_table.to_pylist():
        patient_id = clean(item.get("病人编号"))
        target = selected_by_id.get(patient_id)
        if not target:
            continue
        report_date = parse_dt(item.get("report_date_parsed") or item.get("报告日期"))
        if not report_date or report_date.date() >= target["anchor"]["create_time"].date():
            continue
        pathology_by_id[patient_id].append(
            {
                "report_date": report_date.date().isoformat(),
                "time_precision": "day",
                "specimen_date": clean(item.get("specimen_date_parsed") or item.get("取材日期")),
                "specimen_type": clean(item.get("标本类型")),
                "specimen_name": clean(item.get("标本名称")),
                "clinical_diagnosis": clean(item.get("临床诊断"))[:1200],
                "pathology_diagnosis": clean(item.get("病理诊断"))[:2400],
                "source_record_key": clean(item.get("source_record_key")),
            }
        )
    for patient_id, items in pathology_by_id.items():
        target = selected_by_id[patient_id]
        existing = target["agent_input"]["pathology_before_decision"]
        existing.extend(sorted(items, key=lambda item: item["report_date"])[-5:])


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    return value


def write_outputs(selected: list[dict[str, Any]], candidates: list[dict[str, Any]], exclusions: list[dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    case_rows = []
    index_rows = []
    for index, row in enumerate(selected, start=1):
        case_id = f"PREOP-{index:03d}"
        plan = row["plan"]
        actual = row["actual"]
        case = {
            "schema_version": "preop_decision_agent_v1",
            "case_id": case_id,
            "patient": {
                "patient_id": row["patient_id"],
                "patient_uid": row["patient_uid"],
                "disease_labels": row["disease_labels"],
            },
            "decision_anchor": {
                "time": iso(row["anchor"]["create_time"]),
                "time_precision": "minute",
                "time_basis": row["agent_input"]["time_basis"],
                "source_title": row["anchor"]["title"],
                "source_record_id": row["anchor"]["source_record_id"],
                "source_is_hidden_from_agent": True,
            },
            "observed_action_time": {
                "time": iso(row["actual_time"]),
                "time_precision": "minute",
                "time_basis": row.get("actual_time_basis", "surgery_start_time"),
                "source_record_id": row["actual_time_source"]["source_record_id"],
            },
            "decision_question": {
                "level_1": "直接进入根治性手术、补充分期、分期腹腔镜、病理确认、新辅助或系统治疗、姑息介入、观察随访中选择下一步",
                "level_2": "仅在选择手术后决定具体术式",
            },
            "agent_input": row["agent_input"],
            "reference_labels": {
                "label_type": "observed_clinician_behavior_not_normative_ground_truth",
                "documented_plan": plan,
                "actual_action": actual,
                "subsequent_feedback": row["feedback"],
                "old100_preop_imaging_metastasis_exception": row["old100_exception"],
                "preop_metastasis_signal": row["preop_metastasis_signal"],
            },
            "leakage_control": {
                "plan_document_hidden": True,
                "current_plan_text_absent_from_agent_input": True,
                "current_actual_action_absent_from_agent_input": True,
                "longitudinal_events_strictly_before_anchor": True,
                "decision_document_state_included_only_after_plan_sentence_redaction": True,
                "imaging_time_basis": "exam_datetime_proxy_not_report_release_time",
            },
        }
        case_rows.append(case)
        index_rows.append(
            {
                "序号": index,
                "case_id": case_id,
                "患者编号": row["patient_id"],
                "patient_uid": row["patient_uid"],
                "病种": row["disease_labels"],
                "决策截断时间": iso(row["anchor"]["create_time"]),
                "时间精度": "分钟",
                "隐藏计划文书名称": row["anchor"]["title"],
                "计划动作分类": row["reference_plan_category"],
                "记录的计划": ";".join(plan.get("explicit_options", [])),
                "实际动作": ";".join(actual.get("exact_actions", [])),
                "实际动作时间": iso(row["actual_time"]),
                "实际动作时间依据": row.get("actual_time_basis", "surgery_start_time"),
                "计划至实施间隔小时": round((row["actual_time"] - row["anchor"]["create_time"]).total_seconds() / 3600, 2),
                "术前影像数_180天": row["pre_imaging_count"],
                "术前状态文书数": row["pre_state_document_count"],
                "重要检验条目数": len(row["agent_input"].get("important_laboratory_before_decision", [])),
                "术前病理事件数": len(row["agent_input"].get("pathology_before_decision", [])),
                "原100例例外": "Y" if row["old100_exception"] else "N",
                "术前影像明确转移": "Y" if row["preop_metastasis_signal"] else "N",
                "输入计划泄漏检查": "通过",
                "标签性质": "医生实际行为_非规范金标准",
            }
        )
    with (OUT / "preop_agent_cases_180.jsonl").open("w", encoding="utf-8") as handle:
        for row in case_rows:
            handle.write(json.dumps(jsonable(row), ensure_ascii=False) + "\n")
    with (OUT / "preop_agent_inputs_180.jsonl").open("w", encoding="utf-8") as handle:
        for row in case_rows:
            safe_case = {
                "schema_version": row["schema_version"],
                "case_id": row["case_id"],
                "patient": {
                    "patient_id": row["patient"]["patient_id"],
                    "patient_uid": row["patient"]["patient_uid"],
                },
                "decision_time": row["decision_anchor"]["time"],
                "decision_question": row["decision_question"],
                "agent_input": row["agent_input"],
            }
            handle.write(json.dumps(safe_case, ensure_ascii=False) + "\n")
    with (OUT / "preop_reference_labels_180.jsonl").open("w", encoding="utf-8") as handle:
        for row in case_rows:
            answer = {
                "case_id": row["case_id"],
                "decision_anchor": row["decision_anchor"],
                "observed_action_time": row["observed_action_time"],
                "reference_labels": row["reference_labels"],
                "leakage_control": row["leakage_control"],
            }
            handle.write(json.dumps(answer, ensure_ascii=False) + "\n")
    pd.DataFrame(index_rows).to_csv(OUT / "cohort_index_180.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(exclusions).to_csv(OUT / "exclusion_audit.csv", index=False, encoding="utf-8-sig")
    candidate_rows = [
        {
            "患者编号": row["patient_id"],
            "patient_uid": row["patient_uid"],
            "窗口编号": row["window_id"],
            "计划锚点": iso(row["anchor"]["create_time"]),
            "计划动作分类": row["reference_plan_category"],
            "来源丰富度": row["source_score"],
            "原100例例外": "Y" if row["old100_exception"] else "N",
            "是否入选": "Y" if row in selected else "N",
        }
        for row in candidates
    ]
    pd.DataFrame(candidate_rows).to_csv(OUT / "eligible_candidate_audit.csv", index=False, encoding="utf-8-sig")
    distribution = Counter(row["reference_plan_category"] for row in selected)
    audit = {
        "generated_at": "2026-10-07",
        "target_n": TARGET_N,
        "selected_n": len(selected),
        "eligible_patient_windows": len(candidates),
        "selected_unique_patients": len({row["patient_id"] for row in selected}),
        "old100_exception_n": sum(row["old100_exception"] for row in selected),
        "ordinary_new_n": sum(not row["old100_exception"] for row in selected),
        "surgical_arm_n": sum(row.get("actual_time_basis") == "surgery_start_time" for row in selected),
        "non_surgical_arm_n": sum(row.get("actual_time_basis") != "surgery_start_time" for row in selected),
        "action_time_basis_distribution": dict(Counter(row.get("actual_time_basis", "surgery_start_time") for row in selected)),
        "decision_time_basis_distribution": dict(Counter(row["anchor"].get("time_basis", "index_create_time") for row in selected)),
        "sanitized_decision_state_n": sum(
            bool(row["agent_input"].get("sanitized_state_from_decision_document")) for row in selected
        ),
        "important_laboratory_count": sum(
            len(row["agent_input"].get("important_laboratory_before_decision", [])) for row in selected
        ),
        "cases_with_important_laboratory": sum(
            bool(row["agent_input"].get("important_laboratory_before_decision")) for row in selected
        ),
        "cases_without_important_laboratory": sum(
            not row["agent_input"].get("important_laboratory_before_decision") for row in selected
        ),
        "laboratory_scope": "决策截断前90天内命中重要检验规则的条目；Stage 7全部Parquet分片",
        "action_distribution": dict(distribution),
        "exclusion_counts": dict(Counter(row["reason"] for row in exclusions)),
        "hard_checks": {
            "all_minute_precision_anchor": all(
                row["anchor"]["create_time"].hour != 0 or row["anchor"]["create_time"].minute != 0
                for row in selected
            ),
            "all_have_minute_or_exam_time_action_proxy": all(row["actual_time"] for row in selected),
            "all_anchor_strictly_before_actual_action": all(
                row["anchor"]["create_time"] < row["actual_time"] for row in selected
            ),
            "all_have_pre_anchor_imaging": all(row["pre_imaging_count"] >= 1 for row in selected),
            "all_longitudinal_events_before_anchor": True,
            "same_time_decision_document_only_after_plan_redaction": True,
            "all_plan_documents_hidden": True,
            "all_old100_rows_are_explicit_preop_imaging_metastasis": all(
                (not row["old100_exception"]) or row["preop_metastasis_signal"] for row in selected
            ),
        },
        "known_limitations": [
            "影像只有检查时间，尚无完整报告签发或医生阅片时间。",
            "reference_labels记录医生实际行为，不代表指南或专家规范答案。",
            "决策文书仅保留删除计划与实施动作句后的病情部分；无法安全删除时整段不进入Agent输入。",
            "非手术动作中，影像使用检查时间；穿刺和介入使用实施后文书临床时间代理，需在正式建模前抽样核对。",
            "系统治疗候选因既往治疗回顾与实际新实施难以稳定区分，当前高精度版本未纳入。",
        ],
    }
    (OUT / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


def main() -> None:
    bundles = load_bundles()
    decisions = complete_decisions(bundles)
    patient_ids = {clean(bundle["patient"]["patient_id"]) for bundle in bundles}
    documents = load_documents(patient_ids)
    imaging = load_imaging(patient_ids)
    surgical_candidates, surgical_exclusions = build_candidates(decisions, documents, imaging)
    old_ids, _ = old100_metadata()
    non_surgical_candidates, non_surgical_exclusions = build_non_surgical_candidates(
        bundles, documents, imaging, old_ids
    )
    non_surgical_selected = choose_non_surgical(non_surgical_candidates, per_category=15)
    non_surgical_ids = {row["patient_id"] for row in non_surgical_selected}
    surgical_selected = choose_cohort(
        [row for row in surgical_candidates if row["patient_id"] not in non_surgical_ids],
        TARGET_N - len(non_surgical_selected),
    )
    selected = non_surgical_selected + surgical_selected
    candidates = surgical_candidates + non_surgical_candidates
    exclusions = surgical_exclusions + non_surgical_exclusions
    if len(selected) < 150:
        raise RuntimeError(f"eligible cohort below requested minimum: {len(selected)}")
    enrich_timeline(selected)
    write_outputs(selected, candidates, exclusions)


if __name__ == "__main__":
    main()

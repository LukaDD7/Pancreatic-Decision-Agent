from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from scripts.cohort_construction.paths import data_root
from scripts.cohort_construction.model_api import ModelAPISettings, load_model_api_settings

ROOT = data_root()
PRIOR_ROOT = ROOT / "pipeline_outputs_stage8_v1/pancreas_restage_special_cases_v2"
OUTPUT = ROOT / "pipeline_outputs_stage8_v1/pancreas_decision_failure_cases_v3"
CANDIDATES = PRIOR_ROOT / "restricted/rule_candidates.parquet"
PRIOR_SHORTLIST = PRIOR_ROOT / "restricted/shortlist.parquet"
PRIOR_MODEL = PRIOR_ROOT / "restricted/model_adjudication.jsonl"
IMAGING = ROOT / "pipeline_outputs_stage8_v1/pancreas_imaging_report_index_v2/restricted/index/report_index.parquet"
PROMPT_VERSION = "pancreas_decision_failure_five_class_v3"

REASON_LABELS = {
    "C1_OCCULT_METASTASIS": "隐匿性转移",
    "C2_INCOMPLETE_STAGING": "可用数据内分期不完整/缺少术前肝MRI",
    "C3_STALE_IMAGING": "最近分期影像距手术超过4周",
    "C4_PREOP_METASTASIS_NOT_ACTED": "术前已有转移证据但手术计划未及时改变",
    "C5_VASCULAR_UNDERESTIMATION": "术中血管侵犯较术前估计严重",
}

NEGATION = re.compile(r"未见|未发现|未查见|无明显|未提示|排除|除外|阴性|无[^，。；]{0,12}(?:转移|种植)")
UNCERTAINTY = re.compile(r"可能|可疑|考虑|倾向|不除外|不能除外|待排|性质待定|建议复查")
METASTASIS = re.compile(r"肝.{0,18}(?:转移|癌结节)|腹膜.{0,18}(?:转移|种植)|大网膜.{0,18}(?:转移|种植)|转移瘤|转移灶")
LIVER_LESION = re.compile(r"肝.{0,24}(?:结节|病灶|占位|低密度|异常信号|转移)")
OPERATIVE_LIVER = re.compile(r"肝(?:脏)?(?:表面|内)?.{0,30}(?:结节|病灶|转移|占位)")
OPERATIVE_DISTANT = re.compile(
    r"肝.{0,30}(?:结节|转移)|腹膜.{0,30}(?:结节|转移|种植)|"
    r"大网膜.{0,30}(?:结节|转移|种植)|腹壁.{0,30}(?:结节|转移|恶性)|"
    r"腹腔(?:种植|继发性恶性肿瘤)"
)
OPERATIVE_VESSEL = re.compile(
    r"(?:SMA|SMV|肠系膜上动脉|肠系膜上静脉|腹腔干|肝总动脉|门静脉|PV).{0,100}(?:侵犯|包绕|无法分离|不能分离)|"
    r"(?:侵犯|包绕|无法分离|不能分离).{0,100}(?:SMA|SMV|肠系膜上动脉|肠系膜上静脉|腹腔干|肝总动脉|门静脉|PV)",
    re.I | re.S,
)
ABORT = re.compile(r"终止手术|放弃.{0,10}切除|未行.{0,10}切除|无法切除|不可切除|仅行.{0,20}(?:探查|活检|旁路)")
MAJOR_RESECTION = re.compile(r"胰十二指肠切除|Whipple|胰体尾.{0,8}切除|远端胰腺切除|全胰切除|胰腺.{0,12}根治")


def clean(value: object) -> str:
    return str(value or "").strip()


def as_date(value: object) -> date | None:
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", clean(value))
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def load_json(value: object, default):
    try:
        return json.loads(clean(value))
    except (json.JSONDecodeError, TypeError):
        return default


def split_clauses(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", item).strip() for item in re.split(r"[。；;\n]", text) if item.strip()]


def positive_metastasis_clauses(text: str) -> tuple[list[str], list[str]]:
    strong, uncertain = [], []
    for clause in split_clauses(text):
        if not (METASTASIS.search(clause) or LIVER_LESION.search(clause)):
            continue
        if NEGATION.search(clause):
            continue
        if UNCERTAINTY.search(clause):
            uncertain.append(clause[:360])
        elif METASTASIS.search(clause):
            strong.append(clause[:360])
    return strong, uncertain


def is_cross_sectional(method: str) -> bool:
    return bool(re.search(r"(?:胰腺|上腹部|全腹部|肝脏).*(?:CT|MR)|(?:CT|MR).*(?:胰腺|上腹部|全腹部|肝脏)", method, re.I))


def is_hepatic_mri(method: str) -> bool:
    return bool(re.search(r"肝脏.*MR|MR.*肝脏", method, re.I))


def operative_text(row: dict) -> str:
    evidence = load_json(row.get("evidence_snippets_json"), [])
    return " ".join(clean(item.get("snippet")) for item in evidence)


def build_features(candidates: list[dict], imaging_rows: list[dict], prior: dict[str, dict], prior_reviewed: set[str]) -> list[dict]:
    by_uid: dict[str, list[dict]] = defaultdict(list)
    for row in imaging_rows:
        if row.get("exam_date") and row.get("patient_uid"):
            by_uid[row["patient_uid"]].append(row)
    output = []
    for row in candidates:
        event = as_date(row.get("event_date"))
        preop = []
        if event:
            for image in by_uid.get(row["patient_uid"], []):
                exam = as_date(image.get("exam_date"))
                if not exam or not is_cross_sectional(clean(image.get("exam_method"))):
                    continue
                gap = (event - exam).days
                if 0 <= gap <= 180:
                    value = dict(image)
                    value["days_before_event"] = gap
                    preop.append(value)
        preop.sort(key=lambda item: (item["days_before_event"], clean(item.get("source_record_key"))))
        latest = preop[0] if preop else None
        hepatic_mri = [item for item in preop if is_hepatic_mri(clean(item.get("exam_method")))]
        strong, uncertain = [], []
        for image in preop:
            text = f"{clean(image.get('description_deidentified'))} {clean(image.get('diagnosis_deidentified'))}"
            s, u = positive_metastasis_clauses(text)
            for clause in s:
                strong.append({"date": image["exam_date"], "method": image["exam_method"], "text": clause, "source_record_key": image["source_record_key"]})
            for clause in u:
                uncertain.append({"date": image["exam_date"], "method": image["exam_method"], "text": clause, "source_record_key": image["source_record_key"]})
        op_text = operative_text(row)
        distant = clean(row.get("phenotype")) == "INTRAOPERATIVE_SUSPECTED_METASTASIS" or bool(OPERATIVE_DISTANT.search(op_text))
        local_unresectable = clean(row.get("phenotype")) == "INTRAOPERATIVE_LOCAL_UNRESECTABLE"
        vascular = bool(OPERATIVE_VESSEL.search(op_text))
        liver = bool(OPERATIVE_LIVER.search(op_text))
        stale = latest is not None and latest["days_before_event"] > 28
        no_imaging = latest is None
        no_liver_mri = not hepatic_mri
        if strong and distant:
            primary = "C4_PREOP_METASTASIS_NOT_ACTED"
        elif distant and stale:
            primary = "C3_STALE_IMAGING"
        elif distant and (no_imaging or (liver and no_liver_mri)):
            primary = "C2_INCOMPLETE_STAGING"
        elif distant:
            primary = "C1_OCCULT_METASTASIS"
        elif vascular and stale:
            primary = "C3_STALE_IMAGING"
        elif vascular:
            primary = "C5_VASCULAR_UNDERESTIMATION"
        else:
            primary = "EXCLUDE_OTHER"
        secondary = []
        if distant and liver and no_liver_mri:
            secondary.append("C2_INCOMPLETE_STAGING")
        if stale:
            secondary.append("C3_STALE_IMAGING")
        if strong:
            secondary.append("C4_PREOP_METASTASIS_NOT_ACTED")
        if vascular:
            secondary.append("C5_VASCULAR_UNDERESTIMATION")
        prior_row = prior.get(row["candidate_id"])
        output.append({
            **row,
            "operative_distant_signal": distant,
            "operative_liver_signal": liver,
            "operative_local_unresectable_signal": local_unresectable,
            "operative_vascular_signal": vascular,
            "latest_preop_imaging_date": latest["exam_date"] if latest else "",
            "latest_preop_imaging_method": latest["exam_method"] if latest else "",
            "latest_preop_imaging_age_days": latest["days_before_event"] if latest else None,
            "hepatic_mri_before_event": bool(hepatic_mri),
            "latest_hepatic_mri_date": hepatic_mri[0]["exam_date"] if hepatic_mri else "",
            "preop_strong_metastasis_json": json.dumps(strong[:5], ensure_ascii=False),
            "preop_uncertain_metastasis_json": json.dumps(uncertain[:5], ensure_ascii=False),
            "rule_primary_reason": primary,
            "rule_secondary_reasons_json": json.dumps(sorted(set(secondary) - {primary}), ensure_ascii=False),
            "previously_model_reviewed": row["candidate_id"] in prior_reviewed,
            "previously_selected": prior_row is not None,
            "prior_case_id": clean(prior_row.get("case_id")) if prior_row else "",
            "review_round": "v2_prior" if row["candidate_id"] in prior_reviewed else "v3_new",
        })
    return output


def select_for_model(features: list[dict], prior_uids: set[str]) -> list[dict]:
    by_reason: dict[str, list[dict]] = defaultdict(list)
    seen_uids = set(prior_uids)
    ranked = sorted(features, key=lambda row: (
        -int(row.get("rule_score") or 0),
        int(row.get("latest_preop_imaging_age_days") or 9999),
        clean(row.get("candidate_id")),
    ))
    for row in ranked:
        reason = row["rule_primary_reason"]
        if reason not in REASON_LABELS or row["previously_model_reviewed"] or row["patient_uid"] in seen_uids:
            continue
        if not (row["operative_distant_signal"] or row["operative_vascular_signal"]):
            continue
        by_reason[reason].append(row)
        seen_uids.add(row["patient_uid"])
    limits = {
        "C1_OCCULT_METASTASIS": 45,
        "C2_INCOMPLETE_STAGING": 55,
        "C3_STALE_IMAGING": 45,
        "C4_PREOP_METASTASIS_NOT_ACTED": 35,
        "C5_VASCULAR_UNDERESTIMATION": 55,
    }
    selected = []
    for reason, limit in limits.items():
        selected.extend(by_reason[reason][:limit])
    return selected


def model_payload(row: dict) -> dict:
    evidence = load_json(row.get("evidence_snippets_json"), [])[:4]
    return {
        "candidate_id": row["candidate_id"],
        "event_date": row.get("event_date"),
        "phenotype": row.get("phenotype"),
        "planned_procedure": row.get("planned_procedure"),
        "actual_procedure": row.get("actual_procedure"),
        "operative_evidence": [{"date": x.get("date"), "title": x.get("title"), "text": clean(x.get("snippet"))[:1000]} for x in evidence],
        "latest_preop_imaging_date": row.get("latest_preop_imaging_date"),
        "latest_preop_imaging_method": row.get("latest_preop_imaging_method"),
        "latest_preop_imaging_age_days": row.get("latest_preop_imaging_age_days"),
        "hepatic_mri_before_event": row.get("hepatic_mri_before_event"),
        "preop_strong_metastasis": load_json(row.get("preop_strong_metastasis_json"), []),
        "preop_uncertain_metastasis": load_json(row.get("preop_uncertain_metastasis_json"), []),
        "rule_primary_reason": row.get("rule_primary_reason"),
        "nearby_pathology_specimen": row.get("nearby_pathology_specimen"),
        "nearby_pathology_diagnosis": clean(row.get("nearby_pathology_diagnosis"))[:1200],
    }


def invoke(settings: ModelAPISettings, row: dict) -> dict:
    payload = model_payload(row)
    prompt = f"""你是胰腺外科病例证据审阅者。只按提供材料分类，不能补写事实，也不要直接评价医生过失。

目标是回顾性识别根治性手术前分期/决策流程失配。分类：
C1_OCCULT_METASTASIS：术前近期影像未提示转移，术中才发现隐匿肝/腹膜等转移。
C2_INCOMPLETE_STAGING：术中发现尤其小肝转移，而可用数据中缺少充分术前分期；没有肝MRI只能写“可用数据内缺失”，不能断言现实中没做。
C3_STALE_IMAGING：最近可用分期影像距手术超过28天；28天以内不属于此类。
C4_PREOP_METASTASIS_NOT_ACTED：术前报告已明确转移或高度提示转移，但根治手术计划仍推进；仅“可能/不除外”通常证据不足。
C5_VASCULAR_UNDERESTIMATION：术中因SMA、SMV、腹腔干、肝总动脉、门静脉等侵犯比术前估计严重，不能按计划切除；这是影像低估类。
EXCLUDE：未证明计划与术中事实发生关键冲突、实际完成常规根治切除、证据矛盾或材料不足。

允许primary_class从以上6类中选；secondary_classes可多选前5类。返回JSON字段：valid_case, primary_class, secondary_classes, confidence(high|medium|low), decision_gap, operative_trigger, imaging_assessment, rationale, evidence_quotes（最多4条短句）。

证据：{json.dumps(payload, ensure_ascii=False)}"""
    response = requests.post(
        settings.api_url,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        json={
            "model": settings.model,
            "messages": [
                {"role": "system", "content": "严格做医学证据归类，输出合法JSON，不作诊疗建议。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 1300,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    response.raise_for_status()
    value = json.loads(response.json()["choices"][0]["message"]["content"])
    allowed = set(REASON_LABELS) | {"EXCLUDE"}
    if value.get("primary_class") not in allowed or value.get("confidence") not in {"high", "medium", "low"}:
        raise ValueError("invalid_model_output")
    value["candidate_id"] = row["candidate_id"]
    return value


def write_xlsx(path: Path, rows: list[dict], fields: list[str], title: str) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    sheet.append(fields)
    for row in rows:
        sheet.append([row.get(field) for field in fields])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    workbook.save(path)


def main() -> None:
    restricted = OUTPUT / "restricted"
    audit_dir = OUTPUT / "audit"
    restricted.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)

    candidates = pq.read_table(CANDIDATES).to_pylist()
    prior_rows = pq.read_table(PRIOR_SHORTLIST).to_pylist()
    prior = {row["candidate_id"]: row for row in prior_rows}
    prior_reviewed = {
        json.loads(line)["candidate_id"]
        for line in PRIOR_MODEL.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    wanted_uids = {row["patient_uid"] for row in candidates}
    imaging_rows = [row for row in pq.read_table(IMAGING).to_pylist() if row.get("patient_uid") in wanted_uids]
    features = build_features(candidates, imaging_rows, prior, prior_reviewed)
    selected = select_for_model(features, {row["patient_uid"] for row in prior_rows})

    result_path = restricted / "model_adjudication.jsonl"
    model_results = {}
    if result_path.exists():
        for line in result_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                model_results[value["candidate_id"]] = value
    pending = [row for row in selected if row["candidate_id"] not in model_results]
    errors = {}
    model_name = "existing_results"
    if pending:
        settings = load_model_api_settings()
        model_name = settings.model
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(invoke, settings, row): row["candidate_id"] for row in pending}
            for future in as_completed(futures):
                candidate_id = futures[future]
                try:
                    model_results[candidate_id] = future.result()
                except Exception as exc:
                    errors[candidate_id] = f"{type(exc).__name__}:{exc}"
        with result_path.open("w", encoding="utf-8") as stream:
            for key in sorted(model_results):
                stream.write(json.dumps(model_results[key], ensure_ascii=False) + "\n")

    feature_by_id = {row["candidate_id"]: row for row in features}
    consolidated = []
    seen_uids = set()
    for old in prior_rows:
        row = feature_by_id[old["candidate_id"]]
        if row["patient_uid"] in seen_uids:
            continue
        if row["rule_primary_reason"] in REASON_LABELS:
            prior_primary = row["rule_primary_reason"]
        elif old["case_class"] == "occult_distant_metastasis_aborted":
            prior_primary = "C1_OCCULT_METASTASIS"
        elif row["operative_vascular_signal"]:
            prior_primary = "C5_VASCULAR_UNDERESTIMATION"
        else:
            continue
        seen_uids.add(row["patient_uid"])
        consolidated.append((row, {
            "primary_class": prior_primary,
            "secondary_classes": load_json(row["rule_secondary_reasons_json"], []),
            "confidence": old["confidence"],
            "decision_gap": old["model_rationale"],
            "operative_trigger": old["trigger"],
            "imaging_assessment": "本轮按现有影像索引补充回顾性原因标签；既往20例未重复调用模型。",
            "rationale": old["model_rationale"],
            "evidence_quotes": load_json(old["evidence_quotes_json"], []),
            "source_round": "v2_prior",
        }))

    new_valid = []
    for row in selected:
        model = model_results.get(row["candidate_id"])
        if not model or not model.get("valid_case") or model["primary_class"] == "EXCLUDE" or model["confidence"] == "low":
            continue
        strict_valid = {
            "C1_OCCULT_METASTASIS": bool(
                row["operative_distant_signal"]
                and row["latest_preop_imaging_age_days"] is not None
                and row["latest_preop_imaging_age_days"] <= 28
                and not load_json(row["preop_strong_metastasis_json"], [])
            ),
            "C2_INCOMPLETE_STAGING": bool(
                row["operative_distant_signal"]
                and (not row["hepatic_mri_before_event"] or not row["latest_preop_imaging_date"])
            ),
            "C3_STALE_IMAGING": bool(
                row["latest_preop_imaging_age_days"] is not None
                and row["latest_preop_imaging_age_days"] > 28
            ),
            "C4_PREOP_METASTASIS_NOT_ACTED": bool(
                row["operative_distant_signal"]
                and load_json(row["preop_strong_metastasis_json"], [])
            ),
            "C5_VASCULAR_UNDERESTIMATION": bool(row["operative_vascular_signal"]),
        }[model["primary_class"]]
        if not strict_valid:
            continue
        new_valid.append((row, {**model, "source_round": "v3_new"}))
    new_valid.sort(key=lambda pair: (
        0 if pair[1]["confidence"] == "high" else 1,
        -int(pair[0].get("rule_score") or 0),
        pair[0]["candidate_id"],
    ))
    new_reason_limits = {
        "C1_OCCULT_METASTASIS": 8,
        "C2_INCOMPLETE_STAGING": 26,
        "C3_STALE_IMAGING": 17,
        "C4_PREOP_METASTASIS_NOT_ACTED": 15,
        "C5_VASCULAR_UNDERESTIMATION": 15,
    }
    new_reason_counts = Counter()
    for row, model in new_valid:
        reason = model["primary_class"]
        if (
            row["patient_uid"] in seen_uids
            or len(consolidated) >= 100
            or new_reason_counts[reason] >= new_reason_limits[reason]
        ):
            continue
        seen_uids.add(row["patient_uid"])
        new_reason_counts[reason] += 1
        consolidated.append((row, model))

    final = []
    for index, (row, model) in enumerate(consolidated, 1):
        final.append({
            "case_id": f"PAN-DECISION-{index:03d}",
            "prior_case_id": row.get("prior_case_id", ""),
            "candidate_id": row["candidate_id"],
            "patient_id": row["patient_id"],
            "patient_uid": row["patient_uid"],
            "visit_id": row["visit_id"],
            "event_date": row["event_date"],
            "disease_labels": row.get("disease_labels", ""),
            "primary_reason_code": model["primary_class"],
            "primary_reason": REASON_LABELS[model["primary_class"]],
            "secondary_reason_codes_json": json.dumps(model.get("secondary_classes") or [], ensure_ascii=False),
            "confidence": model["confidence"],
            "source_round": model["source_round"],
            "decision_gap": clean(model.get("decision_gap")),
            "operative_trigger": clean(model.get("operative_trigger")),
            "imaging_assessment": clean(model.get("imaging_assessment")),
            "latest_preop_imaging_date": row["latest_preop_imaging_date"],
            "latest_preop_imaging_method": row["latest_preop_imaging_method"],
            "latest_preop_imaging_age_days": row["latest_preop_imaging_age_days"],
            "hepatic_mri_before_event": row["hepatic_mri_before_event"],
            "latest_hepatic_mri_date": row["latest_hepatic_mri_date"],
            "planned_procedure": row.get("planned_procedure", ""),
            "actual_procedure": row.get("actual_procedure", ""),
            "nearby_pathology_date": row.get("nearby_pathology_date", ""),
            "nearby_pathology_diagnosis": row.get("nearby_pathology_diagnosis", ""),
            "preop_strong_metastasis_json": row["preop_strong_metastasis_json"],
            "preop_uncertain_metastasis_json": row["preop_uncertain_metastasis_json"],
            "operative_distant_signal": row["operative_distant_signal"],
            "operative_vascular_signal": row["operative_vascular_signal"],
            "rule_primary_reason": row["rule_primary_reason"],
            "model_rationale": clean(model.get("rationale")),
            "evidence_quotes_json": json.dumps(model.get("evidence_quotes") or [], ensure_ascii=False),
            "source_evidence_json": row.get("evidence_snippets_json", ""),
            "review_status": "READY_FOR_CLINICIAN_CONFIRMATION",
        })

    final_ids = {row["candidate_id"] for row in final}
    selected_ids = {row["candidate_id"] for row in selected}
    model_valid_ids = {row["candidate_id"] for row, _ in new_valid}
    ledger = []
    for row in features:
        if row["candidate_id"] in final_ids:
            status = "SELECTED_CONSOLIDATED"
        elif row["candidate_id"] in model_valid_ids:
            status = "VALID_DUPLICATE_OR_CAP_EXCLUDED"
        elif row["candidate_id"] in model_results:
            status = "MODEL_REVIEWED_NOT_SELECTED"
        elif row["previously_model_reviewed"]:
            status = "PRIOR_REVIEWED_NOT_SELECTED"
        elif row["candidate_id"] in selected_ids:
            status = "MODEL_ERROR_OR_PENDING"
        else:
            status = "NOT_PRIORITIZED_THIS_ROUND"
        ledger.append({
            "candidate_id": row["candidate_id"],
            "patient_uid": row["patient_uid"],
            "patient_id": row["patient_id"],
            "visit_id": row["visit_id"],
            "event_date": row["event_date"],
            "rule_score": row["rule_score"],
            "rule_primary_reason": row["rule_primary_reason"],
            "previously_model_reviewed": row["previously_model_reviewed"],
            "previously_selected": row["previously_selected"],
            "review_round": row["review_round"],
            "review_status": status,
        })

    pq.write_table(pa.Table.from_pylist(final), restricted / "shortlist.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(ledger), restricted / "review_ledger.parquet", compression="zstd")
    fields = list(final[0]) if final else []
    with (restricted / "shortlist.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(final)
    write_xlsx(restricted / "shortlist.xlsx", final, fields, "五类特殊病例")
    write_xlsx(restricted / "review_ledger.xlsx", ledger, list(ledger[0]), "检索台账")

    audit = {
        "status": "PASSED" if 50 <= len(final) <= 100 else "REVIEW_REQUIRED",
        "prompt_version": PROMPT_VERSION,
        "model": model_name,
        "guideline_rules": {
            "imaging_within_days_before_treatment": 28,
            "hepatic_mri_before_surgery": True,
            "interpretation": "Retrospective gap classification using current guideline; not proof of historical negligence.",
        },
        "candidate_episodes_total": len(features),
        "prior_model_reviewed": len(prior_reviewed),
        "prior_selected": len(prior_rows),
        "new_sent_to_model": len(selected),
        "new_model_succeeded": sum(row["candidate_id"] in model_results for row in selected),
        "new_model_errors": errors,
        "new_valid_before_dedup": len(new_valid),
        "final_unique_cases": len(final),
        "reason_counts": dict(Counter(row["primary_reason_code"] for row in final)),
        "round_counts": dict(Counter(row["source_round"] for row in final)),
        "ledger_status_counts": dict(Counter(row["review_status"] for row in ledger)),
    }
    (audit_dir / "acceptance.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

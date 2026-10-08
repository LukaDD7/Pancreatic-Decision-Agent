from __future__ import annotations

import csv
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from scripts.cohort_construction.paths import data_root
from scripts.cohort_construction.model_api import ModelAPISettings, load_model_api_settings

ROOT = data_root()
INPUT = ROOT / "pipeline_outputs_stage8_v1" / "pancreas_restage_special_cases_v2" / "restricted" / "rule_candidates.parquet"
OUTPUT_ROOT = ROOT / "pipeline_outputs_stage8_v1" / "pancreas_restage_special_cases_v2"
PROMPT_VERSION = "pancreas_special_case_adjudication_v1"

MAJOR_RESECTION = re.compile(
    r"胰十二指肠切除|Whipple|胰体尾.{0,8}切除|远端胰腺切除|"
    r"胰腺.{0,12}(?:根治|大部|次全|全).{0,8}切除|根治性.{0,8}胰腺.{0,8}切除"
)
LIMITED_PROCEDURE = re.compile(r"探查|活检|旁路|吻合|造口|神经.{0,4}离断|胰腺部分切除")
REMOTE_SITE = re.compile(r"肝|腹膜|网膜|腹腔|腹壁|肠系膜|盆腔")
MALIGNANT = re.compile(r"腺癌|转移|恶性|癌结节|癌组织")
PII_PATTERNS = (
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?:姓名|身份证号|患者编号|住院号|联系方式|现居住地址)[：:]?\s*[^，。；;\n]{2,40}"),
)


def clean(value: object) -> str:
    return str(value or "").strip()


def select_rule_shortlist(rows: list[dict]) -> list[dict]:
    selected = []
    for row in rows:
        if not row.get("structured_plan_actual_mismatch") or "PDAC" not in clean(row.get("disease_labels")):
            continue
        actual = clean(row.get("actual_procedure"))
        pathology = f"{clean(row.get('nearby_pathology_specimen'))} {clean(row.get('nearby_pathology_diagnosis'))}"
        relevant = (
            REMOTE_SITE.search(actual) and (not pathology.strip() or MALIGNANT.search(pathology))
        ) or "LOCAL_UNRESECTABLE" in clean(row.get("phenotype")) or re.search(
            r"胰腺.{0,8}(?:肿瘤|肿块).{0,8}活检", actual
        )
        if MAJOR_RESECTION.search(actual) or not LIMITED_PROCEDURE.search(actual) or not relevant:
            continue
        selected.append(row)
    return selected[:30]


def sanitize(text: str) -> str:
    value = text
    for pattern in PII_PATTERNS:
        value = pattern.sub("[已脱敏]", value)
    return value


def model_input(row: dict) -> dict:
    evidence = []
    try:
        items = json.loads(clean(row.get("evidence_snippets_json")) or "[]")
    except json.JSONDecodeError:
        items = []
    for item in items[:3]:
        evidence.append({
            "date": item.get("date"),
            "title": item.get("title"),
            "text": sanitize(clean(item.get("snippet")))[:1200],
        })
    return {
        "candidate_id": row["candidate_id"],
        "disease": row.get("disease_labels"),
        "event_date": row.get("event_date"),
        "planned_procedure": row.get("planned_procedure"),
        "actual_procedure": row.get("actual_procedure"),
        "nearby_pathology_date": row.get("nearby_pathology_date"),
        "nearby_pathology_specimen": row.get("nearby_pathology_specimen"),
        "nearby_pathology_diagnosis": sanitize(clean(row.get("nearby_pathology_diagnosis")))[:1800],
        "rule_flags": json.loads(row.get("episode_flags_json") or "[]"),
        "document_evidence": evidence,
    }


def invoke(settings: ModelAPISettings, row: dict) -> dict:
    payload = model_input(row)
    prompt = f"""你是胰腺外科病例审阅者。只根据给出的原始证据判断，不补写缺失事实。

要识别的是：术前计划根治性胰腺切除，但因为术中发现远处转移或局部不可切除，实际没有完成计划的根治切除。

分类只能选：
- occult_distant_metastasis_aborted：术中发现肝、腹膜、网膜等远处病灶，根治切除未完成；最好有病理支持。
- local_unresectability_aborted：因局部血管/邻近结构侵犯等不能完成根治切除，改为活检、旁路或有限切除。
- other_deescalation：确有计划与实际术式降级，但原因不是前两类或证据不完整。
- completed_resection_not_case：实际完成了主要胰腺根治切除，不属于目标病例。
- insufficient：材料不足以判断。

注意：未见转移是否定证据；正常逐层关腹不是终止手术；活检阴性不能当转移；计划术式和实际术式必须分开。

返回JSON，字段固定为：classification, valid_case, confidence(high|medium|low), trigger, pathology_support, curative_resection_completed(true|false|null), rationale, evidence_quotes（最多3条原文短句）。

证据：
{json.dumps(payload, ensure_ascii=False)}"""
    body = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": "严格进行临床证据归类，输出合法JSON，不作诊疗建议。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 1400,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        settings.api_url,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=90,
    )
    response.raise_for_status()
    result = response.json()
    value = json.loads(result["choices"][0]["message"]["content"])
    allowed = {
        "occult_distant_metastasis_aborted", "local_unresectability_aborted",
        "other_deescalation", "completed_resection_not_case", "insufficient",
    }
    if value.get("classification") not in allowed or value.get("confidence") not in {"high", "medium", "low"}:
        raise ValueError("invalid_model_enum")
    value["candidate_id"] = row["candidate_id"]
    usage = result.get("usage") or {}
    value["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    value["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    return value


def write_xlsx(path: Path, rows: list[dict], fields: list[str]) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "特殊病例"
    sheet.append(fields)
    for row in rows:
        sheet.append([row.get(field) for field in fields])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    workbook.save(path)


def main() -> int:
    rows = pq.read_table(INPUT).to_pylist()
    shortlist = select_rule_shortlist(rows)
    result_path = OUTPUT_ROOT / "restricted" / "model_adjudication.jsonl"
    model_results = {}
    errors = {}
    model_name = "existing_results"
    if result_path.is_file():
        for line in result_path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            model_results[value["candidate_id"]] = value
    else:
        settings = load_model_api_settings()
        model_name = settings.model
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(invoke, settings, row): row["candidate_id"] for row in shortlist}
            for future in as_completed(futures):
                candidate_id = futures[future]
                try:
                    model_results[candidate_id] = future.result()
                except Exception as exc:
                    errors[candidate_id] = f"{type(exc).__name__}:{exc}"

    final = []
    for row in shortlist:
        model = model_results.get(row["candidate_id"])
        if not model:
            continue
        if not model.get("valid_case") or model["classification"] in {"completed_resection_not_case", "insufficient"}:
            continue
        flags = json.loads(row.get("episode_flags_json") or "[]")
        pathology_text = f"{clean(row.get('nearby_pathology_specimen'))} {clean(row.get('nearby_pathology_diagnosis'))}"
        quotes_text = " ".join(clean(value) for value in (model.get("evidence_quotes") or []))
        remote_pathology_confirmed = bool(
            re.search(r"(?:肝|腹膜|网膜|腹腔|腹壁|肠系膜|盆腔).{0,80}(?:腺癌|转移癌|转移性|癌结节)", pathology_text)
            or re.search(r"(?:冰冻|快速病理|病理).{0,100}(?:腺癌|转移癌|转移性)", quotes_text)
        )
        pathology_negative_or_missing = not pathology_text or bool(
            re.search(r"(?:未见|阴性).{0,20}(?:肿瘤|癌)", pathology_text)
        )
        explicit_local_anatomy = bool(re.search(
            r"(?:SMA|SMV|肠系膜上动脉|肠系膜上静脉|腹腔干|肝总动脉).{0,80}(?:侵犯|包绕|无法.*分离)|"
            r"(?:侵犯|包绕|无法.*分离).{0,80}(?:SMA|SMV|肠系膜上动脉|肠系膜上静脉|腹腔干|肝总动脉)",
            quotes_text + " " + clean(model.get("trigger")), re.I | re.S,
        ))
        if model["classification"] == "occult_distant_metastasis_aborted" and remote_pathology_confirmed:
            evidence_grade = "A_PATHOLOGY_CONFIRMED"
            final_confidence = "high"
        elif model["classification"] == "local_unresectability_aborted" and explicit_local_anatomy:
            evidence_grade = "A_OPERATIVE_ANATOMY_EXPLICIT"
            final_confidence = "high"
        elif pathology_negative_or_missing:
            evidence_grade = "B_OPERATIVE_EXPLICIT_PATHOLOGY_MISSING_OR_NEGATIVE"
            final_confidence = "medium"
        else:
            evidence_grade = "B_MULTISOURCE_SUPPORT"
            final_confidence = "medium"
        final.append({
            "case_id": f"PAN-SPECIAL-{len(final) + 1:02d}",
            "candidate_id": row["candidate_id"],
            "patient_id": row["patient_id"],
            "patient_uid": row["patient_uid"],
            "visit_id": row["visit_id"],
            "event_date": row["event_date"],
            "disease_labels": row["disease_labels"],
            "case_class": model["classification"],
            "confidence": final_confidence,
            "evidence_grade": evidence_grade,
            "remote_pathology_confirmed": remote_pathology_confirmed,
            "neoadjuvant_or_conversion_signal": "neoadjuvant" in flags,
            "planned_procedure": row["planned_procedure"],
            "actual_procedure": row["actual_procedure"],
            "trigger": clean(model.get("trigger")),
            "pathology_support": clean(model.get("pathology_support")),
            "nearby_pathology_date": row["nearby_pathology_date"],
            "nearby_pathology_diagnosis": row["nearby_pathology_diagnosis"],
            "model_rationale": clean(model.get("rationale")),
            "evidence_quotes_json": json.dumps(model.get("evidence_quotes") or [], ensure_ascii=False),
            "source_evidence_json": row["evidence_snippets_json"],
            "rule_score": row["rule_score"],
            "review_status": "READY_FOR_CLINICIAN_CONFIRMATION",
        })

    fields = list(final[0]) if final else []
    restricted = OUTPUT_ROOT / "restricted"
    audit_dir = OUTPUT_ROOT / "audit"
    restricted.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    if final:
        pq.write_table(pa.Table.from_pylist(final), restricted / "shortlist.parquet", compression="zstd")
        with (restricted / "shortlist.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(final)
        write_xlsx(restricted / "shortlist.xlsx", final, fields)
    raw_results = [model_results[key] for key in sorted(model_results)]
    with (restricted / "model_adjudication.jsonl").open("w", encoding="utf-8") as stream:
        for value in raw_results:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    audit = {
        "status": "PASSED" if 10 <= len(final) <= 30 else "REVIEW_REQUIRED",
        "prompt_version": PROMPT_VERSION,
        "model": model_name,
        "rule_shortlist": len(shortlist),
        "model_succeeded": len(model_results),
        "model_failed": len(errors),
        "final_cases": len(final),
        "class_counts": dict(Counter(row["case_class"] for row in final)),
        "confidence_counts": dict(Counter(row["confidence"] for row in final)),
        "errors": errors,
    }
    (audit_dir / "adjudication.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

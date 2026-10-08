from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from scripts.cohort_construction.paths import data_root

ROOT = data_root()
OUTPUT = ROOT / "pipeline_outputs_stage8_v1" / "pancreas_restage_special_cases_v2"
DOCUMENT_ROOTS = (
    ("post2021", ROOT / "pipeline_outputs_stage5_v1" / "restricted" / "document_l1"),
    ("pre2021", ROOT / "pipeline_outputs_stage5_v1" / "restricted" / "legacy_xml_document_l1_v1"),
)
PATHOLOGY_RECORD = (
    ROOT / "pipeline_outputs_stage6_v2" / "restricted" / "increment" / "pathology_v1"
    / "full" / "pathology_record" / "part-00001.parquet"
)
PATHOLOGY_LINK = (
    ROOT / "pipeline_outputs_stage6_v2" / "restricted" / "increment" / "pathology_v1"
    / "full" / "record_links_pathology" / "part-00001.parquet"
)
IDENTITY_DB = ROOT / "pipeline_outputs_stage6_v2" / "restricted" / "state" / "stage6_full_state_v2.sqlite3"


PATTERNS = {
    "pancreas": re.compile(r"胰腺|胰头|胰体|胰尾|胰管|胰周|胰胆", re.I),
    "planned_resection": re.compile(r"拟行.{0,30}(?:胰十二指肠切除|Whipple|胰体尾切除|全胰切除|胰腺切除)|"
                                  r"计划.{0,30}(?:胰十二指肠切除|Whipple|胰体尾切除|全胰切除|胰腺切除)", re.I | re.S),
    "actual_resection": re.compile(r"(?:行|施行|完成)(?:了)?[^。；\n]{0,30}(?:胰十二指肠切除|Whipple|胰体尾切除|全胰切除|胰腺切除)", re.I),
    "exploration": re.compile(r"术中探查|腹腔探查|剖腹探查|探查术|进腹后|开腹后|腹腔镜探查", re.I),
    "distant_lesion": re.compile(r"肝(?:脏)?转移(?:瘤)?|腹膜转移|腹腔种植|腹膜种植|大网膜转移|"
                                 r"肠系膜转移|盆腔转移|肝(?:脏)?(?:表面)?(?:多发)?(?:结节|病灶|占位)", re.I),
    "biopsy_frozen": re.compile(r"冰冻|快速病理|术中病理|活检|取.{0,20}送病理|送.{0,20}病理", re.I | re.S),
    "malignant_result": re.compile(r"(?:冰冻|快速病理|术中病理).{0,80}(?:提示|结果|考虑|为).{0,30}"
                                   r"(?:腺癌|转移癌|恶性|癌)|(?:肝|腹膜|大网膜).{0,80}病理.{0,50}"
                                   r"(?:腺癌|转移癌|恶性|癌)", re.I | re.S),
    "abort": re.compile(r"终止手术|结束手术|放弃(?:根治性)?切除|未予(?:根治性)?切除|未行(?:根治性)?切除|"
                        r"无法切除|不宜切除|不可切除|仅行.{0,30}(?:探查|活检|旁路)|"
                        r"姑息性手术|改行.{0,30}(?:旁路|活检)", re.I | re.S),
    "local_unresectable": re.compile(
        r"(?:肿瘤|病灶).{0,80}(?:包绕|侵犯|侵及|粘连).{0,80}(?:肠系膜上动脉|SMA|腹腔干|"
        r"肝总动脉|门静脉|肠系膜上静脉|SMV|PV)|"
        r"(?:肠系膜上动脉|SMA|腹腔干|肝总动脉|门静脉|肠系膜上静脉|SMV|PV).{0,80}"
        r"(?:包绕|侵犯|侵及|无法分离|不能分离)", re.I | re.S,
    ),
    "neoadjuvant": re.compile(r"新辅助|转化治疗|转化化疗|FOLFIRINOX|mFOLFIRINOX|AG方案|"
                              r"白蛋白紫杉醇.{0,20}吉西他滨|吉西他滨.{0,20}白蛋白紫杉醇", re.I | re.S),
    "restaging": re.compile(r"重新分期|再分期|疗效评估|评估疗效|化疗后复查|治疗后复查|"
                            r"复查.{0,30}(?:增强CT|CT|MRI|磁共振)|MDT|多学科", re.I | re.S),
    "progression": re.compile(r"疾病进展|肿瘤进展|较前增大|新发.{0,30}(?:转移|结节|病灶)|"
                              r"新增.{0,30}(?:转移|结节|病灶)|远处转移|失去手术机会|取消手术", re.I | re.S),
}

PREFILTER = re.compile(
    r"探查|冰冻|快速病理|活检|肝转移|腹膜转移|腹腔种植|不可切除|无法切除|未行切除|"
    r"放弃切除|终止手术|新辅助|转化治疗|FOLFIRINOX|AG方案|重新分期|再分期|疗效评估|MDT",
    re.I,
)

NEGATION_RE = re.compile(r"(?:未见|未发现|未查见|无明显|无|否认|排除).{0,16}$", re.I | re.S)
PROCEDURE_FIELDS_RE = re.compile(
    r"拟实施手术(?:名称)?[：:]\s*(?P<planned>.{1,160}?)\s*实施手术(?:名称)?[：:]\s*"
    r"(?P<actual>.{1,180}?)(?:手术人员|手术医生|术者|麻醉方式|麻醉人员)[：:]", re.I | re.S,
)
RESECTION_RE = re.compile(r"胰十二指肠切除|Whipple|胰体尾(?:脾脏)?切除|胰体尾切除|全胰切除|"
                         r"胰腺根治性?次全切除|胰腺切除|胰头切除", re.I)
LIMITED_PROCEDURE_RE = re.compile(r"探查|活检|旁路|引流", re.I)


def clean(value: object) -> str:
    return str(value or "").strip()


def normalize_id(value: object) -> str:
    return re.sub(r"\s+", "", clean(value).upper())


def iso_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    match = re.match(r"^(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})", clean(value))
    if not match:
        return ""
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return ""


def decode_content(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("gb18030", "utf-8"):
        try:
            return bytes(value).decode(encoding)
        except UnicodeDecodeError:
            continue
    return bytes(value).decode("gb18030", errors="replace")


def evidence_snippet(text: str, matched: set[str], radius: int = 150) -> str:
    positions = []
    for name in matched:
        found = PATTERNS[name].search(text)
        if found:
            positions.append((found.start(), found.end()))
    if not positions:
        return text[:500]
    snippets = []
    for start, end in sorted(positions)[:5]:
        snippet = re.sub(r"\s+", " ", text[max(0, start - radius): min(len(text), end + radius)]).strip()
        if snippet and snippet not in snippets:
            snippets.append(snippet)
    return " …… ".join(snippets)[:1800]


def has_positive_match(name: str, text: str) -> bool:
    pattern = PATTERNS[name]
    if name not in {"distant_lesion", "malignant_result", "abort", "progression"}:
        return bool(pattern.search(text))
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 24):match.start()]
        if not NEGATION_RE.search(prefix):
            return True
    return False


def procedure_fields(text: str) -> tuple[str, str]:
    match = PROCEDURE_FIELDS_RE.search(text)
    if not match:
        return "", ""
    planned = re.sub(r"\s+", " ", match.group("planned")).strip(" ：:；;")
    actual = re.sub(r"\s+", " ", match.group("actual")).strip(" ：:；;")
    return planned[:300], actual[:300]


def load_pathology() -> tuple[dict[str, set[str]], dict[str, list[dict[str, str]]]]:
    links = {
        row["source_record_key"]: row["patient_uid"]
        for row in pq.read_table(PATHOLOGY_LINK).to_pylist()
        if row.get("disposition_class") == "hard" and row.get("event_eligible") and row.get("patient_uid")
    }
    labels: dict[str, set[str]] = defaultdict(set)
    events: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in pq.read_table(PATHOLOGY_RECORD).to_pylist():
        uid = links.get(row["source_record_key"])
        if not uid:
            continue
        label = clean(row.get("disease_label"))
        if label:
            labels[uid].add(label)
        event_date = next((iso_date(row.get(key)) for key in (
            "match_date_parsed", "specimen_date_parsed", "received_date_parsed", "report_date_parsed"
        ) if iso_date(row.get(key))), "")
        diagnosis = clean(row.get("病理诊断"))
        specimen = clean(row.get("标本名称"))
        combined = " ".join((specimen, diagnosis, clean(row.get("肉眼所见")), clean(row.get("镜下所见"))))
        events[uid].append({
            "event_date": event_date,
            "specimen": specimen,
            "diagnosis": diagnosis,
            "source_record_key": row["source_record_key"],
            "distant_specimen": str(bool(re.search(r"肝|腹膜|大网膜|肠系膜|盆腔", combined))),
            "malignant": str(bool(re.search(r"腺癌|转移癌|恶性|癌", diagnosis))),
        })
    return labels, events


def load_aliases(wanted_uids: set[str]) -> dict[str, str]:
    connection = sqlite3.connect(f"file:{IDENTITY_DB}?mode=ro", uri=True)
    grouped: dict[str, set[str]] = defaultdict(set)
    for value, uid in connection.execute(
        "SELECT normalized_value,patient_uid FROM identity_alias WHERE id_type='PATIENT_ID'"
    ):
        if uid in wanted_uids:
            grouped[normalize_id(value)].add(uid)
    connection.close()
    return {value: next(iter(uids)) for value, uids in grouped.items() if len(uids) == 1}


def nearest_pathology(events: list[dict[str, str]], event_date: str) -> tuple[dict[str, str] | None, int | None]:
    if not event_date:
        return None, None
    target = date.fromisoformat(event_date)
    values = []
    for row in events:
        if not row["event_date"]:
            continue
        gap = (date.fromisoformat(row["event_date"]) - target).days
        if -3 <= gap <= 21 and (row["distant_specimen"] == "True" or row["malignant"] == "True"):
            values.append((abs(gap), gap, row))
    if not values:
        return None, None
    _, gap, row = min(values, key=lambda item: (item[0], item[1], item[2]["source_record_key"]))
    return row, gap


def scan_documents(aliases: dict[str, str]) -> list[dict[str, object]]:
    result = []
    columns = ["PATIENT_ID", "VISIT_ID", "文书名称", "create_time", "文书内容", "source_file", "source_row", "source_record_id"]
    for source_period, root in DOCUMENT_ROOTS:
        dataset = ds.dataset(root, format="parquet")
        for batch in dataset.to_batches(columns=columns, batch_size=1024):
            for row in batch.to_pylist():
                patient_id = normalize_id(row.get("PATIENT_ID"))
                uid = aliases.get(patient_id)
                if not uid:
                    continue
                title = clean(row.get("文书名称"))
                content = decode_content(row.get("文书内容"))
                text = f"{title}\n{content}"
                if not PREFILTER.search(text) or not PATTERNS["pancreas"].search(text):
                    continue
                matched = {name for name in PATTERNS if name != "pancreas" and has_positive_match(name, text)}
                planned_procedure, actual_procedure = procedure_fields(text)
                if planned_procedure or actual_procedure:
                    matched.discard("planned_resection")
                    matched.discard("actual_resection")
                if planned_procedure and RESECTION_RE.search(planned_procedure):
                    matched.add("planned_resection")
                if actual_procedure and RESECTION_RE.search(actual_procedure):
                    matched.add("actual_resection")
                high_value = matched & {
                    "exploration", "distant_lesion", "biopsy_frozen", "malignant_result", "abort",
                    "local_unresectable", "neoadjuvant", "restaging", "progression",
                }
                if not high_value:
                    continue
                result.append({
                    "patient_uid": uid,
                    "patient_id": patient_id,
                    "visit_id": normalize_id(row.get("VISIT_ID")),
                    "event_date": iso_date(row.get("create_time")),
                    "document_title": title,
                    "source_period": source_period,
                    "source_file": clean(row.get("source_file")),
                    "source_row": int(row.get("source_row") or 0),
                    "source_record_id": clean(row.get("source_record_id")),
                    "planned_procedure": planned_procedure,
                    "actual_procedure": actual_procedure,
                    "matched_flags": sorted(matched),
                    "evidence_snippet": evidence_snippet(text, matched),
                })
    return result


def score_episode(rows: list[dict[str, object]], pathology: dict[str, list[dict[str, str]]]) -> dict[str, object] | None:
    flags = set()
    for row in rows:
        flags.update(row["matched_flags"])
    surgery_flags = {"exploration", "distant_lesion", "biopsy_frozen", "malignant_result", "abort", "local_unresectable"}
    if len(flags & surgery_flags) < 2:
        return None
    structured_mismatch_rows = [
        row for row in rows
        if row.get("planned_procedure") and row.get("actual_procedure")
        and RESECTION_RE.search(clean(row["planned_procedure"]))
        and not RESECTION_RE.search(clean(row["actual_procedure"]))
        and LIMITED_PROCEDURE_RE.search(clean(row["actual_procedure"]))
    ]
    dates = sorted(row["event_date"] for row in rows if row["event_date"])
    event_date = clean(structured_mismatch_rows[0]["event_date"]) if structured_mismatch_rows else (dates[0] if dates else "")
    score = 0
    weights = {
        "planned_resection": 2, "exploration": 2, "distant_lesion": 3,
        "biopsy_frozen": 3, "malignant_result": 3, "abort": 4,
        "local_unresectable": 4, "actual_resection": -2,
    }
    for name, weight in weights.items():
        if name in flags:
            score += weight
    same_doc_strong = any(
        ({"distant_lesion", "biopsy_frozen"} <= set(row["matched_flags"]))
        or ({"local_unresectable", "abort"} <= set(row["matched_flags"]))
        or ({"distant_lesion", "abort"} <= set(row["matched_flags"]))
        for row in rows
    )
    if same_doc_strong:
        score += 4
    if structured_mismatch_rows:
        score += 10
    nearest, pathology_gap = nearest_pathology(pathology, event_date)
    if nearest:
        score += 4
        if nearest["distant_specimen"] == "True" and nearest["malignant"] == "True":
            score += 3
    if structured_mismatch_rows and "distant_lesion" in flags:
        phenotype = "INTRAOPERATIVE_SUSPECTED_METASTASIS"
    elif {"distant_lesion", "biopsy_frozen"} <= flags and ({"abort", "malignant_result"} & flags):
        phenotype = "INTRAOPERATIVE_SUSPECTED_METASTASIS"
    elif {"local_unresectable", "abort"} <= flags:
        phenotype = "INTRAOPERATIVE_LOCAL_UNRESECTABLE"
    elif {"exploration", "abort"} <= flags:
        phenotype = "ABORTED_OR_LIMITED_EXPLORATION"
    else:
        phenotype = "SURGICAL_RESTAGING_REVIEW"
    if score < 11:
        return None
    if phenotype == "INTRAOPERATIVE_SUSPECTED_METASTASIS" and not structured_mismatch_rows:
        if not any({"distant_lesion", "biopsy_frozen"} <= set(row["matched_flags"]) for row in rows):
            return None
    if "actual_resection" in flags and not structured_mismatch_rows and "abort" not in flags:
        return None
    best_rows = sorted(rows, key=lambda row: (
        -len(set(row["matched_flags"]) & surgery_flags), row["event_date"], row["source_record_id"]
    ))[:5]
    return {
        "patient_uid": rows[0]["patient_uid"],
        "patient_id": rows[0]["patient_id"],
        "visit_id": rows[0]["visit_id"],
        "event_date": event_date,
        "phenotype": phenotype,
        "rule_score": score,
        "episode_flags_json": json.dumps(sorted(flags), ensure_ascii=False),
        "evidence_document_count": len(rows),
        "structured_plan_actual_mismatch": bool(structured_mismatch_rows),
        "planned_procedure": clean(structured_mismatch_rows[0]["planned_procedure"]) if structured_mismatch_rows else "",
        "actual_procedure": clean(structured_mismatch_rows[0]["actual_procedure"]) if structured_mismatch_rows else "",
        "evidence_titles_json": json.dumps(sorted({clean(row["document_title"]) for row in rows}), ensure_ascii=False),
        "evidence_snippets_json": json.dumps([
            {"date": row["event_date"], "title": row["document_title"], "flags": row["matched_flags"],
             "snippet": row["evidence_snippet"], "source_file": row["source_file"],
             "source_row": row["source_row"], "source_record_id": row["source_record_id"],
             "planned_procedure": row.get("planned_procedure", ""),
             "actual_procedure": row.get("actual_procedure", "")}
            for row in best_rows
        ], ensure_ascii=False),
        "nearby_pathology_date": nearest["event_date"] if nearest else "",
        "nearby_pathology_gap_days": pathology_gap,
        "nearby_pathology_specimen": nearest["specimen"] if nearest else "",
        "nearby_pathology_diagnosis": nearest["diagnosis"] if nearest else "",
        "nearby_pathology_source_record_key": nearest["source_record_key"] if nearest else "",
    }


def build_candidates(document_rows: list[dict[str, object]], labels: dict[str, set[str]], pathology: dict[str, list[dict[str, str]]]) -> list[dict[str, object]]:
    by_episode: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in document_rows:
        episode = row["visit_id"] or f"date:{row['event_date']}"
        by_episode[(row["patient_uid"], episode)].append(row)
    candidates = []
    for (uid, _), rows in by_episode.items():
        candidate = score_episode(rows, pathology.get(uid, []))
        if candidate:
            candidate["disease_labels"] = ";".join(sorted(labels.get(uid, set())))
            candidates.append(candidate)
    candidates.sort(key=lambda row: (-int(row["rule_score"]), row["event_date"], row["patient_uid"], row["visit_id"]))
    for index, row in enumerate(candidates, 1):
        row["candidate_id"] = f"PAN-RS-{index:04d}"
    return candidates


def main() -> int:
    if OUTPUT.exists():
        raise RuntimeError(f"output_exists:{OUTPUT}")
    labels, pathology = load_pathology()
    aliases = load_aliases(set(labels))
    documents = scan_documents(aliases)
    candidates = build_candidates(documents, labels, pathology)
    restricted = OUTPUT / "restricted"
    audit_dir = OUTPUT / "audit"
    restricted.mkdir(parents=True)
    audit_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(candidates), restricted / "rule_candidates.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(documents), restricted / "matched_documents.parquet", compression="zstd")
    audit = {
        "status": "RULE_CANDIDATES_READY",
        "pathology_linked_patients": len(labels),
        "unique_patient_id_aliases": len(aliases),
        "matched_documents": len(documents),
        "candidate_episodes": len(candidates),
        "candidate_patients": len({row["patient_uid"] for row in candidates}),
        "score_ge_12": sum(int(row["rule_score"]) >= 12 for row in candidates),
        "score_ge_16": sum(int(row["rule_score"]) >= 16 for row in candidates),
        "phenotypes": dict(sorted({name: sum(row["phenotype"] == name for row in candidates) for name in {row["phenotype"] for row in candidates}}.items())),
    }
    (audit_dir / "rule_retrieval.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

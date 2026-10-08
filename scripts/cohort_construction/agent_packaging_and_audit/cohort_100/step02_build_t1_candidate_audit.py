from __future__ import annotations

import argparse
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
from scripts.cohort_construction.agent_packaging_and_audit.cohort_100 import (
    step01_build_patient_states_100 as state_builder,
)


PROJECT_ROOT = data_root() / "pipeline_outputs_stage8_v1" / "pancreas_decision_window_enriched_v1"
STATE_OUTPUT = PROJECT_ROOT / "restricted" / "patient_state_100_v1" / "outputs" / "state_100_20261003"
DEFAULT_OUT = PROJECT_ROOT / "restricted" / "t1_candidate_audit_v1" / "outputs" / "t1_candidates_20261005"
WINDOW_DAYS = 90

DOCUMENT_TITLE = re.compile(
    r"MDT|多学科|会诊|讨论|病程|查房|出院|手术|术后|穿刺|活检|病理|EUS|FNA|CT|MR|PET|探查",
    re.I,
)
MDT_PATTERN = re.compile(r"MDT|多学科", re.I)
BIOPSY_PATTERN = re.compile(r"EUS\s*[-+／/]?\s*FNA|FNA|细针穿刺|穿刺活检|活检|穿刺病理", re.I)
BIOPSY_ACTUAL_PATTERN = re.compile(
    r"(?:今日|已|术中|在.{0,15}下|于\d{4}[-年].{0,20})?行[^。；\n]{0,60}(?:穿刺活检|活检|FNA)|"
    r"(?:穿刺活检|活检|FNA)(?:术后|病理结果|病理诊断|结果|证实)",
    re.I,
)
STAGING_LAP_PATTERN = re.compile(r"分期腹腔镜|腹腔镜.{0,30}(?:探查|探查术)|(?:探查|探查术).{0,30}腹腔镜", re.I | re.S)
OPEN_EXPLORATION_PATTERN = re.compile(r"剖腹探查|开腹探查", re.I)
RESULT_CONTEXT = re.compile(r"术中|探查见|手术经过|实施手术|术后|病理|结果|提示|证实|发现", re.I)
PLAN_ONLY_TITLE = re.compile(r"知情同意|术前讨论|术前小结|申请单|告知", re.I)
BIOPSY_RESULT_TITLE = re.compile(r"EUS|FNA|穿刺|活检|手术记录|术后首次病程|出院小结|出院记录|病理", re.I)
PATH_BIOPSY_PATTERN = re.compile(r"穿刺|活检|细针|FNA|EUS", re.I)
PATH_TARGET_PATTERN = re.compile(r"肝|腹膜|网膜|淋巴结|远处|转移", re.I)
PET_PATTERN = re.compile(r"PET\s*[-/]?\s*CT|PET/CT|正电子", re.I)
PET_RESULT_PATTERN = re.compile(r"示|提示|FDG|SUV|代谢|摄取|检查", re.I)
PET_PLAN_PATTERN = re.compile(r"可行|建议|拟行|有助于|有助", re.I)
DATE_PATTERN = re.compile(r"(?P<year>20\d{2})[-/.年]?(?P<month>1[0-2]|0?[1-9])[-/.月]?(?P<day>3[01]|[12]\d|0?[1-9])日?(?!\d)")


def clean(value: Any) -> str:
    return state_builder.clean(value)


def iso(value: datetime | None) -> str | None:
    return state_builder.iso(value)


def event_time(row: dict[str, Any], fields: list[str]) -> tuple[datetime | None, str | None]:
    for field in fields:
        parsed = state_builder.parse_dt(row.get(field))
        if parsed is not None:
            return parsed, field
    return None, None


def in_t1_window(value: datetime | None, t0: datetime) -> bool:
    return value is not None and t0 < value <= t0 + timedelta(days=WINDOW_DAYS)


def excerpt(text: str, pattern: re.Pattern[str] | None = None, limit: int = 700) -> str:
    text = clean(text)
    if not text:
        return ""
    if pattern:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 180)
            return text[start : start + limit].replace("\r", " ").replace("\n", " ")
    return text[:limit].replace("\r", " ").replace("\n", " ")


def nearest_embedded_date(text: str, position: int, end_position: int) -> datetime | None:
    matches = list(DATE_PATTERN.finditer(text[max(0, position - 80) : end_position + 100]))
    local_start = min(80, position)
    local_end = local_start + (end_position - position)
    associated = []
    for match in matches:
        if match.end() <= local_start and local_start - match.end() <= 25:
            between = text[max(0, position - 80) : end_position + 100][match.end() : local_start]
            if re.fullmatch(r"[\s,，:：()（）我院行检查]*", between):
                associated.append(match)
        elif match.start() >= local_end and match.start() - local_end <= 50:
            between = text[max(0, position - 80) : end_position + 100][local_end : match.start()]
            if not re.search(r"[\u4e00-\u9fff]", between):
                associated.append(match)
    if not associated:
        return None
    match = min(associated, key=lambda item: min(abs(item.start() - local_start), abs(item.end() - local_end)))
    try:
        return datetime(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None


def imaging_subtype(method: str, text: str) -> str:
    value = f"{method} {text}"
    if re.search(r"PET\s*[-/]?\s*CT|正电子", value, re.I):
        return "PET_CT"
    if re.search(r"MR|MRI|磁共振", value, re.I):
        return "MR"
    if re.search(r"CT|计算机断层", value, re.I):
        return "CT"
    return "other_imaging"


def load_pathology_details_by_uid(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_by_uid = {clean(row.get("pathology_record_uid")): row for row in events}
    needed = set(event_by_uid)
    if not needed:
        return []
    columns = [
        "pathology_record_uid",
        "source_record_key",
        "标本类型",
        "标本名称",
        "临床诊断",
        "病理诊断",
        "镜下所见",
    ]
    table = state_builder.read_pathology_table(columns)
    filtered = table.filter(pc.is_in(table["pathology_record_uid"], value_set=pa.array(sorted(needed))))
    output = []
    for row in filtered.to_pylist():
        event = event_by_uid.get(clean(row.get("pathology_record_uid")))
        if not event:
            continue
        item = dict(row)
        item.update(
            {
                "patient_uid": event["patient_uid"],
                "_available_dt": event["_available_dt"],
                "_time_basis": event["_time_basis"],
            }
        )
        output.append(item)
    return output


def add_candidate(
    output: list[dict[str, Any]],
    case_id: str,
    patient_uid: str,
    t0: datetime,
    t0_basis: str,
    observed_at: datetime,
    observed_at_basis: str,
    event_class: str,
    event_subtype: str,
    source_type: str,
    source_id: str,
    source_label: str,
    raw_text: str,
    target_candidate: bool = True,
    review_priority: str = "standard",
) -> None:
    output.append(
        {
            "case_id": case_id,
            "patient_uid": patient_uid,
            "T0": iso(t0),
            "T0_time_basis": t0_basis,
            "observed_at": iso(observed_at),
            "observed_at_basis": observed_at_basis,
            "days_after_T0": round((observed_at - t0).total_seconds() / 86400, 3),
            "event_class": event_class,
            "event_subtype": event_subtype,
            "source_type": source_type,
            "source_id": source_id,
            "source_label": source_label,
            "evidence_excerpt": raw_text,
            "target_candidate": target_candidate,
            "review_priority": review_priority,
        }
    )


def build_candidates() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    cohort, imaging, id_to_uid = state_builder.load_cohort_and_imaging()
    states = [json.loads(line) for line in (STATE_OUTPUT / "patient_states_100_pre_t0.jsonl").read_text(encoding="utf-8").splitlines() if line]
    state_by_case = {clean(row["case_id"]): row for row in states}
    t0_by_uid = {clean(row["patient_uid"]): state_builder.parse_dt(row["decision_timepoint"]) for row in states}
    basis_by_uid = {clean(row["patient_uid"]): clean(row["decision_timepoint_basis"]) for row in states}
    case_by_uid = {clean(row["patient_uid"]): clean(row["case_id"]) for row in states}

    documents, _, pathology_events = state_builder.load_timeline_rows(set(t0_by_uid))
    post_documents: list[dict[str, Any]] = []
    for row in documents:
        uid = clean(row.get("patient_uid"))
        dt, basis = event_time(row, ["create_time", "event_time_used"])
        if uid in t0_by_uid and in_t1_window(dt, t0_by_uid[uid]) and DOCUMENT_TITLE.search(clean(row.get("document_type"))):
            item = dict(row)
            item["_available_dt"] = dt
            item["_time_basis"] = basis
            post_documents.append(item)
    document_text = state_builder.retrieve_document_text(post_documents)

    candidates: list[dict[str, Any]] = []
    pet_mentions: list[dict[str, Any]] = []
    excluded_counts: Counter[str] = Counter()

    for row in imaging.to_dict("records"):
        case_id = clean(row.get("patient_id"))
        uid = id_to_uid.get(case_id)
        if not uid or uid not in t0_by_uid:
            continue
        dt = state_builder.parse_dt(row.get("exam_datetime"))
        if not in_t1_window(dt, t0_by_uid[uid]):
            continue
        text = state_builder.imaging_text(row)
        method = clean(row.get("exam_method"))
        subtype = imaging_subtype(method, text)
        add_candidate(
            candidates,
            case_id,
            uid,
            t0_by_uid[uid],
            basis_by_uid[uid],
            dt,
            "exam_datetime",
            "imaging",
            subtype,
            "imaging",
            f"IMG:{clean(row.get('index_report_uid'))}",
            method,
            excerpt(text),
            review_priority="high" if subtype in {"MR", "PET_CT"} else "standard",
        )

    seen_document_events: set[tuple[str, str]] = set()
    seen_verified_pet: set[tuple[str, str]] = set()
    for row in post_documents:
        uid = clean(row.get("patient_uid"))
        case_id = case_by_uid[uid]
        title = clean(row.get("document_type"))
        text = document_text.get(clean(row.get("source_record_key")), "")
        combined = f"{title}\n{text}"
        dt = row["_available_dt"]
        source_id = f"DOC:{clean(row.get('source_record_key'))[:16]}"
        pet_match = PET_PATTERN.search(text)
        if pet_match:
            context = text[max(0, pet_match.start() - 180) : pet_match.start() + 520]
            embedded_date = nearest_embedded_date(text, pet_match.start(), pet_match.end())
            result_like = bool(PET_RESULT_PATTERN.search(context))
            plan_like = bool(PET_PLAN_PATTERN.search(context))
            if plan_like and embedded_date is None:
                pet_status = "recommendation_or_generic_mention"
            elif embedded_date and embedded_date.date() <= t0_by_uid[uid].date():
                pet_status = "historical_or_pre_T0"
            elif embedded_date and embedded_date.date() <= (t0_by_uid[uid] + timedelta(days=WINDOW_DAYS)).date() and result_like:
                pet_status = "verified_post_T0_result_mention"
            elif embedded_date:
                pet_status = "dated_outside_T1_window"
            else:
                pet_status = "undated_result_or_history_unresolved"
            pet_mentions.append(
                {
                    "case_id": case_id,
                    "patient_uid": uid,
                    "T0": iso(t0_by_uid[uid]),
                    "document_observed_at": iso(dt),
                    "document_time_basis": clean(row.get("_time_basis")),
                    "embedded_PET_date": iso(embedded_date),
                    "PET_mention_status": pet_status,
                    "source_id": source_id,
                    "source_label": title,
                    "evidence_excerpt": excerpt(text, PET_PATTERN),
                }
            )
            if pet_status == "verified_post_T0_result_mention":
                pet_key = (case_id, embedded_date.date().isoformat())
                if pet_key not in seen_verified_pet:
                    seen_verified_pet.add(pet_key)
                    add_candidate(
                        candidates,
                        case_id,
                        uid,
                        t0_by_uid[uid],
                        basis_by_uid[uid],
                        embedded_date,
                        "document_embedded_date_only",
                        "imaging_document",
                        "PET_CT",
                        "document",
                        source_id,
                        title,
                        excerpt(text, PET_PATTERN),
                        review_priority="high",
                    )
        matches: list[tuple[str, str, re.Pattern[str], str]] = []
        if MDT_PATTERN.search(combined):
            matches.append(("MDT", "MDT", MDT_PATTERN, "high"))
        if (
            BIOPSY_RESULT_TITLE.search(title)
            and BIOPSY_PATTERN.search(combined)
            and BIOPSY_ACTUAL_PATTERN.search(combined)
            and not PLAN_ONLY_TITLE.search(title)
        ):
            matches.append(("biopsy", "biopsy_or_FNA_document", BIOPSY_PATTERN, "high"))
        if STAGING_LAP_PATTERN.search(combined) and RESULT_CONTEXT.search(combined) and not PLAN_ONLY_TITLE.search(title):
            matches.append(("staging_laparoscopy", "staging_laparoscopy_result", STAGING_LAP_PATTERN, "high"))
        elif OPEN_EXPLORATION_PATTERN.search(combined) and RESULT_CONTEXT.search(combined) and not PLAN_ONLY_TITLE.search(title):
            matches.append(("surgical_exploration", "open_exploration_result", OPEN_EXPLORATION_PATTERN, "standard"))
        for event_class, subtype, pattern, priority in matches:
            key = (source_id, event_class)
            if key in seen_document_events:
                continue
            seen_document_events.add(key)
            add_candidate(
                candidates,
                case_id,
                uid,
                t0_by_uid[uid],
                basis_by_uid[uid],
                dt,
                clean(row.get("_time_basis")) or "document_time",
                event_class,
                subtype,
                "document",
                source_id,
                title,
                excerpt(text, pattern),
                review_priority=priority,
            )

    post_pathology_events: list[dict[str, Any]] = []
    for row in pathology_events:
        uid = clean(row.get("patient_uid"))
        dt, basis = event_time(row, ["report_time", "event_time_used"])
        if uid in t0_by_uid and in_t1_window(dt, t0_by_uid[uid]):
            item = dict(row)
            item["_available_dt"] = dt
            item["_time_basis"] = basis
            post_pathology_events.append(item)
    pathology_details = load_pathology_details_by_uid(post_pathology_events)
    for row in pathology_details:
        uid = clean(row.get("patient_uid"))
        case_id = case_by_uid[uid]
        fields = ["标本类型", "标本名称", "临床诊断", "病理诊断", "镜下所见"]
        text = "；".join(clean(row.get(field)) for field in fields if clean(row.get(field)))
        specimen_type = clean(row.get("标本类型"))
        explicit_biopsy = bool(PATH_BIOPSY_PATTERN.search(text))
        small_specimen = "小标本" in specimen_type
        if not explicit_biopsy and not small_specimen:
            excluded_counts["post_T0_pathology_large_or_resection_specimen"] += 1
            continue
        if PATH_TARGET_PATTERN.search(text):
            subtype = "distant_site_small_specimen_pathology"
        elif explicit_biopsy:
            subtype = "explicit_biopsy_pathology"
        else:
            subtype = "small_specimen_pathology_candidate"
        add_candidate(
            candidates,
            case_id,
            uid,
            t0_by_uid[uid],
            basis_by_uid[uid],
            row["_available_dt"],
            clean(row.get("_time_basis")) or "pathology_time",
            "pathology_candidate",
            subtype,
            "pathology",
            f"PATH:{clean(row.get('pathology_record_uid'))}",
            clean(row.get("标本名称") or row.get("标本类型")),
            excerpt(text, PATH_BIOPSY_PATTERN),
            review_priority="high",
        )

    candidate_df = pd.DataFrame(candidates)
    if not candidate_df.empty:
        candidate_df = candidate_df.sort_values(["case_id", "observed_at", "event_class", "source_id"]).reset_index(drop=True)
        candidate_df["candidate_rank_within_case"] = candidate_df.groupby("case_id").cumcount() + 1
        candidate_df["is_earliest_observed_candidate"] = candidate_df["candidate_rank_within_case"].eq(1)
        candidate_df["candidate_event_id"] = [f"T1C-{index + 1:04d}" for index in range(len(candidate_df))]

    strata = pd.read_csv(STATE_OUTPUT / "analysis_strata_100.csv", dtype=str).fillna("")
    strata_case_column = "患者ID" if "患者ID" in strata.columns else strata.columns[0]
    strata["_case_id"] = strata[strata_case_column].map(clean)
    strata_lookup = strata.set_index("_case_id").to_dict("index")
    cohort_lookup = {clean(row["患者ID"]): row.to_dict() for _, row in cohort.iterrows()}
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidate_df.to_dict("records"):
        by_case[clean(row["case_id"])].append(row)

    case_rows: list[dict[str, Any]] = []
    for case_id, state in sorted(state_by_case.items()):
        rows = by_case.get(case_id, [])
        counts = Counter(clean(row["event_subtype"]) for row in rows)
        classes = Counter(clean(row["event_class"]) for row in rows)
        first = rows[0] if rows else {}
        cohort_row = cohort_lookup[case_id]
        stratum = strata_lookup.get(case_id, {})
        case_rows.append(
            {
                "case_id": case_id,
                "patient_uid": clean(state["patient_uid"]),
                "T0": clean(state["decision_timepoint"]),
                "T0_time_basis": clean(state["decision_timepoint_basis"]),
                "disease_stratum": clean(stratum.get("分析病种分层") or stratum.get("分析病种") or stratum.get("病种") or stratum.get("disease_stratum")),
                "observed_pathway": clean(cohort_row.get("四类路径")),
                "candidate_count": len(rows),
                "has_any_T1_candidate": bool(rows),
                "has_MR": counts["MR"] > 0,
                "has_PET_CT": counts["PET_CT"] > 0,
                "has_repeat_CT": counts["CT"] > 0,
                "has_MDT": classes["MDT"] > 0,
                "has_biopsy_document": classes["biopsy"] > 0,
                "has_small_specimen_pathology": classes["pathology_candidate"] > 0,
                "has_biopsy_or_small_specimen_candidate": classes["biopsy"] > 0 or classes["pathology_candidate"] > 0,
                "has_staging_laparoscopy": classes["staging_laparoscopy"] > 0,
                "has_open_exploration": classes["surgical_exploration"] > 0,
                "earliest_candidate_at": clean(first.get("observed_at")),
                "earliest_candidate_type": clean(first.get("event_subtype")),
                "earliest_candidate_source_id": clean(first.get("source_id")),
                "earliest_candidate_excerpt": clean(first.get("evidence_excerpt")),
                "proposed_for_manual_review": bool(rows),
                "新增资料摘要_待审": "",
                "对应T0关键未知项_待专家": "",
                "关键未知是否解决_待审": "",
                "M状态是否改变_待审": "",
                "可切除性是否改变_待审": "",
                "治疗路径是否改变_待审": "",
                "是否仍不确定_待审": "",
                "是否具有决策信息增益_待审": "",
                "审计备注": "",
            }
        )
    case_df = pd.DataFrame(case_rows)
    evidence_rich = (
        case_df["has_MDT"]
        | case_df["has_biopsy_document"]
        | case_df["has_small_specimen_pathology"]
        | case_df["has_staging_laparoscopy"]
        | case_df["has_open_exploration"]
    )
    exact_t0 = case_df["T0_time_basis"].eq("exam_datetime_proxy")
    case_df["screening_tier"] = "C_no_candidate_or_date_only_T0"
    case_df.loc[exact_t0 & case_df["has_any_T1_candidate"], "screening_tier"] = "B_exact_T0_imaging_candidate"
    case_df.loc[exact_t0 & evidence_rich, "screening_tier"] = "A_exact_T0_potentially_decision_changing_evidence"

    exact_t0_cases = set(case_df.loc[case_df["T0_time_basis"].eq("exam_datetime_proxy"), "case_id"])
    exact_candidate_cases = set(case_df.loc[case_df["has_any_T1_candidate"], "case_id"]) & exact_t0_cases
    metrics = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "window_policy": "strictly after T0 through T0+90 days; observed tests are evidence events, not best-test labels",
        "case_count": len(case_df),
        "candidate_event_count": len(candidate_df),
        "cases_with_any_candidate": int(case_df["has_any_T1_candidate"].sum()),
        "cases_without_candidate": int((~case_df["has_any_T1_candidate"]).sum()),
        "cases_with_exact_T0_and_candidate": len(exact_candidate_cases),
        "cases_by_screening_tier": dict(Counter(case_df["screening_tier"])),
        "cases_by_candidate_class": {key: len(set(candidate_df.loc[candidate_df["event_class"].eq(key), "case_id"])) for key in sorted(candidate_df["event_class"].unique())},
        "events_by_subtype": dict(Counter(candidate_df["event_subtype"])),
        "post_T0_pathology_events_found": len(post_pathology_events),
        "post_T0_pathology_details_matched_by_pathology_uid": len(pathology_details),
        "PET_CT_document_mention_count": len(pet_mentions),
        "PET_CT_document_mention_cases": len({row["case_id"] for row in pet_mentions}),
        "PET_CT_mentions_by_status": dict(Counter(row["PET_mention_status"] for row in pet_mentions)),
        "excluded": dict(excluded_counts),
        "manual_adjudication_fields_left_blank": [
            "新增资料摘要_待审",
            "对应T0关键未知项_待专家",
            "关键未知是否解决_待审",
            "M状态是否改变_待审",
            "可切除性是否改变_待审",
            "治疗路径是否改变_待审",
            "是否仍不确定_待审",
            "是否具有决策信息增益_待审",
        ],
    }
    pet_df = pd.DataFrame(pet_mentions)
    if not pet_df.empty:
        pet_df = pet_df.sort_values(["case_id", "document_observed_at", "source_id"]).reset_index(drop=True)
    return candidate_df, case_df, pet_df, metrics


def write_outputs(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_df, case_df, pet_df, metrics = build_candidates()
    candidate_df.to_csv(output_dir / "t1_candidate_events_100.csv", index=False, encoding="utf-8-sig")
    case_df.to_csv(output_dir / "t1_candidate_cases_100.csv", index=False, encoding="utf-8-sig")
    pet_df.to_csv(output_dir / "t1_pet_ct_document_mentions.csv", index=False, encoding="utf-8-sig")
    with (output_dir / "t1_candidate_events_100.jsonl").open("w", encoding="utf-8") as handle:
        for row in candidate_df.to_dict("records"):
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    (output_dir / "t1_candidate_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build post-T0 90-day evidence candidates for manual T1 audit.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    write_outputs(args.output_dir)
    print(args.output_dir)


if __name__ == "__main__":
    main()

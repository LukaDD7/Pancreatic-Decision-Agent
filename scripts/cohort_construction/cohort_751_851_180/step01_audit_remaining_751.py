from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from scripts.cohort_construction.paths import data_root

ROOT = data_root()
OUT = ROOT / "outputs" / "remaining_751_research_usability_20261006"
RESTAGE = ROOT / "pipeline_outputs_stage8_v1/pancreas_restage_special_cases_v2/restricted"
FAILURE = ROOT / "pipeline_outputs_stage8_v1/pancreas_decision_failure_cases_v3/restricted"
STAGE7_ROOTS = [
    ROOT / "pipeline_outputs_stage7_v1/restricted/full_day_timeline/tasks",
    ROOT / "pipeline_outputs_stage7_v2_extension/restricted/pathology_total_extension/tasks",
]
ALIAS_DB = ROOT / "pipeline_outputs_stage6_v2/restricted/state/stage6_full_state_v2.sqlite3"
INVENTORY = ROOT / "影像数据汇总报告/patient_folder_inventory.parquet"


def clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "null", "nat"} else text


def clean_id(value) -> str:
    return re.sub(r"\.0$", "", clean(value).upper())


def iso_date(value) -> str:
    if value is None:
        return ""
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


def read_jsonl(path: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return pd.DataFrame(rows)


def read_stage7(event_type: str, columns: list[str], target_uids: set[str]) -> pd.DataFrame:
    paths = []
    for root in STAGE7_ROOTS:
        paths.extend(str(path) for path in root.glob(f"patient_*/{event_type}/*.parquet"))
    if not paths:
        return pd.DataFrame(columns=columns)
    dataset = ds.dataset(paths, format="parquet")
    available = [column for column in columns if column in dataset.schema.names]
    table = dataset.to_table(columns=available, filter=ds.field("patient_uid").isin(sorted(target_uids)))
    frame = table.to_pandas()
    return frame.drop_duplicates()


def ct_scope(method) -> str:
    text = clean(method).upper()
    if "CT" not in text or "PET" in text:
        return "非CT"
    if any(term in text for term in ("胰腺", "胰胆", "胰管")):
        return "明确胰腺CT"
    if any(term in text for term in ("上腹", "全腹", "腹部", "腹盆", "肝胆脾", "肝胆", "腹腔", "盆腔")):
        return "其他腹部CT"
    return "其他CT"


def is_pancreas_mr(method) -> bool:
    text = clean(method).upper()
    return any(term in text for term in ("MR", "MRI", "磁共振")) and any(
        term in text for term in ("胰腺", "胰胆", "胰管", "MRCP")
    )


def load_aliases(target_uids: set[str]):
    conn = sqlite3.connect(f"file:{ALIAS_DB}?mode=ro", uri=True)
    conn.execute("CREATE TEMP TABLE wanted_uid(patient_uid TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO wanted_uid VALUES (?)", ((uid,) for uid in target_uids))
    rows = conn.execute(
        "SELECT a.patient_uid, a.raw_value, a.source_system "
        "FROM identity_alias a JOIN wanted_uid w ON w.patient_uid=a.patient_uid "
        "WHERE a.id_type='PATIENT_ID'"
    ).fetchall()
    conn.close()
    grouped = defaultdict(list)
    for uid, raw, source in rows:
        patient_id = clean_id(raw)
        if patient_id:
            grouped[uid].append((clean(source), patient_id))
    preference = {"imaging": 0, "document": 1, "lab_l1": 2, "pathology": 3}
    preferred = {
        uid: sorted(values, key=lambda item: preference.get(item[0], 9))[0][1]
        for uid, values in grouped.items()
    }
    all_ids = {uid: {patient_id for _, patient_id in values} for uid, values in grouped.items()}
    return preferred, all_ids


def has_text(value) -> bool:
    return bool(clean(value))


def priority_source_status(statuses: set[str]) -> str:
    if "MODEL_REVIEWED_NOT_SELECTED" in statuses:
        return "模型已复核但未入选"
    if "VALID_DUPLICATE_OR_CAP_EXCLUDED" in statuses:
        return "有效但因重复或配额未入选"
    return "尚未优先审查"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ledger = pq.read_table(FAILURE / "review_ledger.parquet").to_pandas()
    selected_uids = set(ledger.loc[ledger.review_status.eq("SELECTED_CONSOLIDATED"), "patient_uid"])
    remaining_ledger = ledger[~ledger.patient_uid.isin(selected_uids)].copy()
    target_uids = set(remaining_ledger.patient_uid)
    assert len(target_uids) == 751

    status_map = remaining_ledger.groupby("patient_uid").review_status.agg(lambda x: set(x)).map(priority_source_status)
    candidates = pq.read_table(RESTAGE / "rule_candidates.parquet").to_pandas()
    candidates = candidates[candidates.patient_uid.isin(target_uids)].copy()
    candidates["event_date"] = candidates.event_date.map(iso_date)
    candidate_ledger = remaining_ledger[
        ["candidate_id", "rule_primary_reason", "review_status", "previously_model_reviewed"]
    ].drop_duplicates("candidate_id")
    candidates = candidates.merge(candidate_ledger, on="candidate_id", how="left")
    matched_docs = pq.read_table(RESTAGE / "matched_documents.parquet").to_pandas()
    matched_docs = matched_docs[matched_docs.patient_uid.isin(target_uids)].copy()
    matched_docs["event_date"] = matched_docs.event_date.map(iso_date)

    model = read_jsonl(FAILURE / "model_adjudication.jsonl")
    if not model.empty:
        model_small = model[
            ["candidate_id", "valid_case", "primary_class", "confidence", "decision_gap"]
        ].drop_duplicates("candidate_id")
        candidates = candidates.merge(model_small, on="candidate_id", how="left")

    imaging = read_stage7(
        "imaging_event", ["patient_uid", "event_date", "exam_method", "source_record_key"], target_uids
    )
    documents = read_stage7(
        "document_day_detail",
        ["patient_uid", "event_date", "document_type", "procedure_candidate", "anchor_eligible", "source_record_key"],
        target_uids,
    )
    pathology = read_stage7(
        "pathology_event", ["patient_uid", "event_date", "event_eligible", "source_record_key"], target_uids
    )
    labs = read_stage7(
        "lab_day_summary", ["patient_uid", "event_date", "lab_order_count", "lab_item_count"], target_uids
    )
    encounters = read_stage7(
        "encounter_interval", ["patient_uid", "encounter_uid", "admission_time", "discharge_time", "encounter_interval_status"], target_uids
    )
    timeline = read_stage7(
        "timeline_event", ["patient_uid", "event_date", "event_category", "event_type", "anchor_eligible"], target_uids
    )
    for frame in (imaging, documents, pathology, labs, timeline):
        if "event_date" in frame:
            frame["event_date"] = frame.event_date.map(iso_date)

    preferred_ids, all_aliases = load_aliases(target_uids)
    inventory_keys = set()
    for row in pq.read_table(INVENTORY, columns=["patient_id_raw", "exam_date_raw"]).to_pylist():
        patient_id = clean_id(row.get("patient_id_raw"))
        exam_date = iso_date(row.get("exam_date_raw"))
        if patient_id and exam_date:
            inventory_keys.add((patient_id, exam_date))

    followup_path = ROOT / "pipeline_outputs_stage6_v2/restricted/increment/followup_v1/full/record_links_followup/part-00001.parquet"
    followup = pq.read_table(
        followup_path, columns=["patient_uid", "disposition_class", "event_eligible"]
    ).to_pandas()
    followup_uids = set(
        followup.loc[
            followup.patient_uid.isin(target_uids)
            & followup.disposition_class.eq("hard"),
            "patient_uid",
        ]
    )

    imaging_by_uid = defaultdict(list)
    for row in imaging.itertuples(index=False):
        if row.event_date:
            imaging_by_uid[row.patient_uid].append((row.event_date, clean(row.exam_method)))
    docs_by_uid = defaultdict(list)
    for row in documents.itertuples(index=False):
        if row.event_date:
            docs_by_uid[row.patient_uid].append(
                (row.event_date, clean(row.document_type), bool(row.procedure_candidate), bool(row.anchor_eligible))
            )
    path_by_uid = defaultdict(set)
    for row in pathology.itertuples(index=False):
        if row.event_date and bool(row.event_eligible):
            path_by_uid[row.patient_uid].add(row.event_date)
    lab_by_uid = defaultdict(list)
    for row in labs.itertuples(index=False):
        if row.event_date:
            lab_by_uid[row.patient_uid].append((row.event_date, int(row.lab_item_count or 0)))
    timeline_by_uid = defaultdict(list)
    for row in timeline.itertuples(index=False):
        if row.event_date:
            timeline_by_uid[row.patient_uid].append(
                (row.event_date, clean(row.event_category), clean(row.event_type), bool(row.anchor_eligible))
            )

    encounter_counts = encounters.groupby("patient_uid").encounter_uid.nunique().to_dict() if not encounters.empty else {}
    rows = []
    episode_rows = []
    for uid in sorted(target_uids):
        pc = candidates[candidates.patient_uid.eq(uid)].copy()
        pc = pc.sort_values(["rule_score", "event_date"], ascending=[False, True], kind="stable")
        best = pc.iloc[0]
        patient_matched_docs = matched_docs[matched_docs.patient_uid.eq(uid)]
        patient_id = preferred_ids.get(uid) or clean_id(best.patient_id)
        candidate_dates = sorted({x for x in pc.event_date if x})
        candidate_visits = {clean(x) for x in pc.visit_id if clean(x)}
        decision_span = (
            (date.fromisoformat(candidate_dates[-1]) - date.fromisoformat(candidate_dates[0])).days
            if len(candidate_dates) >= 2
            else 0
        )

        img_rows = imaging_by_uid.get(uid, [])
        all_img_dates = sorted({d for d, _ in img_rows})
        pan_ct_dates = sorted({d for d, m in img_rows if ct_scope(m) == "明确胰腺CT"})
        abdomen_ct_dates = sorted({d for d, m in img_rows if ct_scope(m) in {"明确胰腺CT", "其他腹部CT"}})
        pan_mr_dates = sorted({d for d, m in img_rows if is_pancreas_mr(m)})
        pancreatobiliary_dates = sorted(set(pan_ct_dates) | set(pan_mr_dates))
        aliases = all_aliases.get(uid, {patient_id} if patient_id else set())
        matched_image_dates = sorted(
            {
                d
                for d in pancreatobiliary_dates
                if any((alias, d) in inventory_keys for alias in aliases)
            }
        )

        doc_rows = docs_by_uid.get(uid, [])
        doc_dates = sorted({d for d, _, _, _ in doc_rows})
        doc_types = sorted({t for _, t, _, _ in doc_rows if t})
        procedure_doc_dates = sorted({d for d, _, is_proc, _ in doc_rows if is_proc})
        path_dates = sorted(path_by_uid.get(uid, set()))
        lab_rows = lab_by_uid.get(uid, [])
        lab_dates = sorted({d for d, _ in lab_rows})
        lab_items = sum(items for _, items in lab_rows)
        all_event_dates = sorted({d for d, _, _, _ in timeline_by_uid.get(uid, [])})
        timeline_span = (
            (date.fromisoformat(all_event_dates[-1]) - date.fromisoformat(all_event_dates[0])).days
            if len(all_event_dates) >= 2
            else 0
        )

        interwindow_imaging = interwindow_pathology = interwindow_labs = interwindow_procedure = 0
        for start, end in zip(candidate_dates, candidate_dates[1:]):
            interwindow_imaging += sum(start < d < end for d in all_img_dates)
            interwindow_pathology += sum(start < d < end for d in path_dates)
            interwindow_labs += sum(start < d < end for d in lab_dates)
            interwindow_procedure += sum(start < d < end for d in procedure_doc_dates)
        interwindow_evidence = interwindow_imaging + interwindow_pathology + interwindow_labs + interwindow_procedure

        reason_counts = Counter(clean(x) for x in pc.rule_primary_reason if clean(x))
        reason_codes = sorted(code for code in reason_counts if code.startswith("C"))
        flags = set()
        for value in pc.episode_flags_json:
            try:
                flags.update(json.loads(value or "[]"))
            except json.JSONDecodeError:
                pass
        has_plan = any(has_text(x) for x in pc.planned_procedure) or any(
            has_text(x) for x in patient_matched_docs.planned_procedure
        )
        has_actual = any(has_text(x) for x in pc.actual_procedure) or any(
            has_text(x) for x in patient_matched_docs.actual_procedure
        )
        matched_plan_values = [clean(x) for x in patient_matched_docs.planned_procedure if has_text(x)]
        matched_actual_values = [clean(x) for x in patient_matched_docs.actual_procedure if has_text(x)]
        has_path_anchor = bool(path_dates) or any(has_text(x) for x in pc.nearby_pathology_date)
        has_preop_imaging = False
        latest_preop_age = None
        for event_date in candidate_dates:
            ages = [
                (date.fromisoformat(event_date) - date.fromisoformat(img_date)).days
                for img_date in all_img_dates
                if 0 <= (date.fromisoformat(event_date) - date.fromisoformat(img_date)).days <= 180
            ]
            if ages:
                has_preop_imaging = True
                latest_preop_age = min(ages) if latest_preop_age is None else min(latest_preop_age, min(ages))

        for value in patient_matched_docs.matched_flags:
            if isinstance(value, (list, tuple)):
                flags.update(clean(x) for x in value if clean(x))
        strong_operating_signal = bool(flags & {"abort", "biopsy_frozen", "distant_lesion", "local_unresectable", "progression"})
        evidence_doc_max = int(pd.to_numeric(pc.evidence_document_count, errors="coerce").fillna(0).max())
        rule_score_max = int(pd.to_numeric(pc.rule_score, errors="coerce").fillna(0).max())
        if reason_codes and has_plan and has_actual and has_preop_imaging and evidence_doc_max >= 3 and strong_operating_signal:
            special_tier = "A_可直接复核"
        elif reason_codes and evidence_doc_max >= 2 and (has_plan or has_actual) and (has_preop_imaging or has_path_anchor):
            special_tier = "B_补充核验后可用"
        elif reason_codes or strong_operating_signal:
            special_tier = "C_保留为线索"
        else:
            special_tier = "D_当前不适合五类研究"

        if len(pancreatobiliary_dates) >= 2 and has_path_anchor and len(matched_image_dates) >= 2:
            longitudinal_tier = "A_现有影像可计算"
        elif len(pancreatobiliary_dates) >= 2 and has_path_anchor:
            longitudinal_tier = "B_报告纵向且可下载补齐"
        elif len(pancreatobiliary_dates) >= 2:
            longitudinal_tier = "C_有纵向影像但缺病理锚点"
        elif len(all_img_dates) >= 2 and has_path_anchor:
            longitudinal_tier = "D_有临床纵向但非明确胰腺序列"
        else:
            longitudinal_tier = "E_影像纵向不足"

        modality_count = sum(
            [bool(doc_dates), bool(all_img_dates), bool(path_dates), bool(lab_dates), uid in followup_uids]
        )
        expansion_signals = sum(
            [
                len(pancreatobiliary_dates) >= 2,
                int(encounter_counts.get(uid, 0)) >= 2,
                len(procedure_doc_dates) >= 2,
                len(path_dates) >= 2,
                uid in followup_uids,
            ]
        )
        if modality_count >= 4 and len(all_event_dates) >= 10 and timeline_span >= 90 and (
            len(candidate_dates) >= 2 or expansion_signals >= 2
        ):
            agent_tier = "A_连续轨迹可直接打包"
        elif modality_count >= 3 and len(all_event_dates) >= 5 and timeline_span >= 30:
            agent_tier = "B_可打包但需补一类证据"
        elif modality_count >= 2 and len(all_event_dates) >= 2:
            agent_tier = "C_可作局部轨迹"
        else:
            agent_tier = "D_轨迹稀疏"

        if len(candidate_dates) >= 2 and decision_span >= 7 and interwindow_evidence >= 1:
            continuous_tier = "A_多窗口连续决策"
        elif len(candidate_dates) >= 2:
            continuous_tier = "B_多窗口但中间证据薄"
        elif len(candidate_dates) == 1 and timeline_span >= 90 and expansion_signals >= 2:
            continuous_tier = "C1_强可扩展第二决策窗口"
        elif len(candidate_dates) == 1 and timeline_span >= 30 and expansion_signals >= 1:
            continuous_tier = "C2_可扩展第二决策窗口"
        else:
            continuous_tier = "D_目前仅单时点"

        tier_rank = {"A": 4, "B": 3, "C": 2, "D": 1, "E": 0}
        ranks = {
            "五类特殊病例": tier_rank[special_tier[0]],
            "纵向研究": tier_rank[longitudinal_tier[0]],
            "Agent轨迹": tier_rank[agent_tier[0]],
            "连续决策": tier_rank[continuous_tier[0]],
        }
        recommended = sorted(ranks, key=lambda key: (-ranks[key], key))
        best_rank = ranks[recommended[0]]
        recommended_directions = ";".join(key for key in recommended if ranks[key] == best_rank and best_rank >= 2)
        if not recommended_directions:
            recommended_directions = "暂不进入主队列"
        overall = (
            "优先进入连续决策队列"
            if continuous_tier.startswith("A_")
            else "优先进入专项复核"
            if any(x.startswith("A_") for x in (special_tier, longitudinal_tier, agent_tier))
            else "可用但需补证据"
            if any(x.startswith(("B_", "C_")) for x in (special_tier, longitudinal_tier, agent_tier, continuous_tier))
            else "当前索引证据不足"
        )

        rows.append(
            {
                "患者编号": patient_id,
                "patient_uid": uid,
                "来源状态": status_map.get(uid, ""),
                "候选事件数": len(pc),
                "决策日期数": len(candidate_dates),
                "决策日期": ";".join(candidate_dates),
                "决策跨度天数": decision_span,
                "候选住院次数": len(candidate_visits),
                "规则最高分": rule_score_max,
                "规则主因线索": ";".join(reason_codes),
                "证据文书最大数": evidence_doc_max,
                "有计划术式": "Y" if has_plan else "U",
                "有实际术式": "Y" if has_actual else "U",
                "有术前180天影像": "Y" if has_preop_imaging else "U",
                "最近术前影像距决策天数": latest_preop_age if latest_preop_age is not None else "",
                "有病理锚点": "Y" if has_path_anchor else "U",
                "五类特殊病例可用性": special_tier,
                "明确胰腺CT日期数": len(pan_ct_dates),
                "胰腺相关MR日期数": len(pan_mr_dates),
                "胰胆影像日期数": len(pancreatobiliary_dates),
                "现有影像文件匹配日期数": len(matched_image_dates),
                "胰胆影像日期": ";".join(pancreatobiliary_dates),
                "纵向研究可用性": longitudinal_tier,
                "文书日期数": len(doc_dates),
                "文书类型数": len(doc_types),
                "手术候选文书日期数": len(procedure_doc_dates),
                "检验日期数": len(lab_dates),
                "检验项目累计数": lab_items,
                "病理日期数": len(path_dates),
                "随访硬连接": "Y" if uid in followup_uids else "U",
                "事件模态数": modality_count,
                "全时间轴事件日期数": len(all_event_dates),
                "全时间轴跨度天数": timeline_span,
                "Agent轨迹可用性": agent_tier,
                "窗口间影像事件数": interwindow_imaging,
                "窗口间病理事件数": interwindow_pathology,
                "窗口间检验日期数": interwindow_labs,
                "窗口间手术候选文书日期数": interwindow_procedure,
                "窗口间证据合计": interwindow_evidence,
                "第二窗口扩展信号数": expansion_signals,
                "连续决策可用性": continuous_tier,
                "总体建议": overall,
                "推荐研究方向": recommended_directions,
                "最优候选事件": clean(best.candidate_id),
                "最优候选日期": clean(best.event_date),
                "最优候选表型": clean(best.phenotype),
                "最优候选计划术式": clean(best.planned_procedure) or (matched_plan_values[0] if matched_plan_values else ""),
                "最优候选实际术式": clean(best.actual_procedure) or (matched_actual_values[0] if matched_actual_values else ""),
                "最优候选病理日期": clean(best.nearby_pathology_date),
                "疾病标签": ";".join(sorted({clean(x) for x in pc.disease_labels if clean(x)})),
            }
        )

        for episode in pc.itertuples(index=False):
            episode_rows.append(
                {
                    "患者编号": patient_id,
                    "patient_uid": uid,
                    "来源状态": status_map.get(uid, ""),
                    "candidate_id": episode.candidate_id,
                    "候选日期": episode.event_date,
                    "visit_id": clean(episode.visit_id),
                    "表型": clean(episode.phenotype),
                    "规则分": int(episode.rule_score or 0),
                    "规则主因线索": clean(episode.rule_primary_reason),
                    "证据文书数": int(episode.evidence_document_count or 0),
                    "结构化计划实际不一致": "Y" if bool(episode.structured_plan_actual_mismatch) else "N",
                    "计划术式": clean(episode.planned_procedure),
                    "实际术式": clean(episode.actual_procedure),
                    "附近病理日期": clean(episode.nearby_pathology_date),
                    "疾病标签": clean(episode.disease_labels),
                    "模型已判有效": "Y" if episode.valid_case is True else ("N" if episode.valid_case is False else "U"),
                    "模型主类": clean(episode.primary_class),
                    "模型置信度": clean(episode.confidence),
                }
            )

    patient_df = pd.DataFrame(rows)
    continuous_order = {
        "A_多窗口连续决策": 0,
        "C1_强可扩展第二决策窗口": 1,
        "C2_可扩展第二决策窗口": 2,
        "D_目前仅单时点": 3,
    }
    patient_df["_continuous_order"] = patient_df["连续决策可用性"].map(continuous_order).fillna(9)
    patient_df["_agent_order"] = patient_df["Agent轨迹可用性"].str[0].map({"A": 0, "B": 1, "C": 2, "D": 3}).fillna(9)
    patient_df["_special_order"] = patient_df["五类特殊病例可用性"].str[0].map({"A": 0, "B": 1, "C": 2, "D": 3}).fillna(9)
    patient_df = patient_df.sort_values(
        ["_continuous_order", "_agent_order", "_special_order", "规则最高分"],
        ascending=[True, True, True, False],
        kind="stable",
    ).drop(columns=["_continuous_order", "_agent_order", "_special_order"]).reset_index(drop=True)
    patient_df.insert(0, "序号", range(1, len(patient_df) + 1))
    episode_df = pd.DataFrame(episode_rows).sort_values(["患者编号", "候选日期", "规则分"], ascending=[True, True, False])
    episode_df.insert(0, "序号", range(1, len(episode_df) + 1))

    priority_df = patient_df[
        patient_df["总体建议"].isin(["优先进入连续决策队列", "优先进入专项复核"])
    ].copy()
    continuous_priority_df = patient_df[
        patient_df["连续决策可用性"].str.startswith(("A_", "C1_"), na=False)
    ].copy()
    summary_rows = []
    for field in ["来源状态", "总体建议", "五类特殊病例可用性", "纵向研究可用性", "Agent轨迹可用性", "连续决策可用性"]:
        for value, count in patient_df[field].value_counts(dropna=False).items():
            summary_rows.append({"分组": field, "类别": value, "患者数": int(count)})
    summary_df = pd.DataFrame(summary_rows)

    check = {
        "target_patients": int(len(patient_df)),
        "candidate_events": int(len(episode_df)),
        "unique_candidate_ids": int(episode_df.candidate_id.nunique()),
        "priority_patients": int(len(priority_df)),
        "selected_overlap": int(patient_df.patient_uid.isin(selected_uids).sum()),
        "duplicate_patient_uid": int(patient_df.patient_uid.duplicated().sum()),
    }
    assert check["target_patients"] == 751
    assert check["selected_overlap"] == 0
    assert check["duplicate_patient_uid"] == 0

    outputs = {
        "summary": summary_df,
        "patients": patient_df,
        "priority": priority_df,
        "continuous_priority": continuous_priority_df,
        "episodes": episode_df,
    }
    for name, frame in outputs.items():
        frame.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8-sig")
        (OUT / f"{name}.json").write_text(
            frame.to_json(orient="split", force_ascii=False, index=False), encoding="utf-8"
        )
    (OUT / "audit.json").write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(check, ensure_ascii=False, indent=2))
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()

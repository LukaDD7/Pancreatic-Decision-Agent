from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from scripts.cohort_construction.paths import data_root

ROOT = data_root()
OUT = ROOT / "outputs" / "continuous_decision_safd_851_20261006"
PRIOR = ROOT / "outputs" / "remaining_751_research_usability_20261006"
RESTAGE = ROOT / "pipeline_outputs_stage8_v1/pancreas_restage_special_cases_v2/restricted"
FAILURE = ROOT / "pipeline_outputs_stage8_v1/pancreas_decision_failure_cases_v3/restricted"
STAGE7_ROOTS = [
    ROOT / "pipeline_outputs_stage7_v1/restricted/full_day_timeline/tasks",
    ROOT / "pipeline_outputs_stage7_v2_extension/restricted/pathology_total_extension/tasks",
]
FOLLOWUP = ROOT / "pipeline_outputs_stage6_v2/restricted/increment/followup_v1/full/record_links_followup/part-00001.parquet"


def clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "null", "nat"} else text


def iso_date(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
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


def read_stage7(event_type: str, columns: list[str], target_uids: set[str]) -> pd.DataFrame:
    paths: list[str] = []
    for root in STAGE7_ROOTS:
        paths.extend(str(path) for path in root.glob(f"patient_*/{event_type}/*.parquet"))
    if not paths:
        return pd.DataFrame(columns=columns)
    dataset = ds.dataset(paths, format="parquet")
    available = [column for column in columns if column in dataset.schema.names]
    table = dataset.to_table(columns=available, filter=ds.field("patient_uid").isin(sorted(target_uids)))
    return table.to_pandas().drop_duplicates()


def ct_scope(method) -> str:
    text = clean(method).upper()
    if "CT" not in text or "PET" in text:
        return "非CT"
    if any(term in text for term in ("胰腺", "胰胆", "胰管")):
        return "明确胰腺CT"
    return "其他CT"


def is_pancreas_mr(method) -> bool:
    text = clean(method).upper()
    return any(term in text for term in ("MR", "MRI", "磁共振")) and any(
        term in text for term in ("胰腺", "胰胆", "胰管", "MRCP")
    )


def is_pancreas_imaging(method) -> bool:
    return ct_scope(method) == "明确胰腺CT" or is_pancreas_mr(method)


def document_signal(document_type: str) -> tuple[str, str]:
    text = clean(document_type)
    if any(term in text for term in ("手术记录", "麻醉记录", "手术护理记录", "术后首次病程")):
        return "actual_action", "手术或治疗实施"
    if any(term in text for term in ("术前小结", "术前讨论", "手术同意", "治疗计划", "化疗方案", "放疗计划", "多学科", "MDT")):
        return "planned_option", "治疗方案或可选动作"
    if any(term in text for term in ("化疗记录", "放疗记录", "介入记录", "消融记录", "ERCP记录", "置管记录", "治疗记录")):
        return "actual_action", "治疗实施"
    if any(term in text for term in ("出院记录", "出院小结", "转出记录", "术后病程")):
        return "feedback_context", "阶段结局或出院反馈"
    if any(term in text for term in ("入院记录", "入院小结", "转入记录")):
        return "state_context", "阶段起始状态"
    return "", ""


def action_category(text: str) -> str:
    value = clean(text)
    if not value:
        return ""
    if any(term in value for term in ("胰十二指肠", "Whipple", "胰头十二指肠")):
        return "胰十二指肠切除"
    if any(term in value for term in ("胰体尾", "远端胰", "胰尾")):
        return "远端胰腺切除"
    if "全胰" in value:
        return "全胰切除"
    if any(term in value for term in ("探查", "活检", "穿刺", "冰冻")):
        return "探查或病理确认"
    if any(term in value for term in ("化疗", "FOLFIRINOX", "吉西他滨", "白蛋白紫杉醇", "奥沙利铂", "替吉奥")):
        return "系统治疗"
    if "放疗" in value or "放化疗" in value:
        return "放疗或放化疗"
    if any(term in value for term in ("ERCP", "支架", "引流", "PTCD")):
        return "胆道或介入处理"
    if any(term in value for term in ("随访", "观察", "复查")):
        return "观察随访"
    if any(term in value for term in ("手术", "切除")):
        return "其他手术"
    return "其他明确动作"


def encounter_for_date(event_date: str, encounter_rows: list[dict]) -> str:
    if not event_date:
        return ""
    value = date.fromisoformat(event_date)
    matches = []
    for row in encounter_rows:
        admission = iso_date(row.get("admission_time"))
        discharge = iso_date(row.get("discharge_time"))
        if not admission:
            continue
        start = date.fromisoformat(admission)
        end = date.fromisoformat(discharge) if discharge else start
        if start <= value <= end:
            matches.append(((end - start).days, clean(row.get("encounter_uid"))))
    return sorted(matches)[0][1] if matches else ""


def event_ref(modality: str, event_date: str, label: str, source_key: str, encounter_uid: str = "") -> dict:
    return {
        "event_date": event_date,
        "modality": modality,
        "label": label,
        "source_record_key": source_key,
        "encounter_uid": encounter_uid,
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    remaining = pd.read_csv(PRIOR / "patients.csv", dtype=str).fillna("")
    shortlist = pq.read_table(FAILURE / "shortlist.parquet").to_pandas()
    shortlist["event_date"] = shortlist.event_date.map(iso_date)
    selected_uids = set(shortlist.patient_uid)
    assert len(remaining) == 751 and len(selected_uids) == 100

    cohort_rows = []
    for row in remaining.itertuples(index=False):
        cohort_rows.append(
            {
                "patient_uid": row.patient_uid,
                "患者编号": clean(getattr(row, "患者编号")),
                "队列来源": "剩余751例",
                "原连续决策分层": clean(getattr(row, "连续决策可用性")),
                "疾病标签": clean(getattr(row, "疾病标签")),
                "正式补建251例": "Y" if clean(getattr(row, "连续决策可用性")) == "C2_可扩展第二决策窗口" else "N",
            }
        )
    for uid, group in shortlist.groupby("patient_uid", sort=False):
        row = group.iloc[0]
        cohort_rows.append(
            {
                "patient_uid": uid,
                "患者编号": clean(row.patient_id),
                "队列来源": "原100例",
                "原连续决策分层": "原100例",
                "疾病标签": clean(row.disease_labels),
                "正式补建251例": "N",
            }
        )
    cohort = pd.DataFrame(cohort_rows)
    assert len(cohort) == 851 and cohort.patient_uid.nunique() == 851
    target_uids = set(cohort.patient_uid)

    candidates = pq.read_table(RESTAGE / "rule_candidates.parquet").to_pandas()
    candidates = candidates[candidates.patient_uid.isin(target_uids)].copy()
    candidates["event_date"] = candidates.event_date.map(iso_date)
    matched = pq.read_table(RESTAGE / "matched_documents.parquet").to_pandas()
    matched = matched[matched.patient_uid.isin(target_uids)].copy()
    matched["event_date"] = matched.event_date.map(iso_date)

    imaging = read_stage7(
        "imaging_event", ["patient_uid", "encounter_uid", "event_date", "exam_method", "source_record_key"], target_uids
    )
    documents = read_stage7(
        "document_day_detail",
        ["patient_uid", "encounter_uid", "event_date", "document_type", "source_record_key"],
        target_uids,
    )
    pathology = read_stage7(
        "pathology_event", ["patient_uid", "encounter_uid", "event_date", "event_eligible", "source_record_key"], target_uids
    )
    labs = read_stage7(
        "lab_day_summary", ["patient_uid", "encounter_uid", "event_date", "lab_order_count", "lab_item_count"], target_uids
    )
    encounters = read_stage7(
        "encounter_interval", ["patient_uid", "encounter_uid", "admission_time", "discharge_time", "encounter_interval_status"], target_uids
    )
    for frame in (imaging, documents, pathology, labs):
        if "event_date" in frame:
            frame["event_date"] = frame.event_date.map(iso_date)

    followup = pq.read_table(FOLLOWUP, columns=["patient_uid", "disposition_class", "event_eligible"]).to_pandas()
    followup_uids = set(followup.loc[followup.patient_uid.isin(target_uids) & followup.disposition_class.eq("hard"), "patient_uid"])

    by_uid = lambda frame: {uid: group.copy() for uid, group in frame.groupby("patient_uid")}
    candidate_by_uid = by_uid(candidates)
    matched_by_uid = by_uid(matched)
    imaging_by_uid = by_uid(imaging)
    document_by_uid = by_uid(documents)
    pathology_by_uid = by_uid(pathology)
    lab_by_uid = by_uid(labs)
    encounter_by_uid = by_uid(encounters)

    patient_rows = []
    window_rows = []
    transition_rows = []
    bundles = []

    for patient in cohort.itertuples(index=False):
        uid = patient.patient_uid
        pc = candidate_by_uid.get(uid, pd.DataFrame())
        pm = matched_by_uid.get(uid, pd.DataFrame())
        pi = imaging_by_uid.get(uid, pd.DataFrame())
        pdx = document_by_uid.get(uid, pd.DataFrame())
        pp = pathology_by_uid.get(uid, pd.DataFrame())
        pl = lab_by_uid.get(uid, pd.DataFrame())
        pe = encounter_by_uid.get(uid, pd.DataFrame())
        encounter_rows = pe.to_dict("records") if not pe.empty else []

        window_map: dict[str, dict] = {}

        def ensure_window(key: str, window_date: str, trigger: str, encounter_uid: str = "") -> dict:
            if key not in window_map:
                window_map[key] = {
                    "window_date": window_date,
                    "encounter_uid": encounter_uid,
                    "triggers": set(),
                    "evidence": [],
                    "planned_options_exact": [],
                    "actual_actions_exact": [],
                    "evidence_quotes": [],
                }
            window_map[key]["triggers"].add(trigger)
            return window_map[key]

        # 规则候选事件为已识别的治疗/手术决策锚点；同次住院只保留最高分事件日期。
        candidate_groups: dict[tuple[str, str], list] = defaultdict(list)
        for row in pc.itertuples(index=False):
            event_date = clean(row.event_date)
            if not event_date:
                continue
            encounter_uid = encounter_for_date(event_date, encounter_rows)
            candidate_groups[(encounter_uid, event_date)].append(row)
        for (encounter_uid, event_date), rows in candidate_groups.items():
            best = sorted(rows, key=lambda row: (int(row.rule_score or 0), clean(row.event_date)), reverse=True)[0]
            key = f"decision:{encounter_uid or event_date}:{event_date}"
            window = ensure_window(key, event_date, "rule_candidate", encounter_uid)
            for row in rows:
                window["evidence"].append(event_ref("candidate", clean(row.event_date), clean(row.phenotype), clean(row.candidate_id), encounter_uid))
                if clean(row.planned_procedure):
                    window["planned_options_exact"].append(clean(row.planned_procedure))
                if clean(row.actual_procedure):
                    window["actual_actions_exact"].append(clean(row.actual_procedure))

        # 同次住院中的计划/实施文书按阶段归并，避免按固定天数切窗。
        document_groups: dict[tuple[str, str], list] = defaultdict(list)
        for row in pdx.itertuples(index=False):
            signal, label = document_signal(clean(row.document_type))
            if not signal or not clean(row.event_date):
                continue
            encounter_uid = clean(row.encounter_uid) or encounter_for_date(clean(row.event_date), encounter_rows)
            group_id = encounter_uid or clean(row.event_date)
            document_groups[(group_id, signal)].append((row, label, encounter_uid))
        for (group_id, signal), rows in document_groups.items():
            dates = sorted(clean(row.event_date) for row, _, _ in rows)
            event_date = dates[-1] if signal == "planned_option" else dates[0] if signal == "actual_action" else dates[-1]
            encounter_uid = rows[0][2]
            same_encounter_candidates = [
                value for value in window_map.values() if encounter_uid and value["encounter_uid"] == encounter_uid and "rule_candidate" in value["triggers"]
            ]
            attach_to_candidate = False
            if signal in {"planned_option", "actual_action"} and same_encounter_candidates:
                candidate_window = min(same_encounter_candidates, key=lambda value: abs((date.fromisoformat(value["window_date"]) - date.fromisoformat(event_date)).days))
                day_delta = (date.fromisoformat(event_date) - date.fromisoformat(candidate_window["window_date"])).days
                attach_to_candidate = (signal == "planned_option" and day_delta <= 0) or (signal == "actual_action" and day_delta == 0)
            if attach_to_candidate:
                window = candidate_window
                window["triggers"].add(signal)
            else:
                kind = "decision" if signal in {"planned_option", "actual_action"} else "feedback" if signal == "feedback_context" else "observation"
                key = f"{kind}:{group_id}:{event_date}:{signal}"
                window = ensure_window(key, event_date, signal, encounter_uid)
            for row, label, _ in rows:
                if signal == "actual_action" and clean(row.event_date) != event_date:
                    continue
                window["evidence"].append(event_ref("document", clean(row.event_date), f"{label}:{clean(row.document_type)}", clean(row.source_record_key), encounter_uid))
                if signal == "actual_action":
                    document_type = clean(row.document_type)
                    if any(term in document_type for term in ("手术记录", "术后首次病程", "麻醉记录", "手术护理记录", "术后病程")):
                        window["actual_actions_exact"].append("手术已实施（文书证实，具体术式未从当前索引明确）")
                    else:
                        window["actual_actions_exact"].append("治疗已实施（文书证实，具体方案未从当前索引明确）")

        # 胰腺影像和病理是状态更新/反馈。它们不因“存在一次检查”自动变成决策。
        for row in pi.itertuples(index=False):
            event_date = clean(row.event_date)
            method = clean(row.exam_method)
            if not event_date or not is_pancreas_imaging(method):
                continue
            encounter_uid = clean(row.encounter_uid) or encounter_for_date(event_date, encounter_rows)
            existing = [value for value in window_map.values() if value["window_date"] == event_date]
            if existing:
                window = existing[0]
                window["triggers"].add("pancreas_imaging")
            else:
                window = ensure_window(f"observation:imaging:{event_date}", event_date, "pancreas_imaging", encounter_uid)
            window["evidence"].append(event_ref("imaging", event_date, method, clean(row.source_record_key), encounter_uid))

        for row in pp.itertuples(index=False):
            event_date = clean(row.event_date)
            if not event_date or not bool(row.event_eligible):
                continue
            encounter_uid = clean(row.encounter_uid) or encounter_for_date(event_date, encounter_rows)
            existing = [value for value in window_map.values() if value["window_date"] == event_date]
            if existing:
                window = existing[0]
                window["triggers"].add("pathology_feedback")
            else:
                window = ensure_window(f"feedback:pathology:{event_date}", event_date, "pathology_feedback", encounter_uid)
            window["evidence"].append(event_ref("pathology", event_date, "病理事件", clean(row.source_record_key), encounter_uid))

        # 将同日重复窗口合并；不同日期保留临床事件触发，不使用7天阈值。
        date_windows: dict[str, dict] = {}
        for value in window_map.values():
            event_date = value["window_date"]
            if event_date not in date_windows:
                date_windows[event_date] = value
            else:
                target = date_windows[event_date]
                target["triggers"].update(value["triggers"])
                target["evidence"].extend(value["evidence"])
                target["planned_options_exact"].extend(value["planned_options_exact"])
                target["actual_actions_exact"].extend(value["actual_actions_exact"])

        windows = []
        for index, event_date in enumerate(sorted(date_windows), start=1):
            value = date_windows[event_date]
            triggers = set(value["triggers"])
            planned_exact = list(dict.fromkeys(filter(None, value["planned_options_exact"])))
            actual_exact = list(dict.fromkeys(filter(None, value["actual_actions_exact"])))
            if planned_exact or actual_exact or triggers & {"rule_candidate", "planned_option", "actual_action"}:
                window_type = "decision_candidate"
            elif "pathology_feedback" in triggers or "feedback_context" in triggers:
                window_type = "feedback"
            else:
                window_type = "observation"

            evidence = sorted(value["evidence"], key=lambda item: (item["event_date"], item["modality"], item["source_record_key"]))
            imaging_methods = list(dict.fromkeys(item["label"] for item in evidence if item["modality"] == "imaging"))
            pathology_present = any(item["modality"] == "pathology" for item in evidence)
            document_labels = list(dict.fromkeys(item["label"] for item in evidence if item["modality"] == "document"))

            lab_context = []
            if not pl.empty:
                same_encounter = pl[pl.encounter_uid.eq(value["encounter_uid"])] if value["encounter_uid"] else pl[pl.event_date.eq(event_date)]
                same_encounter = same_encounter[same_encounter.event_date.le(event_date)]
                for row in same_encounter.itertuples(index=False):
                    lab_context.append(
                        {
                            "event_date": clean(row.event_date),
                            "lab_order_count": int(row.lab_order_count or 0),
                            "lab_item_count": int(row.lab_item_count or 0),
                            "role": "state_context_only",
                        }
                    )

            quotes = []
            if not pm.empty:
                for row in pm[pm.event_date.eq(event_date)].itertuples(index=False):
                    if clean(row.planned_procedure):
                        planned_exact.append(clean(row.planned_procedure))
                    if clean(row.actual_procedure):
                        actual_exact.append(clean(row.actual_procedure))
                    snippet = clean(row.evidence_snippet)
                    if snippet:
                        quotes.append(
                            {
                                "source_record_id": clean(row.source_record_id),
                                "document_title": clean(row.document_title),
                                "text": snippet[:500],
                            }
                        )
            planned_exact = list(dict.fromkeys(filter(None, planned_exact)))
            actual_exact = list(dict.fromkeys(filter(None, actual_exact)))
            planned_categories = list(dict.fromkeys(filter(None, (action_category(text) for text in planned_exact))))
            actual_categories = list(dict.fromkeys(filter(None, (action_category(text) for text in actual_exact))))
            if planned_exact or actual_exact:
                window_type = "decision"

            window = {
                "window_id": f"W{index}",
                "window_date": event_date,
                "window_type": window_type,
                "encounter_uid": value["encounter_uid"],
                "trigger_events": sorted(triggers),
                "state": {
                    "disease_labels": clean(patient.疾病标签),
                    "imaging_methods": imaging_methods,
                    "pathology_present": pathology_present,
                    "document_context": document_labels,
                    "lab_context": lab_context,
                    "evidence_status": "Y" if evidence else "U",
                },
                "available_actions": {
                    "status": "Y" if planned_exact else "U",
                    "explicit_options": planned_exact,
                    "action_categories": planned_categories,
                    "note": "仅记录原文明确计划或讨论；U表示当前索引未见，不等同于临床未讨论。",
                },
                "actual_action": {
                    "status": "Y" if actual_exact else "U",
                    "exact_actions": actual_exact,
                    "action_categories": actual_categories,
                    "note": "仅记录原文或实施文书证实的动作。",
                },
                "feedback": {
                    "status": "pending",
                    "next_window_id": "",
                    "next_window_date": "",
                    "observed_next_window_type": "",
                    "observed_evidence": [],
                    "causal_interpretation": "not_inferred",
                },
                "evidence": evidence[:100],
                "evidence_quotes": quotes[:30],
            }
            windows.append(window)

        # 反馈定义为后继观测，不能倒推为某动作造成的结果。
        for left, right in zip(windows, windows[1:]):
            left["feedback"] = {
                "status": "Y",
                "next_window_id": right["window_id"],
                "next_window_date": right["window_date"],
                "observed_next_window_type": right["window_type"],
                "observed_evidence": right["evidence"][:20],
                "causal_interpretation": "not_inferred",
            }
        if windows:
            windows[-1]["feedback"] = {
                "status": "Y" if uid in followup_uids else "U",
                "next_window_id": "",
                "next_window_date": "",
                "observed_next_window_type": "followup_link" if uid in followup_uids else "",
                "observed_evidence": [],
                "causal_interpretation": "not_inferred",
            }

        decision_positions = [index for index, window in enumerate(windows) if window["window_type"] == "decision"]
        decision_candidate_count = sum(window["window_type"] == "decision_candidate" for window in windows)
        observation_count = sum(window["window_type"] == "observation" for window in windows)
        feedback_count = sum(window["window_type"] == "feedback" for window in windows)
        has_decision_feedback_redecision = any(
            windows[middle]["window_type"] in {"observation", "feedback"}
            for left, right in zip(decision_positions, decision_positions[1:])
            for middle in range(left + 1, right)
        )
        if has_decision_feedback_redecision:
            research_tier = "A_决策-反馈-再决策"
        elif len(decision_positions) >= 2:
            research_tier = "B_至少两次决策"
        elif len(decision_positions) == 1 and len(windows) >= 2:
            research_tier = "C_单次决策加纵向反馈"
        elif observation_count + feedback_count >= 2:
            research_tier = "D_纵向观察无明确动作"
        elif decision_candidate_count >= 1:
            research_tier = "D_仅决策候选加纵向观察"
        else:
            research_tier = "E_仅纵向观察"

        patient_rows.append(
            {
                "患者编号": patient.患者编号,
                "patient_uid": uid,
                "队列来源": patient.队列来源,
                "正式补建251例": patient.正式补建251例,
                "原连续决策分层": patient.原连续决策分层,
                "疾病标签": patient.疾病标签,
                "总窗口数": len(windows),
                "决策窗口数": len(decision_positions),
                "候选决策窗口数": decision_candidate_count,
                "观察窗口数": observation_count,
                "反馈窗口数": feedback_count,
                "连续决策研究分层": research_tier,
                "有明确可选动作窗口数": sum(window["available_actions"]["status"] == "Y" for window in windows),
                "有明确实际动作窗口数": sum(window["actual_action"]["status"] == "Y" for window in windows),
                "有硬连接随访": "Y" if uid in followup_uids else "U",
                "需要人工复核": "Y" if research_tier.startswith(("C_", "D_", "E_")) else "N",
            }
        )

        for window in windows:
            window_rows.append(
                {
                    "患者编号": patient.患者编号,
                    "patient_uid": uid,
                    "队列来源": patient.队列来源,
                    "正式补建251例": patient.正式补建251例,
                    "窗口编号": window["window_id"],
                    "窗口日期": window["window_date"],
                    "窗口类型": window["window_type"],
                    "触发事件": ";".join(window["trigger_events"]),
                    "状态证据": "Y" if window["state"]["evidence_status"] == "Y" else "U",
                    "可选动作状态": window["available_actions"]["status"],
                    "明确可选动作": ";".join(window["available_actions"]["explicit_options"]),
                    "实际动作状态": window["actual_action"]["status"],
                    "明确实际动作": ";".join(window["actual_action"]["exact_actions"]),
                    "后续反馈状态": window["feedback"]["status"],
                    "后继窗口": window["feedback"]["next_window_id"],
                    "后继日期": window["feedback"]["next_window_date"],
                    "证据条目数": len(window["evidence"]),
                    "原文证据数": len(window["evidence_quotes"]),
                }
            )
        for left, right in zip(windows, windows[1:]):
            transition_rows.append(
                {
                    "患者编号": patient.患者编号,
                    "patient_uid": uid,
                    "起始窗口": left["window_id"],
                    "结束窗口": right["window_id"],
                    "起始日期": left["window_date"],
                    "结束日期": right["window_date"],
                    "间隔天数": (date.fromisoformat(right["window_date"]) - date.fromisoformat(left["window_date"])).days,
                    "起始窗口类型": left["window_type"],
                    "结束窗口类型": right["window_type"],
                    "起始实际动作状态": left["actual_action"]["status"],
                    "后继状态证据": right["state"]["evidence_status"],
                    "因果解释": "未推断",
                }
            )
        bundles.append(
            {
                "schema_version": "continuous_decision_SAFD_v2",
                "patient": {
                    "patient_id": patient.患者编号,
                    "patient_uid": uid,
                    "cohort_source": patient.队列来源,
                    "formal_C2_expansion": patient.正式补建251例 == "Y",
                    "disease_labels": patient.疾病标签,
                },
                "coverage": {
                    "window_count": len(windows),
                    "decision_window_count": len(decision_positions),
                    "decision_candidate_window_count": decision_candidate_count,
                    "observation_window_count": observation_count,
                    "feedback_window_count": feedback_count,
                    "continuous_decision_research_tier": research_tier,
                },
                "windows": windows,
                "agent_contract": {
                    "state": "仅使用窗口当时及此前可获得的索引证据。",
                    "available_actions": "仅记录原文明确计划；U不等于未讨论。",
                    "actual_action": "仅记录原文或实施文书证实的动作。",
                    "feedback": "记录后继观测，不将时间先后解释为因果。",
                },
            }
        )

    patient_df = pd.DataFrame(patient_rows).sort_values(["连续决策研究分层", "正式补建251例", "患者编号"], kind="stable")
    patient_df.insert(0, "序号", range(1, len(patient_df) + 1))
    window_df = pd.DataFrame(window_rows).sort_values(["患者编号", "窗口日期", "窗口编号"], kind="stable")
    window_df.insert(0, "序号", range(1, len(window_df) + 1))
    transition_df = pd.DataFrame(transition_rows).sort_values(["患者编号", "起始日期"], kind="stable")
    transition_df.insert(0, "序号", range(1, len(transition_df) + 1))

    summary_rows = []
    for scope, frame in [
        ("全851例", patient_df),
        ("正式补建251例", patient_df[patient_df["正式补建251例"].eq("Y")]),
        ("原100例", patient_df[patient_df["队列来源"].eq("原100例")]),
    ]:
        for tier, count in frame["连续决策研究分层"].value_counts().items():
            summary_rows.append({"范围": scope, "指标": "连续决策研究分层", "类别": tier, "患者数": int(count)})
        summary_rows.extend(
            [
                {"范围": scope, "指标": "覆盖", "类别": "患者数", "患者数": int(len(frame))},
                {"范围": scope, "指标": "覆盖", "类别": "至少2个总窗口", "患者数": int((frame["总窗口数"] >= 2).sum())},
                {"范围": scope, "指标": "覆盖", "类别": "至少2个明确决策窗口", "患者数": int((frame["决策窗口数"] >= 2).sum())},
                {"范围": scope, "指标": "覆盖", "类别": "至少1个明确实际动作", "患者数": int((frame["有明确实际动作窗口数"] >= 1).sum())},
            ]
        )
    summary_df = pd.DataFrame(summary_rows)

    checks = {
        "patients": int(len(patient_df)),
        "formal_C2_patients": int(patient_df["正式补建251例"].eq("Y").sum()),
        "windows": int(len(window_df)),
        "transitions": int(len(transition_df)),
        "duplicate_patient_uid": int(patient_df.patient_uid.duplicated().sum()),
        "duplicate_patient_window": int(window_df.duplicated(["patient_uid", "窗口编号"]).sum()),
        "negative_transition_days": int((transition_df["间隔天数"] < 0).sum()),
        "isolated_lab_windows": 0,
    }
    assert checks["patients"] == 851
    assert checks["formal_C2_patients"] == 251
    assert checks["duplicate_patient_uid"] == 0
    assert checks["duplicate_patient_window"] == 0
    assert checks["negative_transition_days"] == 0

    outputs = {
        "summary": summary_df,
        "patient_cohort": patient_df,
        "windows_SAFD": window_df,
        "transitions": transition_df,
        "formal_C2_251": patient_df[patient_df["正式补建251例"].eq("Y")].copy(),
        "formal_C2_251_windows_SAFD": window_df[window_df["正式补建251例"].eq("Y")].copy(),
        "formal_C2_251_transitions": transition_df[
            transition_df.patient_uid.isin(set(patient_df.loc[patient_df["正式补建251例"].eq("Y"), "patient_uid"]))
        ].copy(),
    }
    for name, frame in outputs.items():
        frame.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8-sig")
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), OUT / f"{name}.parquet", compression="zstd")
        (OUT / f"{name}.json").write_text(frame.to_json(orient="records", force_ascii=False), encoding="utf-8")
    with (OUT / "agent_patient_bundle_SAFD.jsonl").open("w", encoding="utf-8") as handle:
        for bundle in bundles:
            handle.write(json.dumps(bundle, ensure_ascii=False) + "\n")
    (OUT / "audit.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()

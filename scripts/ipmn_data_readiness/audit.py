import argparse
import csv
import gzip
import hashlib
import json
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

from .candidate import SCHEMA_PATH
from .handoff import build_readiness_handoff
from .rules import exam_view, lab_kind, parse_date, pathology_view, surgery_date

BD_TYPES = {"BD_EXPLICIT", "UNTYPED_MPD_NORMAL"}
TARGET_EVENTS = {"IPMN_HGD", "IPMN_IC", "IPMN_HGD_IC_PLUS_CPDAC"}
CPDAC_EVENTS = {"CPDAC_CANDIDATE"}
UNATTRIBUTED_EVENTS = {"PANC_CA_WITH_IPMN_UNATTRIBUTED", "PANC_CA_NO_IPMN_WORDING"}
CROSS_SECTIONAL = {"CT", "MRI", "MRCP", "EUS"}


def read_csv_from_zip(archive, name):
    with archive.open(name) as raw:
        text = (line.decode("utf-8-sig") for line in raw)
        return list(csv.DictReader(text))


def single_archive_csv(archive, suffix):
    names = [name for name in archive.namelist() if name.endswith(suffix) and not Path(name).name.startswith("._")]
    if len(names) != 1:
        raise ValueError(f"Expected exactly one {suffix}, found {len(names)}")
    return read_csv_from_zip(archive, names[0])


def unique_research_rows(rows, label):
    indexed = {row["research_id"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"Duplicate research_id in {label}")
    return indexed


def source_indexes(source_root):
    with ZipFile(source_root / "索引与说明.zip") as archive:
        l2_rows = single_archive_csv(archive, "L2全部索引.csv")
    with ZipFile(source_root / "L1主队列.zip") as archive:
        l1_rows = single_archive_csv(archive, "队列索引.csv")
    l1 = unique_research_rows(l1_rows, "L1")
    l2 = unique_research_rows(l2_rows, "L2")
    missing = set(l1) - set(l2)
    if missing:
        raise ValueError(f"L1 research_id missing from L2: {sorted(missing)}")
    return l1, l2


def in_window(value, start, end):
    return bool(value and start <= value <= end)


def event_period(value, entry, landmark, three_year_end):
    if not value:
        return "date_missing"
    day = parse_date(value)
    if not day:
        return "date_missing"
    if day < entry:
        return "before_entry"
    if day <= landmark:
        return "entry_to_landmark"
    if day <= three_year_end:
        return "post_landmark_3y"
    return "after_3y"


def percentile(values, proportion):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * proportion
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] * (1 - fraction) + ordered[upper] * fraction, 2)


def patient_audit(patient, l1_row, l2_row):
    entry = parse_date(l2_row.get("entry_date"))
    landmark = entry + timedelta(days=365) if entry else None
    end_3y = landmark + timedelta(days=3 * 365) if landmark else None
    exams = []
    for index, record in enumerate(patient.get("imaging_all") or []):
        view = exam_view(record)
        view["ref"] = f"imaging_all:{index}"
        exams.append(view)
    pathologies = []
    for index, record in enumerate(patient.get("pathology_all") or []):
        view = pathology_view(record)
        view["ref"] = f"pathology_all:{index}"
        pathologies.append(view)
    documents = patient.get("documents_all") or []
    surgeries = []
    deaths = []
    contacts = []
    diabetes_mentions = []
    for index, record in enumerate(documents):
        created = parse_date(record.get("create_time") or record.get("CREATE_DATE_TIME") or record.get("archive_event_time"))
        if created:
            contacts.append(created)
        title = str(record.get("文书名称") or "")
        if created and "死亡" in title:
            deaths.append({"date": created.isoformat(), "ref": f"documents_all:{index}", "basis": "document_title_proxy"})
        operation_date = surgery_date(record)
        if operation_date:
            text = str(record.get("document_text") or record.get("文书内容") or "")
            surgeries.append({"date": operation_date.isoformat(), "ref": f"documents_all:{index}", "basis": "operation_record", "total_resection_candidate": "全胰" in text})
        text = str(record.get("document_text") or record.get("文书内容") or "")
        if created and "糖尿病" in text:
            diabetes_mentions.append(created.isoformat())
    for pathology in pathologies:
        if pathology["resection"] and pathology["date"]:
            surgeries.append({key: pathology[key] for key in ("date", "ref", "total_resection_candidate")} | {"basis": "pathology_resection_proxy"})
    for exam in exams:
        if exam["date"]:
            contacts.append(parse_date(exam["date"]))
    for pathology in pathologies:
        if pathology["date"]:
            contacts.append(parse_date(pathology["date"]))
    lab_counts = Counter()
    lab_days = {key: set() for key in ("ca199", "hba1c", "total_bilirubin", "direct_bilirubin", "indirect_bilirubin")}
    for record in patient.get("laboratory_all") or []:
        kind = lab_kind(record.get("item_name"))
        sampled = parse_date(record.get("sample_time") or record.get("report_time"))
        if sampled:
            contacts.append(sampled)
        if kind and sampled:
            lab_counts[kind] += 1
            lab_days[kind].add(sampled.isoformat())
    first_year = [exam for exam in exams if entry and exam["pancreas"] and not exam["postop"] and in_window(parse_date(exam["date"]), entry, landmark)]
    cross_first_year = [exam for exam in first_year if exam["modality"] in CROSS_SECTIONAL]
    modality_days = {modality: set() for modality in ("CT", "MRI", "MRCP", "EUS")}
    size_days = {modality: set() for modality in ("CT", "MRI", "MRCP", "EUS")}
    mpd_days = {modality: set() for modality in ("CT", "MRI", "MRCP", "EUS")}
    mpd_status_days = {modality: set() for modality in ("CT", "MRI", "MRCP", "EUS")}
    nodule_days = {modality: [] for modality in ("CT", "MRI", "MRCP", "EUS")}
    for exam in cross_first_year:
        modality = exam["modality"]
        modality_days[modality].add(exam["date"])
        if exam["cyst_size_mm"] is not None:
            size_days[modality].add(exam["date"])
        if exam["mpd_mm"] is not None:
            mpd_days[modality].add(exam["date"])
        if exam["mpd_status"] != "unknown":
            mpd_status_days[modality].add(exam["date"])
        if exam["nodule_status"] != "not_mentioned":
            nodule_days[modality].append((exam["date"], exam["nodule_status"]))
    mr_days = modality_days["MRI"] | modality_days["MRCP"]
    mr_size_days = size_days["MRI"] | size_days["MRCP"]
    mr_mpd_days = mpd_days["MRI"] | mpd_days["MRCP"]
    mr_mpd_status_days = mpd_status_days["MRI"] | mpd_status_days["MRCP"]
    ct_days = modality_days["CT"]
    all_days = set().union(*modality_days.values()) if modality_days else set()
    numeric_size_days = set().union(*size_days.values()) if size_days else set()
    numeric_mpd_days = set().union(*mpd_days.values()) if mpd_days else set()

    def intervals(days):
        ordered = sorted(parse_date(day) for day in days)
        return [(later - earlier).days for earlier, later in zip(ordered, ordered[1:])]

    all_intervals = intervals(all_days)
    ct_intervals = intervals(ct_days)
    mr_intervals = intervals(mr_days)
    same_modality_2 = len(ct_days) >= 2 or len(mr_days) >= 2
    same_modality_3 = len(ct_days) >= 3 or len(mr_days) >= 3
    size_repeat_same_modality = len(size_days["CT"]) >= 2 or len(mr_size_days) >= 2
    mpd_repeat_same_modality = len(mpd_days["CT"]) >= 2 or len(mr_mpd_days) >= 2
    mpd_status_repeat_same_modality = len(mpd_status_days["CT"]) >= 2 or len(mr_mpd_status_days) >= 2
    new_nodule_candidate = False
    for observations in nodule_days.values():
        observations = sorted(observations)
        for later_date, later_status in observations:
            if later_status == "explicit_present_candidate" and any(day < later_date and status == "explicit_absent" for day, status in observations):
                new_nodule_candidate = True
    target_events = [event for event in pathologies if event["malignancy"] in TARGET_EVENTS]
    hgd_events = [event for event in pathologies if event["malignancy"] == "IPMN_HGD"]
    ic_events = [event for event in pathologies if event["malignancy"] == "IPMN_IC"]
    joint_events = [event for event in pathologies if event["malignancy"] == "IPMN_HGD_IC_PLUS_CPDAC"]
    lgd_events = [event for event in pathologies if event["ipmn_lgd_candidate"]]
    non_target_pathologies = [event for event in pathologies if event["non_target_pancreas_pathology_candidate"]]
    cpdac_events = [event for event in pathologies if event["malignancy"] in CPDAC_EVENTS]
    unattributed_events = [event for event in pathologies if event["malignancy"] in UNATTRIBUTED_EVENTS]
    prelandmark_target = [event for event in target_events if parse_date(event["date"]) and parse_date(event["date"]) <= landmark] if landmark else []
    prelandmark_cpdac = [event for event in cpdac_events if parse_date(event["date"]) and parse_date(event["date"]) <= landmark] if landmark else []
    prelandmark_unattributed = [event for event in unattributed_events if parse_date(event["date"]) and parse_date(event["date"]) <= landmark] if landmark else []
    prelandmark_deaths = [event for event in deaths if landmark and parse_date(event["date"]) <= landmark]
    prelandmark_surgeries = [event for event in surgeries if landmark and parse_date(event["date"]) <= landmark]
    prelandmark_malignant_imaging = [{"date": exam["date"], "ref": exam["ref"], "definite_pancreatic_cancer_candidate": exam["definite_pancreatic_cancer_candidate"]} for exam in exams if landmark and exam["date"] and parse_date(exam["date"]) <= landmark and (exam["malignant_wording_candidate"] or exam["definite_pancreatic_cancer_candidate"])]
    postlandmark_contacts = sorted(day for day in contacts if landmark and day >= landmark)
    last_contact = max(contacts).isoformat() if contacts else ""
    baseline_bd = l2_row.get("baseline_subtype") in BD_TYPES
    no_prelandmark_cancer_rule = not (prelandmark_target or prelandmark_cpdac or prelandmark_unattributed)
    eligibility_proxy = bool(entry and baseline_bd and no_prelandmark_cancer_rule and not prelandmark_deaths and not prelandmark_surgeries and postlandmark_contacts)
    post_3y_target = [event for event in target_events if event_period(event["date"], entry, landmark, end_3y) == "post_landmark_3y"] if entry else []
    post_3y_cpdac = [event for event in cpdac_events if event_period(event["date"], entry, landmark, end_3y) == "post_landmark_3y"] if entry else []
    post_3y_unattributed = [event for event in unattributed_events if event_period(event["date"], entry, landmark, end_3y) == "post_landmark_3y"] if entry else []
    post_3y_deaths = [event for event in deaths if event_period(event["date"], entry, landmark, end_3y) == "post_landmark_3y"] if entry else []
    post_3y_surgeries = [event for event in surgeries if event_period(event["date"], entry, landmark, end_3y) == "post_landmark_3y"] if entry else []
    ca199_first_year = {day for day in lab_days["ca199"] if entry and in_window(parse_date(day), entry, landmark)}
    bilirubin_first_year = set().union(*(lab_days[kind] for kind in ("total_bilirubin", "direct_bilirubin", "indirect_bilirubin")))
    bilirubin_first_year = {day for day in bilirubin_first_year if entry and in_window(parse_date(day), entry, landmark)}
    hba1c_first_year = {day for day in lab_days["hba1c"] if entry and in_window(parse_date(day), entry, landmark)}
    diabetes_first_year = {day for day in diabetes_mentions if entry and in_window(parse_date(day), entry, landmark)}
    research_rows = (patient.get("followup_analysis_only") or []) + (patient.get("research_source_rows_analysis_only") or [])
    age_available = any(row.get("age") not in (None, "") for row in research_rows)
    sex_available = any(row.get("sex(男=1,女=0)") not in (None, "") or row.get("sex（男1女0）") not in (None, "") for row in research_rows)
    reasons = []
    refs = []
    if not postlandmark_contacts and entry:
        reasons.append("NO_CONTACT_ON_OR_AFTER_LANDMARK")
    if prelandmark_target or prelandmark_cpdac or prelandmark_unattributed:
        reasons.append("PRELANDMARK_CANCER_ATTRIBUTION_AND_DATE")
    if prelandmark_deaths:
        reasons.append("DEATH_DATE_AND_VITAL_STATUS")
    if prelandmark_surgeries:
        reasons.append("PRELANDMARK_RESECTION_SCOPE_AND_INDEX_LESION")
    if prelandmark_malignant_imaging:
        reasons.append("PRELANDMARK_IMAGING_CANCER_KNOWN_VS_SUSPICION")
        refs.extend(event["ref"] for event in prelandmark_malignant_imaging)
    if post_3y_target or post_3y_cpdac or post_3y_unattributed:
        reasons.append("POSTLANDMARK_CANCER_PATHOLOGY_ATTRIBUTION")
    if post_3y_deaths:
        reasons.append("POSTLANDMARK_DEATH_AS_COMPETING_EVENT")
    if post_3y_surgeries:
        reasons.append("POSTLANDMARK_RESECTION_SCOPE_AND_OUTCOME")
    if size_repeat_same_modality or mpd_repeat_same_modality:
        reasons.append("INDEX_CYST_MATCH_AND_PROTOCOL_COMPARABILITY")
    for event in target_events + cpdac_events + unattributed_events + deaths + surgeries:
        if event.get("date") and entry and parse_date(event["date"]) <= end_3y:
            refs.append(event["ref"])
    return {
        "research_id": l1_row["research_id"],
        "evidence_grade": l1_row.get("evidence_grade", ""),
        "L2_route": l2_row.get("L2_route", ""),
        "baseline_subtype": l2_row.get("baseline_subtype", ""),
        "baseline_bd_rule_candidate": baseline_bd,
        "entry_date": entry.isoformat() if entry else "",
        "landmark_date": landmark.isoformat() if landmark else "",
        "three_year_end": end_3y.isoformat() if end_3y else "",
        "last_contact_date": last_contact,
        "contact_on_or_after_landmark": bool(postlandmark_contacts),
        "no_prelandmark_cancer_rule": no_prelandmark_cancer_rule,
        "prelandmark_death_proxy": bool(prelandmark_deaths),
        "prelandmark_resection_proxy": bool(prelandmark_surgeries),
        "prelandmark_malignant_imaging_review": prelandmark_malignant_imaging,
        "landmark_eligibility_rule_proxy": eligibility_proxy,
        "entry_evidence_available_date": l1_row.get("entry_evidence_available_date", ""),
        "total_pancreas_imaging_records": sum(exam["pancreas"] for exam in exams),
        "first_year_all_cross_sectional_days": len(all_days),
        "first_year_ct_days": len(ct_days),
        "first_year_mri_days": len(modality_days["MRI"]),
        "first_year_mrcp_days": len(modality_days["MRCP"]),
        "first_year_mr_family_days": len(mr_days),
        "first_year_eus_days": len(modality_days["EUS"]),
        "first_year_numeric_size_days": len(numeric_size_days),
        "first_year_numeric_mpd_days": len(numeric_mpd_days),
        "first_year_all_median_interval_days": statistics.median(all_intervals) if all_intervals else "",
        "first_year_ct_median_interval_days": statistics.median(ct_intervals) if ct_intervals else "",
        "first_year_mr_median_interval_days": statistics.median(mr_intervals) if mr_intervals else "",
        "repeat_same_modality_2": same_modality_2,
        "repeat_same_modality_3": same_modality_3,
        "repeat_numeric_size_same_modality": size_repeat_same_modality,
        "repeat_numeric_mpd_same_modality": mpd_repeat_same_modality,
        "repeat_mpd_status_same_modality": mpd_status_repeat_same_modality,
        "new_nodule_text_candidate": new_nodule_candidate,
        "nodule_explicit_present_first_year": any(status == "explicit_present_candidate" for observations in nodule_days.values() for _, status in observations),
        "ca199_first_year_days": len(ca199_first_year),
        "bilirubin_first_year_days": len(bilirubin_first_year),
        "hba1c_first_year_days": len(hba1c_first_year),
        "diabetes_text_first_year_days": len(diabetes_first_year),
        "age_in_analysis_only_research_rows": age_available,
        "sex_in_analysis_only_research_rows": sex_available,
        "target_event_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in target_events)) if entry else {},
        "hgd_event_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in hgd_events)) if entry else {},
        "ipmn_ic_event_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in ic_events)) if entry else {},
        "joint_target_cpdac_event_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in joint_events)) if entry else {},
        "lgd_event_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in lgd_events)) if entry else {},
        "non_target_pathology_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in non_target_pathologies)) if entry else {},
        "cpdac_event_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in cpdac_events)) if entry else {},
        "unattributed_cancer_event_counts_by_period": dict(Counter(event_period(event["date"], entry, landmark, end_3y) for event in unattributed_events)) if entry else {},
        "postlandmark_3y_target_pathology_candidates": post_3y_target,
        "postlandmark_3y_cpdac_pathology_candidates": post_3y_cpdac,
        "postlandmark_3y_unattributed_cancer_candidates": post_3y_unattributed,
        "postlandmark_3y_death_proxies": post_3y_deaths,
        "postlandmark_3y_resection_proxies": post_3y_surgeries,
        "postlandmark_3y_total_resection_proxies": [event for event in post_3y_surgeries if event["total_resection_candidate"]],
        "postlandmark_3y_non_total_resection_proxies": [event for event in post_3y_surgeries if not event["total_resection_candidate"]],
        "manual_review_reasons": reasons,
        "manual_review_refs": list(dict.fromkeys(refs)),
    }


def table_row(stage, count, denominator, status, definition):
    return {"stage": stage, "count": count, "denominator": denominator, "status": status, "definition": definition}


def distribution_rows(metric, values, scope, status, definition):
    counts = Counter(values)
    return [
        {"metric": metric, "scope": scope, "category": "0", "count": counts[0], "status": status, "definition": definition},
        {"metric": metric, "scope": scope, "category": "1", "count": counts[1], "status": status, "definition": definition},
        {"metric": metric, "scope": scope, "category": "2", "count": counts[2], "status": status, "definition": definition},
        {"metric": metric, "scope": scope, "category": ">=3", "count": sum(value for key, value in counts.items() if key >= 3), "status": status, "definition": definition},
        {"metric": metric, "scope": scope, "category": "median", "count": statistics.median(values) if values else 0, "status": status, "definition": definition},
        {"metric": metric, "scope": scope, "category": "Q1", "count": percentile(values, 0.25), "status": status, "definition": definition},
        {"metric": metric, "scope": scope, "category": "Q3", "count": percentile(values, 0.75), "status": status, "definition": definition},
    ]


def build_tables(rows, source_counts):
    n3 = [row for row in rows if row["baseline_bd_rule_candidate"] and row["entry_date"]]
    n4_upper = [row for row in n3 if row["no_prelandmark_cancer_rule"]]
    n4 = [row for row in n3 if row["landmark_eligibility_rule_proxy"]]
    n5 = [row for row in n4 if row["repeat_same_modality_2"]]
    n5_size = [row for row in n4 if row["repeat_numeric_size_same_modality"]]
    n6 = [row for row in n5 if row["contact_on_or_after_landmark"]]
    events = sum(bool(row["postlandmark_3y_target_pathology_candidates"]) for row in n6)
    events_size = sum(bool(row["postlandmark_3y_target_pathology_candidates"]) for row in n5_size)
    table_a = [
        table_row("N0_source_recall", source_counts["N0"], "", "source_rule", "全来源召回底库；不是确诊IPMN人数"),
        table_row("N1_L1_prepath_diagnosis", source_counts["N1"], "N0", "source_rule_requires_review", "病理前影像主诊断或临床诊断的规则预审主队列"),
        table_row("N2_baseline_BD_rule_candidate", sum(row["baseline_bd_rule_candidate"] for row in rows), "N1", "rule_candidate", "BD_EXPLICIT或UNTYPED_MPD_NORMAL；未分型正常MPD仍需临床审定"),
        table_row("N3_BD_with_imaging_entry", len(n3), "N2", "rule_candidate", "N2且有病理前CT/MRI/MRCP/EUS entry日期"),
        table_row("N4_upper_no_recorded_prelandmark_cancer", len(n4_upper), "N3", "upper_bound_not_eligible_cohort", "仅排除规则发现的landmark前恶性病理候选；未闭合死亡、切除、未知结局"),
        table_row("N4_observed_eligibility_proxy", len(n4), "N3", "conservative_proxy_not_clinical_N4", "无规则发现的landmark前癌/死亡/胰腺切除代理，且landmark当日或之后有记录；不是临床确认风险集"),
        table_row("N5_repeat_same_modality", len(n5), "N4_proxy", "measurement_coverage", "首年至少2个CT日或至少2个MRI/MRCP日；未证明同协议或同病灶"),
        table_row("N5_repeat_numeric_cyst_size", len(n5_size), "N4_proxy", "measurement_coverage", "首年同模态至少2个有报告囊肿数值的日期；未证明同一index cyst"),
        table_row("N6_minimal_outcome_trace", len(n6), "N5", "minimal_trace_only", "N5且landmark后有任一临床接触；不等于完整3年结局追踪"),
        table_row("E_postlandmark_3y_target_pathology_candidates", events, "N6", "requires_blinded_outcome_adjudication", "N6中landmark后3年内至少有1个IPMN相关HGD/IC病理规则候选的患者数；不是审定主事件数"),
        table_row("E_in_repeat_numeric_cyst_size_subset", events_size, "N5_repeat_numeric_cyst_size", "requires_blinded_outcome_adjudication", "重复囊肿数值子集中的主事件病理规则候选；用于观察强动态对照共同集合的信息限制"),
    ]
    table_b = []
    distributions = [
        ("total_pancreas_imaging_records", "total_pancreas_imaging_records", "全期胰腺相关影像报告记录数；不是DICOM检查数"),
        ("all_cross_sectional_days", "first_year_all_cross_sectional_days", "CT/MRI/MRCP/EUS不同检查日"),
        ("CT_days", "first_year_ct_days", "首年CT不同检查日"),
        ("MR_family_days", "first_year_mr_family_days", "首年MRI/MRCP合并不同检查日；未证明协议可比"),
        ("numeric_cyst_size_days", "first_year_numeric_size_days", "首年有报告囊肿数值的不同检查日；未匹配病灶"),
        ("numeric_MPD_days", "first_year_numeric_mpd_days", "首年有报告MPD数值的不同检查日；未匹配部位"),
    ]
    for scope, scope_rows in (("N3", n3), ("N4_proxy", n4)):
        for label, field, definition in distributions:
            table_b.extend(distribution_rows(label, [row[field] for row in scope_rows], scope, "report_level", definition))
        for field, label in (("first_year_all_median_interval_days", "all_cross_sectional_interval_days"), ("first_year_ct_median_interval_days", "CT_interval_days"), ("first_year_mr_median_interval_days", "MR_family_interval_days")):
            values = [row[field] for row in scope_rows if row[field] != ""]
            for category, value in (("median_of_patient_medians", statistics.median(values) if values else ""), ("Q1", percentile(values, 0.25)), ("Q3", percentile(values, 0.75)), ("patients_with_interval", len(values))):
                table_b.append({"metric": label, "scope": scope, "category": category, "count": value, "status": "actual_date_difference", "definition": "同患者首年相邻检查日的实际日期差；先取患者中位数再汇总"})
    for field, label, definition in [
        ("repeat_same_modality_2", "at_least_2_same_modality_days", "CT>=2或MR家族>=2"),
        ("repeat_same_modality_3", "at_least_3_same_modality_days", "CT>=3或MR家族>=3"),
        ("repeat_numeric_size_same_modality", "repeat_numeric_size_same_modality", "同模态至少2次囊肿数值；非同病灶证明"),
        ("repeat_numeric_mpd_same_modality", "repeat_numeric_mpd_same_modality", "同模态至少2次MPD数值；非同部位证明"),
        ("repeat_mpd_status_same_modality", "repeat_mpd_status_same_modality", "同模态至少2次明确MPD正常/扩张状态；未提及不作正常"),
    ]:
        for scope, scope_rows in (("N3", n3), ("N4_proxy", n4)):
            table_b.append({"metric": label, "scope": scope, "category": "patients", "count": sum(row[field] for row in scope_rows), "status": "report_level", "definition": definition})
    for scope, scope_rows in (("N3", n3), ("N4_proxy", n4)):
        table_b.append({"metric": "entry_report_available_date", "scope": scope, "category": "nonblank", "count": sum(bool(row["entry_evidence_available_date"]) for row in scope_rows), "status": "source_coverage", "definition": "entry报告/临床可用日期非空；检查日不能替代可用日"})
    table_b.extend([
        {"metric": "DICOM_files_delivered", "scope": "all_archives", "category": "files", "count": 0, "status": "not_delivered", "definition": "ZIP清单无DICOM；不等于医院没有原始影像"},
        {"metric": "segmentation_masks_delivered", "scope": "all_archives", "category": "files", "count": 0, "status": "not_delivered", "definition": "ZIP清单无胰腺/囊肿/胰管mask"},
        {"metric": "verified_index_cyst_links", "scope": "all_archives", "category": "patients", "count": 0, "status": "not_available", "definition": "交付字段没有经审定的跨时点index cyst匹配"},
    ])
    table_c = []
    for period in ("before_entry", "entry_to_landmark", "post_landmark_3y", "after_3y", "date_missing"):
        for field, label in (
            ("target_event_counts_by_period", "IPMN_HGD_IC_pathology_candidates"),
            ("hgd_event_counts_by_period", "IPMN_HGD_only_pathology_candidates"),
            ("ipmn_ic_event_counts_by_period", "IPMN_derived_IC_pathology_candidates"),
            ("joint_target_cpdac_event_counts_by_period", "same_pathology_HGD_IC_plus_cPDAC_candidates"),
            ("cpdac_event_counts_by_period", "cPDAC_pathology_candidates"),
            ("unattributed_cancer_event_counts_by_period", "unattributed_pancreatic_cancer_candidates"),
            ("lgd_event_counts_by_period", "IPMN_LGD_pathology_candidates"),
            ("non_target_pathology_counts_by_period", "non_target_pancreas_pathology_candidates"),
        ):
            for scope, scope_rows in (("N3", n3), ("N6", n6)):
                count = sum(bool(row[field].get(period)) for row in scope_rows)
                table_c.append({"metric": label, "scope": scope, "period": period, "count": count, "status": "patient_count_requires_adjudication", "definition": "按患者计；取首次规则候选是否在窗口内，事件日优先取材日，缺失时以接收/报告日代理"})
    for field, label, definition in [
        ("contact_on_or_after_landmark", "any_contact_on_or_after_landmark", "任一文书、影像、病理或检验日期；不等于有效无事件随访"),
        ("postlandmark_3y_death_proxies", "death_document_title_proxies", "死亡文书标题及创建日代理；无完整死亡登记"),
        ("postlandmark_3y_resection_proxies", "pancreatic_resection_proxies", "手术记录日期或病理切除代理；范围与index病灶待核"),
        ("postlandmark_3y_total_resection_proxies", "total_pancreatectomy_proxies", "文本提示全胰切除；须先于竞争事件判定核对标本中是否有HGD/IC"),
        ("postlandmark_3y_non_total_resection_proxies", "partial_or_undetermined_resection_proxies", "未提示全胰的切除代理；部分切除与范围未明需进一步区分"),
    ]:
        for scope, scope_rows in (("N3", n3), ("N4_proxy", n4), ("N6", n6)):
            table_c.append({"metric": label, "scope": scope, "period": "landmark_to_3y" if field != "contact_on_or_after_landmark" else "on_or_after_landmark", "count": sum(bool(row[field]) for row in scope_rows), "status": "proxy_requires_adjudication", "definition": definition})
    for scope, scope_rows in (("N3", n3), ("N4_proxy", n4), ("N6", n6)):
        table_c.append({"metric": "prelandmark_malignant_imaging_review", "scope": scope, "period": "up_to_landmark", "count": sum(bool(row["prelandmark_malignant_imaging_review"]) for row in scope_rows), "status": "review_not_automatic_exclusion", "definition": "landmark前影像恶性措辞候选；须区分已知癌与基线疑似，疑似病变按v1.0保留"})
    table_c.extend([
        {"metric": "pathology_negative_unoperated", "scope": "all", "period": "all", "count": "not_estimable", "status": "prohibited_inference", "definition": "未手术/无病理不能自动视为LGD或无事件"},
        {"metric": "complete_vital_status_registry", "scope": "all", "period": "all", "count": 0, "status": "not_delivered", "definition": "未发现可闭合全队列生存状态的登记"},
        {"metric": "complete_loss_to_followup_reason", "scope": "all", "period": "all", "count": 0, "status": "not_delivered", "definition": "末次接触不能区分失访、院外随访、死亡或监测停止"},
    ])
    table_d = []
    variables = [
        ("repeat_numeric_size_same_modality", "cyst_size_signed_change", "同模态至少2次数值才可谈有符号变化；当前未计算增长率，未匹配同一囊肿"),
        ("repeat_numeric_mpd_same_modality", "MPD_numeric_change", "同模态至少2次MPD数值；部位、协议与测量误差待核"),
        ("repeat_mpd_status_same_modality", "MPD_status_longitudinal_coverage", "同模态至少2次明确正常/扩张状态；可支持状态变化审查，不等于数值变化率"),
        ("new_nodule_text_candidate", "new_mural_nodule_text_candidate", "先前明确阴性后出现明确阳性文本候选；非同病灶或临床确认"),
        ("nodule_explicit_present_first_year", "mural_nodule_present_text_candidate", "首年任一报告明确阳性候选；未提及不等于阴性"),
        ("ca199_first_year_days", "CA19_9_at_least_1", "首年至少1个采样日；胆红素语境另列"),
        ("ca199_first_year_days", "CA19_9_at_least_2", "首年至少2个采样日；只是动态变量的最低覆盖"),
        ("bilirubin_first_year_days", "bilirubin_at_least_1", "首年至少1个总/直接/间接胆红素采样日"),
        ("hba1c_first_year_days", "HbA1c_at_least_1", "首年至少1个糖化血红蛋白采样日"),
        ("hba1c_first_year_days", "HbA1c_at_least_2", "首年至少2个糖化血红蛋白采样日；只是动态变量的最低覆盖"),
        ("diabetes_text_first_year_days", "diabetes_text_mention", "首年文书提及糖尿病；未确诊日期或新发状态"),
        ("age_in_analysis_only_research_rows", "age_in_legacy_research_rows", "既有研究行有年龄；属于analysis_only，需回源核验"),
        ("sex_in_analysis_only_research_rows", "sex_in_legacy_research_rows", "既有研究行有性别；属于analysis_only，需回源核验"),
    ]
    for field, label, definition in variables:
        for scope, scope_rows in (("N3", n3), ("N4_proxy", n4), ("N5_repeat_same_modality", n5)):
            threshold = 2 if label.endswith("at_least_2") else 1
            count = sum((row[field] >= threshold if isinstance(row[field], int) and not isinstance(row[field], bool) else bool(row[field])) for row in scope_rows)
            table_d.append({"variable": label, "scope": scope, "available_patients": count, "denominator": len(scope_rows), "coverage": round(count / len(scope_rows), 6) if scope_rows else "", "status": "component_coverage_not_frozen_M1", "definition": definition})
    table_d.append({"variable": "Kyoto_HRS_WF_landmark_reconstruction", "scope": "N4_proxy", "available_patients": "not_estimable", "denominator": len(n4), "coverage": "", "status": "not_yet_constructable", "definition": "当前组件覆盖不能替代完整landmark时点HRS/WF；黄疸、症状、EUS强化结节等尚需审定"})
    return table_a, table_b, table_c, table_d, {"N3": n3, "N4_upper": n4_upper, "N4_proxy": n4, "N5": n5, "N5_size": n5_size, "N6": n6}


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(rows)


def data_dictionary():
    definitions = [
        ("source.research_id", "去标识研究ID；仅用于本地回链", "队列索引.csv", "不得公开"),
        ("source.entry_date", "病理前首次合格IPMN影像主诊断检查日", "L2全部索引.csv", "不等于报告可用日"),
        ("source.baseline_subtype", "entry当日及之前规则分型", "L2全部索引.csv", "UNTYPED_MPD_NORMAL不是明确BD文字诊断"),
        ("source.exam_date", "影像检查日", "imaging_all", "报告可用日期大量缺失"),
        ("source.pathology_dates", "取材/接收/报告日期分别保留", "pathology_all", "事件优先取材日；代理日期需标记"),
        ("source.laboratory_times", "采样与报告时间", "laboratory_all", "首年覆盖按采样日"),
        ("derived.landmark_date", "entry加365日", "程序派生", "严格固定365日"),
        ("derived.three_year_end", "landmark加1095日", "程序派生", "3个365日；右删失允许，不要求人人满3年"),
        ("derived.baseline_bd_rule_candidate", "明确BD或未分型且MPD正常", "程序派生", "规则候选，不是医生审定"),
        ("derived.landmark_eligibility_rule_proxy", "无规则癌/死亡/切除代理且landmark后有接触", "程序派生", "保守代理，不是N4临床真值"),
        ("derived.first_year_ct_days", "entry至landmark的CT不同检查日数", "程序派生", "同日多报告去重；协议未核"),
        ("derived.first_year_mr_family_days", "entry至landmark的MRI/MRCP不同检查日并集", "程序派生", "未证明MRI与MRCP协议可比"),
        ("derived.repeat_same_modality_2", "CT>=2或MR家族>=2", "程序派生", "只表示观测次数"),
        ("derived.repeat_numeric_size_same_modality", "同模态至少2日有囊肿数值", "程序派生", "未证明同一index cyst或测量误差合格"),
        ("derived.repeat_numeric_mpd_same_modality", "同模态至少2日有MPD数值", "程序派生", "未证明同部位或协议可比"),
        ("derived.new_nodule_text_candidate", "同模态先明确无结节后明确有结节", "程序派生", "文本候选，未确认新发"),
        ("derived.contact_on_or_after_landmark", "landmark后任一文书/影像/病理/检验日期", "程序派生", "最低接触证据，不等于有效无事件随访"),
        ("derived.target_event_counts_by_period", "IPMN相关HGD/IC病理规则候选按窗口计数", "程序派生", "必须盲态病理归属审定"),
        ("derived.cpdac_event_counts_by_period", "cPDAC病理规则候选按窗口计数", "程序派生", "必须审定与IPMN空间/病理关系"),
        ("derived.unattributed_cancer_event_counts_by_period", "来源不明胰腺恶性病理候选", "程序派生", "不得自动并入主结局或cPDAC"),
        ("derived.postlandmark_3y_death_proxies", "死亡标题文书创建日候选", "程序派生", "不是完整死亡登记"),
        ("derived.postlandmark_3y_resection_proxies", "手术记录或病理切除候选", "程序派生", "需核真实手术日、全/部分切除及index病灶"),
        ("derived.manual_review_reasons", "触发人工核对的原因", "程序派生", "原因可多项并存"),
        ("derived.manual_review_refs", "分支名与记录序号", "程序派生", "回链本地json.gz，不含正文摘录"),
        ("derived.age_in_analysis_only_research_rows", "旧研究行年龄可用标记", "程序派生", "旧研究值未回源核验"),
        ("derived.sex_in_analysis_only_research_rows", "旧研究行性别可用标记", "程序派生", "旧研究值未回源核验"),
        ("delivered.DICOM", "原始医学影像文件", "ZIP清单", "本次交付为0；不能推断医院不存在"),
        ("delivered.mask", "胰腺/囊肿/胰管分割", "ZIP清单", "本次交付为0"),
    ]
    return [{"field": field, "definition": definition, "source": source, "limitation": limitation} for field, definition, source, limitation in definitions]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_report(table_a, table_b, table_c, table_d, summary):
    lines = [
        "# IPMN v1.0 Pre-study Data Readiness Audit v0",
        "",
        "本报告是只读规则审计，不是临床队列冻结、结局审定、表型筛选或模型检验。所有N4/N5/N6与事件均保留相应代理状态。",
        "",
        "## 表A：人群与landmark flow",
        "",
        "| 阶段 | 数量 | 分母 | 状态 | 定义 |",
        "|---|---:|---|---|---|",
    ]
    lines += [f"| {row['stage']} | {row['count']} | {row['denominator']} | {row['status']} | {row['definition']} |" for row in table_a]
    lines += ["", "## 表B：首年纵向影像密度（N4规则代理集）", "", "| 指标 | 分类 | 数量 | 定义 |", "|---|---|---:|---|"]
    selected_b = [row for row in table_b if row["scope"] in ("N4_proxy", "all_archives")]
    lines += [f"| {row['metric']} | {row['category']} | {row['count']} | {row['definition']} |" for row in selected_b]
    lines += ["", "## 表C：结局与随访（患者数）", "", "| 指标 | 范围 | 窗口 | 数量 | 状态 |", "|---|---|---|---:|---|"]
    selected_c = [row for row in table_c if row["scope"] in ("N6", "all")]
    lines += [f"| {row['metric']} | {row['scope']} | {row['period']} | {row['count']} | {row['status']} |" for row in selected_c]
    lines += ["", "## 表D：M1组件可构建性（N4规则代理集）", "", "| 变量 | 可用人数/分母 | 覆盖率 | 定义 |", "|---|---:|---:|---|"]
    selected_d = [row for row in table_d if row["scope"] == "N4_proxy"]
    lines += [f"| {row['variable']} | {row['available_patients']}/{row['denominator']} | {row['coverage']} | {row['definition']} |" for row in selected_d]
    n6_events = next(row["count"] for row in table_a if row["stage"] == "E_postlandmark_3y_target_pathology_candidates")
    n6_count = next(row["count"] for row in table_a if row["stage"] == "N6_minimal_outcome_trace")
    n5_size = next(row["count"] for row in table_a if row["stage"] == "N5_repeat_numeric_cyst_size")
    lines += [
        "",
        "## 初步可行性判断",
        "",
        f"- 报告驱动路径：N4规则代理集中有 {n5_size} 人具备同模态重复囊肿数值的最低覆盖，但同一index cyst、协议与测量误差均未闭合。",
        f"- 临床确认路径：最低结局追踪代理N6为 {n6_count} 人，其中主事件病理规则候选为 {n6_events} 人；该数可在人工审定后下降，不能当作功效或M1/M2可估计性的结论。",
        "- 原始影像路径：当前包无DICOM、无mask、无已核定跨时点病灶匹配，不能直接启动以原始影像为基础的主表型发现。",
        "- 当前最接近路径D（报告/检验驱动的纵向表型可行性审计），并需警惕路径B（纵向资料存在但临床事件或随访信息不足）。这不是v1.0第17节A-D结案。",
        "- 下一步应先完成人工结局/手术/死亡与index cyst抽样复核，再由隔离统计角色评价真实事件预算；不得在候选开发阶段查看候选表型与癌结局的关联。",
        "",
        "## 关键限制",
        "",
        "- landmark固定为entry后365日，主窗口终点为landmark后1095日；右删失允许，不要求每人完成3年。", 
        "- 报告可用日期大量缺失，首年覆盖主要按检查日；临床当时可用性仍需补全。",
        "- 规则分类沿用交付包上游原语并作本地适配；病理候选、阴性表述、手术与死亡均非临床Reference。",
        "- 末次接触不能区分失访、院外随访、死亡或停止监测；无病理者不能算LGD或无事件。",
        "- M1表只表示组件覆盖，不表示已冻结或可拟合M1；完整Kyoto HRS/WF尚未重建。",
        "",
        "## 运行信息",
        "",
        f"- 审计日期：{summary['as_of_date']}",
        f"- 实际来源最大接触日期：{summary['observed_data_cutoff']}",
        f"- entry不晚于审计日前4个日历年的人数：{summary['entry_at_least_4y_before_as_of_n3']}",
        f"- 人工核对队列人数：{summary['manual_review_patients']}",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Read-only IPMN v1.0 data-readiness audit")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--review-file", type=Path, help="Structured readiness reviews; never used as candidate-discovery input")
    args = parser.parse_args()
    as_of = parse_date(args.as_of)
    if not as_of:
        raise ValueError("--as-of must be an ISO date")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    l1, l2 = source_indexes(args.source_root)
    rows = []
    with ZipFile(args.source_root / "L1主队列.zip") as archive:
        names = [name for name in archive.namelist() if name.endswith(".json.gz") and not Path(name).name.startswith("._")]
        for position, name in enumerate(names, 1):
            research_id = Path(name).name.removesuffix(".json.gz")
            with archive.open(name) as raw, gzip.open(raw, "rt", encoding="utf-8") as handle:
                patient = json.load(handle)
            rows.append(patient_audit(patient, l1[research_id], l2[research_id]))
            if position % 500 == 0:
                print(f"Audited {position}/{len(names)} patients", flush=True)
    if len(rows) != len(l1) or {row["research_id"] for row in rows} != set(l1):
        raise ValueError("Patient archives do not exactly cover the L1 index")
    table_a, table_b, table_c, table_d, scopes = build_tables(rows, {"N0": len(l2), "N1": len(l1)})
    write_csv(args.output_dir / "table_A_population_landmark_flow.csv", table_a)
    write_csv(args.output_dir / "table_B_longitudinal_imaging_density.csv", table_b)
    write_csv(args.output_dir / "table_C_outcomes_followup.csv", table_c)
    write_csv(args.output_dir / "table_D_M1_constructability.csv", table_d)
    write_csv(args.output_dir / "data_dictionary.csv", data_dictionary())
    review_fields = ("research_id", "entry_date", "landmark_date", "baseline_subtype", "L2_route", "manual_review_reasons", "manual_review_refs")
    review_rows = [{key: "|".join(row[key]) if isinstance(row[key], list) else row[key] for key in review_fields} for row in rows if row["manual_review_reasons"]]
    write_csv(args.output_dir / "manual_review_queue.csv", review_rows)
    with gzip.open(args.output_dir / "patient_audit.jsonl.gz", "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    observed_cutoff = max((row["last_contact_date"] for row in rows if row["last_contact_date"]), default="")
    summary = {
        "audit_version": "ipmn_data_readiness_v0",
        "created_at": datetime.now().astimezone().isoformat(),
        "as_of_date": as_of.isoformat(),
        "observed_data_cutoff": observed_cutoff,
        "source_root": str(args.source_root),
        "source_patients": len(rows),
        "entry_at_least_4y_before_as_of_n3": sum(bool(row["entry_date"] and (parse_date(row["entry_date"]) + timedelta(days=4 * 365)) <= as_of) for row in scopes["N3"]),
        "manual_review_patients": len(review_rows),
        "table_A": table_a,
        "N4_proxy_entry_year_counts": dict(sorted(Counter(row["entry_date"][:4] for row in scopes["N4_proxy"]).items())),
        "N4_proxy_full_4y_calendar_opportunity_by_as_of": sum(bool((parse_date(row["entry_date"]) + timedelta(days=4 * 365)) <= as_of) for row in scopes["N4_proxy"]),
        "N6_full_4y_calendar_opportunity_by_as_of": sum(bool((parse_date(row["entry_date"]) + timedelta(days=4 * 365)) <= as_of) for row in scopes["N6"]),
        "N6_contact_at_or_after_individual_3y_end": sum(bool(row["last_contact_date"] and parse_date(row["last_contact_date"]) >= parse_date(row["three_year_end"])) for row in scopes["N6"]),
        "blinding": "Only aggregate event counts and component coverage; no phenotype-outcome associations, Cox models, feature selection, HMM, or outcome-ranked candidates.",
    }
    code_paths = [
        Path(__file__),
        Path(__file__).with_name("rules.py"),
        Path(__file__).with_name("handoff.py"),
        Path(__file__).with_name("candidate.py"),
        Path(__file__).with_name("measurement.py"),
        SCHEMA_PATH,
    ]
    summary["code_sha256"] = {path.name: sha256(path) for path in code_paths}
    summary["source_sha256"] = {name: sha256(args.source_root / name) for name in ("L1主队列.zip", "索引与说明.zip", "纳入规则与使用说明.md")}
    reviews = json.loads(args.review_file.read_text(encoding="utf-8")) if args.review_file else None
    handoff = build_readiness_handoff(summary, reviews)
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "readiness_handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "feasibility_report.md").write_text(render_report(table_a, table_b, table_c, table_d, summary), encoding="utf-8")
    print(json.dumps(summary["table_A"], ensure_ascii=False, indent=2))
    print(f"Outputs: {args.output_dir}")


if __name__ == "__main__":
    main()

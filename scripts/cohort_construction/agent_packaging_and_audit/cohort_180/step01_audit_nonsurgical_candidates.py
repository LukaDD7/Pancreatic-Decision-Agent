from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from scripts.cohort_construction.paths import data_root
from scripts.cohort_construction.cohort_751_851_180 import step03_build_preop_decision_180 as base

ROOT = data_root()


PLAN_RULES = [
    (
        "补充分期影像",
        re.compile(r"(?:建议|需|完善|进一步|复查)[^。；\n]{0,70}(?:肝脏)?(?:MR|MRI|磁共振|增强CT|PET)", re.I),
    ),
    (
        "穿刺或病理确认",
        re.compile(r"(?:拟|计划|建议|考虑|决定|定于)[^。；\n]{0,80}(?:穿刺|活检|EUS|FNA)", re.I),
    ),
    (
        "胆道或介入处理",
        re.compile(r"(?:拟|计划|建议|考虑|决定|定于)[^。；\n]{0,80}(?:ERCP|PTCD|PTBD|ENBD|支架|引流)", re.I),
    ),
    (
        "系统治疗",
        re.compile(r"(?:拟|计划|建议|考虑|决定|予|选择|开始|继续)[^。；\n]{0,100}(?:新辅助|化疗|放疗|免疫治疗|靶向治疗)", re.I),
    ),
]


def actual_event(category, anchor, documents, imaging):
    end = anchor + timedelta(days=30)
    if category == "补充分期影像":
        matches = [
            row
            for row in imaging
            if anchor < row["exam_time"] <= end
            and re.search(r"MR|MRI|磁共振|CT|PET", row["exam_method"], re.I)
        ]
        if matches:
            row = sorted(matches, key=lambda item: item["exam_time"])[0]
            return row["exam_time"], "imaging", row["source_record_key"]
        return None
    terms = {
        "穿刺或病理确认": re.compile(r"穿刺|活检|EUS|FNA", re.I),
        "胆道或介入处理": re.compile(r"ERCP|PTCD|PTBD|ENBD|支架|引流", re.I),
        "系统治疗": re.compile(r"化疗记录|予[^。；\n]{0,80}(?:化疗|放疗|免疫治疗|靶向治疗)|第\s*\d+\s*周期", re.I),
    }[category]
    matches = [
        row
        for row in documents
        if anchor < row["create_time"] <= end
        and terms.search(row["title"] + " " + row["text"])
        and not base.PLAN_TITLE.search(row["title"])
    ]
    if not matches:
        return None
    row = sorted(matches, key=lambda item: item["create_time"])[0]
    return row["create_time"], "document_create_time_proxy", row["source_record_id"]


def main():
    bundles = base.load_bundles()
    patient_ids = {base.clean(bundle["patient"]["patient_id"]) for bundle in bundles}
    documents = base.load_documents(patient_ids)
    imaging = base.load_imaging(patient_ids)
    docs_by = defaultdict(list)
    images_by = defaultdict(list)
    for row in documents:
        docs_by[row["patient_id"]].append(row)
    for row in imaging:
        images_by[row["patient_id"]].append(row)
    old_ids, _ = base.old100_metadata()
    counts = Counter()
    patients = defaultdict(set)
    examples = defaultdict(list)
    for patient_id in sorted(patient_ids - old_ids):
        docs = sorted(docs_by[patient_id], key=lambda item: item["create_time"])
        images = images_by[patient_id]
        for row in docs:
            if base.POST_TITLE.search(row["title"]):
                continue
            for category, pattern in PLAN_RULES:
                match = pattern.search(row["text"])
                if not match:
                    continue
                pre_images = [image for image in images if row["create_time"] - timedelta(days=180) <= image["exam_time"] < row["create_time"]]
                if not pre_images:
                    continue
                actual = actual_event(category, row["create_time"], docs, images)
                if not actual:
                    continue
                key = (patient_id, category)
                if patient_id in patients[category]:
                    continue
                counts[category] += 1
                patients[category].add(patient_id)
                if len(examples[category]) < 3:
                    examples[category].append(
                        {
                            "patient_id": patient_id,
                            "plan_time": base.iso(row["create_time"]),
                            "plan_title": row["title"],
                            "plan_text": match.group(0)[:300],
                            "actual_time": base.iso(actual[0]),
                            "actual_basis": actual[1],
                            "actual_source": actual[2],
                        }
                    )
                break
    print(json.dumps({"counts": counts, "examples": examples}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

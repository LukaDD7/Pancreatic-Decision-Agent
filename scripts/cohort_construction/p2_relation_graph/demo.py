"""Invented examples, never human gold or patient-derived text."""
from scripts.cohort_construction.p2_decision_ir.extract import Extractor
import json
from pathlib import Path


def data():
    texts = [
        "目前胆红素偏高，拟行PTCD，待黄疸改善后再评估手术条件。",
        "今日重新评估：PTCD引流不畅，取消此前PTCD安排，改行ERCP，待黄疸改善后再评估手术条件。",
    ]
    notes = [{"patient_uid": "SYNTHETIC_ONLY", "record_key": f"fictional-{i}",
              "visit_id": "fictional-visit", "text": text, "document_type": "医师查房记录",
              "family": "WARD_ROUND", "body_time": f"2025-01-0{i+1} 10:00:00",
              "system_time": f"2025-01-0{i+1} 10:00:00"} for i, text in enumerate(texts)]
    config = Path(__file__).parents[1]/"p2_decision_localization/configs/rules-v0.3.json"
    extractor = Extractor(json.loads(config.read_text()))
    units = [u for n in notes for u in extractor.extract(n)]
    return notes, units

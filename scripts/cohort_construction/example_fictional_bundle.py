"""Build a minimal patient bundle from entirely fictional source records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.cohort_construction.agent_packaging_and_audit.shared.stage8a_models import (
    build_event_uid,
    build_report_uid,
    deduplicate_reports,
    sha256_text,
)
from scripts.cohort_construction.source_semantics import resolve_available_time, visible_before


def build_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    from scripts.cohort_construction.source_semantics import parse_datetime

    decision_time = parse_datetime(payload["decision_time"])
    if decision_time is None:
        raise ValueError("decision_time must be ISO-8601")

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in payload["records"]:
        source_type = raw["source_type"]
        resolved = resolve_available_time(raw, source_type)
        trace = {
            "source_system": raw["source_system"],
            "source_file": raw["source_file"],
            "source_sheet": raw.get("source_sheet", ""),
            "source_row": raw.get("source_row", ""),
            "source_record_id": raw["source_record_id"],
            "source_record_key": raw["source_record_key"],
        }
        if not visible_before(resolved, decision_time):
            excluded.append(
                {
                    **trace,
                    "reason": "not_provably_visible_before_decision",
                    "time_field": resolved["field"],
                    "time_precision": resolved["precision"],
                }
            )
            continue
        event_time = resolved["value"].isoformat(sep=" ", timespec="seconds")
        event_uid = build_event_uid(
            payload["patient_uid"], raw["source_record_key"], raw["event_type"], event_time
        )
        content_hash = sha256_text(raw["text"])
        included.append(
            {
                **trace,
                "patient_uid": payload["patient_uid"],
                "event_uid": event_uid,
                "content_hash": content_hash,
                "report_uid": build_report_uid(
                    payload["patient_uid"], event_uid, content_hash, raw["source_system"]
                ),
                "report_type": raw["report_type"],
                "event_type": raw["event_type"],
                "clinical_time": raw.get("clinical_time", ""),
                "available_time": event_time,
                "available_time_field": resolved["field"],
                "available_time_status": resolved["status"],
                "available_time_precision": resolved["precision"],
                "text": raw["text"],
            }
        )

    reports, source_map = deduplicate_reports(included)
    return {
        "schema_version": "fictional_patient_bundle_v1",
        "patient_uid": payload["patient_uid"],
        "decision_time": decision_time.isoformat(sep=" ", timespec="seconds"),
        "reports": reports,
        "source_map": source_map,
        "visible_records": included,
        "excluded_records": excluded,
        "labels": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = build_bundle(json.loads(args.input.read_text(encoding="utf-8")))
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import importlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts.cohort_construction.example_fictional_bundle import build_bundle
from scripts.cohort_construction.source_semantics import resolve_available_time, visible_before


ROOT = Path(__file__).resolve().parents[1]


def test_laboratory_availability_does_not_use_sample_time_first():
    resolved = resolve_available_time(
        {
            "sample_time": "2026-01-01 08:00:00",
            "report_time": "2026-01-01 11:00:00",
            "available_time": "2026-01-01 12:00:00",
        },
        "laboratory",
    )
    assert resolved["field"] == "available_time"
    assert resolved["value"] == datetime(2026, 1, 1, 12)


def test_date_only_same_day_is_not_provably_visible():
    resolved = resolve_available_time({"report_time": "2026-01-15"}, "pathology")
    assert resolved["precision"] == "date_only"
    assert not visible_before(resolved, datetime(2026, 1, 15, 10))


def test_fictional_example_separates_sources_and_exclusions():
    payload = json.loads((ROOT / "examples" / "fictional_raw_records.json").read_text(encoding="utf-8"))
    bundle = build_bundle(payload)
    assert len(bundle["reports"]) == 2
    assert len(bundle["excluded_records"]) == 2
    assert {row["source_system"] for row in bundle["source_map"]} == {"fictional_ris", "fictional_lis"}
    assert all(row["source_file"].startswith("fictional_") for row in bundle["source_map"])
    assert all(row["text"].startswith("虚构示例") for row in bundle["visible_records"])
    assert bundle["labels"] == {}


def test_pathology_reader_uses_every_parquet_shard(monkeypatch, tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    pytest.importorskip("pandas")
    monkeypatch.setenv("PANCREATIC_DATA_ROOT", str(tmp_path))
    module = importlib.import_module(
        "scripts.cohort_construction.agent_packaging_and_audit.cohort_100.step01_build_patient_states_100"
    )
    root = tmp_path / "pathology"
    root.mkdir()
    pq.write_table(pa.table({"source_record_key": ["a"], "病人编号": ["FICTIONAL-A"]}), root / "part-1.parquet")
    pq.write_table(pa.table({"source_record_key": ["b"], "病人编号": ["FICTIONAL-B"]}), root / "part-2.parquet")
    monkeypatch.setattr(module, "PATHOLOGY_ROOT", root)
    table = module.read_pathology_table(["source_record_key", "病人编号"])
    assert table.num_rows == 2
    assert set(table["source_record_key"].to_pylist()) == {"a", "b"}


def test_document_archival_time_cannot_make_later_body_visible():
    resolved = resolve_available_time({"create_time": "2026-01-14 09:00:00",
                                      "document_completion_time": "2026-01-16 11:00:00"}, "document")
    assert resolved["field"] == "document_completion_time"
    assert not visible_before(resolved, datetime(2026, 1, 15, 10))


def test_document_missing_body_completion_has_no_archival_fallback():
    resolved = resolve_available_time({"create_time": "2026-01-14 09:00:00",
                                      "event_time_used": "2026-01-14 09:00:00"}, "document")
    assert resolved["status"] == "unknown"
    assert not visible_before(resolved, datetime(2026, 1, 15, 10))

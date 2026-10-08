from data_pipeline.report_index.imaging_fields import split_imaging_source_fields
from data_pipeline.stage5_sources.document_stream_reader import build_record
from data_pipeline.stage5_sources.imaging_record_cluster import IMAGING_COLUMNS
from data_pipeline.stage5_sources.lab_result_parser import parse_result
from data_pipeline.stage7_timeline.stage7_models import day_id, event_id


def test_document_record_keeps_source_and_system_create_time():
    record = build_record(
        [
            "FICTIONAL-P001",
            "FICTIONAL-V001",
            "2026-01-01 08:00:00",
            "2026-01-03 10:00:00",
            "入院记录",
            "2026-01-01 09:30:00",
            "完全虚构的文书正文",
        ],
        source_file="fictional_documents.csv",
        source_row=2,
    )
    assert record["create_time"].isoformat(sep=" ") == "2026-01-01 09:30:00"
    assert record["source_record_id"] == "fictional_documents.csv:2"
    assert record["parser_status"] == "OK"


def test_laboratory_parser_preserves_operator_without_clinical_inference():
    result = parse_result(">120.5")
    assert result["result_type"] == "QUALIFIED_NUMERIC"
    assert result["result_operator"] == ">"
    assert result["result_numeric_value"] == 120.5


def test_imaging_source_fields_keep_result_class_separate():
    row = [""] * len(IMAGING_COLUMNS)
    row[6] = "胰腺CT平扫+增强"
    row[8] = "完全虚构的检查所见"
    row[13] = "完全虚构的影像印象"
    row[14] = "阴性"
    result = split_imaging_source_fields(row)
    assert result["source_result_class"] == "阴性"
    assert result["source_result_class_is_model_generated"] is False
    assert "阴性" not in result["report_text"]


def test_stage7_identifiers_are_deterministic():
    assert day_id("FICTIONAL-UID", "2026-01-01") == day_id("FICTIONAL-UID", "2026-01-01")
    assert event_id("imaging", "fictional-key", "IMAGING_EXAM") == event_id(
        "imaging", "fictional-key", "IMAGING_EXAM"
    )

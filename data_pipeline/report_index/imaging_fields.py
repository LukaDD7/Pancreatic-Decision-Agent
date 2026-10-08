"""Semantic mapping for the 15-column imaging-report export.

The source column names are counter-intuitive in the current hospital export:
column 9 contains the long findings text, column 14 contains the impression,
and column 15 is a source-system positive/negative class.  The last field is
not report prose and must not be presented as a model-generated diagnosis.
"""

from __future__ import annotations

from typing import Any, Sequence

from data_pipeline.stage5_sources.imaging_record_cluster import IMAGING_COLUMNS


RULE_VERSION = "imaging_source_field_semantics_v2"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def split_imaging_source_fields(tokens: Sequence[Any]) -> dict[str, Any]:
    if len(tokens) < len(IMAGING_COLUMNS):
        raise ValueError(f"expected {len(IMAGING_COLUMNS)} imaging columns, got {len(tokens)}")
    values = [_text(value) for value in tokens[: len(IMAGING_COLUMNS)]]
    findings = values[8]
    impression = values[13]
    result_class = values[14]
    return {
        "exam_method": values[6],
        "findings_text": findings,
        "findings_source_column": IMAGING_COLUMNS[8],
        "impression_text": impression,
        "impression_source_column": IMAGING_COLUMNS[13],
        "source_result_class": result_class,
        "source_result_class_column": IMAGING_COLUMNS[14],
        "source_result_class_is_model_generated": False,
        "report_text": "\n".join(part for part in (findings, impression) if part),
        "rule_search_text": "\n".join(part for part in (values[6], findings, impression) if part),
        "rule_version": RULE_VERSION,
    }

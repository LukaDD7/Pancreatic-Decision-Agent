"""Schema constants for the controlled admission/discharge document L1 pilot."""

from __future__ import annotations

from typing import Final


RAW_FIELDS: Final[tuple[str, ...]] = (
    "PATIENT_ID",
    "VISIT_ID",
    "ADMISSION_DATE_TIME",
    "DISCHARGE_DATE_TIME",
    "文书名称",
    "CREATE_DATE_TIME",
    "文书内容",
)

RAW_TIME_FIELDS: Final[tuple[str, ...]] = (
    "ADMISSION_DATE_TIME",
    "DISCHARGE_DATE_TIME",
    "CREATE_DATE_TIME",
)

PARSED_TIME_FIELDS: Final[tuple[str, ...]] = (
    "admission_time",
    "discharge_time",
    "create_time",
)

INGESTION_FIELDS: Final[tuple[str, ...]] = (
    "source_file",
    "source_row",
    "source_record_id",
    "row_hash",
    "parser_status",
    "ingestion_version",
)

AUDIT_FIELDS: Final[tuple[str, ...]] = (
    "encounter_interval_status",
    "content_length",
    "physical_line_count",
    "content_sha256",
)

INGESTION_VERSION: Final[str] = "document_l1_restricted_v1"
PILOT_ROW_LIMIT: Final[int] = 10_000
DEFAULT_BATCH_SIZE: Final[int] = 10_000
SOURCE_ENCODING: Final[str] = "gb18030"


def expected_header() -> tuple[str, ...]:
    return RAW_FIELDS

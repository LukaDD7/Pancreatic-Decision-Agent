"""Schema constants for the controlled laboratory L1 ingestion pilot."""

from __future__ import annotations

from typing import Final

RAW_FIELDS: Final[tuple[str, ...]] = (
    "PATIENT_ID",
    "VISIT_ID",
    "NAME",
    "检验项目名称",
    "检验结果值",
    "检验结果单位",
    "结果正常标志",
    "送检时间",
    "报告时间",
    "检验参考值",
    "检验单号",
    "标本",
)

IDENTIFIER_FIELDS: Final[frozenset[str]] = frozenset(
    {"PATIENT_ID", "VISIT_ID", "检验单号"}
)
TIMESTAMP_FIELDS: Final[frozenset[str]] = frozenset({"送检时间", "报告时间"})
INGESTION_FIELDS: Final[tuple[str, ...]] = (
    "source_workbook",
    "source_sheet",
    "source_row",
    "source_record_id",
    "row_hash",
    "ingestion_version",
)
INGESTION_VERSION: Final[str] = "lab_ingestion_v1"
PILOT_SHEETS: Final[tuple[str, ...]] = ("Sheet1", "Sheet1(12)")
PILOT_ROW_LIMIT: Final[int] = 100_000
DEFAULT_BATCH_SIZE: Final[int] = 25_000


def expected_header() -> tuple[str, ...]:
    return RAW_FIELDS

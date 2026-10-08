"""Excel-1900 time normalization with explicit missing and order states."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any


TIME_RULE_VERSION = "excel1900_1899-12-30_v1"
EXCEL_EPOCH = datetime(1899, 12, 30)


def excel_serial_to_datetime(value: int | float) -> datetime | None:
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(serial) or serial < 0:
        return None
    return EXCEL_EPOCH + timedelta(days=serial)


def datetime_to_excel_serial(value: date | datetime) -> float:
    current = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
    return (current - EXCEL_EPOCH).total_seconds() / 86400.0


def parse_excel_datetime(value: Any) -> tuple[float | None, datetime | None, str]:
    if value is None:
        return None, None, "EMPTY"
    if isinstance(value, datetime):
        return datetime_to_excel_serial(value), value, "PARSED"
    if isinstance(value, date):
        current = datetime.combine(value, datetime.min.time())
        return datetime_to_excel_serial(current), current, "PARSED"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = float(value)
        parsed = excel_serial_to_datetime(serial)
        if parsed is None:
            return serial, None, "INVALID_EXCEL_DATETIME"
        return serial, parsed, "PARSED"
    return None, None, "INVALID_EXCEL_DATETIME"


def normalize_time_pair(specimen_value: Any, report_value: Any) -> dict[str, Any]:
    specimen_serial, specimen_time, specimen_status = parse_excel_datetime(specimen_value)
    report_serial, report_time, report_status = parse_excel_datetime(report_value)
    if "INVALID_EXCEL_DATETIME" in (specimen_status, report_status):
        status = "INVALID_EXCEL_DATETIME"
    elif specimen_time is not None and report_time is not None and specimen_time > report_time:
        status = "INVALID_SPECIMEN_REPORT_ORDER"
    elif specimen_time is None and report_time is None:
        status = "EMPTY"
    elif specimen_time is None:
        status = "MISSING_SPECIMEN_TIME"
    elif report_time is None:
        status = "MISSING_REPORT_TIME"
    else:
        status = "PARSED"
    return {
        "raw_excel_serial": json.dumps(
            {"送检时间": specimen_serial, "报告时间": report_serial},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "event_time": specimen_time,
        "available_time": report_time,
        "time_parse_status": status,
        "time_rule_version": TIME_RULE_VERSION,
    }

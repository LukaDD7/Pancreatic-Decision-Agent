"""Shared source-time semantics for cohort construction.

The returned time is the best available proxy for when a fact could have been
seen.  It is intentionally kept separate from the clinical event time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


TIME_FIELD_PRIORITY = {
    "document": ("create_time", "event_time_used"),
    "laboratory": ("available_time", "report_time", "event_time_used"),
    "pathology": ("report_time", "available_time", "event_time_used"),
    "imaging": ("report_time", "available_time", "exam_datetime", "event_time_used"),
}

PROXY_FIELDS = {"event_time_used", "exam_datetime"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_datetime(value: Any) -> datetime | None:
    text = _text(value).replace("T", " ")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def time_precision(value: Any) -> str:
    text = _text(value).replace("T", " ")
    if not text:
        return "unknown"
    if len(text) <= 10:
        return "date_only"
    parsed = parse_datetime(text)
    if parsed and parsed.time() == datetime.min.time():
        return "midnight_or_date_only"
    return "datetime"


def resolve_available_time(record: dict[str, Any], source_type: str) -> dict[str, Any]:
    """Resolve availability time without treating clinical time as equivalent."""

    if source_type not in TIME_FIELD_PRIORITY:
        raise ValueError(f"unsupported source type: {source_type}")
    for field in TIME_FIELD_PRIORITY[source_type]:
        value = record.get(field)
        parsed = parse_datetime(value)
        if parsed is not None:
            return {
                "value": parsed,
                "field": field,
                "precision": time_precision(value),
                "status": "proxy" if field in PROXY_FIELDS else "recorded",
            }
    return {"value": None, "field": None, "precision": "unknown", "status": "unknown"}


def visible_before(
    resolved: dict[str, Any], decision_time: datetime, *, exclude_ambiguous_same_day: bool = True
) -> bool:
    value = resolved.get("value")
    if not isinstance(value, datetime) or value > decision_time:
        return False
    if (
        exclude_ambiguous_same_day
        and resolved.get("precision") in {"date_only", "midnight_or_date_only"}
        and value.date() == decision_time.date()
    ):
        return False
    return True

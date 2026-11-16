"""Build diagnostic events without treating exam time as report visibility."""
from __future__ import annotations

from copy import deepcopy
import re


_CLOCK_ROLES = ("exam_time", "report_time", "verified_time")


def _unknown_clock():
    return dict(value=None, precision="unknown")


def _infer_precision(value):
    if not isinstance(value, str) or not value:
        return "unknown"
    if len(value) == 16:
        return "minute"
    if len(value) >= 19:
        return "second"
    if len(value) == 10:
        return "date"
    return "unknown"


def _normalize_clock(raw):
    if isinstance(raw, dict):
        value = raw.get("value")
        clock = dict(value=value,
                     precision=raw.get("precision") or _infer_precision(value))
        if "raw" in raw:
            clock["raw"] = deepcopy(raw["raw"])
        return clock
    if isinstance(raw, str) and raw:
        return dict(value=raw, precision=_infer_precision(raw), raw=raw)
    return _unknown_clock()


def _combined_clock(role, resolved_sources):
    clocks = [_normalize_clock((source.get("clocks") or {}).get(role))
              for source in resolved_sources]
    present = [clock for clock in clocks if clock.get("value") is not None]
    if not present:
        return _unknown_clock()
    signatures = {(clock.get("value"), clock.get("precision")) for clock in present}
    if len(signatures) != 1:
        return _unknown_clock()
    result = deepcopy(present[0])
    if any(clock.get("raw") != result.get("raw") for clock in present):
        result.pop("raw", None)
    return result


def _clock_flags(resolved_sources):
    inconsistent = []
    missing = []
    for role in _CLOCK_ROLES:
        clocks = [_normalize_clock((source.get("clocks") or {}).get(role))
                  for source in resolved_sources]
        present = [clock for clock in clocks if clock.get("value") is not None]
        signatures = {(clock.get("value"), clock.get("precision")) for clock in present}
        if len(signatures) > 1:
            inconsistent.append(role)
        if len(present) != len(resolved_sources):
            missing.append(role)
    return inconsistent, missing


def build_diagnostic_events(sources, evidence_inventory):
    """Combine each evidence item's sources into an auditable diagnostic event.

    A clock is combined only when all supplied values agree on value and
    precision. Conflicting group clocks remain unknown; their original values
    stay available in ``source_clocks`` and are flagged in ``audit``.
    """
    source_by_id = {source.get("source_id"): source for source in sources
                    if source.get("source_id") is not None}
    events = []
    for item in evidence_inventory:
        requested_ids = list(item.get("source_ids") or ())
        source_ids = list(dict.fromkeys(requested_ids))
        duplicate_ids = sorted({source_id for source_id in requested_ids
                                if requested_ids.count(source_id) > 1})
        resolved_sources = [source_by_id[source_id] for source_id in source_ids
                            if source_id in source_by_id]
        missing_ids = [source_id for source_id in source_ids
                       if source_id not in source_by_id]
        source_clocks = {
            source["source_id"]: deepcopy(source.get("clocks") or {})
            for source in resolved_sources
        }
        combined = {role: _combined_clock(role, resolved_sources)
                    for role in _CLOCK_ROLES}
        inconsistent, missing_clocks = _clock_flags(resolved_sources)

        if combined["report_time"].get("value") is not None:
            visibility_clock = deepcopy(combined["report_time"])
            visibility_basis = "report_time"
        elif "report_time" not in inconsistent \
                and combined["verified_time"].get("value") is not None:
            visibility_clock = deepcopy(combined["verified_time"])
            visibility_basis = "verified_time_fallback"
        else:
            visibility_clock = _unknown_clock()
            visibility_basis = "unknown"

        events.append(dict(
            event_id=item.get("event_id"),
            discovery_unit_id=item.get("discovery_unit_id"),
            source_ids=source_ids,
            exam_time=combined["exam_time"],
            report_time=combined["report_time"],
            verified_time=combined["verified_time"],
            visibility_clock=visibility_clock,
            visibility_basis=visibility_basis,
            source_clocks=source_clocks,
            audit=dict(
                requested_source_ids=requested_ids,
                resolved_source_ids=[source["source_id"] for source in resolved_sources],
                duplicate_source_ids=duplicate_ids,
                missing_source_ids=missing_ids,
                clock_inconsistent_flags=inconsistent,
                clock_missing_flags=missing_clocks,
            ),
        ))
    return events


def diagnostic_event_map(events):
    return {event["event_id"]: event for event in events}


def derive_preoperative_task_bounds(sources):
    """Derive an explicit operation-start cutoff only from an exact source field."""
    pattern = re.compile(
        r"手术时间[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?::\d{2})?\s*[-–—]")
    matches = []
    for source in sources:
        match = pattern.search(source.get("text") or "")
        if match:
            matches.append(dict(value=match.group(1), precision="minute",
                                source_id=source["source_id"],
                                source_title=source.get("title"),
                                extraction="exact_operation_time_field"))
    if not matches:
        return dict(operation_start=_unknown_clock(), status="UNKNOWN",
                    evidence=[], note="没有可程序定位的手术开始时间；不得据此推断术前/术后。")
    matches.sort(key=lambda item: (item["value"], item["source_id"]))
    first = matches[0]
    return dict(operation_start=dict(value=first["value"], precision="minute"),
                status="DERIVED_FROM_OPERATION_RECORD_REVIEW_REQUIRED",
                evidence=matches,
                note="仅作主任务时间边界候选；不是完整治疗阶段重建。")

"""Offline projection of located candidate spans into reviewable task candidates.

The projection is deterministic and uses frozen discovery objects. It does not
turn a located quote into a confirmed clinical decision or clinical Reference.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import hashlib
import json


PROJECTION_VERSION = "candidate-checkpoint-reprojection-0.1.0"
SUPPORT_FUNCTIONS = {"repeat", "execution", "generic", "uncertain"}
OUT_OF_SCOPE_PROFILES = {"postoperative_course", "discharge_summary", "operative"}
MODES = ["ADVANCE", "HOLD_FOR_EVIDENCE", "REDIRECT"]


def _digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _clock(value):
    if isinstance(value, dict):
        return deepcopy(value)
    return dict(value=value or None, precision="unknown")


def _has_clock(value):
    return bool(_clock(value).get("value"))


def _action_space():
    return dict(
        schema_version="management-action-plan-0.1.0",
        modes=list(MODES),
        action_plan_schema=dict(
            type="object", additionalProperties=False,
            required=["mode", "actions", "rationale"],
            properties=dict(
                mode=dict(type="string", enum=list(MODES)),
                actions=dict(
                    type="array", minItems=1,
                    items=dict(
                        type="object", additionalProperties=False,
                        required=["action_type", "target", "timing"],
                        properties=dict(
                            action_type=dict(type="string", minLength=1),
                            target=dict(type="string", minLength=1),
                            timing=dict(type="string", minLength=1),
                            conditions=dict(type="array", items=dict(type="string")),
                        ),
                    ),
                ),
                rationale=dict(type="string", minLength=1),
                evidence_to_obtain=dict(type="array", items=dict(type="string")),
            ),
        ),
        answer_prefilled=False,
        note="动作空间是待评审作答schema，不是程序推荐或医生Reference。",
    )


def _task_scope():
    return dict(
        research_task="PDAC_CURATIVE_RESECTION_PATH",
        includes_preoperative_assessment=True,
        includes_nat_reassessment=True,
        excludes_routine_intraoperative_and_postoperative_management=True,
    )


def _provenance(**kwargs):
    return dict(
        projection=PROJECTION_VERSION,
        actor="deterministic_offline_program",
        discovery_reused=True,
        clinical_reference_created=False,
        **kwargs,
    )


def _source_id(candidate):
    span = candidate.get("span") or {}
    if span.get("source_id"):
        return span["source_id"]
    source_ids = candidate.get("source_ids") or candidate.get("origin_source_ids") or []
    return source_ids[0] if len(source_ids) == 1 else None


def _valid_span(candidate):
    span = candidate.get("span") or {}
    return (bool(span.get("source_id") and span.get("quote"))
            and isinstance(span.get("start"), int)
            and not isinstance(span.get("start"), bool)
            and isinstance(span.get("end"), int)
            and not isinstance(span.get("end"), bool)
            and 0 <= span["start"] < span["end"])


def _profile(candidate, source):
    return (candidate.get("origin_document_profile")
            or candidate.get("discovery_document_profile")
            or (source or {}).get("document_profile"))


def _is_admission_intent(quote):
    admission = any(term in quote for term in (
        "收入我科", "收入本科", "收入院", "收治入院", "收住入院", "住院治疗"))
    intent = any(term in quote for term in ("为求", "为行", "欲行", "希望", "要求"))
    management = any(term in quote for term in ("手术", "治疗", "诊治"))
    return admission and intent and management


def _classify(candidate, source):
    profile = _profile(candidate, source)
    function = candidate.get("record_function")
    quote = (candidate.get("span") or {}).get("quote") or ""
    if profile in OUT_OF_SCOPE_PROFILES:
        return "TASK_OUT_OF_SCOPE", "ORIGIN_DOCUMENT_PROFILE_OUT_OF_MAIN_TASK"
    if _is_admission_intent(quote):
        return "SUPPORT_ONLY", "ADMISSION_INTENT_NOT_INDEPENDENT_DECISION"
    if function in SUPPORT_FUNCTIONS:
        return "SUPPORT_ONLY", "RECORD_FUNCTION_NOT_INDEPENDENT_DECISION"
    if candidate.get("scope") == "no":
        return "TASK_OUT_OF_SCOPE", "DISCOVERY_SCOPE_NO"
    if candidate.get("status", "ACCEPTED") != "ACCEPTED" or not _valid_span(candidate):
        return "REVIEW_REQUIRED", "LOCATED_ACCEPTED_SPAN_REQUIRED"
    if candidate.get("scope", "yes") != "yes":
        return "REVIEW_REQUIRED", "RESEARCH_SCOPE_UNCERTAIN"
    if function not in {"new_decision", "reassessment"}:
        return "REVIEW_REQUIRED", "RECORD_FUNCTION_REQUIRES_REVIEW"
    if not _has_clock(candidate.get("clock")) and not _has_clock((source or {}).get("clock")):
        return "REVIEW_REQUIRED", "MISSING_EVALUATION_CLOCK"
    return "RETAINED", "ACTIONABLE_CANDIDATE_PENDING_REVIEW"


def _group_id(case_id, source_id, event_ids):
    return "CG-" + _digest([case_id, source_id, sorted(event_ids)])


def _masked_spans(group, source):
    valid = sorted((item for item in group if _valid_span(item["candidate"])),
                   key=lambda item: (item["candidate"]["span"]["start"],
                                     item["candidate"]["span"]["end"],
                                     item["event_id"]))
    merged = []
    for item in valid:
        candidate = item["candidate"]
        span = candidate["span"]
        if merged and span["start"] <= merged[-1]["end"]:
            current = merged[-1]
            current["end"] = max(current["end"], span["end"])
            current["member_event_ids"].append(item["event_id"])
            current["migration_statuses"].append(item["migration_status"])
        else:
            merged.append(dict(
                source_id=span["source_id"], start=span["start"], end=span["end"],
                member_event_ids=[item["event_id"]],
                migration_statuses=[item["migration_status"]]))
    for span in merged:
        text = (source or {}).get("text") or ""
        span["quote"] = text[span["start"]:span["end"]]
        span["member_event_ids"] = sorted(set(span["member_event_ids"]))
        span["migration_statuses"] = sorted(set(span["migration_statuses"]))
        span["mask_reason"] = "CURRENT_GROUP_ANSWER_AND_ACTION_LEAKAGE"
    return merged


def _management_checkpoint(case_id, group, source):
    retained = sorted((item for item in group if item["migration_status"] == "RETAINED"),
                      key=lambda item: item["event_id"])
    source_id = retained[0]["source_id"]
    event_ids = [item["event_id"] for item in retained]
    checkpoint_id = "CP-M-" + _digest([case_id, source_id, event_ids])
    timed = next((item for item in retained
                  if (item["candidate"].get("clock") or {}).get("value")), None)
    first = timed or retained[0]
    candidate = first["candidate"]
    clock = _clock(candidate.get("clock"))
    if not clock.get("value") and source:
        clock = _clock(source.get("clock"))
    time_basis = (candidate.get("clock_basis") or (source or {}).get("clock_basis")
                  or "source_retrieval_clock_not_confirmed_choice_time")
    return dict(
        checkpoint_id=checkpoint_id,
        kind="management_decision",
        management_question=(
            "在该记录时点，针对PDAC根治性切除路径，下一步应推进、暂缓补证还是调整管理方向？"),
        management_question_source="program_generated_task_header_not_model_quote",
        program_generated_task_header=True,
        clock=clock,
        time_basis=time_basis,
        evaluation_time_role="document_retrieval_clock_not_confirmed_choice_time",
        source_ids=[source_id],
        member_event_ids=event_ids,
        supporting_event_ids=sorted(item["event_id"] for item in group
                                    if item["migration_status"] == "SUPPORT_ONLY"),
        masked_spans=_masked_spans(group, source),
        record_functions=sorted({item["candidate"].get("record_function")
                                 for item in retained}),
        actionability_status="PENDING_ACTIONABILITY_REVIEW",
        review_status="REVIEW_REQUIRED",
        candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION",
        action_space=_action_space(),
        task_scope=_task_scope(),
        provenance=_provenance(
            grouping_rule="all_candidate_spans_from_one_source",
            group_id=group[0]["group_id"],
            source_event_ids=sorted(item["event_id"] for item in group),
            quote_location_is_not_actionability_confirmation=True),
    )


def _diagnostic_checkpoint(case_id, event, node_kind):
    source_ids = sorted(event.get("source_ids") or [])
    checkpoint_id = "CP-D-" + _digest([case_id, event["event_id"], node_kind])
    common = dict(
        checkpoint_id=checkpoint_id,
        kind=node_kind,
        source_ids=source_ids,
        member_event_ids=[],
        record_functions=[],
        actionability_status="PROGRAM_GENERATED_CANDIDATE_PENDING_REVIEW",
        review_status="REVIEW_REQUIRED",
        candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION",
        action_space=_action_space(),
        task_scope=_task_scope(),
        program_generated_task_header=True,
        management_question_source="program_generated_task_header_not_source_title",
        diagnostic_event_id=event["event_id"],
    )
    if node_kind == "pre_exam_evaluation":
        return dict(
            common,
            management_question="在该检查执行前，下一步管理及检查选择应如何安排？",
            clock=_clock(event.get("exam_time")),
            time_basis="exam_time_as_pre_exam_evaluation_anchor_not_choice_timestamp",
            evaluation_time_role="before_exam_execution",
            mask_whole_diagnostic_event=True,
            show_complete_diagnostic_event=False,
            masked_spans=[],
            provenance=_provenance(
                diagnostic_event_id=event["event_id"],
                diagnostic_clock="exam_time",
                source_clocks=deepcopy(event.get("source_clocks") or {}),
                no_documented_choice_process_inferred=True),
        )
    visibility_basis = ("report_time_as_visibility_proxy"
                        if _has_clock(event.get("report_time")) else
                        "verified_time_as_degraded_visibility_proxy")
    return dict(
        common,
        management_question="该检查结果可见后，下一步管理应推进、暂缓补证还是调整方向？",
        clock=_clock(event.get("visibility_clock")),
        time_basis=visibility_basis,
        evaluation_time_role="after_report_visibility_proxy",
        mask_whole_diagnostic_event=False,
        show_complete_diagnostic_event=True,
        masked_spans=[],
        provenance=_provenance(
            diagnostic_event_id=event["event_id"],
            visibility_basis=event.get("visibility_basis") or visibility_basis,
            source_clocks=deepcopy(event.get("source_clocks") or {}),
            evidence_arrival_does_not_imply_evidence_used=True),
    )


def _migration(case_id, item, status, reason):
    candidate = item["candidate"]
    return dict(
        migration_id="MG-" + _digest([case_id, "candidate_span", item["event_id"]]),
        migration_type="candidate_span",
        event_id=item["event_id"],
        source_id=item["source_id"],
        group_id=item["group_id"],
        migration_status=status,
        status=status,
        reason=reason,
        original_record_function=candidate.get("record_function"),
        origin_document_profile=_profile(candidate, item["source"]),
        span=deepcopy(candidate.get("span") or {}),
        to_checkpoint_id=None,
    )


def _diagnostic_migration(case_id, event, node_kind, checkpoint_id, reason):
    return dict(
        migration_id="MG-" + _digest([case_id, "diagnostic_checkpoint",
                                     event["event_id"], node_kind]),
        migration_type="diagnostic_checkpoint",
        event_id=event["event_id"],
        source_ids=sorted(event.get("source_ids") or []),
        node_kind=node_kind,
        migration_status="RETAINED" if checkpoint_id else "NOT_GENERATED",
        status="RETAINED" if checkpoint_id else "NOT_GENERATED",
        reason=reason,
        to_checkpoint_id=checkpoint_id,
    )


def project_candidate_checkpoints(*, case_id, candidates, sources, diagnostic_events,
                                  task_time_bounds=None):
    """Reproject candidate spans and diagnostic events without changing inputs."""
    if task_time_bounds is None:
        from .diagnostic_events import derive_preoperative_task_bounds
        task_time_bounds = derive_preoperative_task_bounds(sources)
    operation_start = _clock(task_time_bounds.get("operation_start")).get("value")
    source_by_id = {source.get("source_id"): source for source in sources}
    prepared = []
    grouped = {}
    for index, original in enumerate(candidates):
        candidate = deepcopy(original)
        event_id = candidate.get("event_id") or candidate.get("record_id") or (
            "candidate-%d" % index)
        source_id = _source_id(candidate)
        source = source_by_id.get(source_id)
        status, reason = _classify(candidate, source)
        item = dict(candidate=candidate, event_id=event_id, source_id=source_id,
                    source=source, migration_status=status, reason=reason,
                    input_index=index, group_id=None)
        grouped.setdefault(source_id, []).append(item)
        prepared.append(item)
    for source_id, group in grouped.items():
        group_id = _group_id(case_id, source_id, [item["event_id"] for item in group])
        for item in group:
            item["group_id"] = group_id

    migrations = [_migration(case_id, item, item["migration_status"], item["reason"])
                  for item in prepared]
    migration_by_event = {migration["event_id"]: migration for migration in migrations}
    support_records = []
    candidate_checkpoints = []
    for source_id, group in grouped.items():
        retained = [item for item in group if item["migration_status"] == "RETAINED"]
        if retained:
            checkpoint = _management_checkpoint(case_id, group, source_by_id.get(source_id))
            candidate_checkpoints.append(checkpoint)
            for item in retained:
                migration_by_event[item["event_id"]]["to_checkpoint_id"] = (
                    checkpoint["checkpoint_id"])
            for item in group:
                if item["migration_status"] == "SUPPORT_ONLY":
                    migration_by_event[item["event_id"]]["to_checkpoint_id"] = (
                        checkpoint["checkpoint_id"])
        for item in group:
            if item["migration_status"] != "SUPPORT_ONLY":
                continue
            support_records.append(dict(
                support_id="SUP-" + _digest([case_id, item["event_id"]]),
                event_id=item["event_id"], group_id=item["group_id"],
                source_ids=[item["source_id"]] if item["source_id"] else [],
                support_type=item["reason"],
                to_checkpoint_id=migration_by_event[item["event_id"]]["to_checkpoint_id"],
                record_function=item["candidate"].get("record_function"),
                span=deepcopy(item["candidate"].get("span") or {}),
                provenance=_provenance(candidate_span_retained_as_context=True),
            ))

    diagnostic_source_visibility = {source_id: _clock(event.get("visibility_clock")).get("value")
                                    for event in diagnostic_events
                                    for source_id in event.get("source_ids") or []}
    projected_diagnostics = []
    for original in diagnostic_events:
        event = deepcopy(original)
        event["candidate_checkpoint_ids"] = {}
        event["generation_audits"] = []
        if event.get("visibility_basis") is None:
            if _has_clock(event.get("report_time")):
                event["visibility_clock"] = event["report_time"]
                event["visibility_basis"] = "report_time"
            elif _has_clock(event.get("verified_time")):
                event["visibility_clock"] = event["verified_time"]
                event["visibility_basis"] = "verified_time_fallback"
            else:
                event["visibility_clock"] = _clock(None)
                event["visibility_basis"] = "unknown"
        visibility_basis = event["visibility_basis"]
        exam_available = _has_clock(event.get("exam_time"))
        report_available = _has_clock(event.get("visibility_clock"))
        exam_out = bool(operation_start and exam_available
                        and _clock(event["exam_time"])["value"] >= operation_start)
        report_out = bool(operation_start and report_available
                          and _clock(event["visibility_clock"])["value"] >= operation_start)
        exam_value = _clock(event.get("exam_time")).get("value")
        context_source_ids = []
        if exam_value:
            for source in sources:
                source_id = source.get("source_id")
                if source_id in (event.get("source_ids") or []):
                    continue
                visible = diagnostic_source_visibility.get(
                    source_id, _clock(source.get("clock")).get("value"))
                if visible and visible <= exam_value:
                    context_source_ids.append(source_id)
        pre_missing_reason = ("AFTER_OPERATION_START_TASK_OUT_OF_SCOPE" if exam_out else
                              "MISSING_EXAM_TIME" if not exam_available else
                              "NO_VISIBLE_PRE_EXAM_CLINICAL_CONTEXT" if not context_source_ids
                              else "exam_time_present")
        specifications = [
            ("pre_exam_evaluation", exam_available and not exam_out and bool(context_source_ids),
             "exam_time_present" if not exam_out else "AFTER_OPERATION_START_TASK_OUT_OF_SCOPE",
             pre_missing_reason),
            ("evidence_arrival_reassessment", report_available and not report_out,
             visibility_basis if not report_out else "AFTER_OPERATION_START_TASK_OUT_OF_SCOPE",
             "AFTER_OPERATION_START_TASK_OUT_OF_SCOPE" if report_out else
             ("MISSING_OR_CONFLICTING_REPORT_VISIBILITY"
              if (event.get("audit") or {}).get("clock_inconsistent_flags") else
              "MISSING_REPORT_AND_VERIFIED_TIME_VISIBILITY_UNKNOWN")), 
        ]
        for node_kind, available, reason, missing_reason in specifications:
            if available:
                checkpoint = _diagnostic_checkpoint(case_id, event, node_kind)
                candidate_checkpoints.append(checkpoint)
                checkpoint_id = checkpoint["checkpoint_id"]
                event["candidate_checkpoint_ids"][node_kind] = checkpoint_id
                audit_reason = reason
            else:
                checkpoint_id = None
                audit_reason = missing_reason
            audit = dict(node_kind=node_kind, generated=available,
                         checkpoint_id=checkpoint_id, reason=audit_reason)
            event["generation_audits"].append(audit)
            migration = _diagnostic_migration(
                case_id, event, node_kind, checkpoint_id, audit_reason)
            if audit_reason == "AFTER_OPERATION_START_TASK_OUT_OF_SCOPE":
                migration["migration_status"] = migration["status"] = "TASK_OUT_OF_SCOPE"
            migrations.append(migration)
        projected_diagnostics.append(event)

    candidate_migrations = [item for item in migrations
                            if item["migration_type"] == "candidate_span"]
    summary = dict(
        candidate_input_count=len(candidates),
        candidate_migration_count=len(candidate_migrations),
        candidate_migration_conserved=len(candidate_migrations) == len(candidates),
        candidate_checkpoint_count=len(candidate_checkpoints),
        support_record_count=len(support_records),
        migration_status_counts=dict(sorted(Counter(
            item["migration_status"] for item in migrations).items())),
        migration_type_counts=dict(sorted(Counter(
            item["migration_type"] for item in migrations).items())),
        migration_reason_counts=dict(sorted(Counter(
            item["reason"] for item in migrations).items())),
        candidate_checkpoint_kind_counts=dict(sorted(Counter(
            item["kind"] for item in candidate_checkpoints).items())),
        task_time_bounds=deepcopy(task_time_bounds),
        clinical_accuracy_assessed=False,
        note="离线程序重投影与迁移计数，不代表临床准确性或专家Reference。",
    )
    return dict(
        candidate_checkpoints=candidate_checkpoints,
        migrations=migrations,
        support_records=support_records,
        diagnostic_events=projected_diagnostics,
        summary=summary,
    )


__all__ = ["project_candidate_checkpoints"]

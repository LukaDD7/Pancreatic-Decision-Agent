"""Offline projection of located candidate spans into reviewable task candidates.

The projection is deterministic and consumes external Discovery objects, whether
fresh or frozen. It does not turn a located quote into a confirmed clinical
decision or clinical Reference.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import hashlib
import json


PROJECTION_VERSION = "candidate-checkpoint-reprojection-0.3.0"
PRIMARY_EVALUATION_SCOPE = "PRIMARY_TASK_PREOPERATIVE_OR_NAT_REASSESSMENT"
NON_PRIMARY_EVALUATION_SCOPE = "NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY"
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


def _evaluation_scope(clock, operation_start):
    value = _clock(clock).get("value")
    postoperative = bool(operation_start and value and value >= operation_start)
    return dict(
        primary_evaluation_eligible=not postoperative,
        evaluation_scope=(NON_PRIMARY_EVALUATION_SCOPE if postoperative
                          else PRIMARY_EVALUATION_SCOPE),
        evaluation_scope_reason=("AFTER_OPERATION_START_NON_PRIMARY_P2_TRAJECTORY"
                                 if postoperative else
                                 "PREOPERATIVE_OR_NAT_REASSESSMENT_PRIMARY_TASK"
                                 if operation_start else
                                 "OPERATION_BOUNDARY_UNKNOWN_PRIMARY_TASK_CANDIDATE_REVIEW_REQUIRED"),
    )


def _operation_day_order_unknown(clock, operation_start):
    value = _clock(clock).get("value")
    operation_value = _clock(operation_start).get("value")
    if not value or not operation_value or value[:10] != operation_value[:10]:
        return False
    coarse = {"date", "day", "unknown"}
    precision = (_clock(clock).get("precision") or "unknown").lower()
    operation_precision = (_clock(operation_start).get("precision") or "unknown").lower()
    return (len(value) <= 10 or len(operation_value) <= 10
            or precision in coarse or operation_precision in coarse)


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
        discovery_input="external_case_result_fresh_or_frozen_not_inferred_here",
        clinical_reference_created=False,
        **kwargs,
    )


def _candidate_action(candidate, *, event_id=None):
    action = deepcopy(candidate.get("candidate_action") or {})
    if not action.get("action_type") or not action.get("target"):
        return None
    for key in ("rationale", "reason"):
        if candidate.get(key):
            action[key] = candidate[key]
    action.update(actor="discovery_llm", review_source="discovery_candidate_action",
                  answer_prefilled=False, clinical_reference_created=False)
    if event_id:
        action["event_id"] = event_id
    return action


def _normalized_action_target(value):
    import re
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").lower())
    normalized = normalized.replace("磁共振", "mr").replace("mri", "mr")
    for prefix in ("胰腺", "腹部", "上腹部"):
        normalized = normalized.replace(prefix, "")
    return normalized.removesuffix("检查")


def _diagnostic_management_claims(checkpoints, events, reviews_by_unit):
    claims = {}
    for checkpoint in checkpoints:
        if checkpoint["kind"] != "management_decision":
            continue
        clock = _clock(checkpoint.get("clock")).get("value")
        targets = {_normalized_action_target(action.get("target"))
                   for action in checkpoint.get("candidate_actions") or []} - {""}
        if not clock or not targets:
            continue
        eligible = []
        for event in events:
            unit_id = event.get("discovery_unit_id") or event["event_id"]
            review = reviews_by_unit.get((unit_id, "pre_exam_evaluation")) or {}
            if review.get("disposition") != "yes" \
                    or set(checkpoint.get("source_ids") or []) & set(event["source_ids"]):
                continue
            anchor = (_clock(event.get("exam_time")).get("value")
                      or _clock(event.get("visibility_clock")).get("value"))
            diagnostic_target = _normalized_action_target(
                (review.get("candidate_action") or {}).get("target"))
            if anchor and clock <= anchor and diagnostic_target in targets:
                eligible.append((anchor, event["event_id"]))
        if eligible:
            _, event_id = min(eligible)
            claims[event_id] = checkpoint["checkpoint_id"]
    return claims


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
    intent = any(term in quote for term in ("为求", "为行", "欲行", "希望", "要求", "进一步诊治"))
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
    if _candidate_action(candidate) is None:
        return "REVIEW_REQUIRED", "DISCOVERY_CONCRETE_ACTION_REQUIRED"
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
        actionability_status="DISCOVERY_AGENT_ACTIONABLE_CANDIDATE",
        review_status="REVIEW_REQUIRED",
        candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION",
        candidate_actions=[action for action in (
            _candidate_action(item["candidate"], event_id=item["event_id"])
            for item in retained) if action],
        action_space=_action_space(),
        task_scope=_task_scope(),
        **_evaluation_scope(clock, None),
        provenance=_provenance(
            grouping_rule="all_candidate_spans_from_one_source",
            group_id=group[0]["group_id"],
            source_event_ids=sorted(item["event_id"] for item in group),
            quote_location_is_not_actionability_confirmation=True),
    )


def _diagnostic_checkpoint(case_id, event, node_kind, review, *, pre_time_basis=None,
                           operation_start=None):
    source_ids = sorted(event.get("source_ids") or [])
    evaluation_clock = (event.get("exam_time")
                        if node_kind == "pre_exam_evaluation" and pre_time_basis == "exam_time"
                        else event.get("visibility_clock"))
    checkpoint_id = "CP-D-" + _digest([case_id, event["event_id"], node_kind])
    action = _candidate_action(review, event_id=event["event_id"])
    common = dict(
        checkpoint_id=checkpoint_id,
        kind=node_kind,
        source_ids=source_ids,
        member_event_ids=[],
        record_functions=[],
        actionability_status="DISCOVERY_AGENT_ACTIONABLE_CANDIDATE",
        review_status="REVIEW_REQUIRED",
        candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION",
        candidate_actions=[action],
        discovery_review=deepcopy(review),
        action_space=_action_space(),
        task_scope=_task_scope(),
        program_generated_task_header=True,
        management_question_source="discovery_agent_question_or_program_action_header",
        diagnostic_event_id=event["event_id"],
        **_evaluation_scope(evaluation_clock, operation_start),
    )
    if node_kind == "pre_exam_evaluation":
        return dict(
            common,
            management_question=(review.get("management_question") or
                                 "在此时点，是否应安排%s？请给出 Mode 和具体管理动作。" %
                                 action["target"]),
            clock=_clock(event.get("exam_time") if pre_time_basis == "exam_time"
                         else event.get("visibility_clock")),
            time_basis=pre_time_basis,
            evaluation_time_role=("before_exam_execution" if pre_time_basis == "exam_time"
                                  else "report_visibility_fallback_for_exam_choice_evaluation"),
            mask_whole_diagnostic_event=True,
            show_complete_diagnostic_event=False,
            masked_spans=[],
            provenance=_provenance(
                diagnostic_event_id=event["event_id"],
                diagnostic_clock=pre_time_basis,
                source_clocks=deepcopy(event.get("source_clocks") or {}),
                no_documented_choice_process_inferred=True),
        )
    visibility_basis = ("report_time_as_visibility_proxy"
                        if _has_clock(event.get("report_time")) else
                        "verified_time_as_degraded_visibility_proxy")
    return dict(
        common,
        management_question=(review.get("management_question") or
                             "该检查结果可见后，下一步管理应如何安排？"),
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
                                  task_time_bounds=None, diagnostic_reviews=None):
    """Project only Discovery-reviewed, actionable candidates into review tasks."""
    if task_time_bounds is None:
        from .diagnostic_events import derive_preoperative_task_bounds
        task_time_bounds = derive_preoperative_task_bounds(sources)
    operation_start = _clock(task_time_bounds.get("operation_start")).get("value")
    reviews_by_unit = {}
    for review in diagnostic_reviews or []:
        unit_id = (review.get("discovery_unit_id") or review.get("unit_id")
                   or review.get("diagnostic_event_id"))
        if unit_id and review.get("candidate_kind"):
            reviews_by_unit[(unit_id, review["candidate_kind"])] = deepcopy(review)
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
        effective_clock = (candidate.get("clock") if _has_clock(candidate.get("clock"))
                           else (source or {}).get("clock"))
        candidate_time = _clock(effective_clock).get("value")
        if status == "RETAINED" and operation_start and candidate_time:
            if _operation_day_order_unknown(
                    effective_clock, task_time_bounds.get("operation_start")):
                status, reason = "REVIEW_REQUIRED", "OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN"
            elif candidate_time >= operation_start:
                status, reason = "TASK_OUT_OF_SCOPE", "AFTER_OPERATION_START_TASK_OUT_OF_SCOPE"
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
    management_claims = _diagnostic_management_claims(
        candidate_checkpoints, diagnostic_events, reviews_by_unit)
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
        clock_flags = (event.get("audit") or {}).get("clock_inconsistent_flags") or []
        exam_usable = exam_available and "exam_time" not in clock_flags
        report_time_usable = (_has_clock(event.get("report_time"))
                              and "report_time" not in clock_flags)
        pre_time_basis = ("exam_time" if exam_usable else
                          "report_time_fallback_for_pre_exam_evaluation"
                          if report_time_usable else None)
        pre_clock = (event.get("exam_time") if pre_time_basis == "exam_time"
                     else event.get("visibility_clock"))
        pre_value = _clock(pre_clock).get("value")
        context_source_ids = []
        if pre_value:
            for source in sources:
                source_id = source.get("source_id")
                if source_id in (event.get("source_ids") or []):
                    continue
                visible = diagnostic_source_visibility.get(
                    source_id, _clock(source.get("clock")).get("value"))
                if visible and visible <= pre_value:
                    context_source_ids.append(source_id)
        unit_id = event.get("discovery_unit_id") or event["event_id"]
        reviews = {kind: reviews_by_unit.get((unit_id, kind)) for kind in
                   ("pre_exam_evaluation", "evidence_arrival_reassessment")}

        def gate_reason(node_kind, clock_available):
            review = reviews[node_kind]
            if review is None:
                return "DISCOVERY_REVIEW_REQUIRED"
            if review.get("disposition") == "no":
                return "DISCOVERY_AGENT_REJECTED_OUT_OF_RESEARCH_SCOPE"
            if review.get("disposition") != "yes":
                return "DISCOVERY_AGENT_REVIEW_UNCERTAIN"
            if _candidate_action(review) is None:
                return "DISCOVERY_CONCRETE_ACTION_REQUIRED"
            if not clock_available:
                if node_kind == "pre_exam_evaluation":
                    return "MISSING_EXAM_AND_REPORT_TIME_FOR_EVALUATION_ANCHOR"
                return ("MISSING_OR_CONFLICTING_REPORT_VISIBILITY" if clock_flags else
                        "MISSING_REPORT_AND_VERIFIED_TIME_VISIBILITY_UNKNOWN")
            evaluation_clock = (pre_clock if node_kind == "pre_exam_evaluation"
                                else event["visibility_clock"])
            if _operation_day_order_unknown(
                    evaluation_clock, task_time_bounds.get("operation_start")):
                return "OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN"
            if node_kind == "pre_exam_evaluation" and not context_source_ids:
                return "NO_VISIBLE_PRE_EXAM_CLINICAL_CONTEXT"
            return pre_time_basis if node_kind == "pre_exam_evaluation" else visibility_basis

        pre_gate = gate_reason("pre_exam_evaluation", bool(pre_time_basis))
        pre_available = pre_gate in ("exam_time", "report_time_fallback_for_pre_exam_evaluation")
        specifications = [
            ("pre_exam_evaluation", pre_available, pre_gate),
            ("evidence_arrival_reassessment", False, None),
        ]
        post_gate = gate_reason("evidence_arrival_reassessment", report_available)
        post_available = post_gate == visibility_basis
        postoperative_report = bool(
            operation_start and _clock(event["visibility_clock"]).get("value")
            and _clock(event["visibility_clock"])["value"] >= operation_start)
        management_claim = (management_claims.get(event["event_id"])
                            if pre_available else None)
        if management_claim:
            pre_available = False
            pre_gate = "SAME_DIAGNOSTIC_ACTION_MANAGEMENT_CP_RETAINED"
            if post_available and not postoperative_report:
                post_available = False
                post_gate = "SAME_DIAGNOSTIC_ACTION_MANAGEMENT_CP_RETAINED"
        if pre_available and post_available and not postoperative_report:
            post_gate = "SAME_DIAGNOSTIC_SOURCE_PRE_EXAM_CP_RETAINED"
            post_available = False
        specifications[0] = ("pre_exam_evaluation", pre_available, pre_gate)
        specifications[1] = ("evidence_arrival_reassessment", post_available, post_gate)
        for node_kind, available, audit_reason in specifications:
            destination_id = None
            if available:
                conflicts = [checkpoint for checkpoint in candidate_checkpoints
                             if set(checkpoint.get("source_ids") or []) & set(event["source_ids"])]
                management_conflicts = [checkpoint for checkpoint in conflicts
                                        if checkpoint["kind"] == "management_decision"]
                if node_kind == "pre_exam_evaluation" \
                        and len(management_conflicts) == len(conflicts):
                    checkpoint = _diagnostic_checkpoint(
                        case_id, event, node_kind, reviews[node_kind],
                        pre_time_basis=pre_time_basis, operation_start=operation_start)
                    destination_id = checkpoint["checkpoint_id"]
                    candidate_checkpoints.append(checkpoint)
                    for old in management_conflicts:
                        candidate_checkpoints.remove(old)
                        for member_id in old.get("member_event_ids", []) \
                                + old.get("supporting_event_ids", []):
                            migration = migration_by_event.get(member_id)
                            if migration:
                                migration.update(migration_status="SUPPORT_ONLY", status="SUPPORT_ONLY",
                                                 reason="SAME_SOURCE_PRE_EXAM_CP_RETAINED",
                                                 to_checkpoint_id=destination_id)
                        for support in support_records:
                            if support.get("to_checkpoint_id") == old["checkpoint_id"]:
                                support["to_checkpoint_id"] = destination_id
                elif conflicts and not (
                        node_kind == "evidence_arrival_reassessment" and postoperative_report):
                    available = False
                    destination_id = conflicts[0]["checkpoint_id"]
                    audit_reason = ("SAME_SOURCE_MANAGEMENT_CP_RETAINED"
                                    if conflicts[0]["kind"] == "management_decision"
                                    else "SOURCE_ALREADY_ASSIGNED_TO_DIAGNOSTIC_CP")
            if available:
                if destination_id is None:
                    checkpoint = _diagnostic_checkpoint(
                        case_id, event, node_kind, reviews[node_kind],
                        pre_time_basis=pre_time_basis, operation_start=operation_start)
                    destination_id = checkpoint["checkpoint_id"]
                    candidate_checkpoints.append(checkpoint)
                checkpoint_id = destination_id
                event["candidate_checkpoint_ids"][node_kind] = checkpoint_id
            else:
                checkpoint_id = None
            audit = dict(node_kind=node_kind, generated=available,
                         checkpoint_id=checkpoint_id, reason=audit_reason,
                         discovery_disposition=(reviews[node_kind] or {}).get("disposition"))
            event["generation_audits"].append(audit)
            migration = _diagnostic_migration(
                case_id, event, node_kind, checkpoint_id, audit_reason)
            if audit_reason == "DISCOVERY_AGENT_REJECTED_OUT_OF_RESEARCH_SCOPE":
                migration["migration_status"] = migration["status"] = "TASK_OUT_OF_SCOPE"
            elif audit_reason in ("SAME_DIAGNOSTIC_SOURCE_PRE_EXAM_CP_RETAINED",
                                  "SAME_SOURCE_MANAGEMENT_CP_RETAINED",
                                  "SAME_DIAGNOSTIC_ACTION_MANAGEMENT_CP_RETAINED",
                                  "SOURCE_ALREADY_ASSIGNED_TO_DIAGNOSTIC_CP"):
                migration["migration_status"] = migration["status"] = "SUPPORT_ONLY"
                migration["to_checkpoint_id"] = (
                    event["candidate_checkpoint_ids"].get("pre_exam_evaluation")
                    if audit_reason == "SAME_DIAGNOSTIC_SOURCE_PRE_EXAM_CP_RETAINED"
                    else management_claim or destination_id)
            elif not available and audit_reason not in (
                    "MISSING_EXAM_TIME", "MISSING_REPORT_AND_VERIFIED_TIME_VISIBILITY_UNKNOWN"):
                migration["migration_status"] = migration["status"] = "REVIEW_REQUIRED"
            migrations.append(migration)
        projected_diagnostics.append(event)

    candidate_migrations = [item for item in migrations
                            if item["migration_type"] == "candidate_span"]
    summary = dict(
        candidate_input_count=len(candidates),
        candidate_migration_count=len(candidate_migrations),
        candidate_migration_conserved=len(candidate_migrations) == len(candidates),
        candidate_checkpoint_count=len(candidate_checkpoints),
        diagnostic_review_count=len(diagnostic_reviews or []),
        diagnostic_events_with_multiple_checkpoints=sum(
            1 for event in projected_diagnostics
            if len(event["candidate_checkpoint_ids"]) > 1),
        support_record_count=len(support_records),
        migration_status_counts=dict(sorted(Counter(
            item["migration_status"] for item in migrations).items())),
        migration_type_counts=dict(sorted(Counter(
            item["migration_type"] for item in migrations).items())),
        migration_reason_counts=dict(sorted(Counter(
            item["reason"] for item in migrations).items())),
        candidate_checkpoint_kind_counts=dict(sorted(Counter(
            item["kind"] for item in candidate_checkpoints).items())),
        primary_evaluation_checkpoint_count=sum(
            item["primary_evaluation_eligible"] for item in candidate_checkpoints),
        non_primary_evaluation_checkpoint_count=sum(
            not item["primary_evaluation_eligible"] for item in candidate_checkpoints),
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

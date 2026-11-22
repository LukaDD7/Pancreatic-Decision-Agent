"""Project Discovery candidates into goal-oriented management state transitions.

The projection is deterministic. Discovery actions remain review candidates; no
checkpoint or action cycle is a confirmed clinical decision or expert Reference.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re

from .candidate_checkpoints import (
    _action_space,
    _candidate_action,
    _classify,
    _clock,
    _digest,
    _evaluation_scope,
    _has_clock,
    _is_admission_intent,
    _masked_spans,
    _normalized_action_target,
    _profile,
    _source_id,
    _task_scope,
    _valid_span,
    validate_candidate_spans,
)
from .actions import get_action
from .ordering import compare


PROJECTION_VERSION = "management-trajectory-0.3.7"
BEFORE = "BEFORE_ACQUISITION"
AFTER = "AFTER_EVIDENCE_ARRIVAL"
REASSESSMENT = "MANAGEMENT_REASSESSMENT"
PHASES = {
    "INITIAL_RESECTABILITY_ASSESSMENT",
    "PREOPERATIVE_OPTIMIZATION",
    "NAT_OR_CONVERSION",
    "POST_TREATMENT_REASSESSMENT",
    "SURGICAL_EXPLORATION",
    "POSTOPERATIVE_TRAJECTORY",
}
REVIEW_KINDS = {
    "pre_exam_evaluation": BEFORE,
    "evidence_arrival_reassessment": AFTER,
    BEFORE: BEFORE,
    AFTER: AFTER,
}
DIAGNOSTIC_ACTION_TERMS = (
    "diagnostic", "imaging", "exam", "patholog", "biopsy", "endoscop",
    "检查", "影像", "病理", "活检",
)
ROUTINE_POSTOPERATIVE_TERMS = (
    "icu", "监护", "补液", "镇痛", "出院", "拔管", "换药", "抗感染",
    "术后常规", "伤口", "引流管护理", "生命体征观察",
)
CURATIVE_ACTION_TERMS = ("根治", "切除", "resect", "explor", "探查")
ADJUDICATION_DISPOSITIONS = {"RETAIN_CP", "SUPPORT_ONLY", "REVIEW_REQUIRED"}
ADJUDICATION_EVOLUTIONS = {
    "FIRST_VISIBLE", "NEW_EVIDENCE_ROUND", "SAME_ROUND_SUPPORT", "REPEATED_PLAN",
    "EXECUTION", "DIFFERENT_TASK", "UNCERTAIN",
}
ACTION_REVIEW_FLAG = "ACTION_TASK_MISMATCH_REVIEW_REQUIRED"
ACTION_REVIEW_ACTOR = "case_level_action_review_llm"
ACTION_REVIEW_DISPOSITIONS = {"REMOVE_INDEPENDENT_CP", "RETAIN_CP_WITH_ACTION"}
ACTION_REVIEW_REMOVE_REASON = "CASE_LEVEL_ACTION_REVIEW_REMOVE_INDEPENDENT_CP"


def _provenance(**values):
    return dict(
        projection=PROJECTION_VERSION,
        actor="deterministic_offline_program",
        discovery_input="external_case_result_fresh_or_frozen_not_inferred_here",
        clinical_reference_created=False,
        **values,
    )


def _task_header_fields():
    return dict(
        knowledge_version="2026-11-09",
        allowed_time_precisions=["date", "minute", "second"],
        unknown_time_precision_requires_review=True,
        next_key_surgical_step="REVIEW_REQUIRED",
        next_key_surgical_step_basis="NOT_INFERRED_WITHOUT_EXPLICIT_SOURCE_FACT",
    )


def _value(clock):
    return _clock(clock).get("value")


def _action_signature(action):
    action_type = re.sub(r"\s+", "_", (action.get("action_type") or "").strip().upper())
    timing = re.sub(r"\s+", " ", (action.get("timing") or "").strip()).lower()
    conditions = tuple(sorted(
        re.sub(r"\s+", " ", str(condition).strip()).lower()
        for condition in action.get("conditions") or []))
    return action_type, _normalized_action_target(action.get("target")), timing, conditions


def _question_signature(action):
    _, target, timing, conditions = _action_signature(action)
    return target, timing, conditions


def _deduplicable_diagnostic_action(action):
    if not _is_diagnostic_action(action):
        return False
    text = " ".join(((action.get("action_type") or ""),
                     (action.get("target") or ""))).lower()
    return not any(term in text for term in CURATIVE_ACTION_TERMS)


def _unique_candidate_actions(actions):
    unique = {}
    for action in actions:
        if not action:
            continue
        signature = _action_signature(action)
        if _deduplicable_diagnostic_action(action):
            signature = ("DIAGNOSTIC_ACTION",) + signature[1:]
        if signature not in unique:
            unique[signature] = deepcopy(action)
            unique[signature]["merged_event_ids"] = sorted({
                action.get("event_id")} - {None})
            unique[signature]["merged_action_types"] = sorted({
                action.get("action_type")} - {None})
        else:
            unique[signature]["merged_event_ids"] = sorted(set(
                unique[signature]["merged_event_ids"] + [action.get("event_id")]) - {None})
            unique[signature]["merged_action_types"] = sorted(set(
                unique[signature]["merged_action_types"] + [action.get("action_type")]) - {None})
    return list(unique.values())


def _is_diagnostic_action(action):
    text = " ".join(((action.get("action_type") or ""),
                     (action.get("target") or ""))).lower()
    return (any(term in text for term in DIAGNOSTIC_ACTION_TERMS)
            or bool(re.search(r"(?:^|[^a-z])(?:ct|mr|mri|eus)(?:[^a-z]|$)", text)))


def _candidate_clock(item):
    return (_value(item["candidate"].get("clock"))
            or _value((item["source"] or {}).get("clock")))


def _attach_action_review(checkpoint, review):
    checkpoint["original_candidate_actions"] = deepcopy(
        review["original_candidate_actions"])
    reviewed = deepcopy(review)
    reviewed["provenance"] = dict(
        actor=ACTION_REVIEW_ACTOR,
        source="checkpoint_adjudication.action_reviews",
        clinical_reference_created=False)
    checkpoint["action_review"] = reviewed
    checkpoint["action_review_provenance"] = deepcopy(reviewed["provenance"])
    checkpoint.setdefault("provenance", {})["action_review_actor"] = ACTION_REVIEW_ACTOR


def _state_token(candidate):
    return deepcopy(dict(
        evidence_state_id=candidate.get("evidence_state_id"),
        input_evidence_ids=sorted(candidate.get("input_evidence_ids") or []),
        new_evidence_event_ids=sorted(candidate.get("new_evidence_event_ids") or []),
    ))


def _has_condition_or_action_increment(candidate):
    if any(bool(candidate.get(key)) for key in (
            "condition_changed", "new_condition", "action_changed",
            "treatment_changed", "state_increment")):
        return True
    quote = (candidate.get("span") or {}).get("quote") or ""
    return bool(re.search(
        r"新发|新出现|病情变化|新增(?:症状|并发症|转移)|取消|改期|更改为|调整为|"
        r"改用|停药|疗效评估后|复查后调整", quote))


def _has_state_increment(candidate):
    explicit = (
        candidate.get("evidence_increment"), candidate.get("input_evidence_ids"),
        candidate.get("new_evidence_event_ids"),
    )
    return _has_condition_or_action_increment(candidate) or any(
        bool(value) for value in explicit) or bool(re.search(
            r"病理结果回报后", (candidate.get("span") or {}).get("quote") or ""))


def _routine_postoperative(item, operation_start):
    if not operation_start or not _candidate_clock(item) \
            or _candidate_clock(item) < operation_start:
        return False
    candidate = item["candidate"]
    action = candidate.get("candidate_action") or {}
    text = " ".join((
        (candidate.get("span") or {}).get("quote") or "",
        action.get("action_type") or "", action.get("target") or "",
    )).lower()
    curative_action = " ".join((
        action.get("action_type") or "", action.get("target") or "")).lower()
    if any(term in curative_action for term in CURATIVE_ACTION_TERMS):
        return False
    return any(term in text for term in ROUTINE_POSTOPERATIVE_TERMS)


def _preoperative_action_conflict(candidate):
    action = candidate.get("candidate_action") or {}
    action_id = action.get("action_id")
    if action_id and action_id != "OTHER_SPECIFIED":
        try:
            dictionary_action = get_action(action_id)
        except ValueError:
            pass
        else:
            return "POSTOPERATIVE_TRAJECTORY" not in dictionary_action["allowed_phases"]
    if action.get("phase") in PHASES - {"POSTOPERATIVE_TRAJECTORY"}:
        return True
    action_type = (action.get("action_type") or "").lower()
    target = (action.get("target") or "").lower()
    text = action_type + " " + target
    if any(term in text for term in (
            "start_nat", "neoadjuvant", "conversion", "新辅助", "转化治疗", "术前")):
        return True
    if "术后" in target:
        return False
    return any(term in text for term in (
        "根治", "切除", "resect", "explor", "探查"))


def _migration(case_id, item, status, reason):
    candidate = item["candidate"]
    return dict(
        migration_id="MG-" + _digest([case_id, "candidate_span", item["event_id"]]),
        migration_type="candidate_span",
        event_id=item["event_id"],
        source_id=item["source_id"],
        group_id=item.get("group_id"),
        migration_status=status,
        status=status,
        reason=reason,
        original_record_function=candidate.get("record_function"),
        origin_document_profile=_profile(candidate, item["source"]),
        span=deepcopy(candidate.get("span") or {}),
        to_checkpoint_id=None,
    )


def _masked_spans_for_items(items, source_by_id):
    spans = []
    by_source = {}
    for item in items:
        if item.get("source_id") and _valid_span(item["candidate"]):
            by_source.setdefault(item["source_id"], []).append(item)
    for source_id, group in sorted(by_source.items()):
        spans.extend(_masked_spans(group, source_by_id.get(source_id)))
    return sorted(spans, key=lambda span: (
        span["source_id"], span["start"], span["end"]))


def _management_checkpoint(case_id, group):
    items = group["items"]
    event_ids = [item["event_id"] for item in items]
    first = items[0]
    candidate = first["candidate"]
    clock = _clock(candidate.get("clock"))
    source_clock = _clock((first["source"] or {}).get("clock"))
    if clock.get("raw") is None and source_clock.get("raw") is not None \
            and clock.get("value") == source_clock.get("value") \
            and clock.get("precision") == source_clock.get("precision"):
        clock["raw"] = deepcopy(source_clock["raw"])
    if not _has_clock(clock):
        clock = source_clock
    checkpoint_id = "CP-M-" + _digest([case_id, event_ids])
    return dict(
        checkpoint_id=checkpoint_id,
        kind=REASSESSMENT,
        management_question=(
            "在该记录时点，针对PDAC根治性切除路径，下一步应推进、暂缓补证还是调整管理方向？"),
        management_question_source="program_generated_task_header_not_model_quote",
        program_generated_task_header=True,
        clock=clock,
        clock_basis=(candidate.get("clock_basis")
                     or (first["source"] or {}).get("clock_basis")
                     or "source_retrieval_clock_not_confirmed_choice_time"),
        time_basis=(candidate.get("clock_basis")
                    or (first["source"] or {}).get("clock_basis")
                    or "source_retrieval_clock_not_confirmed_choice_time"),
        evaluation_time_role="document_retrieval_clock_not_confirmed_choice_time",
        source_ids=sorted({item["source_id"] for item in items if item["source_id"]}),
        support_source_ids=sorted({item["source_id"] for item in items if item["source_id"]}),
        member_event_ids=event_ids,
        supporting_event_ids=[],
        masked_spans=[],
        record_functions=sorted({item["candidate"].get("record_function")
                                 for item in items if item["candidate"].get("record_function")}),
        actionability_status="DISCOVERY_REVIEWED_MANAGEMENT_CANDIDATE",
        review_status="REVIEW_REQUIRED",
        candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION",
        candidate_actions=_unique_candidate_actions(
            _candidate_action(item["candidate"], event_id=item["event_id"])
            for item in items),
        action_space=_action_space(),
        task_scope=_task_scope(),
        **_task_header_fields(),
        review_flags=sorted({item["reason"] for item in items
                             if item.get("postoperative_trajectory")}),
        provenance=_provenance(
            grouping_rule="adjacent_unresolved_question_and_normalized_action",
            group_id=group["group_id"],
            source_event_ids=event_ids),
    )


def _group_management(case_id, prepared, migration_by_event, evidence_arrivals):
    retained = sorted((item for item in prepared
                       if item["migration_status"] == "RETAINED"
                       or item.get("postoperative_trajectory")),
                      key=lambda item: (_candidate_clock(item) or "", item["event_id"]))
    groups = []
    source_clock_groups = {}
    current = None
    for item in retained:
        candidate = item["candidate"]
        signature = _action_signature(candidate.get("candidate_action") or {})
        question_signature = _question_signature(candidate.get("candidate_action") or {})
        state_token = _state_token(candidate)
        increment = _has_state_increment(candidate)
        projection_key = (item["source_id"], _candidate_clock(item))
        same_source_group = (source_clock_groups.get(projection_key)
                             if item["source_id"] and projection_key[1] else None)
        if same_source_group is not None:
            same_source_group["items"].append(item)
            same_source_group["has_state_increment"] = (
                same_source_group["has_state_increment"] or increment)
            current = same_source_group
            continue
        evidence_arrived = bool(
            current and any(_candidate_clock(current["items"][-1]) < arrival
                            <= _candidate_clock(item) for arrival in evidence_arrivals))
        adjacent_same_question = bool(
            current and not increment and not evidence_arrived
            and question_signature[0]
            and current["question_signature"] == question_signature
            and current["postoperative_trajectory"] == bool(
                item.get("postoperative_trajectory"))
            and current["state_token"] == state_token)
        if current is None or not adjacent_same_question:
            current = dict(signature=signature, question_signature=question_signature,
                           state_token=state_token,
                           postoperative_trajectory=bool(item.get("postoperative_trajectory")),
                           has_state_increment=increment, items=[item])
            groups.append(current)
            if item["source_id"] and projection_key[1]:
                source_clock_groups[projection_key] = current
        else:
            current["items"].append(item)
            current["has_state_increment"] = current["has_state_increment"] or increment
            if item["source_id"] and projection_key[1]:
                source_clock_groups[projection_key] = current

    drafts = []
    for group in groups:
        items = group["items"]
        group_id = "CG-" + _digest([case_id, [item["event_id"] for item in items]])
        group["group_id"] = group_id
        for index, item in enumerate(items):
            item["group_id"] = group_id
            migration = migration_by_event[item["event_id"]]
            migration["group_id"] = group_id
            migration["migration_status"] = migration["status"] = (
                item["migration_status"] if index == 0 else "MERGED")
            migration["reason"] = (
                item["reason"] if index == 0
                else "MERGED_SAME_UNRESOLVED_MANAGEMENT_QUESTION")
            item["migration_status"] = migration["migration_status"]
        group["checkpoint"] = _management_checkpoint(case_id, group)
        group["attached_items"] = []
        group["diagnostic_event_ids"] = []
        drafts.append(group)
    return drafts


def _apply_checkpoint_adjudications(management_drafts, diagnostic_drafts,
                                    migration_by_event, checkpoint_adjudications):
    if checkpoint_adjudications is None:
        return management_drafts
    raw_items = (checkpoint_adjudications.get("items")
                 if isinstance(checkpoint_adjudications, dict)
                 else checkpoint_adjudications)
    if not isinstance(raw_items, list):
        raise ValueError("checkpoint_adjudications must be an item list or a dict with items")

    management_by_id = {
        draft["checkpoint"]["checkpoint_id"]: draft for draft in management_drafts}
    diagnostic_by_id = {
        draft["checkpoint"]["checkpoint_id"]: draft for draft in diagnostic_drafts}
    adjudications = {}
    comparators = {}
    for original in raw_items:
        if not isinstance(original, dict):
            raise ValueError("checkpoint adjudication items must be dicts")
        item = deepcopy(original)
        checkpoint_id = item.get("checkpoint_id")
        if checkpoint_id not in management_by_id:
            raise ValueError("unknown adjudicated management checkpoint: %s" % checkpoint_id)
        if checkpoint_id in adjudications:
            raise ValueError("duplicate checkpoint adjudication: %s" % checkpoint_id)
        disposition = item.get("disposition")
        evolution = item.get("evolution")
        if disposition not in ADJUDICATION_DISPOSITIONS:
            raise ValueError("unknown checkpoint adjudication disposition: %s" % disposition)
        if evolution not in ADJUDICATION_EVOLUTIONS:
            raise ValueError("unknown checkpoint adjudication evolution: %s" % evolution)
        comparator_id = item.get("comparator_checkpoint_id")
        if disposition == "SUPPORT_ONLY" and not comparator_id:
            raise ValueError("SUPPORT_ONLY requires comparator_checkpoint_id: %s" % checkpoint_id)
        if comparator_id:
            comparator = management_by_id.get(comparator_id) or diagnostic_by_id.get(comparator_id)
            if comparator is None:
                raise ValueError("unknown comparator checkpoint: %s" % comparator_id)
            if comparator["checkpoint"]["kind"] not in {BEFORE, AFTER, REASSESSMENT}:
                raise ValueError("comparator must be a management or diagnostic CP: %s"
                                 % comparator_id)
            comparators[checkpoint_id] = comparator_id
        adjudications[checkpoint_id] = item

    visiting, visited = set(), set()

    def check_cycle(checkpoint_id):
        if checkpoint_id in visiting:
            raise ValueError("cyclic checkpoint adjudication comparator chain: %s" % checkpoint_id)
        if checkpoint_id in visited:
            return
        visiting.add(checkpoint_id)
        comparator_id = comparators.get(checkpoint_id)
        if comparator_id in comparators:
            check_cycle(comparator_id)
        visiting.remove(checkpoint_id)
        visited.add(checkpoint_id)

    for checkpoint_id in comparators:
        check_cycle(checkpoint_id)
        if adjudications[checkpoint_id]["disposition"] == "REVIEW_REQUIRED":
            continue
        checkpoint = management_by_id[checkpoint_id]["checkpoint"]
        comparator = (management_by_id.get(comparators[checkpoint_id])
                      or diagnostic_by_id[comparators[checkpoint_id]])["checkpoint"]
        clock = _value(checkpoint["clock"])
        comparator_clock = _value(comparator["clock"])
        if not clock or not comparator_clock:
            raise ValueError("comparator clocks must be known: %s -> %s"
                             % (checkpoint_id, comparators[checkpoint_id]))
        relation = compare(comparator, checkpoint)["relation"]
        current_disposition = adjudications[checkpoint_id]["disposition"]
        if relation != "before" and not (
                current_disposition == "SUPPORT_ONLY" and relation == "same_time_unresolved"):
            raise ValueError("comparator must be strictly earlier, or same-clock SUPPORT_ONLY: %s -> %s"
                             % (checkpoint_id, comparators[checkpoint_id]))

    missing = sorted(set(management_by_id) - set(adjudications))
    if missing:
        raise ValueError("incomplete checkpoint adjudication coverage: %s" % ",".join(missing))

    def final_destination(checkpoint_id):
        diagnostic = diagnostic_by_id.get(checkpoint_id)
        if diagnostic is not None:
            return diagnostic
        draft = management_by_id[checkpoint_id]
        adjudication = adjudications.get(checkpoint_id)
        if adjudication is None or adjudication["disposition"] in {
                "RETAIN_CP", "REVIEW_REQUIRED"}:
            return draft
        comparator_id = comparators.get(checkpoint_id)
        return final_destination(comparator_id) if comparator_id else None

    retained = []
    for draft in management_drafts:
        checkpoint_id = draft["checkpoint"]["checkpoint_id"]
        adjudication = adjudications.get(checkpoint_id)
        if adjudication is None:
            retained.append(draft)
            continue
        disposition = adjudication["disposition"]
        if disposition in {"RETAIN_CP", "REVIEW_REQUIRED"}:
            draft["global_adjudication_retain"] = True
            draft["checkpoint"]["global_adjudication"] = deepcopy(adjudication)
            migration_status = "RETAINED" if disposition == "RETAIN_CP" else "REVIEW_REQUIRED"
            reason = ("GLOBAL_ADJUDICATION_" + adjudication["evolution"])
            for item in draft["items"]:
                item["migration_status"] = migration_status
                item["global_adjudication"] = True
                migration_by_event[item["event_id"]].update(
                    migration_status=migration_status, status=migration_status,
                    reason=reason, to_checkpoint_id=checkpoint_id)
            retained.append(draft)
            continue
        comparator_id = adjudication.get("comparator_checkpoint_id")
        destination = final_destination(checkpoint_id)
        if destination is None:
            raise ValueError("SUPPORT_ONLY comparator chain has no retained destination: %s"
                             % checkpoint_id)
        reason = "GLOBAL_ADJUDICATION_" + adjudication["evolution"]
        destination_id = destination["checkpoint"]["checkpoint_id"]
        destination["checkpoint"].setdefault(
            "supporting_adjudications", []).append(deepcopy(adjudication))
        for item in draft["items"]:
            item["migration_status"] = "SUPPORT_ONLY"
            item["global_adjudication"] = True
            item["attached_to"] = destination_id
            migration_by_event[item["event_id"]].update(
                migration_status="SUPPORT_ONLY", status="SUPPORT_ONLY", reason=reason,
                to_checkpoint_id=destination_id)
            if item not in destination["attached_items"]:
                destination["attached_items"].append(item)
    return retained


def _review_map(diagnostic_reviews):
    result = {}
    for original in diagnostic_reviews or []:
        review = deepcopy(original)
        unit_id = (review.get("discovery_unit_id") or review.get("unit_id")
                   or review.get("diagnostic_event_id"))
        kind = REVIEW_KINDS.get(review.get("candidate_kind"))
        if unit_id and kind:
            result[(unit_id, kind)] = review
    return result


def _visibility(event):
    if event.get("visibility_basis") is None:
        if _has_clock(event.get("report_time")):
            event["visibility_clock"] = deepcopy(event["report_time"])
            event["visibility_basis"] = "report_time"
        elif _has_clock(event.get("verified_time")):
            event["visibility_clock"] = deepcopy(event["verified_time"])
            event["visibility_basis"] = "verified_time_fallback"
        else:
            event["visibility_clock"] = _clock(None)
            event["visibility_basis"] = "unknown"
    return event


def _operation_day_order_unknown(clock, operation_start):
    value = _value(clock)
    operation_value = _value(operation_start)
    if not value or not operation_value or value[:10] != operation_value[:10]:
        return False
    coarse = {"date", "day", "unknown"}
    precision = (_clock(clock).get("precision") or "unknown").lower()
    operation_precision = (_clock(operation_start).get("precision") or "unknown").lower()
    return (len(value) <= 10 or len(operation_value) <= 10
            or precision in coarse or operation_precision in coarse)


def _is_pathology_event(event, source_by_id):
    values = [event.get(key) for key in (
        "event_type", "diagnostic_type", "modality", "category")]
    for source_id in event.get("source_ids") or []:
        source = source_by_id.get(source_id) or {}
        values.extend((source.get("kind"), source.get("document_profile"),
                       source.get("title")))
    text = " ".join(str(value or "") for value in values).lower()
    return "patholog" in text or "病理" in text


def _pre_exam_anchor(event, source_by_id):
    flags = (event.get("audit") or {}).get("clock_inconsistent_flags") or []
    exam_usable = _has_clock(event.get("exam_time")) and "exam_time" not in flags
    report_usable = _has_clock(event.get("report_time")) and "report_time" not in flags
    if exam_usable:
        return dict(basis="exam_time", clock=event.get("exam_time"), proxy=False)
    if _is_pathology_event(event, source_by_id) and "specimen_time" not in flags:
        raw_clocks = [event.get("specimen_time")]
        raw_clocks.extend((event.get("source_clocks") or {}).get(source_id, {}).get(
            "specimen_time") for source_id in event.get("source_ids") or [])
        clocks = [_clock(raw) for raw in raw_clocks if _has_clock(raw)]
        signatures = {(clock.get("value"), clock.get("precision")) for clock in clocks}
        if len(signatures) == 1:
            return dict(basis="specimen_time_proxy_for_pre_exam_evaluation",
                        clock=clocks[0], proxy=True)
    if report_usable:
        return dict(basis="report_time_fallback_for_pre_exam_evaluation",
                    clock=event.get("visibility_clock"), proxy=True)
    return dict(basis=None, clock=None, proxy=False)


def _diagnostic_gate(event, kind, review, operation_start, context_source_ids,
                     operation_start_clock=None, pre_anchor=None):
    flags = (event.get("audit") or {}).get("clock_inconsistent_flags") or []
    if (event.get("audit") or {}).get("placeholder_only_event"):
        if kind == BEFORE:
            return False, "PLACEHOLDER_ONLY_REPORT_NOT_ACQUISITION_CANDIDATE", None, None
        return False, "PLACEHOLDER_ONLY_REPORT_NOT_EVIDENCE_ARRIVAL", None, None
    pre_anchor = pre_anchor or _pre_exam_anchor(event, {})
    pre_basis = pre_anchor["basis"]
    pre_clock = pre_anchor["clock"]
    if review is None:
        return False, "DISCOVERY_REVIEW_REQUIRED", None, None
    if review.get("disposition") == "no":
        return False, "DISCOVERY_AGENT_REJECTED_OUT_OF_RESEARCH_SCOPE", None, None
    if review.get("disposition") != "yes":
        return False, "DISCOVERY_AGENT_REVIEW_UNCERTAIN", None, None
    if kind == BEFORE:
        if not pre_basis:
            return False, "MISSING_EXAM_AND_REPORT_TIME_FOR_EVALUATION_ANCHOR", None, None
        if _operation_day_order_unknown(pre_clock, operation_start_clock or operation_start):
            return False, "OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN", None, None
        if not context_source_ids:
            return False, "NO_VISIBLE_PRE_EXAM_CLINICAL_CONTEXT", None, None
        return True, pre_basis, pre_clock, pre_basis
    if not _has_clock(event.get("visibility_clock")):
        reason = ("MISSING_OR_CONFLICTING_REPORT_VISIBILITY" if flags else
                  "MISSING_REPORT_AND_VERIFIED_TIME_VISIBILITY_UNKNOWN")
        return False, reason, None, None
    if _operation_day_order_unknown(
            event["visibility_clock"], operation_start_clock or operation_start):
        return False, "OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN", None, None
    basis = ("report_time_as_visibility_proxy" if _has_clock(event.get("report_time"))
             else "verified_time_as_degraded_visibility_proxy")
    return True, event.get("visibility_basis") or basis, event["visibility_clock"], basis


def _diagnostic_checkpoint(case_id, event_group, kind, reviews, clocks, bases,
                           operation_start=None, specifications=None):
    event_ids = sorted(event["event_id"] for event in event_group)
    source_ids = sorted({source_id for event in event_group
                         for source_id in event.get("source_ids") or []})
    before = kind == BEFORE
    checkpoint_id = "CP-D-" + _digest([case_id, event_ids, kind])
    actions = _unique_candidate_actions(
        _candidate_action(review, event_id=event["event_id"])
        for event, review in zip(event_group, reviews))
    question = ("在目标证据获取前，针对PDAC根治性切除路径，下一步应推进、暂缓补证还是调整管理方向？"
                if before else "该批新证据可见后，下一步管理应如何安排？")
    basis = bases[0] if len(set(bases)) == 1 else "joint_projection_with_distinct_source_clocks"
    specifications = specifications or []
    review_flags = sorted({flag for spec in specifications
                           for flag in spec.get("review_flags") or []})
    preoperative_acquisition_after_operation = any(
        _value(event.get("exam_time"))
        and _value(event.get("exam_time")) < operation_start
        and len(_value(event.get("exam_time"))) > 10 and len(operation_start) > 10
        and (_clock(event.get("exam_time")).get("precision") or "unknown").lower()
        not in {"date", "day", "unknown"}
        for event in event_group) if operation_start else False
    if not before and operation_start and _value(clocks[0]) >= operation_start \
            and (preoperative_acquisition_after_operation
                 or any(_preoperative_action_conflict(review) for review in reviews)):
        review_flags = sorted(set(review_flags + ["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"]))
    evaluation_time_role = "before_acquisition_not_confirmed_choice_time" if before \
        else "after_report_visibility_proxy"
    if before and basis == "specimen_time_proxy_for_pre_exam_evaluation":
        evaluation_time_role = "before_specimen_collection_proxy_not_confirmed_choice_time"
    return dict(
        checkpoint_id=checkpoint_id,
        kind=kind,
        management_question=question,
        management_question_source="program_generated_task_header_without_target_type_leakage"
                                   if before else "program_generated_task_header",
        program_generated_task_header=True,
        clock=_clock(clocks[0]),
        clock_basis=basis,
        time_basis=basis,
        evaluation_time_role=evaluation_time_role,
        clock_proxy_basis=basis if before and any(
            spec.get("clock_proxy") for spec in specifications) else None,
        review_flags=review_flags,
        temporal_order_status=("DEGRADED_REVIEW_REQUIRED" if review_flags
                               else "NOT_DEGRADED"),
        source_ids=source_ids,
        support_source_ids=source_ids,
        member_event_ids=[],
        supporting_event_ids=[],
        masked_spans=[],
        record_functions=[],
        actionability_status="DISCOVERY_REVIEWED_MANAGEMENT_CANDIDATE",
        review_status="REVIEW_REQUIRED",
        candidate_status="CANDIDATE_NOT_CONFIRMED_CLINICAL_DECISION",
        candidate_actions=[action for action in actions if action],
        action_space=_action_space(),
        task_scope=_task_scope(),
        **_task_header_fields(),
        **_evaluation_scope(clocks[0], operation_start),
        diagnostic_event_id=event_ids[0],
        diagnostic_event_ids=event_ids,
        mask_whole_diagnostic_event=before,
        show_complete_diagnostic_event=not before,
        provenance=_provenance(
            diagnostic_event_ids=event_ids,
            joint_events_merged=len(event_ids) > 1,
            no_documented_choice_process_inferred=before,
            evidence_arrival_does_not_imply_evidence_used=not before),
    )


def _diagnostic_migration(case_id, event, kind, checkpoint_id, status, reason):
    return dict(
        migration_id="MG-" + _digest([case_id, "diagnostic_checkpoint", event["event_id"], kind]),
        migration_type="diagnostic_checkpoint",
        event_id=event["event_id"],
        source_ids=sorted(event.get("source_ids") or []),
        node_kind=kind,
        migration_status=status,
        status=status,
        reason=reason,
        to_checkpoint_id=checkpoint_id,
    )


def _diagnostic_modality(event, source_by_id, review):
    action = (review or {}).get("candidate_action") or {}
    values = [event.get(key) for key in ("event_type", "diagnostic_type", "modality")]
    values.extend((action.get("action_type"), action.get("target")))
    for source_id in event.get("source_ids") or []:
        source = source_by_id.get(source_id) or {}
        values.extend((source.get("title"), source.get("document_profile")))
    text = " ".join(str(value or "") for value in values).lower()
    modalities = (
        ("MRI", r"(?:^|[^a-z])(?:mr|mri)(?:[^a-z]|$)|磁共振"),
        ("CT", r"(?:^|[^a-z])ct(?:[^a-z]|$)"),
        ("EUS", r"(?:^|[^a-z])eus(?:[^a-z]|$)|超声内镜"),
        ("PATHOLOGY", r"patholog|病理"),
        ("LAPAROSCOPY", r"laparoscop|腹腔镜"),
        ("PET", r"(?:^|[^a-z])pet(?:[^a-z]|$)"),
    )
    return next((name for name, pattern in modalities if re.search(pattern, text)), None)


def _project_diagnostics(case_id, events, reviews_by_unit, sources, operation_start,
                         operation_start_clock=None):
    projected = [_visibility(deepcopy(event)) for event in events]
    source_by_id = {source.get("source_id"): source for source in sources}
    visibility_by_source = {
        source_id: _value(event.get("visibility_clock"))
        for event in projected for source_id in event.get("source_ids") or []
    }
    specifications = {}
    grouped = {BEFORE: {}, AFTER: {}}
    for event in projected:
        unit_id = event.get("discovery_unit_id") or event["event_id"]
        event["candidate_checkpoint_ids"] = {}
        event["generation_audits"] = []
        pre_anchor = _pre_exam_anchor(event, source_by_id)
        for kind in (BEFORE, AFTER):
            review = reviews_by_unit.get((unit_id, kind))
            pre_value = _value(pre_anchor["clock"])
            context_source_ids = []
            if kind == BEFORE and pre_value:
                for source in sources:
                    source_id = source.get("source_id")
                    if source_id in (event.get("source_ids") or []):
                        continue
                    visible = visibility_by_source.get(
                        source_id, _value((source_by_id.get(source_id) or {}).get("clock")))
                    if visible and visible <= pre_value:
                        context_source_ids.append(source_id)
            available, reason, clock, basis = _diagnostic_gate(
                event, kind, review, operation_start, context_source_ids,
                operation_start_clock=operation_start_clock, pre_anchor=pre_anchor)
            specifications[(event["event_id"], kind)] = dict(
                available=available, reason=reason, clock=clock, basis=basis, review=review,
                clock_proxy=pre_anchor["proxy"] if kind == BEFORE else True,
                review_flags=[])
            if available:
                group_key = (_value(clock),
                             _action_signature(review.get("candidate_action") or {}))
                joint_group_id = (event.get("joint_evidence_group_id")
                                  or event.get("joint_group_id"))
                if kind == BEFORE:
                    modality = _diagnostic_modality(event, source_by_id, review)
                    group_key += (("same_acquisition", modality) if modality
                                  else ("event", event["event_id"]),)
                else:
                    group_key += (("joint", joint_group_id) if joint_group_id
                                  else ("event", event["event_id"]),)
                grouped[kind].setdefault(group_key, []).append(event)
        before_spec = specifications[(event["event_id"], BEFORE)]
        after_spec = specifications[(event["event_id"], AFTER)]
        same_pre_post_clock = compare(
            dict(clock=before_spec["clock"]), dict(clock=after_spec["clock"])
        )["relation"] == "same_time_unresolved" or (
            _value(before_spec["clock"]) is not None
            and _value(before_spec["clock"]) == _value(after_spec["clock"]))
        if before_spec["available"] and after_spec["available"] and same_pre_post_clock:
            for spec in (before_spec, after_spec):
                spec["review_flags"].append(
                    "DEGRADED_PRE_POST_SAME_CLOCK_REVIEW_REQUIRED")

    drafts = []
    destinations = {}
    for kind in (BEFORE, AFTER):
        for _, event_group in sorted(grouped[kind].items()):
            event_group.sort(key=lambda event: event["event_id"])
            specs = [specifications[(event["event_id"], kind)] for event in event_group]
            checkpoint = _diagnostic_checkpoint(
                case_id, event_group, kind, [spec["review"] for spec in specs],
                [spec["clock"] for spec in specs], [spec["basis"] for spec in specs],
                operation_start=operation_start, specifications=specs)
            draft = dict(checkpoint=checkpoint, items=[], attached_items=[],
                         diagnostic_event_ids=[event["event_id"] for event in event_group],
                         events=event_group, has_state_increment=True)
            drafts.append(draft)
            for index, event in enumerate(event_group):
                reason = ("ACTIONABLE_DIAGNOSTIC_PROJECTION_NON_PRIMARY_TASK"
                          if not checkpoint["primary_evaluation_eligible"]
                          else "ACTIONABLE_DIAGNOSTIC_PROJECTION")
                destinations[(event["event_id"], kind)] = (
                    checkpoint["checkpoint_id"], "RETAINED" if index == 0 else "MERGED",
                    reason if index == 0
                    else "JOINT_EVIDENCE_WITHOUT_INCREMENTAL_STATE_OR_ACTION_MERGED")

    migrations = []
    for event in projected:
        for kind in (BEFORE, AFTER):
            spec = specifications[(event["event_id"], kind)]
            destination = destinations.get((event["event_id"], kind))
            if destination:
                checkpoint_id, status, reason = destination
            else:
                checkpoint_id, reason = None, spec["reason"]
                if reason == "DISCOVERY_AGENT_REJECTED_OUT_OF_RESEARCH_SCOPE":
                    status = "TASK_OUT_OF_SCOPE"
                elif reason == "MISSING_REPORT_AND_VERIFIED_TIME_VISIBILITY_UNKNOWN":
                    status = "NOT_GENERATED"
                else:
                    status = "REVIEW_REQUIRED"
            event["candidate_checkpoint_ids"][kind] = checkpoint_id
            event["generation_audits"].append(dict(
                node_kind=kind, generated=bool(checkpoint_id), checkpoint_id=checkpoint_id,
                reason=reason, discovery_disposition=(spec["review"] or {}).get("disposition"),
                review_flags=list(spec["review_flags"])))
            migrations.append(_diagnostic_migration(
                case_id, event, kind, checkpoint_id, status, reason))
    return projected, drafts, migrations


def _validate_action_review(review, checkpoint, *, management, task_time_bounds):
    checkpoint_id = review.get("checkpoint_id")
    if review.get("operation_start") != task_time_bounds.get("operation_start"):
        raise ValueError("action review operation_start mismatch: %s" % checkpoint_id)
    expected_source_ids = sorted({
        item["source_id"] for item in task_time_bounds.get("evidence") or []
        if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        and item["source_id"]})
    operation_source_ids = review.get("operation_source_ids")
    if not expected_source_ids or not isinstance(operation_source_ids, list) \
            or not operation_source_ids \
            or not all(isinstance(item, str) for item in operation_source_ids) \
            or operation_source_ids != sorted(set(operation_source_ids)) \
            or set(operation_source_ids) != set(expected_source_ids):
        raise ValueError("action review operation_source_ids mismatch: %s" % checkpoint_id)
    if review.get("actor") != ACTION_REVIEW_ACTOR:
        raise ValueError("invalid action review actor: %s" % checkpoint_id)
    if review.get("source_reads_verified") is not True:
        raise ValueError("action review source reads not verified: %s" % checkpoint_id)
    if not isinstance(review.get("rationale"), str) or not review["rationale"].strip():
        raise ValueError("action review rationale required: %s" % checkpoint_id)
    trigger_flags = review.get("trigger_flags")
    if not isinstance(trigger_flags, list) or ACTION_REVIEW_FLAG not in trigger_flags:
        raise ValueError("action review trigger flag missing: %s" % checkpoint_id)
    if ACTION_REVIEW_FLAG not in (checkpoint.get("review_flags") or []):
        raise ValueError("action review checkpoint lacks trigger flag: %s" % checkpoint_id)
    original_actions = review.get("original_candidate_actions")
    if not isinstance(original_actions, list) \
            or original_actions != (checkpoint.get("candidate_actions") or []):
        raise ValueError("action review original candidate actions mismatch: %s"
                         % checkpoint_id)
    disposition = review.get("disposition")
    if disposition not in ACTION_REVIEW_DISPOSITIONS:
        raise ValueError("unknown action review disposition: %s" % disposition)
    if management and disposition == "REMOVE_INDEPENDENT_CP":
        raise ValueError("cannot remove management CP by action review: %s" % checkpoint_id)
    action = review.get("candidate_action")
    if disposition == "REMOVE_INDEPENDENT_CP":
        if action is not None:
            raise ValueError("removed action review must not retain an action: %s"
                             % checkpoint_id)
        return
    if action is not None and not isinstance(action, dict):
        raise ValueError("retained action review action must be an object or null: %s"
                         % checkpoint_id)


def _reviewed_action(review, event_id):
    action = deepcopy(review["candidate_action"])
    action.update(
        rationale=review["rationale"], actor=ACTION_REVIEW_ACTOR,
        review_source="checkpoint_adjudication.action_reviews",
        answer_prefilled=False, clinical_reference_created=False,
        event_id=event_id)
    return action


def _apply_action_reviews(projected_diagnostics, diagnostic_drafts,
                          diagnostic_migrations, checkpoint_adjudications,
                          task_time_bounds):
    raw_reviews = (checkpoint_adjudications.get("action_reviews", [])
                   if isinstance(checkpoint_adjudications, dict) else [])
    if not isinstance(raw_reviews, list):
        raise ValueError("checkpoint_adjudications.action_reviews must be a list")
    reviews = []
    seen = set()
    for original in raw_reviews:
        if not isinstance(original, dict):
            raise ValueError("action review items must be dicts")
        review = deepcopy(original)
        checkpoint_id = review.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("action review checkpoint_id required")
        if checkpoint_id in seen:
            raise ValueError("duplicate action review: %s" % checkpoint_id)
        seen.add(checkpoint_id)
        reviews.append(review)

    diagnostic_by_id = {
        draft["checkpoint"]["checkpoint_id"]: draft for draft in diagnostic_drafts}
    required_ids = {checkpoint_id for checkpoint_id, draft in diagnostic_by_id.items()
                    if ACTION_REVIEW_FLAG in (draft["checkpoint"].get("review_flags") or [])}
    removed_diagnostic_ids = set()
    deferred = []
    reviewed_ids = set()
    dispositions = Counter()
    for review in reviews:
        checkpoint_id = review["checkpoint_id"]
        draft = diagnostic_by_id.get(checkpoint_id)
        if draft is None:
            deferred.append(review)
            continue
        checkpoint = draft["checkpoint"]
        _validate_action_review(
            review, checkpoint, management=False, task_time_bounds=task_time_bounds)
        reviewed_ids.add(checkpoint_id)
        dispositions[review["disposition"]] += 1
        if review["disposition"] == "RETAIN_CP_WITH_ACTION":
            _attach_action_review(checkpoint, review)
            checkpoint["candidate_actions"] = (
                [_reviewed_action(review, checkpoint["diagnostic_event_ids"][0])]
                if review.get("candidate_action") is not None else [])
            continue
        removed_diagnostic_ids.add(checkpoint_id)
        kind = checkpoint["kind"]
        for event_id in draft["diagnostic_event_ids"]:
            migration = next(item for item in diagnostic_migrations
                             if item["event_id"] == event_id and item["node_kind"] == kind)
            migration.update(
                migration_status="CONTEXT_ONLY", status="CONTEXT_ONLY",
                reason=ACTION_REVIEW_REMOVE_REASON, to_checkpoint_id=None)
            event = next(item for item in projected_diagnostics
                         if item["event_id"] == event_id)
            event["candidate_checkpoint_ids"][kind] = None
            for audit in event["generation_audits"]:
                if audit["node_kind"] == kind:
                    audit.update(
                        generated=False, checkpoint_id=None,
                        reason=ACTION_REVIEW_REMOVE_REASON)

    retained_diagnostics = [draft for draft in diagnostic_drafts
                            if draft["checkpoint"]["checkpoint_id"]
                            not in removed_diagnostic_ids]
    audit = dict(required_ids=required_ids, reviewed_ids=reviewed_ids,
                 dispositions=dispositions, deferred=deferred)
    return retained_diagnostics, audit


def _apply_management_action_reviews(management_drafts, action_review_audit,
                                     task_time_bounds):
    management_by_id = {
        draft["checkpoint"]["checkpoint_id"]: draft for draft in management_drafts}
    action_review_audit["required_ids"].update(
        checkpoint_id for checkpoint_id, draft in management_by_id.items()
        if ACTION_REVIEW_FLAG in (draft["checkpoint"].get("review_flags") or []))
    for review in action_review_audit["deferred"]:
        checkpoint_id = review["checkpoint_id"]
        draft = management_by_id.get(checkpoint_id)
        if draft is None:
            raise ValueError("unknown action review checkpoint: %s" % checkpoint_id)
        checkpoint = draft["checkpoint"]
        _validate_action_review(
            review, checkpoint, management=True, task_time_bounds=task_time_bounds)
        action_review_audit["reviewed_ids"].add(checkpoint_id)
        action_review_audit["dispositions"][review["disposition"]] += 1
        _attach_action_review(checkpoint, review)
        event_ids = checkpoint.get("member_event_ids") or []
        checkpoint["candidate_actions"] = (
            [_reviewed_action(review, event_ids[0] if event_ids else None)]
            if review.get("candidate_action") is not None else [])
    return management_drafts


def _attach_management_to_diagnostics(management_drafts, diagnostic_drafts,
                                      migration_by_event):
    retained = []
    for draft in management_drafts:
        if draft.get("global_adjudication_retain") \
                or any(item.get("postoperative_trajectory") for item in draft["items"]):
            retained.append(draft)
            continue
        checkpoint = draft["checkpoint"]
        actions = checkpoint["candidate_actions"]
        clock = _value(checkpoint["clock"])
        destination = None
        reason = None
        if actions and all(_is_diagnostic_action(action) for action in actions) \
                and not draft["has_state_increment"]:
            signatures = {_action_signature(action) for action in actions}
            candidates = [other for other in diagnostic_drafts
                          if other["checkpoint"]["kind"] == BEFORE
                          and clock and _value(other["checkpoint"]["clock"]) >= clock
                          and signatures <= {_action_signature(action)
                                             for action in other["checkpoint"]["candidate_actions"]}]
            if candidates:
                destination = min(candidates, key=lambda other: (
                    _value(other["checkpoint"]["clock"]), other["checkpoint"]["checkpoint_id"]))
                reason = "DIAGNOSTIC_ACQUISITION_ACTION_PROJECTED_AS_BEFORE_CP"
        elif actions:
            signatures = {_action_signature(action) for action in actions}
            input_evidence_ids = set(draft["state_token"]["input_evidence_ids"])
            candidates = [other for other in diagnostic_drafts
                          if other["checkpoint"]["kind"] == AFTER
                          and clock and clock >= _value(other["checkpoint"]["clock"])
                          and signatures <= {_action_signature(action)
                                             for action in other["checkpoint"]["candidate_actions"]}
                          and (not draft["has_state_increment"] or (
                              input_evidence_ids
                              and input_evidence_ids <= set(other["diagnostic_event_ids"])
                              and not any(_has_condition_or_action_increment(item["candidate"])
                                          for item in draft["items"])))]
            if candidates:
                destination = min(candidates, key=lambda other: (
                    _value(other["checkpoint"]["clock"]), other["checkpoint"]["checkpoint_id"]))
                reason = "SAME_ACTION_AFTER_EVIDENCE_ARRIVAL_CP_RETAINED"
        if destination is None:
            retained.append(draft)
            continue
        for item in draft["items"]:
            item["migration_status"] = "SUPPORT_ONLY"
            item["attached_to"] = destination["checkpoint"]["checkpoint_id"]
            migration = migration_by_event[item["event_id"]]
            migration.update(migration_status="SUPPORT_ONLY", status="SUPPORT_ONLY",
                             reason=reason,
                             to_checkpoint_id=destination["checkpoint"]["checkpoint_id"])
        destination["attached_items"].extend(draft["items"])
        for attached_item in draft["attached_items"]:
            attached_item["attached_to"] = destination["checkpoint"]["checkpoint_id"]
            if attached_item not in destination["attached_items"]:
                destination["attached_items"].append(attached_item)
    return retained


def _visible_diagnostic_state(diagnostic_events, clock, excluded_event_ids=()):
    cutoff = _value(clock)
    excluded = set(excluded_event_ids)
    if not cutoff:
        return None
    return frozenset(
        event["event_id"] for event in diagnostic_events
        if event["event_id"] not in excluded
        and _value(event.get("visibility_clock"))
        and _value(event["visibility_clock"]) <= cutoff)


def _absorb_redundant_before_checkpoints(management_drafts, diagnostic_drafts,
                                         diagnostic_events, migration_by_event,
                                         diagnostic_migration_by_key):
    retained_diagnostics = []
    redirects = {}
    merges = []
    reason = "SAME_EVIDENCE_STATE_MANAGEMENT_CP_RETAINED"
    for diagnostic in sorted(diagnostic_drafts, key=lambda draft: (
            _value(draft["checkpoint"]["clock"]) or "",
            draft["checkpoint"]["checkpoint_id"])):
        checkpoint = diagnostic["checkpoint"]
        if checkpoint["kind"] != BEFORE or not checkpoint["primary_evaluation_eligible"]:
            retained_diagnostics.append(diagnostic)
            continue
        clock = checkpoint["clock"]
        before_state = _visible_diagnostic_state(
            diagnostic_events, clock, diagnostic["diagnostic_event_ids"])
        candidates = []
        for management in management_drafts:
            management_clock = management["checkpoint"]["clock"]
            if not _value(management_clock) or not _value(clock) \
                    or _value(management_clock) >= _value(clock) \
                    or any(item.get("postoperative_trajectory")
                           or _has_condition_or_action_increment(item["candidate"])
                           for item in management["items"]):
                continue
            management_state = _visible_diagnostic_state(
                diagnostic_events, management_clock)
            if management_state is not None and management_state == before_state:
                candidates.append(management)
        if not candidates:
            retained_diagnostics.append(diagnostic)
            continue
        destination = max(candidates, key=lambda draft: (
            _value(draft["checkpoint"]["clock"]), draft["checkpoint"]["checkpoint_id"]))
        destination_checkpoint = destination["checkpoint"]
        destination_id = destination_checkpoint["checkpoint_id"]
        checkpoint_id = checkpoint["checkpoint_id"]
        destination_checkpoint["candidate_actions"] = _unique_candidate_actions(
            destination_checkpoint["candidate_actions"] + checkpoint["candidate_actions"])
        destination_checkpoint["diagnostic_event_ids"] = sorted(set(
            destination_checkpoint.get("diagnostic_event_ids") or [])
            | set(diagnostic["diagnostic_event_ids"]))
        destination["diagnostic_event_ids"] = list(
            destination_checkpoint["diagnostic_event_ids"])
        merge = dict(
            absorbed_checkpoint_id=checkpoint_id,
            destination_checkpoint_id=destination_id,
            absorbed_kind=BEFORE,
            clock=deepcopy(clock),
            diagnostic_event_ids=list(diagnostic["diagnostic_event_ids"]),
            reason=reason,
            evidence_state_comparison="deterministic_visible_diagnostic_event_ids",
        )
        destination_checkpoint.setdefault("absorbed_before_checkpoints", []).append(merge)
        merges.append(deepcopy(merge))
        redirects[checkpoint_id] = destination_id
        for item in diagnostic["attached_items"]:
            item["attached_to"] = destination_id
            if item not in destination["attached_items"]:
                destination["attached_items"].append(item)
            migration_by_event[item["event_id"]]["to_checkpoint_id"] = destination_id
        destination_checkpoint.setdefault("supporting_adjudications", []).extend(
            checkpoint.get("supporting_adjudications") or [])
        for event_id in diagnostic["diagnostic_event_ids"]:
            migration = diagnostic_migration_by_key[(event_id, BEFORE)]
            migration.update(migration_status="SUPPORT_ONLY", status="SUPPORT_ONLY",
                             reason=reason, to_checkpoint_id=destination_id)
        for event in diagnostic_events:
            if event["event_id"] not in diagnostic["diagnostic_event_ids"]:
                continue
            event["candidate_checkpoint_ids"][BEFORE] = destination_id
            for audit in event["generation_audits"]:
                if audit["node_kind"] == BEFORE:
                    audit.update(checkpoint_id=destination_id, reason=reason)
    return retained_diagnostics, redirects, merges


def _attach_support(prepared, drafts, migration_by_event):
    support_records = []
    for item in prepared:
        if item["migration_status"] not in {"SUPPORT_ONLY", "CONTEXT_ONLY"}:
            continue
        checkpoint_id = item.get("attached_to")
        destination = next((draft for draft in drafts
                            if draft["checkpoint"]["checkpoint_id"] == checkpoint_id), None)
        if destination is None:
            same_source = [draft for draft in drafts
                           if item["source_id"] in draft["checkpoint"].get("source_ids", [])]
            action = _candidate_action(item["candidate"])
            target = _normalized_action_target((action or {}).get("target"))
            same_action = [draft for draft in drafts if target and target in {
                _normalized_action_target(candidate_action.get("target"))
                for candidate_action in draft["checkpoint"]["candidate_actions"]}]
            candidates = same_source or same_action
            if candidates:
                destination = min(candidates, key=lambda draft: (
                    _value(draft["checkpoint"]["clock"]) or "",
                    draft["checkpoint"]["checkpoint_id"]))
        if destination is not None:
            if item not in destination["attached_items"]:
                destination["attached_items"].append(item)
            checkpoint_id = destination["checkpoint"]["checkpoint_id"]
            if not item.get("global_adjudication"):
                migration_by_event[item["event_id"]]["to_checkpoint_id"] = checkpoint_id
        support_records.append(dict(
            support_id="SUP-" + _digest([item["event_id"]]),
            event_id=item["event_id"],
            group_id=item.get("group_id"),
            source_ids=[item["source_id"]] if item["source_id"] else [],
            support_type=migration_by_event[item["event_id"]]["reason"],
            to_checkpoint_id=checkpoint_id,
            record_function=item["candidate"].get("record_function"),
            candidate_action=deepcopy(item["candidate"].get("candidate_action") or {}),
            span=deepcopy(item["candidate"].get("span") or {}),
            provenance=_provenance(candidate_span_retained_as_context=True),
        ))
    return support_records


def _phase(checkpoint, seen_nat, source_text=""):
    actions = checkpoint.get("candidate_actions") or []
    if seen_nat and re.search(
            r"已[^。；\n]{0,24}化疗|化疗后|第[一二三四五六七八九十0-9]+次化疗", source_text):
        return "POST_TREATMENT_REASSESSMENT"
    explicit = [action.get("phase") for action in actions if action.get("phase") in PHASES]
    if explicit:
        return explicit[0]
    text = " ".join(value for action in actions for value in (
        action.get("action_type") or "", action.get("target") or "")).lower()
    if any(term in text for term in ("neoadjuvant", "conversion", "chemotherap",
                                     "radiotherap", "新辅助", "转化治疗", "化疗", "化学治疗", "放疗")):
        return "NAT_OR_CONVERSION"
    if any(term in text for term in ("drainage", "nutrition", "optimization", "减黄",
                                     "引流", "营养支持", "术前优化")):
        return "PREOPERATIVE_OPTIMIZATION"
    if any(term in text for term in ("explor", "laparoscop", "laparotom", "探查")):
        return "SURGICAL_EXPLORATION"
    if seen_nat:
        return "POST_TREATMENT_REASSESSMENT"
    return "INITIAL_RESECTABILITY_ASSESSMENT"


def _new_diagnostic_events(diagnostic_events, comparator, checkpoint):
    if comparator is None:
        return []
    result = []
    for event in diagnostic_events:
        if (event.get("audit") or {}).get("placeholder_only_event"):
            continue
        visibility = event.get("visibility_clock")
        if not _has_clock(visibility):
            continue
        projected = dict(clock=visibility)
        if compare(comparator, projected)["relation"] != "before":
            continue
        relation_to_current = compare(projected, checkpoint)["relation"]
        same_value = _value(visibility) == _value(checkpoint.get("clock"))
        if relation_to_current == "before" or (
                relation_to_current == "same_time_unresolved" and same_value):
            result.append(event["event_id"])
    return sorted(result)


def _agent_evidence_projection(adjudication, diagnostic_events):
    supports = (adjudication or {}).get("evidence_supports") or []
    diagnostic_by_id = {event["event_id"]: event for event in diagnostic_events}
    cited_event_ids = set()
    assertions = []
    for support in supports:
        if isinstance(support, str):
            if support in diagnostic_by_id:
                cited_event_ids.add(support)
            else:
                assertions.append(dict(
                    reference=support, assertion_role="AGENT_CITED_NON_DIAGNOSTIC_FACT",
                    clinical_truth_confirmed=False))
            continue
        if not isinstance(support, dict):
            continue
        source_id = support.get("source_id")
        matched = [event["event_id"] for event in diagnostic_events
                   if source_id in (event.get("source_ids") or [])]
        if matched:
            cited_event_ids.update(matched)
            continue
        assertions.append(dict(
            source_id=source_id, quote=support.get("quote"),
            start=support.get("start"), end=support.get("end"),
            assertion_role="AGENT_CITED_SOURCE_FACT_NOT_CONFIRMED_CLINICAL_TRUTH",
            temporal_projection="SOURCE_CITATION_NOT_DETERMINISTIC_DIAGNOSTIC_AVAILABILITY",
            clinical_truth_confirmed=False))
    return sorted(cited_event_ids), assertions


def _finalize(case_id, management_drafts, diagnostic_drafts, diagnostic_events,
              migration_by_event, support_records, operation_start=None,
              checkpoint_redirects=None):
    drafts = management_drafts + diagnostic_drafts
    checkpoint_redirects = checkpoint_redirects or {}
    kind_order = {BEFORE: 0, REASSESSMENT: 1, AFTER: 2}
    drafts.sort(key=lambda draft: (
        _value(draft["checkpoint"]["clock"]) or "9999",
        kind_order.get(draft["checkpoint"]["kind"], 1),
        draft["checkpoint"]["checkpoint_id"]))
    checkpoint_by_id = {draft["checkpoint"]["checkpoint_id"]: draft["checkpoint"]
                        for draft in drafts}
    episode_id = "ME-" + _digest([case_id, "PDAC_CURATIVE_RESECTION_PATH"])
    checkpoints, cycles, states = [], [], []
    seen_nat = False
    for draft in drafts:
        checkpoint = draft["checkpoint"]
        own_items = draft.get("items") or []
        attached = draft.get("attached_items") or []
        all_items = own_items + [item for item in attached if item not in own_items]
        source_ids = set(checkpoint.get("source_ids") or [])
        source_ids.update(item["source_id"] for item in all_items if item["source_id"])
        checkpoint["source_ids"] = sorted(source_ids)
        checkpoint["support_source_ids"] = sorted(source_ids)
        checkpoint["supporting_event_ids"] = sorted(item["event_id"] for item in attached)
        checkpoint["record_functions"] = sorted({
            item["candidate"].get("record_function") for item in all_items
            if item["candidate"].get("record_function")})
        checkpoint["masked_spans"] = _masked_spans_for_items(
            all_items, {item["source_id"]: item["source"] for item in all_items})
        for item in own_items:
            migration_by_event[item["event_id"]]["to_checkpoint_id"] = checkpoint["checkpoint_id"]

        cutoff = _value(checkpoint["clock"])
        event_ids = set(draft.get("diagnostic_event_ids") or [])
        available = sorted(event["event_id"] for event in diagnostic_events
                           if _value(event.get("visibility_clock")) and cutoff
                           and _value(event["visibility_clock"]) <= cutoff
                           and (checkpoint["kind"] == AFTER
                                or event["event_id"] not in event_ids))
        discovery_input_evidence = sorted({
            evidence_id for item in own_items
            for evidence_id in item["candidate"].get("input_evidence_ids") or []})
        if checkpoint["kind"] == AFTER:
            input_evidence = sorted(event_ids)
        else:
            input_evidence = available
        adjudication = checkpoint.get("global_adjudication") or {}
        comparator_id = adjudication.get("comparator_checkpoint_id")
        comparator = checkpoint_by_id.get(checkpoint_redirects.get(comparator_id, comparator_id))
        if checkpoint["kind"] == AFTER:
            new_evidence = sorted(event_ids)
        elif checkpoint["kind"] == REASSESSMENT \
                and adjudication.get("evolution") == "NEW_EVIDENCE_ROUND":
            new_evidence = _new_diagnostic_events(
                diagnostic_events, comparator, checkpoint)
        else:
            new_evidence = []
        agent_cited_events, non_diagnostic_assertions = _agent_evidence_projection(
            adjudication, diagnostic_events)
        evolution_status = "NO_NEW_EVIDENCE_EVOLUTION_CLAIM"
        if adjudication.get("evolution") == "FIRST_VISIBLE":
            evolution_status = "FIRST_VISIBLE_NOT_AN_EVOLUTION"
        elif adjudication.get("evolution") == "NEW_EVIDENCE_ROUND":
            if comparator is None:
                evolution_status = "REVIEW_REQUIRED_MISSING_COMPARATOR_STATE"
            elif not new_evidence and not non_diagnostic_assertions:
                evolution_status = "REVIEW_REQUIRED_EMPTY_NEW_EVIDENCE_STATE"
            else:
                evolution_status = "AGENT_ADJUDICATED_EVIDENCE_REVIEW_REQUIRED"
        source_text = "\n".join((item["candidate"].get("span") or {}).get("quote") or ""
                                for item in all_items)
        evaluation_scope = _evaluation_scope(cutoff, operation_start)
        phase = ("POSTOPERATIVE_TRAJECTORY"
                 if not evaluation_scope["primary_evaluation_eligible"]
                 else _phase(checkpoint, seen_nat, source_text))
        seen_nat = seen_nat or phase == "NAT_OR_CONVERSION"
        state_id = "ES-" + _digest([
            case_id, checkpoint["checkpoint_id"], available, input_evidence, new_evidence])
        cycle_id = "AC-" + _digest([case_id, checkpoint["checkpoint_id"]])
        review_flags = set(checkpoint.get("review_flags") or [])
        if evolution_status.startswith("REVIEW_REQUIRED"):
            review_flags.add(evolution_status)
        checkpoint.update(episode_id=episode_id, phase=phase, evidence_state_id=state_id,
                          management_goal="PDAC_CURATIVE_RESECTION_PATH",
                          input_evidence_ids=input_evidence,
                          new_evidence_event_ids=new_evidence,
                          agent_cited_diagnostic_event_ids=agent_cited_events,
                          non_diagnostic_evidence_assertions=non_diagnostic_assertions,
                          evidence_evolution_status=evolution_status,
                          review_flags=sorted(review_flags),
                          action_cycle_id=cycle_id, **evaluation_scope)
        states.append(dict(
            evidence_state_id=state_id, episode_id=episode_id, phase=phase,
            clock=deepcopy(checkpoint["clock"]),
            available_diagnostic_event_ids=available,
            new_evidence_event_ids=new_evidence,
            input_evidence_ids=input_evidence,
            agent_cited_diagnostic_event_ids=agent_cited_events,
            agent_evidence_use_status=("AGENT_CITED_REVIEW_REQUIRED" if agent_cited_events
                                       or non_diagnostic_assertions
                                       else "NO_AGENT_EVIDENCE_USE_CLAIM"),
            non_diagnostic_evidence_assertions=non_diagnostic_assertions,
            evidence_evolution_status=evolution_status,
            review_flags=sorted(review_flags),
            checkpoint_ids=[checkpoint["checkpoint_id"]],
            review_status="REVIEW_REQUIRED",
            provenance=_provenance(
                state_is_time_projection_not_confirmed_evidence_use=True,
                discovery_input_evidence_ids=discovery_input_evidence,
                discovery_input_reference_role="PROVENANCE_ONLY_NOT_AGENT_EVIDENCE_USE",
                comparator_checkpoint_id=adjudication.get("comparator_checkpoint_id")),
        ))
        cycles.append(dict(
            action_cycle_id=cycle_id, episode_id=episode_id, phase=phase,
            goal="PDAC_CURATIVE_RESECTION_PATH",
            evidence_state_id=state_id, input_evidence_ids=input_evidence,
            candidate_action_space=deepcopy(checkpoint["action_space"]),
            candidate_actions=deepcopy(checkpoint["candidate_actions"]),
            support_source_ids=list(checkpoint["support_source_ids"]),
            member_event_ids=list(checkpoint["member_event_ids"]),
            supporting_event_ids=list(checkpoint["supporting_event_ids"]),
            diagnostic_event_ids=sorted(event_ids),
            new_evidence_event_ids=new_evidence,
            agent_cited_diagnostic_event_ids=agent_cited_events,
            non_diagnostic_evidence_assertions=deepcopy(non_diagnostic_assertions),
            checkpoint_ids=[checkpoint["checkpoint_id"]],
            review_status="REVIEW_REQUIRED",
            review_flags=sorted(review_flags),
            **evaluation_scope,
            provenance=_provenance(action_cycle_is_candidate_not_reference=True),
        ))
        checkpoints.append(checkpoint)

    for support in support_records:
        cycle = next((cycle for cycle in cycles
                      if cycle["checkpoint_ids"][0] == support.get("to_checkpoint_id")), None)
        support["action_cycle_id"] = cycle["action_cycle_id"] if cycle else None
    episode = dict(
        episode_id=episode_id,
        goal="PDAC_CURATIVE_RESECTION_PATH",
        goal_note="持续根治性切除管理目标；CT/MR/EUS/病理/NAT/手术评估不按检查类型拆分。",
        checkpoint_ids=[checkpoint["checkpoint_id"] for checkpoint in checkpoints],
        action_cycle_ids=[cycle["action_cycle_id"] for cycle in cycles],
        phase_transitions=[],
        review_status="REVIEW_REQUIRED",
        provenance=_provenance(episode_is_program_organization_not_confirmed_clinical_episode=True),
    )
    previous = None
    for checkpoint in checkpoints:
        phase = checkpoint["phase"]
        if phase != previous:
            episode["phase_transitions"].append(dict(
                from_phase=previous, to_phase=phase,
                checkpoint_id=checkpoint["checkpoint_id"],
                clock=deepcopy(checkpoint["clock"])))
            previous = phase
    return checkpoints, cycles, states, [episode] if checkpoints else []


def project_management_trajectory(*, case_id, candidates, sources, diagnostic_events,
                                  diagnostic_reviews=None, task_time_bounds=None,
                                  checkpoint_adjudications=None):
    """Project one continuous curative goal into reviewable state/action cycles."""
    validate_candidate_spans(candidates, sources)
    if task_time_bounds is None:
        from .diagnostic_events import derive_preoperative_task_bounds
        task_time_bounds = derive_preoperative_task_bounds(sources)
    operation_start = _value(task_time_bounds.get("operation_start"))
    source_by_id = {source.get("source_id"): source for source in sources}
    prepared = []
    for index, original in enumerate(candidates):
        candidate = deepcopy(original)
        event_id = candidate.get("event_id") or candidate.get("record_id") or (
            "candidate-%d" % index)
        source_id = _source_id(candidate)
        source = source_by_id.get(source_id)
        status, reason = _classify(candidate, source)
        action = candidate.get("candidate_action") or {}
        action_text = " ".join((action.get("action_type") or "", action.get("target") or ""))
        quote = (candidate.get("span") or {}).get("quote") or ""
        if status == "RETAINED" and (
                _is_admission_intent(quote + " " + action_text)
                or "入院常规及特殊检查" in action_text):
            status, reason = "SUPPORT_ONLY", "ADMISSION_OR_GENERIC_WORKUP_NOT_INDEPENDENT_DECISION"
        effective_clock = (candidate.get("clock") if _has_clock(candidate.get("clock"))
                           else (source or {}).get("clock"))
        candidate_time = _candidate_clock(dict(candidate=candidate, source=source))
        postoperative_trajectory = False
        if status == "RETAINED" and operation_start and candidate_time:
            if _operation_day_order_unknown(
                    effective_clock, task_time_bounds.get("operation_start")):
                status, reason = "REVIEW_REQUIRED", "OPERATION_DAY_VISIBILITY_ORDER_UNKNOWN"
            elif candidate_time >= operation_start and not _routine_postoperative(
                    dict(candidate=candidate, source=source), operation_start):
                postoperative_trajectory = True
                status = "REVIEW_REQUIRED"
                reason = ("ACTION_TASK_MISMATCH_REVIEW_REQUIRED"
                          if _preoperative_action_conflict(candidate)
                          else "POSTOPERATIVE_TRAJECTORY_NON_PRIMARY_REVIEW_REQUIRED")
        if status == "SUPPORT_ONLY" and candidate.get("record_function") == "repeat" \
                and _has_state_increment(candidate) and _valid_span(candidate) \
                and _candidate_action(candidate) is not None \
                and (_has_clock(candidate.get("clock"))
                     or _has_clock((source or {}).get("clock"))):
            status, reason = "RETAINED", "REPEAT_WITH_STATE_INCREMENT_RETAINED"
        if status == "RETAINED" and _routine_postoperative(
                dict(candidate=candidate, source=source), operation_start):
            status, reason = "TASK_OUT_OF_SCOPE", "POSTOPERATIVE_ROUTINE_MANAGEMENT_OUT_OF_MAIN_TASK"
        prepared.append(dict(candidate=candidate, event_id=event_id, source_id=source_id,
                             source=source, migration_status=status, reason=reason,
                             postoperative_trajectory=postoperative_trajectory,
                             input_index=index, group_id=None))
    migrations = [_migration(case_id, item, item["migration_status"], item["reason"])
                  for item in prepared]
    migration_by_event = {migration["event_id"]: migration for migration in migrations}
    projected_diagnostics, diagnostic_drafts, diagnostic_migrations = _project_diagnostics(
        case_id, diagnostic_events, _review_map(diagnostic_reviews), sources, operation_start,
        operation_start_clock=task_time_bounds.get("operation_start"))
    evidence_arrivals = sorted({_value(draft["checkpoint"]["clock"])
                                for draft in diagnostic_drafts
                                if draft["checkpoint"]["kind"] == AFTER} - {None})
    diagnostic_drafts, action_review_audit = _apply_action_reviews(
        projected_diagnostics, diagnostic_drafts, diagnostic_migrations,
        checkpoint_adjudications, task_time_bounds)
    management_drafts = _group_management(
        case_id, prepared, migration_by_event, evidence_arrivals)
    management_drafts = _apply_management_action_reviews(
        management_drafts, action_review_audit, task_time_bounds)
    management_drafts = _apply_checkpoint_adjudications(
        management_drafts, diagnostic_drafts, migration_by_event,
        checkpoint_adjudications)
    management_drafts = _attach_management_to_diagnostics(
        management_drafts, diagnostic_drafts, migration_by_event)
    diagnostic_migration_by_key = {
        (migration["event_id"], migration["node_kind"]): migration
        for migration in diagnostic_migrations}
    diagnostic_drafts, checkpoint_redirects, checkpoint_merges = \
        _absorb_redundant_before_checkpoints(
            management_drafts, diagnostic_drafts, projected_diagnostics,
            migration_by_event, diagnostic_migration_by_key)
    all_drafts = management_drafts + diagnostic_drafts
    support_records = _attach_support(prepared, all_drafts, migration_by_event)
    checkpoints, cycles, states, episodes = _finalize(
        case_id, management_drafts, diagnostic_drafts, projected_diagnostics,
        migration_by_event, support_records, operation_start=operation_start,
        checkpoint_redirects=checkpoint_redirects)
    migrations.extend(diagnostic_migrations)

    candidate_migrations = [item for item in migrations
                            if item["migration_type"] == "candidate_span"]
    diagnostic_count = len(diagnostic_events) * 2
    action_reviewed_ids = action_review_audit["reviewed_ids"]
    action_review_required_ids = action_review_audit["required_ids"]
    missing_action_review_ids = sorted(action_review_required_ids - action_reviewed_ids)
    action_review_disposition_counts = dict(sorted(
        action_review_audit["dispositions"].items()))
    summary = dict(
        format_version=PROJECTION_VERSION,
        candidate_input_count=len(candidates),
        candidate_migration_count=len(candidate_migrations),
        candidate_migration_conserved=len(candidate_migrations) == len(candidates),
        diagnostic_input_count=len(diagnostic_events),
        diagnostic_migration_count=len(diagnostic_migrations),
        diagnostic_migration_conserved=len(diagnostic_migrations) == diagnostic_count,
        migration_conserved=(len(migrations) == len(candidates) + diagnostic_count),
        candidate_checkpoint_count=len(checkpoints),
        management_episode_count=len(episodes),
        action_cycle_count=len(cycles),
        evidence_state_count=len(states),
        diagnostic_review_count=len(diagnostic_reviews or []),
        action_review_count=len(action_reviewed_ids),
        action_reviewed_count=len(action_reviewed_ids),
        action_reviewed_checkpoint_ids=sorted(action_reviewed_ids),
        action_review_disposition_counts=action_review_disposition_counts,
        action_review_required_count=len(action_review_required_ids),
        action_review_required_ids=sorted(action_review_required_ids),
        missing_action_review_count=len(missing_action_review_ids),
        missing_action_review_checkpoint_ids=missing_action_review_ids,
        diagnostic_events_with_multiple_checkpoints=sum(
            1 for event in projected_diagnostics
            if len({checkpoint_id for checkpoint_id in event["candidate_checkpoint_ids"].values()
                    if checkpoint_id}) > 1),
        support_record_count=len(support_records),
        before_acquisition_merge_count=len(checkpoint_merges),
        migration_status_counts=dict(sorted(Counter(
            item["migration_status"] for item in migrations).items())),
        migration_type_counts=dict(sorted(Counter(
            item["migration_type"] for item in migrations).items())),
        migration_reason_counts=dict(sorted(Counter(
            item["reason"] for item in migrations).items())),
        candidate_checkpoint_kind_counts=dict(sorted(Counter(
            item["kind"] for item in checkpoints).items())),
        primary_evaluation_checkpoint_count=sum(
            item["primary_evaluation_eligible"] for item in checkpoints),
        non_primary_evaluation_checkpoint_count=sum(
            not item["primary_evaluation_eligible"] for item in checkpoints),
        task_time_bounds=deepcopy(task_time_bounds),
        clinical_accuracy_assessed=False,
        note="离线程序状态投影与迁移计数，不代表临床准确性或专家Reference。",
    )
    return dict(
        format_version=PROJECTION_VERSION,
        candidate_checkpoints=checkpoints,
        checkpoint_merges=checkpoint_merges,
        migrations=migrations,
        support_records=support_records,
        diagnostic_events=projected_diagnostics,
        management_episodes=episodes,
        action_cycles=cycles,
        evidence_states=states,
        summary=summary,
    )


__all__ = ["project_management_trajectory"]

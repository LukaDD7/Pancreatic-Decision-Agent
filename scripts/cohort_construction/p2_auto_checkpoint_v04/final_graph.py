"""Deterministic final-review dependency audit (CE-AUD-001).

This module checks the *final* reviewed objects, not clinical correctness.  It
never rewrites ``semantic_review``, invents a predecessor, or promotes a record
to a first Checkpoint.  Repeated/executed records and non-Checkpoint records can
legitimately carry a historical clinical judgment; their classification alone
does not invalidate a dependency.
"""
from copy import deepcopy
from datetime import datetime


AUDIT_VERSION = "final-graph-audit-v1"
ISSUE_ID = "CE-AUD-001"
COMPARISON = "evidence_comparison_previous"
PROCESS = "same_management_process"
IDENTITY = "same_event"


def _review(event):
    return event.get("semantic_review") or {}


def _value(event, field):
    return _review(event).get(field, event.get(field))


def _supported(citations):
    citations = citations or {}
    return bool(citations.get("valid") or any(
        item.get("eligible_as_source_support") for item in citations.get("ambiguous", [])))


def _explicit_time(event):
    if event.get("time_basis") != "explicit_documented_choice" or not event.get("choice_time"):
        return None
    try:
        return datetime.fromisoformat(event["choice_time"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _final_classification_reasons(event, prefix):
    reasons = []
    if _value(event, "scope") == "no":
        reasons.append(prefix + "_FINAL_SCOPE_NO")
    if _value(event, "record_function") == "generic":
        reasons.append(prefix + "_FINAL_FUNCTION_GENERIC")
    return reasons


def _base_guard(event, target_id, relation, byid):
    """Check one declared relation; do not infer missing semantic relations."""
    eid = event["event_id"]
    target = byid.get(target_id)
    hard, soft = [], []
    if target is None:
        hard.append("TARGET_EVENT_MISSING")
    if target_id == eid and relation != IDENTITY:
        hard.append("SELF_PREDECESSOR")
    if target_id == eid and relation == IDENTITY:
        soft.append("SELF_IDENTITY_REDUNDANT")
    if relation in (COMPARISON, PROCESS):
        provided = event.get("provided_prior_event_ids")
        if provided is not None and target_id not in provided:
            hard.append("TARGET_NOT_IN_PROVIDED_PRIOR_EVENT_IDS")
    if relation == IDENTITY:
        provided = event.get("provided_identity_event_ids")
        if provided is not None and target_id not in provided and target_id != eid:
            hard.append("TARGET_NOT_IN_PROVIDED_IDENTITY_EVENT_IDS")
    hard.extend(_final_classification_reasons(event, "SOURCE"))
    if target:
        hard.extend(_final_classification_reasons(target, "TARGET"))
        if _value(target, "scope") not in ("yes", "no"):
            soft.append("TARGET_FINAL_SCOPE_UNCERTAIN")
        function = _value(target, "record_function")
        if function in ("repeat", "execution"):
            soft.append("TARGET_REPEAT_OR_EXECUTION_MAY_CARRY_PRIOR_JUDGMENT")
        if function == "uncertain":
            soft.append("TARGET_FINAL_FUNCTION_UNCERTAIN")
        if not target.get("checkpoint_status", "").startswith("CANDIDATE_"):
            soft.append("TARGET_IS_NOT_A_CANDIDATE_CHECKPOINT")
        if target.get("checkpoint_status", "").startswith("UNCERTAIN"):
            soft.append("TARGET_CHECKPOINT_STATUS_UNCERTAIN")
        if relation in (COMPARISON, PROCESS):
            old, current = _explicit_time(target), _explicit_time(event)
            if old is not None and current is not None:
                try:
                    if old >= current:
                        hard.append("TARGET_EXPLICIT_CLINICAL_TIME_NOT_EARLIER")
                except TypeError:
                    soft.append("EXPLICIT_CLINICAL_TIME_ZONES_NOT_COMPARABLE")
    if relation in (PROCESS, IDENTITY):
        if not _supported(event.get("validated_connection_citations")):
            hard.append("RELATION_SOURCE_PROOF_UNVERIFIED")
        if event.get("discovery_source_supported") is False:
            hard.append("SOURCE_ACTION_PROOF_UNVERIFIED")
        if target and target.get("discovery_source_supported") is False:
            hard.append("TARGET_ACTION_PROOF_UNVERIFIED")
        if relation == PROCESS and _review(event).get("process_link") != "supported":
            hard.append("MODEL_PROCESS_LINK_NOT_SUPPORTED")
    return dict(relation=relation, source_event_id=target_id, target_event_id=eid,
                declared_reference_event_id=target_id, resolved_reference_event_id=target_id,
                allowed=not hard, hard_reasons=sorted(set(hard)),
                soft_reasons=sorted(set(soft)), identity_resolution=None)


def _cyclic_edges(edges):
    """Return edges in directed cycles; identity equivalence is not passed here."""
    bysource = {}
    for source, target in edges:
        bysource.setdefault(source, set()).add(target)
    cyclic = set()
    for start, end in edges:
        pending = [end]; seen = set()
        while pending:
            node = pending.pop()
            if node == start:
                cyclic.add((start, end))
                break
            if node in seen:
                continue
            seen.add(node); pending.extend(bysource.get(node, ()))
    return cyclic


def _identity_resolution(target_id, byid, identity_edges):
    """Resolve only a source-supported identity component with a unique judgment.

    No earliest/nearest event heuristic is used.  An explicitly recorded
    canonical id is accepted only when its same-event path is also verified.
    """
    adjacency = {}
    for record in identity_edges:
        if not record["allowed"] or record["source_event_id"] == record["target_event_id"]:
            continue
        a, b = record["source_event_id"], record["target_event_id"]
        adjacency.setdefault(a, set()).add(b); adjacency.setdefault(b, set()).add(a)
    pending, members = [target_id], set()
    while pending:
        node = pending.pop()
        if node in members:
            continue
        members.add(node); pending.extend(adjacency.get(node, ()))
    if len(members) == 1:
        return None
    judgments = sorted(eid for eid in members if eid in byid and
                       _value(byid[eid], "scope") == "yes" and
                       _value(byid[eid], "record_function") in ("new_decision", "reassessment"))
    explicit = byid[target_id].get("canonical_event_id")
    canonical = explicit if explicit in judgments else judgments[0] if len(judgments) == 1 else None
    supporting = [deepcopy(r) for r in identity_edges if r["allowed"] and
                  r["source_event_id"] in members and r["target_event_id"] in members]
    return dict(status="resolved" if canonical else "unresolved_multiple_judgments",
                canonical_event_id=canonical, member_event_ids=sorted(members),
                basis="verified_same_event_component", supporting_relations=supporting)


def audit_final_graph(events):
    """Return ``(deepcopy_events, audit_report)`` without changing model proposals.

    A hard contradiction in a new-information comparison dependency demotes an
    otherwise candidate Checkpoint to ``UNCERTAIN_FINAL_GRAPH``.  Invalid
    process/identity relations are blocked independently.  Soft target-status
    signals remain visible without being turned into clinical-error labels.
    """
    result = deepcopy(events)
    ids = [event["event_id"] for event in result]
    if len(ids) != len(set(ids)):
        raise ValueError("DUPLICATE_EVENT_ID_IN_FINAL_GRAPH")
    for event in result:
        previous_audit = event.pop("final_graph_audit", None)
        if previous_audit and previous_audit.get("version") == AUDIT_VERSION:
            event["checkpoint_status"] = previous_audit["before"]["checkpoint_status"]
            # Remove only reasons introduced by this exact audit, preserving
            # all pre-existing validation evidence and its original order.
            event["validation_reasons"] = deepcopy(previous_audit["before"]["validation_reasons"])
    byid = {event["event_id"]: event for event in result}
    dependencies = []
    for event in sorted(result, key=lambda item: item["event_id"]):
        review = _review(event)
        for field, relation in (("previous_event_id", COMPARISON), ("same_process_previous_id", PROCESS)):
            target_id = review.get(field, event.get(field))
            if target_id:
                dependencies.append(_base_guard(event, target_id, relation, byid))
            elif relation == COMPARISON and event.get("checkpoint_status") == "CANDIDATE_NEW_EVIDENCE":
                dependencies.append(_base_guard(event, None, relation, byid))
        for target_id in sorted(set(review.get("same_event_with", event.get("same_event_with", [])) or [])):
            dependencies.append(_base_guard(event, target_id, IDENTITY, byid))
    for relation in (COMPARISON, PROCESS):
        records = [record for record in dependencies if record["relation"] == relation]
        # Detect cycles in the declared graph even when an order/license guard
        # has already failed; they remain separate audit explanations.
        edges = [(r["target_event_id"], r["source_event_id"]) for r in records
                 if r["source_event_id"] in byid]
        cyclic = _cyclic_edges(edges)
        for record in records:
            if (record["target_event_id"], record["source_event_id"]) in cyclic:
                record["hard_reasons"] = sorted(set(record["hard_reasons"] + ["DIRECTED_DEPENDENCY_CYCLE"]))
                record["allowed"] = False
    identity_edges = [record for record in dependencies if record["relation"] == IDENTITY]
    for record in dependencies:
        if record["relation"] not in (COMPARISON, PROCESS) or record["source_event_id"] not in byid:
            continue
        resolution = _identity_resolution(record["source_event_id"], byid, identity_edges)
        record["identity_resolution"] = resolution
        if resolution and resolution["status"] == "resolved":
            record["resolved_reference_event_id"] = resolution["canonical_event_id"]
            # Identity equivalence is not a time/payload permission.  A carrier
            # can have been supplied as an earlier record while its canonical
            # judgment was not, or the canonical judgment's verified clinical
            # time can contradict the historical dependency.  Validate the
            # resolved reference with the same policy as the declared one.
            canonical_guard = _base_guard(byid[record["target_event_id"]],
                                         record["resolved_reference_event_id"],
                                         record["relation"], byid)
            resolution["canonical_dependency_validation"] = canonical_guard
            record["hard_reasons"] = sorted(set(record["hard_reasons"] + [
                "RESOLVED_REFERENCE_" + reason for reason in canonical_guard["hard_reasons"]]))
            record["soft_reasons"] = sorted(set(record["soft_reasons"] + [
                "RESOLVED_REFERENCE_" + reason for reason in canonical_guard["soft_reasons"]]))
            record["allowed"] = record["allowed"] and canonical_guard["allowed"]
            if record["resolved_reference_event_id"] == record["target_event_id"]:
                record["hard_reasons"] = sorted(set(record["hard_reasons"] + ["DEPENDENCY_RESOLVES_TO_CURRENT_EVENT"]))
                record["allowed"] = False
        elif resolution:
            record["soft_reasons"] = sorted(set(record["soft_reasons"] + ["SAME_EVENT_CANONICAL_UNRESOLVED"]))
    for relation in (COMPARISON, PROCESS):
        records = [record for record in dependencies if record["relation"] == relation]
        edges = [(r["target_event_id"], r["resolved_reference_event_id"]) for r in records
                 if r["resolved_reference_event_id"] in byid]
        cyclic = _cyclic_edges(edges)
        for record in records:
            if ((record["target_event_id"], record["resolved_reference_event_id"]) in cyclic and
                    record["identity_resolution"]):
                record["hard_reasons"] = sorted(set(record["hard_reasons"] + [
                    "RESOLVED_REFERENCE_DIRECTED_DEPENDENCY_CYCLE"]))
                record["allowed"] = False
    event_audits = []
    for event in result:
        own = [deepcopy(record) for record in dependencies if record["target_event_id"] == event["event_id"]]
        before = dict(checkpoint_status=event.get("checkpoint_status"),
                      validation_reasons=deepcopy(event.get("validation_reasons", [])))
        own_hard = _final_classification_reasons(event, "SOURCE")
        hard_comparison = [reason for record in own if record["relation"] == COMPARISON
                           for reason in record["hard_reasons"]]
        should_demote = bool(own_hard or hard_comparison) and str(before["checkpoint_status"]).startswith("CANDIDATE_")
        if should_demote:
            event["checkpoint_status"] = "UNCERTAIN_FINAL_GRAPH"
        reasons = sorted(set(own_hard + ["FINAL_GRAPH_" + reason for record in own for reason in record["hard_reasons"]]))
        if reasons:
            event["validation_reasons"] = list(dict.fromkeys(before["validation_reasons"] + reasons))
        audit = dict(version=AUDIT_VERSION, issue_id=ISSUE_ID, before=before,
                     checkpoint_demoted=should_demote, hard_reasons=reasons,
                     soft_reasons=sorted({reason for record in own for reason in record["soft_reasons"]}),
                     dependencies=own, semantic_review_preserved=True)
        event["final_graph_audit"] = audit
        event_audits.append(dict(event_id=event["event_id"], **deepcopy(audit)))
    report = dict(version=AUDIT_VERSION, issue_id=ISSUE_ID,
                  policy="structural_audit_not_clinical_reference", event_count=len(result),
                  demoted_checkpoint_count=sum(a["checkpoint_demoted"] for a in event_audits),
                  blocked_relation_count=sum(not r["allowed"] for r in dependencies if r["relation"] != COMPARISON),
                  dependencies=dependencies, events=event_audits)
    return result, report


def relation_guard(event, target_id, relation, events):
    """Return the final audit record used to guard an assembly union operation.

    ``events`` may be the event list or an ``event_id -> event`` mapping.  The
    caller must retain the original declared IDs in its exported relation and
    may expose ``resolved_reference_event_id`` as an explicit audited resolution.
    """
    if relation not in (COMPARISON, PROCESS, IDENTITY):
        raise ValueError("UNKNOWN_FINAL_GRAPH_RELATION")
    audit = event.get("final_graph_audit", {})
    if audit.get("version") == AUDIT_VERSION:
        for record in audit["dependencies"]:
            if record["relation"] == relation and record["source_event_id"] == target_id:
                return deepcopy(record)
    values = list(events.values()) if isinstance(events, dict) else list(events)
    audited, _ = audit_final_graph(values)
    owner = next((item for item in audited if item["event_id"] == event["event_id"]), None)
    if owner:
        for record in owner["final_graph_audit"]["dependencies"]:
            if record["relation"] == relation and record["source_event_id"] == target_id:
                return deepcopy(record)
    byid = {item["event_id"]: item for item in values}
    fallback = _base_guard(event, target_id, relation, byid)
    fallback["allowed"] = False
    fallback["hard_reasons"] = sorted(set(fallback["hard_reasons"] + ["RELATION_NOT_DECLARED_BY_MODEL"]))
    return fallback

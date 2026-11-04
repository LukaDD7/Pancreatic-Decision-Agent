"""Bounded, source-linked pipeline; incomplete requests are counted, not dropped."""
from collections import Counter

from .core import (FIELDS, NORMALIZATION_SCHEMA, RELATION_SCHEMA, SourceStore,
                   assess_relation, candidates, clock, digest, event_hypotheses,
                   normalize, validate_graph)
from .model import ModelFailure, public_view, mention_payload


def provisional_mentions(units):
    """Local preparation only: wrappers with unknown semantics, never inferred events."""
    out = []
    for unit in units:
        kinds = []
        if unit["role"] == "DECISION_CANDIDATE":
            kinds.append("DecisionMention")
        if unit["execution_cues"]:
            kinds.append("ActionMention")
        if unit["feedback_cues"] and unit["role"] != "INSTRUCTION_MENTION":
            kinds.append("FeedbackMention")
        for kind in kinds:
            out.append({"id": digest(["lexical_wrapper", unit["id"], kind])[:24],
                "unit_id": unit["id"], "kind": kind, "patient_uid": unit["patient_uid"],
                "visit_id": unit.get("visit_id"),
                "anchor": {k: unit[k] for k in ["patient_uid", "source_id", "source_sha256", "span", "quote"]},
                "signature": {f: {"state": "uncertain", "value": None, "supports": []} for f in FIELDS},
                "temporality": "uncertain", "temporality_supports": [],
                "ordering_time": unit["ordering_time"], "time_basis": unit["time_basis"],
                "topics": unit["topic_candidates"], "status": "LEXICAL_WRAPPER_NOT_NORMALIZED",
                "confidence": None, "expert_reference": None})
    return out


def context_views(store, a, b, *, event_members=None, max_chars=18000):
    """Endpoints plus all intervening source documents if budget permits.

    Even zero-hit documents are included. If truncated, no silent assumption of
    'no new evidence' is possible: coverage is explicitly marked incomplete.
    Future-dated source documents are not admitted. Full endpoint documents are
    researcher context, not pre-decision replay or blinded clinical input.
    """
    patient = a["patient_uid"]
    if patient != b["patient_uid"]:
        raise ValueError("cross-patient context")
    at, bt = clock(a["ordering_time"]), clock(b["ordering_time"])
    members = event_members or [a, b]
    if any(m["patient_uid"] != patient for m in members):
        raise ValueError("cross-patient event context")
    endpoints = list(dict.fromkeys(m["anchor"]["source_id"] for m in members))
    known = [clock(m["ordering_time"]) for m in members if clock(m["ordering_time"])]
    if known:
        at = min(known)
    others, unknown = [], 0
    for (p, sid), note in store.notes.items():
        if p != patient or sid in endpoints:
            continue
        raw = note.get("body_time") or note.get("system_time")
        # Source corpus has hospital-local naive clocks; use the same parser as
        # frozen extraction, preserving date-only uncertainty.
        from scripts.cohort_construction.p2_fhir_replay.core import instant
        t = instant(raw)
        if t is None:
            unknown += 1
        elif at and bt and at <= t <= bt:
            others.append((t, sid))
    others.sort()
    views, used, omitted = [], 0, []
    for sid in endpoints + [sid for _, sid in others]:
        note = store.notes[patient, sid]
        if used + len(note["text"]) > max_chars:
            if sid in endpoints:
                anchors = [m["anchor"] for m in members if m["anchor"]["source_id"] == sid]
                lo = max(0, min(a["span"][0] for a in anchors)-500)
                hi = min(len(note["text"]), max(a["span"][1] for a in anchors)+500)
                views.append(store.view(patient, sid, lo, hi, alias=f"S{len(views)}"))
                used += hi-lo
            omitted.append(sid)
        else:
            views.append(store.view(patient, sid, alias=f"S{len(views)}"))
            used += len(note["text"])
    return views, {"complete_intervening_proxy_window": not omitted and at is not None and bt is not None and at < bt and unknown == 0,
                   "omitted_source_count": len(omitted), "unknown_time_source_count": unknown,
                   "source_count": len(views), "scope": "proxy-window researcher context; not certified historical visibility"}


def failed_edge(candidate, code):
    return {**candidate, "relation": "uncertain", "proposed_relation": "uncertain",
            "supports": [], "issues": [code], "status": "REVIEW", "score": 0.0,
            "score_kind": "UNCALIBRATED_SUPPORT_FEATURE", "confidence": None}


def normalization_review_flags(unit, mentions):
    """Flag coverage questions, not source defects or confirmed model mistakes."""
    roles = Counter(m["kind"] for m in mentions)
    flags = []
    if roles["DecisionMention"] > 1:
        flags.append("MULTIPLE_JUDGMENTS_ATOMICITY_REVIEW")
    if unit["feedback_cues"] and not roles["FeedbackMention"] and unit["role"] != "INSTRUCTION_MENTION":
        flags.append("FEEDBACK_CUE_WITHOUT_TYPED_MENTION_REVIEW")
    if unit["execution_cues"] and not roles["ActionMention"]:
        flags.append("EXECUTION_CUE_WITHOUT_TYPED_MENTION_REVIEW")
    return flags


def reconstruct(units, notes, *, client=None, top_k=4, max_pairs=None):
    store = SourceStore(notes)
    for unit in units:
        store.verify_unit(unit)
    mentions, ledger, edges, pair_ledger = [], [], [], []
    omissions = {}
    if client is None:
        mentions = provisional_mentions(units)
        ledger = [{"unit_id": u["id"], "status": "PENDING_SEMANTIC_NORMALIZATION"} for u in units]
    else:
        for unit in units:
            note = store.verify_unit(unit)
            lo, hi = max(0, unit["span"][0]-800), min(len(note["text"]), unit["span"][1]+800)
            views = [store.view(unit["patient_uid"], unit["source_id"], lo, hi)]
            try:
                answer = client.ask("normalize_mentions", {
                    "unit_span": unit["span"], "unit_quote": unit["quote"],
                    "sources": [public_view(v) for v in views],
                    "instruction": "仅拆分unit_span内有原文依据的mention；周围文字只作语境。没有适合角色可返回空列表。"}, NORMALIZATION_SCHEMA)
                found = normalize(unit, answer, views)
                mentions.extend(found)
                ledger.append({"unit_id": unit["id"], "status": "NORMALIZED" if found else "MODEL_NO_MENTION_REVIEW",
                               "mention_ids": [m["id"] for m in found],
                               "review_flags": normalization_review_flags(unit, found)})
            except Exception as exc:
                # Keep source units even when transport/schema/grounding fails.
                ledger.append({"unit_id": unit["id"], "status": "NORMALIZATION_FAILED_REVIEW",
                               "error_type": type(exc).__name__,
                               "error_code": str(exc) if isinstance(exc, ModelFailure) else "SCHEMA_OR_SOURCE_VALIDATION_FAILED"})
    byid = {m["id"]: m for m in mentions}
    calls = 0

    member_groups = {}

    def classify(batch):
        nonlocal calls
        for pair in batch:
            a, b = byid[pair["from"]], byid[pair["to"]]
            previous_members = member_groups.get(a["id"], [a])
            current_members = member_groups.get(b["id"], [b])
            views, coverage = context_views(store, a, b, event_members=previous_members+current_members)
            pair_ledger.append({**pair, "context_coverage": coverage})
            if client is None or (max_pairs is not None and calls >= max_pairs):
                edges.append(failed_edge(pair, "NOT_RUN" if client is None else "PAIR_BUDGET_NOT_RUN"))
                continue
            calls += 1
            try:
                answer = client.ask(pair["stage"], {
                    "previous": mention_payload(a), "current": mention_payload(b),
                    "previous_event_mentions": [mention_payload(m) for m in previous_members],
                    "current_event_mentions": [mention_payload(m) for m in current_members],
                    "sources": [public_view(v) for v in views], "context_coverage": coverage,
                    "instruction": {
                        "coreference": "只判same_event/different_event/unrelated/uncertain；不以缺少新证据证明same_event。",
                        "decision_relation": "只判reaffirm/revise/supersede/unrelated/uncertain；独立事件未证实则uncertain。",
                        "typed_relation": "仅按节点类型判informs/implemented_by/evaluated_by/triggers_reassessment，或unrelated/uncertain。"
                    }[pair["stage"]]}, RELATION_SCHEMA)
                edge = assess_relation(pair, answer, byid, views)
                edge["context_coverage"] = coverage
                edges.append(edge)
            except Exception as exc:
                code = str(exc) if isinstance(exc, ModelFailure) else type(exc).__name__
                edges.append(failed_edge(pair, "MODEL_OR_GROUNDING_FAILURE:" + code))

    batch, omissions["coreference"] = candidates(mentions, stage="coreference", top_k=top_k)
    classify(batch)
    events, membership, _ = event_hypotheses(mentions, edges)
    # Event-to-event shortlist uses a representative for ranking; the classifier
    # sees every member and its source context, not only that representative.
    representatives = []
    for event in events:
        rep = max((byid[mid] for mid in event["mentions"]), key=lambda m: (m["ordering_time"] or "", m["id"]))
        representatives.append(rep)
        member_groups[rep["id"]] = [byid[mid] for mid in event["mentions"]]
    batch, omissions["decision_relation"] = candidates(representatives, stage="decision_relation", top_k=top_k)
    classify(batch)
    batch, omissions["typed_relation"] = candidates(mentions, stage="typed_relation", top_k=top_k)
    classify(batch)
    graph = validate_graph(mentions, edges)
    if graph["membership"] != membership:
        graph["issues"].append({"code": "RELATION_REGENERATION_REQUIRED_AFTER_COREFERENCE_CONFLICT"})
    graph.update({"mentions": mentions, "normalization_ledger": ledger,
                  "candidate_ledger": pair_ledger, "candidate_omissions": omissions,
                  "source_document_count": len(notes), "input_unit_count": len(units),
                  "model_requests": [] if client is None else client.receipts,
                  "mode": "PREPARE_ONLY" if client is None else "SEMANTIC_PROPOSALS"})
    graph["summary"] = {"input_units": len(units), "source_documents": len(notes),
        "mentions": len(mentions), "mention_roles": dict(Counter(m["kind"] for m in mentions)),
        "event_hypotheses": len(graph["events"]), "relation_candidates": len(edges),
        "auxiliary_event_hypotheses": len(graph["auxiliary_events"]),
        "relations": dict(Counter(e["relation"] for e in graph["edges"])),
        "normalization_statuses": dict(Counter(e["status"] for e in ledger)),
        "graph_conflicts": len(graph["issues"]), "auto_accepted": 0,
        "clinical_reference_generated": False, "clinical_validation": False}
    return graph

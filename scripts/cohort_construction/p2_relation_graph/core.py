"""Source-grounded mention, candidate, event hypothesis and graph contracts.

No missing text is interpreted as negative evidence; no model proposal is a
confirmed decision, episode, acceptable-action reference or calibrated score.
"""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json

from jsonschema import Draft202012Validator


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def clock(value):
    if not value or len(value) <= 10:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


KINDS = ["DecisionMention", "EvidenceMention", "ActionMention", "FeedbackMention"]
FIELDS = ["management_question", "clinical_target", "assessment", "intended_action",
          "condition", "goal", "commitment"]
REF = {"type": "object", "required": ["source", "quote"], "additionalProperties": False,
       "properties": {"source": {"type": "string"}, "quote": {"type": "string", "minLength": 1}}}
FIELD = {"type": "object", "required": ["state", "value", "supports"], "additionalProperties": False,
         "properties": {"state": {"enum": ["stated", "not_mentioned", "uncertain"]},
                        "value": {"type": ["string", "null"]},
                        "supports": {"type": "array", "items": REF}}}
NORMALIZATION_SCHEMA = {
    "type": "object", "required": ["mentions"], "additionalProperties": False,
    "properties": {"mentions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["kind", "anchor", "signature", "temporality", "temporality_supports"],
        "properties": {"kind": {"enum": KINDS}, "anchor": REF,
            "signature": {"type": "object", "required": FIELDS, "additionalProperties": False,
                          "properties": {f: FIELD for f in FIELDS}},
            "temporality": {"enum": ["current", "historical", "future_plan", "conditional", "uncertain"]},
            "temporality_supports": {"type": "array", "items": REF}}}}}}

DD_RELATIONS = {"reaffirm", "revise", "supersede"}
TYPED = {"informs": ("EvidenceMention", "DecisionMention"),
         "implemented_by": ("DecisionMention", "ActionMention"),
         "evaluated_by": ("ActionMention", "FeedbackMention"),
         "triggers_reassessment": ("FeedbackMention", "DecisionMention")}
RELATIONS = sorted(DD_RELATIONS | set(TYPED) | {"same_event", "different_event", "unrelated", "uncertain"})
SUPPORT_ROLES = ["same_event_reference", "reassessment", "new_evidence", "feedback",
                 "changed_assessment", "changed_action", "changed_condition", "replacement",
                 "implementation", "evaluation", "informs", "different_question"]
RELATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["relation", "same_management_question", "same_decision_event",
                 "new_decision_relevant_evidence", "current_action_relation", "supports"],
    "properties": {"relation": {"enum": RELATIONS},
        **{f: {"enum": ["yes", "no", "uncertain"]} for f in
           ["same_management_question", "same_decision_event", "new_decision_relevant_evidence"]},
        "current_action_relation": {"enum": ["same", "changed", "unclear"]},
        "supports": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["purpose", "span"], "properties": {
                "purpose": {"enum": SUPPORT_ROLES}, "span": REF}}}}}


class SourceStore:
    def __init__(self, notes):
        self.notes = {}
        for note in notes:
            key = (note["patient_uid"], note["record_key"])
            if key in self.notes:
                raise ValueError("duplicate source identity")
            self.notes[key] = note

    def verify_unit(self, unit):
        note = self.notes[(unit["patient_uid"], unit["source_id"])]
        text = note["text"]
        if (hashlib.sha256(text.encode()).hexdigest() != unit["source_sha256"]
                or text[slice(*unit["span"])] != unit["quote"]):
            raise ValueError("source hash / unit span mismatch")
        return note

    def view(self, patient, source_id, start=0, end=None, alias="S0"):
        note = self.notes[patient, source_id]
        text = note["text"]
        end = len(text) if end is None else end
        if not (0 <= start < end <= len(text)):
            raise ValueError("invalid source window")
        return {"alias": alias, "patient_uid": patient, "source_id": source_id,
                "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "start": start, "end": end, "text": text[start:end],
                "document_type": note.get("document_type"),
                "body_time": note.get("body_time"), "recorded_time": note.get("system_time")}


def resolve(ref, views):
    """Resolve exact quotes, refusing ambiguous matches instead of guessing."""
    view = next((v for v in views if v["alias"] == ref["source"]), None)
    if view is None:
        raise ValueError("support references a source not supplied to this request")
    quote, text = ref["quote"], view["text"]
    if ("start" in ref) != ("end" in ref):
        raise ValueError("both source coordinates are required together")
    if "start" in ref and "end" in ref:
        start, end = ref["start"], ref["end"]
        if not (view["start"] <= start < end <= view["end"]) or text[start-view["start"]:end-view["start"]] != quote:
            raise ValueError("support span does not equal source")
    else:
        positions = [i for i in range(len(text)) if text.startswith(quote, i)]
        if len(positions) != 1:
            raise ValueError("support quote absent or ambiguous")
        start = view["start"] + positions[0]
        end = start + len(quote)
    return {"patient_uid": view["patient_uid"], "source_id": view["source_id"],
            "source_sha256": view["source_sha256"], "span": [start, end], "quote": quote}


def normalize(unit, response, views):
    Draft202012Validator(NORMALIZATION_SCHEMA).validate(response)
    result = []
    for item in response["mentions"]:
        anchor = resolve(item["anchor"], views)
        # Normalization may split a retrieved unit; it cannot quietly expand
        # decision retrieval to the entire surrounding document.
        if anchor["source_id"] != unit["source_id"] or anchor["patient_uid"] != unit["patient_uid"]:
            raise ValueError("mention outside its source unit")
        if not (unit["span"][0] <= anchor["span"][0] < anchor["span"][1] <= unit["span"][1]):
            raise ValueError("mention outside its source unit")
        signature = {}
        for name, field in item["signature"].items():
            supports = [resolve(r, views) for r in field["supports"]]
            if field["state"] == "stated" and (not supports or not field["value"]):
                raise ValueError("stated field requires value and source support")
            if field["state"] == "not_mentioned" and (field["value"] is not None or supports):
                raise ValueError("not mentioned must not become a fact")
            signature[name] = {**field, "supports": supports}
        temporal_supports = [resolve(r, views) for r in item["temporality_supports"]]
        if item["temporality"] != "uncertain" and not temporal_supports:
            raise ValueError("temporality requires source support")
        mid = digest([unit["id"], item["kind"], anchor["span"], signature])[:24]
        result.append({"id": mid, "unit_id": unit["id"], "kind": item["kind"],
            "patient_uid": unit["patient_uid"], "visit_id": unit.get("visit_id"),
            "anchor": anchor, "signature": signature, "temporality": item["temporality"],
            "temporality_supports": temporal_supports, "ordering_time": unit["ordering_time"],
            "time_basis": unit["time_basis"], "topics": unit["topic_candidates"],
            "status": "MODEL_PROPOSED", "confidence": None, "expert_reference": None})
    if len({m["id"] for m in result}) != len(result):
        raise ValueError("duplicate normalized mentions")
    return result


def value(mention, field):
    f = mention["signature"][field]
    return f["value"] if f["state"] == "stated" else None


def overlap(a, b):
    # A ranking aid, not an embedding, semantic identity or a clinical threshold.
    aa, bb = set(a or ""), set(b or "")
    return len(aa & bb) / len(aa | bb) if aa and bb else 0.0


def candidates(mentions, *, stage, top_k=4):
    """Bounded prior candidates. Unknown questions stay eligible, not rejected.

    Same/unknown proxy times are allowed only for coreference review. Directed
    temporal relations require strictly earlier proxy times; this is not proof
    of event time or historical availability. All omissions get separate counts.
    """
    if top_k < 1:
        raise ValueError("top_k must be positive")
    by_patient = defaultdict(list)
    for m in mentions:
        by_patient[m["patient_uid"]].append(m)
    output, omitted = [], defaultdict(int)
    for patient, members in sorted(by_patient.items()):
        members.sort(key=lambda m: (m["ordering_time"] or "", m["id"]))
        for index, current in enumerate(members):
            if stage in {"coreference", "decision_relation"} and current["kind"] != "DecisionMention":
                continue
            ranked = []
            for previous in members[:index]:
                types = (previous["kind"], current["kind"])
                if stage in {"coreference", "decision_relation"} and types != ("DecisionMention", "DecisionMention"):
                    continue
                if stage == "typed_relation" and types not in TYPED.values():
                    continue
                pt, ct = clock(previous["ordering_time"]), clock(current["ordering_time"])
                if stage != "coreference" and (pt is None or ct is None or pt >= ct):
                    omitted["unordered_temporal_pairs"] += 1
                    continue
                pq, cq = value(previous, "management_question"), value(current, "management_question")
                shared = set(previous["topics"]) & set(current["topics"])
                similarity = overlap(pq, cq)
                # No rejection solely because either signature is unknown.
                if pq and cq and not shared and similarity < 0.15:
                    omitted["question_topic_filter"] += 1
                    continue
                rank = (int(pq is not None and pq == cq), similarity, len(shared),
                        previous["ordering_time"] or "", previous["id"])
                ranked.append((rank, previous))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            omitted["top_k"] += max(0, len(ranked)-top_k)
            for _, previous in ranked[:top_k]:
                output.append({"id": digest([stage, previous["id"], current["id"]])[:24],
                    "from": previous["id"], "to": current["id"], "stage": stage,
                    "patient_uid": patient, "cross_visit": previous.get("visit_id") != current.get("visit_id"),
                    "basis": "question lexical similarity/topic + proxy time; not a confirmed relation"})
    return output, dict(omitted)


def assess_relation(candidate, response, mentions, views):
    Draft202012Validator(RELATION_SCHEMA).validate(response)
    a, b = mentions[candidate["from"]], mentions[candidate["to"]]
    if a["patient_uid"] != b["patient_uid"]:
        raise ValueError("cross-patient relation")
    supports = [{"purpose": r["purpose"], "span": resolve(r["span"], views)} for r in response["supports"]]
    if any(s["span"]["patient_uid"] != a["patient_uid"] for s in supports):
        raise ValueError("cross-patient support")
    proposed, issues = response["relation"], []
    purposes = {s["purpose"] for s in supports}
    types = (a["kind"], b["kind"])
    if proposed != "uncertain" and not supports:
        issues.append("NO_RELATION_SUPPORT")
    stage_allowed = {"coreference": {"same_event", "different_event", "unrelated", "uncertain"},
                     "decision_relation": DD_RELATIONS | {"unrelated", "uncertain"},
                     "typed_relation": set(TYPED) | {"unrelated", "uncertain"}}
    if proposed not in stage_allowed[candidate["stage"]]:
        issues.append("WRONG_RELATION_STAGE")
    if proposed in DD_RELATIONS | {"same_event", "different_event"} and types != ("DecisionMention", "DecisionMention"):
        issues.append("WRONG_NODE_TYPES")
    if proposed in TYPED and TYPED[proposed] != types:
        issues.append("WRONG_NODE_TYPES")
    if proposed in DD_RELATIONS | {"same_event"} and response["same_management_question"] != "yes":
        issues.append("QUESTION_NOT_ESTABLISHED")
    if proposed == "different_event" and response["same_decision_event"] != "no":
        issues.append("DISTINCT_EVENTS_NOT_ESTABLISHED")
    if proposed == "unrelated" and (response["same_management_question"] != "no" or "different_question" not in purposes):
        issues.append("UNRELATED_NOT_ESTABLISHED")
    if proposed == "same_event":
        if response["same_decision_event"] != "yes" or "same_event_reference" not in purposes:
            issues.append("COREFERENCE_NEEDS_EXPLICIT_EVENT_REFERENCE")
        if purposes & {"reassessment", "replacement"} or response["current_action_relation"] == "changed":
            issues.append("COREFERENCE_CONFLICT")
    if proposed in DD_RELATIONS and response["same_decision_event"] != "no":
        issues.append("DISTINCT_EVENTS_NOT_ESTABLISHED")
    if proposed == "reaffirm" and (response["current_action_relation"] != "same" or
                                    not purposes & {"reassessment", "new_evidence"}):
        issues.append("REAFFIRM_WITHOUT_POSITIVE_REASSESSMENT_EVIDENCE")
    if proposed in {"revise", "supersede"} and not purposes & {"changed_action", "changed_assessment", "changed_condition"}:
        issues.append("REVISION_WITHOUT_CHANGE_SUPPORT")
    if proposed == "supersede" and "replacement" not in purposes:
        issues.append("REPLACEMENT_NOT_ESTABLISHED")
    needed = {"informs": {"informs"}, "implemented_by": {"implementation"},
              "evaluated_by": {"evaluation"}, "triggers_reassessment": {"feedback", "reassessment"}}
    if proposed in needed and not needed[proposed] <= purposes:
        issues.append("TYPED_RELATION_SUPPORT_MISSING")
    if proposed == "implemented_by":
        av, bv = value(a, "intended_action"), value(b, "intended_action")
        if av is None or bv is None or av != bv:
            issues.append("ACTION_COMPATIBILITY_NOT_ESTABLISHED")
        if b["temporality"] in {"future_plan", "conditional", "uncertain"}:
            issues.append("EXECUTION_NOT_ESTABLISHED")
    if proposed in DD_RELATIONS | set(TYPED):
        at, bt = clock(a["ordering_time"]), clock(b["ordering_time"])
        if at is None or bt is None or at >= bt:
            issues.append("TEMPORAL_ORDER_UNRESOLVED")
        if a["temporality"] == "historical" or b["temporality"] == "historical":
            issues.append("HISTORICAL_EVENT_TIME_REVIEW")
    # This score describes evidence features only. It is NOT model probability.
    score = min(1.0, 0.25 * len(purposes)) if supports and not issues and proposed != "uncertain" else 0.0
    return {**candidate, **response, "supports": supports, "proposed_relation": proposed,
            "relation": "uncertain" if issues else proposed,
            "issues": issues, "status": "REVIEW", "score": score,
            "score_kind": "UNCALIBRATED_SUPPORT_FEATURE", "confidence": None}


def event_hypotheses(mentions, edges):
    """Build explicitly provisional coreference components without data deletion.

    A distinct-event edge anywhere inside a component blocks the entire merge;
    inferred transitive membership is exposed as such, never counted as gold.
    """
    decision = {m["id"]: m for m in mentions if m["kind"] == "DecisionMention"}
    parents = {i: i for i in decision}
    def root(i):
        while parents[i] != i:
            i = parents[i]
        return i
    for edge in edges:
        a, b = edge["from"], edge["to"]
        if edge["relation"] == "same_event" and a in decision and b in decision:
            if decision[a]["patient_uid"] != decision[b]["patient_uid"]:
                raise ValueError("cross-patient coreference")
            parents[root(a)] = root(b)
    groups = defaultdict(list)
    for mid in decision:
        groups[root(mid)].append(mid)
    conflicts = set()
    for edge in edges:
        if edge["from"] in decision and edge["to"] in decision and edge["relation"] in DD_RELATIONS | {"different_event", "unrelated"}:
            if root(edge["from"]) == root(edge["to"]):
                conflicts.add(root(edge["from"]))
    events, membership, issues = [], {}, []
    for group, mids in sorted(groups.items()):
        if group in conflicts:
            issues.append({"code": "COREFERENCE_TRANSITIVITY_CONFLICT", "mentions": sorted(mids)})
        for members in ([[m] for m in sorted(mids)] if group in conflicts else [sorted(mids)]):
            event_id = digest(["DecisionEventHypothesis", members])[:24]
            events.append({"id": event_id, "kind": "DecisionEventHypothesis", "mentions": members,
                           "patient_uid": decision[members[0]]["patient_uid"],
                           "status": "REVIEW", "confirmed": False, "expert_reference": None})
            membership.update({mid: event_id for mid in members})
    return events, membership, issues


def validate_graph(mentions, edges):
    """Validate global consistency; demote contradictory relations, never invent replacements."""
    byid = {m["id"]: m for m in mentions}
    if len(byid) != len(mentions):
        raise ValueError("duplicate mention identities")
    for e in edges:
        if e["from"] not in byid or e["to"] not in byid:
            raise ValueError("dangling graph endpoint")
        if byid[e["from"]]["patient_uid"] != byid[e["to"]]["patient_uid"]:
            raise ValueError("cross-patient graph edge")
    events, membership, issues = event_hypotheses(mentions, edges)
    auxiliary = [{"id": digest(["auxiliary_event", m["id"]])[:24],
                  "kind": m["kind"].replace("Mention", "EventHypothesis"),
                  "mentions": [m["id"]], "patient_uid": m["patient_uid"],
                  "status": "REVIEW", "confirmed": False, "expert_reference": None}
                 for m in mentions if m["kind"] != "DecisionMention"]
    endpoint_map = {**membership, **{e["mentions"][0]: e["id"] for e in auxiliary}}
    conflict_mentions = {m for issue in issues for m in issue["mentions"]}
    final_edges = []
    for edge in edges:
        e = {**edge, "issues": list(edge["issues"])}
        if e["relation"] == "same_event" and {e["from"], e["to"]} & conflict_mentions:
            e["issues"].append("COREFERENCE_TRANSITIVITY_CONFLICT")
            e["relation"], e["score"] = "uncertain", 0.0
        e["from_event"] = endpoint_map[e["from"]]
        e["to_event"] = endpoint_map[e["to"]]
        if e["relation"] in DD_RELATIONS and e["from_event"] == e["to_event"]:
            e["issues"].append("RELATION_WITHIN_SAME_EVENT")
            e["relation"], e["score"] = "uncertain", 0.0
        final_edges.append(e)
    directed = [e for e in final_edges if e["relation"] in DD_RELATIONS | set(TYPED)]
    adjacency = defaultdict(set)
    for e in directed:
        adjacency[e["from_event"]].add(e["to_event"])
    def reachable(start, target):
        todo, seen = [start], set()
        while todo:
            node = todo.pop()
            if node == target:
                return True
            if node not in seen:
                seen.add(node)
                todo.extend(adjacency[node] - seen)
        return False
    for e in directed:
        if reachable(e["to_event"], e["from_event"]):
            issues.append({"code": "EVENT_TEMPORAL_CYCLE", "edge": e["id"]})
            e["issues"].append("EVENT_TEMPORAL_CYCLE")
            e["relation"], e["score"] = "uncertain", 0.0
    return {"events": events, "auxiliary_events": auxiliary, "membership": membership,
            "event_endpoint_map": endpoint_map, "edges": final_edges, "issues": issues,
            "confirmed_decision_count": None, "episode_count": None,
            "auto_accept_enabled": False, "calibration_status": "NO_HUMAN_GOLD"}

"""Event-aligned validation v0.3: explicit scope, FP accounting, no calibration."""
import argparse
from collections import Counter, defaultdict
from itertools import combinations
import json
from pathlib import Path

DD = {"reaffirm", "revise", "supersede"}
PROCESS = {"informs", "implemented_by", "evaluated_by", "triggers_reassessment"}
POSITIVE = DD | PROCESS


def rates(tp, predicted, gold):
    return {"true_positive": tp, "false_positive": predicted-tp, "false_negative": gold-tp,
            "predicted": predicted, "gold": gold, "precision": tp/predicted if predicted else None,
            "recall": tp/gold if gold else None, "f1": 2*tp/(predicted+gold) if predicted+gold else None}


def coref_pairs(mapping):
    return {frozenset([a, b]) for a, b in combinations(sorted(mapping), 2) if mapping[a] == mapping[b]}


def maximum_assignment(weights):
    """Deterministic Hungarian maximum-weight assignment; no scipy dependency."""
    if not weights or not weights[0]: return []
    nr, nc = len(weights), len(weights[0]); n = max(nr, nc)
    cost = [[-(weights[i][j] if i < nr and j < nc else 0.0) for j in range(n)] for i in range(n)]
    u, v, p, way = [0.0]*(n+1), [0.0]*(n+1), [0]*(n+1), [0]*(n+1)
    for i in range(1, n+1):
        p[0] = i; j0 = 0; mins = [float("inf") ]*(n+1); used = [False]*(n+1)
        while True:
            used[j0] = True; i0 = p[j0]; delta = float("inf"); j1 = 0
            for j in range(1, n+1):
                if not used[j]:
                    cur = cost[i0-1][j-1]-u[i0]-v[j]
                    if cur < mins[j]: mins[j], way[j] = cur, j0
                    if mins[j] < delta: delta, j1 = mins[j], j
            for j in range(n+1):
                if used[j]: u[p[j]] += delta; v[j] -= delta
                else: mins[j] -= delta
            j0 = j1
            if p[j0] == 0: break
        while True:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
            if j0 == 0: break
    return sorted((p[j]-1, j-1) for j in range(1, n+1) if 0 < p[j] <= nr and j <= nc)


def _identity(m):
    a = m.get("anchor", {})
    return (a.get("patient_uid", m.get("patient_uid")), a.get("source_id"),
            a.get("source_sha256"), tuple(a.get("span", [])), m["kind"])


def _in_scope(m, scope):
    if scope["kind"] == "aligned_mentions": return m["id"] in scope["mention_ids"]
    a = m.get("anchor", {})
    if not all(k in a for k in ["patient_uid", "source_id", "source_sha256", "span"]):
        raise ValueError("source-window evaluation needs source-grounded anchors")
    lo, hi = a["span"]
    return any(a["patient_uid"] == w["patient_uid"] and a["source_id"] == w["source_id"]
               and a["source_sha256"] == w["source_sha256"]
               and w["start"] <= lo < hi <= w["end"] for w in scope["windows"])


def _validate_gold(gold):
    if gold.get("annotation_status") != "ADJUDICATED" or not gold.get("complete_within_declared_scope"):
        raise ValueError("complete adjudicated reconstruction gold required")
    if gold.get("origin") not in {"independent_human", "synthetic_fixture"} or not gold.get("annotator_ids"):
        raise ValueError("gold annotation provenance required")
    if gold['origin'] == 'independent_human' and (len(set(gold['annotator_ids'])) < 2 or not gold.get('adjudicator_id')):
        raise ValueError('real gold needs independent annotators and adjudicator provenance')
    scope = gold.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") not in {"source_windows", "aligned_mentions"}:
        raise ValueError("machine-readable evaluation scope required")
    if scope["kind"] == "aligned_mentions" and gold["origin"] != "synthetic_fixture":
        raise ValueError("real gold requires independent source-window scope")
    if scope["kind"] == "source_windows":
        if not scope.get("windows"): raise ValueError("source windows required")
        for w in scope["windows"]:
            if not all(k in w for k in ["patient_uid", "source_id", "source_sha256", "start", "end"]) or not 0 <= w["start"] < w["end"]:
                raise ValueError("invalid evaluation window")
    elif not scope.get("mention_ids"): raise ValueError("fixture scope cannot be empty")
    mentions = {m["id"]: m for m in gold["mentions"]}
    if len(mentions) != len(gold["mentions"]) or set(mentions) != set(gold["mention_to_event"]):
        raise ValueError("gold mentions and event membership must be complete and unique")
    if not all(_in_scope(m, scope) for m in mentions.values()): raise ValueError("gold mention outside scope")
    if any(m['kind'] not in {'DecisionMention','EvidenceMention','ActionMention','FeedbackMention'} for m in mentions.values()):
        raise ValueError("unknown gold mention type")
    return mentions, scope


def _mention_alignment(pred, truth, gold):
    if gold["scope"]["kind"] == "aligned_mentions":
        mapping = {i: i for i in pred.keys() & truth.keys() if pred[i]["kind"] == truth[i]["kind"]}
    else:
        index = defaultdict(list)
        for mid, m in truth.items(): index[_identity(m)].append(mid)
        mapping = {}
        for mid, m in sorted(pred.items()):
            ids = index[_identity(m)]
            if len(ids) == 1 and ids[0] not in mapping.values(): mapping[mid] = ids[0]
    manual = gold.get("manual_mention_alignment", [])
    if manual and not gold.get("alignment_adjudicator_id"): raise ValueError("manual alignment needs provenance")
    for entry in manual:
        p, g = entry["predicted"], entry["gold"]
        if p not in pred or g not in truth or pred[p]["kind"] != truth[g]["kind"]:
            raise ValueError("invalid manual mention alignment")
        if gold["scope"]["kind"] == "source_windows":
            pa, ga = pred[p]["anchor"], truth[g]["anchor"]
            if any(pa[k] != ga[k] for k in ["patient_uid", "source_id", "source_sha256"]) or not (pa["span"][0] < ga["span"][1] and ga["span"][0] < pa["span"][1]):
                raise ValueError("manual alignment must overlap in same patient/source/version")
        if (p in mapping and mapping[p] != g) or (g in mapping.values() and mapping.get(p) != g):
            raise ValueError("mention alignment must be one-to-one")
        mapping[p] = g
    return mapping


def _clusters(mentions, membership, tokens):
    groups, meta = defaultdict(set), {}
    for mid, m in mentions.items():
        eid = membership.get(mid, "singleton:"+mid)
        patient = m.get("patient_uid", m.get("anchor", {}).get("patient_uid", "fixture"))
        pair = (patient, m["kind"])
        if eid in meta and meta[eid] != pair: raise ValueError("event cluster mixes patients or types")
        meta[eid] = pair; groups[eid].add(tokens[mid])
    return groups, meta


def _b3(pred_mapping, gold_mapping):
    common = sorted(pred_mapping.keys() & gold_mapping.keys())
    if not common: return {"precision": None, "recall": None, "f1": None, "common_mentions": 0}
    ps, gs = defaultdict(set), defaultdict(set)
    for m in common: ps[pred_mapping[m]].add(m); gs[gold_mapping[m]].add(m)
    p = sum(len(ps[pred_mapping[m]] & gs[gold_mapping[m]])/len(ps[pred_mapping[m]]) for m in common)/len(common)
    r = sum(len(ps[pred_mapping[m]] & gs[gold_mapping[m]])/len(gs[gold_mapping[m]]) for m in common)/len(common)
    return {"precision": p, "recall": r, "f1": 2*p*r/(p+r) if p+r else 0.0,
            "common_mentions": len(common), "scope": "common aligned mentions only; detection reported separately"}


def evaluate(graph, gold):
    truth, scope = _validate_gold(gold)
    all_pred = {m["id"]: m for m in graph["mentions"]}
    if len(all_pred) != len(graph["mentions"]): raise ValueError("duplicate prediction mention identity")
    pred = {i: m for i, m in all_pred.items() if _in_scope(m, scope)}
    if any(m['kind'] not in {'DecisionMention','EvidenceMention','ActionMention','FeedbackMention'} for m in pred.values()):
        raise ValueError("unknown prediction mention type")
    alignment = _mention_alignment(pred, truth, gold)
    p_tokens = {m: "G:"+alignment[m] if m in alignment else "P:"+m for m in pred}
    g_tokens = {m: "G:"+m for m in truth}
    p_member = {**graph.get("event_endpoint_map", {}), **graph.get("membership", {})}
    pc, pm = _clusters(pred, p_member, p_tokens)
    gc, gm = _clusters(truth, gold["mention_to_event"], g_tokens)
    pids, gids = sorted(pc), sorted(gc)
    weights = [[len(pc[p] & gc[g])/len(pc[p] | gc[g]) if pm[p] == gm[g] else 0.0 for g in gids] for p in pids]
    event_match = {pids[i]: gids[j] for i, j in maximum_assignment(weights) if weights[i][j] > 0}
    p_canonical = {m: "G:"+event_match[e] if e in event_match else "P:"+e
                   for m in pred for e in [p_member.get(m, "singleton:"+m)]}
    g_canonical = {m: "G:"+gold["mention_to_event"][m] for m in truth}

    def edge_key(e, mapping):
        if e["from"] not in mapping or e["to"] not in mapping: return None
        return e["stage"], mapping[e["from"]], mapping[e["to"]]

    gold_rows, gold_edges, unresolved = {}, set(), set()
    for e in gold["relations"]:
        if e["stage"] == "coreference": continue
        k = edge_key(e, g_canonical)
        if k is None: raise ValueError("gold relation has missing endpoint")
        label = e["relation"]
        if label not in POSITIVE | {"unrelated", "uncertain"}: raise ValueError("invalid gold relation")
        kinds = (truth[e['from']]['kind'], truth[e['to']]['kind'])
        allowed = {'informs':('EvidenceMention','DecisionMention'), 'implemented_by':('DecisionMention','ActionMention'),
                   'evaluated_by':('ActionMention','FeedbackMention'), 'triggers_reassessment':('FeedbackMention','DecisionMention')}
        if label in DD and (e['stage'] != 'decision_relation' or kinds != ('DecisionMention','DecisionMention') or k[1] == k[2]):
            raise ValueError('invalid gold decision-event relation')
        if label in PROCESS and (e['stage'] != 'typed_relation' or kinds != allowed[label]):
            raise ValueError('invalid gold process relation')
        if k in gold_rows and gold_rows[k] != label: raise ValueError("conflicting gold event relation labels")
        gold_rows[k] = label
        if label in POSITIVE: gold_edges.add((*k, label))
        if label == "uncertain": unresolved.add(k)
    predicted, outside = {}, 0
    for e in graph["edges"]:
        if e["stage"] == "coreference": continue
        if e["from"] not in all_pred or e["to"] not in all_pred: raise ValueError("dangling prediction edge")
        k = edge_key(e, p_canonical)
        if k is None: outside += 1; continue
        if e["relation"] in POSITIVE:
            triple = (*k, e["relation"])
            predicted[triple] = max(predicted.get(triple, float("-inf")), e.get("score", 0.0))
    unassessed = {t for t in predicted if t[:3] in unresolved}
    assessed = set(predicted)-unassessed

    def family(labels):
        gp = {t for t in gold_edges if t[3] in labels}; pp = {t for t in assessed if t[3] in labels}
        per_class = {l: rates(sum(t[3] == l for t in pp & gp), sum(t[3] == l for t in pp), sum(t[3] == l for t in gp)) for l in sorted(labels)}
        fs = [x["f1"] for x in per_class.values() if x["f1"] is not None]
        return {**rates(len(pp & gp), len(pp), len(gp)), "per_class": per_class,
                "macro_f1": sum(fs)/len(fs) if fs else None,
                "macro_labels_with_support": [l for l in sorted(labels) if per_class[l]["f1"] is not None]}

    curves = []
    for threshold in [0, .25, .5, .75, 1.0]:
        selected = {t for t, score in predicted.items() if score >= threshold}
        known, unknown = selected-unassessed, selected & unassessed; tp = len(known & gold_edges)
        curves.append({"threshold": threshold, "accepted_edges": len(selected), "assessed_edges": len(known),
            "unassessed_edges": len(unknown), "correct_edges": tp,
            "precision": tp/len(selected) if selected and not unknown else None,
            "precision_on_assessed": tp/len(known) if known else None,
            "precision_lower_bound": tp/len(selected) if selected else None,
            "precision_upper_bound": (tp+len(unknown))/len(selected) if selected else None,
            "accepted_fraction_of_predicted_positive_edges": len(selected)/len(predicted) if predicted else None,
            "gold_positive_recall": tp/len(gold_edges) if gold_edges else None})
    p_dec = {p_tokens[m]: p_member.get(m, "singleton:"+m) for m in pred if pred[m]["kind"] == "DecisionMention"}
    g_dec = {g_tokens[m]: gold["mention_to_event"][m] for m in truth if truth[m]["kind"] == "DecisionMention"}
    gpairs, ppairs = coref_pairs(g_dec), coref_pairs(p_dec)
    g_dec_ids = {i for i, m in truth.items() if m["kind"] == "DecisionMention"}
    p_dec_ids = {i for i, m in pred.items() if m["kind"] == "DecisionMention"}
    matched_dec = sum(p in p_dec_ids and g in g_dec_ids for p, g in alignment.items())
    candidate_pairs = set()
    for e in graph.get("candidate_ledger", graph["edges"]):
        if e["stage"] != "coreference":
            k = edge_key(e, p_canonical)
            if k: candidate_pairs.add(k)
    confusion, pred_labels = Counter(), defaultdict(set)
    for t in assessed: pred_labels[t[:3]].add(t[3])
    for k in set(gold_rows) | set(pred_labels):
        gl = gold_rows.get(k, "NO_RELATION")
        pl = "MULTIPLE_RELATIONS" if len(pred_labels[k]) > 1 else next(iter(pred_labels[k]), "NO_POSITIVE_PREDICTION")
        confusion[gl, pl] += 1
    combined = family(POSITIVE)
    return {"evaluation_version": "0.3", "evaluation_mode": gold.get("evaluation_mode", "end_to_end"),
        "mention_detection_and_typing": rates(len(alignment), len(pred), len(truth)),
        "decision_mention_identity_recall": rates(matched_dec, len(p_dec_ids), len(g_dec_ids)),
        "coreference_pairwise": rates(len(ppairs & gpairs), len(ppairs), len(gpairs)),
        "coreference_b3_common_mentions": _b3(p_dec, g_dec),
        "event_alignment": {"policy": "positive-overlap one-to-one maximum Jaccard; relation-blind; sorted-ID ties",
            "predicted_events": len(pc), "gold_events": len(gc), "matched_events": len(event_match), "mapping": event_match},
        "decision_relation_edges": family(DD), "process_link_edges": family(PROCESS),
        "typed_relation_edges": combined, "relation_macro_f1": combined["macro_f1"],
        "candidate_edge_recall": sum(t[:3] in candidate_pairs for t in gold_edges)/len(gold_edges) if gold_edges else None,
        "confusion": [{"gold": a, "predicted": b, "count": n} for (a, b), n in sorted(confusion.items())],
        "scope_graph_complete": len(alignment) == len(pred) == len(truth) and assessed == gold_edges
            and {frozenset(c) for c in pc.values()} == {frozenset(c) for c in gc.values()} and not unassessed,
        "unassessed_prediction_edges": len(unassessed), "out_of_scope_prediction_mentions": len(all_pred)-len(pred),
        "out_of_scope_prediction_edges": outside,
        "unannotated_prediction_pairs": len({t[:3] for t in assessed}-set(gold_rows)),
        "coverage_precision_curve": curves,
        "review_rate": sum(e.get("status") == "REVIEW" for e in graph["edges"])/len(graph["edges"]) if graph["edges"] else None,
        "auto_accept_enabled": False, "gold_origin": gold["origin"],
        "limitations": ["Exact source-span/type or audited manual mention alignment required",
            "B3 conditional on common aligned mentions; detection scored separately",
            "Unknown gold edges unassessed with precision bounds; absent complete-scope edges are negatives",
            "Report decision and process metrics separately; combined fields retained for compatibility",
            "Scope-level unweighted metrics; no confidence calibration or clinical Reference"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--graph", type=Path, required=True); p.add_argument("--gold", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True); args = p.parse_args()
    repo = Path(__file__).resolve().parents[3]
    if not any(args.output.resolve().is_relative_to(repo/d) for d in ["private", "outputs"]):
        p.error("identity-bearing evaluation output must stay in private/ or outputs/")
    result = evaluate(json.loads(args.graph.read_text()), json.loads(args.gold.read_text()))
    with args.output.open("x") as f: json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__": main()

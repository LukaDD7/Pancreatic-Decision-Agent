"""Development challenge-set evaluation; never imported by reconstruction code.

References are source-only AI development reviews, not exhaustive clinical gold.
Only this module may read reference labels and prior pipeline annotations. It
does not call a model, change production outputs, or infer missing annotations.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any


def read(path):
    return json.loads(Path(path).read_text())


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def metric(numerator, denominator, *, reason="no eligible denominator"):
    if not denominator:
        return {"status": "not_measured", "numerator": numerator, "denominator": denominator,
                "value": None, "reason": reason}
    return {"status": "measured", "numerator": numerator, "denominator": denominator,
            "value": numerator / denominator}


def load_reference(path):
    """Read frozen references and require an external, matching SHA256 receipt."""
    path = Path(path)
    paths = sorted(path.glob("reference_[ab].json")) if path.is_dir() else [path]
    if not paths:
        raise ValueError("NO_REFERENCE_FILES")
    items, receipts, seen = [], [], set()
    for source in paths:
        data = read(source)
        if "FROZEN" not in str(data.get("status", "")):
            raise ValueError("REFERENCE_NOT_FROZEN:" + str(source))
        actual = file_sha(source)
        receipt_path = source.with_name(source.stem + "_validation.json")
        if not receipt_path.exists():
            receipt_path = source.with_name(source.stem + "_freeze.json")
        if not receipt_path.exists():
            receipt_path = source.with_suffix(".freeze.json")
        if not receipt_path.exists():
            raise ValueError("REFERENCE_FREEZE_RECEIPT_MISSING:" + str(source))
        freeze = read(receipt_path)
        expected = freeze.get("reference_sha256", freeze.get("file_sha256", freeze.get("sha256")))
        if expected != actual:
            raise ValueError("REFERENCE_FROZEN_HASH_MISMATCH:" + str(source))
        if freeze.get("status", "PASS") not in {"PASS", "FROZEN", "FROZEN_DEVELOPMENT_REFERENCE"}:
            raise ValueError("REFERENCE_FREEZE_RECEIPT_NOT_PASS:" + str(receipt_path))
        for item in data["items"]:
            identity = item["reference_id"]
            if identity in seen:
                raise ValueError("DUPLICATE_REFERENCE_ID:" + identity)
            if item.get("label") not in {"positive", "negative", "uncertain"}:
                raise ValueError("INVALID_REFERENCE_LABEL:" + identity)
            seen.add(identity)
            items.append(_normalize_reference_item(item))
        receipts.append({"path": str(source.resolve()), "sha256": actual,
                         "freeze_receipt": str(receipt_path.resolve()),
                         "origin": data.get("origin"), "coverage": data.get("coverage"),
                         "blinding": data.get("blinding"), "expert_gold": False,
                         "clinical_reference": False})
    return {"items": items, "frozen_references": receipts}


def _normalize_reference_item(item):
    item = deepcopy(item)
    item.setdefault("title", item.get("review_target"))
    expected = item.get("checkpoint_expectation", item.get("checkpoint_eligible"))
    if expected in {"initial_process_anchor", "initial_pathway_anchor"}:
        category = "initial_anchor"
    elif expected in {"eligible_new_evidence_round", "yes"}:
        category = "new_evidence_round"
    elif expected == "candidate_initial_or_new_diagnostic_choice":
        category = "anchor_or_new_diagnostic_choice"
    elif expected in {"uncertain_new_evidence_round", "uncertain_test_choice", "uncertain"}:
        category = "uncertain"
    elif item.get("label") == "negative":
        category = "negative"
    else:
        category = "not_defined"
    item["checkpoint_category"] = category
    evidence = item.get("evidence_expectations", {})
    if isinstance(evidence, list):
        converted = {"needed": [], "forbidden": [], "current_action_to_mask": []}
        for expectation in evidence:
            source = {**expectation["source"], "reason": expectation.get("reason")}
            kind = expectation["expected"]
            if kind == "needed":
                converted["needed"].append(source)
            elif kind == "mask_current_choice":
                converted["current_action_to_mask"].append({**source, "availability": "current_choice_not_prechoice_evidence"})
            else:
                converted["forbidden"].append({**source, "availability": kind})
        item["evidence_expectations"] = converted
    return item


def _source_hashes(case_dir):
    path = case_dir / "source_fhir_r4_bundle.json"
    if not path.exists():
        return {}
    result = {}
    for entry in read(path).get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") in {"Basic", "Provenance"}:
            continue
        for extension in resource.get("extension", []):
            if extension.get("url", "").endswith("source-clock-and-provenance"):
                meta = json.loads(extension["valueString"])
                if meta.get("source_id") and meta.get("source_sha256"):
                    result[meta["source_id"]] = meta["source_sha256"]
    return result


def _span(value, hashes=None):
    if not isinstance(value, dict):
        return None
    outer = value
    value = value.get("anchor", value)
    source_id = value.get("source_id", outer.get("source_id"))
    start, end = value.get("start"), value.get("end")
    if not source_id or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        return None
    return {"source_id": source_id,
            "source_sha256": value.get("source_sha256", (hashes or {}).get(source_id)),
            "start": start, "end": end, "quote": value.get("quote"),
            "interval": outer.get("interval", value.get("interval")),
            "kind": outer.get("kind", value.get("kind")),
            "disposition": outer.get("disposition", value.get("disposition"))}


def _citation_spans(citations, hashes=None):
    if isinstance(citations, list):
        return [span for item in citations if (span := _span(item, hashes))]
    result = []
    for item in (citations or {}).get("valid", []):
        span = _span(item, hashes)
        if span:
            result.append(span)
    for item in (citations or {}).get("ambiguous", []):
        if not item.get("eligible_as_source_support"):
            continue
        for occurrence in item.get("occurrences", []):
            span = _span({**item, **occurrence}, hashes)
            if span:
                result.append(span)
    return result


def _action_spans(event, hashes=None):
    if "action_citations" in event:
        return _citation_spans(event["action_citations"], hashes)
    # Baseline semantic.supports also includes facts; retain only support at the
    # actual proposed action anchor, not arbitrary facts elsewhere in the note.
    anchor = _span(event.get("anchor"), hashes)
    result = []
    for support in event.get("semantic", {}).get("supports", []):
        span = _span(support, hashes)
        if span and anchor and _overlap(span, anchor) > 0:
            result.append({**span, "start": max(span["start"], anchor["start"]),
                           "end": min(span["end"], anchor["end"])})
    return result


def _overlap(left, right):
    if not left or not right or left["source_id"] != right["source_id"]:
        return 0
    if (not left.get("source_sha256") or not right.get("source_sha256")
            or left["source_sha256"] != right["source_sha256"]):
        return 0
    return max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))


def _contains(block, expected):
    return _overlap(block, expected) == expected["end"] - expected["start"]


def _exposure(blocks, expected, *, full=False):
    predicate = _contains if full else lambda block, target: _overlap(block, target) > 0
    if any(predicate(block, expected) for block in blocks):
        return True
    if any(block["source_id"] == expected["source_id"] and not block.get("source_sha256")
           for block in blocks):
        return None  # An unauthenticated block is not a certified negative.
    return False


def _subtract_needed_conflicts(forbidden, needed):
    """Do not score the same raw characters as both required and prohibited."""
    segments = [forbidden]
    conflict = False
    for required in needed:
        next_segments = []
        for segment in segments:
            if not _overlap(segment, required):
                next_segments.append(segment)
                continue
            conflict = True
            if segment["start"] < required["start"]:
                next_segments.append({**segment, "end": required["start"], "quote": None})
            if required["end"] < segment["end"]:
                next_segments.append({**segment, "start": required["end"], "quote": None})
        segments = next_segments
    return segments, conflict


def _event_id(event, index=0):
    return event.get("event_id", event.get("candidate_id", "unidentified-" + str(index)))


def _scope(event):
    review = event.get("semantic_review") or {}
    return review.get("scope", event.get("scope", event.get("semantic", {}).get("scope", "uncertain")))


def _function(event):
    review = event.get("semantic_review") or {}
    return review.get("record_function", event.get("record_function", event.get("semantic", {}).get("record_function")))


def _decision(event):
    if _scope(event) != "yes":
        return False
    function = _function(event)
    if function in {"repeat", "execution", "generic", "uncertain", "existing_plan_restatement", "historical_restatement"}:
        return False
    if event.get("semantic", {}).get("role") in {"report_restatement", "template", "nondecision", "uncertain"}:
        return False
    if event.get("historical_hint") or event.get("semantic", {}).get("temporal_role") == "historical":
        return False
    return function in {"new_decision", "reassessment", "new_choice_or_assessment"} or (
        function is None and event.get("semantic", {}).get("role") == "decision")


def _selected(event):
    return str(event.get("checkpoint_status", "")).startswith("CANDIDATE_")


def _uncertain(event):
    status = str(event.get("checkpoint_status", ""))
    return (any(word in status for word in ["UNCERTAIN", "UNRESOLVED", "DEFERRED", "FAILED"])
            or _scope(event) == "uncertain" or _function(event) == "uncertain")


def _asserted_checkpoint(event):
    return _selected(event) and not _uncertain(event)


def _event_edges(events, references, hashes=None):
    edges = {}
    for index, event in enumerate(events):
        spans = _action_spans(event, hashes)
        matches = []
        for ref_index, reference in enumerate(references):
            expected = _span(reference["anchor"])
            overlap = max((_overlap(span, expected) for span in spans), default=0)
            if overlap:
                matches.append((ref_index, overlap / (expected["end"] - expected["start"])))
        edges[index] = [ref_index for ref_index, _ in sorted(matches, key=lambda pair: (-pair[1], pair[0]))]
    return edges


def _assignment(edges):
    """Maximum-cardinality one-to-one alignment; no candidate id dependence."""
    assigned = {}

    def visit(event_index, seen):
        for reference_index in edges[event_index]:
            if reference_index in seen:
                continue
            seen.add(reference_index)
            if reference_index not in assigned or visit(assigned[reference_index], seen):
                assigned[reference_index] = event_index
                return True
        return False

    for event_index in sorted(edges, key=lambda index: (len(edges[index]), index)):
        visit(event_index, set())
    return assigned


def _cached_actual_contexts(case_dir, events, cache_dir):
    """Recover blocks actually forwarded by baseline LLM2, excluding choices."""
    if cache_dir is None or not (case_dir / "model_receipts.json").exists():
        return {}, ["BASELINE_ACTUAL_MODEL_CACHE_UNAVAILABLE"]
    by_id = {_event_id(event): event for event in events}
    contexts, warnings = {}, []
    for receipt in read(case_dir / "model_receipts.json"):
        if receipt.get("task") != "evidence_evolution" or receipt.get("status") != "SUCCESS":
            continue
        path = cache_dir / (receipt["request_sha256"] + ".json")
        if not path.exists():
            warnings.append("MISSING_BASELINE_REQUEST:" + receipt["request_sha256"])
            continue
        cached = read(path)
        # v0.3 keys include [endpoint, body], while the endpoint is deliberately
        # absent from the cache file. Do not mislabel hash(body) as that key, or
        # read API credentials to reconstruct it. Verify receipt identity and
        # the saved result digest, and separately record the body/file hashes.
        if cached.get("receipt", {}).get("request_sha256") != receipt["request_sha256"]:
            raise ValueError("BASELINE_CACHED_RECEIPT_IDENTITY_MISMATCH:" + str(path))
        if "result_sha256" in cached and _digest(cached.get("result")) != cached["result_sha256"]:
            raise ValueError("BASELINE_CACHED_RESULT_HASH_MISMATCH:" + str(path))
        payload = json.loads(cached["request"]["messages"][-1]["content"])["input"]
        event_id = payload["current_decision"]["candidate_id"]
        window_path = case_dir / "window_audits" / (event_id + ".json")
        if not window_path.exists():
            warnings.append("MISSING_BASELINE_WINDOW:" + event_id)
            continue
        window = read(window_path)
        event = by_id.get(event_id, {})
        previous = by_id.get(event.get("previous_candidate_id"), {})
        alias_map = {f"E{index:04}": block for index, block in enumerate(window.get("blocks", []))}
        carried = previous.get("evolution", {}).get("supports", [])
        for index, quote in enumerate(carried):
            alias_map[f"BASE{index:03}"] = {"anchor": quote, "interval": "baseline_carried_quote"}
        selected, seen = [], set()
        for page in event.get("evolution", {}).get("paging", {}).get("page_results", []):
            for quote in page.get("selections", []):
                identity = (quote["source_id"], quote["start"], quote["end"])
                if identity not in seen:
                    selected.append(quote)
                    seen.add(identity)
        for index, quote in enumerate(selected):
            alias_map[f"PAGE{index:04}"] = {"anchor": quote}
        blocks, unresolved = [], []
        for provided in payload.get("blocks", []):
            alias = provided["alias"]
            if alias in {"CURRENT", "PREVIOUS"}:
                continue  # Permitted reconstruction disclosure, not evidence.
            raw = alias_map.get(alias)
            if raw is None:
                unresolved.append(alias)
                continue
            span = _span(raw)
            displayed = provided.get("source_text", "")
            raw_quote = (raw.get("anchor", raw)).get("quote", "")
            if not span or len(displayed) != len(raw_quote) or any(
                    shown != actual and shown != "*" for shown, actual in zip(displayed, raw_quote)):
                unresolved.append(alias)
                continue
            blocks.append({**span, "interval": provided.get("interval"),
                           "kind": provided.get("kind"), "alias": alias})
        contexts[event_id] = {"blocks": blocks, "measured": not unresolved,
                              "partially_measured": bool(unresolved), "unresolved_aliases": unresolved,
                              "origin": "actual_cached_evidence_evolution_request",
                              "request_sha256": receipt["request_sha256"],
                              "request_body_sha256": _digest(cached["request"]),
                              "cache_file_sha256": file_sha(path)}
    return contexts, sorted(set(warnings))


def load_run(root, case_ids, *, baseline=False, cache_dir=None):
    root = Path(root) if root is not None else None
    if root is None or not root.exists():
        return {"status": "not_run", "cases": {}, "root": str(root) if root else None}
    if baseline and cache_dir is None:
        candidates = [root.parent / "full_fixed10" / "model_cache", root / "model_cache", root.parent / "model_cache"]
        cache_dir = next((candidate for candidate in candidates if candidate.exists()), None)
    elif cache_dir is not None:
        cache_dir = Path(cache_dir)
    cases = {}
    for case_id in sorted(case_ids):
        case_dir = root / case_id
        event_file = case_dir / "decision_events.json"
        discovery_file = case_dir / ("normalization.json" if baseline else "discovery.json")
        if not event_file.exists() and not discovery_file.exists():
            continue
        events = read(event_file) if event_file.exists() else []
        discovery_data = read(discovery_file) if discovery_file.exists() else {}
        discovered = discovery_data.get("results", []) if baseline else discovery_data.get("events", [])
        hashes = _source_hashes(case_dir)
        warnings = []
        contexts = {}
        windows = {}
        if baseline:
            contexts, warnings = _cached_actual_contexts(case_dir, events, cache_dir)
            for event in events:
                window_path = case_dir / "window_audits" / (_event_id(event) + ".json")
                if window_path.exists():
                    window = read(window_path)
                    windows[_event_id(event)] = {"blocks": window.get("blocks", []), "measured": True,
                                                "origin": "compiled_window_not_actual_model_material"}
            raw_path = case_dir / "rule_candidates.json"
            raw = read(raw_path) if raw_path.exists() else []
        else:
            for event in events:
                event_id = _event_id(event)
                if event.get("evidence_context") is not None:
                    windows[event_id] = {**event["evidence_context"], "measured": True,
                                         "origin": "compiled_evidence_context"}
                actual = event.get("actual_model_evidence", event.get("evidence_context"))
                if isinstance(actual, list):
                    actual = {"blocks": actual}
                if actual is not None:
                    contexts[event_id] = {**actual, "measured": actual.get("measured", True),
                                          "origin": "actual_model_evidence" if "actual_model_evidence" in event
                                          else "event_evidence_context_contract"}
            inventory_path = case_dir / "unit_inventory.json"
            raw = read(inventory_path) if inventory_path.exists() else []
            if isinstance(raw, dict):
                raw = raw.get("units", raw.get("items", []))
        cases[case_id] = {"events": events, "discovered": discovered, "raw": raw,
                          "initial_unit_kind": "rule_anchor" if baseline else "fulltext_routing",
                          "source_hashes": hashes, "contexts": contexts, "windows": windows,
                          "final_available": event_file.exists(), "warnings": warnings,
                          "input_files": {str(path.resolve()): file_sha(path) for path in [event_file, discovery_file,
                              case_dir / "source_fhir_r4_bundle.json", case_dir / "model_receipts.json",
                              case_dir / "unit_inventory.json"]
                                          if path.exists()}}
    return {"status": "available" if cases else "no_case_outputs", "root": str(root.resolve()),
            "cases": cases, "baseline_cache": str(cache_dir) if cache_dir else None}


def _blocks(context, hashes):
    result = []
    entries = context if isinstance(context, list) else (context or {}).get("blocks", [])
    for raw in entries:
        span = _span(raw, hashes)
        if span:
            result.append(span)
    return result


def _observations(event, hashes):
    if "validated_observation_citations" in event:
        return _citation_spans(event["validated_observation_citations"], hashes)
    return _citation_spans(event.get("evolution", {}).get("supports", []), hashes)


def _forbidden_kind(span):
    availability = str(span.get("availability", ""))
    if availability == "historical_baseline_not_new":
        return "historical_not_new"
    if "uncertain" in availability:
        return "availability_uncertain"
    return "hard_forbidden"


def evaluate_run(reference_items, run):
    """Pure evaluator over normalized case outputs, also used by fixture tests."""
    if run.get("status") in {"not_run", "no_case_outputs"}:
        return {"status": "not_measured", "reason": "run outputs unavailable", "root": run.get("root")}
    reference_items = [_normalize_reference_item(item) for item in reference_items]
    totals = Counter()
    details, warnings = [], []
    unavailable_cases = sorted({item["case_id"] for item in reference_items} - set(run["cases"]))
    for case_id, case in sorted(run["cases"].items()):
        references = [item for item in reference_items if item["case_id"] == case_id]
        positives = [item for item in references if item["label"] == "positive"]
        hashes = case.get("source_hashes", {})
        discovered = [event for event in case.get("discovered", []) if _decision(event)]
        decisions = [event for event in case.get("events", []) if _decision(event)]
        selected = [event for event in case.get("events", []) if _selected(event)]
        selected_valid = [event for event in selected if _decision(event) and not _uncertain(event)]
        discovery_assignment = _assignment(_event_edges(discovered, positives, hashes))
        decision_assignment = _assignment(_event_edges(decisions, positives, hashes))
        selected_edges = _event_edges(selected_valid, positives, hashes)
        selected_assignment = _assignment(selected_edges)
        cp_positives = [item for item in positives if item["checkpoint_category"] in {
            "initial_anchor", "new_evidence_round", "anchor_or_new_diagnostic_choice"}]
        cp_assignment = _assignment(_event_edges(selected_valid, cp_positives, hashes))
        new_rounds = [item for item in positives if item["checkpoint_category"] == "new_evidence_round"]
        initial_anchors = [item for item in positives if item["checkpoint_category"] == "initial_anchor"]
        new_selected = [event for event in selected_valid if "NEW_EVIDENCE" in event.get("checkpoint_status", "")]
        first_selected = [event for event in selected_valid if "FIRST" in event.get("checkpoint_status", "")]
        new_assignment = _assignment(_event_edges(new_selected, new_rounds, hashes))
        anchor_assignment = _assignment(_event_edges(first_selected, initial_anchors, hashes))
        duplicate_edges = _event_edges(selected, positives, hashes)
        assigned_predictions = set(_assignment(duplicate_edges).values())
        totals["duplicate_selected_extras"] += sum(bool(matches) and index not in assigned_predictions
                                                     for index, matches in duplicate_edges.items())
        totals["selected_scope_or_function_inconsistent"] += len(selected) - len(selected_valid)
        totals["selected_with_explicit_uncertainty"] += sum(_uncertain(event) for event in selected)
        totals["discovery_positive_total"] += len(positives)
        totals["discovery_positive_hit"] += len(discovery_assignment)
        if case.get("final_available", True):
            totals["positive_total"] += len(positives)
            totals["decision_positive_hit"] += len(decision_assignment)
            totals["checkpoint_positive_total"] += len(cp_positives)
            totals["checkpoint_positive_hit"] += len(cp_assignment)
            totals["new_round_total"] += len(new_rounds)
            totals["new_round_correct"] += len(new_assignment)
            totals["initial_anchor_total"] += len(initial_anchors)
            totals["initial_anchor_correct"] += len(anchor_assignment)
        all_events = case.get("events", [])
        all_edges = _event_edges(all_events, references, hashes)
        matches_by_reference = {index: [] for index in range(len(references))}
        for event_index, matches in all_edges.items():
            for reference_index in matches:
                matches_by_reference[reference_index].append(all_events[event_index])
        for reference_index, reference in enumerate(references):
            matched = matches_by_reference[reference_index]
            label = reference["label"]
            entry = {"reference_id": reference["reference_id"], "case_id": case_id, "label": label,
                     "checkpoint_category": reference["checkpoint_category"],
                     "title": reference.get("title"), "matched_event_ids": [_event_id(event) for event in matched],
                     "selected_event_ids": [_event_id(event) for event in matched if _selected(event)],
                     "uncertain_event_ids": [_event_id(event) for event in matched if _uncertain(event)]}
            if label == "negative" and case.get("final_available", True):
                totals["negative_total"] += 1
                totals["negative_checkpoint_fp"] += any(_selected(event) for event in matched)
                totals["negative_independent_decision_fp"] += any(_decision(event) for event in matched)
            elif label == "uncertain" and case.get("final_available", True):
                totals["uncertain_total"] += 1
                forced_positive = any(_asserted_checkpoint(event) for event in matched)
                preserved = bool(matched) and not forced_positive and any(_uncertain(event) for event in matched)
                totals["uncertain_preserved"] += preserved
                totals["uncertain_forced_positive"] += forced_positive
                totals["uncertain_unmatched"] += not matched
                totals["uncertain_forced_negative"] += bool(matched) and not forced_positive and not preserved
            if reference["checkpoint_category"] == "uncertain" and case.get("final_available", True):
                forced = any(_asserted_checkpoint(event) for event in matched)
                totals["checkpoint_uncertain_total"] += 1
                totals["checkpoint_uncertain_preserved"] += bool(matched) and not forced and any(_uncertain(event) for event in matched)
                totals["checkpoint_uncertain_forced_positive"] += forced
                totals["checkpoint_uncertain_unmatched"] += not matched
            if label != "positive":
                details.append(entry)
                continue
            pos_index = positives.index(reference)
            entry.update(discovery_hit=pos_index in discovery_assignment,
                         decision_hit=pos_index in decision_assignment,
                         checkpoint_hit=pos_index in selected_assignment)
            initial_kind = case.get("initial_unit_kind", "rule_anchor")
            if initial_kind == "fulltext_routing":
                routed = any(reference["anchor"]["source_id"] in unit.get("source_ids", [])
                             for unit in case.get("raw", []))
            else:
                routed = any(_overlap(_span(unit, hashes), _span(reference["anchor"]))
                             for unit in case.get("raw", []))
            totals["initial_" + initial_kind + "_total"] += 1
            totals["initial_" + initial_kind + "_hit"] += routed
            entry["initial_proposal_or_routing_hit"] = routed
            preferred = (selected_valid[selected_assignment[pos_index]] if pos_index in selected_assignment else
                         decisions[decision_assignment[pos_index]] if pos_index in decision_assignment else
                         matched[0] if matched else None)
            contexts = case.get("contexts", {})
            context = contexts.get(_event_id(preferred)) if preferred is not None else None
            blocks = _blocks(context, hashes)
            measured = context is not None and context.get("measured", True)
            expected_evidence = reference.get("evidence_expectations", {})
            facts = []
            for expected in expected_evidence.get("needed", []):
                span = _span(expected)
                if span is None:
                    continue
                hit = _exposure(blocks, span, full=True)
                fact_measured = measured and hit is not None
                if fact_measured or preferred is None:
                    totals["needed_end_to_end_total"] += 1
                    totals["needed_end_to_end_hit"] += bool(hit)
                if fact_measured:
                    totals["needed_conditional_total"] += 1
                    totals["needed_conditional_hit"] += bool(hit)
                elif preferred is not None:
                    totals["needed_actual_material_unknown"] += 1
                facts.append({"kind": "needed", "source_id": span["source_id"], "start": span["start"],
                              "end": span["end"], "hit": hit if fact_measured or preferred is None else None,
                              "material_measured": fact_measured})
                availability = str(expected.get("availability", ""))
                if any(term in availability for term in ["proxy", "uncertified", "unknown", "uncertain"]):
                    totals["reference_needed_availability_uncertified"] += 1
            observed = _observations(preferred or {}, hashes)
            window = case.get("windows", {}).get(_event_id(preferred)) if preferred is not None else None
            window_blocks = _blocks(window, hashes)
            blind = (preferred or {}).get("blind_visible_evidence")
            blind_blocks = _blocks(blind, hashes)
            forbidden = list(expected_evidence.get("forbidden", [])) + list(expected_evidence.get("current_action_to_mask", []))
            required_spans = [span for item in expected_evidence.get("needed", []) if (span := _span(item))]
            for expected in forbidden:
                span = _span(expected)
                if span is None:
                    continue
                kind = _forbidden_kind(expected)
                scored_segments, conflict = _subtract_needed_conflicts(span, required_spans)
                if conflict:
                    totals["reference_needed_forbidden_overlap"] += 1
                def exposure_in(candidate_blocks):
                    values = [_exposure(candidate_blocks, part) for part in scored_segments]
                    return True if True in values else None if None in values or not scored_segments else False
                model_hit, window_hit = exposure_in(blocks), exposure_in(window_blocks)
                if kind == "hard_forbidden":
                    if measured and model_hit is not None:
                        totals["forbidden_actual_total"] += 1
                        totals["forbidden_actual_hit"] += model_hit
                    if window is not None and window_hit is not None:
                        totals["forbidden_window_total"] += 1
                        totals["forbidden_window_hit"] += window_hit
                    blind_hit = exposure_in(blind_blocks)
                    if blind is not None and blind_hit is not None:
                        totals["forbidden_blind_total"] += 1
                        totals["forbidden_blind_hit"] += blind_hit
                elif kind == "historical_not_new":
                    if measured and model_hit is not None:
                        totals["historical_new_claim_total"] += 1
                        evolution = (preferred or {}).get("semantic_review", {}).get("evidence_evolution",
                            (preferred or {}).get("evolution", {}).get("evidence_evolution"))
                        totals["historical_new_claim_hit"] += evolution == "new_relevant" and any(
                            _overlap(quote, span) for quote in observed)
                else:
                    if measured and model_hit is not None:
                        totals["uncertain_availability_context_total"] += 1
                        totals["uncertain_availability_context_hit"] += model_hit
                        totals["uncertain_availability_observed_as_fact"] += any(_overlap(quote, span) for quote in observed)
                facts.append({"kind": kind, "source_id": span["source_id"], "start": span["start"], "end": span["end"],
                              "actual_evidence_hit": model_hit if measured else None,
                              "compiled_window_hit": window_hit if window is not None else None,
                              "reference_needed_forbidden_conflict": conflict,
                              "scored_nonconflicting_ranges": [[part["start"], part["end"]] for part in scored_segments]})
            entry["evidence_checks"] = facts
            entry["evaluated_event_id"] = _event_id(preferred) if preferred else None
            entry["actual_material_origin"] = context.get("origin") if context else None
            entry["actual_material_measured"] = measured
            if context:
                entry["actual_material_receipt"] = {key: context[key] for key in ["request_sha256", "request_body_sha256",
                    "cache_file_sha256", "unresolved_aliases"] if key in context}
            if preferred is not None:
                uncertainty = preferred.get("uncertainty_dimensions", {})
                basis = preferred.get("time_basis", "unknown")
                totals["matched_positive_time_uncertified"] += basis not in {"explicit_documented_choice"}
                totals["matched_positive_source_availability_uncertified"] += uncertainty.get("source_availability") != "certified"
                totals["matched_positive_source_gap_reported"] += bool(preferred.get("source_gaps"))
            details.append(entry)
        warnings.extend(case.get("warnings", []))
    return {"status": "development_challenge_set_only", "root": run.get("root"),
            "evaluated_cases": sorted(run["cases"]), "unavailable_cases": unavailable_cases,
            "metrics": {
                "discovery_positive_coverage": metric(totals["discovery_positive_hit"], totals["discovery_positive_total"]),
                "independent_decision_positive_coverage": metric(totals["decision_positive_hit"], totals["positive_total"]),
                "candidate_checkpoint_positive_coverage": metric(totals["checkpoint_positive_hit"], totals["checkpoint_positive_total"]),
                "new_evidence_round_correctly_selected": metric(totals["new_round_correct"], totals["new_round_total"]),
                "initial_anchor_correctly_selected": metric(totals["initial_anchor_correct"], totals["initial_anchor_total"]),
                "initial_rule_anchor_coverage": metric(totals["initial_rule_anchor_hit"], totals["initial_rule_anchor_total"]),
                "initial_fulltext_unit_routing_coverage": metric(totals["initial_fulltext_routing_hit"], totals["initial_fulltext_routing_total"]),
                "labelled_negative_checkpoint_false_positive_rate": metric(totals["negative_checkpoint_fp"], totals["negative_total"]),
                "labelled_negative_specificity": metric(totals["negative_total"] - totals["negative_checkpoint_fp"], totals["negative_total"]),
                "labelled_negative_independent_decision_rate": metric(totals["negative_independent_decision_fp"], totals["negative_total"]),
                "uncertain_abstain_rate": metric(totals["uncertain_preserved"], totals["uncertain_total"]),
                "checkpoint_uncertain_abstain_rate": metric(totals["checkpoint_uncertain_preserved"], totals["checkpoint_uncertain_total"]),
                "needed_actual_material_coverage_conditional": metric(totals["needed_conditional_hit"], totals["needed_conditional_total"]),
                "needed_material_end_to_end_coverage": metric(totals["needed_end_to_end_hit"], totals["needed_end_to_end_total"]),
                "forbidden_actual_evidence_rate": metric(totals["forbidden_actual_hit"], totals["forbidden_actual_total"]),
                "forbidden_compiled_window_rate": metric(totals["forbidden_window_hit"], totals["forbidden_window_total"]),
                "forbidden_blind_packet_rate": metric(totals["forbidden_blind_hit"], totals["forbidden_blind_total"]),
                "historical_baseline_claimed_new_rate": metric(totals["historical_new_claim_hit"], totals["historical_new_claim_total"]),
                "precision": {"status": "not_measured", "value": None, "reason": "reference is a non-exhaustive challenge set"},
                "whole_case_recall": {"status": "not_measured", "value": None, "reason": "reference is not a complete event census"},
            }, "counts": dict(totals), "warnings": sorted(set(warnings)), "items": details,
            "input_file_receipts": {case_id: case.get("input_files", {}) for case_id, case in run["cases"].items()}}


def evaluate(reference, baseline_root=None, current_root=None, *, baseline_cache=None):
    frozen = load_reference(reference)
    cases = {item["case_id"] for item in frozen["items"]}
    baseline = load_run(baseline_root, cases, baseline=True, cache_dir=baseline_cache)
    current = load_run(current_root, cases)
    baseline_report = evaluate_run(frozen["items"], baseline)
    current_report = evaluate_run(frozen["items"], current)
    differences = {}
    for name, before in baseline_report.get("metrics", {}).items():
        after = current_report.get("metrics", {}).get(name, {})
        if before.get("status") == after.get("status") == "measured":
            differences[name] = {"baseline": before["value"], "current": after["value"],
                                 "delta": after["value"] - before["value"],
                                 "same_denominator": before["denominator"] == after["denominator"],
                                 "same_case_coverage": baseline_report.get("evaluated_cases") == current_report.get("evaluated_cases")}
    return {"schema_version": "v0.4-development-evaluation-raw-v1",
            "status": "DEVELOPMENT_RAW_REPORT", "expert_gold": False, "clinical_validation": False,
            "reference_frozen_receipts": frozen["frozen_references"],
            "reference_label_counts": dict(Counter(item["label"] for item in frozen["items"])),
            "contract": {
                "event_alignment": "source id + exact source hash + supported action span overlap; one-to-one positive coverage",
                "evidence_needed": "entire reference span must be contained in actual evidence material",
                "evidence_forbidden": "any reference-span overlap in eligible evidence material; current reconstruction disclosure excluded",
                "no_denominator": "not_measured", "precision_and_whole_case_recall": "not measured: incomplete challenge set",
                "time_and_source_gaps": "reported separately; absence of a gap flag does not certify availability",
                "explicit_uncertainty": "retained CANDIDATE_*UNCERTAIN traces are abstentions, not asserted positive checkpoints",
                "reference_boundary_conflicts": "characters labelled both needed and forbidden are flagged and unscored; nonconflicting forbidden ranges remain evaluated",
            }, "baseline": baseline_report, "current": current_report, "differences": differences}


def render_markdown(report):
    lines = ["# 原文开发挑战集指标（raw report）", "",
             "这些是AI原文开发审查指标，不是医生gold或临床验证。参考集非穷尽，不能报告整体precision/F1或全病例召回。", "",
             "| 指标 | v0.3 | v0.4 |", "|---|---:|---:|"]
    names = list(report.get("baseline", {}).get("metrics", {}))
    for name in report.get("current", {}).get("metrics", {}):
        if name not in names:
            names.append(name)
    for name in names:
        values = []
        for key in ["baseline", "current"]:
            metric_value = report[key].get("metrics", {}).get(name, {})
            if metric_value.get("status") == "measured":
                values.append(f"{metric_value['numerator']}/{metric_value['denominator']} ({metric_value['value']:.1%})")
            else:
                values.append("not_measured")
        lines.append(f"| {name} | {values[0]} | {values[1]} |")
    lines.extend(["", "同一正例的额外多选、uncertain被强判、时间未认证及来源缺口见JSON counts；逐项命中与材料依据见items。",
                  "CURRENT/PREVIOUS与当前全文供重建识别，不作为盲评泄漏；表中泄漏仅核对evidence_context或明确的blind packet。", "",
                  "参考材料中同时被标为needed和mask的字符单列待核，不参与泄漏判错；其余无冲突范围照常评分。含明确UNCERTAIN的保留候选不计为强判阳性。", "",
                  "冻结参考："])
    for receipt in report["reference_frozen_receipts"]:
        lines.append(f"- `{Path(receipt['path']).name}` SHA256 `{receipt['sha256']}`")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--current")
    parser.add_argument("--baseline-cache")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = evaluate(args.reference, args.baseline, args.current, baseline_cache=args.baseline_cache)
    output = Path(args.output)
    if output.suffix == ".json":
        json_path, markdown_path = output, output.with_suffix(".md")
        output.parent.mkdir(parents=True, exist_ok=True)
    else:
        output.mkdir(parents=True, exist_ok=True)
        json_path, markdown_path = output / "evaluation_raw.json", output / "evaluation_raw.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    markdown_path.write_text(render_markdown(report))
    print(json.dumps({"status": report["status"], "json": str(json_path), "markdown": str(markdown_path),
                      "labels": report["reference_label_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Layered source-challenge diagnostics, separate from frozen legacy metrics.

Evaluation-only: reconstruction must never import this module or its references.
Raw-span coverage does not certify event identity, novelty, relation semantics or
clinical correctness. A non-exhaustive challenge set cannot measure global F1.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from .evaluate import (
    _action_spans, _assignment, _citation_spans, _decision, _event_edges,
    _event_id, _function, _normalize_reference_item, _overlap, _scope,
    _selected, _span, _uncertain, evaluate_run, file_sha, load_reference,
    load_run, read,
)
from .contract_scorecard import _verified_external_events


DIMENSIONS = (
    "scope", "event_identity_or_granularity", "choice_time",
    "source_availability", "evidence_evolution",
    "evidence_to_decision_connection", "process_connection",
)
REASON_CATEGORIES = {
    "genuine_missing_information", "rule_ambiguity", "processing_gap",
    "model_conflict", "no_uncertainty",
}


def _coverage(hit, miss, unknown=0):
    total = hit + miss + unknown
    if not total:
        return {"status": "not_measured", "hit": hit, "known_miss": miss,
                "unknown": unknown, "denominator": 0, "value": None}
    return {"status": "partially_measured" if unknown else "measured",
            "hit": hit, "known_miss": miss, "unknown": unknown,
            "denominator": total, "value": hit / total,
            "lower_bound": hit / total, "upper_bound": (hit + unknown) / total}


def _not_measured(reason):
    return {"status": "not_measured", "value": None, "reason": reason}


def _primary_span(event, hashes):
    # This is only a locator diagnostic. Citation order is not clinical gold.
    primary = _span(event.get("anchor"), hashes)
    if primary is not None:
        return primary
    actions = _action_spans(event, hashes)
    return actions[0] if actions else None


def _matches(events, reference, hashes):
    expected = _span(reference["anchor"])
    result = []
    for index, event in enumerate(events):
        spans = [span for span in _action_spans(event, hashes)
                 if _overlap(span, expected)]
        if not spans:
            continue
        primary = _primary_span(event, hashes)
        result.append({
            "event_id": _event_id(event, index),
            "scope": _scope(event), "record_function": _function(event),
            "checkpoint_status": event.get("checkpoint_status"),
            "program_reports_independent_decision": _decision(event),
            "program_selects_checkpoint": _selected(event),
            "program_reports_global_uncertainty": _uncertain(event),
            "mention_origin": event.get("mention_origin", "retained_event"),
            "primary_locator_overlap": bool(_overlap(primary, expected)),
            "secondary_only_support_overlap": not bool(_overlap(primary, expected)),
            "matched_citation_ranges": [
                {key: span.get(key) for key in
                 ("source_id", "source_sha256", "start", "end")}
                for span in spans],
        })
    return result


def _dimension_records(event):
    review = event.get("semantic_review") or {}
    raw = event.get("uncertainty_dimensions", review.get("uncertainty_dimensions", {}))
    if isinstance(raw, list):
        return {item["dimension"]: item for item in raw
                if isinstance(item, dict) and item.get("dimension")}
    return raw if isinstance(raw, dict) else {}


def uncertainty_completeness(event):
    """Output documentation completeness only; never uncertainty accuracy."""
    records = _dimension_records(event)
    citations = event.get("uncertainty_dimension_citations", {})
    details = {}
    for dimension in DIMENSIONS:
        item = records.get(dimension)
        if not isinstance(item, dict):
            details[dimension] = {"complete": False, "reason": "structured_dimension_missing"}
            continue
        status = item.get("status")
        category = item.get("reason_category")
        explanation = item.get("explanation")
        has_explanation = isinstance(explanation, str) and bool(explanation.strip())
        complete = (status in {"certain", "uncertain", "not_applicable"} and
                    category in REASON_CATEGORIES and
                    (has_explanation or status != "uncertain"))
        raw_quotes = item.get("source_quotes", [])
        verified = bool(_citation_spans(citations.get(dimension)))
        details[dimension] = {
            "complete": complete, "status": status,
            "reason_category": category, "has_explanation": has_explanation,
            "source_quote_declared": isinstance(raw_quotes, list) and bool(raw_quotes),
            "source_quote_verified": verified,
        }
    return {"event_id": _event_id(event), "all_dimensions_complete":
            all(item["complete"] for item in details.values()), "dimensions": details}


def selected_new_documentation(event):
    """Mechanical evidence/comparison documentation, not semantic truth."""
    review = event.get("semantic_review") or {}
    comparison = review.get("evidence_comparison") or {}
    validation = event.get("comparison_validation") or {}
    change = comparison.get("change_kind")
    previous = event.get("previous_event_id", review.get("previous_event_id"))
    prior_ids = event.get("provided_prior_event_ids", [])
    checks = {
        "previous_event_provided": bool(previous and previous in prior_ids),
        "previous_known_quote_verified": bool(_citation_spans(validation.get("previous_known"))),
        "current_information_quote_verified": bool(_citation_spans(validation.get("current_information"))),
        "change_kind_documented": change in {"new_measurement", "changed_state", "explicit_reinterpretation"},
        "reinterpretation_quote_if_needed": change != "explicit_reinterpretation" or bool(
            _citation_spans(validation.get("reinterpretation"))),
        "comparison_explanation_present": bool(str(comparison.get("explanation", "")).strip()),
        "evidence_connection_marked_supported": review.get("evidence_connection") == "supported",
        "observation_quote_verified": bool(_citation_spans(event.get("validated_observation_citations"))),
        "connection_quote_verified": bool(_citation_spans(event.get("validated_connection_citations"))),
    }
    claims = review.get("observation_availability", [])
    observations = _citation_spans(event.get("validated_observation_citations"))
    declared = {(item.get("observation", {}).get("source_id"), item.get("observation", {}).get("quote"))
                for item in claims if isinstance(item, dict) and item.get("availability") in
                {"explicitly_known_before", "record_proxy_only", "uncertain", "after_choice"}}
    if "availability_validation" in event:
        # Model quotes contain length-preserving pseudonyms; validated spans
        # refer back to raw source text. Compare source coordinates, not names.
        availability_spans = [span for item in event["availability_validation"]
            if item.get("availability") in {"explicitly_known_before", "record_proxy_only", "uncertain", "after_choice"}
            for span in _citation_spans(item.get("validated_observation"))]
        checks["every_observation_has_availability_record"] = bool(observations) and all(
            any(_overlap(quote, span) == quote["end"] - quote["start"] for span in availability_spans)
            for quote in observations)
    else:
        checks["every_observation_has_availability_record"] = bool(observations) and all(
            (quote["source_id"], quote.get("quote")) in declared for quote in observations)
    return {"event_id": _event_id(event), "complete": all(checks.values()), "checks": checks,
            "availability_status_counts": dict(Counter(item.get("availability", "missing")
                for item in claims if isinstance(item, dict))), "semantic_correctness_claimed": False}


def evaluate_layers(reference_items, run):
    """Fixed-reference, item-level metrics plus unchanged legacy raw output."""
    references = [_normalize_reference_item(item) for item in reference_items]
    cases = run.get("cases", {})
    legacy = evaluate_run(references, run)
    legacy_items = {item["reference_id"]: item for item in legacy.get("items", [])}
    verified_events, receipt_warnings = _verified_external_events(legacy)
    counts = Counter()
    items = []
    definite_assignments = {}
    initial_assignments = {}
    for case_id, case in cases.items():
        valid = [event for event in case.get("events", [])
                 if _decision(event) and _selected(event) and not _uncertain(event)]
        for category, marker, store in [
            ("new_evidence_round", "NEW_EVIDENCE", definite_assignments),
            ("initial_anchor", "FIRST", initial_assignments),
        ]:
            targets = [item for item in references if item["case_id"] == case_id
                       and item["label"] == "positive"
                       and item["checkpoint_category"] == category]
            predictions = [event for event in valid
                           if marker in event.get("checkpoint_status", "")]
            assignment = _assignment(_event_edges(predictions, targets, case.get("source_hashes")))
            store.update({targets[i]["reference_id"]: _event_id(predictions[j])
                          for i, j in assignment.items()})
    for reference in references:
        case_id, identity = reference["case_id"], reference["reference_id"]
        case = cases.get(case_id)
        discovery_available = case is not None and case.get("discovery_available", True)
        final_available = case is not None and case.get("final_available", True)
        hashes = (case or {}).get("source_hashes", {})
        discovery = _matches((case or {}).get("discovered", []) +
                             (case or {}).get("rejected_mention_traces", []), reference, hashes)
        matched = _matches((case or {}).get("events", []), reference, hashes)
        detail = {"reference_id": identity, "case_id": case_id,
                  "label": reference["label"], "checkpoint_category": reference["checkpoint_category"],
                  "reference_anchor": reference["anchor"],
                  "discovery_available": discovery_available, "final_available": final_available,
                  "discovery_matches": discovery, "final_matches": matched}
        if reference["label"] == "positive":
            for layer, available, matches in [
                ("discovery_mention", discovery_available, discovery),
                ("final_mention", final_available, matched),
            ]:
                counts[layer + ("_unknown" if not available else "_hit" if matches else "_miss")] += 1
            detail["mention_detected_in_discovery"] = bool(discovery) if discovery_available else None
            detail["mention_retained_final"] = bool(matched) if final_available else None
            counts["positive_span_with_multiple_final_events"] += len(matched) > 1
            detail["matched_program_classifications"] = dict(Counter(
                str(match["record_function"]) for match in matched))
            category = reference["checkpoint_category"]
            for expected, prefix, assignment in [
                ("new_evidence_round", "definite_new_checkpoint", definite_assignments),
                ("initial_anchor", "initial_anchor", initial_assignments),
            ]:
                if category == expected:
                    hit = identity in assignment
                    counts[prefix + ("_unknown" if not final_available else "_hit" if hit else "_miss")] += 1
                    detail[prefix + "_source_aligned_event_id"] = assignment.get(identity)
            legacy_item = legacy_items.get(identity, {})
            needed = { (value["source_id"], value["source_sha256"], value["start"], value["end"]): value
                      for value in reference.get("evidence_expectations", {}).get("needed", []) }
            raw_checks = {(value["source_id"], value["start"], value["end"]): value
                          for value in legacy_item.get("evidence_checks", []) if value["kind"] == "needed"}
            receipt = legacy_item.get("actual_material_receipt", {})
            event_id = legacy_item.get("evaluated_event_id")
            authenticated = bool(receipt.get("request_sha256") and receipt.get("cache_file_sha256")) or (
                case_id, event_id) in verified_events
            detail["required_source_span_checks"] = []
            for key, value in needed.items():
                check = raw_checks.get((key[0], key[2], key[3]), {})
                if not final_available:
                    outcome = "unknown"
                elif event_id is None:
                    outcome = "miss"  # Complete final run, target never detected.
                elif not authenticated or check.get("hit") is None:
                    outcome = "unknown"
                else:
                    outcome = "hit" if check["hit"] else "miss"
                counts["needed_" + outcome] += 1
                detail["required_source_span_checks"].append({
                    "source_id": key[0], "source_sha256": key[1], "start": key[2], "end": key[3],
                    "outcome": outcome, "evaluated_event_id": event_id,
                    "actual_request_authenticated": authenticated,
                })
        elif reference["label"] == "negative":
            primary_independent = [m for m in matched if m["primary_locator_overlap"]
                                   and m["program_reports_independent_decision"]]
            primary_cp = [m for m in matched if m["primary_locator_overlap"]
                          and m["program_selects_checkpoint"]]
            secondary = [m for m in matched if m["secondary_only_support_overlap"]]
            for prefix, selected in [("negative_primary_independent", primary_independent),
                                     ("negative_primary_checkpoint", primary_cp),
                                     ("negative_secondary_only_support", secondary)]:
                counts[prefix + ("_unknown" if not final_available else "_hit" if selected else "_miss")] += 1
            detail["negative_primary_independent_event_ids"] = [m["event_id"] for m in primary_independent]
            detail["negative_primary_checkpoint_event_ids"] = [m["event_id"] for m in primary_cp]
            detail["negative_secondary_only_support_event_ids"] = [m["event_id"] for m in secondary]
            detail["additional_independent_event_error"] = _not_measured(
                "No exhaustive event-identity annotation. Primary/secondary source-anchor diagnostics are not semantic adjudication.")
        items.append(detail)
    completeness = []
    for case_id, case in sorted(cases.items()):
        for event in case.get("events", []):
            record = uncertainty_completeness(event)
            record["case_id"] = case_id
            completeness.append(record)
    def coverage(prefix):
        return _coverage(counts[prefix + "_hit"], counts[prefix + "_miss"], counts[prefix + "_unknown"])
    structured_schema_available = any(
        any(isinstance(value, dict) and "status" in value
            for value in _dimension_records(event).values())
        for case in cases.values() for event in case.get("events", []))
    comparison_schema_available = any("evidence_comparison" in (event.get("semantic_review") or {})
        for case in cases.values() for event in case.get("events", []))
    selected_documentation = [dict(selected_new_documentation(event), case_id=case_id)
        for case_id, case in sorted(cases.items()) for event in case.get("events", [])
        if _selected(event) and "NEW_EVIDENCE" in event.get("checkpoint_status", "")]
    metrics = {
        "discovery_raw_mention_span_coverage": coverage("discovery_mention"),
        "final_retained_mention_span_coverage": coverage("final_mention"),
        "definite_new_checkpoint_source_alignment": coverage("definite_new_checkpoint"),
        "initial_anchor_source_alignment": coverage("initial_anchor"),
        "needed_source_span_actual_request_coverage": coverage("needed"),
        "negative_primary_independent_anchor_rate": coverage("negative_primary_independent"),
        "negative_primary_checkpoint_anchor_rate": coverage("negative_primary_checkpoint"),
        "negative_secondary_only_support_rate": coverage("negative_secondary_only_support"),
        "uncertainty_structured_dimension_output_completeness": _coverage(
            sum(v["all_dimensions_complete"] for v in completeness),
            sum(not v["all_dimensions_complete"] for v in completeness))
            if structured_schema_available else _not_measured(
                "This run has no v0.5 structured seven-dimension uncertainty output; legacy global flags cannot be recoded as seven-dimensional assessments."),
        "selected_new_checkpoint_documentation_completeness": _coverage(
            sum(v["complete"] for v in selected_documentation),
            sum(not v["complete"] for v in selected_documentation))
            if comparison_schema_available else _not_measured(
                "This run has no structured evidence comparison schema; no backfilled semantic claims."),
    }
    per_dimension = {}
    for dimension in DIMENSIONS:
        checks = [record["dimensions"][dimension] for record in completeness]
        per_dimension[dimension] = {
            "field_completeness": _coverage(sum(v["complete"] for v in checks),
                                             sum(not v["complete"] for v in checks))
                if structured_schema_available else _not_measured("Structured dimension schema unavailable in this run."),
            "source_quote_declared_count": sum(v.get("source_quote_declared", False) for v in checks),
            "source_quote_verified_count": sum(v.get("source_quote_verified", False) for v in checks),
        }
    unavailable = {
        "event_identity_merge_split_accuracy": "No exhaustive pairwise/event identity gold.",
        "predecessor_selection_accuracy": "Frozen reference lacks target predecessor identities.",
        "evidence_evolution_semantic_accuracy": "Source overlap is not a newness or causal-connection adjudication.",
        "needed_clinical_fact_semantic_coverage": "Reference specifies source spans, not exhaustively normalized equivalent facts.",
        "clinical_answer_leakage_rate": "Raw forbidden-span intersections need target-specific semantic audit; reconstruction is not a blind packet.",
        "uncertainty_dimension_semantic_correctness": "Field completeness does not determine whether uncertainty is warranted.",
        "whole_case_precision": "Non-exhaustive development challenge reference.",
        "whole_case_recall": "No whole-case event census.",
        "whole_case_f1": "Neither exhaustive precision nor recall available.",
        "episode_reconstruction_accuracy": "No exhaustive episode reference.",
    }
    metrics.update({name: _not_measured(reason) for name, reason in unavailable.items()})
    return {"schema_version": "p2-layered-development-evaluation-v1",
            "status": "DEVELOPMENT_DIAGNOSTICS_ONLY", "root": run.get("root"),
            "expert_gold": False, "clinical_validation": False,
            "reference_label_counts": dict(Counter(item["label"] for item in references)),
            "evaluated_cases": sorted(cases), "metrics": metrics,
            "counts": dict(counts), "items": items,
            "uncertainty_dimension_output_audit": {"all_events": completeness, "per_dimension": per_dimension,
                "semantic_correctness_claimed": False},
            "selected_new_checkpoint_documentation_audit": selected_documentation,
            "legacy_raw_unchanged": legacy,
            "warnings": receipt_warnings,
            "interpretation": {
                "mention": "Per-reference raw-span presence including validated semantic rejection traces at discovery; no scope/function filter. One event may mention several targets, so this is not a unique-event count.",
                "checkpoint": "One-to-one hash+action-span aligned candidate selection, not proof of correct novelty, predecessor or relation.",
                "negative": "Primary locator is event.anchor or first supported action citation. Secondary support alone never proves an additional independent event error.",
                "uncertainty": "Seven-dimensional output documentation completeness only. Older runs without this schema are not measured, not assigned zero accuracy.",
                "legacy": "Frozen raw metrics are embedded unchanged; new metrics have different names and must not be presented as direct improvement over old definitions.",
            }}


def load_contract(path):
    path = Path(path)
    receipt = path.with_suffix(".freeze.json")
    if not receipt.exists() or read(receipt).get("sha256") != file_sha(path):
        raise ValueError("LAYERED_METRIC_CONTRACT_HASH_MISMATCH")
    contract = read(path)
    if "FROZEN" not in str(contract.get("status", "")):
        raise ValueError("LAYERED_METRIC_CONTRACT_NOT_FROZEN")
    return contract, {"path": str(path.resolve()), "sha256": file_sha(path),
                      "freeze_receipt": str(receipt.resolve())}


def compare_reports(reports):
    """Compare semantic categories and source targets, never variable event ids."""
    names = list(reports)
    if not names:
        return {}
    before = reports[names[0]]
    result = {}
    for name in names[1:]:
        after = reports[name]
        deltas = {}
        for identity, old in before["metrics"].items():
            new = after["metrics"].get(identity, {})
            if old.get("value") is not None and new.get("value") is not None:
                deltas[identity] = {"before": old["value"], "after": new["value"],
                    "delta": new["value"] - old["value"],
                    "same_denominator": old.get("denominator") == new.get("denominator"),
                    "before_unknown": old.get("unknown"), "after_unknown": new.get("unknown")}
        def signature(item):
            selected = [match for match in item["final_matches"] if match["program_selects_checkpoint"]]
            return {
                "discovery_mention": item.get("mention_detected_in_discovery"),
                "final_mention": item.get("mention_retained_final"),
                "program_function_counts": dict(Counter(str(match["record_function"])
                    for match in item["final_matches"])),
                "selected_checkpoint_status_counts": dict(Counter(str(match["checkpoint_status"])
                    for match in selected)),
                "primary_independent_negative_flag": bool(item.get("negative_primary_independent_event_ids")),
                "primary_checkpoint_negative_flag": bool(item.get("negative_primary_checkpoint_event_ids")),
                "secondary_only_support_flag": bool(item.get("negative_secondary_only_support_event_ids")),
                "needed_source_outcomes": [{key: value.get(key) for key in
                    ("source_id", "source_sha256", "start", "end", "outcome")}
                    for value in item.get("required_source_span_checks", [])],
            }
        old_items = {item["reference_id"]: item for item in before["items"]}
        changes = []
        for item in after["items"]:
            old_item = old_items.get(item["reference_id"])
            if old_item is None:
                continue
            left, right = signature(old_item), signature(item)
            fields = [field for field in left if left[field] != right[field]]
            if fields:
                changes.append({"reference_id": item["reference_id"], "case_id": item["case_id"],
                    "changed_fields": fields, "before": left, "after": right})
        result[names[0] + "_vs_" + name] = {"metric_differences": deltas,
            "changed_reference_items": changes, "changed_reference_item_count": len(changes),
            "variable_event_id_changes_ignored": True,
            "attribution": "Joint version difference. If code and model both changed, this is not a controlled model/code effect."}
    return result


def compare(reference, contract_path, runs):
    frozen = load_reference(reference)
    contract, contract_receipt = load_contract(contract_path)
    expected = {v["sha256"] for v in contract["input_reference_manifests"]}
    actual = {v["sha256"] for v in frozen["frozen_references"]}
    if expected != actual:
        raise ValueError("LAYERED_CONTRACT_REFERENCE_MISMATCH")
    cases = {v["case_id"] for v in frozen["items"]}
    reports = {}
    for name, root in runs.items():
        loaded = load_run(root, cases)
        for case_id, case in loaded.get("cases", {}).items():
            path = Path(root) / case_id / "discovery.json"
            case["discovery_available"] = path.exists()
            rejected = []
            if path.exists():
                for unit in read(path).get("units", []):
                    for index, mention in enumerate(unit.get("rejected_mentions", [])):
                        rejected.append({"event_id": f"rejection:{unit['unit_id']}:{index}",
                            "action_citations": mention.get("validated_quotes", {}),
                            "scope": "uncertain", "record_function": "generic",
                            "mention_origin": "semantic_rejection_trace", "reason": mention.get("reason")})
            case["rejected_mention_traces"] = rejected
        reports[name] = evaluate_layers(frozen["items"], loaded)
    return {"schema_version": "p2-layered-development-comparison-v1",
            "expert_gold": False, "clinical_validation": False,
            "metric_contract_receipt": contract_receipt,
            "reference_frozen_receipts": frozen["frozen_references"], "runs": reports,
            "differences": compare_reports(reports)}


def render_markdown(report):
    lines = ["# 分层开发指标", "", "同一套新定义用于表内每个版本。旧口径原值保存在JSON的 legacy_raw_unchanged；这里没有临床准确率或完整F1。", ""]
    names = list(report["runs"])
    lines += ["| 指标 | " + " | ".join(names) + " |",
              "|---|" + "---:|" * len(names)]
    metric_names = list(next(iter(report["runs"].values()), {}).get("metrics", {}))
    for metric_name in metric_names:
        values = []
        for name in names:
            value = report["runs"][name]["metrics"][metric_name]
            if value["status"] == "not_measured":
                values.append("未测量")
            else:
                values.append(f"{value['hit']}/{value['denominator']}；未知{value['unknown']}")
        lines.append("| " + metric_name + " | " + " | ".join(values) + " |")
    lines += ["", "mention只回答原文提及是否被保留；负例primary/secondary只诊断程序锚点位置；uncertainty完整性只回答七维字段和理由是否齐全。三者均不能代替临床语义裁定。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--run", action="append", required=True,
                        help="Repeat NAME=PATH for each v0.4-format run.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    runs = {}
    for value in args.run:
        if "=" not in value:
            parser.error("--run requires NAME=PATH")
        name, root = value.split("=", 1)
        if not name or name in runs:
            parser.error("empty or duplicate run name")
        runs[name] = root
    report = compare(args.reference, args.contract, runs)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "evaluation_layers.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (output / "evaluation_layers.md").write_text(render_markdown(report))
    print(json.dumps({"status": "DEVELOPMENT_DIAGNOSTICS_ONLY", "output": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

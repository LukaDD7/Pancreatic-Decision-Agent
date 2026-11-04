"""Fixed-denominator scorecard layered over the frozen raw evaluator.

This explicitly preserves the raw evaluator's conditional/available denominators
and separately applies the preregistered development contract. Unknown material
is not recoded as a clinical negative; coverage bounds retain it in the fixed
denominator. No model or production reconstruction code reads this scorecard.
"""

from collections import Counter
import argparse
import hashlib
import json
from pathlib import Path

from .evaluate import _forbidden_kind, _normalize_reference_item


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def load_contract(path):
    path = Path(path)
    contract = read(path)
    freeze_path = path.with_suffix(".freeze.json")
    if "FROZEN" not in contract.get("status", "") or not freeze_path.exists():
        raise ValueError("FROZEN_CONTRACT_RECEIPT_REQUIRED")
    if read(freeze_path).get("sha256") != sha(path):
        raise ValueError("CONTRACT_FROZEN_HASH_MISMATCH")
    return contract, {"path": str(path.resolve()), "sha256": sha(path),
                      "freeze_receipt": str(freeze_path.resolve())}


def reference_profile(contract):
    counts = Counter()
    for manifest in contract.get("input_reference_manifests", []):
        path = Path(manifest["path"])
        if sha(path) != manifest["sha256"]:
            raise ValueError("CONTRACT_REFERENCE_HASH_MISMATCH:" + str(path))
        for raw in read(path)["items"]:
            item = _normalize_reference_item(raw)
            counts["checkpoint_" + item["checkpoint_category"]] += 1
            if item["label"] != "positive":
                continue
            seen = set()
            evidence = item.get("evidence_expectations", {})
            for role in ["needed", "forbidden", "current_action_to_mask"]:
                for value in evidence.get(role, []):
                    key = (role, value["source_id"], value["source_sha256"], value["start"], value["end"])
                    if key in seen:
                        continue
                    seen.add(key)
                    counts["needed" if role == "needed" else _forbidden_kind(value)] += 1
    return dict(counts)


def _target(contract, identity):
    return next(metric for metric in contract["metrics"] if metric["id"] == identity)


def _goal(lower, upper, target):
    threshold, operator = target["threshold"], target["operator"]
    if operator == ">=":
        return "pass" if lower >= threshold else "fail" if upper < threshold else "not_demonstrated"
    if operator == "<=":
        return "pass" if upper <= threshold else "fail" if lower > threshold else "not_demonstrated"
    if operator == "==":
        return "pass" if lower == upper == threshold else "fail" if lower > threshold or upper < threshold else "not_demonstrated"
    raise ValueError("UNSUPPORTED_TARGET_OPERATOR:" + operator)


def fixed_coverage(hits, measured_total, fixed_total, target=None, *, unknown_extra=0):
    if min(hits, measured_total, fixed_total, unknown_extra) < 0 or hits > measured_total or measured_total > fixed_total:
        raise ValueError("INVALID_FIXED_DENOMINATOR_COUNTS")
    # A previously reported hit lacking its required request receipt becomes
    # unknown, never a known miss. measured_total is reduced correspondingly.
    if unknown_extra > hits:
        raise ValueError("MORE_UNVERIFIED_HITS_THAN_HITS")
    verified_hits = hits - unknown_extra
    known_miss = measured_total - hits
    unknown = fixed_total - verified_hits - known_miss
    if not fixed_total:
        return {"status": "not_measured", "reason": "no fixed denominator"}
    lower, upper = verified_hits / fixed_total, (verified_hits + unknown) / fixed_total
    result = {"status": "measured" if not unknown else "partially_measured",
              "fixed_denominator": fixed_total, "hit": verified_hits,
              "known_miss": known_miss, "unknown": unknown,
              "coverage_lower_bound": lower, "coverage_upper_bound": upper,
              "conservative_coverage": lower, "unknown_is_not_negative": True}
    if target is not None:
        result["target"] = {"operator": target["operator"], "threshold": target["threshold"]}
        result["target_status"] = _goal(lower, upper, target)
    return result


def _metric_counts(run, name):
    value = run.get("metrics", {}).get(name, {})
    return value.get("numerator", 0), value.get("denominator", 0)


def _verified_external_events(run):
    """Use only hash-checked output files already cited by the raw report."""
    verified, warnings = set(), []
    for case_id, inputs in run.get("input_file_receipts", {}).items():
        event_file = next((Path(path) for path in inputs if Path(path).name == "decision_events.json"), None)
        receipt_file = next((Path(path) for path in inputs if Path(path).name == "model_receipts.json"), None)
        if event_file is None or receipt_file is None:
            continue
        for path in [event_file, receipt_file]:
            if not path.exists() or sha(path) != inputs[str(path)]:
                warnings.append("CHANGED_OR_MISSING_RUN_RECEIPT:" + str(path))
                break
        else:
            valid = {receipt["request_sha256"] for receipt in read(receipt_file)
                     if receipt.get("status") == "SUCCESS"
                     and receipt.get("task") == "document_checkpoint_review"}
            for event in read(event_file):
                if event.get("review_request_sha256") in valid:
                    verified.add((case_id, event["event_id"]))
    return verified, warnings


def _has_request_receipt(item, verified):
    receipt = item.get("actual_material_receipt", {})
    return bool(receipt.get("request_sha256") and receipt.get("cache_file_sha256")) or (
        item["case_id"], item.get("evaluated_event_id")) in verified


def score_run(run, contract, profile, *, verified_events=None):
    if run.get("status") == "not_measured":
        return {"status": "not_measured", "reason": "run unavailable; no thresholds claimed"}
    verified_events = verified_events or set()
    targets = {}
    discovery = _target(contract, "source_challenge_decision_discovery_coverage")
    hits, observed = _metric_counts(run, "discovery_positive_coverage")
    targets[discovery["id"]] = fixed_coverage(hits, observed, discovery["denominator_count"], discovery)
    new = _target(contract, "definite_new_evidence_checkpoint_coverage")
    hits, observed = _metric_counts(run, "new_evidence_round_correctly_selected")
    targets[new["id"]] = fixed_coverage(hits, observed, new["denominator_count"], new)

    negative = _target(contract, "labelled_negative_independent_selection_rate")
    negative_total = negative["denominator_count"]
    cp_errors, observed_negative = _metric_counts(run, "labelled_negative_checkpoint_false_positive_rate")
    decision_errors, decision_observed = _metric_counts(run, "labelled_negative_independent_decision_rate")
    if observed_negative != decision_observed or observed_negative > negative_total:
        raise ValueError("NEGATIVE_DENOMINATOR_CONFLICT")
    unknown_negative = negative_total - observed_negative
    # The raw evaluator has no item-level independent-decision flag. Respect
    # this limitation instead of inventing the overlap of the two error sets.
    error_lower = max(cp_errors, decision_errors)
    error_upper = min(observed_negative, cp_errors + decision_errors) + unknown_negative
    lower, upper = error_lower / negative_total, error_upper / negative_total
    targets[negative["id"]] = {
        "status": "measured" if error_lower == error_upper else "partially_measured",
        "fixed_denominator": negative_total, "known_checkpoint_errors": cp_errors,
        "known_independent_decision_errors": decision_errors, "unknown_units": unknown_negative,
        "combined_error_count_lower_bound": error_lower, "combined_error_count_upper_bound": error_upper,
        "error_rate_lower_bound": lower, "error_rate_upper_bound": upper,
        "target": {"operator": negative["operator"], "threshold": negative["threshold"]},
        "target_status": _goal(lower, upper, negative),
        "union_limitation": "Raw report does not encode both error sets item by item; exact union is not fabricated.",
    }

    required = _target(contract, "required_evidence_actual_input_coverage")
    needed_total = required["denominator_count"]
    hits, observed = _metric_counts(run, "needed_material_end_to_end_coverage")
    unverified_hits = 0
    for item in run.get("items", []):
        if not _has_request_receipt(item, verified_events):
            unverified_hits += sum(check.get("kind") == "needed" and check.get("hit") is True
                                   for check in item.get("evidence_checks", []))
    # If old reports omit item checks, receipt verification is unimplemented;
    # never certify a count of hits solely from the aggregate metric.
    counted_hits = sum(check.get("kind") == "needed" and check.get("hit") is True
                       for item in run.get("items", []) for check in item.get("evidence_checks", []))
    unverified_hits += max(0, hits - counted_hits)
    targets[required["id"]] = fixed_coverage(hits, observed, needed_total, required, unknown_extra=unverified_hits)
    targets[required["id"]]["unverified_request_receipt_hits"] = unverified_hits
    targets[required["id"]]["raw_conditional_coverage"] = run.get("metrics", {}).get("needed_actual_material_coverage_conditional")

    prohibited = _target(contract, "prohibited_material_as_prior_fact")
    counts = run.get("counts", {})
    categories = {
        "hard_forbidden_in_actual_evidence": counts.get("forbidden_actual_hit", 0),
        "historical_baseline_claimed_new": counts.get("historical_new_claim_hit", 0),
        "unconfirmed_availability_used_as_observation": counts.get("uncertain_availability_observed_as_fact", 0),
    }
    observed_controls = (counts.get("forbidden_actual_total", 0) + counts.get("historical_new_claim_total", 0)
                         + counts.get("uncertain_availability_context_total", 0))
    fixed_controls = sum(profile.get(key, 0) for key in ["hard_forbidden", "historical_not_new", "availability_uncertain"])
    unknown_controls = max(0, fixed_controls - observed_controls)
    violations = sum(categories.values())
    target_status = ("fail" if violations else "pass" if observed_controls and not unknown_controls
                     else "not_demonstrated")
    targets[prohibited["id"]] = {
        "status": "measured" if observed_controls and not unknown_controls else "partially_measured" if observed_controls else "not_measured",
        "known_violation_count": violations, "categories": categories,
        "fixed_reference_controls": fixed_controls, "measured_controls": observed_controls,
        "unknown_or_not_applicable_controls": unknown_controls,
        "reference_boundary_conflicts": counts.get("reference_needed_forbidden_overlap", 0),
        "target_status": target_status, "target": {"operator": "==", "threshold": 0},
        "blind_packet_metric": run.get("metrics", {}).get("forbidden_blind_packet_rate"),
        "scope_note": "Material checks are source-span controls, not a semantic clinical correctness score.",
    }
    uncertainty = _target(contract, "uncertainty_dimension_reason_preservation")
    targets[uncertainty["id"]] = {
        "status": "not_measured", "target_status": "not_demonstrated",
        "fixed_reference_units": uncertainty["denominator_count_explicit_reference_units"],
        "reason": "Raw global abstention does not verify every uncertainty dimension and a source-bound reason category.",
        "secondary_global_abstain": run.get("metrics", {}).get("checkpoint_uncertain_abstain_rate"),
    }

    initial_hits, initial_observed = _metric_counts(run, "initial_anchor_correctly_selected")
    initial = fixed_coverage(initial_hits, initial_observed, profile.get("checkpoint_initial_anchor", 6))
    diagnostic_items = [item for item in run.get("items", []) if item.get("checkpoint_category") == "anchor_or_new_diagnostic_choice"]
    diagnostic = fixed_coverage(sum(item.get("checkpoint_hit") is True for item in diagnostic_items),
                                len(diagnostic_items), profile.get("checkpoint_anchor_or_new_diagnostic_choice", 1))
    statuses = [value["target_status"] for value in targets.values()]
    return {"status": "development_target_not_met" if "fail" in statuses else
                      "development_targets_demonstrated" if all(status == "pass" for status in statuses) else "not_demonstrated",
            "evaluated_cases": run.get("evaluated_cases", []),
            "missing_reference_cases": sorted(set(contract["reference_cases"]) - set(run.get("evaluated_cases", []))),
            "targets": targets, "initial_anchors_separate": initial,
            "diagnostic_anchor_or_new_separate": diagnostic,
            "whole_case_precision": {"status": "not_measured", "reason": "non-exhaustive source challenge reference"},
            "clinical_validation": False}


def build_scorecard(evaluation, contract, profile, *, verified_events=None):
    expected_hashes = {item["sha256"] for item in contract.get("input_reference_manifests", [])}
    actual_hashes = {item["sha256"] for item in evaluation.get("reference_frozen_receipts", [])}
    if expected_hashes and expected_hashes != actual_hashes:
        raise ValueError("EVALUATION_CONTRACT_REFERENCE_HASH_MISMATCH")
    result = {"schema_version": "fixed-denominator-contract-scorecard-v1", "expert_gold": False,
              "clinical_validation": False, "reference_profile": profile,
              "protocol_difference": "Raw evaluator retains available/conditional denominators; this separate scorecard keeps fixed totals and reports verified hits, known misses and unknowns without changing raw metrics."}
    warnings = []
    for name in ["baseline", "current"]:
        run = evaluation[name]
        derived, messages = _verified_external_events(run) if verified_events is None else (verified_events.get(name, set()), [])
        result[name] = score_run(run, contract, profile, verified_events=derived)
        warnings.extend(messages)
    result["warnings"] = warnings
    return result


def markdown(report):
    lines = ["# 预注册目标固定分母记分表", "",
             "独立保留原始评价公式；本表固定全部参考单位，未知不改为阴性，也不因缺失缩小分母。仅为AI原文开发挑战集。", "",
             "| 目标 | v0.3 | v0.4 |", "|---|---|---|"]
    identities = list(report.get("baseline", {}).get("targets", {})) or list(report.get("current", {}).get("targets", {}))
    for identity in identities:
        values = []
        for name in ["baseline", "current"]:
            value = report[name].get("targets", {}).get(identity, {})
            if "fixed_denominator" in value and "hit" in value:
                values.append(f"{value['hit']}/{value['fixed_denominator']}；明确miss {value['known_miss']}，unknown {value['unknown']}；{value['target_status']}")
            else:
                values.append(value.get("target_status", "not_measured"))
        lines.append(f"| {identity} | {values[0]} | {values[1]} |")
    lines.extend(["", "初始锚点6项、初始/新轮不定的诊断选择1项另报；不纳入确定新CP的30项分母。",
                  "uncertainty维度及理由完整率尚未实现，不能用全局abstain率替代。总体precision/F1与临床验证均未测量。", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    contract, receipt = load_contract(args.contract)
    report = build_scorecard(read(args.evaluation), contract, reference_profile(contract))
    report.update(contract_receipt=receipt, raw_evaluation_receipt={"path": str(Path(args.evaluation).resolve()), "sha256": sha(args.evaluation)})
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "contract_scorecard.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (output / "contract_scorecard.md").write_text(markdown(report))
    print(json.dumps({"status": "SCORECARD_WRITTEN", "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

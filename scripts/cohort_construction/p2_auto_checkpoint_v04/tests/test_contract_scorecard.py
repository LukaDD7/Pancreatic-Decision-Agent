"""Fictional fixed-denominator and uncertainty accounting controls."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.contract_scorecard import (
    _verified_external_events, build_scorecard, fixed_coverage, load_contract, score_run, sha,
)


def contract():
    def target(identity, denominator, threshold, operator=">="):
        return {"id": identity, "denominator_count": denominator, "threshold": threshold, "operator": operator}
    return {"reference_cases": ["fictional"], "metrics": [
        target("source_challenge_decision_discovery_coverage", 40, .9),
        target("definite_new_evidence_checkpoint_coverage", 30, .8),
        target("labelled_negative_independent_selection_rate", 29, .1, "<="),
        target("required_evidence_actual_input_coverage", 53, .95),
        target("prohibited_material_as_prior_fact", 66, 0, "=="),
        {"id": "uncertainty_dimension_reason_preservation", "denominator_count_explicit_reference_units": 17,
         "threshold": 1, "operator": "=="},
    ]}


def metric(numerator, denominator):
    return {"status": "measured", "numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def raw_run(**metrics):
    defaults = {
        "discovery_positive_coverage": metric(27, 40),
        "new_evidence_round_correctly_selected": metric(11, 30),
        "labelled_negative_checkpoint_false_positive_rate": metric(0, 29),
        "labelled_negative_independent_decision_rate": metric(4, 29),
        "needed_material_end_to_end_coverage": metric(32, 51),
        "initial_anchor_correctly_selected": metric(2, 6),
    }
    defaults.update(metrics)
    return {"status": "development_challenge_set_only", "evaluated_cases": ["fictional"],
            "metrics": defaults,
            "counts": {"forbidden_actual_hit": 7, "forbidden_actual_total": 39},
            "items": [{"case_id": "fictional", "evaluated_event_id": "E1",
                       "actual_material_receipt": {"request_sha256": "request", "cache_file_sha256": "cache"},
                       "evidence_checks": [{"kind": "needed", "hit": True}] * 32}]}


PROFILE = {"hard_forbidden": 63, "historical_not_new": 1, "availability_uncertain": 2,
           "checkpoint_initial_anchor": 6, "checkpoint_anchor_or_new_diagnostic_choice": 1}


class FixedScorecardTests(unittest.TestCase):
    def test_SC001_unknown_material_keeps_total_53_without_becoming_known_miss(self):
        result = fixed_coverage(32, 51, 53, {"threshold": .95, "operator": ">="})
        self.assertEqual((result["hit"], result["known_miss"], result["unknown"]), (32, 19, 2))
        self.assertEqual(result["coverage_lower_bound"], 32 / 53)
        self.assertEqual(result["coverage_upper_bound"], 34 / 53)
        self.assertEqual(result["target_status"], "fail")

    def test_SC002_partial_discovery_does_not_shrink_fixed_denominator(self):
        result = fixed_coverage(35, 35, 40, {"threshold": .9, "operator": ">="})
        self.assertEqual(result["fixed_denominator"], 40)
        self.assertEqual(result["known_miss"], 0)
        self.assertEqual(result["unknown"], 5)
        self.assertEqual(result["target_status"], "not_demonstrated")

    def test_SC003_conservative_lower_bound_can_demonstrate_threshold(self):
        result = fixed_coverage(51, 51, 53, {"threshold": .95, "operator": ">="})
        self.assertEqual(result["target_status"], "pass")
        self.assertEqual(result["unknown"], 2)

    def test_SC004_receiptless_claimed_hit_becomes_unknown_not_failure(self):
        run = raw_run()
        run["items"][0]["actual_material_receipt"] = {}
        result = score_run(run, contract(), PROFILE)
        target = result["targets"]["required_evidence_actual_input_coverage"]
        self.assertEqual((target["hit"], target["known_miss"], target["unknown"]), (0, 19, 34))
        self.assertEqual(target["unverified_request_receipt_hits"], 32)

    def test_SC004_external_success_receipt_can_authenticate_current_input(self):
        run = raw_run()
        run["items"][0]["actual_material_receipt"] = {}
        result = score_run(run, contract(), PROFILE, verified_events={("fictional", "E1")})
        self.assertEqual(result["targets"]["required_evidence_actual_input_coverage"]["hit"], 32)

    def test_SC005_negative_error_union_not_fabricated(self):
        run = raw_run(labelled_negative_checkpoint_false_positive_rate=metric(1, 29),
                      labelled_negative_independent_decision_rate=metric(2, 29))
        result = score_run(run, contract(), PROFILE)["targets"]["labelled_negative_independent_selection_rate"]
        self.assertEqual(result["combined_error_count_lower_bound"], 2)
        self.assertEqual(result["combined_error_count_upper_bound"], 3)
        self.assertEqual(result["target_status"], "not_demonstrated")

    def test_SC005_known_four_negative_errors_fail_even_if_other_metrics_unknown(self):
        result = score_run(raw_run(), contract(), PROFILE)["targets"]["labelled_negative_independent_selection_rate"]
        self.assertEqual(result["target_status"], "fail")
        self.assertEqual(result["error_rate_lower_bound"], 4 / 29)

    def test_SC006_abstain_rate_does_not_prove_dimension_reason_preservation(self):
        run = raw_run(checkpoint_uncertain_abstain_rate=metric(17, 17))
        result = score_run(run, contract(), PROFILE)["targets"]["uncertainty_dimension_reason_preservation"]
        self.assertEqual(result["status"], "not_measured")
        self.assertEqual(result["target_status"], "not_demonstrated")

    def test_SC007_no_tested_prohibited_controls_cannot_pass_zero_target(self):
        run = raw_run()
        run["counts"] = {}
        result = score_run(run, contract(), PROFILE)["targets"]["prohibited_material_as_prior_fact"]
        self.assertEqual(result["known_violation_count"], 0)
        self.assertEqual(result["target_status"], "not_demonstrated")

    def test_SC007_known_prohibited_fact_fails_zero_target(self):
        result = score_run(raw_run(), contract(), PROFILE)["targets"]["prohibited_material_as_prior_fact"]
        self.assertEqual(result["known_violation_count"], 7)
        self.assertEqual(result["target_status"], "fail")

    def test_SC008_initial_6_and_diagnostic_1_are_not_newcp_30(self):
        run = raw_run()
        run["items"].append({"case_id": "fictional", "checkpoint_category": "anchor_or_new_diagnostic_choice", "checkpoint_hit": True})
        result = score_run(run, contract(), PROFILE)
        self.assertEqual(result["targets"]["definite_new_evidence_checkpoint_coverage"]["fixed_denominator"], 30)
        self.assertEqual(result["initial_anchors_separate"]["fixed_denominator"], 6)
        self.assertEqual(result["diagnostic_anchor_or_new_separate"]["fixed_denominator"], 1)

    def test_SC009_contract_reference_hash_mismatch_is_rejected(self):
        c = contract()
        c["input_reference_manifests"] = [{"sha256": "one"}]
        raw = {"baseline": raw_run(), "current": {"status": "not_measured"},
               "reference_frozen_receipts": [{"sha256": "two"}]}
        with self.assertRaisesRegex(ValueError, "REFERENCE_HASH_MISMATCH"):
            build_scorecard(raw, c, PROFILE)

    def test_SC010_frozen_contract_hash_detects_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "metric_contract.json"
            p.write_text(json.dumps({**contract(), "status": "FROZEN"}))
            p.with_suffix(".freeze.json").write_text(json.dumps({"sha256": sha(p)}))
            load_contract(p)
            p.write_text(p.read_text() + " ")
            with self.assertRaisesRegex(ValueError, "CONTRACT_FROZEN_HASH_MISMATCH"):
                load_contract(p)

    def test_SC011_scorecard_does_not_mutate_raw_evaluation_or_contract(self):
        raw = {"baseline": raw_run(), "current": {"status": "not_measured"}}
        c = contract()
        before = deepcopy((raw, c))
        result = build_scorecard(raw, c, PROFILE, verified_events={})
        self.assertEqual((raw, c), before)
        self.assertEqual(result["current"]["status"], "not_measured")

    def test_SC012_changed_artifact_receipt_not_accepted_as_actual_model_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            e, r = root / "decision_events.json", root / "model_receipts.json"
            e.write_text(json.dumps([{"event_id": "E1", "review_request_sha256": "req"}]))
            r.write_text(json.dumps([{"task": "document_checkpoint_review", "status": "SUCCESS", "request_sha256": "req"}]))
            run = {"input_file_receipts": {"fictional": {str(e): sha(e), str(r): sha(r)}}}
            verified, warnings = _verified_external_events(run)
            self.assertEqual(verified, {("fictional", "E1")})
            self.assertEqual(warnings, [])
            e.write_text(e.read_text() + " ")
            verified, warnings = _verified_external_events(run)
            self.assertEqual(verified, set())
            self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()

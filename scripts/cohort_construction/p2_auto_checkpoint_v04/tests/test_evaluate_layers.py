"""Counterexamples for mention/event/checkpoint metric separation."""

from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.evaluate_layers import (
    DIMENSIONS, compare_reports, evaluate_layers, selected_new_documentation, uncertainty_completeness,
)
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_evaluate import (
    event, ref, run, span,
)


class LayeredEvaluationTests(unittest.TestCase):
    def test_L001_repeated_record_retains_mention_but_not_independent_decision(self):
        repeated = event(record_function="repeat", checkpoint_status="REPEATED_SAME_EVENT")
        result = evaluate_layers([ref()], run([repeated]))
        self.assertEqual(result["metrics"]["final_retained_mention_span_coverage"]["hit"], 1)
        self.assertEqual(result["legacy_raw_unchanged"]["metrics"]["independent_decision_positive_coverage"]["numerator"], 0)
        self.assertFalse(result["items"][0]["final_matches"][0]["program_reports_independent_decision"])

    def test_L002_scope_no_is_still_a_detected_mention(self):
        result = evaluate_layers([ref()], run([event(scope="no")]))
        self.assertEqual(result["metrics"]["discovery_raw_mention_span_coverage"]["hit"], 1)

    def test_L003_source_hash_mismatch_cannot_align(self):
        candidate = event()
        candidate["action_citations"]["valid"][0]["source_sha256"] = "0" * 64
        result = evaluate_layers([ref()], run([candidate]))
        self.assertEqual(result["metrics"]["final_retained_mention_span_coverage"]["hit"], 0)

    def test_L004_secondary_table_support_does_not_become_primary_extra_event(self):
        negative = ref("N1", "negative", start=30, end=40)
        candidate = event(anchor=span(10, 20))
        candidate["action_citations"]["valid"].append(span(30, 40))
        result = evaluate_layers([ref(), negative], run([candidate]))
        self.assertEqual(result["metrics"]["negative_primary_independent_anchor_rate"]["hit"], 0)
        self.assertEqual(result["metrics"]["negative_secondary_only_support_rate"]["hit"], 1)
        self.assertEqual(result["legacy_raw_unchanged"]["metrics"]["labelled_negative_independent_decision_rate"]["numerator"], 1)
        self.assertEqual(result["items"][1]["additional_independent_event_error"]["status"], "not_measured")

    def test_L005_primary_negative_is_diagnostic_not_semantic_gold(self):
        result = evaluate_layers([ref("N1", "negative")], run([event()]))
        self.assertEqual(result["metrics"]["negative_primary_independent_anchor_rate"]["hit"], 1)
        self.assertEqual(result["items"][0]["additional_independent_event_error"]["status"], "not_measured")

    def test_L006_one_large_event_mentions_two_targets_but_newcp_assignment_one(self):
        refs = [ref("P1", start=10, end=20), ref("P2", start=30, end=40)]
        result = evaluate_layers(refs, run([event(start=0, end=50)]))
        self.assertEqual(result["metrics"]["final_retained_mention_span_coverage"]["hit"], 2)
        self.assertEqual(result["metrics"]["definite_new_checkpoint_source_alignment"]["hit"], 1)
        self.assertEqual(result["metrics"]["event_identity_merge_split_accuracy"]["status"], "not_measured")

    def test_L007_missing_case_keeps_fixed_denominator_unknown(self):
        missing = ref("P2", case_id="missing")
        result = evaluate_layers([ref(), missing], run([event()]))
        measure = result["metrics"]["final_retained_mention_span_coverage"]
        self.assertEqual((measure["hit"], measure["known_miss"], measure["unknown"], measure["denominator"]), (1, 0, 1, 2))

    def test_L008_discovery_only_does_not_count_final_failure(self):
        result = evaluate_layers([ref()], run([], discovered=[event()], final_available=False))
        self.assertEqual(result["metrics"]["discovery_raw_mention_span_coverage"]["hit"], 1)
        self.assertEqual(result["metrics"]["final_retained_mention_span_coverage"]["unknown"], 1)

    def test_L009_legacy_uncertainty_schema_is_not_measured(self):
        result = evaluate_layers([ref()], run([event(uncertainty_dimensions={"event_identity": "new_decision"})]))
        self.assertEqual(result["metrics"]["uncertainty_structured_dimension_output_completeness"]["status"], "not_measured")

    def test_L010_all_structured_dimensions_complete_without_accuracy_claim(self):
        dimensions = {name: {"status": "certain", "reason_category": "no_uncertainty",
                              "explanation": "", "source_quotes": []} for name in DIMENSIONS}
        dimensions["choice_time"] = {"status": "uncertain", "reason_category": "genuine_missing_information",
                                     "explanation": "Original choice time is not recorded.", "source_quotes": []}
        candidate = event(uncertainty_dimensions=dimensions)
        result = evaluate_layers([ref()], run([candidate]))
        self.assertEqual(result["metrics"]["uncertainty_structured_dimension_output_completeness"]["hit"], 1)
        self.assertEqual(result["metrics"]["uncertainty_dimension_semantic_correctness"]["status"], "not_measured")

    def test_L011_uncertain_dimension_without_explanation_is_missing_documentation(self):
        dimensions = {name: {"status": "uncertain", "reason_category": "model_conflict",
                              "explanation": "", "source_quotes": []} for name in DIMENSIONS}
        result = uncertainty_completeness(event(uncertainty_dimensions=dimensions))
        self.assertFalse(result["all_dimensions_complete"])

    def test_L012_raw_quote_presence_is_not_verified_source_binding(self):
        candidate = event(uncertainty_dimensions={"scope": {
            "status": "uncertain", "reason_category": "model_conflict", "explanation": "Unclear scope.",
            "source_quotes": [{"source_id": "D1", "quote": "XXXXX"}]}})
        result = uncertainty_completeness(candidate)
        self.assertTrue(result["dimensions"]["scope"]["source_quote_declared"])
        self.assertFalse(result["dimensions"]["scope"]["source_quote_verified"])

    def test_L013_validated_rejection_detected_but_not_retained_event(self):
        rejection = event(mention_origin="semantic_rejection_trace", scope="uncertain", record_function="generic")
        result = evaluate_layers([ref()], run([], discovered=[], rejected_mention_traces=[rejection]))
        self.assertEqual(result["metrics"]["discovery_raw_mention_span_coverage"]["hit"], 1)
        self.assertEqual(result["metrics"]["final_retained_mention_span_coverage"]["hit"], 0)
        self.assertEqual(result["items"][0]["discovery_matches"][0]["mention_origin"], "semantic_rejection_trace")

    def test_L014_needed_registered_without_actual_request_receipt_is_unknown(self):
        reference = ref(evidence_expectations={"needed": [span(0, 8)]})
        result = evaluate_layers([reference], run([event()], contexts={"E1": {"blocks": [span(0, 8)]}}))
        self.assertEqual(result["metrics"]["needed_source_span_actual_request_coverage"]["unknown"], 1)

    def test_L015_needed_missing_target_is_end_to_end_miss(self):
        reference = ref(evidence_expectations={"needed": [span(0, 8)]})
        result = evaluate_layers([reference], run([]))
        self.assertEqual(result["metrics"]["needed_source_span_actual_request_coverage"]["known_miss"], 1)

    def test_L016_unlabelled_output_does_not_create_precision_or_false_positive(self):
        result = evaluate_layers([ref()], run([event(), event("unlabelled", 80, 90)]))
        self.assertEqual(result["metrics"]["whole_case_precision"]["status"], "not_measured")
        self.assertEqual(result["metrics"]["whole_case_f1"]["status"], "not_measured")

    def _documented_new(self):
        return event(previous_event_id="prior", provided_prior_event_ids=["prior"],
            semantic_review={"evidence_connection": "supported", "evidence_comparison": {
                "change_kind": "changed_state", "explanation": "Different current patient state."},
                "observation_availability": [{"observation": {"source_id": "D1", "quote": "XXXXXXXX"},
                                                "availability": "record_proxy_only"}]},
            comparison_validation={"previous_known": {"valid": [span(0, 4)]},
                                   "current_information": {"valid": [span(0, 8)]}},
            validated_observation_citations={"valid": [span(0, 8)]},
            validated_connection_citations={"valid": [span(0, 20)]})

    def test_L017_new_checkpoint_documentation_is_structural_not_semantic_accuracy(self):
        candidate = self._documented_new()
        check = selected_new_documentation(candidate)
        self.assertTrue(check["complete"])
        self.assertFalse(check["semantic_correctness_claimed"])

    def test_L018_reinterpretation_requires_own_supported_quote(self):
        candidate = self._documented_new()
        candidate["semantic_review"]["evidence_comparison"]["change_kind"] = "explicit_reinterpretation"
        check = selected_new_documentation(candidate)
        self.assertFalse(check["complete"])
        self.assertFalse(check["checks"]["reinterpretation_quote_if_needed"])

    def test_L019_unsupported_predecessor_and_missing_availability_not_complete(self):
        candidate = self._documented_new()
        candidate["provided_prior_event_ids"] = []
        candidate["semantic_review"]["observation_availability"] = []
        check = selected_new_documentation(candidate)
        self.assertFalse(check["complete"])
        self.assertFalse(check["checks"]["previous_event_provided"])
        self.assertFalse(check["checks"]["every_observation_has_availability_record"])

    def test_L020_id_only_change_is_not_reconstruction_difference(self):
        before = evaluate_layers([ref()], run([event("old-id")]))
        after = evaluate_layers([ref()], run([event("new-id")]))
        delta = compare_reports({"old": before, "new": after})["old_vs_new"]
        self.assertEqual(delta["changed_reference_item_count"], 0)

    def test_L021_function_change_preserves_mention_but_changes_classification(self):
        before = evaluate_layers([ref()], run([event()]))
        after = evaluate_layers([ref()], run([event(record_function="repeat", checkpoint_status="REPEATED_SAME_EVENT")]))
        delta = compare_reports({"old": before, "new": after})["old_vs_new"]
        self.assertEqual(delta["metric_differences"]["final_retained_mention_span_coverage"]["delta"], 0)
        self.assertIn("program_function_counts", delta["changed_reference_items"][0]["changed_fields"])

    def test_L022_masked_availability_quote_uses_raw_verified_span_mapping(self):
        candidate = self._documented_new()
        candidate["semantic_review"]["observation_availability"][0]["observation"]["quote"] = "MASKED!"
        candidate["availability_validation"] = [{"availability": "record_proxy_only",
            "validated_observation": {"valid": [span(0, 8)]}}]
        self.assertTrue(selected_new_documentation(candidate)["complete"])


if __name__ == "__main__":
    unittest.main()

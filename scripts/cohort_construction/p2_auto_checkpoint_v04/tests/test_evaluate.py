"""Fictional development-evaluation controls, not patient or expert labels."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.evaluate import (
    _cached_actual_contexts, _digest, evaluate_run, file_sha, load_reference, metric,
)


HASH = hashlib.sha256(b"fictional source").hexdigest()


def span(start=10, end=20, source_id="D1", **extra):
    return {"source_id": source_id, "source_sha256": HASH, "start": start,
            "end": end, "quote": "X" * (end - start), **extra}


def ref(identity="P1", label="positive", start=10, end=20, **extra):
    return {"reference_id": identity, "case_id": "fictional", "label": label,
            "anchor": span(start, end), "checkpoint_eligible": "yes" if label == "positive" else label,
            **extra}


def event(identity="E1", start=10, end=20, **extra):
    return {"event_id": identity, "source_id": "D1", "scope": "yes", "record_function": "new_decision",
            "action_citations": {"valid": [span(start, end)], "ambiguous": [], "invalid": []},
            "checkpoint_status": "CANDIDATE_NEW_EVIDENCE", **extra}


def run(events, *, contexts=None, windows=None, discovered=None, **case_extra):
    return {"status": "available", "cases": {"fictional": {
        "events": events, "discovered": events if discovered is None else discovered,
        "contexts": contexts or {}, "windows": windows or {}, "raw": [], "final_available": True,
        **case_extra}}}


class DevelopmentMetricTests(unittest.TestCase):
    def test_E001_scope_no_cannot_count_as_positive_detection(self):
        report = evaluate_run([ref()], run([event(scope="no")]))
        self.assertEqual(report["metrics"]["independent_decision_positive_coverage"]["numerator"], 0)
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["numerator"], 0)
        self.assertEqual(report["counts"]["selected_scope_or_function_inconsistent"], 1)

    def test_E002_decision_existence_and_checkpoint_selection_are_separate(self):
        report = evaluate_run([ref()], run([event(checkpoint_status="UNCERTAIN_EVIDENCE_EVOLUTION")]))
        self.assertEqual(report["metrics"]["independent_decision_positive_coverage"]["value"], 1)
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["value"], 0)

    def test_E003_positive_decision_with_uncertain_cp_not_in_positive_cp_denominator(self):
        reference = ref(checkpoint_expectation="uncertain_new_evidence_round")
        report = evaluate_run([reference], run([event(checkpoint_status="UNCERTAIN_EVIDENCE_EVOLUTION")]))
        self.assertEqual(report["metrics"]["independent_decision_positive_coverage"]["value"], 1)
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["status"], "not_measured")
        self.assertEqual(report["metrics"]["checkpoint_uncertain_abstain_rate"]["value"], 1)

    def test_E004_initial_anchor_and_new_round_subtypes_separate(self):
        reference = ref(checkpoint_expectation="initial_process_anchor")
        report = evaluate_run([reference], run([event(checkpoint_status="CANDIDATE_FIRST_ANCHOR")]))
        self.assertEqual(report["metrics"]["initial_anchor_correctly_selected"]["value"], 1)
        self.assertEqual(report["metrics"]["new_evidence_round_correctly_selected"]["status"], "not_measured")

    def test_E005_non_exhaustive_reference_does_not_create_precision(self):
        report = evaluate_run([ref()], run([event(), event("unassessed", 80, 90)]))
        self.assertEqual(report["metrics"]["precision"]["status"], "not_measured")
        self.assertEqual(report["metrics"]["whole_case_recall"]["status"], "not_measured")
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["value"], 1)

    def test_E006_repeated_support_mention_is_not_negative_false_positive(self):
        negative = ref("N1", "negative")
        repeated = event(record_function="repeat", checkpoint_status="REPEATED_SAME_EVENT")
        report = evaluate_run([negative], run([repeated]))
        self.assertEqual(report["metrics"]["labelled_negative_specificity"]["value"], 1)
        self.assertEqual(report["metrics"]["labelled_negative_independent_decision_rate"]["value"], 0)
        wrong = evaluate_run([negative], run([event()]))
        self.assertEqual(wrong["metrics"]["labelled_negative_checkpoint_false_positive_rate"]["value"], 1)

    def test_E007_unmatched_uncertain_is_not_automatically_abstention(self):
        report = evaluate_run([ref("U1", "uncertain")], run([]))
        self.assertEqual(report["metrics"]["uncertain_abstain_rate"]["value"], 0)
        self.assertEqual(report["counts"]["uncertain_unmatched"], 1)
        preserved = evaluate_run([ref("U1", "uncertain")], run([event(checkpoint_status="UNCERTAIN_TIME")]))
        self.assertEqual(preserved["metrics"]["uncertain_abstain_rate"]["value"], 1)

    def test_E008_needed_quote_requires_full_span_coverage(self):
        reference = ref(evidence_expectations={"needed": [span(0, 8)]})
        partial = {"E1": {"blocks": [span(0, 4)], "measured": True}}
        report = evaluate_run([reference], run([event()], contexts=partial))
        self.assertEqual(report["metrics"]["needed_actual_material_coverage_conditional"]["value"], 0)
        full = {"E1": {"blocks": [span(0, 8)], "measured": True}}
        report = evaluate_run([reference], run([event()], contexts=full))
        self.assertEqual(report["metrics"]["needed_actual_material_coverage_conditional"]["value"], 1)

    def test_E009_reconstruction_disclosure_is_not_blind_evidence_leak(self):
        reference = ref(evidence_expectations={"needed": [span(0, 8)], "current_action_to_mask": [span(10, 20)]})
        choice_disclosed = event(current_full_document="X" * 100)
        context = {"E1": {"blocks": [span(0, 8)], "measured": True}}
        report = evaluate_run([reference], run([choice_disclosed], contexts=context, windows=context))
        self.assertEqual(report["metrics"]["forbidden_actual_evidence_rate"]["value"], 0)
        self.assertEqual(report["metrics"]["forbidden_blind_packet_rate"]["status"], "not_measured")
        context["E1"]["blocks"].append(span(19, 21))
        report = evaluate_run([reference], run([choice_disclosed], contexts=context, windows=context))
        self.assertEqual(report["metrics"]["forbidden_actual_evidence_rate"]["value"], 1)

    def test_E010_historical_baseline_allowed_but_not_new_observation(self):
        historical = span(0, 8, availability="historical_baseline_not_new")
        reference = ref(evidence_expectations={"forbidden": [historical]})
        context = {"E1": {"blocks": [span(0, 8)], "measured": True}}
        e = event(validated_observation_citations={"valid": [span(0, 8)]},
                  semantic_review={"evidence_evolution": "repeated"})
        report = evaluate_run([reference], run([e], contexts=context))
        self.assertEqual(report["metrics"]["forbidden_actual_evidence_rate"]["status"], "not_measured")
        self.assertEqual(report["metrics"]["historical_baseline_claimed_new_rate"]["value"], 0)
        e["semantic_review"]["evidence_evolution"] = "new_relevant"
        report = evaluate_run([reference], run([e], contexts=context))
        self.assertEqual(report["metrics"]["historical_baseline_claimed_new_rate"]["value"], 1)

    def test_E011_availability_uncertain_is_not_asserted_to_be_future(self):
        reference = ref(evidence_expectations={"forbidden": [span(0, 8, availability="availability_uncertain")]})
        context = {"E1": {"blocks": [span(0, 8)], "measured": True}}
        report = evaluate_run([reference], run([event()], contexts=context))
        self.assertEqual(report["metrics"]["forbidden_actual_evidence_rate"]["status"], "not_measured")
        self.assertEqual(report["counts"]["uncertain_availability_context_hit"], 1)

    def test_E012_multiple_selected_events_same_positive_counted_as_duplicates(self):
        report = evaluate_run([ref()], run([event(), event("E2")]))
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["numerator"], 1)
        self.assertEqual(report["counts"]["duplicate_selected_extras"], 1)

    def test_E012_one_large_prediction_cannot_cover_multiple_positive_events(self):
        report = evaluate_run([ref("P1", start=10, end=20), ref("P2", start=30, end=40)],
                              run([event(start=0, end=50)]))
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["numerator"], 1)
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["denominator"], 2)

    def test_E013_source_hash_mismatch_does_not_align(self):
        e = event()
        e["action_citations"]["valid"][0]["source_sha256"] = "0" * 64
        report = evaluate_run([ref()], run([e]))
        self.assertEqual(report["metrics"]["independent_decision_positive_coverage"]["value"], 0)

    def test_E014_empty_denominator_and_absent_run_are_not_zero_accuracy(self):
        self.assertEqual(metric(0, 0)["status"], "not_measured")
        self.assertIsNone(metric(0, 0)["value"])
        self.assertEqual(evaluate_run([ref()], {"status": "not_run"})["status"], "not_measured")

    def test_E015_missing_actual_material_is_unknown_not_failed_retrieval(self):
        reference = ref(evidence_expectations={"needed": [span(0, 8)]})
        report = evaluate_run([reference], run([event()]))
        self.assertEqual(report["metrics"]["needed_actual_material_coverage_conditional"]["status"], "not_measured")
        self.assertEqual(report["metrics"]["needed_material_end_to_end_coverage"]["status"], "not_measured")
        self.assertEqual(report["counts"]["needed_actual_material_unknown"], 1)

    def test_E016_fulltext_routing_coverage_is_not_event_detection(self):
        report = evaluate_run([ref()], run([], raw=[{"source_ids": ["D1"]}], initial_unit_kind="fulltext_routing"))
        self.assertEqual(report["metrics"]["initial_fulltext_unit_routing_coverage"]["value"], 1)
        self.assertEqual(report["metrics"]["independent_decision_positive_coverage"]["value"], 0)

    def test_E017_discovery_only_does_not_look_like_final_cp_failure(self):
        report = evaluate_run([ref()], run([], discovered=[event()], final_available=False))
        self.assertEqual(report["metrics"]["discovery_positive_coverage"]["value"], 1)
        self.assertEqual(report["metrics"]["candidate_checkpoint_positive_coverage"]["status"], "not_measured")

    def test_E018_reference_freeze_hash_is_required_and_tamper_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "reference_a.json"
            data = {"status": "FROZEN_BEFORE_COMPARISON", "items": [ref()]}
            p.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "RECEIPT_MISSING"):
                load_reference(p)
            p.with_suffix(".freeze.json").write_text(json.dumps({"sha256": file_sha(p)}))
            self.assertEqual(len(load_reference(p)["items"]), 1)
            data["items"][0]["label"] = "negative"
            p.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "FROZEN_HASH_MISMATCH"):
                load_reference(p)

    def test_E019_reference_a_evidence_array_adapts_without_changing_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "reference_a.json"
            item = ref(checkpoint_expectation="uncertain_new_evidence_round", evidence_expectations=[
                {"expected": "needed", "source": span(0, 8), "reason": "fictional"},
                {"expected": "mask_current_choice", "source": span(10, 20), "reason": "fictional"},
            ])
            p.write_text(json.dumps({"status": "FROZEN", "items": [item]}))
            p.with_suffix(".freeze.json").write_text(json.dumps({"sha256": file_sha(p)}))
            item = load_reference(p)["items"][0]
            self.assertEqual(item["label"], "positive")
            self.assertEqual(item["checkpoint_category"], "uncertain")
            self.assertEqual(len(item["evidence_expectations"]["needed"]), 1)
            self.assertEqual(len(item["evidence_expectations"]["current_action_to_mask"]), 1)

    def test_E020_baseline_actual_material_uses_cache_subset_not_entire_window(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            case_dir, cache = root / "case", root / "cache"
            (case_dir / "window_audits").mkdir(parents=True)
            cache.mkdir()
            window_blocks = [{"anchor": span(0, 8)}, {"anchor": span(30, 40)}]
            (case_dir / "window_audits/E1.json").write_text(json.dumps({"blocks": window_blocks}))
            payload = {"current_decision": {"candidate_id": "E1"}, "blocks": [
                {"alias": "E0001", "source_text": "X" * 10, "interval": "between"},
                {"alias": "CURRENT", "source_text": "X" * 10},
            ]}
            request = {"messages": [{"content": json.dumps({"input": payload})}]}
            request_hash = _digest(request)
            (cache / (request_hash + ".json")).write_text(json.dumps({"request": request,
                "receipt": {"request_sha256": request_hash}}))
            (case_dir / "model_receipts.json").write_text(json.dumps([
                {"task": "evidence_evolution", "status": "SUCCESS", "request_sha256": request_hash}]))
            contexts, warnings = _cached_actual_contexts(case_dir, [event()], cache)
            self.assertEqual(warnings, [])
            self.assertTrue(contexts["E1"]["measured"])
            self.assertEqual(len(contexts["E1"]["blocks"]), 1)
            self.assertEqual(contexts["E1"]["blocks"][0]["start"], 30)

    def test_E021_unknown_material_hash_is_not_zero_leakage(self):
        reference = ref(evidence_expectations={"forbidden": [span(0, 8, availability="future")]})
        unauthenticated = {"source_id": "D1", "start": 0, "end": 8, "quote": "X" * 8}
        context = {"E1": {"blocks": [unauthenticated], "measured": True}}
        report = evaluate_run([reference], run([event()], contexts=context, windows=context))
        self.assertEqual(report["metrics"]["forbidden_actual_evidence_rate"]["status"], "not_measured")
        self.assertIsNone(report["items"][0]["evidence_checks"][0]["actual_evidence_hit"])

    def test_E022_historical_restatement_not_new_independent_decision(self):
        historical = event(historical_hint=True, checkpoint_status="HISTORICAL_OR_UNRESOLVED_CANDIDATE",
                           semantic={"role": "decision", "temporal_role": "historical"})
        report = evaluate_run([ref("N1", "negative")], run([historical]))
        self.assertEqual(report["metrics"]["labelled_negative_independent_decision_rate"]["value"], 0)

    def test_E023_reference_needed_mask_overlap_is_flagged_and_not_false_leak(self):
        reference = ref(evidence_expectations={"needed": [span(0, 8)], "current_action_to_mask": [span(0, 20)]})
        context = {"E1": {"blocks": [span(0, 8)], "measured": True}}
        report = evaluate_run([reference], run([event()], contexts=context, windows=context))
        self.assertEqual(report["counts"]["reference_needed_forbidden_overlap"], 1)
        self.assertEqual(report["metrics"]["forbidden_actual_evidence_rate"]["value"], 0)
        forbidden_check = report["items"][0]["evidence_checks"][1]
        self.assertEqual(forbidden_check["scored_nonconflicting_ranges"], [[8, 20]])
        context["E1"]["blocks"].append(span(10, 12))
        report = evaluate_run([reference], run([event()], contexts=context))
        self.assertEqual(report["metrics"]["forbidden_actual_evidence_rate"]["value"], 1)

    def test_E024_retained_candidate_with_explicit_time_uncertainty_is_not_forced_positive(self):
        uncertain_trace = event(record_function="execution", checkpoint_status="CANDIDATE_FIRST_VISIBLE_TEST_TIME_UNCERTAIN")
        report = evaluate_run([ref("U1", "uncertain")], run([uncertain_trace]))
        self.assertEqual(report["metrics"]["uncertain_abstain_rate"]["value"], 1)
        self.assertEqual(report["counts"]["uncertain_forced_positive"], 0)


if __name__ == "__main__":
    unittest.main()

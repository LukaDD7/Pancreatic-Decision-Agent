"""eval: the comparison reads both directory shapes and never calls a provider."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_harness import eval as E


def _write(directory, name, value):
    (Path(directory) / name).write_text(json.dumps(value), encoding="utf-8")


class EvalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.frozen = self.dir / "frozen"
        self.frozen.mkdir()
        self.harness = self.dir / "harness"
        self.harness.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_HEV001_frozen_view_counts_events_statuses_and_tokens(self):
        _write(self.frozen, "decision_events.json", [
            dict(event_id="E1", checkpoint_status="CANDIDATE_CHECKPOINT",
                 record_function="new_decision", choice_time=None),
            dict(event_id="E2", checkpoint_status="NOT_A_CHECKPOINT",
                 record_function="generic", choice_time=None)])
        _write(self.frozen, "episodes.json", [dict(episode_id="EP1")])
        _write(self.frozen, "relations.json", [dict(status="BLOCKED"), dict(status="OK")])
        _write(self.frozen, "model_receipts.json", [
            dict(usage=dict(prompt_tokens=100), cached=False),
            dict(usage=dict(prompt_tokens=50), cached=True)])
        _write(self.frozen, "discovery.json", dict(
            events=[dict(event_id="E1"), dict(event_id="E2")], failures=[]))
        _write(self.frozen, "summary.json", dict(whole_document_or_test_units=3))
        view = E.frozen_view(self.frozen)
        self.assertEqual(view["discovered_events"], 2)
        self.assertEqual(view["final_events"], 2)
        self.assertEqual(view["demoted"], 1)
        self.assertEqual(view["blocked_relations"], 1)
        self.assertEqual(view["prompt_tokens"], 150)
        self.assertEqual(view["cached"], 1)
        self.assertEqual(view["unit_count"], 3)

    def test_HEV002_harness_view_reads_counts_and_audit(self):
        _write(self.harness, "case_result.json", dict(
            discovery=dict(units_run=3),
            events=[dict(event_id="E1", status="ACCEPTED", demoted=False),
                    dict(event_id="E2", status="PROPOSED_WITH_PROBLEMS", demoted=True)],
            rejected_mentions=[dict(quote="x")],
            coverage=dict(units_not_run=0),
            counts=dict(events=2, demoted=1, episodes=1, relations=2),
            episodes=dict(), audit=dict(unresolved_relation_count=1,
                                        events_without_documented_choice_time=1),
            stage_stops=[dict(stage="discovery", stop="answered")]))
        _write(self.harness, "model_receipts.json",
               [dict(usage=dict(prompt_tokens=80))])
        _write(self.harness, "summary.json",
               dict(stages=dict(discovery=dict(zero_event_units=1))))
        view = E.harness_view(self.harness)
        self.assertEqual(view["unit_count"], 3)
        self.assertEqual(view["discovered_events"], 2)
        self.assertEqual(view["final_events"], 1)
        self.assertEqual(view["demoted"], 1)
        self.assertEqual(view["blocked_relations"], 1)
        self.assertEqual(view["zero_event_units"], 1)
        self.assertEqual(view["rejected_mentions"], 1)
        self.assertEqual(view["prompt_tokens"], 80)
        self.assertEqual(view["failures"], 0)

    def test_HEV003_compare_reports_deltas_and_marks_missing_inputs(self):
        _write(self.frozen, "decision_events.json", [])
        _write(self.frozen, "episodes.json", [])
        _write(self.frozen, "relations.json", [])
        _write(self.frozen, "model_receipts.json", [])
        _write(self.harness, "case_result.json", dict(
            counts=dict(episodes=0), events=[], discovery=dict(units_run=0),
            audit=dict(), episodes=dict(), coverage=dict(units_not_run=0)))
        _write(self.harness, "model_receipts.json", [])
        _write(self.harness, "summary.json", dict(status="COMPLETE"))
        _write(self.frozen, "summary.json", dict(status="COMPLETE"))
        result = E.compare(self.frozen, self.harness)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["deltas"]["episodes"], 0)
        # The note must not read as an accuracy claim.
        self.assertIn("不是临床准确性比较", result["note"])

    def test_HEV005_incomplete_harness_is_not_comparable_even_with_all_files(self):
        _write(self.frozen, "decision_events.json", [])
        _write(self.frozen, "summary.json", dict(status="COMPLETE"))
        _write(self.harness, "case_result.json", dict(
            counts=dict(events=14, episodes=14), events=[{}] * 14,
            discovery=dict(units_run=37), coverage=dict(units_not_run=0)))
        _write(self.harness, "model_receipts.json", [])
        _write(self.harness, "summary.json", dict(status="INCOMPLETE"))
        result = E.compare(self.frozen, self.harness)
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["deltas"]["episodes"])
        self.assertEqual(result["comparison_blockers"], ["HARNESS_RUN_NOT_COMPLETE"])

    def test_HEV006_missing_run_status_does_not_certify_comparability(self):
        _write(self.frozen, "decision_events.json", [])
        _write(self.harness, "case_result.json", dict(counts={}))
        result = E.compare(self.frozen, self.harness)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["comparison_blockers"], [
            "FROZEN_RUN_STATUS_MISSING", "HARNESS_RUN_STATUS_MISSING"])

    def test_HEV004_compare_flags_an_incomplete_directory(self):
        result = E.compare(self.frozen, self.harness)
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["deltas"]["prompt_tokens"])


if __name__ == "__main__":
    unittest.main()
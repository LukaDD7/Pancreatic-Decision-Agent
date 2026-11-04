"""CE-AUD-001 offline migration controls, using fictional source records only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.cohort_construction.p2_auto_checkpoint.fhir_io import build
from scripts.cohort_construction.p2_auto_checkpoint.semantic import mask_text
from scripts.cohort_construction.p2_auto_checkpoint_v04.engine import assemble_processes
from scripts.cohort_construction.p2_auto_checkpoint_v06.offline import (
    compare_case, file_hash, replay_release, reset_assembly_fields,
)


def event(eid, function="new_decision", scope="yes", status="CANDIDATE_FIRST_ANCHOR",
          previous=None, process=None, same=None):
    return dict(event_id=eid, source_id="D", source_ids=["D"], scope=scope,
                record_function=function, checkpoint_status=status, management_question="虚构管理问题" + eid,
                source_document_clock=dict(value="2022-01-01 00:00:00"),
                discovery_source_supported=True, validation_reasons=[],
                validated_connection_citations=dict(valid=[dict(source_id="D", quote="虚构原文")],
                                                    ambiguous=[], invalid=[]),
                semantic_review=dict(scope=scope, record_function=function,
                    checkpoint="new_evidence" if status == "CANDIDATE_NEW_EVIDENCE" else "first_anchor",
                    previous_event_id=previous, same_process_previous_id=process,
                    same_event_with=same or [], process_link="supported"))


def frozen(events):
    items = deepcopy(events)
    episodes, relations = assemble_processes(items)
    return items, episodes, relations


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def fixture_release(root, events=None, source_text="虚构原文"):
    release = root / "frozen"; case = release / "SCALE-01"; case.mkdir(parents=True)
    items, episodes, relations = frozen(events or [event("A")])
    for name, value in (("decision_events.json", items), ("episodes.json", episodes),
                         ("relations.json", relations), ("summary.json", {"case_id":"SCALE-01"})):
        write_json(case / name, value)
    text = source_text
    source = dict(source_id="D", kind="document", family="fictional", title="虚构记录", text=text,
                  source_sha256=hashlib.sha256(text.encode()).hexdigest(), clocks={},
                  clock=dict(value=None, precision="unknown"), clock_basis="unknown",
                  provenance={}, original_fields={}, flags=[])
    source_path = case / "source_fhir_r4_bundle.json"
    write_json(source_path, build([source], "SCALE-01", [], []))
    write_json(case / "source_import_receipt.json",
               dict(input_path=str(source_path), sha256=file_hash(source_path)))
    write_json(release / "summary.json", [])
    write_json(release / "case_provenance.json", {"SCALE-01":str(case)})
    return release, case


class OfflineReplayTests(unittest.TestCase):
    def test_CE_AUD001_offline_resets_grouping_and_does_not_modify_input(self):
        items, _, _ = frozen([event("A"), event("B", same=["A"])])
        snapshot = deepcopy(items)
        reset = reset_assembly_fields(items)
        self.assertEqual(items, snapshot)
        self.assertEqual(reset[1]["checkpoint_status"], "CANDIDATE_FIRST_ANCHOR")
        for item in reset:
            self.assertNotIn("episode_id", item)
            self.assertNotIn("canonical_event_id", item)
            self.assertNotIn("status_before_semantic_identity_grouping", item)

    def test_CE_AUD001_offline_grouped_status_without_snapshot_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "ORIGINAL_CANDIDATE_STATUS_MISSING"):
            reset_assembly_fields([event("A", status="GROUPED_SAME_EVENT_MENTION")])

    def test_CE_AUD001_offline_rejects_already_audited_inputs(self):
        item = event("A"); item["final_graph_audit"] = {"version":"old"}
        with self.assertRaisesRegex(ValueError, "INPUT_ALREADY_FINAL_GRAPH_AUDITED"):
            reset_assembly_fields([item])

    def test_CE_AUD001_offline_legacy_replay_can_reproduce_frozen_identity_grouping(self):
        inputs = frozen([event("A"), event("B", same=["A"])])
        compared = compare_case(*inputs)
        self.assertTrue(compared["summary"]["frozen_legacy_replay_comparison"]["equal"])
        self.assertTrue(all(row["semantic_review_unchanged"] for row in compared["event_comparison"]))
        self.assertEqual(compared["summary"]["model_calls"], 0)
        self.assertFalse(compared["summary"]["reference_read"])

    def test_CE_AUD001_offline_reports_frozen_replay_mismatch_instead_of_assuming_parity(self):
        items, episodes, relations = frozen([event("A")])
        episodes[0]["episode_id"] = "FROZEN_DIFFERENT_ID"
        compared = compare_case(items, episodes, relations)
        parity = compared["summary"]["frozen_legacy_replay_comparison"]
        self.assertFalse(parity["equal"])
        self.assertFalse(parity["full_episode_objects_equal"])

    def test_CE_AUD001_offline_blocks_generic_bridge_without_claiming_clinical_error(self):
        items = [event("A"), event("G", function="generic", status="NOT_RELATED", process="A"),
                 event("B", process="G")]
        compared = compare_case(*frozen(items))
        summary = compared["summary"]
        self.assertEqual(summary["legacy_counts"]["candidate_episode_count"], 1)
        self.assertEqual(summary["strict_counts"]["candidate_episode_count"], 2)
        self.assertEqual(summary["supported_to_uncertain_relation_count"], 2)
        self.assertFalse(summary["clinical_accuracy_measured"])
        self.assertIsNone(summary["clinical_error_count"])
        self.assertFalse(summary["improvement_proven"])

    def test_CE_AUD001_offline_keeps_per_event_demotion_reasons_and_original_reference(self):
        items = [event("G", function="generic", status="NOT_RELATED"),
                 event("B", status="CANDIDATE_NEW_EVIDENCE", previous="G", process="G")]
        compared = compare_case(*frozen(items))
        row = next(row for row in compared["event_comparison"] if row["event_id"] == "B")
        self.assertTrue(row["status_changed"])
        self.assertEqual(row["strict_checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")
        self.assertIn("FINAL_GRAPH_TARGET_FINAL_FUNCTION_GENERIC", row["hard_reasons"])
        self.assertEqual(row["dependencies"][0]["declared_reference_event_id"], "G")
        self.assertEqual(row["dependencies"][0]["resolved_reference_event_id"], "G")

    def test_CE_AUD001_offline_input_semantic_proposals_are_immutable(self):
        inputs = frozen([event("A"), event("B", previous="A", process="A",
                                            status="CANDIDATE_NEW_EVIDENCE")])
        snapshot = deepcopy(inputs)
        compare_case(*inputs)
        self.assertEqual(inputs, snapshot)

    def test_CE_AUD001_offline_new_directory_manifest_and_original_inputs_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); release, case = fixture_release(root)
            originals = {str(path):file_hash(path) for path in release.rglob("*.json")}
            output = root / "comparison"
            report = replay_release(release, output)
            self.assertEqual(report["totals"]["case_count"], 1)
            self.assertTrue(report["frozen_input_files_unchanged"])
            self.assertTrue(report["code_files_unchanged_during_replay"])
            self.assertTrue((output / "report.md").exists())
            manifest = json.loads((output / "input_manifest.json").read_text())
            self.assertTrue(manifest["inputs"])
            self.assertTrue(manifest["code"])
            self.assertTrue(all(file_hash(record["path"]) == record["sha256"] for record in manifest["inputs"]))
            source_receipt = json.loads((output / "SCALE-01/source_hash_receipt.json").read_text())
            self.assertEqual(source_receipt["import_validation"]["resources_imported"], 1)
            self.assertTrue(source_receipt["recorded_original_input"]["recorded_sha256_matches"])
            self.assertEqual(originals, {str(path):file_hash(path) for path in release.rglob("*.json")})

    def test_CE_AUD001_offline_output_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); release, _ = fixture_release(root)
            output = root / "comparison"; output.mkdir(); marker = output / "marker"; marker.write_text("keep")
            with self.assertRaises(FileExistsError):
                replay_release(release, output)
            self.assertEqual(marker.read_text(), "keep")

    def test_CE_AUD001_offline_output_cannot_be_within_frozen_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            release, _ = fixture_release(Path(temporary))
            with self.assertRaisesRegex(ValueError, "WITHIN_FROZEN_INPUT"):
                replay_release(release, release / "new")

    def test_CE_AUD001_offline_frozen_source_hash_mismatch_stops_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = event("A"); item["action_citations"] = dict(valid=[dict(source_id="D", source_sha256="wrong")])
            release, _ = fixture_release(root, [item])
            output = root / "comparison"
            with self.assertRaisesRegex(ValueError, "FROZEN_EVENT_SOURCE_HASH_MISMATCH"):
                replay_release(release, output)
            self.assertFalse(output.exists())

    def test_CE_AUD001_offline_recorded_original_source_hash_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); release, case = fixture_release(root)
            receipt_path = case / "source_import_receipt.json"
            receipt = json.loads(receipt_path.read_text()); receipt["sha256"] = "wrong"
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(ValueError, "FROZEN_IMPORT_RECEIPT_HASH_MISMATCH"):
                replay_release(release, root / "comparison")

    def test_CE_AUD001_offline_verifies_and_separately_records_historical_invalid_display_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = "虚构电话13800000000"
            display_hash = hashlib.sha256(mask_text(text).encode()).hexdigest()
            item = event("A")
            item["example_invalid_citations"] = [dict(source_id="D", source_sha256=display_hash,
                                                     status="QUOTE_NOT_IN_SOURCE")]
            release, _ = fixture_release(root, [item], source_text=text)
            output = root / "comparison"; replay_release(release, output)
            receipt = json.loads((output / "SCALE-01/source_hash_receipt.json").read_text())
            self.assertEqual(receipt["legacy_invalid_citation_display_hash_count"], 1)
            self.assertEqual(receipt["event_source_hash_mismatches"], [])
            classified = receipt["verified_legacy_invalid_citation_display_hashes"][0]
            self.assertEqual(classified["display_source_sha256"], display_hash)
            self.assertNotEqual(classified["raw_source_sha256"], display_hash)
            strict = json.loads((output / "SCALE-01/decision_events.json").read_text())
            self.assertEqual(strict[0]["example_invalid_citations"][0]["source_sha256"], display_hash)

    def test_CE_AUD001_offline_does_not_accept_display_hash_as_a_valid_raw_citation_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); text = "虚构电话13800000000"
            item = event("A")
            item["action_citations"] = dict(valid=[dict(source_id="D", start=0, end=2, quote="虚构",
                source_sha256=hashlib.sha256(mask_text(text).encode()).hexdigest(), status="VALID")])
            release, _ = fixture_release(root, [item], source_text=text)
            with self.assertRaisesRegex(ValueError, "FROZEN_EVENT_SOURCE_HASH_MISMATCH"):
                replay_release(release, root / "comparison")

    def test_CE_AUD001_offline_valid_action_raw_character_slice_is_verified(self):
        for quote, expected in [("虚构", True), ("改写", False)]:
            with self.subTest(quote=quote), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); text = "虚构原文"
                item = event("A")
                item["action_citations"] = dict(valid=[dict(source_id="D", start=0, end=2, quote=quote,
                    source_sha256=hashlib.sha256(text.encode()).hexdigest(), status="VALID")])
                release, _ = fixture_release(root, [item], source_text=text)
                if expected:
                    output = root / "comparison"; replay_release(release, output)
                    receipt = json.loads((output / "SCALE-01/source_hash_receipt.json").read_text())
                    self.assertEqual(receipt["valid_action_citation_count"], 1)
                    self.assertTrue(receipt["valid_action_raw_span_checks"][0]["raw_span_and_quote_verified"])
                else:
                    with self.assertRaisesRegex(ValueError, "FROZEN_VALID_ACTION_RAW_SPAN_MISMATCH"):
                        replay_release(release, root / "comparison")


if __name__ == "__main__":
    unittest.main()

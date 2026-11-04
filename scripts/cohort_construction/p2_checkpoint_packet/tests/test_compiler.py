from __future__ import annotations

import copy
import json
import unittest

from scripts.cohort_construction.p2_checkpoint_packet.compiler import (
    PLACEHOLDER, ContractError, build_source_manifest, canonical_sha256,
    compile_partition, freeze_checkpoint_packet, locate_exact_quote,
    make_review_submission, text_sha256, validate_contract,
    validate_review_submission,
)


TARGET = {"checkpoint_id": "CP-fictional-1", "decision_task_id": "fictional-task",
          "visibility_policy_version": "synthetic-policy-1"}


def manifest(text="观察甲。决定乙。实施丙。", **kwargs):
    return build_source_manifest("M-fictional", [{"source_id": "S-fictional", "source_version": "v1",
                                                 "text": text, **kwargs}])


def proposal(source_manifest, segments, **kwargs):
    return {"schema_version": "0.1.0", "proposal_id": "P-fictional", "proposal_version": "1",
            "manifest_id": source_manifest["manifest_id"], "target": copy.deepcopy(TARGET),
            "source_bindings": [{k: source[k] for k in ("source_id", "source_version", "sha256")}
                                for source in source_manifest["sources"]],
            "segments": segments, **kwargs}


def quoted(name, disposition, quote, **context):
    return {"segment_id": name, "source_id": "S-fictional", "disposition": disposition,
            "quote_locator": {"exact_quote": quote, **context}}


def spanned(name, disposition, start, end, quote):
    return {"segment_id": name, "source_id": "S-fictional", "disposition": disposition,
            "resolved_span": {"start": start, "end": end, "exact_quote": quote}}


def safe_compiled():
    source_manifest = manifest()
    plan = proposal(source_manifest, [quoted("s1", "KEEP", "观察甲。"),
                                      quoted("s2", "MASK", "决定乙。"),
                                      quoted("s3", "MASK", "实施丙。")])
    return compile_partition(source_manifest, plan)


def semantic(compiled):
    return {"schema_version": "0.2.0", "assessment_id": "SA-fictional",
            "compiled_sha256": compiled["compiled_sha256"], "target": copy.deepcopy(TARGET),
            "status": "PASS", "reviewer_id": "fictional-auditor", "reviewer_type": "human",
            "independence_attested": True, "assessed_at": "2026-10-28T10:00:00+08:00",
            "audit_protocol_version": "synthetic-audit-protocol-1",
            "review_scope": {"scope_status": "COMPLETE", "included_source_ids": [s["source_id"] for s in compiled["sources"]],
                             "excluded_source_ids": [], "scope_basis": "Fictional positive-control scope."},
            "decision_time_boundary": {"basis": "Fictional decision before result text.", "precision": "event-order",
                                       "exact_time_known": False, "retrospective_reconstruction": True, "uncertainties": []},
            "checks": {name: True for name in ("segmentation_semantics_reviewed",
                                               "permitted_information_scope_reviewed",
                                               "cross_document_restatements_reviewed",
                                               "required_source_coverage_reviewed",
                                               "decision_time_boundary_reviewed",
                                               "all_segments_explicitly_assessed")},
            "segment_assessments": [
                {"source_id": source["source_id"], "start": span["start"], "end": span["end"],
                 "source_sha256": source["source_sha256"], "exact_quote": span["text"],
                 "original_disposition": span["disposition"], "assessment": "ACCEPT",
                 "rationale": "Fictional explicit acceptance.", "issue_codes": []}
                for source in compiled["sources"] for span in source["segments"]],
            "issue_records": [], "source_coverage_assessments": [
                {"source_id": source["source_id"], "disposition": "REVIEWED_INCLUDED",
                 "rationale": "Fictional included source.", "availability_basis": "Fictional event order."}
                for source in compiled["sources"]],
            "required_actions": []}


def frozen_questionnaire():
    # Codes are synthetic test values, never a proposed clinical action scheme.
    resource = {"resourceType": "Questionnaire", "url": "urn:fictional:form", "version": "1",
                "status": "active", "item": [{"linkId": "fictional-item", "type": "choice",
                                               "repeats": True, "text": "虚构测试题",
                                               "answerOption": [{"valueCoding": {"system": "urn:fictional", "code": "a"}},
                                                                {"valueCoding": {"system": "urn:fictional", "code": "b"}}]}]}
    return {"canonical": resource["url"], "version": "1", "sha256": canonical_sha256(resource),
            "frozen": True, "resource": resource}


def packet(compiled=None):
    compiled = safe_compiled() if compiled is None else compiled
    return freeze_checkpoint_packet(compiled, semantic(compiled), frozen_questionnaire(),
                                     packet_id="PK-fictional", packet_version="1")


class ExactSourceTests(unittest.TestCase):
    """CE-PKT-001: exact source and coordinate integrity."""

    def test_emoji_and_crlf_are_codepoints_and_preserved(self):
        text = "甲😀\r\n乙。隐藏。"
        source_manifest = manifest(text)
        original = copy.deepcopy(source_manifest)
        found = locate_exact_quote(source_manifest, "S-fictional", "乙。")
        self.assertEqual((found["anchors"][0]["start"], found["anchors"][0]["end"]), (4, 6))
        result = compile_partition(source_manifest, proposal(source_manifest,
            [spanned("s1", "KEEP", 0, 6, "甲😀\r\n乙。"), spanned("s2", "MASK", 6, 9, "隐藏。")]))
        self.assertEqual(result["mechanical_audit"]["status"], "PASS")
        self.assertEqual("".join(s["text"] for s in result["sources"][0]["segments"]), text)
        self.assertEqual(source_manifest, original)
        self.assertEqual(result["sources"][0]["length_codepoints"], 9)

    def test_line_endings_and_unicode_are_not_normalized(self):
        self.assertNotEqual(text_sha256("甲\r\n乙"), text_sha256("甲\n乙"))
        self.assertNotEqual(text_sha256("é"), text_sha256("e\u0301"))
        source_manifest = manifest("甲\r\n乙")
        self.assertEqual(locate_exact_quote(source_manifest, "S-fictional", "甲\n乙")["status"], "NOT_FOUND")

    def test_changed_source_hash_is_rejected_before_compilation(self):
        source_manifest = manifest()
        plan = proposal(source_manifest, [])
        source_manifest["sources"][0]["text"] += "改字"
        with self.assertRaisesRegex(ContractError, "SOURCE_HASH_MISMATCH"):
            compile_partition(source_manifest, plan)

    def test_wrong_declared_source_length_is_rejected(self):
        source_manifest = manifest("😀")
        source_manifest["sources"][0]["length_codepoints"] = 2  # JavaScript UTF-16 length is not this contract.
        with self.assertRaisesRegex(ContractError, "SOURCE_LENGTH_MISMATCH"):
            compile_partition(source_manifest, proposal(source_manifest, []))

    def test_unpaired_surrogate_rejected(self):
        with self.assertRaisesRegex(ContractError, "valid Unicode"):
            manifest("\ud800")

    def test_resolved_coordinates_require_exact_quote(self):
        source_manifest = manifest("甲😀乙")
        bad = compile_partition(source_manifest, proposal(source_manifest, [spanned("s1", "KEEP", 1, 3, "😀")]))
        self.assertIn("SPAN_QUOTE_MISMATCH", [i["code"] for i in bad["mechanical_audit"]["issues"]])
        good = compile_partition(source_manifest, proposal(source_manifest, [spanned("s1", "KEEP", 0, 3, "甲😀乙")]))
        self.assertEqual(good["mechanical_audit"]["status"], "PASS")

    def test_invalid_span_and_boolean_coordinate_are_rejected(self):
        source_manifest = manifest("甲")
        for start, end in ((0, 0), (0, 2)):
            result = compile_partition(source_manifest, proposal(source_manifest, [spanned("s1", "KEEP", start, end, "甲")]))
            self.assertEqual(result["mechanical_audit"]["status"], "FAIL")
        with self.assertRaises(ContractError):
            compile_partition(source_manifest, proposal(source_manifest, [spanned("s1", "KEEP", False, 1, "甲")]))


class AnchorTests(unittest.TestCase):
    """CE-PKT-002: no first-match shortcut or fuzzy quote repair."""

    def test_repeated_quote_returns_all_and_requires_disambiguation(self):
        source_manifest = manifest("甲稳定。乙稳定。")
        matches = locate_exact_quote(source_manifest, "S-fictional", "稳定。")
        self.assertEqual(matches["status"], "AMBIGUOUS")
        self.assertEqual([(a["start"], a["end"]) for a in matches["anchors"]], [(1, 4), (5, 8)])
        result = compile_partition(source_manifest, proposal(source_manifest, [quoted("s1", "KEEP", "稳定。")]))
        self.assertEqual(result["mechanical_audit"]["status"], "FAIL")
        self.assertIn("QUOTE_AMBIGUOUS", [i["code"] for i in result["mechanical_audit"]["issues"]])
        self.assertTrue(all(s["disposition"] == "REVIEW" for s in result["sources"][0]["segments"]))

    def test_exact_adjacent_context_and_tool_anchor_select_one_occurrence(self):
        source_manifest = manifest("甲稳定。乙稳定。")
        unique = locate_exact_quote(source_manifest, "S-fictional", "稳定。", prefix="乙")
        self.assertEqual(unique["status"], "UNIQUE")
        anchor = unique["anchors"][0]
        plan = proposal(source_manifest, [spanned("s1", "MASK", 0, 5, "甲稳定。乙"),
                                         {"segment_id": "s2", "source_id": "S-fictional", "disposition": "KEEP",
                                          "anchor_id": anchor["anchor_id"]}])
        result = compile_partition(source_manifest, plan, anchor_catalog=[anchor])
        self.assertEqual(result["mechanical_audit"]["status"], "PASS")
        self.assertEqual(result["sources"][0]["segments"][1]["text"], "稳定。")

    def test_overlapping_string_matches_are_retained(self):
        found = locate_exact_quote(manifest("aaa"), "S-fictional", "aa")
        self.assertEqual([(a["start"], a["end"]) for a in found["anchors"]], [(0, 2), (1, 3)])

    def test_rewritten_quote_is_not_repaired_with_regex(self):
        source_manifest = manifest("“原文”。")
        result = compile_partition(source_manifest, proposal(source_manifest, [quoted("s1", "KEEP", '"原文"。')]))
        self.assertIn("QUOTE_NOT_FOUND", [i["code"] for i in result["mechanical_audit"]["issues"]])

    def test_unknown_and_stale_tool_anchors_are_rejected(self):
        source_manifest = manifest("甲乙")
        anchor = locate_exact_quote(source_manifest, "S-fictional", "甲乙")["anchors"][0]
        plan = proposal(source_manifest, [{"segment_id": "s1", "source_id": "S-fictional", "disposition": "KEEP",
                                          "anchor_id": anchor["anchor_id"]}])
        unknown = compile_partition(source_manifest, plan)
        self.assertIn("UNKNOWN_ANCHOR_ID", [i["code"] for i in unknown["mechanical_audit"]["issues"]])
        anchor["source_version"] = "stale"
        stale = compile_partition(source_manifest, plan, anchor_catalog=[anchor])
        self.assertIn("ANCHOR_SOURCE_BINDING_MISMATCH", [i["code"] for i in stale["mechanical_audit"]["issues"]])

    def test_tool_anchor_cannot_be_relabelled_to_another_location(self):
        source_manifest = manifest("甲甲")
        anchor = locate_exact_quote(source_manifest, "S-fictional", "甲")["anchors"][0]
        anchor["start"], anchor["end"] = 1, 2
        plan = proposal(source_manifest, [{"segment_id": "s1", "source_id": "S-fictional", "disposition": "MASK", "anchor_id": anchor["anchor_id"]}])
        result = compile_partition(source_manifest, plan, anchor_catalog=[anchor])
        self.assertIn("ANCHOR_SOURCE_BINDING_MISMATCH", [i["code"] for i in result["mechanical_audit"]["issues"]])


class PartitionTests(unittest.TestCase):
    """CE-PKT-003: exhaustive/exclusive partition, no default KEEP."""

    def test_unassigned_gap_becomes_review(self):
        source_manifest = manifest("甲乙丙")
        result = compile_partition(source_manifest, proposal(source_manifest, [quoted("s1", "KEEP", "甲"), quoted("s2", "MASK", "丙")]))
        segments = result["sources"][0]["segments"]
        self.assertEqual([s["disposition"] for s in segments], ["KEEP", "REVIEW", "MASK"])
        self.assertEqual("".join(s["text"] for s in segments), "甲乙丙")
        self.assertEqual(result["mechanical_audit"]["status"], "FAIL")

    def test_overlap_becomes_review_even_when_both_propose_keep(self):
        source_manifest = manifest("甲乙丙")
        for second_disposition in ("KEEP", "MASK"):
            result = compile_partition(source_manifest, proposal(source_manifest, [quoted("s1", "KEEP", "甲乙"), quoted("s2", second_disposition, "乙丙")]))
            self.assertEqual(result["sources"][0]["segments"][1]["disposition"], "REVIEW")
            self.assertEqual(result["sources"][0]["segments"][1]["generated_reason"], "OVERLAPPING_PROPOSALS")
            self.assertEqual("".join(s["text"] for s in result["sources"][0]["segments"]), "甲乙丙")

    def test_explicit_review_is_mechanically_valid_but_not_releasable(self):
        source_manifest = manifest("甲乙")
        result = compile_partition(source_manifest, proposal(source_manifest, [quoted("s1", "REVIEW", "甲乙")]))
        self.assertEqual(result["mechanical_audit"]["status"], "PASS")
        self.assertEqual(result["mechanical_audit"]["review_segment_count"], 1)
        with self.assertRaisesRegex(ContractError, "UNRESOLVED_REVIEW_SEGMENT"):
            packet(result)

    def test_zero_segment_proposal_is_not_keep_all(self):
        source_manifest = manifest("甲乙")
        result = compile_partition(source_manifest, proposal(source_manifest, []))
        self.assertEqual(result["sources"][0]["segments"][0]["disposition"], "REVIEW")

    def test_empty_source_is_a_valid_empty_partition(self):
        source_manifest = manifest("")
        result = compile_partition(source_manifest, proposal(source_manifest, []))
        self.assertEqual(result["sources"][0]["segments"], [])
        self.assertEqual(result["mechanical_audit"]["status"], "PASS")
        self.assertEqual(packet(result)["materials"][0]["text"], "")

    def test_complete_whole_source_partition_positive_control(self):
        result = safe_compiled()
        self.assertEqual(result["mechanical_audit"]["status"], "PASS")
        self.assertTrue(result["mechanical_audit"]["full_coverage"])
        self.assertTrue(result["mechanical_audit"]["mutually_exclusive"])
        self.assertEqual(result["semantic_audit_status"], "NOT_ASSESSED")


class BindingTests(unittest.TestCase):
    """CE-PKT-004: one source/target/version cannot silently replace another."""

    def test_stale_source_version_and_binding_set_rejected(self):
        source_manifest = manifest()
        plan = proposal(source_manifest, [])
        plan["source_bindings"][0]["source_version"] = "v0"
        with self.assertRaisesRegex(ContractError, "SOURCE_BINDING_MISMATCH"):
            compile_partition(source_manifest, plan)
        plan = proposal(source_manifest, [])
        plan["source_bindings"].append({"source_id": "other", "source_version": "1", "sha256": "0" * 64})
        with self.assertRaisesRegex(ContractError, "SOURCE_BINDING_SET_MISMATCH"):
            compile_partition(source_manifest, plan)

    def test_manifest_identity_mismatch_rejected(self):
        source_manifest = manifest()
        with self.assertRaisesRegex(ContractError, "MANIFEST_ID_MISMATCH"):
            compile_partition(source_manifest, proposal(source_manifest, [], manifest_id="wrong"))

    def test_duplicate_ids_and_two_locators_rejected(self):
        source_manifest = manifest("甲")
        first = quoted("s1", "KEEP", "甲")
        with self.assertRaisesRegex(ContractError, "DUPLICATE_SEGMENT_ID"):
            compile_partition(source_manifest, proposal(source_manifest, [first, first]))
        first["resolved_span"] = {"start": 0, "end": 1, "exact_quote": "甲"}
        with self.assertRaises(ContractError):
            compile_partition(source_manifest, proposal(source_manifest, [first]))

    def test_target_specific_plans_can_have_different_visibility(self):
        source_manifest = manifest("历史安排。")
        keep = proposal(source_manifest, [quoted("s1", "KEEP", "历史安排。")])
        mask = proposal(source_manifest, [quoted("s1", "MASK", "历史安排。")])
        mask["target"]["checkpoint_id"] = "CP-fictional-2"
        result_keep, result_mask = compile_partition(source_manifest, keep), compile_partition(source_manifest, mask)
        self.assertNotEqual(result_keep["compiled_sha256"], result_mask["compiled_sha256"])
        wrong_audit = semantic(result_keep)
        with self.assertRaisesRegex(ContractError, "SEMANTIC_AUDIT_BINDING_MISMATCH"):
            freeze_checkpoint_packet(result_mask, wrong_audit, frozen_questionnaire(), packet_id="P", packet_version="1")


class DoctorIsolationTests(unittest.TestCase):
    """CE-PKT-005: no frozen release from mechanical checks alone."""

    def test_doctor_packet_excludes_masked_text_coordinates_and_research_audit(self):
        compiled = safe_compiled()
        before = copy.deepcopy(compiled)
        doctor = packet(compiled)
        serialized = json.dumps(doctor, ensure_ascii=False)
        self.assertEqual(doctor["materials"][0]["text"], "观察甲。" + PLACEHOLDER)
        for forbidden in ("决定乙。", "实施丙。", "proposal_segment_ids", "generated_reason", "source_sha256"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(doctor["materials"][0]["text"].count(PLACEHOLDER), 1)
        self.assertEqual(compiled, before)
        self.assertNotIn("doctor_payload", compiled)

    def test_semantic_fail_pending_or_unattested_cannot_freeze(self):
        compiled = safe_compiled()
        for change in ({"status": "PENDING"}, {"status": "FAIL"}, {"independence_attested": False}):
            audit = semantic(compiled)
            audit.update(change)
            with self.assertRaisesRegex(ContractError, "INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED"):
                freeze_checkpoint_packet(compiled, audit, frozen_questionnaire(), packet_id="P", packet_version="1")
        audit = semantic(compiled)
        audit["checks"]["permitted_information_scope_reviewed"] = False
        with self.assertRaisesRegex(ContractError, "SEMANTIC_CHECKS_INCOMPLETE"):
            freeze_checkpoint_packet(compiled, audit, frozen_questionnaire(), packet_id="P", packet_version="1")

    def test_mechanical_pass_does_not_claim_missing_clinical_decision_was_masked(self):
        source_manifest = manifest("观察甲。决定乙。")
        bad_semantics = compile_partition(source_manifest, proposal(source_manifest, [quoted("s1", "KEEP", "观察甲。决定乙。")]))
        self.assertEqual(bad_semantics["mechanical_audit"]["status"], "PASS")
        self.assertEqual(bad_semantics["semantic_audit_status"], "NOT_ASSESSED")
        audit = semantic(bad_semantics)
        audit["status"] = "FAIL"  # An independent reviewer must identify the semantic error.
        with self.assertRaisesRegex(ContractError, "INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED"):
            freeze_checkpoint_packet(bad_semantics, audit, frozen_questionnaire(), packet_id="P", packet_version="1")

    def test_mutated_compiled_text_rejected(self):
        compiled = safe_compiled()
        compiled["sources"][0]["segments"][0]["text"] = "改字甲。"
        with self.assertRaisesRegex(ContractError, "COMPILED_HASH_MISMATCH"):
            packet(compiled)

    def test_full_original_reconstruction_hash_checked_even_with_recomputed_object_hash(self):
        compiled = safe_compiled()
        compiled["sources"][0]["segments"][0]["text"] = "改字甲。"
        compiled["compiled_sha256"] = canonical_sha256({k: v for k, v in compiled.items() if k != "compiled_sha256"})
        with self.assertRaisesRegex(ContractError, "PARTITION_SOURCE_HASH_MISMATCH"):
            packet(compiled)

    def test_questionnaire_is_supplied_and_frozen_not_inferred_from_llm_actions(self):
        compiled = safe_compiled()
        doctor = packet(compiled)
        self.assertEqual(doctor["questionnaire"], frozen_questionnaire())
        self.assertEqual(doctor["questionnaire"]["resource"]["item"][0]["repeats"], True)
        bad = frozen_questionnaire()
        bad["resource"]["version"] = "2"
        with self.assertRaisesRegex(ContractError, "QUESTIONNAIRE_BINDING_MISMATCH"):
            freeze_checkpoint_packet(compiled, semantic(compiled), bad, packet_id="P", packet_version="1")
        for not_true in (False, 1):
            bad = frozen_questionnaire()
            bad["frozen"] = not_true
            with self.assertRaises(ContractError):
                freeze_checkpoint_packet(compiled, semantic(compiled), bad, packet_id="P", packet_version="1")


class SubmissionTests(unittest.TestCase):
    """CE-PKT-006: actual expert answers are bound to immutable inputs."""

    def response(self):
        return {"resourceType": "QuestionnaireResponse", "questionnaire": "urn:fictional:form|1",
                "status": "completed", "item": [{"linkId": "fictional-item", "answer": [
                    {"valueCoding": {"system": "urn:fictional", "code": "a"}},
                    {"valueCoding": {"system": "urn:fictional", "code": "b"}}]}]}

    def submission(self, doctor=None):
        return make_review_submission(doctor or packet(), self.response(), submission_id="RS-fictional",
                                      reviewer_assignment_id="A-fictional", submitted_at="2026-10-28T10:00:00+08:00")

    def test_multiple_actual_answers_are_preserved_and_not_prefilled(self):
        doctor = packet()
        self.assertNotIn("questionnaire_response", doctor)
        response = self.response()
        actual = self.submission(doctor)
        self.assertEqual(actual["questionnaire_response"], response)
        validate_review_submission(actual, doctor)

    def test_wrong_packet_or_form_version_rejected(self):
        doctor = packet()
        wrong = self.submission(doctor)
        wrong["packet_binding"]["packet_version"] = "2"
        with self.assertRaisesRegex(ContractError, "SUBMISSION_PACKET_BINDING_MISMATCH"):
            validate_review_submission(wrong, doctor)
        wrong = self.submission(doctor)
        wrong["questionnaire_response"]["questionnaire"] = "urn:fictional:form|2"
        with self.assertRaisesRegex(ContractError, "RESPONSE_QUESTIONNAIRE_BINDING_MISMATCH"):
            validate_review_submission(wrong, doctor)

    def test_changed_doctor_packet_rejected(self):
        doctor = packet()
        actual = self.submission(doctor)
        doctor["materials"][0]["text"] += "篡改"
        with self.assertRaisesRegex(ContractError, "PACKET_HASH_MISMATCH"):
            validate_review_submission(actual, doctor)

    def test_extra_research_fields_not_allowed_in_doctor_packet(self):
        doctor = packet()
        doctor["researcher_original_text"] = "禁止混入"
        with self.assertRaises(ContractError):
            validate_contract("FrozenCheckpointPacket", doctor)


if __name__ == "__main__":
    unittest.main()

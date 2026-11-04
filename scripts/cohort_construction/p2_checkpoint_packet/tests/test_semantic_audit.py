import copy
import unittest

from scripts.cohort_construction.p2_checkpoint_packet.compiler import ContractError, freeze_checkpoint_packet
from scripts.cohort_construction.p2_checkpoint_packet.semantic_audit import (
    revise_partition, validate_semantic_audit, write_assessment,
)
from scripts.cohort_construction.p2_checkpoint_packet.tests import test_compiler as fixtures


class SemanticAuditTests(unittest.TestCase):
    """CE-PKT-007/008/009/010: hash-bound current-partition permission."""

    def unresolved(self, compiled=None):
        compiled = fixtures.safe_compiled() if compiled is None else compiled
        audit = fixtures.semantic(compiled)
        audit.update(status="PENDING", required_actions=["Resolve the masked boundary."])
        audit["review_scope"]["scope_status"] = "INCOMPLETE"
        for segment in audit["segment_assessments"]:
            if segment["original_disposition"] == "MASK":
                segment["assessment"] = "UNRESOLVED"
                segment["rationale"] = "The independent reviewer has not established this boundary."
        return audit

    def test_unresolved_assessment_is_stored_and_never_creates_permission(self):
        compiled = fixtures.safe_compiled()
        audit = self.unresolved(compiled)
        summary = validate_semantic_audit(compiled, audit)
        self.assertEqual(summary["status"], "PENDING")
        with self.assertRaisesRegex(ContractError, "INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED"):
            freeze_checkpoint_packet(compiled, audit, fixtures.frozen_questionnaire(), packet_id="P", packet_version="1")

    def test_every_compiled_span_and_original_quote_must_be_assessed(self):
        compiled = fixtures.safe_compiled()
        missing = fixtures.semantic(compiled)
        missing["segment_assessments"].pop()
        with self.assertRaisesRegex(ContractError, "EVERY_COMPILED_SEGMENT_REQUIRES_ASSESSMENT"):
            validate_semantic_audit(compiled, missing)
        wrong_quote = fixtures.semantic(compiled)
        wrong_quote["segment_assessments"][0]["exact_quote"] = "改写的事实"
        with self.assertRaisesRegex(ContractError, "SEGMENT_ASSESSMENT_BINDING_MISMATCH"):
            validate_semantic_audit(compiled, wrong_quote)
        stale = fixtures.semantic(compiled)
        stale["compiled_sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "SEMANTIC_AUDIT_BINDING_MISMATCH"):
            validate_semantic_audit(compiled, stale)

    def test_revision_does_not_silently_pass_or_erase_original(self):
        compiled = fixtures.safe_compiled()
        audit = self.unresolved(compiled)
        original = copy.deepcopy(compiled)
        mask = next(s for s in audit["segment_assessments"] if s["original_disposition"] == "MASK")
        mask.update(assessment="REVISE", revised_disposition="KEEP", rationale="Reviewer proposes disclosure after revision.")
        revised = revise_partition(fixtures.manifest(), fixtures.proposal(fixtures.manifest(), [
            fixtures.quoted("s1", "KEEP", "观察甲。"), fixtures.quoted("s2", "MASK", "决定乙。"),
            fixtures.quoted("s3", "MASK", "实施丙。")]), audit, compiled=compiled)
        self.assertEqual(compiled, original)
        self.assertEqual(audit["status"], "PENDING")
        self.assertEqual([p["disposition"] for p in revised["sources"][0]["segments"]], ["KEEP", "KEEP", "REVIEW"])
        self.assertNotEqual(revised["compiled_sha256"], compiled["compiled_sha256"])
        self.assertEqual(revised["semantic_audit_status"], "NOT_ASSESSED")

    def test_pass_requires_complete_scope_and_no_blocking_or_unresolved_items(self):
        compiled = fixtures.safe_compiled()
        for change in (
            lambda a: a["review_scope"].update(scope_status="INCOMPLETE"),
            lambda a: a["segment_assessments"][0].update(assessment="UNRESOLVED"),
            lambda a: a["issue_records"].append(dict(issue_id="I-1", code="EXAMPLE", attribution="material_assembly",
                severity="BLOCKING", status="OPEN", description="Unreconciled fictional gap.",
                source_ids=["S-fictional"], supporting_quotes=[])),
            lambda a: a["checks"].update(all_segments_explicitly_assessed=False),
        ):
            audit = fixtures.semantic(compiled)
            change(audit)
            with self.assertRaises(ContractError):
                validate_semantic_audit(compiled, audit)

    def test_scope_exclusions_and_issue_quotes_are_verified(self):
        compiled = fixtures.safe_compiled()
        audit = fixtures.semantic(compiled)
        audit["review_scope"]["excluded_source_ids"] = ["not-reviewed"]
        with self.assertRaisesRegex(ContractError, "SOURCE_COVERAGE_BINDING_MISMATCH"):
            validate_semantic_audit(compiled, audit)
        audit["source_coverage_assessments"].append(dict(source_id="not-reviewed", disposition="REVIEWED_EXCLUDED",
            rationale="Reviewed fictional source is outside this target's scope.",
            availability_basis="Fictional independent review of target relevance."))
        validate_semantic_audit(compiled, audit)
        audit["issue_records"].append(dict(issue_id="I-2", code="EXAMPLE", attribution="model_proposal",
            severity="NON_BLOCKING", status="RESOLVED", description="Fictional resolved example.",
            source_ids=["S-fictional"], supporting_quotes=[dict(source_id="S-fictional", start=0, end=4, exact_quote="观察甲。")]))
        validate_semantic_audit(compiled, audit)
        audit["issue_records"][0]["supporting_quotes"][0]["exact_quote"] = "篡改"
        with self.assertRaisesRegex(ContractError, "SEMANTIC_ISSUE_QUOTE_BINDING_MISMATCH"):
            validate_semantic_audit(compiled, audit)

    def test_writer_uses_exclusive_creation_and_does_not_change_status(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"
            result = write_assessment(self.unresolved(), path)
            self.assertEqual(result["assessment_status"], "PENDING")
            with self.assertRaises(FileExistsError):
                write_assessment(self.unresolved(), path)

    def freeze(self, compiled, audit):
        return freeze_checkpoint_packet(compiled, audit, fixtures.frozen_questionnaire(),
                                        packet_id="PK-fictional-gate", packet_version="1")

    def coverage_audit(self, compiled, source_location, disposition):
        audit = fixtures.semantic(compiled)
        if source_location == "included":
            audit["source_coverage_assessments"][0]["disposition"] = disposition
        else:
            audit["review_scope"]["excluded_source_ids"] = ["S-fictional-excluded"]
            audit["source_coverage_assessments"].append(dict(
                source_id="S-fictional-excluded", disposition=disposition,
                rationale="Fictional source scope assessment.",
                availability_basis="Fictional review of this Checkpoint's information scope."))
        return audit

    def test_pass_unresolved_source_coverage_blocks_validation_and_freeze(self):
        compiled = fixtures.safe_compiled()
        for location in ("included", "excluded"):
            with self.subTest(source_location=location):
                audit = self.coverage_audit(compiled, location, "UNRESOLVED")
                for operation in (validate_semantic_audit, self.freeze):
                    with self.assertRaisesRegex(ContractError, "UNRESOLVED_SOURCE_COVERAGE_CANNOT_PASS"):
                        operation(compiled, audit)

    def test_pending_and_fail_preserve_unresolved_source_coverage_without_permission(self):
        compiled = fixtures.safe_compiled()
        original = copy.deepcopy(compiled)
        for status in ("PENDING", "FAIL"):
            for location in ("included", "excluded"):
                with self.subTest(status=status, source_location=location):
                    audit = self.coverage_audit(compiled, location, "UNRESOLVED")
                    audit["status"] = status
                    audit_before = copy.deepcopy(audit)
                    self.assertEqual(validate_semantic_audit(compiled, audit)["status"], status)
                    with self.assertRaisesRegex(ContractError, "INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED"):
                        self.freeze(compiled, audit)
                    self.assertEqual(audit, audit_before)
        self.assertEqual(compiled, original)

    def test_reviewed_included_and_excluded_source_coverage_allows_freeze(self):
        compiled = fixtures.safe_compiled()
        audit = self.coverage_audit(compiled, "excluded", "REVIEWED_EXCLUDED")
        self.assertEqual(validate_semantic_audit(compiled, audit)["status"], "PASS")
        self.assertEqual(self.freeze(compiled, audit)["materials"][0]["text"],
                         "观察甲。" + fixtures.PLACEHOLDER)

    def test_pass_opposite_source_scope_disposition_blocks_validation_and_freeze(self):
        compiled = fixtures.safe_compiled()
        for location, disposition in (("included", "REVIEWED_EXCLUDED"),
                                      ("excluded", "REVIEWED_INCLUDED")):
            for operation in (validate_semantic_audit, self.freeze):
                with self.subTest(source_location=location, operation=operation.__name__):
                    audit = self.coverage_audit(compiled, location, disposition)
                    before = copy.deepcopy((compiled, audit))
                    with self.assertRaisesRegex(ContractError,
                            "SOURCE_SCOPE_REVISION_REQUIRES_NEW_PARTITION_AND_REAUDIT"):
                        operation(compiled, audit)
                    self.assertEqual((compiled, audit), before)

    def test_pending_and_fail_preserve_opposite_source_scope_without_permission(self):
        compiled = fixtures.safe_compiled()
        for status in ("PENDING", "FAIL"):
            for location, disposition in (("included", "REVIEWED_EXCLUDED"),
                                          ("excluded", "REVIEWED_INCLUDED")):
                with self.subTest(status=status, source_location=location):
                    audit = self.coverage_audit(compiled, location, disposition)
                    audit["status"] = status
                    before = copy.deepcopy((compiled, audit))
                    self.assertEqual(validate_semantic_audit(compiled, audit)["status"], status)
                    with self.assertRaisesRegex(ContractError, "INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED"):
                        self.freeze(compiled, audit)
                    self.assertEqual((compiled, audit), before)

    def test_source_gap_dispositions_do_not_implicitly_change_current_scope(self):
        compiled = fixtures.safe_compiled()
        for location in ("included", "excluded"):
            for disposition in ("REQUIRED_MISSING", "DELIVERY_NOT_FOUND"):
                with self.subTest(source_location=location, disposition=disposition):
                    audit = self.coverage_audit(compiled, location, disposition)
                    before = copy.deepcopy((compiled, audit))
                    self.assertEqual(validate_semantic_audit(compiled, audit)["status"], "PASS")
                    packet = self.freeze(compiled, audit)
                    self.assertEqual([m["source_id"] for m in packet["materials"]], ["S-fictional"])
                    self.assertEqual((compiled, audit), before)

    def test_scope_revision_requires_new_manifest_partition_and_reaudit(self):
        compiled = fixtures.safe_compiled()
        audit = self.coverage_audit(compiled, "excluded", "REVIEWED_INCLUDED")
        audit["status"] = "PENDING"
        audit["source_coverage_assessments"][0]["disposition"] = "REVIEWED_EXCLUDED"
        before = copy.deepcopy((compiled, audit))
        self.assertEqual(validate_semantic_audit(compiled, audit)["status"], "PENDING")
        with self.assertRaisesRegex(ContractError, "INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED"):
            self.freeze(compiled, audit)

        # A source-scope revision is explicit new input, never an automatic
        # movement of a coverage field or a claim about the clinical facts.
        text = "补充观察。"
        revised_manifest = fixtures.build_source_manifest("M-fictional-scope-v2", [
            dict(source_id="S-fictional-excluded", source_version="v1", text=text)])
        revised_proposal = fixtures.proposal(revised_manifest, [dict(
            segment_id="s-new", source_id="S-fictional-excluded", disposition="KEEP",
            resolved_span=dict(start=0, end=len(text), exact_quote=text))])
        revised_compiled = fixtures.compile_partition(revised_manifest, revised_proposal)
        self.assertEqual(revised_compiled["semantic_audit_status"], "NOT_ASSESSED")
        self.assertNotEqual(revised_compiled["compiled_sha256"], compiled["compiled_sha256"])
        with self.assertRaisesRegex(ContractError, "SEMANTIC_AUDIT_BINDING_MISMATCH"):
            validate_semantic_audit(revised_compiled, audit)
        new_audit = fixtures.semantic(revised_compiled)
        new_audit["assessment_id"] = "SA-fictional-new-scope"
        new_audit["review_scope"]["excluded_source_ids"] = ["S-fictional"]
        new_audit["source_coverage_assessments"].append(dict(
            source_id="S-fictional", disposition="REVIEWED_EXCLUDED",
            rationale="Fictional independent reassessment of the new source scope.",
            availability_basis="Fictional declared task scope after explicit revision."))
        packet = self.freeze(revised_compiled, new_audit)
        self.assertEqual([(m["source_id"], m["text"]) for m in packet["materials"]],
                         [("S-fictional-excluded", text)])
        self.assertEqual(packet["provenance"]["compiled_partition_sha256"], revised_compiled["compiled_sha256"])
        self.assertEqual((compiled, audit), before)

    def test_pass_revision_cannot_validate_or_freeze_even_when_disposition_is_unchanged(self):
        compiled = fixtures.safe_compiled()
        for original_disposition, revised_disposition in (("KEEP", "MASK"), ("MASK", "KEEP"),
                                                           ("KEEP", "KEEP"), ("MASK", "MASK")):
            with self.subTest(original=original_disposition, revised=revised_disposition):
                audit = fixtures.semantic(compiled)
                segment = next(s for s in audit["segment_assessments"]
                               if s["original_disposition"] == original_disposition)
                segment.update(assessment="REVISE", revised_disposition=revised_disposition,
                               rationale="Fictional revision requires recompilation and independent reassessment.")
                for operation in (validate_semantic_audit, self.freeze):
                    with self.assertRaisesRegex(ContractError, "REVISION_REQUIRES_RECOMPILE_AND_REAUDIT"):
                        operation(compiled, audit)

    def test_pending_and_fail_revisions_recompile_reaudit_then_freeze(self):
        for status in ("PENDING", "FAIL"):
            for original_disposition, revised_disposition in (("KEEP", "MASK"), ("MASK", "KEEP")):
                with self.subTest(status=status, original=original_disposition, revised=revised_disposition):
                    manifest = fixtures.manifest()
                    proposal = fixtures.proposal(manifest, [fixtures.quoted("s1", "KEEP", "观察甲。"),
                        fixtures.quoted("s2", "MASK", "决定乙。"), fixtures.quoted("s3", "MASK", "实施丙。")])
                    compiled = fixtures.compile_partition(manifest, proposal)
                    audit = fixtures.semantic(compiled)
                    audit.update(status=status, assessment_id="SA-fictional-revision")
                    segment = next(s for s in audit["segment_assessments"]
                                   if s["original_disposition"] == original_disposition)
                    segment.update(assessment="REVISE", revised_disposition=revised_disposition,
                                   rationale="Fictional independent reviewer proposes a changed disposition.")
                    before = copy.deepcopy((manifest, proposal, compiled, audit))
                    self.assertEqual(validate_semantic_audit(compiled, audit)["status"], status)
                    with self.assertRaisesRegex(ContractError, "INDEPENDENT_SEMANTIC_AUDIT_NOT_PASSED"):
                        self.freeze(compiled, audit)
                    revised = revise_partition(manifest, proposal, audit, compiled=compiled)
                    self.assertEqual(revised["semantic_audit_status"], "NOT_ASSESSED")
                    self.assertNotEqual(revised["compiled_sha256"], compiled["compiled_sha256"])
                    # The old partition's PASS cannot approve the newly compiled bytes.
                    with self.assertRaisesRegex(ContractError, "SEMANTIC_AUDIT_BINDING_MISMATCH"):
                        self.freeze(revised, fixtures.semantic(compiled))
                    new_audit = fixtures.semantic(revised)
                    new_audit["assessment_id"] = "SA-fictional-new-partition"
                    self.assertTrue(all(s["assessment"] == "ACCEPT" for s in new_audit["segment_assessments"]))
                    packet = self.freeze(revised, new_audit)
                    expected = (fixtures.PLACEHOLDER if revised_disposition == "MASK"
                                else "观察甲。决定乙。" + fixtures.PLACEHOLDER)
                    self.assertEqual(packet["materials"][0]["text"], expected)
                    self.assertEqual(packet["provenance"]["compiled_partition_sha256"], revised["compiled_sha256"])
                    self.assertEqual((manifest, proposal, compiled, audit), before)


if __name__ == "__main__":
    unittest.main()

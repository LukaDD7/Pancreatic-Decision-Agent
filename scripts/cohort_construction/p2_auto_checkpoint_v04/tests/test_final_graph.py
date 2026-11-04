"""CE-AUD-001 fictional final-classification and dependency counterexamples."""
from copy import deepcopy
import unittest

from scripts.cohort_construction.p2_auto_checkpoint_v04.final_graph import (
    COMPARISON, IDENTITY, PROCESS, audit_final_graph, relation_guard,
)


def event(eid, previous=None, process=None, same=None, function="new_decision", scope="yes",
          status="CANDIDATE_FIRST_ANCHOR", prior=None, identity=None, **kwargs):
    item = dict(event_id=eid, scope=scope, record_function=function, checkpoint_status=status,
                validation_reasons=[], discovery_source_supported=True,
                validated_connection_citations=dict(valid=[dict(source_id="fictional", quote="原文")],
                                                    ambiguous=[], invalid=[]),
                semantic_review=dict(event_id=eid, scope=scope, record_function=function,
                    checkpoint="new_evidence" if status == "CANDIDATE_NEW_EVIDENCE" else "first_anchor",
                    previous_event_id=previous, same_process_previous_id=process,
                    same_event_with=same or [], process_link="supported"),
                previous_event_id=previous, same_process_previous_id=process)
    if prior is not None:
        item["provided_prior_event_ids"] = prior
    if identity is not None:
        item["provided_identity_event_ids"] = identity
    item.update(kwargs)
    return item


def dependent(eid="E1", previous="E0", **kwargs):
    return event(eid, previous=previous, process=previous, status="CANDIDATE_NEW_EVIDENCE", **kwargs)


class FinalGraphTests(unittest.TestCase):
    def audited(self, items):
        audited, report = audit_final_graph(items)
        return {item["event_id"]: item for item in audited}, report

    def test_CE_AUD001_missing_comparison_demotes_without_inventing_first_anchor(self):
        items, report = self.audited([dependent()])
        self.assertEqual(items["E1"]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")
        self.assertEqual(items["E1"]["semantic_review"]["checkpoint"], "new_evidence")
        self.assertEqual(report["demoted_checkpoint_count"], 1)
        self.assertFalse(relation_guard(items["E1"], "E0", PROCESS, items)["allowed"])

    def test_CE_AUD001_absent_comparison_id_demotes_new_information_candidate(self):
        items, _ = self.audited([dependent(previous=None)])
        self.assertEqual(items["E1"]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")

    def test_CE_AUD001_final_generic_target_blocks_comparison_and_process(self):
        old = event("E0", function="generic", status="REPEATED_OR_EXECUTED_MENTION")
        items, report = self.audited([old, dependent()])
        self.assertEqual(items["E1"]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")
        records = items["E1"]["final_graph_audit"]["dependencies"]
        self.assertTrue(all(not record["allowed"] for record in records))
        self.assertIn("TARGET_FINAL_FUNCTION_GENERIC", records[0]["hard_reasons"])
        self.assertEqual(report["blocked_relation_count"], 1)

    def test_CE_AUD001_final_scope_no_target_blocks_comparison_and_process(self):
        items, _ = self.audited([event("E0", scope="no", status="NOT_RELATED"), dependent()])
        self.assertEqual(items["E1"]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")
        self.assertIn("FINAL_GRAPH_TARGET_FINAL_SCOPE_NO", items["E1"]["validation_reasons"])

    def test_CE_AUD001_semantic_review_final_classification_has_authority(self):
        old = event("E0")
        old["semantic_review"]["record_function"] = "generic"
        items, _ = self.audited([old, dependent()])
        self.assertEqual(items["E1"]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")

    def test_CE_AUD001_repeat_execution_and_noncheckpoint_can_carry_prior_judgment(self):
        for function, status in [("repeat", "REPEATED_OR_EXECUTED_MENTION"),
                                 ("execution", "REPEATED_OR_EXECUTED_MENTION"),
                                 ("new_decision", "UNCERTAIN_CONTRACT_OR_SOURCE"),
                                 ("uncertain", "UNCERTAIN_SEMANTIC_JUDGMENT")]:
            with self.subTest(function=function):
                items, _ = self.audited([event("E0", function=function, status=status), dependent()])
                self.assertEqual(items["E1"]["checkpoint_status"], "CANDIDATE_NEW_EVIDENCE")
                self.assertTrue(relation_guard(items["E1"], "E0", PROCESS, items)["allowed"])
                self.assertTrue(items["E1"]["final_graph_audit"]["soft_reasons"])

    def test_CE_AUD001_verified_same_event_resolves_repeat_carrier_preserving_original_id(self):
        canonical = event("DECISION", identity=["REPEAT"])
        carrier = event("REPEAT", function="repeat", status="REPEATED_OR_EXECUTED_MENTION",
                        same=["DECISION"], identity=["DECISION"])
        current = dependent(previous="REPEAT", prior=["REPEAT", "DECISION"])
        items, _ = self.audited([canonical, carrier, current])
        comparison = relation_guard(items["E1"], "REPEAT", COMPARISON, items)
        self.assertTrue(comparison["allowed"])
        self.assertEqual(comparison["declared_reference_event_id"], "REPEAT")
        self.assertEqual(comparison["resolved_reference_event_id"], "DECISION")
        self.assertEqual(comparison["identity_resolution"]["status"], "resolved")
        self.assertTrue(comparison["identity_resolution"]["supporting_relations"])
        self.assertEqual(items["E1"]["semantic_review"]["previous_event_id"], "REPEAT")

    def test_CE_AUD001_resolved_canonical_must_also_be_a_permitted_historical_reference(self):
        canonical = event("DECISION")
        carrier = event("REPEAT", function="repeat", status="REPEATED_OR_EXECUTED_MENTION",
                        same=["DECISION"], identity=["DECISION"])
        current = dependent(previous="REPEAT", prior=["REPEAT"])
        items, _ = self.audited([canonical, carrier, current])
        guard = relation_guard(items["E1"], "REPEAT", COMPARISON, items)
        self.assertFalse(guard["allowed"])
        self.assertIn("RESOLVED_REFERENCE_TARGET_NOT_IN_PROVIDED_PRIOR_EVENT_IDS", guard["hard_reasons"])
        self.assertEqual(guard["declared_reference_event_id"], "REPEAT")
        self.assertEqual(guard["resolved_reference_event_id"], "DECISION")
        self.assertEqual(items["E1"]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")
        self.assertFalse(relation_guard(items["E1"], "REPEAT", PROCESS, items)["allowed"])

    def test_CE_AUD001_resolved_canonical_must_have_an_earlier_explicit_clinical_time(self):
        canonical = event("DECISION", time_basis="explicit_documented_choice", choice_time="2022-01-03T10:00:00")
        carrier = event("REPEAT", function="repeat", status="REPEATED_OR_EXECUTED_MENTION",
                        same=["DECISION"], identity=["DECISION"])
        current = dependent(previous="REPEAT", prior=["REPEAT", "DECISION"],
                            time_basis="explicit_documented_choice", choice_time="2022-01-02T10:00:00")
        items, _ = self.audited([canonical, carrier, current])
        guard = relation_guard(items["E1"], "REPEAT", COMPARISON, items)
        self.assertFalse(guard["allowed"])
        self.assertIn("RESOLVED_REFERENCE_TARGET_EXPLICIT_CLINICAL_TIME_NOT_EARLIER", guard["hard_reasons"])
        self.assertEqual(guard["identity_resolution"]["status"], "resolved")
        self.assertFalse(guard["identity_resolution"]["canonical_dependency_validation"]["allowed"])

    def test_CE_AUD001_final_semantic_rejection_cannot_be_used_as_explicit_canonical(self):
        canonical = event("DECISION", scope="no")
        carrier = event("REPEAT", function="repeat", status="REPEATED_OR_EXECUTED_MENTION",
                        same=["DECISION"], identity=["DECISION"], canonical_event_id="DECISION")
        items, _ = self.audited([canonical, carrier, dependent(previous="REPEAT", prior=["REPEAT", "DECISION"])])
        guard = relation_guard(items["E1"], "REPEAT", COMPARISON, items)
        self.assertIsNone(guard["identity_resolution"])
        self.assertEqual(guard["resolved_reference_event_id"], "REPEAT")
        self.assertFalse(relation_guard(items["REPEAT"], "DECISION", IDENTITY, items)["allowed"])

    def test_CE_AUD001_resolved_identity_aliases_do_not_hide_a_dependency_cycle(self):
        a = dependent("A", "REPEAT_B")
        b = dependent("B", "REPEAT_A")
        ra = event("REPEAT_A", function="repeat", status="REPEATED_OR_EXECUTED_MENTION", same=["A"])
        rb = event("REPEAT_B", function="repeat", status="REPEATED_OR_EXECUTED_MENTION", same=["B"])
        items, _ = self.audited([a, b, ra, rb])
        for eid, reference in [("A", "REPEAT_B"), ("B", "REPEAT_A")]:
            guard = relation_guard(items[eid], reference, COMPARISON, items)
            self.assertFalse(guard["allowed"])
            self.assertIn("RESOLVED_REFERENCE_DIRECTED_DEPENDENCY_CYCLE", guard["hard_reasons"])
            self.assertEqual(items[eid]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")

    def test_CE_AUD001_unverified_same_event_does_not_authenticate_canonical(self):
        carrier = event("REPEAT", same=["DECISION"], function="repeat",
                        status="REPEATED_OR_EXECUTED_MENTION", canonical_event_id="DECISION")
        carrier["validated_connection_citations"]["valid"] = []
        items, _ = self.audited([event("DECISION"), carrier, dependent(previous="REPEAT")])
        comparison = relation_guard(items["E1"], "REPEAT", COMPARISON, items)
        self.assertEqual(comparison["resolved_reference_event_id"], "REPEAT")
        self.assertIsNone(comparison["identity_resolution"])

    def test_CE_AUD001_multiple_identity_judgments_remain_explicitly_unresolved(self):
        carrier = event("REPEAT", same=["A", "B"], function="repeat",
                        status="REPEATED_OR_EXECUTED_MENTION")
        items, _ = self.audited([event("A"), event("B"), carrier, dependent(previous="REPEAT")])
        comparison = relation_guard(items["E1"], "REPEAT", COMPARISON, items)
        self.assertEqual(comparison["resolved_reference_event_id"], "REPEAT")
        self.assertEqual(comparison["identity_resolution"]["status"], "unresolved_multiple_judgments")
        self.assertIn("SAME_EVENT_CANONICAL_UNRESOLVED", comparison["soft_reasons"])

    def test_CE_AUD001_generic_or_scope_no_cannot_bridge_valid_identity_groups(self):
        for classification in [dict(function="generic"), dict(scope="no")]:
            with self.subTest(classification=classification):
                bridge = event("G", same=["A"], **classification)
                current = event("B", same=["G"])
                items, _ = self.audited([event("A"), bridge, current])
                self.assertFalse(relation_guard(items["G"], "A", IDENTITY, items)["allowed"])
                self.assertFalse(relation_guard(items["B"], "G", IDENTITY, items)["allowed"])

    def test_CE_AUD001_generic_or_scope_no_cannot_bridge_management_processes(self):
        for classification in [dict(function="generic"), dict(scope="no")]:
            with self.subTest(classification=classification):
                items, _ = self.audited([event("A"), event("G", process="A", **classification),
                                        event("B", process="G")])
                self.assertFalse(relation_guard(items["G"], "A", PROCESS, items)["allowed"])
                self.assertFalse(relation_guard(items["B"], "G", PROCESS, items)["allowed"])

    def test_CE_AUD001_directed_comparison_cycle_is_blocked(self):
        items, _ = self.audited([dependent("A", "B"), dependent("B", "A")])
        for eid, target in [("A", "B"), ("B", "A")]:
            self.assertEqual(items[eid]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")
            guard = relation_guard(items[eid], target, COMPARISON, items)
            self.assertIn("DIRECTED_DEPENDENCY_CYCLE", guard["hard_reasons"])

    def test_CE_AUD001_identity_cycles_are_valid_equivalence_and_do_not_imply_temporal_cycle(self):
        items, _ = self.audited([event("A", same=["B"]), event("B", same=["A"])])
        self.assertTrue(relation_guard(items["A"], "B", IDENTITY, items)["allowed"])
        self.assertTrue(relation_guard(items["B"], "A", IDENTITY, items)["allowed"])

    def test_CE_AUD001_process_cycle_does_not_demote_valid_comparison_indiscriminately(self):
        a = dependent("A", "OLD"); a["semantic_review"]["same_process_previous_id"] = "B"
        b = event("B", process="A")
        items, _ = self.audited([event("OLD"), a, b])
        self.assertEqual(items["A"]["checkpoint_status"], "CANDIDATE_NEW_EVIDENCE")
        self.assertFalse(relation_guard(items["A"], "B", PROCESS, items)["allowed"])

    def test_CE_AUD001_noncyclic_edge_between_cycles_not_marked_cyclic(self):
        # Every node is in a cycle but A -> C is not itself in a cycle.
        from scripts.cohort_construction.p2_auto_checkpoint_v04.final_graph import _cyclic_edges
        edges = [("A", "B"), ("B", "A"), ("C", "D"), ("D", "C"), ("A", "C")]
        self.assertNotIn(("A", "C"), _cyclic_edges(edges))

    def test_CE_AUD001_provided_temporal_id_set_is_honored(self):
        items, _ = self.audited([event("E0"), dependent(prior=[])])
        self.assertEqual(items["E1"]["checkpoint_status"], "UNCERTAIN_FINAL_GRAPH")
        self.assertIn("TARGET_NOT_IN_PROVIDED_PRIOR_EVENT_IDS",
                      relation_guard(items["E1"], "E0", COMPARISON, items)["hard_reasons"])

    def test_CE_AUD001_identity_id_permission_is_distinct_from_predecessor_permission(self):
        current = event("E1", same=["SIBLING"], prior=[], identity=["SIBLING"])
        items, _ = self.audited([event("SIBLING"), current])
        self.assertTrue(relation_guard(items["E1"], "SIBLING", IDENTITY, items)["allowed"])
        self.assertFalse(relation_guard(items["E1"], "SIBLING", COMPARISON, items)["allowed"])

    def test_CE_AUD001_explicit_future_time_is_blocked_but_proxy_time_is_not_invented(self):
        for basis, blocked in [("explicit_documented_choice", True), ("record_time_proxy", False)]:
            with self.subTest(basis=basis):
                old = event("E0", time_basis=basis, choice_time="2022-01-03T10:00:00")
                current = dependent(time_basis="explicit_documented_choice", choice_time="2022-01-02T10:00:00")
                items, _ = self.audited([old, current])
                self.assertEqual(not relation_guard(items["E1"], "E0", COMPARISON, items)["allowed"], blocked)

    def test_CE_AUD001_no_order_is_inferred_from_id_or_input_order(self):
        items, _ = self.audited([dependent("A", "Z"), event("Z")])
        self.assertTrue(relation_guard(items["A"], "Z", COMPARISON, items)["allowed"])

    def test_CE_AUD001_relation_guard_does_not_permit_an_undeclared_edge(self):
        items = [event("A"), event("B")]
        guard = relation_guard(items[0], "B", PROCESS, items)
        self.assertFalse(guard["allowed"])
        self.assertIn("RELATION_NOT_DECLARED_BY_MODEL", guard["hard_reasons"])

    def test_CE_AUD001_input_and_semantic_review_are_preserved_and_audit_is_idempotent(self):
        original = [event("E0", function="generic"), dependent()]
        snapshot = deepcopy(original)
        audited, report = audit_final_graph(original)
        self.assertEqual(original, snapshot)
        self.assertEqual([item["semantic_review"] for item in audited],
                         [item["semantic_review"] for item in snapshot])
        again, again_report = audit_final_graph(audited)
        self.assertEqual(again, audited)
        self.assertEqual(again_report, report)
        self.assertEqual(audited[1]["final_graph_audit"]["before"]["checkpoint_status"], "CANDIDATE_NEW_EVIDENCE")

    def test_CE_AUD001_duplicate_event_id_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "DUPLICATE_EVENT_ID"):
            audit_final_graph([event("A"), event("A")])


if __name__ == "__main__":
    unittest.main()

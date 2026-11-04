"""RG counterexamples are synthetic, never clinical gold."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cohort_construction.model_api import load_boyu_settings, read_env_file
from scripts.cohort_construction.p2_relation_graph.core import (
    FIELDS, SourceStore, assess_relation, candidates, normalize, resolve, validate_graph)
from scripts.cohort_construction.p2_relation_graph.demo import data
from scripts.cohort_construction.p2_relation_graph.evaluate import evaluate
from scripts.cohort_construction.p2_relation_graph.model import SemanticClient, ModelFailure
from scripts.cohort_construction.p2_relation_graph.pipeline import (
    context_views, provisional_mentions, reconstruct)


def response(relation="uncertain", purposes=()):
    return {"relation": relation, "same_management_question": "yes",
            "same_decision_event": "yes" if relation == "same_event" else "no",
            "new_decision_relevant_evidence": "uncertain", "current_action_relation": "same",
            "supports": [{"purpose": p, "span": {"source": "S0", "quote": "拟行PTCD"}} for p in purposes]}


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.notes, self.units = data()
        self.store = SourceStore(self.notes)
        self.mentions = [m for m in provisional_mentions(self.units) if m["kind"] == "DecisionMention"]
        for i, m in enumerate(self.mentions):
            m["id"] = f"m{i}"
            m["temporality"] = "current"
        self.byid = {m["id"]: m for m in self.mentions}
        self.views = [self.store.view("SYNTHETIC_ONLY", "fictional-0")]
        self.pair = {"id": "p", "from": "m0", "to": "m1", "stage": "decision_relation"}

    def edge(self, rel, a="m0", b="m1"):
        return {"id": a+b+rel, "from": a, "to": b, "relation": rel, "proposed_relation": rel,
                "stage": "coreference" if rel == "same_event" else "decision_relation",
                "issues": [], "supports": [], "status": "REVIEW", "score": 0.5}

    def test_RG001_source_hash_and_span_must_match(self):
        self.store.verify_unit(self.units[0])
        wrong = copy.deepcopy(self.units[0]); wrong["quote"] = "已行ERCP"
        with self.assertRaises(ValueError): self.store.verify_unit(wrong)

    def test_RG002_ambiguous_and_fabricated_support_rejected(self):
        v = {**self.views[0], "text": "拟行PTCD；拟行PTCD", "start": 0, "end": 13}
        with self.assertRaises(ValueError): resolve({"source": "S0", "quote": "拟行PTCD"}, [v])
        with self.assertRaises(ValueError): resolve({"source": "S0", "quote": "已行ERCP"}, self.views)
        self.assertEqual(resolve({"source": "S0", "quote": "拟行PTCD"}, self.views)["quote"], "拟行PTCD")

    def test_RG003_absence_is_not_negative_or_identity(self):
        r = response("same_event")
        e = assess_relation({**self.pair, "stage": "coreference"}, r, self.byid, self.views)
        self.assertEqual(e["relation"], "uncertain")
        self.assertIn("COREFERENCE_NEEDS_EXPLICIT_EVENT_REFERENCE", e["issues"])
        r = response("same_event", ["same_event_reference"])
        e = assess_relation({**self.pair, "stage": "coreference"}, r, self.byid, self.views)
        self.assertEqual(e["relation"], "same_event")
        self.assertEqual(e["status"], "REVIEW")
        self.assertIsNone(e["confidence"])

    def test_RG004_current_plan_is_not_execution(self):
        self.byid["m1"]["kind"] = "ActionMention"
        for m in self.byid.values(): m["signature"]["intended_action"] = {"state": "stated", "value": "PTCD"}
        r = response("implemented_by", ["implementation"])
        p = {**self.pair, "stage": "typed_relation"}
        self.byid["m1"]["temporality"] = "future_plan"
        self.assertEqual(assess_relation(p, r, self.byid, self.views)["relation"], "uncertain")
        self.byid["m1"]["temporality"] = "current"
        self.assertEqual(assess_relation(p, r, self.byid, self.views)["relation"], "implemented_by")

    def test_RG005_wrong_action_implementation_rejected(self):
        self.byid["m1"]["kind"] = "ActionMention"
        for i, a in [("m0", "PTCD"), ("m1", "ERCP")]:
            self.byid[i]["signature"]["intended_action"] = {"state": "stated", "value": a}
        e = assess_relation({**self.pair, "stage": "typed_relation"}, response("implemented_by", ["implementation"]), self.byid, self.views)
        self.assertIn("ACTION_COMPATIBILITY_NOT_ESTABLISHED", e["issues"])

    def test_RG006_reaffirm_requires_positive_evidence(self):
        self.assertEqual(assess_relation(self.pair, response("reaffirm"), self.byid, self.views)["relation"], "uncertain")
        self.assertEqual(assess_relation(self.pair, response("reaffirm", ["reassessment"]), self.byid, self.views)["relation"], "reaffirm")

    def test_RG007_revision_and_supersession_are_distinct(self):
        r = response("revise", ["changed_condition"])
        self.assertEqual(assess_relation(self.pair, r, self.byid, self.views)["relation"], "revise")
        r["relation"] = "supersede"
        self.assertEqual(assess_relation(self.pair, r, self.byid, self.views)["relation"], "uncertain")
        r["supports"].append({"purpose": "replacement", "span": {"source": "S0", "quote": "拟行PTCD"}})
        self.assertEqual(assess_relation(self.pair, r, self.byid, self.views)["relation"], "supersede")

    def test_RG008_unknown_or_same_proxy_time_cannot_order_events(self):
        for time in [None, "2025-01-01", self.byid["m0"]["ordering_time"]]:
            self.byid["m1"]["ordering_time"] = time
            e = assess_relation(self.pair, response("reaffirm", ["reassessment"]), self.byid, self.views)
            self.assertEqual(e["relation"], "uncertain")
        # Same time can still generate a symmetric coreference review pair.
        self.assertTrue(candidates(self.mentions, stage="coreference")[0])
        self.assertFalse(candidates(self.mentions, stage="decision_relation")[0])

    def test_RG009_cross_patient_never_connected(self):
        self.byid["m1"]["patient_uid"] = "other"
        self.assertFalse(candidates(self.mentions, stage="coreference")[0])
        with self.assertRaises(ValueError): assess_relation(self.pair, response(), self.byid, self.views)
        with self.assertRaises(ValueError): validate_graph(self.mentions, [self.edge("revise")])

    def test_RG010_topk_bounded_deterministic_unknown_question_retained(self):
        many = []
        for i in range(8):
            m = copy.deepcopy(self.mentions[0]); m["id"] = str(i); m["ordering_time"] = f"2025-01-0{i+1}T02:00:00+00:00"; many.append(m)
        pairs, omitted = candidates(many, stage="decision_relation", top_k=2)
        self.assertLessEqual(len(pairs), 2*len(many))
        self.assertGreater(omitted["top_k"], 0)
        self.assertEqual(candidates(list(reversed(many)), stage="decision_relation", top_k=2)[0], pairs)

    def test_RG011_zero_hit_intervening_source_included_future_excluded(self):
        mid = {**self.notes[0], "record_key": "zero", "text": "检查结果已返回，需结合临床。", "body_time": "2025-01-01 18:00:00"}
        future = {**mid, "record_key": "future", "body_time": "2025-01-04 18:00:00"}
        views, coverage = context_views(SourceStore(self.notes+[mid, future]), *self.mentions)
        ids = {v["source_id"] for v in views}
        self.assertIn("zero", ids); self.assertNotIn("future", ids)
        self.assertTrue(coverage["complete_intervening_proxy_window"])

    def test_RG012_context_truncation_and_unknown_time_are_explicit(self):
        unknown = {**self.notes[0], "record_key": "unknown", "body_time": None, "system_time": None}
        _, coverage = context_views(SourceStore(self.notes+[unknown]), *self.mentions, max_chars=1)
        self.assertFalse(coverage["complete_intervening_proxy_window"])
        self.assertEqual(coverage["unknown_time_source_count"], 1)
        self.assertGreater(coverage["omitted_source_count"], 0)

    def test_RG013_transitive_coreference_conflict_blocks_merge(self):
        third = copy.deepcopy(self.mentions[1]); third["id"] = "m2"
        graph = validate_graph(self.mentions+[third], [self.edge("same_event"), self.edge("same_event", "m1", "m2"), self.edge("revise", "m0", "m2")])
        self.assertEqual(len(graph["events"]), 3)
        self.assertTrue(graph["issues"])
        self.assertFalse(any(e["relation"] == "same_event" for e in graph["edges"]))
        good = validate_graph(self.mentions, [self.edge("same_event")])
        self.assertEqual(len(good["events"]), 1)
        self.assertFalse(good["events"][0]["confirmed"])

    def test_RG014_contracted_event_cycles_are_flagged(self):
        third = copy.deepcopy(self.mentions[1]); third["id"] = "m2"
        graph = validate_graph(self.mentions+[third], [self.edge("same_event", "m0", "m2"), self.edge("revise"), self.edge("revise", "m1", "m2")])
        self.assertTrue(any(i["code"] == "EVENT_TEMPORAL_CYCLE" for i in graph["issues"]))

    def test_RG015_normalization_splits_roles_without_changing_source(self):
        old = copy.deepcopy(self.notes)
        ref = {"source": "S0", "quote": "拟行PTCD"}
        signature = {f: {"state": "not_mentioned", "value": None, "supports": []} for f in FIELDS}
        signature["intended_action"] = {"state": "stated", "value": "PTCD", "supports": [ref]}
        item = {"kind": "DecisionMention", "anchor": ref, "signature": signature,
                "temporality": "future_plan", "temporality_supports": [ref]}
        result = normalize(self.units[0], {"mentions": [item]}, self.views)
        self.assertEqual(result[0]["signature"]["goal"]["state"], "not_mentioned")
        self.assertEqual(self.notes, old)
        item["signature"]["goal"]["value"] = "根治"
        with self.assertRaises(ValueError): normalize(self.units[0], {"mentions": [item]}, self.views)

    def test_RG016_failed_semantics_preserve_units_and_review(self):
        class Broken:
            receipts = []
            def ask(self, *args): raise RuntimeError("synthetic error")
        g = reconstruct(self.units, self.notes, client=Broken())
        self.assertEqual(len(g["normalization_ledger"]), len(self.units))
        self.assertEqual(g["summary"]["normalization_statuses"], {"NORMALIZATION_FAILED_REVIEW": 2})
        self.assertFalse(g["auto_accept_enabled"])
        self.assertIsNone(g["confirmed_decision_count"])

    def test_RG017_prepare_mode_cannot_claim_semantic_results(self):
        g = reconstruct(self.units, self.notes)
        self.assertEqual(g["mode"], "PREPARE_ONLY")
        self.assertTrue(all(e["relation"] == "uncertain" for e in g["edges"]))
        self.assertTrue(all(m["status"] == "LEXICAL_WRAPPER_NOT_NORMALIZED" for m in g["mentions"]))

    def test_RG018_metrics_include_unproposed_gold_edges(self):
        g = reconstruct(self.units, self.notes)
        decision = [m["id"] for m in g["mentions"] if m["kind"] == "DecisionMention"]
        gold = {"annotation_status": "ADJUDICATED", "complete_within_declared_scope": True,
                "origin": "synthetic_fixture", "scope": {"kind": "aligned_mentions", "mention_ids": [m['id'] for m in g['mentions']]+['missed']}, "annotator_ids": ["fixture-author"],
                "mentions": [{"id": i, "kind": "DecisionMention"} for i in decision+['missed']],
                "mention_to_event": {decision[0]: "e1", decision[1]: "e2", "missed": "e3"},
                "relations": [{"stage": "decision_relation", "from": decision[0], "to": "missed", "relation": "revise"}]}
        score = evaluate(g, gold)
        self.assertEqual(score["candidate_edge_recall"], 0)
        self.assertEqual(score["typed_relation_edges"]["recall"], 0)
        self.assertFalse(score["scope_graph_complete"])
        self.assertFalse(score["auto_accept_enabled"])
        gold["origin"] = "model"
        with self.assertRaises(ValueError): evaluate(g, gold)

    def test_RG019_literal_credentials_and_secret_repr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/"config").mkdir()
            (root/"config/model_provider.json").write_text('{"provider":"boyu","model":"kimi-k3"}')
            credential = root/"credential"
            credential.write_text('export BOYU_API_KEY="synthetic-key"\nexport BOYU_API_BASE="https://example.invalid/v1/"\nBAD=$(touch SHOULD_NOT_EXIST)\n')
            settings = load_boyu_settings(root, environ={}, credential_file=credential)
            self.assertEqual(settings.model, "kimi-k3")
            self.assertNotIn("synthetic-key", repr(settings))
            self.assertNotIn("BAD", read_env_file(credential))
            with self.assertRaises(ValueError): load_boyu_settings(root, environ={"BOYU_API_BASE": "http://example.invalid"}, credential_file=credential)

    def test_RG020_transport_rejects_truncation_and_caches_without_key(self):
        from scripts.cohort_construction.model_api import ModelAPISettings
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            client = SemanticClient(Path(tmp), settings=ModelAPISettings("synthetic-key", "https://example.invalid/v1", "kimi-k3"))
            body = {"model": "kimi-k3", "choices": [{"finish_reason": "length", "message": {"content": '{"ok":true}'}}]}
            mock = Mock(status_code=200); mock.json.return_value = body
            with patch("requests.post", return_value=mock) as post:
                with self.assertRaises(ModelFailure): client.ask("fixture", {}, {"type": "object"})
                body["choices"][0]["finish_reason"] = "stop"
                self.assertEqual(client.ask("fixture", {}, {"type": "object"}), {"ok": True})
                self.assertEqual(client.ask("fixture", {}, {"type": "object"}), {"ok": True})
                self.assertEqual(post.call_count, 2)
            for file in Path(tmp).glob("*.json"):
                self.assertNotIn("synthetic-key", file.read_text())

    def test_RG021_model_does_not_compute_unicode_offsets(self):
        from scripts.cohort_construction.p2_relation_graph.core import REF
        from jsonschema import Draft202012Validator, ValidationError
        ref = {"source": "S0", "quote": "拟行PTCD"}
        Draft202012Validator(REF).validate(ref)
        anchor = resolve(ref, self.views)
        self.assertEqual(self.notes[0]["text"][slice(*anchor["span"])], "拟行PTCD")
        # Real synthetic API run emitted an off-by-one PTCD end coordinate.
        # The model contract now excludes computed coordinates entirely.
        with self.assertRaises(ValidationError): Draft202012Validator(REF).validate({**ref, "start": 8, "end": 13})
        with self.assertRaises(ValueError): resolve({**ref, "start": 8}, self.views)

    def test_RG022_event_context_keeps_all_members(self):
        third = {**self.notes[0], "record_key": "third", "body_time": "2025-01-03 10:00:00"}
        extra = copy.deepcopy(self.mentions[1]); extra["anchor"]["source_id"] = "third"
        extra["ordering_time"] = "2025-01-03T02:00:00+00:00"
        views, _ = context_views(SourceStore(self.notes+[third]), self.mentions[1], extra, event_members=self.mentions+[extra])
        self.assertEqual({v["source_id"] for v in views}, {"fictional-0", "fictional-1", "third"})

    def test_RG023_relation_and_identity_fields_cannot_contradict(self):
        r = response("different_event", ["reassessment"]); r["same_decision_event"] = "yes"
        e = assess_relation({**self.pair, "stage": "coreference"}, r, self.byid, self.views)
        self.assertEqual(e["relation"], "uncertain")
        r = response("unrelated", ["different_question"])
        self.assertEqual(assess_relation(self.pair, r, self.byid, self.views)["relation"], "uncertain")
        r["same_management_question"] = "no"
        self.assertEqual(assess_relation(self.pair, r, self.byid, self.views)["relation"], "unrelated")

    def test_RG024_atomicity_and_cue_coverage_not_silently_certified(self):
        from scripts.cohort_construction.p2_relation_graph.pipeline import normalization_review_flags
        flags = normalization_review_flags(self.units[1], self.mentions)
        self.assertIn("MULTIPLE_JUDGMENTS_ATOMICITY_REVIEW", flags)
        self.assertIn("FEEDBACK_CUE_WITHOUT_TYPED_MENTION_REVIEW", flags)
        feedback = {"kind": "FeedbackMention"}
        flags = normalization_review_flags(self.units[1], [self.mentions[1], feedback])
        self.assertFalse(flags)

    def test_RG025_typed_event_endpoints_are_explicit_nodes(self):
        g = reconstruct(self.units, self.notes)
        ids = {e["id"] for e in g["events"]+g["auxiliary_events"]}
        self.assertIn("FeedbackEventHypothesis", {e["kind"] for e in g["auxiliary_events"]})
        self.assertTrue(all(e["from_event"] in ids and e["to_event"] in ids for e in g["edges"]))
        self.assertFalse(any(e["confirmed"] for e in g["auxiliary_events"]))


if __name__ == "__main__":
    unittest.main()

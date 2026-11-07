"""Fictional engine authority/visibility controls; no API or clinical labels."""

from copy import deepcopy
import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine
from scripts.cohort_construction.p2_auto_checkpoint_v04.validation import resolve_citations
from scripts.cohort_construction.p2_pathway_replay.core import clock


def source(identity="D", text="探查：静脉受侵。遂决定切除重建。操作：完成吻合。", when="2022-01-02 10:00"):
    return {"source_id": identity, "text": text,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "kind": "document", "title": "手术记录" if identity == "D" else "查房记录",
            "clock": clock(when), "clock_basis": "body_record_time", "clocks": {}, "provenance": {}}


def quotes(s=None, quote="静脉受侵"):
    s = s or source()
    return resolve_citations([{"source_id": s["source_id"], "quote": quote}], [s])


def empty_quotes():
    return {"valid": [], "ambiguous": [], "invalid": []}


def event(identity="E1", s=None, question="术中是否联合静脉切除", action="遂决定切除重建。", **extra):
    s = s or source()
    return {"event_id": identity, "source_id": s["source_id"], "source_ids": [s["source_id"]],
            "unit_id": "U" + s["source_id"], "kind": "document", "document_profile": "operative",
            "scope": "yes", "record_function": "new_decision", "management_question": question,
            "action": action, "choice_time": None, "time_basis": "operative_phase",
            "phase": "探查后、切除前", "source_document_clock": s["clock"],
            "discovery_source_supported": True, "action_citations": quotes(s, action),
            "prechoice_citations": quotes(s), "postchoice_citations": empty_quotes(),
            "checkpoint_status": "CANDIDATE_NEW_EVIDENCE", "uncertainty": [], **extra}


def review(identity="E1", **extra):
    return {"event_id": identity, "scope": "yes", "record_function": "new_decision",
            "checkpoint": "new_evidence", "previous_event_id": "E0", "same_event_with": [],
            "same_process_previous_id": "E0", "process_link": "supported",
            "evidence_evolution": "new_relevant", "observation_quotes": [], "connection_quotes": [],
            "rationale": "虚构：探查后明确决定", "uncertainty": [], **extra}


def select(e=None, r=None, observations=None, connections=None, earlier_ids=None, identity_ids=None):
    kwargs = {}
    if "identity_ids" in inspect.signature(engine.select_status).parameters:
        kwargs["identity_ids"] = identity_ids or {"E1", "E2"}
    return engine.select_status(e or event(), r or review(), observations if observations is not None else quotes(),
                                connections if connections is not None else quotes(quote="静脉受侵。遂决定切除重建。"),
                                earlier_ids if earlier_ids is not None else {"E0"}, **kwargs)


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def ask(self, task, payload, schema):
        self.requests.append({"task": task, "payload": deepcopy(payload), "schema": schema})
        return deepcopy(self.result), {"request_sha256": "fictional-request", "status": "SUCCESS"}


def integration_fixture(review_result):
    # This fixture tests quotation roles, with an independently documented
    # operation interval so a prior round is actually temporally eligible.
    current = source(text="手术时间：2022-01-02 08:00-2022-01-02 12:00\n探查：静脉受侵。遂决定切除重建。操作：完成吻合。")
    previous = source("P", "此前拟行手术。", "2022-01-01 09:00")
    future = source("F", "以后复查发现异常。", "2022-01-03 09:00")
    e = event(s=current)
    old = event("E0", previous, question="术前安排", action="此前拟行手术。",
                time_basis="record_time_proxy", phase="术前", prechoice_citations=empty_quotes())
    # Use the real context return contract, including source ledgers, priorities
    # and coverage uncertainty; only the provider is replaced by a fake.
    context = engine.build_evidence_context(e, [previous, current, future], [old, e], budget=10000)
    unit = {"unit_id": e["unit_id"], "source_ids": ["D"], "clock": current["clock"],
            "document_profile": "operative", "type_prior": "探查后可能有选择",
            "original_texts": [{"source_id": "D", "text": current["text"]}]}
    client = FakeClient({"items": [review_result]})
    return current, e, [previous, current, future], old, context, unit, client


class EngineAuthorityTests(unittest.TestCase):
    def test_ENG001_semantic_scope_veto_overrides_discovery_positive(self):
        status, _ = select(r=review(scope="no"))
        self.assertEqual(status, "NOT_RELATED")

    def test_ENG001_semantic_scope_can_restore_rule_discovery_rejection(self):
        status, _ = select(e=event(scope="no"), r=review(scope="yes"))
        self.assertEqual(status, "CANDIDATE_NEW_EVIDENCE")

    def test_ENG002_semantic_repeat_does_not_require_fabricated_new_evidence(self):
        status, reasons = select(r=review(checkpoint="repeat", record_function="repeat"),
                                 observations=empty_quotes(), connections=empty_quotes())
        self.assertEqual(status, "REPEATED_OR_EXECUTED_MENTION")
        self.assertNotIn("NO_VALID_PRIOR_OBSERVATION", reasons)

    def test_ENG002_semantic_uncertain_is_not_upgraded_by_valid_quotes(self):
        status, _ = select(r=review(checkpoint="uncertain"))
        self.assertEqual(status, "UNCERTAIN_SEMANTIC_JUDGMENT")

    def test_ENG003_no_source_backed_observation_cannot_be_new_checkpoint(self):
        invalid = resolve_citations([{"source_id": "D", "quote": "不存在的发现"}], [source()])
        status, reasons = select(observations=invalid)
        self.assertTrue(status.startswith("UNCERTAIN"))
        self.assertIn("NO_VALID_PRIOR_OBSERVATION", reasons)

    def test_ENG003_bad_secondary_quote_does_not_erase_other_valid_observation(self):
        citations = resolve_citations([{"source_id": "D", "quote": "静脉受侵"},
                                       {"source_id": "D", "quote": "不存在的发现"}], [source()])
        status, reasons = select(observations=citations)
        self.assertEqual(status, "CANDIDATE_NEW_EVIDENCE")
        self.assertEqual(reasons, [])
        self.assertEqual(len(citations["invalid"]), 1)

    def test_ENG004_unsupported_actual_action_cannot_be_accepted(self):
        status, reasons = select(e=event(discovery_source_supported=False))
        self.assertTrue(status.startswith("UNCERTAIN"))
        self.assertIn("CURRENT_ACTION_SOURCE_UNVERIFIED", reasons)

    def test_ENG005_model_can_choose_different_valid_predecessor(self):
        e = event(previous_event_id="PROGRAM_WRONG_PREDECESSOR")
        status, _ = select(e=e, r=review(previous_event_id="E_ALTERNATIVE", same_process_previous_id="E_ALTERNATIVE"),
                           earlier_ids={"E0", "E_ALTERNATIVE"})
        self.assertEqual(status, "CANDIDATE_NEW_EVIDENCE")

    def test_ENG006_unknown_or_future_predecessor_id_is_rejected(self):
        for identity in ["NOT_PROVIDED", "FUTURE_EVENT"]:
            with self.subTest(identity=identity):
                status, reasons = select(r=review(previous_event_id=identity))
                self.assertTrue(status.startswith("UNCERTAIN"))
                self.assertIn("UNPROVIDED_OR_NONPRIOR_PREVIOUS_EVENT_ID", reasons)

    def test_ENG006_unknown_process_predecessor_is_rejected(self):
        status, reasons = select(r=review(same_process_previous_id="NOT_PROVIDED"))
        self.assertTrue(status.startswith("UNCERTAIN"))
        self.assertIn("UNPROVIDED_OR_NONPRIOR_SAME_PROCESS_PREVIOUS_ID", reasons)

    def test_ENG007_sibling_identity_allowed_without_becoming_prior_round(self):
        status, _ = select(r=review(same_event_with=["E2"]), identity_ids={"E1", "E2"})
        self.assertEqual(status, "CANDIDATE_NEW_EVIDENCE")

    def test_ENG007_sibling_is_not_accepted_as_earlier_decision(self):
        # Old caller mixed sibling ids into earlier_ids. New identity_ids keeps
        # same-document identity alternatives separate from genuine prior rounds.
        kwargs = {"earlier_ids": {"E0"}, "identity_ids": {"E1", "E2"}}
        if "identity_ids" not in inspect.signature(engine.select_status).parameters:
            kwargs["earlier_ids"] |= kwargs["identity_ids"]
        status, reasons = select(r=review(previous_event_id="E2"), **kwargs)
        self.assertTrue(status.startswith("UNCERTAIN"))
        self.assertIn("UNPROVIDED_OR_NONPRIOR_PREVIOUS_EVENT_ID", reasons)

    def test_ENG008_report_execution_can_be_uncertain_time_process_anchor(self):
        e = event(kind="performed_test_report", record_function="execution", time_basis="historical_unknown")
        r = review(checkpoint="first_anchor", record_function="execution", previous_event_id=None,
                   same_process_previous_id=None, evidence_evolution="uncertain", process_link="uncertain")
        status, reasons = select(e=e, r=r, observations=empty_quotes(), connections=empty_quotes())
        self.assertEqual(status, "CANDIDATE_TEST_ANCHOR_TIME_UNCERTAIN")
        self.assertEqual(reasons, [])

    def test_ENG008_execution_is_not_new_evidence_checkpoint(self):
        status, reasons = select(r=review(record_function="execution"))
        self.assertTrue(status.startswith("UNCERTAIN"))
        self.assertIn("MODEL_EVENT_FUNCTION_CHECKPOINT_CONFLICT", reasons)

    def test_ENG009_new_round_requires_prior_identity_and_connection(self):
        status, reasons = select(r=review(previous_event_id=None), connections=empty_quotes())
        self.assertTrue(status.startswith("UNCERTAIN"))
        self.assertIn("PRIOR_ROUND_UNRESOLVED", reasons)
        self.assertIn("NO_VERIFIED_CONNECTION_QUOTE", reasons)


class ManagementProcessTests(unittest.TestCase):
    def linked(self, link="supported"):
        a = event("E0", question="术前是否安排切除")
        a["semantic_review"] = review("E0", previous_event_id=None, same_process_previous_id=None)
        a["validated_connection_citations"] = empty_quotes()
        b = event("E1", question="探查后是否联合静脉切除")
        b["semantic_review"] = review("E1", same_process_previous_id="E0", process_link=link)
        b["validated_connection_citations"] = quotes(quote="静脉受侵。遂决定切除重建。")
        return [a, b]

    def test_ENG010_different_subquestions_can_share_supported_management_process(self):
        events = self.linked()
        episodes, links = engine.assemble_processes(events)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(set(episodes[0]["candidate_ids"]), {"E0", "E1"})
        self.assertEqual(len(episodes[0]["management_questions"]), 2)
        self.assertEqual(links[0]["status"], "supported")

    def test_ENG010_context_only_does_not_force_process_merge(self):
        episodes, links = engine.assemble_processes(self.linked("context_only"))
        self.assertEqual(len(episodes), 2)
        self.assertEqual(links[0]["status"], "uncertain")

    def test_ENG011_unverified_connection_does_not_force_process_merge(self):
        events = self.linked()
        events[1]["validated_connection_citations"] = empty_quotes()
        episodes, links = engine.assemble_processes(events)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(links[0]["status"], "uncertain")

    def test_ENG011_generic_only_does_not_create_episode_even_with_candidate_flag(self):
        e = event(record_function="generic")
        e["semantic_review"] = review(previous_event_id=None, same_process_previous_id=None)
        e["validated_connection_citations"] = empty_quotes()
        episodes, _ = engine.assemble_processes([e])
        self.assertEqual(episodes, [])


class MappingAndReviewTests(unittest.TestCase):
    def test_ENG012_masked_offsets_map_to_exact_raw_without_rewriting(self):
        s = source(text="姓名张三；静脉受侵，遂决定切除。")
        citations = engine.resolve_model_quotes([{"source_id": "D", "quote": "姓名**；静脉受侵"}], [s], ["张三"])
        item = citations["valid"][0]
        self.assertEqual(item["quote"], "姓名张三；静脉受侵")
        self.assertEqual(item["source_sha256"], s["source_sha256"])
        self.assertEqual(s["text"][item["start"]:item["end"]], item["quote"])
        self.assertEqual(item["submitted_masked_quote"], "姓名**；静脉受侵")

    def test_ENG012_hidden_different_names_preserve_all_occurrences(self):
        s = source(text="张三：稳定。李四：稳定。")
        result = engine.resolve_model_quotes([{"source_id": "D", "quote": "**：稳定。"}], [s], ["张三", "李四"])
        self.assertEqual(result["valid"], [])
        item = result["ambiguous"][0]
        self.assertEqual(item["status"], "AMBIGUOUS_SPAN")
        self.assertEqual([x["quote"] for x in item["occurrences"]], ["张三：稳定。", "李四：稳定。"])
        self.assertIsNone(item["quote"])
        self.assertNotIn("start", item)

    def test_ENG013_connection_can_include_action_but_observation_cannot(self):
        r = review(observation_quotes=[{"source_id": "D", "quote": "静脉受侵"},
                                       {"source_id": "D", "quote": "遂决定切除重建。"}],
                   connection_quotes=[{"source_id": "D", "quote": "静脉受侵。遂决定切除重建。"}])
        _, e, sources, old, context, unit, client = integration_fixture(r)
        with patch.object(engine, "build_evidence_context", return_value=context):
            result = engine.review_unit(unit, [e], [old, e], sources, {"review_context_characters": 10000}, client, [])
        out = result["events"][0]
        self.assertEqual(out["checkpoint_status"], "CANDIDATE_NEW_EVIDENCE")
        self.assertEqual(len(out["validated_observation_citations"]["valid"]), 1)
        self.assertEqual(out["validated_observation_citations"]["invalid"][0]["status"], "QUOTE_OUTSIDE_ALLOWED_SPANS")
        self.assertEqual(len(out["validated_connection_citations"]["valid"]), 1)
        self.assertTrue(client.requests[0]["payload"]["reconstruction_only_actual_choices_disclosed"])

    def test_ENG013_action_only_observation_cannot_establish_new_evidence(self):
        r = review(observation_quotes=[{"source_id": "D", "quote": "遂决定切除重建。"}],
                   connection_quotes=[{"source_id": "D", "quote": "静脉受侵。遂决定切除重建。"}])
        _, e, sources, old, context, unit, client = integration_fixture(r)
        with patch.object(engine, "build_evidence_context", return_value=context):
            result = engine.review_unit(unit, [e], [old, e], sources, {"review_context_characters": 10000}, client, [])
        out = result["events"][0]
        self.assertTrue(out["checkpoint_status"].startswith("UNCERTAIN"))
        self.assertIn("NO_VALID_PRIOR_OBSERVATION", out["validation_reasons"])

    def test_ENG014_future_fact_and_after_choice_execution_rejected_individually(self):
        r = review(observation_quotes=[{"source_id": "D", "quote": "静脉受侵"},
                                       {"source_id": "F", "quote": "以后复查发现异常。"}],
                   connection_quotes=[{"source_id": "D", "quote": "静脉受侵。遂决定切除重建。"},
                                      {"source_id": "D", "quote": "操作：完成吻合。"}])
        _, e, sources, old, context, unit, client = integration_fixture(r)
        with patch.object(engine, "build_evidence_context", return_value=context):
            result = engine.review_unit(unit, [e], [old, e], sources, {"review_context_characters": 10000}, client, [])
        out = result["events"][0]
        self.assertEqual(out["checkpoint_status"], "CANDIDATE_NEW_EVIDENCE")
        self.assertEqual(out["citation_warnings"]["observation_invalid"], 1)
        self.assertEqual(out["citation_warnings"]["connection_invalid"], 1)

    def test_ENG015_final_review_can_veto_discovery_positive_with_record(self):
        r = review(scope="no", checkpoint="not_related", record_function="generic")
        _, e, sources, old, context, unit, client = integration_fixture(r)
        with patch.object(engine, "build_evidence_context", return_value=context):
            result = engine.review_unit(unit, [e], [old, e], sources, {"review_context_characters": 10000}, client, [])
        out = result["events"][0]
        self.assertEqual(out["scope"], "no")
        self.assertEqual(out["record_function"], "generic")
        self.assertEqual(out["checkpoint_status"], "NOT_RELATED")
        self.assertEqual(out["semantic_review"]["rationale"], r["rationale"])

    def test_ENG016_provider_budget_removes_whole_source_and_updates_actual_ledger(self):
        r = review(observation_quotes=[{"source_id": "D", "quote": "静脉受侵"},
                                       {"source_id": "BULK", "quote": "独有证据"}],
                   connection_quotes=[{"source_id": "D", "quote": "静脉受侵。遂决定切除重建。"}])
        _, e, sources, old, _, unit, _ = integration_fixture(r)
        e.update(time_basis="record_time_proxy", phase="当前评估")
        bulk = source("BULK", "完整原始段落。" * 2000 + "独有证据", "2022-01-01 08:00")
        sources.append(bulk)
        context = engine.build_evidence_context(e, sources, [old, e], budget=100000)
        baseline = FakeClient({"items": [r]})
        with patch.object(engine, "build_evidence_context", return_value=deepcopy(context)):
            engine.review_unit(unit, [e], [old, e], sources,
                               {"review_context_characters": 100000, "max_review_payload_characters": 1000000}, baseline, [])
        full_payload = baseline.requests[0]["payload"]
        limit = len(json.dumps(full_payload, ensure_ascii=False)) - len(bulk["text"]) + 2500
        client = FakeClient({"items": [r]})
        with patch.object(engine, "build_evidence_context", return_value=deepcopy(context)):
            result = engine.review_unit(unit, [e], [old, e], sources,
                                        {"review_context_characters": 100000, "max_review_payload_characters": limit}, client, [])
        out = result["events"][0]
        self.assertEqual(out["checkpoint_status"], "CANDIDATE_NEW_EVIDENCE")
        ctx = out["evidence_context"]
        self.assertNotIn("BULK", {block["source_id"] for block in ctx["blocks"]})
        omitted = [entry for entry in ctx["ledger"] if entry["source_id"] == "BULK"
                   and entry["disposition"] == "omitted_provider_payload_budget"]
        self.assertTrue(omitted)
        self.assertTrue(omitted[0]["whole_source_not_truncated"])
        self.assertFalse(ctx["uncertainty"]["source_coverage_complete"])
        self.assertGreaterEqual(ctx["uncertainty"]["omitted_budget_count"], 1)
        self.assertEqual(out["actual_model_evidence"], ctx["blocks"])
        sent = client.requests[0]["payload"]
        self.assertLessEqual(len(json.dumps(sent, ensure_ascii=False)), limit)
        self.assertEqual({block["source_id"] for block in sent["evidence_source_pool"]},
                         {block["source_id"] for block in out["actual_model_evidence"]})
        self.assertEqual(out["validated_observation_citations"]["invalid"][0]["source_id"], "BULK")
        # The actual v04 disposition must survive into material review. It is
        # not an assertion that this full original was absent or irrelevant.
        from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
            freeze_candidate_graph, normalize_sources, prepare_checkpoint_task,
        )
        from scripts.cohort_construction.p2_checkpoint_material_governance.review import build_review_request
        graph = freeze_candidate_graph([out], [], [dict(episode_id='EP-BUDGET',
            candidate_ids=[out['event_id']], checkpoint_ids=[out['event_id']])],
            graph_version='SYNTHETIC-PROVIDER-BUDGET')
        task = prepare_checkpoint_task(graph, normalize_sources(sources), out['event_id'],
            decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
        self.assertEqual([s['source_id'] for s in task['candidate_sources'] if s['source_id'] == 'BULK'], ['BULK'])
        request = build_review_request(task['manifest'], task['compiled'],
            candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'],
            task_context=task['task_context'])
        self.assertEqual([s['source_id'] for s in request['candidate_sources'] if s['source_id'] == 'BULK'], ['BULK'])

    def test_ENG017_shared_request_pool_does_not_expand_another_events_allowed_facts(self):
        s = source(text="手术时间：2022-01-02 08:00-2022-01-02 12:00\n初次所见：甲。决定甲方案。新所见：乙。决定乙方案。")
        prior = source("P", "此前拟行手术。", "2022-01-01 09:00")
        old = event("E0", prior, action="此前拟行手术。", prechoice_citations=empty_quotes(), time_basis="record_time_proxy")
        first = event("E1", s, action="决定甲方案。", prechoice_citations=quotes(s, "初次所见：甲。"))
        second = event("E2", s, action="决定乙方案。", prechoice_citations=quotes(s, "新所见：乙。"))
        sources = [prior, s]
        contexts = {e["event_id"]: engine.build_evidence_context(e, sources, [old, first, second], 10000)
                    for e in [first, second]}
        first_review = review("E1", observation_quotes=[{"source_id": "D", "quote": "新所见：乙。"}],
                              connection_quotes=[{"source_id": "D", "quote": "初次所见：甲。决定甲方案。"}])
        second_review = review("E2", observation_quotes=[{"source_id": "D", "quote": "新所见：乙。"}],
                               connection_quotes=[{"source_id": "D", "quote": "新所见：乙。决定乙方案。"}])
        client = FakeClient({"items": [first_review, second_review]})
        unit = {"unit_id": first["unit_id"], "source_ids": ["D"], "clock": s["clock"],
                "document_profile": "operative", "type_prior": "同文不同阶段分别判断",
                "original_texts": [{"source_id": "D", "text": s["text"]}]}
        with patch.object(engine, "build_evidence_context", side_effect=lambda e, *args, **kwargs: deepcopy(contexts[e["event_id"]])):
            result = engine.review_unit(unit, [first, second], [old, first, second], sources,
                                        {"review_context_characters": 10000}, client, [])
        outputs = {e["event_id"]: e for e in result["events"]}
        self.assertTrue(outputs["E1"]["checkpoint_status"].startswith("UNCERTAIN"))
        self.assertEqual(outputs["E1"]["validated_observation_citations"]["invalid"][0]["status"], "QUOTE_OUTSIDE_ALLOWED_SPANS")
        self.assertEqual(outputs["E2"]["checkpoint_status"], "CANDIDATE_NEW_EVIDENCE")
        self.assertIn("新所见：乙。", [block["text"] for block in client.requests[0]["payload"]["evidence_source_pool"]])


if __name__ == "__main__":
    unittest.main()

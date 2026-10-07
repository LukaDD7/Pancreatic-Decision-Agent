"""All clinical-looking fixtures below are synthetic, not patient records."""

from copy import deepcopy
import json

import pytest

from pancreatic_agent.policies import ScriptedPolicy, build_messages
from pancreatic_agent.state_machine import AuditError, CaseState, Evidence, LoggedEnvironment, Observation, StateMachine, Status, validate_output


def fact(source="SYNTH-E0", at="2030-01-01 08:00:00", **overrides):
    data = dict(source_id=source, domain="M_PERITONEAL", value="Synthetic unresolved finding", kind="imaging", raw_excerpt="Synthetic evidence for a controller test", available_at=at, time_basis="EXACT", reviewed=True)
    data.update(overrides)
    return Evidence(**data)


def baseline(**overrides):
    data = dict(case_id="SYNTHETIC-CASE", evidence=[fact()], decision_time="2030-01-01 09:00:00", baseline_reviewed=True)
    data.update(overrides)
    return CaseState(**data)


def decision(step=0, action="STAGING_LAPAROSCOPY", source="SYNTH-E0"):
    terminal = action in {"CONTINUE_NO_NEW_STAGING", "EXIT_CURATIVE_PATH", "DEFER_EXPERT_REVIEW"}
    mode = {"CONTINUE_NO_NEW_STAGING":"CONTINUE_CURATIVE_PATH", "EXIT_CURATIVE_PATH":"EXIT_CURATIVE_PATH", "DEFER_EXPERT_REVIEW":"DEFER_TO_EXPERT"}.get(action, "PAUSE_FOR_EVIDENCE")
    return {
        "case_id":"SYNTHETIC-CASE", "policy_version":"pancreas_seq_v0.1", "step":step,
        "state_summary":"Synthetic controller output, not a clinical recommendation",
        "decision_gaps":[] if terminal else [{"gap_id":"G1", "domain":"M_PERITONEAL", "current_status":"POSSIBLE", "priority":"HIGH", "why_unresolved":"Synthetic uncertainty", "source_ids":[source]}],
        "mode":mode,
        "action":{"canonical_action":action, "target_gap_id":None if terminal else "G1", "target_site":None if terminal else "PERITONEUM_OMENTUM", "expected_information_gain":"NOT_APPLICABLE" if terminal else "HIGH", "management_if_positive":"Synthetic positive branch", "management_if_negative":"Synthetic negative branch", "rationale":"Synthetic test rationale"},
        "source_ids":[source], "confidence":{"mode_confidence":0.5,"action_confidence":0.5},
    }


def observation(number=1, **overrides):
    time = f"2030-01-0{number+1} 10:00:00"
    data = dict(event_id=f"SYNTH-O{number}", clinical_event_id=f"SYNTH-PROCEDURE{number}", event_type="staging_laparoscopy_result", target_site="PERITONEUM_OMENTUM", resolves_domains=["M_PERITONEAL"], evidence=[fact(f"SYNTH-E{number}",at=time,kind="laparoscopy_finding")], available_at=time, time_basis="EXACT", audited=True, after_baseline=True, before_irreversible_boundary=True, order_reviewed=True)
    data.update(overrides)
    return Observation(**data)


def run(outputs, state=None, events=None):
    machine=StateMachine(state or baseline(),LoggedEnvironment(events or []))
    machine.run(ScriptedPolicy(outputs))
    return machine


def test_real_observation_then_terminal_and_no_later_call():
    seen=[]
    class CapturingPolicy:
        def decide(self,state):
            seen.append(state)
            return json.dumps(decision(step=state["step"],action="STAGING_LAPAROSCOPY" if not state["step"] else "EXIT_CURATIVE_PATH",source="SYNTH-E0" if not state["step"] else "SYNTH-E1"))
    machine=StateMachine(baseline(),LoggedEnvironment([observation()]))
    assert machine.run(CapturingPolicy())==Status.TERMINAL
    assert len(seen)==2
    assert [e["source_id"] for e in seen[0]["evidence"]]==["SYNTH-E0"]
    assert [e["source_id"] for e in seen[1]["evidence"]]==["SYNTH-E0","SYNTH-E1"]
    assert machine.terminal_output["action"]["canonical_action"]=="EXIT_CURATIVE_PATH"
    with pytest.raises(AuditError):machine.run(CapturingPolicy())


def test_unobserved_action_never_creates_a_result():
    machine=run([decision(action="PET_CT")],events=[observation()])
    assert machine.status==Status.COUNTERFACTUAL_UNOBSERVED
    assert len(machine.state.evidence)==1
    assert machine.terminal_output is None


@pytest.mark.parametrize("event_type",["future_plan","retrospective_mention","open_exploration_result","postoperative_pathology","resection_pathology"])
def test_plan_mention_open_operation_and_postop_pathology_are_not_results(event_type):
    assert run([decision()],events=[observation(event_type=event_type)]).status==Status.COUNTERFACTUAL_UNOBSERVED


def test_laparoscopy_is_excluded_if_endpoint_is_any_incision():
    assert run([decision()],events=[observation(after_incision=True)]).status==Status.COUNTERFACTUAL_UNOBSERVED
    assert run([decision(),decision(1,"EXIT_CURATIVE_PATH","SYNTH-E1")],state=baseline(endpoint="BEFORE_DEFINITIVE_RESECTION"),events=[observation(after_incision=True)]).status==Status.TERMINAL


@pytest.mark.parametrize("change",[dict(after_definitive_resection=True),dict(before_irreversible_boundary=False),dict(target_site="LIVER"),dict(resolves_domains=["M_LIVER"])])
def test_endpoint_site_and_question_must_match(change):
    assert run([decision()],events=[observation(**change)]).status==Status.COUNTERFACTUAL_UNOBSERVED


def test_unreviewed_baseline_never_calls_policy():
    machine=run([],state=baseline(baseline_reviewed=False))
    assert machine.status==Status.REVIEW_REQUIRED
    assert not any(x.get("event")=="policy_input" for x in machine.trace)


def test_unreviewed_observation_is_not_revealed():
    machine=run([decision()],events=[observation(audited=False)])
    assert machine.status==Status.REVIEW_REQUIRED
    assert len(machine.state.evidence)==1


def test_future_baseline_is_rejected():
    assert run([],state=baseline(evidence=[fact(at="2030-01-02 00:00:00")])).status==Status.REVIEW_REQUIRED


def test_date_only_available_at_is_rejected():
    assert run([],state=baseline(evidence=[fact(at="2030-01-01")])).status==Status.REVIEW_REQUIRED


def test_strict_time_rejects_proxy_and_reviewed_proxy_preserves_uncertainty():
    f=fact(at=None,time_basis="EXAM_PROXY",source_time="2030-01-01 08:00:00")
    assert run([],state=baseline(evidence=[f])).status==Status.REVIEW_REQUIRED
    state=baseline(evidence=[f],temporal_policy="REVIEWED_PROXY")
    assert run([decision(action="CONTINUE_NO_NEW_STAGING")],state=state).status==Status.TERMINAL
    assert state.visible()["evidence"][0]["available_at"] is None


def test_narrative_order_requires_review_and_remains_imprecise():
    event=observation(available_at=None,time_basis="NARRATIVE_ORDER",evidence=[fact("SYNTH-E1",at=None,time_basis="NARRATIVE_ORDER",kind="laparoscopy_finding")],after_incision=True)
    state=baseline(endpoint="BEFORE_DEFINITIVE_RESECTION",temporal_policy="REVIEWED_PROXY")
    machine=run([decision(),decision(1,"EXIT_CURATIVE_PATH","SYNTH-E1")],state=state,events=[event])
    assert machine.status==Status.TERMINAL
    assert machine.state.decision_time is None
    event.order_reviewed=False
    assert run([decision()],state=state,events=[event]).status==Status.REVIEW_REQUIRED


def test_current_time_not_original_t0_controls_next_observation():
    state=baseline(decision_time="2030-01-02 10:00:00",evidence=[fact(),fact("SYNTH-E1",at="2030-01-02 10:00:00",kind="laparoscopy_finding")],step=1)
    state.baseline_time="2030-01-01 09:00:00"
    old=observation(2,available_at="2030-01-02 09:00:00",evidence=[fact("SYNTH-E2",at="2030-01-02 09:00:00",kind="laparoscopy_finding")])
    status,event,_=LoggedEnvironment([old]).transition(state,decision(1,source="SYNTH-E1"))
    assert status==Status.COUNTERFACTUAL_UNOBSERVED and event is None


def test_duplicate_physical_procedure_is_not_two_acquisitions():
    events=[observation(),observation(2,clinical_event_id="SYNTH-PROCEDURE1")]
    assert run([decision(),decision(1,source="SYNTH-E1")],events=events).state.step==1


def test_maximum_two_acquisitions_no_automatic_terminal_substitution():
    machine=run([decision(),decision(1,source="SYNTH-E1"),decision(2,source="SYNTH-E2")],events=[observation(),observation(2),observation(3)])
    assert machine.status==Status.DEPTH_LIMIT_REACHED
    assert machine.state.step==2 and len(machine.state.evidence)==3
    assert machine.terminal_output is None


def test_two_acquisitions_can_be_followed_by_a_terminal_decision():
    machine=run([decision(),decision(1,source="SYNTH-E1"),decision(2,"EXIT_CURATIVE_PATH","SYNTH-E2")],events=[observation(),observation(2)])
    assert machine.status==Status.TERMINAL


@pytest.mark.parametrize("raw",["not json","```json\n{}\n```",json.dumps({"mode":"CONTINUE"})])
def test_parser_schema_failure_defers_without_repair(raw):
    machine=run([raw])
    assert machine.status==Status.SYSTEM_FAILURE
    assert machine.trace[-1]["reliability_disposition"]=="DEFER_TO_EXPERT"
    assert machine.terminal_output is None


def test_unavailable_source_cannot_be_cited():
    assert run([decision(source="SYNTH-FUTURE")]).status==Status.SYSTEM_FAILURE


def test_pause_requires_existing_gap_and_mode_action_alignment():
    value=decision();value["action"]["target_gap_id"]="missing"
    assert run([value]).status==Status.SYSTEM_FAILURE
    value=decision();value["mode"]="CONTINUE_CURATIVE_PATH"
    assert run([value]).status==Status.SYSTEM_FAILURE


def test_unknown_and_conflicting_evidence_are_preserved():
    state=baseline(evidence=[fact(value=None,epistemic_status="unknown"),fact("SYNTH-CONFLICT",epistemic_status="conflicting")])
    payload=state.visible()
    assert payload["evidence"][0]["value"] is None
    assert payload["evidence"][1]["epistemic_status"]=="conflicting"


def test_censored_value_operator_survives_serialization():
    state=baseline(evidence=[fact(value={"result_raw":"> synthetic-limit","numeric_exact":None})])
    assert state.visible()["evidence"][0]["value"]["result_raw"].startswith(">")


@pytest.mark.parametrize("change",[dict(role="clinician_decision"),dict(role="outcome"),dict(raw_excerpt="拟行腹腔镜探查备开腹根治术"),dict(kind="laparoscopy_finding"),dict(reviewed="false")])
def test_baseline_rejects_decision_outcome_and_mixed_document_leakage(change):
    assert run([],state=baseline(evidence=[fact(**change)])).status==Status.REVIEW_REQUIRED


def test_full_patient_bundle_is_rejected_and_environment_absent_from_model_input():
    with pytest.raises(AuditError):CaseState.from_dict({"case_id":"SYNTHETIC-CASE","documents_all":[],"outcome_post_T0_isolated":{}})
    text=json.dumps(build_messages(baseline().visible()))
    assert "outcome_post_T0_isolated" not in text and "observations" not in text


def test_compatibility_is_explicit_not_inferred_from_generic_mr():
    value=decision(action="LIVER_MRI");value["decision_gaps"][0]["domain"]="M_LIVER";value["action"]["target_site"]="LIVER"
    event=observation(event_type="mr_result",target_site="LIVER",resolves_domains=["M_LIVER"])
    assert run([value],events=[event]).status==Status.COUNTERFACTUAL_UNOBSERVED
    event.compatible_actions=["LIVER_MRI"]
    machine=run([value,decision(1,"EXIT_CURATIVE_PATH","SYNTH-E1")],events=[event])
    assert machine.status==Status.TERMINAL
    assert any(x.get("match")=="COMPATIBLE" for x in machine.trace)


def test_replay_window_and_exact_endpoint_exclude_late_results():
    event=observation(available_at="2030-03-01 10:00:00",evidence=[fact("SYNTH-E1",at="2030-03-01 10:00:00",kind="laparoscopy_finding")])
    assert run([decision()],events=[event]).status==Status.COUNTERFACTUAL_UNOBSERVED
    assert run([decision()],state=baseline(endpoint_time="2030-01-02 09:00:00"),events=[observation()]).status==Status.COUNTERFACTUAL_UNOBSERVED


def test_invalid_schema_output_raw_is_retained_in_trace(tmp_path):
    machine=StateMachine(baseline(),LoggedEnvironment([]),tmp_path/"trace.jsonl")
    machine.run(ScriptedPolicy(["broken output"]))
    lines=[json.loads(x) for x in (tmp_path/"trace.jsonl").read_text().splitlines()]
    assert any(x.get("raw_output")=="broken output" for x in lines)

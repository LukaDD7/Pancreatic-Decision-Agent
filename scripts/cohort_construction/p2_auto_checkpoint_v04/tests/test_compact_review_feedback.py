"""CE-REV-001 versioned compact repair; all sources are synthetic."""
from copy import deepcopy
import json

import pytest

from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint_v04 import review_feedback as live
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import compact_repair as compact
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import refine
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.test_refine import FakeClient
from scripts.cohort_construction.p2_auto_checkpoint_v04.tests.test_review_feedback import (
    CONFIG, assert_original_preserved, proof_error, setup,
)


def compact_config():
    return dict(CONFIG, review_feedback_compact_repair_enabled=True)


def run_compact(event, previous, sources, payload, client):
    return live.run_review_feedback([event], payload, [previous, event], sources, [],
        client, compact_config())


def test_CEREV001_opt_in_compacts_feedback_and_exact_windows_without_narrowing_authority():
    event, previous, sources, corrected, payload = setup()
    event['semantic_proof_validation'] = {'field_errors': [proof_error()]}
    before = deepcopy(event)
    client = FakeClient({'items': [corrected]})
    revised, audit = run_compact(event, previous, sources, payload, client)
    assert audit['status'] == 'COMPLETE'
    assert revised[0]['checkpoint_status'] == 'CANDIDATE_NEW_EVIDENCE'
    sent = client.calls[0]['payload']
    assert len(sent['evidence_source_pool']) < len(payload['evidence_source_pool'])
    assert event == before
    assert audit['repair_contract_version'] == compact.REPAIR_CONTRACT_VERSION
    assert audit['source_authority_expanded'] is False
    assert revised[0]['actual_model_evidence'] == event['actual_model_evidence']


def test_CEREV001_submitted_prior_authority_and_same_unit_identity_are_distinct():
    event, previous, sources, corrected, payload = setup()
    event['provided_identity_event_ids'] = ['E0', 'E1', 'SIBLING']
    event['semantic_proof_validation'] = {'field_errors': [proof_error()]}
    client = FakeClient({'items': [corrected]})
    _, audit = run_compact(event, previous, sources, payload, client)
    sent = client.calls[0]['payload']
    contract = sent['repair_contract']['submitted_event_authority']['E1']
    assert contract['submitted_eligible_previous_event_ids'] == ['E0']
    assert contract['identity_event_ids'] == ['E0', 'E1', 'SIBLING']
    assert sent['validation_feedback'][0]['allowed_revision_scope']['eligible_previous_event_ids'] == ['E0']
    # Identity siblings need not occur in the chronological prior-event ledger.
    assert all(row['event_id'] != 'SIBLING' for row in sent['earlier_event_ledger'])
    payload['events'][0]['evidence_context']['eligible_previous_event_ids'] = []
    with pytest.raises(ValueError, match='COMPACT_REPAIR_SUBMITTED_PRIOR_AUTHORITY_MISMATCH'):
        compact.prepare_compact_payload(payload, [event], [], sources, [previous, event])


def test_CEREV001_compact_rejects_outside_source_event_time_and_unrendered_legal_quote():
    event, previous, sources, corrected, payload = setup()
    event['semantic_proof_validation'] = {'field_errors': [proof_error()]}
    # Keep the original source block legal but omit its optional proposal quote;
    # no source/allowlist authority is removed from the saved event.
    event['semantic_review']['evidence_comparison']['previous_known_quotes'] = []
    event['prechoice_citations'] = {'valid': [], 'ambiguous': [], 'invalid': []}
    event['choice_boundary_citations'] = {'valid': [], 'ambiguous': [], 'invalid': []}
    event['action_citations'] = {'valid': [], 'ambiguous': [], 'invalid': []}
    event['semantic_review']['previous_event_id'] = None
    client = FakeClient({'items': [corrected]})
    _, audit = run_compact(event, previous, sources, payload, client)
    sent = client.calls[0]['payload']
    assert len(sent['evidence_source_pool']) < len(payload['evidence_source_pool'])
    # A valid original observation can remain legal when no longer displayed.
    assert sent['repair_contract']['unrendered_original_block_ids']
    assert sent['repair_contract']['authority_narrowed'] is False
    for mutation in ('source', 'event', 'time'):
        fresh = setup()
        event, previous, sources, corrected, payload = fresh
        event['semantic_proof_validation'] = {'field_errors': [proof_error()]}
        proposal = deepcopy(corrected)
        if mutation == 'source':
            proposal['observation_quotes'][0]['source_id'] = 'UNPROVIDED'
        elif mutation == 'event':
            proposal['previous_event_id'] = 'UNPROVIDED'
        else:
            proposal['choice_time'] = '2030-01-01T00:00:00Z'
        revised, audit = run_compact(event, previous, sources, payload, FakeClient({'items': [proposal]}))
        assert_original_preserved(revised[0], event)
        if mutation == 'time':
            # Time fields are deliberately absent from REVIEW_SCHEMA. A forged
            # addition must fail schema validation before it can be applied;
            # it is not a semantic revalidation failure after provider success.
            assert revised[0]['review_refinement']['status'] == 'REPAIR_NOT_APPLIED'
            assert audit['error'] == 'ValidationError'
        else:
            assert revised[0]['review_refinement']['status'] == 'REPAIR_REVALIDATION_FAILED'


def test_CEREV001_preprovider_failure_is_not_a_provider_second_round():
    event, previous, sources, corrected, payload = setup()
    event['semantic_proof_validation'] = {'field_errors': [proof_error()]}
    revised, audit = live.run_review_feedback([event], payload, [previous, event], sources, [],
        FakeClient({'items': [corrected]}), dict(CONFIG, max_review_payload_characters=1))
    assert audit['attempted'] is False
    assert audit['error_code'] == 'REVIEW_FEEDBACK_PAYLOAD_EXCEEDS_BUDGET'
    assert revised[0]['review_refinement']['attempted'] is False
    # An explicitly new implementation can retry a pre-provider preparation
    # failure; its one allowed provider attempt is still not a second round.
    revised, audit = live.run_review_feedback([event], payload, [previous, event], sources, [],
        FakeClient({'items': [corrected]}), dict(compact_config(), max_review_payload_characters=1))
    assert audit['attempted'] is False
    assert audit['error_code'] == 'COMPACT_REVIEW_FEEDBACK_PAYLOAD_EXCEEDS_BUDGET'
    assert revised[0]['review_refinement']['attempted'] is False


def test_CEREV001_compact_mode_is_explicitly_opt_in_and_boolean():
    event, previous, sources, corrected, payload = setup()
    with pytest.raises(ValueError, match='COMPACT_REPAIR_ENABLED_MUST_BE_BOOLEAN'):
        live.run_review_feedback([event], payload, [previous, event], sources, [],
            FakeClient(), dict(CONFIG, review_feedback_compact_repair_enabled=1))
    revised, audit = live.run_review_feedback([event], payload, [previous, event], sources, [],
        FakeClient({'items': [corrected]}), dict(CONFIG, max_review_payload_characters=1))
    assert audit['attempted'] is False and audit['error_code'] == 'REVIEW_FEEDBACK_PAYLOAD_EXCEEDS_BUDGET'


def test_CEREV001_issue_map_has_real_counterexample_and_existing_regressions():
    from pathlib import Path
    path = Path(__file__).parent / 'review_feedback_issue_map.json'
    issue = next(row for row in json.loads(path.read_text())['issues'] if row['id'] == 'CE-REV-001')
    text = Path(__file__).read_text()
    for test in issue['required_tests']:
        assert 'def ' + test + '(' in text
    assert issue['real_counterexample']['provider_call_for_failed_parent_repairs'] == 0
    assert issue['clinical_gold'] is False
    examples = issue['real_counterexamples']
    assert [row['case_id'] for row in examples] == [f'SCALE-{number:02d}' for number in range(1, 7)]
    assert sum(row['preprovider_budget_failure_group_count'] for row in examples) == 77
    assert sum(row['offline_compact_fitting_group_count'] for row in examples) == 77
    for row in examples:
        receipt = json.loads(Path(row['private_receipt']).read_text())
        assert receipt['no_provider_calls'] is True
        assert receipt['fitting_count'] == receipt['count'] == row['offline_compact_fitting_group_count']
        sizes = [item['compact_payload_characters'] for item in receipt['rows']]
        assert [min(sizes), max(sizes)] == row['compact_size_range_characters']
        assert all(item['actual_provider_request_sent'] is False for item in receipt['rows'])

"""CE-REM-004: saved candidates and development review scope are separate."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import assembly_receipt
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_real_validation.inputs import (
    candidate_scope_receipt, episode_inventory,
)


def _case():
    graph, sources = fictional_graph()
    other = deepcopy(graph['events'][1])
    other.update(event_id='EVENT-OTHER', checkpoint_status='CANDIDATE_NEW_EVIDENCE')
    episodes = [deepcopy(graph['episodes'][0]),
        dict(episode_id='EPISODE-00', candidate_ids=['EVENT-OTHER'], checkpoint_ids=['EVENT-OTHER'])]
    case = dict(events=graph['events'] + [other], episodes=episodes)
    return graph, sources, case


def test_CE_REM004_scope_covers_all_events_without_materializing_others():
    _, _, case = _case()
    selected = next(ep for ep in case['episodes'] if ep['episode_id'] == 'EPISODE-01')
    receipt = candidate_scope_receipt(case, selected)
    scope = receipt['development_review_scope']
    assert scope['not_checkpoint_sampling'] and scope['not_complete_downstream_decision_set']
    assert scope['not_clinical_importance_ranking'] and scope['not_representative_random_sample']
    assert receipt['saved_event_count'] == 4
    assert receipt['candidate_checkpoint_count'] == 3
    assert receipt['selected_checkpoint_count'] == 2
    assert receipt['retained_candidate_outside_development_scope_count'] == 1
    rows = {row['event_id']: row for row in receipt['event_coverage']}
    assert rows['EVENT-OTHER']['disposition'] == 'RETAINED_CANDIDATE_OUTSIDE_DEVELOPMENT_REVIEW_SCOPE'
    assert rows['EVENT-REJECTED']['disposition'] == 'RETAINED_NONCHECKPOINT_EVENT'
    assert not rows['EVENT-OTHER']['material_assembly_requested']
    assert all(row['event_preserved'] and not row['physical_deletion'] for row in rows.values())
    assert not receipt['doctor_blind_review_permission_granted']


def test_CE_REM004_empty_selection_retains_candidates_and_noncandidates():
    _, _, case = _case()
    receipt = candidate_scope_receipt(case, None)
    assert receipt['selected_checkpoint_count'] == 0
    assert receipt['retained_candidate_outside_development_scope_count'] == 3
    assert all(not row['material_assembly_requested'] for row in receipt['event_coverage'])


def test_CE_REM004_material_receipt_distinguishes_noncandidate_from_unassembled_candidate():
    graph, sources, _ = _case()
    receipt = assembly_receipt(graph, sources, [], [])
    rows = {row['event_id']: row for row in receipt['event_material_coverage']}
    assert not rows['EVENT-01']['material_assembly_requested']
    assert rows['EVENT-01']['disposition'] == 'RETAINED_CANDIDATE_NOT_REQUESTED_IN_THIS_ASSEMBLY'
    assert not rows['EVENT-REJECTED']['candidate_checkpoint']
    assert not rows['EVENT-REJECTED']['material_assembly_requested']
    assert all(not row['material_review_passed'] for row in rows.values())
    assert receipt['candidate_checkpoint_count'] == 2
    assert not receipt['doctor_packet_created']


def test_CE_REM004_actual_tasks_and_failures_bound_material_accounting():
    graph, sources, _ = _case()
    receipt = assembly_receipt(graph, sources, [], [])
    assert all(row['disposition'] == 'RETAINED_CANDIDATE_NOT_REQUESTED_IN_THIS_ASSEMBLY'
               for row in receipt['event_material_coverage'] if row['candidate_checkpoint'])
    from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import (
        prepare_candidate_materials,
    )
    tasks, failures = prepare_candidate_materials(graph, sources,
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC')
    assert tasks and not failures
    receipt = assembly_receipt(graph, sources, tasks[:1], [dict(
        event_id='EVENT-02', episode_id='EPISODE-01', status='ASSEMBLY_HOLD',
        reason_code='SYNTHETIC_LOCAL_FAILURE', clinical_release_permitted=False)])
    rows = {row['event_id']: row for row in receipt['event_material_coverage']}
    assert rows['EVENT-01']['disposition'] == 'PREPARED_NOT_REVIEWED'
    assert rows['EVENT-02']['disposition'] == 'ASSEMBLY_HOLD'
    assert all(not row['material_review_passed'] for row in rows.values())
    with pytest.raises(Exception, match='OUTCOME_NOT_UNIQUE'):
        assembly_receipt(graph, sources, tasks[:1], [dict(
            event_id='EVENT-01', episode_id='EPISODE-01', status='ASSEMBLY_HOLD')])


def test_CE_REM004_foreign_duplicate_orphan_and_noncandidate_scope_fail_closed():
    _, _, case = _case()
    selected = next(ep for ep in case['episodes'] if ep['episode_id'] == 'EPISODE-01')
    with pytest.raises(ValueError, match='SELECTED_EPISODE'):
        candidate_scope_receipt(case, dict(selected, episode_id='EP-NOT-IN-CASE'))
    with pytest.raises(ValueError, match='SELECTED_EPISODE_MEMBERSHIP_MISMATCH'):
        candidate_scope_receipt(case, dict(selected, checkpoint_ids=[]))
    duplicate = deepcopy(case)
    duplicate['episodes'].append(deepcopy(selected))
    with pytest.raises(ValueError, match='DUPLICATE.*EPISODE'):
        candidate_scope_receipt(duplicate, selected)
    with patch('scripts.cohort_construction.p2_checkpoint_real_validation.inputs.load_case',
               return_value=duplicate):
        with pytest.raises(ValueError, match='DUPLICATE.*EPISODE'):
            episode_inventory('SYNTHETIC')
    orphan = deepcopy(case)
    orphan['episodes'][0]['candidate_ids'] = []
    orphan['episodes'][0]['event_ids'] = []
    with pytest.raises(ValueError, match='EPISODE_MEMBERSHIP_INVALID'):
        candidate_scope_receipt(orphan, orphan['episodes'][0])
    noncandidate = deepcopy(case)
    event = next(e for e in noncandidate['events'] if e['event_id'] == 'EVENT-01')
    event['checkpoint_status'] = 'UNCERTAIN_REVIEW'
    with pytest.raises(ValueError, match='NONCANDIDATE'):
        candidate_scope_receipt(noncandidate, selected)

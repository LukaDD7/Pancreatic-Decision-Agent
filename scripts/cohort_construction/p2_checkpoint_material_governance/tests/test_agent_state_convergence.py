"""CE-AMG-027 source-scope and no-visible-disposition controls."""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.review import build_review_request, run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import inputs, operation
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_review_remediation import echo_response


def test_CE_AMG027_include_source_starts_review_and_requires_fresh_resolution():
    manifest, compiled = inputs()
    candidate = dict(source_id='new-source', source_version='v1', text='虚构新增来源。')
    requests = []

    def controlled(task, request, schema):
        requests.append(deepcopy(request))
        if len(requests) == 1:
            return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[],
                operations=[dict(operation_id='INCLUDE', operation='INCLUDE_SOURCE',
                    source_id='new-source', reason='虚构扩大声明来源范围')])
        if len(requests) == 2:
            assert requests[1]['previous_round']['previous_review']['operations'][0]['operation'] == 'INCLUDE_SOURCE'
            assert requests[1]['previous_round']['actual_program_diff']['source_scope_changed'] is True
            assert requests[1]['previous_round']['actual_program_diff']['doctor_visible_material_changed'] is False
            return echo_response(request, status='AUTO_REVIEW_HOLD', required_actions=['虚构：新增来源需人工判断'],
                operations=[operation('虚构新增来源。', 'REVIEW', source='new-source')])
        if len(requests) == 3:
            assert requests[2]['previous_round']['actual_program_diff']['source_scope_changed'] is False
            return echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[],
                operations=[operation('虚构新增来源。', 'KEEP', source='new-source')])
        return echo_response(request, status='AUTO_REVIEW_PASS', required_actions=[])

    chain = run_review_chain(manifest, compiled, controlled, candidate_sources=[candidate], max_rounds=4)
    assert chain['stop_reason'] == 'REVIEW_HOLD'
    assert len(chain['rounds']) == 2
    assert validate_review_chain(chain)['deterministic_replay_verified']


def test_CE_AMG027_mask_to_review_research_only_churn_is_rejected_and_replayed():
    manifest, compiled = inputs()
    response = echo_response(build_review_request(manifest, compiled),
        status='AUTO_REVIEW_REVISE', required_actions=[],
        operations=[operation('虚构', 'MASK')])
    chain = run_review_chain(manifest, compiled, lambda *args: response)
    assert chain['stop_reason'] == 'EXECUTION_FAILED'
    assert chain['failure']['reason_code'] == 'REVISE_REQUIRES_DOCTOR_VISIBLE_OR_SOURCE_SCOPE_CHANGE'
    assert validate_review_chain(chain)['round_count'] == 0

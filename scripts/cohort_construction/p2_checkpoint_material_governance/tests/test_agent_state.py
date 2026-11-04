"""CE-AMG-027: iterative reviewer self-history and no-visible-change controls.

Synthetic structural counterexample derived from SCALE-02. It contains no
patient text and makes no claim about clinical correctness or model capability.
"""
from copy import deepcopy

from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import validate_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.review import build_review_request, run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import TEXT, inputs, operation
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_review_remediation import echo_response


def test_CE_AMG027_next_request_contains_own_review_applied_receipts_and_both_diffs():
    manifest, compiled = inputs()
    first_response = []
    requests = []

    def controlled(task, request, schema):
        requests.append(deepcopy(request))
        if not first_response:
            response = echo_response(request, status='AUTO_REVIEW_REVISE', required_actions=[],
                operations=[operation(TEXT, 'KEEP')])
            first_response.append(response)
            return response
        history = request['previous_round']
        assert history['previous_review'] == first_response[0]
        assert history['round_index'] == 1
        assert history['operation_receipts'][0]['status'] == 'APPLIED'
        assert history['actual_program_diff']['changed'][0]['before'] == 'REVIEW'
        assert history['actual_program_diff']['changed'][0]['after'] == 'KEEP'
        assert history['actual_program_diff']['doctor_visible_material_changed']
        assert history['current_compiled_sha256'] == request['response_binding']['compiled_sha256']
        assert history['current_doctor_view_sha256'] == request['response_binding']['doctor_view_sha256']
        assert history['program_execution_receipt']['changed']
        assert request['review_round'] == dict(round_index=2, max_rounds=3,
            is_first_review=False, current_state_produced_by_previous_review=True)
        assert 'continuation, not a first review' in ' '.join(request['instructions']['requirements'])
        return echo_response(request, status='AUTO_REVIEW_PASS', required_actions=[])

    chain = run_review_chain(manifest, compiled, controlled)
    assert chain['state'] == 'AUTO_REVIEW_PASS'
    assert validate_review_chain(chain)['round_count'] == 2
    assert requests[0]['review_round']['is_first_review']


def test_CE_AMG027_mask_to_review_cannot_consume_another_revise_round():
    manifest, compiled = inputs()
    response = echo_response(build_review_request(manifest, compiled),
        status='AUTO_REVIEW_REVISE', required_actions=[], operations=[operation(TEXT, 'REVIEW')])
    chain = run_review_chain(manifest, compiled, lambda *args: response)
    assert chain['stop_reason'] == 'EXECUTION_FAILED'
    assert chain['failure']['reason_code'] == 'REVISE_REQUIRES_ACTUAL_NEW_PARTITION'
    assert chain['state'] == 'AUTO_REVIEW_HOLD'
    assert chain['rounds'] == []
    assert validate_review_chain(chain)['round_count'] == 0


def test_CE_AMG027_no_visible_change_hold_still_applies_and_replays():
    manifest, compiled = inputs()
    response = echo_response(build_review_request(manifest, compiled),
        status='AUTO_REVIEW_HOLD', required_actions=['虚构：来源待人工核对'],
        operations=[operation(TEXT, 'MASK')])
    chain = run_review_chain(manifest, compiled, lambda *args: response)
    assert chain['stop_reason'] == 'REVIEW_HOLD'
    assert chain['rounds'][0]['compiled']['compiled_sha256'] != compiled['compiled_sha256']
    assert not chain['rounds'][0]['diff']['doctor_visible_material_changed']
    assert validate_review_chain(chain)['state'] == 'AUTO_REVIEW_HOLD'

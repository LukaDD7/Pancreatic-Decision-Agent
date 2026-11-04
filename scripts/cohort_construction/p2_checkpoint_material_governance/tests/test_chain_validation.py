"""V061-AUD-007/GAP-02: replay history, targets and candidate graph identity."""
from copy import deepcopy

import pytest

from scripts.cohort_construction.p2_checkpoint_material_governance.chain_validation import (
    bind_candidate_graph, validate_review_chain,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import (
    build_episode_material_set, graph_revision_handoff, validate_episode_material_set,
)
from scripts.cohort_construction.p2_checkpoint_material_governance.review import DIMENSIONS, run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_review_app.synthetic_demo import synthetic_inputs
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256


def response_for(request, *, status='AUTO_REVIEW_HOLD', operations=(), review_id='MR-fictional'):
    return dict(format='material-review-0.2.0', review_id=review_id, target=deepcopy(request['target']),
        **deepcopy(request['response_binding']), reviewer_type='llm_material_review',
        dimensions=[dict(dimension=d, applicability='APPLICABLE', status='RESOLVED',
                         reason='虚构工程测试，未形成临床意见') for d in DIMENSIONS],
        findings=[], operations=list(operations), status=status,
        required_actions=['虚构待人工核验'] if status=='AUTO_REVIEW_HOLD' else [])


def fictional_chain(*, include_source=False, graph=False):
    documents, sources, _ = synthetic_inputs()
    outside = dict(source_id=sources[1]['source_id'], source_version='v1', text=sources[1]['text'],
                   sha256=sources[1]['source_sha256'])
    context = None
    snapshot = dict(events=[deepcopy(documents['selected_event'])],
                    episodes=[dict(episode_id='ep1', event_ids=[documents['selected_event']['event_id']])])
    if graph:
        context = bind_candidate_graph(snapshot, graph_version='graph-v1',
            target_event_id=documents['selected_event']['event_id'],
            checkpoint_id=documents['compiled']['target']['checkpoint_id'],
            task_context=dict(decision_time_evidence='UNRESOLVED', clinical_visibility_certified=False))
    count = 0
    def ask(task, request, schema):
        nonlocal count
        count += 1
        operations = []
        if count == 1:
            for index, segment in enumerate(request['current_dispositions']):
                if segment['disposition'] == 'REVIEW':
                    operations.append(dict(operation_id='OP-'+str(index), operation='SET_DISPOSITION',
                        source_id=segment['source_id'], exact_quote=segment['exact_quote'], disposition='KEEP',
                        reason='虚构程序修订，不认证当时可见性'))
            if include_source:
                operations.append(dict(operation_id='OP-include', operation='INCLUDE_SOURCE',
                    source_id=outside['source_id'], reason='虚构来源范围修订，不改候选图'))
        elif include_source and count == 2:
            operations.append(dict(operation_id='OP-outside', operation='SET_DISPOSITION',
                source_id=outside['source_id'], exact_quote=outside['text'], disposition='MASK',
                reason='虚构后续来源遮蔽'))
        return response_for(request, status='AUTO_REVIEW_REVISE' if operations else 'AUTO_REVIEW_PASS',
                            operations=operations, review_id='MR-fictional-'+str(count))
    chain = run_review_chain(documents['manifest'], documents['compiled'], ask,
        candidate_sources=[outside] if include_source else [],
        excluded_sources=[] if include_source else [outside], max_rounds=4, task_context=context)
    return documents, sources, chain, snapshot


def material_item(chain):
    graph = chain['task_context']['candidate_graph_binding']
    return dict(manifest=chain['manifest'], compiled=chain['compiled'], doctor_view=chain['doctor_view'],
                auto_review_chain=chain, revision_ledger=[r['ledger'] for r in chain['rounds']],
                state=chain['state'], event_reference=graph['target_event_id'])


def episode_reference(snapshot):
    return dict(episode_id='ep1', candidate_version='graph-v1', input_sha256=canonical_sha256(snapshot),
                graph_snapshot=snapshot)


def test_replay_valid_multiround_with_new_source():
    _, _, chain, _ = fictional_chain(include_source=True)
    receipt = validate_review_chain(chain)
    assert receipt['deterministic_replay_verified'] and receipt['round_count'] == 3
    assert receipt['state'] == 'AUTO_REVIEW_PASS'
    assert receipt['clinical_visibility_certified'] is False


@pytest.mark.parametrize('mutation', ['input_hash','target','ledger','output','request','receipt','tail','terminal'])
def test_replay_rejects_historical_tampering(mutation):
    _, _, chain, _ = fictional_chain(include_source=True)
    broken = deepcopy(chain)
    first = broken['rounds'][0]
    if mutation == 'input_hash': first['input_compiled_sha256'] = '0'*64
    elif mutation == 'target': first['review']['target']['checkpoint_id'] = 'other-checkpoint'
    elif mutation == 'ledger': first['ledger']['operations'] = []
    elif mutation == 'output': first['compiled'] = deepcopy(broken['initial_compiled'])
    elif mutation == 'request': first['request']['researcher_sources'][0]['text'] = '伪造正文'
    elif mutation == 'receipt': first['application_receipt']['changed'] = False
    elif mutation == 'tail': broken['compiled'] = deepcopy(broken['initial_compiled'])
    else: broken['stop_reason'] = 'REVIEW_HOLD'
    with pytest.raises(Exception): validate_review_chain(broken)


def test_empty_pass_does_not_become_verified_history():
    _, _, chain, _ = fictional_chain()
    chain['rounds'] = []
    with pytest.raises(Exception, match='NONTERMINAL_OR_EMPTY'): validate_review_chain(chain)


def test_provider_failure_retains_zero_completed_rounds_only_as_hold():
    documents, _, _ = synthetic_inputs()
    def ask(*args): raise RuntimeError('fictional-provider-failure')
    chain = run_review_chain(documents['manifest'], documents['compiled'], ask)
    assert validate_review_chain(chain)['state'] == 'AUTO_REVIEW_HOLD'
    chain['state'] = 'AUTO_REVIEW_PASS'
    with pytest.raises(Exception, match='TERMINAL_STATE'): validate_review_chain(chain)


def test_episode_requires_whole_replay_and_graph_membership():
    _, _, chain, snapshot = fictional_chain(graph=True)
    result = build_episode_material_set(episode_reference(snapshot), [material_item(chain)])
    assert validate_episode_material_set(result)
    handoff = graph_revision_handoff(result, chain['compiled']['target']['checkpoint_id'], 'MR1', '虚构重建')
    assert handoff['previous_material_pass_transferable'] is False
    broken = material_item(deepcopy(chain));broken['auto_review_chain']['rounds'][0]['ledger']['operations'] = []
    with pytest.raises(Exception): build_episode_material_set(episode_reference(snapshot), [broken])


def test_graph_change_requires_new_material_review_not_relabeling():
    _, _, chain, snapshot = fictional_chain(graph=True)
    changed = deepcopy(snapshot);changed['events'][0]['source_ids'] = ['different-source']
    with pytest.raises(Exception, match='GRAPH_CHANGED'):
        build_episode_material_set(episode_reference(changed), [material_item(chain)])
    context = bind_candidate_graph(changed, graph_version='graph-v2',
        target_event_id=chain['task_context']['candidate_graph_binding']['target_event_id'],
        checkpoint_id=chain['compiled']['target']['checkpoint_id'])
    with pytest.raises(Exception, match='GRAPH_CHANGED'):
        validate_review_chain(chain, expected_graph_context=context)


def test_event_reference_cannot_be_rebound_and_graph_requires_target():
    _, _, chain, snapshot = fictional_chain(graph=True)
    item = material_item(chain);item['event_reference'] = 'unrelated-event'
    with pytest.raises(Exception, match='EVENT_MEMBERSHIP'):
        build_episode_material_set(episode_reference(snapshot), [item])
    snapshot['events'] = []
    with pytest.raises(Exception, match='TARGET_EVENT_NOT_IN'):
        bind_candidate_graph(snapshot, graph_version='v2', target_event_id='missing', checkpoint_id='cp1')


def test_reconstruction_entry_runs_fresh_review_and_rejects_previous_pass_response():
    from scripts.cohort_construction.p2_checkpoint_material_governance.episode import restart_material_review_after_graph_revision
    _, _, chain, snapshot = fictional_chain(graph=True)
    old = build_episode_material_set(episode_reference(snapshot), [material_item(chain)])
    changed = deepcopy(snapshot);changed['events'][0]['revision_note'] = '虚构图版本修订'
    reference = episode_reference(changed);reference['candidate_version'] = 'graph-v2'
    old_answer = chain['rounds'][-1]['review']
    rejected = restart_material_review_after_graph_revision(old, chain['compiled']['target']['checkpoint_id'],
        reference, chain['manifest'], chain['compiled'], lambda *args:old_answer,
        target_event_id=chain['task_context']['candidate_graph_binding']['target_event_id'])
    assert rejected['stop_reason'] == 'EXECUTION_FAILED'
    assert rejected['state'] == 'AUTO_REVIEW_HOLD'
    assert validate_review_chain(rejected)['round_count'] == 0
    fresh = restart_material_review_after_graph_revision(old, chain['compiled']['target']['checkpoint_id'],
        reference, chain['manifest'], chain['compiled'],
        lambda task,request,schema:response_for(request,status='AUTO_REVIEW_PASS'),
        target_event_id=chain['task_context']['candidate_graph_binding']['target_event_id'])
    assert validate_review_chain(fresh)['state'] == 'AUTO_REVIEW_PASS'
    assert fresh['rounds'][0]['request_sha256'] != chain['rounds'][-1]['request_sha256']
    assert fresh['clinical_release_permitted'] is False


def test_required_action_revision_adopts_tail_then_holds_and_cannot_be_forged_pass():
    documents,_,_=synthetic_inputs()
    def ask(task,request,schema):
        segment=next(s for s in request['current_dispositions'] if s['disposition']=='REVIEW')
        operation=dict(operation_id='OP-required',operation='SET_DISPOSITION',source_id=segment['source_id'],
            exact_quote=segment['exact_quote'],disposition='KEEP',reason='虚构修订与未决待办')
        answer=response_for(request,status='AUTO_REVIEW_REVISE',operations=[operation])
        answer['required_actions']=['由实际人员核对虚构待办']
        return answer
    chain=run_review_chain(documents['manifest'],documents['compiled'],ask)
    assert validate_review_chain(chain)['state']=='AUTO_REVIEW_HOLD'
    assert chain['stop_reason']=='REVIEW_HOLD'
    assert chain['compiled']==chain['rounds'][-1]['compiled']
    assert chain['compiled']!=documents['compiled']
    chain['state']='AUTO_REVIEW_PASS';chain['stop_reason']='AUTO_PASS'
    with pytest.raises(Exception,match='TERMINAL_REASON'):
        validate_review_chain(chain)

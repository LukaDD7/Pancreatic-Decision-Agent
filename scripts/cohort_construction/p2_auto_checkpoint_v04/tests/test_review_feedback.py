"""CE-AUD-007 live one-pass review repair, with synthetic raw sources only."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.cohort_construction.p2_auto_checkpoint_v04 import review_feedback as current
from scripts.cohort_construction.p2_auto_checkpoint_v04.client import ModelRequestFailed
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine import refine
from scripts.cohort_construction.p2_auto_checkpoint_v05_refine.test_refine import FakeClient, setup


CONFIG={'review_feedback_max_rounds':1}


def run(event, previous, sources, payload, client, config=None, names=None):
    return current.run_review_feedback([event],payload,[previous,event],sources,names or [],
                                       client,CONFIG if config is None else config)


def assert_original_preserved(revised,original):
    assert revised['checkpoint_status']==original['checkpoint_status']
    assert revised['review_request_sha256']==original['review_request_sha256']
    assert revised['semantic_review']==original['semantic_review']
    assert revised['actual_model_evidence']==original['actual_model_evidence']
    assert revised['evidence_context']==original['evidence_context']


def initial_id_failure(marker,location='both'):
    event,previous,sources,corrected,payload=setup()
    event.update(checkpoint_status='UNCERTAIN_SEMANTIC_JUDGMENT',record_function='uncertain',
                 validation_reasons=['MODEL_UNCERTAIN'])
    event['semantic_review'].update(record_function='uncertain',checkpoint='uncertain',
        previous_event_id=None,same_event_with=[],same_process_previous_id=None,
        process_link='uncertain',evidence_evolution='uncertain',observation_quotes=[],
        connection_quotes=[],rationale=marker if location in ('both','rationale') else 'Model returned no unique item.',
        uncertainty=[marker] if location in ('both','uncertainty') else [])
    return event,previous,sources,corrected,payload


@pytest.mark.parametrize('marker',['MODEL_OMITTED_EVENT_ID','MODEL_DUPLICATED_EVENT_ID'])
@pytest.mark.parametrize('location',['rationale','uncertainty'])
def test_CEAUD007_initial_missing_or_duplicate_id_receives_one_mechanical_feedback(marker,location):
    event,previous,sources,corrected,payload=initial_id_failure(marker,location)
    original=deepcopy(event);original_payload=deepcopy(payload)
    assert refine.repair_triggers(event)==[]  # Old proof-only trigger is deliberately insufficient.
    assert current.review_repair_triggers(event)==[marker]
    client=FakeClient({'items':[corrected]})
    revised,audit=run(event,previous,sources,payload,client)
    assert revised[0]['checkpoint_status']=='CANDIDATE_NEW_EVIDENCE'
    assert revised[0]['semantic_review_before_refinement']==original['semantic_review']
    assert revised[0]['review_refinement']['triggers']==[marker]
    assert audit['target_event_ids']==[event['event_id']]
    assert len(client.calls)==1
    sent=client.calls[0]['payload']
    assert sent['validation_feedback'][0]['triggers']==[marker]
    for field in ('events','evidence_source_pool','current_full_document','earlier_event_ledger'):
        assert sent[field]==payload[field]
    assert event==original and payload==original_payload
    again,_=run(revised[0],previous,sources,payload,client)
    assert again==revised and len(client.calls)==1


@pytest.mark.parametrize('marker',['MODEL_OMITTED_EVENT_ID','MODEL_DUPLICATED_EVENT_ID'])
@pytest.mark.parametrize('repair_result',['missing','duplicated','uncertain'])
def test_CEAUD007_initial_id_feedback_missing_duplicate_or_uncertain_does_not_force_positive(marker,repair_result):
    event,previous,sources,corrected,payload=initial_id_failure(marker)
    if repair_result=='uncertain':
        corrected['checkpoint']='uncertain';items=[corrected]
    else:items=[] if repair_result=='missing' else [corrected,corrected]
    client=FakeClient({'items':items})
    revised,audit=run(event,previous,sources,payload,client)
    assert revised[0]['checkpoint_status']=='UNCERTAIN_SEMANTIC_JUDGMENT'
    if repair_result!='uncertain':
        assert_original_preserved(revised[0],event)
        assert revised[0]['review_refinement']['status']=='TARGET_MISSING_OR_DUPLICATED'
    assert revised[0]['review_refinement']['triggers']==[marker]
    again,_=run(revised[0],previous,sources,payload,client)
    assert len(client.calls)==1 and again==revised


@pytest.mark.parametrize('case',['no_marker','no_hash','already_attempted','disabled'])
def test_CEAUD007_initial_id_trigger_does_not_expand_pure_semantic_or_unbound_requests(case):
    event,previous,sources,corrected,payload=initial_id_failure('MODEL_OMITTED_EVENT_ID')
    config=CONFIG
    if case=='no_marker':
        event['semantic_review'].update(rationale='Patient-specific clinical meaning remains uncertain.',
                                        uncertainty=['Clinical meaning remains uncertain.'])
    elif case=='no_hash':event.pop('review_request_sha256')
    elif case=='already_attempted':event['review_refinement']={'attempted':True,'rounds':1}
    else:config={'review_feedback_max_rounds':0}
    client=FakeClient({'items':[corrected]});original=deepcopy(event)
    revised,audit=run(event,previous,sources,payload,client,config)
    assert revised==[original] and not client.calls and audit['attempted'] is False


def test_CEAUD007_one_model_revision_revalidates_and_preserves_actual_source_context():
    event,previous,sources,corrected,payload=setup()
    originals=deepcopy((event,previous,sources,payload))
    client=FakeClient({'items':[corrected]})
    revised,audit=run(event,previous,sources,payload,client)
    assert revised[0]['checkpoint_status']=='CANDIDATE_NEW_EVIDENCE'
    assert revised[0]['review_request_sha256']=='repair-hash'
    assert revised[0]['semantic_review_before_refinement']==event['semantic_review']
    assert revised[0]['actual_model_evidence']==event['actual_model_evidence']
    assert revised[0]['evidence_context']==event['evidence_context']
    assert revised[0]['blind_replay_ready'] is False and revised[0]['expert_reference'] is False
    assert (event,previous,sources,payload)==originals
    assert len(client.calls)==1 and audit['attempted'] is True and audit['status']=='COMPLETE'
    sent=client.calls[0]['payload']
    assert client.calls[0]['task']=='document_checkpoint_review_repair'
    for field in ('events','evidence_source_pool','current_full_document','earlier_event_ledger'):
        assert sent[field]==payload[field]
    assert sent['repair_contract']=={
        'maximum_semantic_repair_rounds':1,
        'original_evidence_pool_sha256':current.digest(payload['evidence_source_pool']),
        'source_context_added':False,'positive_result_required':False}
    assert sent['original_proposals']==[event['semantic_review']]
    # The second invocation sees attempted=True on the event, so no second model
    # revision occurs even if another technical reason remains.
    again,second_audit=run(revised[0],previous,sources,payload,client)
    assert len(client.calls)==1 and second_audit['attempted'] is False
    assert again==revised


def test_CEAUD007_only_failed_targets_are_revised_while_shared_source_pool_stays_exact():
    event,previous,sources,corrected,payload=setup()
    sibling=deepcopy(event)
    sibling.update(event_id='SIBLING',checkpoint_status='CANDIDATE_NEW_EVIDENCE',validation_reasons=[])
    sibling['semantic_review']['event_id']='SIBLING'
    sibling_input=deepcopy(payload['events'][0]);sibling_input['event']['event_id']='SIBLING'
    payload['events'].append(sibling_input)
    original_sibling=deepcopy(sibling);original_payload=deepcopy(payload)
    unrequested=deepcopy(corrected);unrequested['event_id']='SIBLING'
    client=FakeClient({'items':[corrected,unrequested]})
    revised,audit=current.run_review_feedback([event,sibling],payload,[previous,event,sibling],
                                            sources,[],client,CONFIG)
    assert revised[0]['checkpoint_status']=='CANDIDATE_NEW_EVIDENCE'
    assert revised[1]==original_sibling
    assert audit['target_event_ids']==[event['event_id']]
    assert audit['unexpected_event_ids']==['SIBLING']
    sent=client.calls[0]['payload']
    assert [item['event']['event_id'] for item in sent['events']]==[event['event_id']]
    for field in ('evidence_source_pool','current_full_document','earlier_event_ledger'):
        assert sent[field]==payload[field]
    assert payload==original_payload


@pytest.mark.parametrize('items_kind',['missing','duplicated','unexpected'])
def test_CEAUD007_missing_duplicate_or_unexpected_target_cannot_create_positive(items_kind):
    event,previous,sources,corrected,payload=setup()
    wrong=deepcopy(corrected);wrong['event_id']='UNREQUESTED'
    items={'missing':[],'duplicated':[corrected,corrected],'unexpected':[wrong]}[items_kind]
    client=FakeClient({'items':items})
    revised,audit=run(event,previous,sources,payload,client)
    assert_original_preserved(revised[0],event)
    assert revised[0]['review_refinement']['status']=='TARGET_MISSING_OR_DUPLICATED'
    assert len(client.calls)==1
    again,_=run(revised[0],previous,sources,payload,client)
    assert len(client.calls)==1 and again==revised
    assert audit['unexpected_event_ids']==(['UNREQUESTED'] if items_kind=='unexpected' else [])


def test_CEAUD007_unrequested_proposal_is_audited_but_never_inserted():
    event,previous,sources,corrected,payload=setup()
    other=deepcopy(corrected);other['event_id']='UNREQUESTED'
    revised,audit=run(event,previous,sources,payload,FakeClient({'items':[corrected,other]}))
    assert [e['event_id'] for e in revised]==[event['event_id']]
    assert revised[0]['checkpoint_status']=='CANDIDATE_NEW_EVIDENCE'
    assert audit['unexpected_event_ids']==['UNREQUESTED']


@pytest.mark.parametrize('mutation',['broad_observation','unknown_previous','invented_quote'])
def test_CEAUD007_returned_proposal_must_pass_same_source_and_contract_checks(mutation):
    event,previous,sources,corrected,payload=setup()
    if mutation=='broad_observation':
        corrected['observation_quotes']=[{'source_id':'C','quote':sources[1]['text']}]
        corrected['observation_availability'][0]['observation']=corrected['observation_quotes'][0]
    elif mutation=='unknown_previous':corrected['previous_event_id']='UNPROVIDED'
    else:corrected['observation_quotes']=[{'source_id':'C','quote':'原文不存在的患者情况'}]
    client=FakeClient({'items':[corrected]})
    revised,audit=run(event,previous,sources,payload,client)
    assert revised[0]['checkpoint_status'].startswith('UNCERTAIN')
    assert revised[0]['semantic_review']['observation_quotes']==corrected['observation_quotes']
    assert len(client.calls)==1
    assert revised[0]['review_refinement']['status']=='MODEL_REVISION_VALIDATED'
    assert refine.repair_triggers(revised[0])==[]


def test_CEAUD007_model_can_keep_semantic_uncertainty_without_positive_fallback():
    event,previous,sources,corrected,payload=setup()
    corrected['checkpoint']='uncertain'
    revised,audit=run(event,previous,sources,payload,FakeClient({'items':[corrected]}))
    assert revised[0]['checkpoint_status']=='UNCERTAIN_SEMANTIC_JUDGMENT'
    assert revised[0]['semantic_review']['checkpoint']=='uncertain'
    assert audit['status']=='COMPLETE'


@pytest.mark.parametrize('case',['disabled','semantic_only','valid_proxy','already_attempted'])
def test_CEAUD007_does_not_repair_pure_semantic_uncertainty_or_repeat_attempt(case):
    event,previous,sources,corrected,payload=setup()
    config=CONFIG
    if case=='disabled':config={'review_feedback_max_rounds':0}
    elif case=='semantic_only':
        event['checkpoint_status']='UNCERTAIN_SEMANTIC_JUDGMENT'
        event['semantic_review']['checkpoint']='uncertain'
    elif case=='already_attempted':event['review_refinement']={'attempted':True}
    else:
        event['validation_reasons']=['NEW_INFORMATION_AVAILABILITY_ONLY_PROXY_OR_UNRESOLVED']
        corrected['observation_availability'][0]['availability']='record_proxy_only'
        event['semantic_review']=corrected
        event['availability_validation']=[dict(corrected['observation_availability'][0],
            is_declared_observation=True,eligible_as_known_prechoice=False,
            validated_observation={'invalid':[]},validated_basis={'invalid':[]})]
        event['validated_observation_citations']={'valid':[],'invalid':[],'ambiguous':[]}
    original=deepcopy(event);client=FakeClient({'items':[corrected]})
    revised,audit=run(event,previous,sources,payload,client,config)
    assert not client.calls and revised==[original] and audit['attempted'] is False


@pytest.mark.parametrize('mutation',['block_ids','text','coordinates','allowed_spans','budget'])
def test_CEAUD007_context_mismatch_or_feedback_budget_does_not_call_model(mutation):
    event,previous,sources,corrected,payload=setup()
    config=CONFIG
    if mutation=='block_ids':payload['events'][0]['evidence_context']['block_ids']=[]
    elif mutation=='text':payload['evidence_source_pool'][0]['text']+='not originally supplied'
    elif mutation=='coordinates':payload['evidence_source_pool'][0]['start']+=1
    elif mutation=='allowed_spans':
        sid=next(iter(event['evidence_context']['allowed_spans']))
        event['evidence_context']['allowed_spans'][sid][0]['start']+=1
    else:config={**CONFIG,'max_review_payload_characters':1}
    client=FakeClient({'items':[corrected]})
    revised,audit=run(event,previous,sources,payload,client,config)
    assert not client.calls and audit['attempted'] is False
    assert audit['status']=='REPAIR_NOT_APPLIED'
    assert_original_preserved(revised[0],event)


def test_CEAUD007_returned_json_is_schema_checked_again_before_revalidation():
    event,previous,sources,corrected,payload=setup()
    del corrected['rationale']
    client=FakeClient({'items':[corrected]})
    with patch.object(refine,'revalidate',side_effect=AssertionError('must not be called')) as revalidate:
        revised,audit=run(event,previous,sources,payload,client)
    revalidate.assert_not_called()
    assert len(client.calls)==1 and audit['error']=='ValidationError'
    assert_original_preserved(revised[0],event)


@pytest.mark.parametrize('failure',['plain','bound','foreign_task','tampered_hash'])
def test_CEAUD007_failure_uses_own_exception_receipt_never_shared_list_tail(failure):
    event,previous,sources,corrected,payload=setup()
    own={'request_sha256':'own-failed-repair','task':'document_checkpoint_review_repair',
         'status':'FAILED','attempts':[{'error':'SSLError'}]}
    unrelated={'request_sha256':'other-completed','task':'document_checkpoint_review_repair',
               'status':'SUCCESS','attempts':[{'status':'SUCCESS'}]}

    class FailingClient(FakeClient):
        def ask(self,task,payload,schema):
            self.calls.append(deepcopy({'task':task,'payload':payload}))
            self.receipts.extend([deepcopy(own),deepcopy(unrelated)])
            if failure=='plain':raise RuntimeError('synthetic failure')
            exc=ModelRequestFailed(own)
            if failure=='foreign_task':exc.receipt['task']='document_event_discovery'
            if failure=='tampered_hash':exc.receipt['request_sha256']='wrong-hash'
            raise exc

    client=FailingClient()
    revised,audit=run(event,previous,sources,payload,client)
    assert_original_preserved(revised[0],event)
    assert audit['status']=='REPAIR_NOT_APPLIED' and len(client.calls)==1
    if failure=='bound':assert audit['receipt']==own
    else:assert 'receipt' not in audit
    assert client.receipts[-1]==unrelated
    again,_=run(revised[0],previous,sources,payload,client)
    assert len(client.calls)==1 and again==revised


@pytest.mark.parametrize('mutation',['missing_hash','failed_status','wrong_task','client_mutated_context'])
def test_CEAUD007_invalid_receipt_or_mutating_client_cannot_create_positive(mutation):
    event,previous,sources,corrected,payload=setup()

    class InvalidClient(FakeClient):
        def ask(self,task,submitted,schema):
            result,receipt=super().ask(task,submitted,schema)
            if mutation=='missing_hash':del receipt['request_sha256']
            elif mutation=='failed_status':receipt['status']='FAILED'
            elif mutation=='wrong_task':receipt['task']='document_event_discovery'
            else:submitted['current_full_document'][0]['text']+='new text'
            return result,receipt

    client=InvalidClient({'items':[corrected]})
    original=deepcopy(payload)
    revised,audit=run(event,previous,sources,payload,client)
    assert_original_preserved(revised[0],event)
    assert audit['status']=='REPAIR_NOT_APPLIED' and len(client.calls)==1
    assert payload==original
    assert audit['error_code'] in ('REVIEW_FEEDBACK_RECEIPT_INVALID','REVIEW_FEEDBACK_CLIENT_MUTATED_PAYLOAD')


def test_CEAUD007_arbitrary_provider_error_text_is_not_copied_into_audit():
    event,previous,sources,corrected,payload=setup()
    client=FakeClient()
    with patch.object(client,'ask',side_effect=ValueError('credential-and-clinical-sentinel')):
        revised,audit=run(event,previous,sources,payload,client)
    assert 'credential-and-clinical-sentinel' not in json.dumps(audit)
    assert audit['error']=='ValueError' and audit['error_code'] is None
    assert_original_preserved(revised[0],event)


def test_CEAUD007_revalidation_exception_retains_original_without_second_request():
    event,previous,sources,corrected,payload=setup()
    client=FakeClient({'items':[corrected]})
    with patch.object(refine,'revalidate',side_effect=ValueError('synthetic revalidation failure')):
        revised,audit=run(event,previous,sources,payload,client)
    assert_original_preserved(revised[0],event)
    assert revised[0]['review_refinement']['status']=='REPAIR_REVALIDATION_FAILED'
    assert audit['event_results'][event['event_id']]=='REPAIR_REVALIDATION_FAILED'
    again,_=run(revised[0],previous,sources,payload,client)
    assert len(client.calls)==1 and again==revised


@pytest.mark.parametrize('field,value',[
    ('review_feedback_max_rounds',2),('review_feedback_max_rounds',True),
    ('review_feedback_max_rounds',-1),('discovery_feedback_max_rounds',2),
    ('final_graph_validation','true'),('include_support_documents',1)])
def test_CEAUD007_invalid_configuration_fails_before_model_call(field,value):
    event,previous,sources,corrected,payload=setup();client=FakeClient({'items':[corrected]})
    with pytest.raises(ValueError):run(event,previous,sources,payload,client,{**CONFIG,field:value})
    assert not client.calls


def test_CEAUD007_issue_map_references_existing_required_tests():
    path=Path(__file__).parent/'review_feedback_issue_map.json'
    issue_map=json.loads(path.read_text())
    assert issue_map['clinical_gold'] is False
    issue=next(item for item in issue_map['issues'] if item['id']=='CE-AUD-007')
    text=Path(__file__).read_text()
    for test in issue['required_tests']:assert 'def '+test+'(' in text

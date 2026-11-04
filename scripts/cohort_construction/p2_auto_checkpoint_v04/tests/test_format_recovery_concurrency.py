"""CE-AUD-002: request-specific recovery under controlled thread interleaving.

All provider responses are synthetic and local; no clinical data or network.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Lock
from unittest.mock import Mock, patch

import pytest
import requests

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest
from scripts.cohort_construction.p2_auto_checkpoint_model_rerun import run as deepseek
from scripts.cohort_construction.p2_auto_checkpoint_shape_retry import run as qwen
from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport


SCHEMA={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
PAYLOAD={'instruction':'synthetic frozen task','current_full_document':[{'text':'synthetic only'}],
         'evidence_source_pool':[]}
WRAPPERS=[(qwen,'qwen3.8-max'),(deepseek,'deepseek-v4.1-flash')]


def make_client(tmp_path,module,model):
    cfg={'model_temperature':0,'max_output_tokens':32,'semantic_model':model,
         'max_concurrent_model_calls':2}
    settings=ModelAPISettings('synthetic-secret','https://apicz.boyuerichdata.com/v1',model,'disabled')
    c=module.ShapeRetryClient(tmp_path,cfg,settings)
    key=digest([settings.chat_completions_url,module.body_for(settings,cfg,'target',PAYLOAD,SCHEMA)])
    return c,key


def response(content,finish_reason='stop'):
    r=Mock();r.json.return_value={'choices':[{'finish_reason':finish_reason,'message':{'content':content}}]}
    return r


@pytest.mark.parametrize('module,model',WRAPPERS)
@pytest.mark.parametrize('invalid_content',['{}','{'])
def test_CEAUD002_own_schema_or_json_failure_recovers_without_interleaving(tmp_path,module,model,invalid_content):
    c,key=make_client(tmp_path,module,model)
    session=Mock();session.post.side_effect=[response(invalid_content) for _ in range(3)]+[
        response('{"ok":true}')]
    with patch.object(transport._thread,'session',session,create=True),patch.object(transport.time,'sleep'):
        result,receipt=c.ask('target',PAYLOAD,SCHEMA)
    assert result=={'ok':True} and receipt['request_sha256']!=key
    assert session.post.call_count==4
    assert c.receipts[0]['status']=='FAILED' and c.receipts[0]['request_sha256']==key
    assert len(c.recovery_records)==1 and c.recovery_records[0]['status']=='SUCCESS'


@pytest.mark.parametrize('module,model',WRAPPERS)
def test_CEAUD002_interleaved_other_success_does_not_hide_own_schema_failure(tmp_path,module,model):
    c,key=make_client(tmp_path,module,model)
    target_recorded,other_completed=Event(),Event()
    original_record=c._record_receipt
    calls=[];calls_lock=Lock()

    def record(receipt):
        original_record(receipt)
        if receipt['request_sha256']==key and receipt['status']=='FAILED':
            target_recorded.set()
            assert other_completed.wait(5),'other request did not complete'

    def post(*args,**kwargs):
        body=deepcopy(kwargs['json'])
        with calls_lock:calls.append(body)
        request=json.loads(body['messages'][1]['content'])
        if request['task']=='target' and 'format_recovery_contract' not in request['input']:
            return response('{}')  # All three original attempts fail required-field validation.
        return response('{"ok":true}')

    def other_request():
        assert target_recorded.wait(5),'target failure was not recorded'
        try:return c.ask('other',PAYLOAD,SCHEMA)
        finally:other_completed.set()

    def session():
        result=Mock();result.post.side_effect=post;return result

    with patch.object(c,'_record_receipt',side_effect=record), \
            patch.object(transport.requests,'Session',side_effect=session), \
            patch.object(transport.time,'sleep'),ThreadPoolExecutor(max_workers=2) as pool:
        target=pool.submit(c.ask,'target',PAYLOAD,SCHEMA)
        other=pool.submit(other_request)
        assert other.result(timeout=10)[0]=={'ok':True}
        assert target.result(timeout=10)[0]=={'ok':True}

    assert c.receipts[0]['request_sha256']==key
    assert c.receipts[0]['status']=='FAILED'
    assert c.receipts[1]['status']=='SUCCESS' and c.receipts[1]['request_sha256']!=key
    feedback=[json.loads(b['messages'][1]['content']) for b in calls
              if 'format_recovery_contract' in json.loads(b['messages'][1]['content'])['input']]
    assert len(feedback)==1
    sent=feedback[0]['input']
    assert {k:sent[k] for k in PAYLOAD if k!='instruction'}=={k:v for k,v in PAYLOAD.items() if k!='instruction'}
    assert sent['format_recovery_contract']['original_request_sha256']==key
    assert len(c.recovery_records)==1 and c.recovery_records[0]['status']=='SUCCESS'
    assert not (Path(tmp_path)/(key+'.json')).exists()


@pytest.mark.parametrize('module,model',WRAPPERS)
def test_CEAUD002_interleaved_other_schema_cannot_turn_network_failure_into_feedback(tmp_path,module,model):
    c,key=make_client(tmp_path,module,model)
    target_recorded,other_completed=Event(),Event()
    original_record=c._record_receipt
    calls=[];calls_lock=Lock()

    def record(receipt):
        original_record(receipt)
        if receipt['request_sha256']==key and receipt['status']=='FAILED':
            target_recorded.set()
            assert other_completed.wait(5),'other schema failure was not recorded'

    def post(*args,**kwargs):
        request=json.loads(kwargs['json']['messages'][1]['content'])
        with calls_lock:calls.append(request)
        if request['task']=='target':raise requests.ConnectionError('synthetic network failure')
        return response('{}')

    def other_request():
        assert target_recorded.wait(5),'target failure was not recorded'
        try:
            # Direct base call intentionally leaves the other schema failure
            # as the newest receipt, without invoking its wrapper feedback.
            with pytest.raises(transport.ModelRequestFailed):
                transport.Client.ask(c,'other',PAYLOAD,SCHEMA)
        finally:other_completed.set()

    def session():
        result=Mock();result.post.side_effect=post;return result

    with patch.object(c,'_record_receipt',side_effect=record), \
            patch.object(transport.requests,'Session',side_effect=session), \
            patch.object(transport.time,'sleep'),ThreadPoolExecutor(max_workers=2) as pool:
        target=pool.submit(c.ask,'target',PAYLOAD,SCHEMA);other=pool.submit(other_request)
        other.result(timeout=10)
        with pytest.raises(transport.ModelRequestFailed) as caught:target.result(timeout=10)
    assert caught.value.request_sha256==key
    assert c.receipts[-1]['request_sha256']!=key
    assert c.receipts[-1]['attempts'][0]['error']=='ValidationError'
    assert len(calls)==6
    assert all('format_recovery_contract' not in item['input'] for item in calls)
    assert not c.recovery_records


@pytest.mark.parametrize('module,model',WRAPPERS)
@pytest.mark.parametrize('own_error,should_recover',[('ValidationError',True),('SSLError',False)])
def test_CEAUD002_legacy_mock_fallback_matches_request_hash(tmp_path,module,model,own_error,should_recover):
    c,key=make_client(tmp_path,module,model)

    def request(task,payload,schema):
        if 'format_recovery_contract' in payload:return {'ok':True},{'request_sha256':'feedback'}
        c.receipts.extend([
            {'request_sha256':key,'attempts':[{'error':own_error}]},
            {'request_sha256':'unrelated','attempts':[{'error':'ValidationError'}]}])
        raise RuntimeError('legacy synthetic failure')

    with patch.object(transport.Client,'ask',side_effect=request) as ask:
        if should_recover:assert c.ask('target',PAYLOAD,SCHEMA)[0]=={'ok':True}
        else:
            with pytest.raises(RuntimeError):c.ask('target',PAYLOAD,SCHEMA)
    assert ask.call_count==(2 if should_recover else 1)


@pytest.mark.parametrize('module,model',WRAPPERS)
def test_CEAUD002_real_cached_original_is_returned_without_feedback(tmp_path,module,model):
    c,key=make_client(tmp_path,module,model)
    session=Mock();session.post.return_value=response('{"ok":true}')
    # Calls in the main thread might otherwise inherit a mock session from a
    # preceding transport test; retain and restore its state through patch.
    with patch.object(transport._thread,'session',session,create=True):
        first,first_receipt=c.ask('target',PAYLOAD,SCHEMA)
        c.failed_schema.add(key)
        second,second_receipt=c.ask('target',PAYLOAD,SCHEMA)
    assert first==second=={'ok':True}
    assert first_receipt['cached'] is False and second_receipt['cached'] is True
    assert session.post.call_count==1
    assert not c.recovery_records


@pytest.mark.parametrize('module,model',WRAPPERS)
def test_CEAUD002_truncation_does_not_trigger_format_feedback(tmp_path,module,model):
    c,key=make_client(tmp_path,module,model)
    session=Mock();session.post.return_value=response('unparsed synthetic output','length')
    with patch.object(transport._thread,'session',session,create=True),patch.object(transport.time,'sleep'):
        with pytest.raises(transport.ModelRequestFailed) as caught:c.ask('target',PAYLOAD,SCHEMA)
    assert caught.value.request_sha256==key
    assert session.post.call_count==3 and not c.recovery_records


@pytest.mark.parametrize('module,model',WRAPPERS)
def test_CEAUD002_failed_feedback_stops_after_one_changed_request(tmp_path,module,model):
    c,key=make_client(tmp_path,module,model)
    session=Mock();session.post.return_value=response('{}')
    with patch.object(transport._thread,'session',session,create=True),patch.object(transport.time,'sleep'):
        with pytest.raises(transport.ModelRequestFailed):c.ask('target',PAYLOAD,SCHEMA)
    assert session.post.call_count==6  # Three exact-body attempts per request; two bodies only.
    request_bodies=[json.loads(call.kwargs['json']['messages'][1]['content'])
                    for call in session.post.call_args_list]
    assert len({digest(body) for body in request_bodies})==2
    assert sum('format_recovery_contract' in body['input'] for body in request_bodies)==3
    assert len(c.recovery_records)==1 and c.recovery_records[0]['status']=='FAILED'
    assert not (Path(tmp_path)/(key+'.json')).exists()


def test_CEAUD002_issue_map_references_existing_required_tests():
    issue_map=json.loads((Path(__file__).parent/'transport_issue_map_v06.json').read_text())
    issue=next(item for item in issue_map['issues'] if item['id']=='CE-AUD-002')
    assert issue_map['clinical_gold'] is False
    for entry in issue['required_test_files']:
        text=(Path(__file__).resolve().parents[4]/entry['file']).read_text()
        for test in entry['tests']:assert 'def '+test+'(' in text

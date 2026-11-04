import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest, write
from scripts.cohort_construction.p2_auto_checkpoint_v06.experiments.fixed_model import (
    CacheOnlyClient, REMINDER, body_for, verify,
)


SCHEMA={'type':'object','properties':{'items':{'type':'array'}},'required':['items']}


def client(tmp_path):
    instance=object.__new__(CacheOnlyClient)
    instance.cache=tmp_path
    instance.config={'model_temperature':0,'max_output_tokens':16000}
    instance.settings=SimpleNamespace(model='deepseek-v4.1-flash',thinking=None,
        chat_completions_url='https://example.test/v1/chat/completions')
    instance.failed_schema=set()
    instance.receipts=[]
    instance._record_receipt=instance.receipts.append
    instance.missing=[]
    return instance


def save(instance,payload,result=None):
    result=result or {'items':[]}
    body=body_for(instance.settings,instance.config,'test',payload,SCHEMA)
    key=digest([instance.settings.chat_completions_url,body])
    write(instance.cache/(key+'.json'),dict(request=body,result=result,
        result_sha256=digest(result),receipt={'request_sha256':key,'status':'SUCCESS'}))
    return key


def test_exact_cache_hit_and_no_rewrite(tmp_path):
    c=client(tmp_path);p={'instruction':'Use original text','source_text':'synthetic'}
    key=save(c,p);original=(tmp_path/(key+'.json')).read_bytes()
    result,receipt=c.ask('test',p,SCHEMA)
    assert result=={'items':[]}
    assert receipt['cached'] is True
    assert (tmp_path/(key+'.json')).read_bytes()==original
    assert not c.missing


def test_cache_miss_never_creates_cache(tmp_path):
    c=client(tmp_path)
    with pytest.raises(RuntimeError,match='CACHE_ONLY_MISS:'):
        c.ask('test',{'instruction':'synthetic'},SCHEMA)
    assert len(c.missing)==1
    assert list(tmp_path.iterdir())==[]


def test_format_recovery_uses_actual_changed_hash(tmp_path):
    c=client(tmp_path);p={'instruction':'synthetic','source_text':'unchanged'}
    original=digest([c.settings.chat_completions_url,body_for(c.settings,c.config,'test',p,SCHEMA)])
    c.failed_schema.add(original)
    revised=dict(p,instruction=p['instruction']+'\n'+REMINDER,
        format_recovery_contract=dict(original_request_sha256=original,
        maximum_format_feedback_rounds=1,source_context_added=False,semantic_positive_required=False))
    changed=save(c,revised)
    _,receipt=c.ask('test',p,SCHEMA)
    assert receipt['request_sha256']==changed
    assert changed!=original
    assert not (tmp_path/(original+'.json')).exists()
    assert p=={'instruction':'synthetic','source_text':'unchanged'}


def test_corrupt_response_rejected(tmp_path):
    c=client(tmp_path);p={'instruction':'synthetic'};key=save(c,p)
    data=json.loads((tmp_path/(key+'.json')).read_text());data['result_sha256']='invalid'
    write(tmp_path/(key+'.json'),data)
    with pytest.raises(ValueError,match='CACHE_RESPONSE_HASH_MISMATCH'):
        c.ask('test',p,SCHEMA)


def test_changed_request_under_successful_filename_is_rejected(tmp_path):
    c=client(tmp_path);p={'instruction':'synthetic'};key=save(c,p)
    path=tmp_path/(key+'.json');data=json.loads(path.read_text());data['request']['temperature']=1
    write(path,data)
    with pytest.raises(ValueError,match='CACHE_REQUEST_HASH_MISMATCH'):
        c.ask('test',p,SCHEMA)


def test_protected_output_mutation_fails_closed(tmp_path):
    f=tmp_path/'old.json';f.write_text('original')
    write(tmp_path/'protected_old_hashes.json',{str(f):'mismatched'})
    with pytest.raises(ValueError,match='OLD_ARTIFACT_CHANGED'):verify(tmp_path)
    assert not json.loads((tmp_path/'old_artifact_protection_receipt.json').read_text())['passed']

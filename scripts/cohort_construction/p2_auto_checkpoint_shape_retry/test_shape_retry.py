import tempfile
from pathlib import Path
from unittest.mock import patch
from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_shape_retry.run import ShapeRetryClient,body_for
from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest,write

CFG={'model_temperature':0,'max_output_tokens':100,'semantic_model':'qwen3.8-max'}
SETTINGS=ModelAPISettings('secret','https://apicz.boyuerichdata.com/v1','qwen3.8-max','disabled')
P={'instruction':'frozen semantic task','current_full_document':[{'text':'synthetic only'}],'evidence_source_pool':[]}
S={'type':'object'}

def setup(tmp,failed=False,cached=False):
 key=digest([SETTINGS.chat_completions_url,body_for(SETTINGS,CFG,'task',P,S)])
 if failed:write(Path(tmp)/'invocations/a.json',{'status':'FAILED','request_sha256':key,'attempts':[{'error':'ValidationError'}]})
 if cached:write(Path(tmp)/(key+'.json'),{})
 return ShapeRetryClient(tmp,CFG,SETTINGS),key

def test_failed_exact_request_gets_one_source_unchanged_reminder():
 with tempfile.TemporaryDirectory() as d,patch.object(Client,'ask',return_value=({'ok':True},{'request_sha256':'newhash'})) as ask:
  c,key=setup(d,failed=True);c.ask('task',P,S);sent=ask.call_args.args[1]
  assert sent['current_full_document']==P['current_full_document'] and sent['evidence_source_pool']==[]
  assert sent['format_recovery_contract']['original_request_sha256']==key
  assert len(c.recovery_records)==1 and ask.call_count==1
  assert not (Path(d)/(key+'.json')).exists()

def test_success_cached_original_never_gets_reminder():
 with tempfile.TemporaryDirectory() as d,patch.object(Client,'ask',return_value=({},{})) as ask:
  c,key=setup(d,failed=True,cached=True);c.ask('task',P,S)
  assert ask.call_args.args[1]==P and not c.recovery_records

def test_unfailed_request_keeps_original_contract():
 with tempfile.TemporaryDirectory() as d,patch.object(Client,'ask',return_value=({},{})) as ask:
  c,key=setup(d);c.ask('task',P,S);assert ask.call_args.args[1]==P

def test_bad_format_feedback_has_no_positive_fallback_or_second_feedback():
 with tempfile.TemporaryDirectory() as d,patch.object(Client,'ask',side_effect=RuntimeError('failure')) as ask:
  c,key=setup(d,failed=True)
  try:c.ask('task',P,S)
  except RuntimeError:pass
  else:assert False
  assert ask.call_count==1 and c.recovery_records[0]['status']=='FAILED'

def test_new_schema_failure_also_gets_only_one_feedback_round():
 with tempfile.TemporaryDirectory() as d:
  c,key=setup(d)
  def original(task,payload,schema):
   if 'format_recovery_contract' not in payload:
    c.receipts.append({'request_sha256':key,'attempts':[{'error':'ValidationError'}]})
    raise RuntimeError('schema')
   return {},{'request_sha256':'new'}
  with patch.object(Client,'ask',side_effect=original) as ask:
   c.ask('task',P,S)
   assert ask.call_count==2 and c.recovery_records[0]['status']=='SUCCESS'

def test_network_failure_does_not_claim_format_failure():
 with tempfile.TemporaryDirectory() as d:
  c,key=setup(d)
  def original(*args):
   c.receipts.append({'request_sha256':key,'attempts':[{'error':'SSLError'}]})
   raise RuntimeError('network')
  with patch.object(Client,'ask',side_effect=original) as ask:
   try:c.ask('task',P,S)
   except RuntimeError:pass
   else:assert False
   assert ask.call_count==1 and not c.recovery_records

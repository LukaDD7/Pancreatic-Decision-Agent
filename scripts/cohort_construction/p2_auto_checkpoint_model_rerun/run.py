"""Versioned interface recovery around the frozen semantic reconstruction.

Cached original successes stay original. Only an uncached exact request with a
recorded JSON/schema failure receives one format reminder; its different body
gets its own actual hash. Never alias a changed request under an original key.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import hashlib
from urllib.parse import urlsplit
from scripts.cohort_construction.p2_auto_checkpoint_v04 import engine,run
from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client
from scripts.cohort_construction.p2_auto_checkpoint_v04.prompts import SYSTEM_INSTRUCTIONS
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest,write

REMINDER='''输出格式反馈：先前完全相同输入有JSON/schema校验失败。请返回schema规定的完整对象，不省略任何required字段，尤其events每项的rationale字符串。没有形成理由可填空字符串并保留uncertainty，不能编造临床理由。status/reason_category等必须使用schema的enum；遵守maxItems。所有临床事实、范围、事件与证据判断仍须依据本次原文，不能为了格式通过而强行肯定或连接。'''


def body_for(settings,config,task,payload,schema):
    body=dict(model=settings.model,temperature=config['model_temperature'],max_tokens=config['max_output_tokens'],
      response_format={'type':'json_object'},messages=[dict(role='system',content=SYSTEM_INSTRUCTIONS),
      dict(role='user',content=json.dumps(dict(task=task,schema=schema,input=payload),ensure_ascii=False))])
    if settings.thinking:body['thinking']={'type':settings.thinking}
    return body


class ShapeRetryClient(Client):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        if urlsplit(self.settings.chat_completions_url).hostname!='apicz.boyuerichdata.com' or self.settings.model!='deepseek-v4.1-flash':
            raise ValueError('SHAPE_RECOVERY_OUTSIDE_FROZEN_MODEL_OR_HOST')
        self.failed_schema=set()
        for path in (self.cache/'invocations').glob('*.json'):
            try:r=json.loads(path.read_text())
            except (OSError,json.JSONDecodeError):continue
            if r.get('status')=='FAILED' and any(a.get('error') in ['ValidationError','JSONDecodeError'] for a in r.get('attempts',[])):
                self.failed_schema.add(r['request_sha256'])
        self.recovery_records=[]

    def ask(self,task,payload,schema):
        original_body=body_for(self.settings,self.config,task,payload,schema)
        original_key=digest([self.settings.chat_completions_url,original_body])
        if (self.cache/(original_key+'.json')).exists():return super().ask(task,payload,schema)
        if original_key not in self.failed_schema:
            try:return super().ask(task,payload,schema)
            except RuntimeError as exc:
                failed=self.failure_receipt_for(exc,original_key)
                if not any(a.get('error') in ['ValidationError','JSONDecodeError']
                           for a in failed.get('attempts',[])):
                    raise
        revised=deepcopy(payload);revised['instruction']+='\n'+REMINDER
        revised['format_recovery_contract']=dict(original_request_sha256=original_key,
          maximum_format_feedback_rounds=1,source_context_added=False,semantic_positive_required=False)
        # Source, full text, evidence pools, event IDs and allowlists are byte-for-
        # byte identical Python values; only task instructions/metadata differ.
        for key in payload:
            if key!='instruction':assert revised[key]==payload[key]
        record=dict(original_request_sha256=original_key,task=task,format_feedback_rounds=1,source_context_added=False)
        write(self.cache/'shape_recovery_original_requests'/(original_key+'.json'),
              dict(request=original_body,request_sha256=original_key,
                   chat_completions_endpoint=self.settings.chat_completions_url))
        try:
            result,receipt=super().ask(task,revised,schema)
            record.update(actual_request_sha256=receipt['request_sha256'],status='SUCCESS')
            return result,receipt
        except Exception as exc:
            record.update(status='FAILED',error=type(exc).__name__);raise
        finally:
            self.recovery_records.append(record)
            write(self.cache/'shape_recovery_audits'/(original_key+'.json'),record)


def main():
    if not any(arg == '--config' or arg.startswith('--config=') for arg in sys.argv[1:]):
        sys.argv.extend(['--config', str(run.ROOT / 'config/p2_auto_checkpoint_v05_deepseek.json')])
    engine.Client=ShapeRetryClient
    try:run.main()
    finally:
        if '--output' in sys.argv:
            output=Path(sys.argv[sys.argv.index('--output')+1])
            if output.exists():
                files=list(Path(__file__).parent.glob('*.py'))
                write(output/'format_recovery_manifest.json',dict(
                  version='v0.5-format-contract-feedback-1',maximum_format_feedback_rounds=1,
                  source_context_added=False,semantic_positive_required=False,
                  clinical_validation=False,reference_labels_read=False,
                  code_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}))

if __name__=='__main__':main()

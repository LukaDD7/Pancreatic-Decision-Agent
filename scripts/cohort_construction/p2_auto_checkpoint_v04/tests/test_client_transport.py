"""Provider transport regressions with no external requests or clinical data."""
import tempfile
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import requests
from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_v04 import client
from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
    validate_provider_success_binding,
)


class TransportTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(client._thread,'session'):del client._thread.session

    def test_CE_REAL020_prepared_session_transport_does_not_resend_prepared_request(self):
        """Real requests 2.32 preparation works; no prepared body is coerced to bytes."""
        envelope = dict(choices=[dict(finish_reason='stop',
            message=dict(content='{"ok":true}'))])
        sent = Mock(status_code=200, text=json.dumps(envelope))
        sent.json.return_value = envelope
        schema = {'type':'object','required':['ok'],
            'properties':{'ok':{'type':'boolean'}}}
        settings = ModelAPISettings('credential-sentinel',
            'https://example.invalid/v1', 'test')
        cfg = dict(model_temperature=0, max_output_tokens=32,
            max_http_attempts_per_request=1)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(client.requests.Session, 'send',
                    return_value=sent) as send:
                provider = client.Client(Path(directory), cfg, settings)
                result, receipt = provider.ask('synthetic',
                    {'no_patient_data':True}, schema)
            request = send.call_args.args[0]
            self.assertEqual(result, {'ok':True})
            self.assertIsInstance(request.body, bytes)
            self.assertEqual(hashlib.sha256(request.body).hexdigest(),
                receipt['actual_provider_http_body_sha256'])
            self.assertEqual(receipt['status'], 'SUCCESS')
            self.assertEqual(send.call_count, 1)

    def test_CE_REAL021_cross_worker_total_budget_is_shared_and_cache_conserving(self):
        """An original invocation can be replayed once, not spend a second request."""
        schema = {'type':'object','required':['ok'],
            'properties':{'ok':{'type':'boolean'}}}
        settings = ModelAPISettings('credential-sentinel',
            'https://example.invalid/v1', 'test')
        cfg = dict(model_temperature=0, max_output_tokens=32,
            max_http_attempts_per_request=1, max_total_model_requests=1,
            max_total_http_attempts=1, execution_batch_id='FICTIONAL-CE-REAL021')
        envelope = dict(choices=[dict(finish_reason='stop',
            message=dict(content='{"ok":true}'))])
        sent = Mock(status_code=200, text=json.dumps(envelope))
        sent.json.return_value = envelope
        key = (settings.chat_completions_url, settings.model, cfg['execution_batch_id'])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(client.requests.Session, 'send', return_value=sent) as send:
            try:
                provider = client.Client(Path(directory), cfg, settings)
                _, first = provider.ask('synthetic', {'no_patient_data':True}, schema)
                with self.assertRaisesRegex(ValueError,
                        'MODEL_TOTAL_BATCH_REQUEST_BUDGET_EXHAUSTED'):
                    provider.ask('different', {'no_patient_data':True}, schema)
                _, cached = provider.ask('synthetic', {'no_patient_data':True}, schema)
                self.assertTrue(cached['cached'])
                self.assertFalse(cached['provider_invocation_occurred'])
                self.assertEqual(cached['invocation_id'], first['invocation_id'])
                self.assertEqual(send.call_count, 1)
                budget = client._batch_budgets[key]
                self.assertEqual(budget['requests'], 1)
                self.assertEqual(budget['http_attempts'], 1)
                self.assertEqual(budget['invocations'][0]['cached_replays'], 1)
            finally:
                client._batch_budgets.pop(key, None)

    def test_NET001_tls_failure_recreates_session_and_keeps_same_request(self):
        first,second=Mock(),Mock()
        first.post.side_effect=requests.exceptions.SSLError('peer closed TLS')
        response=Mock();response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        second.post.return_value=response
        cfg={'model_temperature':0,'max_output_tokens':32,'max_concurrent_model_calls':2}
        settings=ModelAPISettings('test-secret','https://example.invalid/v1','test')
        with tempfile.TemporaryDirectory() as directory,patch.object(client.requests,'Session',side_effect=[first,second]),patch.object(client.time,'sleep'):
            c=client.Client(Path(directory),cfg,settings)
            result,receipt=c.ask('synthetic',{'no_patient_data':True},{'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}})
            self.assertTrue(result['ok']);self.assertEqual(receipt['status'],'SUCCESS')
            first.close.assert_called_once();self.assertEqual(first.post.call_args.kwargs['json'],second.post.call_args.kwargs['json'])
            self.assertNotIn('verify',second.post.call_args.kwargs)
            self.assertEqual([a['status'] for a in receipt['attempts']],['FAILED','SUCCESS'])

    def test_NET002_clients_share_global_configured_concurrency_limit(self):
        cfg={'model_temperature':0,'max_output_tokens':32,'max_concurrent_model_calls':2}
        settings=ModelAPISettings('test-secret','https://example.invalid/v1','test')
        with tempfile.TemporaryDirectory() as directory:
            a=client.Client(Path(directory)/'a',cfg,settings);b=client.Client(Path(directory)/'b',cfg,settings)
            self.assertIs(a.slots,b.slots)

    def test_CE_NET003_ten_attempt_limit_is_explicit_and_preserves_request_and_diagnostics(self):
        session=Mock()
        session.post.side_effect=requests.exceptions.ConnectionError('synthetic transport failure')
        cfg={'model_temperature':0,'max_output_tokens':32,'max_http_attempts_per_request':10}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        with tempfile.TemporaryDirectory() as directory,patch.object(client.requests,'Session',return_value=session),patch.object(client.time,'sleep') as sleep:
            c=client.Client(Path(directory),cfg,settings)
            with self.assertRaises(client.ModelRequestFailed) as caught:
                c.ask('synthetic',{'no_patient_data':True},schema)
            attempts=caught.exception.receipt['attempts']
            self.assertEqual(len(attempts),10)
            self.assertEqual([row['attempt'] for row in attempts],list(range(1,11)))
            self.assertEqual({row['error'] for row in attempts},{'ConnectionError'})
            self.assertEqual(session.post.call_count,10)
            bodies=[call.kwargs['json'] for call in session.post.call_args_list]
            self.assertTrue(all(body==bodies[0] for body in bodies))
            self.assertEqual(sleep.call_count,9)
            self.assertEqual([call.args[0] for call in sleep.call_args_list],
                             [2,4,8,16,32,60,60,60,60])
        for invalid in (0,True,'10'):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError,'MODEL_HTTP_ATTEMPTS_INVALID'):
                    client.Client(Path(directory),dict(cfg,max_http_attempts_per_request=invalid),settings)
        for key,value in [('retry_backoff_base_seconds',0),
                          ('retry_backoff_base_seconds',True),
                          ('retry_backoff_max_seconds',1),
                          ('min_request_interval_seconds',-1),
                          ('min_request_interval_seconds',3601)]:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError,'MODEL_RATE_CONFIG_INVALID'):
                    client.Client(Path(directory),dict(cfg,**{key:value}),settings)

    def test_CE_NET004_request_start_pacing_is_bounded_and_audited(self):
        session=Mock()
        session.post.side_effect=requests.exceptions.HTTPError(response=Mock(status_code=429))
        cfg={'model_temperature':0,'max_output_tokens':32,'max_http_attempts_per_request':3,
             'retry_backoff_base_seconds':10,'retry_backoff_max_seconds':15,
             'min_request_interval_seconds':5}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        monotonic=iter([0,0,0,0,10,10,30,30,45])
        pacing=[client.Lock(),None]
        with tempfile.TemporaryDirectory() as directory,patch.object(client.requests,'Session',return_value=session), \
                patch.dict(client._pacing_locks,{'https://example.invalid/v1/chat/completions':pacing}), \
                patch.object(client.time,'monotonic',side_effect=lambda:next(monotonic)), \
                patch.object(client.time,'sleep') as sleep:
            c=client.Client(Path(directory),cfg,settings)
            with self.assertRaises(client.ModelRequestFailed) as caught:
                c.ask('synthetic',{'no_patient_data':True},{'type':'object'})
            receipt=caught.exception.receipt
            self.assertEqual([call.args[0] for call in sleep.call_args_list],[10,5,15,5])
            self.assertEqual([row.get('retry_delay_seconds') for row in receipt['attempts']],[10,15,None])
            self.assertEqual(receipt['pacing_seconds_total'],10)
            self.assertEqual(receipt['min_request_interval_seconds'],5)
            self.assertEqual(receipt['retry_backoff_max_seconds'],15)
        self.assertNotIn('credential-sentinel',json.dumps(receipt))

    def test_CE_NET004_pacing_is_shared_across_clients_on_same_endpoint(self):
        pacing=[client.Lock(),None]
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        cfg={'model_temperature':0,'max_output_tokens':32,'min_request_interval_seconds':5}
        with tempfile.TemporaryDirectory() as directory,patch.dict(
                client._pacing_locks,{'https://example.invalid/v1/chat/completions':pacing}), \
                patch.object(client.time,'monotonic',side_effect=[100,100,101,105]), \
                patch.object(client.time,'sleep') as sleep:
            first=client.Client(Path(directory)/'a',cfg,settings)
            second=client.Client(Path(directory)/'b',cfg,settings)
            self.assertEqual(first._wait_for_request_start(),0)
            self.assertEqual(second._wait_for_request_start(),4)
            sleep.assert_called_once_with(4)
            self.assertEqual(pacing[1],105)

    def test_CEAUD002_failure_exception_binds_sanitized_receipt_to_request(self):
        session=Mock();response=Mock()
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{
            'content':'{"ok":"failed-clinical-sentinel"}'}}]}
        session.post.return_value=response
        cfg={'model_temperature':0,'max_output_tokens':32}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        with tempfile.TemporaryDirectory() as directory,patch.object(client.requests,'Session',return_value=session),patch.object(client.time,'sleep'):
            c=client.Client(directory,cfg,settings)
            with self.assertRaises(client.ModelRequestFailed) as caught:
                c.ask('synthetic',{'no_patient_data':True},schema)
            failure=caught.exception
            self.assertIsInstance(failure,RuntimeError)
            self.assertEqual(failure.request_sha256,c.receipts[0]['request_sha256'])
            self.assertEqual(failure.receipt['status'],'FAILED')
            self.assertEqual(failure.receipt['attempts'][0]['schema_path'],['properties','ok','type'])
            c.receipts[0]['attempts'].clear()
            self.assertEqual(len(failure.receipt['attempts']),3)
            archive=list((Path(directory)/'invocations').glob('*.json'))
            self.assertEqual(len(archive),1)
            saved=archive[0].read_text()
            self.assertNotIn('credential-sentinel',saved)
            self.assertNotIn('failed-clinical-sentinel',saved)
            self.assertNotIn('response',json.loads(saved))

    def test_CEAUD002_output_truncation_reason_is_safe_and_not_schema_failure(self):
        session=Mock();response=Mock()
        response.json.return_value={'choices':[{'finish_reason':'length','message':{
            'content':'failed-clinical-sentinel'}}]}
        session.post.return_value=response
        cfg={'model_temperature':0,'max_output_tokens':32}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        with tempfile.TemporaryDirectory() as directory,patch.object(client.requests,'Session',return_value=session),patch.object(client.time,'sleep'):
            c=client.Client(directory,cfg,settings)
            with self.assertRaises(client.ModelRequestFailed) as caught:
                c.ask('synthetic',{}, {'type':'object'})
            attempts=caught.exception.receipt['attempts']
            self.assertEqual(len(attempts),3)
            self.assertEqual({a['reason'] for a in attempts},{'OUTPUT_TRUNCATED'})
            self.assertEqual({a['error'] for a in attempts},{'ValueError'})
            self.assertNotIn('failed-clinical-sentinel',json.dumps(caught.exception.receipt))

    def test_CEAUD002_failure_lookup_cannot_use_other_hash_or_stale_same_hash(self):
        cfg={'model_temperature':0,'max_output_tokens':32}
        settings=ModelAPISettings('secret','https://example.invalid/v1','test')
        with tempfile.TemporaryDirectory() as directory:
            c=client.Client(directory,cfg,settings)
            own={'request_sha256':'own','status':'FAILED','attempts':[{'error':'SSLError'}]}
            other={'request_sha256':'other','status':'FAILED','attempts':[{'error':'ValidationError'}]}
            c._record_receipt(own);c._record_receipt(other)
            self.assertEqual(c.failure_receipt_for(RuntimeError(), 'own'),own)
            self.assertEqual(c.failure_receipt_for(client.ModelRequestFailed(other), 'own'),{})
            c._record_receipt({'request_sha256':'own','status':'SUCCESS','attempts':[]})
            self.assertEqual(c.failure_receipt_for(RuntimeError(), 'own'),{})
    def test_CE_NET003_http_receipt_retains_status_without_provider_content(self):
        session=Mock();response=Mock()
        response.status_code=503
        response.raise_for_status.side_effect=requests.exceptions.HTTPError(response=response)
        session.post.return_value=response
        cfg={'model_temperature':0,'max_output_tokens':32,'max_http_attempts_per_request':1}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        with tempfile.TemporaryDirectory() as directory,patch.object(client.requests,'Session',return_value=session):
            c=client.Client(Path(directory),cfg,settings)
            with self.assertRaises(client.ModelRequestFailed) as caught:
                c.ask('synthetic',{'no_patient_data':True},{'type':'object'})
            attempt=caught.exception.receipt['attempts'][0]
            self.assertEqual({key: attempt[key] for key in ('attempt', 'status', 'error', 'http_status')},
                dict(attempt=1, status='FAILED', error='HTTPError', http_status=503))
            self.assertEqual(attempt['stage'], 'PROVIDER_HTTP_RESPONSE')
            self.assertEqual(attempt['reason_code'], 'PROVIDER_HTTP_503')
            self.assertEqual(attempt['provider_response_sha256'],
                caught.exception.failure_evidence['provider_response_sha256'])
            evidence = caught.exception.receipt['attempts'][0]['evidence']
            self.assertEqual(evidence['relative_path'],
                'invocations/%s/attempts/0001.json' % caught.exception.receipt['invocation_id'])
            self.assertNotIn('provider_response_text"', json.dumps(attempt))
            self.assertNotIn('credential-sentinel',json.dumps(caught.exception.receipt))
    def test_CE_NET005_raw_response_snapshot_precedes_http_json_and_schema_failures(self):
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        import hashlib
        for kind in ('HTTP_503','ENVELOPE_JSON','SCHEMA'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                response=Mock();response.status_code=503 if kind=='HTTP_503' else 200
                if kind=='HTTP_503':
                    response.text='synthetic HTTP failure body'
                    response.raise_for_status.side_effect=requests.exceptions.HTTPError(response=response)
                elif kind=='ENVELOPE_JSON':
                    response.text='{"choices":\n  malformed'
                    response.json.side_effect=lambda:json.loads(response.text)
                else:
                    envelope={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":"wrong type"}'}}]}
                    response.text=json.dumps(envelope);response.json.return_value=envelope
                session=Mock();session.post.return_value=response
                cfg={'model_temperature':0,'max_output_tokens':32,'max_http_attempts_per_request':1}
                with patch.object(client._thread,'session',session,create=True):
                    c=client.Client(Path(directory),cfg,settings)
                    with self.assertRaises(client.ModelRequestFailed) as caught:
                        c.ask('synthetic',{'no_patient_data':True},schema)
                invocation=caught.exception.receipt['invocation_id']
                snapshot=Path(directory)/'invocations'/invocation/'attempts/0001.provider-response.json'
                saved=json.loads(snapshot.read_text())
                self.assertEqual(saved['provider_response_text'],response.text)
                self.assertEqual(saved['provider_response_sha256'],hashlib.sha256(
                    response.text.encode('utf-8')).hexdigest())
                self.assertEqual(saved['invocation_id'],invocation)
                self.assertEqual(saved['transport_request_sha256'],caught.exception.request_sha256)
                self.assertNotIn('credential-sentinel',snapshot.read_text())
                if kind!='HTTP_503':
                    self.assertEqual(json.loads((snapshot.parent/'0001.json').read_text())['raw_response_snapshot'],
                        dict(relative_path=str(snapshot.relative_to(directory)),
                             sha256=saved['provider_response_sha256']))

    def test_CE_NET006_structural_envelope_and_content_failures_have_bound_replayable_diagnostics(self):
        from scripts.cohort_construction.p2_auto_checkpoint_v04.provider_diagnostics import (
            private_chain_evidence, replay_transport_evidence, validate_private_chain_evidence,
        )
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        cases=[('EMPTY_CHOICES',dict(choices=[]),'PROVIDER_ENVELOPE_PARSE'),
               ('NOT_OBJECT',[],'PROVIDER_ENVELOPE_PARSE'),
               ('MISSING_CONTENT',dict(choices=[dict(finish_reason='stop',message={})]),
                'PROVIDER_CONTENT_PARSE'),
               ('NULL_CONTENT',dict(choices=[dict(finish_reason='stop',message=dict(content=None))]),
                'PROVIDER_CONTENT_PARSE')]
        for label,envelope,expected_stage in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                response=Mock();response.status_code=200;response.text=json.dumps(envelope)
                response.json.return_value=envelope
                session=Mock();session.post.return_value=response
                cfg=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=1)
                with patch.object(client._thread,'session',session,create=True):
                    provider=client.Client(Path(directory),cfg,settings)
                    with self.assertRaises(client.ModelRequestFailed) as caught:
                        provider.ask('synthetic',{'no_patient_data':True},schema)
                evidence=caught.exception.failure_evidence
                self.assertEqual(evidence['terminal_stage'],expected_stage)
                if expected_stage=='PROVIDER_ENVELOPE_PARSE':
                    self.assertEqual(evidence['diagnostic']['kind'],'PROVIDER_ENVELOPE_STRUCTURE')
                    self.assertEqual(evidence['reason_code'],'PROVIDER_ENVELOPE_INVALID')
                else:
                    self.assertEqual(evidence['reason_code'],
                        'PROVIDER_CONTENT_NOT_TEXT' if label=='NULL_CONTENT' else 'PROVIDER_CONTENT_MISSING')
                self.assertTrue(replay_transport_evidence(evidence,schema)['evidence_binding_verified'])
                embedded=private_chain_evidence(evidence)
                validate_private_chain_evidence(embedded,embedded)

    def test_CEREM_ADV034_cached_result_retries_and_metadata_are_replayable(self):
        """Fresh and cached success retain every failed attempt and safe metadata."""
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        cfg=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=3)
        secret='credential-sentinel-private-provider-metadata'
        with tempfile.TemporaryDirectory() as directory:
            session=Mock()
            failure=Mock(status_code=503,text='{"error":"synthetic retry"}')
            failure.raise_for_status.side_effect=requests.exceptions.HTTPError(response=failure)
            envelope=dict(model='synthetic-model',usage=dict(input_tokens=3,output_tokens=2,
                nested_secret=secret),choices=[dict(finish_reason='stop',
                message=dict(content='{"ok":true}'))],private_extension=secret)
            success=Mock(status_code=200,text=json.dumps(envelope))
            success.json.return_value=envelope
            session.post.side_effect=[failure,success]
            with patch.object(client._thread,'session',session,create=True), \
                    patch.object(client.time,'sleep'):
                provider=client.Client(Path(directory),cfg,settings)
                result,receipt=provider.ask('synthetic',{'no_patient_data':True},schema)
            self.assertEqual(result,{'ok':True})
            self.assertEqual(receipt['usage'],dict(input_tokens=3,output_tokens=2))
            self.assertNotIn(secret,json.dumps(receipt['usage']))
            self.assertNotIn(secret,json.dumps(receipt['returned_model']))
            # Full provider evidence is intentionally retained in the private
            # chain/cache; only operational usage/model fields are allowlisted.
            self.assertEqual([row['status'] for row in receipt['attempts']],['FAILED','SUCCESS'])
            outcome=dict(stage='SUCCESS',review=result,cached=False,receipt=receipt)
            validate_provider_success_binding(receipt['terminal_attempt_evidence'],outcome,
                {'no_patient_data':True},{'no_patient_data':True},schema=schema)
            # A fresh receipt cannot pretend that an external cache reader has
            # recovered the preceding failed attempt. Read it from the immutable
            # per-attempt file, as the production cache path does.
            second,cached=provider.ask('synthetic',{'no_patient_data':True},schema)
            failed_pointer=receipt['attempts'][0]['evidence']
            archived=json.loads((Path(directory)/failed_pointer['relative_path']).read_text())
            self.assertEqual(second,result)
            self.assertTrue(cached['cached'])
            self.assertFalse(cached['provider_invocation_occurred'])
            self.assertFalse(cached['provider_transport_verified'])
            self.assertEqual(session.post.call_count,2)
            self.assertEqual(cached['attempts'][0]['archived_evidence'],
                client.private_chain_evidence(archived))
            outcome=dict(stage='SUCCESS',review=result,cached=True,receipt=cached)
            validate_provider_success_binding(cached['terminal_attempt_evidence'],outcome,
                {'no_patient_data':True},{'no_patient_data':True},cached=True,schema=schema)
            cache_path=Path(directory)/(cached['request_sha256']+'.json')
            saved=json.loads(cache_path.read_text())
            saved['result']={'ok':False};saved['result_sha256']=client.digest(saved['result'])
            cache_path.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError,'PROVIDER_CACHE_RESULT_RESPONSE_MISMATCH'):
                provider.ask('synthetic',{'no_patient_data':True},schema)
            saved=json.loads(cache_path.read_text())
            saved['result']={'ok':True};saved['result_sha256']=client.digest(saved['result'])
            saved['receipt']['attempts']=saved['receipt']['attempts'][-1:]
            cache_path.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError,'PROVIDER_RETRY_HISTORY_BINDING_MISMATCH'):
                provider.ask('synthetic',{'no_patient_data':True},schema)
        # Metadata failure is a retained failed attempt, not a success with an
        # arbitrary nested provider value promoted into operational receipts.
        envelope['model']='synthetic model\n'+secret
        with tempfile.TemporaryDirectory() as directory:
            session=Mock();session.post.return_value=success
            with patch.object(client._thread,'session',session,create=True):
                provider=client.Client(Path(directory),cfg,settings)
                with self.assertRaises(client.ModelRequestFailed) as caught:
                    provider.ask('synthetic',{'no_patient_data':True},schema)
            self.assertNotIn(secret,json.dumps(caught.exception.receipt))
    def test_CEAUD002_cached_task_identity_is_required(self):
        """A same-body cache entry cannot be replayed under another task receipt."""
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        cfg=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=1)
        with tempfile.TemporaryDirectory() as directory:
            envelope=dict(choices=[dict(finish_reason='stop',message=dict(content='{"ok":true}'))])
            response=Mock(status_code=200,text=json.dumps(envelope))
            response.json.return_value=envelope
            session=Mock();session.post.return_value=response
            with patch.object(client._thread,'session',session,create=True):
                provider=client.Client(Path(directory),cfg,settings)
                provider.ask('synthetic',{'no_patient_data':True},schema)
                path=Path(directory)/(provider.receipts[-1]['request_sha256']+'.json')
                saved=json.loads(path.read_text())
                saved['receipt']['task']='stale-task'
                path.write_text(json.dumps(saved))
                with self.assertRaisesRegex(ValueError,'PROVIDER_CACHE_TASK_MISMATCH') as caught:
                    provider.ask('synthetic',{'no_patient_data':True},schema)
            self.assertEqual(str(caught.exception),'PROVIDER_CACHE_TASK_MISMATCH')
            self.assertNotIn('credential-sentinel',str(caught.exception))

    def _rejecting_session(self, status, text):
        failure=Mock(status_code=status,text=text)
        failure.raise_for_status.side_effect=requests.exceptions.HTTPError(response=failure)
        session=Mock();session.post.return_value=failure
        return session

    def test_CE_NET005_a_non_retryable_4xx_is_not_repeated(self):
        """A request the provider rejected as sent must not be resent verbatim."""
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        cfg=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=5)
        with tempfile.TemporaryDirectory() as directory:
            session=self._rejecting_session(422,'{"error":"unprocessable"}')
            with patch.object(client._thread,'session',session,create=True), \
                    patch.object(client.time,'sleep') as sleep:
                provider=client.Client(Path(directory),cfg,settings)
                with self.assertRaises(client.ModelRequestFailed) as caught:
                    provider.ask('synthetic',{'no_patient_data':True},schema)
            receipt=caught.exception.receipt
            self.assertEqual(session.post.call_count,1)
            self.assertEqual(len(receipt['attempts']),1)
            self.assertEqual(receipt['attempts'][0]['retry_suppressed'],'NON_RETRYABLE_HTTP_STATUS')
            self.assertIsNone(receipt['attempts'][0].get('retry_delay_seconds'))
            self.assertEqual(receipt['attempts'][0]['http_status'],422)
            sleep.assert_not_called()

    def test_CE_NET005_a_retryable_429_is_still_retried(self):
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        cfg=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=3,
                 retry_backoff_base_seconds=1,retry_backoff_max_seconds=2)
        with tempfile.TemporaryDirectory() as directory:
            session=self._rejecting_session(429,'{"error":"rate limited"}')
            with patch.object(client._thread,'session',session,create=True), \
                    patch.object(client.time,'sleep'):
                provider=client.Client(Path(directory),cfg,settings)
                with self.assertRaises(client.ModelRequestFailed) as caught:
                    provider.ask('synthetic',{'no_patient_data':True},schema)
            self.assertEqual(session.post.call_count,3)
            self.assertEqual([row.get('retry_suppressed') for row in caught.exception.receipt['attempts']],
                             [None,None,None])

    def test_CE_NET006_an_input_overflow_rejection_is_terminal_and_labelled(self):
        """Oversized input is a shrink-the-request condition, not a retry loop."""
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        cfg=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=10)
        body='{"error":{"message":"This model maximum context length is 131072 tokens."}}'
        with tempfile.TemporaryDirectory() as directory:
            session=self._rejecting_session(400,body)
            with patch.object(client._thread,'session',session,create=True), \
                    patch.object(client.time,'sleep') as sleep:
                provider=client.Client(Path(directory),cfg,settings)
                with self.assertRaises(client.ModelRequestFailed) as caught:
                    provider.ask('synthetic',{'no_patient_data':True},schema)
            attempt=caught.exception.receipt['attempts'][0]
            self.assertEqual(session.post.call_count,1)
            self.assertEqual(attempt['reason'],'INPUT_OVERFLOW_SUSPECTED')
            self.assertIs(attempt['input_overflow_suspected'],True)
            self.assertEqual(attempt['retry_suppressed'],'PROVIDER_INPUT_OVERFLOW')
            # The transport reason code still replays from the HTTP status.
            self.assertEqual(attempt['reason_code'],'PROVIDER_HTTP_400')
            sleep.assert_not_called()

    def test_CE_NET007_a_server_error_is_still_retried(self):
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        cfg=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=2,
                 retry_backoff_base_seconds=1,retry_backoff_max_seconds=2)
        with tempfile.TemporaryDirectory() as directory:
            session=self._rejecting_session(503,'{"error":"unavailable"}')
            with patch.object(client._thread,'session',session,create=True), \
                    patch.object(client.time,'sleep'):
                provider=client.Client(Path(directory),cfg,settings)
                with self.assertRaises(client.ModelRequestFailed):
                    provider.ask('synthetic',{'no_patient_data':True},schema)
            self.assertEqual(session.post.call_count,2)

    def test_CE_NET008_read_timeout_is_configurable_and_defaults_to_history(self):
        """A bigger payload needs a longer read timeout, without changing old runs.

        The frozen 0.7.0 batch had four requests that ran past 180 s and one at
        1112 s. Raising the payload cap without also raising this read timeout
        would convert a large but answerable request into attempts that time
        out, which spends the batch's HTTP allowance on nothing.
        """
        schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
        settings=ModelAPISettings('credential-sentinel','https://example.invalid/v1','test')
        base=dict(model_temperature=0,max_output_tokens=32,max_http_attempts_per_request=1)
        seen=[]
        def session_for():
            session=Mock()
            def post(*args,**kwargs):
                seen.append(kwargs.get('timeout'))
                raise requests.exceptions.ConnectionError('synthetic transport failure')
            session.post.side_effect=post
            return session
        with tempfile.TemporaryDirectory() as directory:
            # No key at all keeps exactly the historical (20, 180) pair.
            with patch.object(client._thread,'session',session_for(),create=True), \
                    patch.object(client.time,'sleep'):
                with self.assertRaises(client.ModelRequestFailed):
                    client.Client(Path(directory),base,settings).ask(
                        'synthetic',{'no_patient_data':True},schema)
            # An explicit value is used verbatim and does not leak into others.
            with patch.object(client._thread,'session',session_for(),create=True), \
                    patch.object(client.time,'sleep'):
                provider=client.Client(Path(directory),dict(base,provider_read_timeout_seconds=1800),settings)
                self.assertEqual(provider.read_timeout_seconds,1800)
                with self.assertRaises(client.ModelRequestFailed):
                    provider.ask('synthetic',{'no_patient_data':True},schema)
        self.assertEqual(seen,[(20,180),(20,1800)])
        for invalid in (0,-1,True,'1800',3601,float('inf')):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError,'MODEL_READ_TIMEOUT_INVALID'):
                    client.Client(Path(directory),dict(base,provider_read_timeout_seconds=invalid),settings)

if __name__=='__main__':unittest.main()

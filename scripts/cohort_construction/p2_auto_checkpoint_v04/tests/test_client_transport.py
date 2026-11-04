"""Provider transport regressions with no external requests or clinical data."""
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import requests
from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_v04 import client


class TransportTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(client._thread,'session'):del client._thread.session

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
            self.assertEqual(attempt,dict(attempt=1,status='FAILED',error='HTTPError',http_status=503))
            self.assertNotIn('credential-sentinel',json.dumps(caught.exception.receipt))


if __name__=='__main__':unittest.main()

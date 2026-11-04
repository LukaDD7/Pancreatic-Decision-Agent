"""Second independent offline review; all HTTP responses are fictional.

Assertions below reproduce remaining defects, not repaired acceptance criteria.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import Mock, patch

import requests

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
from scripts.cohort_construction.p2_checkpoint_material_review_app import build
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read
from scripts.cohort_construction.p2_checkpoint_real_validation.batch import (
    _reconstruction_ledger, execute_batch, replay_batch,
)
from scripts.cohort_construction.p2_checkpoint_real_validation.budget import BatchModelBudget
from scripts.cohort_construction.p2_checkpoint_real_validation.tests.test_real_validation import (
    CONFIG, _write_synthetic_batch_case,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    build_graph_reconstruction_request, graph_reconstruction_client,
    run_graph_reconstruction, validate_graph_reconstruction_chain,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import (
    fixtures, proposed,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response


def revise(request):
    response = passing_graph_response(request)
    response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
        request_id='REVIEW2-REV', request_kind='EVENT_RECONSTRUCTION',
        reason='Fictional independent review', affected_event_ids=['EVENT-01'],
        affected_episode_ids=['EPISODE-01'],
        evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
        required_reconstruction=True)])
    return response


def raw_response(response):
    envelope = dict(choices=[dict(finish_reason='stop', message=dict(
        content=json.dumps(response, ensure_ascii=False)))])
    http = Mock()
    http.json.return_value = envelope
    return http


def hash_domain_probes(root):
    results = []
    for scenario in ('schema_rejection', 'connection_failure'):
        _, audit_request, audit_response = fixtures()
        request = build_graph_reconstruction_request(audit_request, audit_response)
        response = proposed(request)
        response['graph'] = {'、': ['、']}
        session = Mock()
        if scenario == 'schema_rejection':
            session.post.return_value = raw_response(response)
        else:
            session.post.side_effect = requests.exceptions.ConnectionError('synthetic')
        provider = transport.Client(root / scenario, dict(model_temperature=0,
            max_output_tokens=64, max_http_attempts_per_request=1),
            ModelAPISettings('synthetic', 'https://example.invalid/v1', 'synthetic'))
        ledger = BatchModelBudget(None, max_requests=1, max_http_attempts=1,
            max_http_attempts_per_request=1)
        context = dict(case_id='SYNTHETIC',
            graph_sha256=request['response_binding']['graph_sha256'])
        adapter = ledger.adapter('reconstruction', graph_reconstruction_client(provider), context)
        with patch.object(transport._thread, 'session', session, create=True):
            chain = run_graph_reconstruction(audit_request, audit_response, adapter)
        expected = _reconstruction_ledger(ledger.snapshot(), 'SYNTHETIC',
            request['response_binding']['graph_sha256'])
        assert validate_graph_reconstruction_chain(chain)['replay_verified']
        try:
            validate_graph_reconstruction_chain(chain,
                expected_request=(audit_request, audit_response), expected_provider=expected)
        except ValueError as exc:
            error = str(exc)
        else:
            raise AssertionError('Expected the current hash-domain defect')
        assert error == 'GRAPH_RECONSTRUCTION_LEDGER_REQUEST_BINDING_MISMATCH'
        assert chain['failure']['request_sha256'] != expected['request_sha256']
        results.append(dict(scenario=scenario, reproduced=True,
            standalone_replay_verified=True, parent_and_ledger_replay_error=error,
            response_preserved=chain['response_evidence_preserved'],
            invocation_preserved=bool(chain['failure'].get('provider_invocation_id')),
            transport_request_sha256=chain['failure']['request_sha256'],
            payload_sha256=expected['request_sha256']))
    return results


def full_batch_real_client_probe(root):
    from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(root, graph, sources, sources)
    session = Mock()

    def post(*args, **kwargs):
        request = json.loads(kwargs['json']['messages'][1]['content'])
        if request['task'] == 'checkpoint_candidate_reconstruction_independent_audit':
            return raw_response(revise(request['input']))
        response = proposed(request['input'])
        response['graph'] = {'、': ['、']}
        return raw_response(response)

    session.post.side_effect = post
    settings = ModelAPISettings('synthetic', 'https://example.invalid/v1', 'synthetic')
    with patch.object(transport._thread, 'session', session, create=True), \
            patch.object(transport.time, 'sleep', return_value=None):
        try:
            execute_batch(preflight_output=preflight, authorization=authorization,
                base_config=dict(CONFIG, model_temperature=0, max_output_tokens=16000),
                model_client_factory=lambda cache, config: transport.Client(cache, config, settings),
                output=root / 'execution', private_root=root)
        except ValueError as exc:
            error = str(exc)
        else:
            raise AssertionError('Expected final batch replay to reject mismatched hash domains')
    assert error == 'GRAPH_RECONSTRUCTION_LEDGER_REQUEST_BINDING_MISMATCH'
    return dict(reproduced=True, error=error, mocked_http_attempts=session.post.call_count,
        progress_saved=(root / 'execution/batch-progress.json').is_file(),
        execution_receipt_created=(root / 'execution/batch-execution-receipt.json').is_file())


def missing_failed_chain_probe(root):
    from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
    graph, sources = fictional_graph()
    preflight, authorization = _write_synthetic_batch_case(root, graph, sources, sources)

    class FakeClient:
        def ask(self, task, payload, schema, system_instructions):
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                return revise(payload), dict(invocation_id='SYNTHETIC-GRAPH',
                    response_sha256='a' * 64, cached=False, status='SUCCESS',
                    attempts=[dict(attempt=1)])
            error = RuntimeError('synthetic failure')
            error.receipt = dict(invocation_id='SYNTHETIC-PROVIDER', cached=False,
                status='FAILED', attempts=[dict(attempt=1, error='ConnectionError')])
            raise error

    result = execute_batch(preflight_output=preflight, authorization=authorization,
        base_config=CONFIG, model_client_factory=lambda *args: FakeClient(),
        output=root / 'execution', private_root=root)
    assert result['replay_verified']
    folder = root / 'execution/automatic-revisions/SCALE-01'
    summary = read(folder / 'automatic-revision-summary.json')
    assert summary['generations'] == []
    # Only this newly generated fictional output is changed; preserve both files.
    chain_path = folder / 'reconstruction-0001.json'
    chain_path.rename(folder / 'reconstruction-0001.json.preserved')
    receipt = root / 'execution/batch-replay-receipt.json'
    receipt.rename(root / 'execution/baseline-replay-receipt.json')
    after = replay_batch(root / 'execution')
    assert after['replay_verified']
    assert after['cases'][0]['reconstruction_chain_replays'] == []
    return dict(reproduced=True, summary_generations=summary['generations'],
        replay_verified_without_required_failed_chain=after['replay_verified'],
        replayed_reconstruction_count=0,
        reconstruction_ledger_records=sum(row.get('stage') == 'reconstruction'
            for row in read(root / 'execution/model-call-ledger.json')['records']))


def main():
    root = Path(mkdtemp(prefix='p2-review2-probes-', dir='/private/tmp'))
    previous_umask = os.umask(0o077)
    try:
        with patch.object(build, 'PRIVATE_ROOT', root), patch(
                'requests.sessions.Session.post', side_effect=AssertionError('NO_NETWORK')):
            results = dict(synthetic_only=True, actual_provider_requests=0,
                output_root=str(root), hash_domain_probes=hash_domain_probes(root))
            results['real_client_full_batch'] = full_batch_real_client_probe(root / 'real-batch')
            results['missing_failed_chain'] = missing_failed_chain_probe(root / 'missing-chain')
        with (root / 'results.json').open('x') as out:
            json.dump(results, out, ensure_ascii=False, indent=2)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    main()

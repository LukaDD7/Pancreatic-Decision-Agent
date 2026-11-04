"""Offline review probes; all cases are fictional and no provider is contacted.

These assertions reproduce defects, rather than asserting the desired repaired
behavior. Do not register them as passing production regression tests.
"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace
from unittest.mock import patch

from scripts.cohort_construction.p2_checkpoint_material_review_app import build
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import freeze_candidate_graph
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read, write
from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch
from scripts.cohort_construction.p2_checkpoint_real_validation.tests.test_real_validation import CONFIG
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import fixtures, proposed
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.reconstruction import (
    build_graph_reconstruction_request, graph_reconstruction_client,
    run_graph_reconstruction, validate_graph_reconstruction_chain,
)


def revise(request):
    response = passing_graph_response(request)
    response.update(status='AUTO_GRAPH_AUDIT_REVISE', revision_requests=[dict(
        request_id='REVIEW-PROBE-REV', request_kind='EVENT_RECONSTRUCTION',
        reason='Fictional review probe', affected_event_ids=['EVENT-01'],
        affected_episode_ids=['EPISODE-01'],
        evidence=[dict(source_id='DOC-02', exact_quote='决定：暂缓切除并补充病理证据。')],
        required_reconstruction=True)])
    return response


def synthetic_batch(root, name, *, reconstruct=False):
    base = root / name
    case_dir = base / 'preflight/preflight/SCALE-01'
    build.private_mkdir(case_dir, parents=True)
    graph, sources = fictional_graph()
    current = [s for s in sources if s['source_id'] != 'DOC-BUDGET']
    history = next(s for s in sources if s['source_id'] == 'DOC-BUDGET')
    graph['events'][0]['actual_model_evidence'].append(dict(
        source_id=history['source_id'], source_sha256=history['sha256'],
        start=0, end=len(history['text']), quote=history['text']))
    graph = freeze_candidate_graph(graph['events'], graph['relations'], graph['episodes'],
        graph_version=graph['graph_version'], discovery_units=graph['discovery_units'],
        unit_inventory=graph['unit_inventory'], final_graph_audit=graph['final_graph_audit'])
    contract = dict(batch_id='SYNTHETIC-REVIEW', manifest_sha256='a' * 64)
    write(base / 'preflight/batch-contract.json', contract)
    write(base / 'preflight/batch-preflight.json', dict(cases=[dict(
        case_id='SCALE-01', status='READY_FOR_AUTHORIZED_DEVELOPMENT_EXECUTION')]))
    write(case_dir / 'graph.json', graph)
    write(case_dir / 'sources.json', sources)
    write(case_dir / 'current-unit-sources.json', current)
    write(case_dir / 'execution-request.json', dict(episode_id='EPISODE-01',
        decision_task_id='SYNTHETIC', visibility_policy_version='SYNTHETIC',
        graph_audit_context=dict(processing_units=[dict(unit_id='ALL',
            source_ids=[s['source_id'] for s in current])])) )
    authorization = dict(contract, semantic_model='kimi-k3', max_requests=60,
        max_http_attempts=600, remote_execution_authorized=True,
        doctor_packet_release=False, real_doctor_assignment_creation=False,
        clinical_release_permitted=False, execution_subset=['SCALE-01'],
        model_switch_is_explicit=True, no_case_replacement=True,
        max_rounds=3, automatic_graph_reconstruction_authorized=True,
        max_graph_revisions=1, graph_revision_human_final_review_required=True)
    write(base / 'authorization.json', authorization)
    seen = []

    class FakeClient:
        def ask(self, task, payload, schema, system_instructions):
            seen.append(dict(task=task, payload=deepcopy(payload)))
            if task == 'checkpoint_candidate_reconstruction_independent_audit':
                audit_count = sum(s['task'] == task for s in seen)
                response = revise(payload) if reconstruct and audit_count == 1 else passing_graph_response(payload)
            elif task == 'checkpoint_candidate_graph_bounded_reconstruction':
                response = proposed(payload)
            else:
                raise AssertionError('Material stage was unexpectedly reached')
            return response, dict(invocation_id='SYNTHETIC-' + str(len(seen)),
                response_sha256='b' * 64, cached=False, status='SUCCESS',
                attempts=[dict(attempt=1)])

    result = execute_batch(preflight_output=base / 'preflight',
        authorization=base / 'authorization.json', base_config=CONFIG,
        model_client_factory=lambda *a: FakeClient(), output=base / 'execution',
        private_root=root)
    return base, result, seen


def main():
    root = Path(mkdtemp(prefix='p2-fix-review-probes-', dir='/private/tmp'))
    results = dict(synthetic_only=True, actual_provider_requests=0, output_root=str(root))
    with patch.object(build, 'PRIVATE_ROOT', root), patch(
            'requests.sessions.Session.post', side_effect=AssertionError('NO_NETWORK')):
        base, result, seen = synthetic_batch(root, 'first-pass')
        run = read(base / 'execution/episodes/SCALE-01/EPISODE-01/run.json')
        missing = [f['reason_code'] for f in run['assembly_failures']]
        assert missing == ['ASSEMBLY_SELECTED_SOURCE_MISSING']
        results['initial_pass_still_missing_recovered_source'] = dict(
            reproduced=True, terminal=result['cases'][0]['state'], missing=missing,
            full_source_count=4, supplied_to_pipeline_count=3)

        base, result, seen = synthetic_batch(root, 'reconstruction-success', reconstruct=True)
        audits = [s['payload'] for s in seen if s['task'] == 'checkpoint_candidate_reconstruction_independent_audit']
        assert len(audits) == 2
        assert audits[0]['audit_scope']['source_coverage_complete'] is True
        assert audits[1]['audit_scope']['source_coverage_complete'] is False
        assert audits[1]['audit_scope']['uncovered_supplied_source_ids'] == ['DOC-BUDGET']
        results['fresh_audit_scope_expanded_and_pass_blocked'] = dict(
            reproduced=True, first_source_count=3, new_source_count=4,
            uncovered=['DOC-BUDGET'], terminal=result['cases'][0]['state'])

        # Exercise the production Client, with only the HTTP method replaced.
        from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client
        _, audit_request, audit_response = fixtures()
        request = build_graph_reconstruction_request(audit_request, audit_response)
        response = proposed(request)
        response['graph'] = {'、': ['、']}
        raw = dict(choices=[dict(finish_reason='stop', message=dict(
            content=json.dumps(response, ensure_ascii=False)))], model='SYNTHETIC', usage={})
        http_response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: raw)
        settings = SimpleNamespace(model='SYNTHETIC', thinking=None,
            chat_completions_url='https://invalid.test/no-network', api_key='synthetic')
        client = Client(root / 'transport-cache', dict(model_temperature=0,
            max_output_tokens=1000, max_http_attempts_per_request=1), settings=settings)
        with patch('requests.sessions.Session.post', return_value=http_response):
            chain = run_graph_reconstruction(audit_request, audit_response,
                graph_reconstruction_client(client))
        assert chain['failure']['stage'] == 'PROVIDER_REQUEST'
        assert 'response' not in chain and 'provider_receipt' not in chain
        assert 'provider_invocation_id' not in chain['failure']
        assert client.receipts[-1]['invocation_id']
        results['transport_schema_rejection_still_discards_response_and_invocation'] = dict(
            reproduced=True, saved_failure=chain['failure'],
            invocation_available_in_client=True,
            cache_has_parsed_response=bool(list((root / 'transport-cache').glob('*.json'))))

        changed = deepcopy(chain)
        changed['request']['input_graph']['graph_version'] = 'TAMPERED-UNBOUND-VERSION'
        changed['chain_sha256'] = canonical_sha256({k: v for k, v in changed.items() if k != 'chain_sha256'})
        replay = validate_graph_reconstruction_chain(changed)
        assert replay['replay_verified']
        results['provider_failure_replay_skips_request_validation'] = dict(
            reproduced=True, replay_verified_after_request_tampering=True)

        # A provider-failure chain from a different graph can be inserted into
        # the first batch's failed-revision folder and is accepted by batch replay.
        from scripts.cohort_construction.p2_checkpoint_real_validation.batch import replay_batch
        transplant = root / 'first-pass/execution/automatic-revisions/SCALE-01/reconstruction-0001.json'
        write(transplant, chain)
        batch_output = root / 'first-pass/execution'
        old_receipt = batch_output / 'batch-replay-receipt.json'
        # Preserve the first review result; this directory is new synthetic output.
        old_receipt.rename(batch_output / 'original-review-replay-receipt.json')
        replayed = replay_batch(batch_output)
        assert replayed['replay_verified']
        results['failed_chain_not_bound_to_parent_batch_graph'] = dict(
            reproduced=True, unrelated_chain_accepted=True)

    with (root / 'probe-results.json').open('x') as out:
        json.dump(results, out, ensure_ascii=False, indent=2)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Independent acceptance of the last missing-revision-directory fix.

Production Client and execute_batch are used with fictional HTTP responses.
Only newly generated temporary artifacts are moved; nothing is deleted.
"""
import json
import os
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import Mock, patch

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint_v04 import client as transport
from scripts.cohort_construction.p2_checkpoint_material_review_app import build
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph
from scripts.cohort_construction.p2_checkpoint_real_validation.batch import execute_batch, replay_batch
from scripts.cohort_construction.p2_checkpoint_real_validation.tests.test_real_validation import CONFIG, _write_synthetic_batch_case
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import proposed
from receipts.p2_fix_review2_20261102_probes import raw_response, revise


def expect_rejection(output, expected):
    try:
        replay_batch(output)
    except ValueError as exc:
        assert str(exc) == expected, str(exc)
        return str(exc)
    raise AssertionError('Required revision evidence was missing but replay accepted it')


def main():
    root = Path(mkdtemp(prefix='p2-review4-probes-', dir='/private/tmp'))
    old_umask = os.umask(0o077)
    try:
        with patch.object(build, 'PRIVATE_ROOT', root), patch(
                'requests.sessions.Session.post', side_effect=AssertionError('NO_NETWORK')):
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
                result = execute_batch(preflight_output=preflight, authorization=authorization,
                    base_config=dict(CONFIG, model_temperature=0, max_output_tokens=16000),
                    model_client_factory=lambda cache, config: transport.Client(cache, config, settings),
                    output=root / 'execution', private_root=root)
            assert result['replay_verified']
            output = root / 'execution'
            folder = output / 'automatic-revisions/SCALE-01'
            chain = folder / 'reconstruction-0001.json'
            saved_chain = folder / 'reconstruction-0001.json.preserved'
            chain.rename(saved_chain)
            missing_chain = expect_rejection(output,
                'REAL_BATCH_RECONSTRUCTION_LEDGER_CHAIN_INVENTORY_MISMATCH')
            saved_chain.rename(chain)
            assert replay_batch(output)['replay_verified']

            summary = folder / 'automatic-revision-summary.json'
            saved_summary = folder / 'automatic-revision-summary.json.preserved'
            summary.rename(saved_summary)
            missing_summary = expect_rejection(output,
                'REAL_BATCH_RECONSTRUCTION_LEDGER_REVISION_DELIVERY_MISSING')
            saved_summary.rename(summary)
            assert replay_batch(output)['replay_verified']

            saved_folder = root / 'preserved-complete-revision-directory'
            folder.rename(saved_folder)
            missing_directory = expect_rejection(output,
                'REAL_BATCH_RECONSTRUCTION_LEDGER_REVISION_DELIVERY_MISSING')
            saved_folder.rename(folder)
            restored = replay_batch(output)
            assert restored['replay_verified']
            results = dict(actual_provider_requests=0, synthetic_only=True,
                output_root=str(root), production_client_full_batch_replay_verified=True,
                mocked_http_attempts=session.post.call_count,
                missing_single_chain_rejected=missing_chain,
                missing_summary_rejected=missing_summary,
                missing_entire_revision_directory_rejected=missing_directory,
                restored_complete_delivery_replay_verified=restored['replay_verified'],
                conclusion='PREVIOUSLY_REPORTED_MISSING_DIRECTORY_DEFECT_FIXED')
            with (root / 'results.json').open('x') as out:
                json.dump(results, out, ensure_ascii=False, indent=2)
            print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        os.umask(old_umask)


if __name__ == '__main__':
    main()

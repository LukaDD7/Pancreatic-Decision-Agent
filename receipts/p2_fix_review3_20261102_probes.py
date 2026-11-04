"""Third independent review: production Client with mock HTTP, fictional data.

The whole-directory assertion reproduces a remaining defect; this script is
review evidence, not a passing production acceptance test.
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
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import read
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_graph_reconstruction import proposed
from receipts.p2_fix_review2_20261102_probes import raw_response, revise


def main():
    root = Path(mkdtemp(prefix='p2-review3-probes-', dir='/private/tmp'))
    previous_umask = os.umask(0o077)
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
            assert read(folder / 'automatic-revision-summary.json')['generations'] == []
            chain = folder / 'reconstruction-0001.json'
            preserved_chain = folder / 'reconstruction-0001.json.preserved'
            chain.rename(preserved_chain)
            try:
                replay_batch(output)
            except ValueError as exc:
                missing_file_error = str(exc)
            else:
                raise AssertionError('A missing chain file must be rejected')
            assert missing_file_error == 'REAL_BATCH_RECONSTRUCTION_LEDGER_CHAIN_INVENTORY_MISMATCH'
            preserved_chain.rename(chain)
            assert replay_batch(output)['replay_verified']

            folder.rename(root / 'preserved-complete-revision-directory')
            (output / 'batch-replay-receipt.json').rename(root / 'baseline-replay-receipt.json')
            missing_directory_replay = replay_batch(output)
            assert missing_directory_replay['replay_verified']
            ledger = read(output / 'model-call-ledger.json')
            results = dict(actual_provider_requests=0, synthetic_only=True, output_root=str(root),
                production_client_full_batch_replay_verified=True,
                mocked_http_attempts=session.post.call_count,
                execution_receipt_created=(output / 'batch-execution-receipt.json').is_file(),
                generations=[], missing_single_file_rejected=missing_file_error,
                restored_chain_replay_verified=True,
                missing_entire_revision_directory_replay_verified=missing_directory_replay['replay_verified'],
                reconstruction_ledger_records=sum(r.get('stage') == 'reconstruction' for r in ledger['records']),
                returned_reconstruction_chain_replays=missing_directory_replay['cases'][0].get('reconstruction_chain_replays'))
            with (root / 'results.json').open('x') as out:
                json.dump(results, out, ensure_ascii=False, indent=2)
            print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    main()

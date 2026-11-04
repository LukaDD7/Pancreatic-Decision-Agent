"""Offline checks for a model-only rerun of the frozen primary contract."""
import ast
import inspect
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.cohort_construction.model_api import ModelAPISettings
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest, write
from scripts.cohort_construction.p2_auto_checkpoint_model_rerun import run as current
from scripts.cohort_construction.p2_auto_checkpoint_shape_retry import run as frozen
from scripts.cohort_construction.p2_auto_checkpoint_v04.client import Client


CFG = {'model_temperature': 0, 'max_output_tokens': 100, 'semantic_model': 'deepseek-v4.1-flash'}
SETTINGS = ModelAPISettings('synthetic-secret', 'https://apicz.boyuerichdata.com/v1',
                            'deepseek-v4.1-flash', 'disabled')
PAYLOAD = {'instruction': 'frozen synthetic task',
           'current_full_document': [{'text': 'synthetic only'}], 'evidence_source_pool': []}
SCHEMA = {'type': 'object'}


def _ast(function):
    return ast.dump(ast.parse(inspect.getsource(function)), include_attributes=False)


def test_new_config_changes_only_the_two_model_identifiers():
    root = current.run.ROOT
    original = json.loads((root / 'config/p2_auto_checkpoint_v05.json').read_text())
    actual = json.loads((root / 'config/p2_auto_checkpoint_v05_deepseek.json').read_text())
    assert {key for key in original if original[key] != actual[key]} == {
        'semantic_model', 'model_alias_from_user'}
    assert actual['semantic_model'] == actual['model_alias_from_user'] == 'deepseek-v4.1-flash'
    assert set(actual) == set(original)


def test_frozen_request_and_recovery_policy_is_unchanged():
    assert current.REMINDER == frozen.REMINDER
    assert _ast(current.body_for) == _ast(frozen.body_for)
    # Method source is indented; normalize only its external indentation.
    assert inspect.getsource(current.ShapeRetryClient.ask) == inspect.getsource(frozen.ShapeRetryClient.ask)
    old = inspect.getsource(frozen.ShapeRetryClient.__init__)
    new = inspect.getsource(current.ShapeRetryClient.__init__)
    assert new == old.replace("'qwen3.8-max'", "'deepseek-v4.1-flash'")


@pytest.mark.parametrize('arguments', [[], ['--case-workers', '2']])
def test_entrypoint_injects_only_the_new_default_config(arguments):
    arguments = ['model-rerun', *arguments]
    with patch.object(current.sys, 'argv', arguments.copy()), patch.object(current.run, 'main') as execute, \
            patch.object(current.engine, 'Client', Client):
        current.main()
        execute.assert_called_once_with()
        assert current.sys.argv == arguments + [
            '--config', str(current.run.ROOT / 'config/p2_auto_checkpoint_v05_deepseek.json')]
        assert current.engine.Client is current.ShapeRetryClient


@pytest.mark.parametrize('arguments', [['--config', 'explicit.json'], ['--config=explicit.json']])
def test_entrypoint_respects_explicit_config(arguments):
    with patch.object(current.sys, 'argv', ['model-rerun', *arguments]), \
            patch.object(current.run, 'main') as execute, patch.object(current.engine, 'Client', Client):
        current.main()
        execute.assert_called_once_with()
        assert current.sys.argv == ['model-rerun', *arguments]


@pytest.mark.parametrize('model,host', [
    ('qwen3.8-max', 'https://apicz.boyuerichdata.com/v1'),
    ('deepseek-v4.1-flash', 'https://other.invalid/v1'),
])
def test_model_and_host_guard_are_enforced(model, host):
    settings = ModelAPISettings('synthetic-secret', host, model, 'disabled')
    with tempfile.TemporaryDirectory() as cache, pytest.raises(
            ValueError, match='SHAPE_RECOVERY_OUTSIDE_FROZEN_MODEL_OR_HOST'):
        current.ShapeRetryClient(cache, {**CFG, 'semantic_model': model}, settings)


def _client(cache, *, failed=False):
    key = digest([SETTINGS.chat_completions_url,
                  current.body_for(SETTINGS, CFG, 'task', PAYLOAD, SCHEMA)])
    if failed:
        write(Path(cache) / 'invocations/synthetic.json', {
            'status': 'FAILED', 'request_sha256': key, 'attempts': [{'error': 'ValidationError'}]})
    return current.ShapeRetryClient(cache, CFG, SETTINGS), key


def test_actual_format_feedback_preserves_every_clinical_payload_field():
    with tempfile.TemporaryDirectory() as cache, patch.object(
            Client, 'ask', return_value=({'ok': True}, {'request_sha256': 'actual-new-hash'})) as request:
        client, original_hash = _client(cache, failed=True)
        client.ask('task', PAYLOAD, SCHEMA)
        sent = request.call_args.args[1]
        assert request.call_count == 1
        assert {key: sent[key] for key in PAYLOAD if key != 'instruction'} == {
            key: value for key, value in PAYLOAD.items() if key != 'instruction'}
        assert sent['instruction'] == PAYLOAD['instruction'] + '\n' + frozen.REMINDER
        assert sent['format_recovery_contract'] == {
            'original_request_sha256': original_hash, 'maximum_format_feedback_rounds': 1,
            'source_context_added': False, 'semantic_positive_required': False}
        assert not (Path(cache) / (original_hash + '.json')).exists()


def test_network_failure_does_not_trigger_format_feedback():
    with tempfile.TemporaryDirectory() as cache:
        client, original_hash = _client(cache)

        def fail(*args):
            client.receipts.append({'request_sha256': original_hash, 'attempts': [{'error': 'SSLError'}]})
            raise RuntimeError('synthetic network failure')

        with patch.object(Client, 'ask', side_effect=fail) as request, pytest.raises(RuntimeError):
            client.ask('task', PAYLOAD, SCHEMA)
        assert request.call_count == 1
        assert not client.recovery_records


def test_first_discovery_request_differs_only_in_model():
    old_settings = ModelAPISettings('synthetic-secret', SETTINGS.api_url, 'qwen3.8-max', 'disabled')
    old = frozen.body_for(old_settings, {**CFG, 'semantic_model': 'qwen3.8-max'},
                          'document_event_discovery', PAYLOAD, SCHEMA)
    new = current.body_for(SETTINGS, CFG, 'document_event_discovery', PAYLOAD, SCHEMA)
    assert new['model'] == 'deepseek-v4.1-flash'
    assert old['model'] == 'qwen3.8-max'
    assert {key: value for key, value in old.items() if key != 'model'} == {
        key: value for key, value in new.items() if key != 'model'}

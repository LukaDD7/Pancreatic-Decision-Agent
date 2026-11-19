"""Actual provider calls, separately cached for the frozen v0.4 contract."""
import hashlib
import json
import os
import time
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock, local
from uuid import uuid4

import requests
from jsonschema import Draft202012Validator, ValidationError

from scripts.cohort_construction.model_api import load_boyu_settings
from scripts.cohort_construction.p2_auto_checkpoint.extract import digest, write
from .prompts import SYSTEM_INSTRUCTIONS
from .provider_diagnostics import (FORMAT as EVIDENCE_FORMAT, json_diagnostic,
    private_chain_evidence, provider_reason, receipt_diagnostic, schema_diagnostic)

_slot_limits = {}
_pacing_locks = {}
_pacing_last_started = {}
_pacing_registry_lock = Lock()
_thread = local()
# Optional process-wide ceilings for complete discovery/review batches. Every
# Client for the same endpoint/model shares the reservation state: per-case
# workers cannot each spend a nominally global allowance.
_batch_budget_lock = Lock()
_batch_budgets = {}

# HTTP statuses that describe a transient condition and may be retried. Any
# other 4xx is the provider saying this request cannot succeed as sent, so
# repeating the identical bytes only burns the batch's attempt allowance.
_RETRYABLE_STATUS = frozenset((408, 409, 425, 429))
_OVERFLOW_MARKERS = ('context_length_exceeded', 'maximum context length',
    'context window', 'too many tokens', 'input is too long', 'prompt is too long',
    'exceeds the maximum', 'reduce the length')


def _retry_decision(exc, evidence):
    """Classify one failed attempt without reading credentials or headers.

    Everything except a terminal 4xx keeps its previous behaviour: retry. Only
    the provider's own rejection of this request is treated as final, because
    resending identical bytes cannot change the answer.
    """
    verdict = dict(retryable=True, input_overflow_suspected=False)
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if type(status) is int and 400 <= status < 500 \
                and status not in _RETRYABLE_STATUS:
            text = evidence.get('provider_response_text')
            hint = text.lower() if isinstance(text, str) else ''
            verdict['retryable'] = False
            verdict['input_overflow_suspected'] = any(
                marker in hint for marker in _OVERFLOW_MARKERS)
    return verdict


def _safe_provider_metadata(raw):
    """Keep only bounded operational metadata, never arbitrary provider strings."""
    allowed = ('input_tokens', 'output_tokens', 'total_tokens', 'cache_read_input_tokens',
        'cache_creation_input_tokens', 'prompt_tokens', 'completion_tokens')
    usage = {}
    for key in allowed:
        value = raw.get('usage', {}).get(key) if isinstance(raw.get('usage'), dict) else None
        if type(value) is int and 0 <= value <= 10**12:
            usage[key] = value
    model = raw.get('model')
    if model is not None and (not isinstance(model, str) or not model
            or len(model) > 128 or any(ord(char) < 32 for char in model)
            or any(char.isspace() for char in model)):
        raise ValueError('PROVIDER_RETURNED_MODEL_UNSAFE')
    return usage, model


class ModelRequestFailed(RuntimeError):
    """A failed invocation bound to its own receipt, including under concurrency.

    The receipt contains request identity, timings and sanitized failure
    categories. Parsed response evidence is a separate private exception field;
    it is never serialized into the invocation receipt or its string message.
    RuntimeError inheritance preserves existing callers' exception handling.
    """
    def __init__(self, receipt, *, response_evidence=None, failure_evidence=None):
        self.request_sha256 = receipt['request_sha256']
        self.receipt = deepcopy(receipt)
        self.response_evidence = deepcopy(response_evidence)
        self.failure_evidence = deepcopy(failure_evidence)
        super().__init__('MODEL_REQUEST_FAILED:' + self.request_sha256)


class Client:
    def __init__(self, cache, config, settings=None):
        self.cache = Path(cache); self.cache.mkdir(parents=True, exist_ok=True)
        self.config = config; self.settings = settings or load_boyu_settings()
        # Versioned experiment choice, without mutating the shared provider
        # profile or silently falling back to an earlier model.
        if config.get('semantic_model'):
            self.settings = replace(self.settings, model=config['semantic_model'])
        self.receipts = []
        self.max_http_attempts = self.config.get('max_http_attempts_per_request', 3)
        if type(self.max_http_attempts) is not int or not 1 <= self.max_http_attempts <= 100:
            raise ValueError('MODEL_HTTP_ATTEMPTS_INVALID')
        self._batch_budget = None
        limits = ('max_total_model_requests', 'max_total_http_attempts')
        if any(key in config for key in limits):
            if not all(key in config for key in limits):
                raise ValueError('MODEL_TOTAL_BATCH_BUDGET_INCOMPLETE')
            for key in limits:
                if type(config[key]) is not int or config[key] < 1:
                    raise ValueError('MODEL_TOTAL_BATCH_BUDGET_INVALID')
            batch_id = config.get('execution_batch_id')
            if not isinstance(batch_id, str) or not batch_id or len(batch_id) > 200:
                raise ValueError('MODEL_TOTAL_BATCH_ID_REQUIRED')
            key = (self.settings.chat_completions_url, self.settings.model, batch_id)
            with _batch_budget_lock:
                budget = _batch_budgets.setdefault(key, dict(requests=0,
                    http_attempts=0, invocations=[], lock=Lock()))
                for field, limit in (('requests', config[limits[0]]),
                        ('http_attempts', config[limits[1]])):
                    if budget.setdefault(field + '_limit', limit) != limit:
                        raise ValueError('MODEL_TOTAL_BATCH_BUDGET_CONFLICT')
            self._batch_budget = budget
        self.retry_backoff_base_seconds = self.config.get('retry_backoff_base_seconds', 2.0)
        self.retry_backoff_max_seconds = self.config.get('retry_backoff_max_seconds', 60.0)
        self.min_request_interval_seconds = self.config.get('min_request_interval_seconds', 0.0)
        for value in (self.retry_backoff_base_seconds, self.retry_backoff_max_seconds,
                      self.min_request_interval_seconds):
            if type(value) not in (int, float) or not 0 <= value <= 3600:
                raise ValueError('MODEL_RATE_CONFIG_INVALID')
        # Read timeout. The historical 180 s default is kept for every config
        # that does not set this, so old batches keep the timing they ran with.
        # A larger untrimmed payload can legitimately take longer to answer;
        # leaving the old value would turn that into attempt timeouts and burn
        # the batch's HTTP allowance on a request that was never too big.
        self.read_timeout_seconds = self.config.get('provider_read_timeout_seconds', 180)
        if type(self.read_timeout_seconds) not in (int, float) \
                or not 1 <= self.read_timeout_seconds <= 3600:
            raise ValueError('MODEL_READ_TIMEOUT_INVALID')
        if (self.retry_backoff_base_seconds == 0
                or self.retry_backoff_max_seconds < self.retry_backoff_base_seconds):
            raise ValueError('MODEL_RATE_CONFIG_INVALID')
        self._receipt_lock = Lock()
        limit=max(1,int(config.get('max_concurrent_model_calls',2)))
        self.slots=_slot_limits.setdefault(limit,BoundedSemaphore(limit))
        # A configured start interval applies to every Client on the same
        # provider endpoint in this process, not just retries within one call.
        with _pacing_registry_lock:
            pacing=_pacing_locks.setdefault(self.settings.chat_completions_url,
                [Lock(),None])
        self._pacing_lock=pacing[0]
        self._pacing_state=pacing

    def _wait_for_request_start(self):
        """Serialize only the shared interval check; network I/O stays outside."""
        if not self.min_request_interval_seconds:
            return 0.0
        with self._pacing_lock:
            last_started=self._pacing_state[1]
            now=time.monotonic()
            delay=max(0.0,self.min_request_interval_seconds-(now-last_started)) \
                if last_started is not None else 0.0
            if delay:
                time.sleep(delay)
            self._pacing_state[1]=time.monotonic()
            return delay

    def _reserve_batch_invocation(self, invocation_id, *, cached=False):
        budget = self._batch_budget
        if budget is None:
            return
        with budget['lock']:
            if cached:
                for row in budget['invocations']:
                    if row['invocation_id'] == invocation_id:
                        row['cached_replays'] += 1
                        return
                raise ValueError('MODEL_CACHE_ORIGINAL_NOT_IN_AUTHORIZED_BATCH')
            if budget['requests'] >= budget['requests_limit']:
                raise ValueError('MODEL_TOTAL_BATCH_REQUEST_BUDGET_EXHAUSTED')
            if budget['http_attempts'] + self.max_http_attempts > budget['http_attempts_limit']:
                raise ValueError('MODEL_TOTAL_BATCH_HTTP_BUDGET_EXHAUSTED')
            budget['requests'] += 1
            budget['http_attempts'] += self.max_http_attempts
            budget['invocations'].append(dict(invocation_id=invocation_id,
                attempts_used=0, cached_replays=0))

    def _settle_batch_invocation(self, receipt):
        if self._batch_budget is None:
            return
        with self._batch_budget['lock']:
            matches = [row for row in self._batch_budget['invocations']
                if row['invocation_id'] == receipt.get('invocation_id')]
            if len(matches) != 1:
                raise ValueError('MODEL_TOTAL_BATCH_SETTLEMENT_IDENTITY_MISMATCH')
            actual = len(receipt.get('attempts', []))
            if not 1 <= actual <= self.max_http_attempts:
                raise ValueError('MODEL_TOTAL_BATCH_SETTLEMENT_ATTEMPTS_INVALID')
            self._batch_budget['http_attempts'] -= self.max_http_attempts - actual
            matches[0]['attempts_used'] = actual
            matches[0]['status'] = receipt.get('status')

    def _record_receipt(self, receipt):
        # Serialize in-memory bookkeeping only; do not add per-call disk scans
        # or writes. Invocation archives remain the existing audit mechanism.
        with self._receipt_lock:
            self.receipts.append(deepcopy(receipt))

    def failure_receipt_for(self, exc, request_sha256):
        """Resolve this request's failure, never another request's last receipt.

        Production transport exceptions carry an invocation-specific snapshot.
        The hash lookup is retained for older clients and offline mock callers
        that raise a plain RuntimeError after appending a sanitized receipt.
        """
        if isinstance(exc, ModelRequestFailed):
            return deepcopy(exc.receipt) if exc.request_sha256 == request_sha256 else {}
        with self._receipt_lock:
            for receipt in reversed(self.receipts):
                if receipt.get('request_sha256') == request_sha256:
                    # Do not skip a newer success and pick a stale failure for
                    # the same hash. Missing status supports legacy test mocks.
                    return deepcopy(receipt) if receipt.get('status') in (None, 'FAILED') else {}
        return {}

    def _write_private_evidence(self, path, evidence):
        """Create one private JSON file without replacing a prior attempt."""
        old_umask = os.umask(0o077)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as stream:
                json.dump(evidence, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
        finally:
            os.umask(old_umask)

    def _save_attempt_evidence(self, archive, evidence):
        """Exclusive append-only private evidence; receipts contain its hash only."""
        path = archive.parent / archive.stem / 'attempts' / ('%04d.json' % evidence['attempt'])
        relative = str(path.relative_to(self.cache))
        evidence_sha256 = private_chain_evidence(evidence)['evidence_content_sha256']
        binding = dict(format=EVIDENCE_FORMAT, relative_path=relative,
            sha256=evidence_sha256, attempt=evidence['attempt'])
        if path.exists():
            saved = json.loads(path.read_text(encoding='utf-8'))
            if private_chain_evidence(saved)['evidence_content_sha256'] != evidence_sha256:
                raise ValueError('PROVIDER_ATTEMPT_EVIDENCE_COLLISION')
            return binding
        self._write_private_evidence(path, evidence)
        return binding

    def ask(self, task, payload, schema, *, system_instructions=SYSTEM_INSTRUCTIONS):
        body = dict(model=self.settings.model, temperature=self.config['model_temperature'],
            max_tokens=self.config['max_output_tokens'], response_format={'type':'json_object'},
            messages=[dict(role='system',content=system_instructions),dict(role='user',
                content=json.dumps(dict(task=task,schema=schema,input=payload),ensure_ascii=False))])
        if self.settings.thinking: body['thinking']={'type':self.settings.thinking}
        # Keep the historical invocation/cache key separate from the exact
        # prepared HTTP bytes. The latter are computed once per request and
        # hashed on every attempt; ``json=body`` alone cannot prove wire bytes.
        key=digest([self.settings.chat_completions_url,body]); path=self.cache/(key+'.json')
        request = requests.Request('POST', self.settings.chat_completions_url,
            headers={'Authorization':'Bearer '+self.settings.api_key}, json=body)
        prepared = request.prepare()
        # requests.Session.prepare_request requires a Request, not an already
        # PreparedRequest (the latter lacks ``cookies`` in requests 2.32.x).
        # First obtain the Request-level body bytes. If Session.send is the
        # actual transport, its prepared body must equal these bytes; mocked
        # post-only transports explicitly leave wire-byte identity unknown.
        wire_body = prepared.body
        if isinstance(wire_body, str):
            wire_body = wire_body.encode('utf-8')
        if not isinstance(wire_body, bytes):
            raise TypeError('PROVIDER_PREPARED_BODY_BYTES_REQUIRED')
        http_body_sha256 = hashlib.sha256(wire_body).hexdigest()
        declared_payload_sha256 = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        if path.exists():
            from .provider_diagnostics import (replay_transport_evidence,
                validate_provider_success_binding, validate_private_chain_evidence)
            saved=json.loads(path.read_text())
            # Bind every cached identity before reading or returning its result.
            # These errors are deliberately free of request/provider content.
            if not isinstance(saved, dict) \
                    or digest([self.settings.chat_completions_url,saved.get('request')]) != key:
                raise ValueError('PROVIDER_CACHE_REQUEST_BODY_HASH_MISMATCH')
            original_receipt = saved.get('receipt')
            if not isinstance(original_receipt, dict) \
                    or original_receipt.get('request_sha256') != key:
                raise ValueError('PROVIDER_CACHE_RECEIPT_REQUEST_HASH_MISMATCH')
            if original_receipt.get('status') != 'SUCCESS':
                raise ValueError('PROVIDER_CACHE_SUCCESS_STATUS_REQUIRED')
            if original_receipt.get('task') != task:
                raise ValueError('PROVIDER_CACHE_TASK_MISMATCH')
            Draft202012Validator(schema).validate(saved['result'])
            assert digest(saved['result'])==saved['result_sha256']
            if not isinstance(original_receipt, dict) \
                    or original_receipt.get('receipt_origin') != 'PROVIDER_CLIENT' \
                    or original_receipt.get('cached') is not False \
                    or original_receipt.get('provider_invocation_occurred') is not True \
                    or original_receipt.get('provider_transport_verified') is not True:
                raise ValueError('PROVIDER_CACHE_PROVENANCE_INVALID')
            receipt=dict(original_receipt,cached=True,provider_invocation_occurred=False,
                provider_transport_verified=False,receipt_origin='PROVIDER_CLIENT_CACHE')
            evidence = original_receipt.get('terminal_attempt_evidence')
            if evidence is None:
                reference = original_receipt.get('attempts', [{}])[-1].get('evidence', {})
                if not isinstance(reference, dict) or not reference.get('attempt') \
                        or not isinstance(reference.get('relative_path'), str) \
                        or reference.get('format') != EVIDENCE_FORMAT:
                    raise ValueError('PROVIDER_CACHE_ATTEMPT_EVIDENCE_REQUIRED')
                archived_path = (self.cache / reference['relative_path']).resolve()
                if self.cache.resolve() not in archived_path.parents or not archived_path.is_file():
                    raise ValueError('PROVIDER_CACHE_ATTEMPT_ARCHIVE_MISSING')
                archived = json.loads(archived_path.read_text(encoding='utf-8'))
                embedded = private_chain_evidence(archived)
                if embedded['evidence_content_sha256'] != reference.get('sha256') \
                        or archived.get('terminal_stage') != 'SUCCESS':
                    raise ValueError('PROVIDER_CACHE_ATTEMPT_ARCHIVE_MISMATCH')
                evidence = embedded
            validate_private_chain_evidence(evidence, evidence)
            if saved['result'] != evidence.get('response'):
                raise ValueError('PROVIDER_CACHE_RESULT_RESPONSE_MISMATCH')
            replay_transport_evidence(evidence, schema)
            attempts = original_receipt.get('attempts')
            if not isinstance(attempts, list) or not attempts:
                raise ValueError('PROVIDER_CACHE_ATTEMPTS_REQUIRED')
            receipt['attempts'] = deepcopy(attempts)
            for attempt in receipt['attempts'][:-1]:
                reference = attempt.get('evidence')
                if not isinstance(reference, dict) or not isinstance(reference.get('relative_path'), str):
                    raise ValueError('PROVIDER_CACHE_RETRY_POINTER_REQUIRED')
                archived_path = (self.cache / reference['relative_path']).resolve()
                if self.cache.resolve() not in archived_path.parents or not archived_path.is_file():
                    raise ValueError('PROVIDER_CACHE_RETRY_ARCHIVE_MISSING')
                archived = json.loads(archived_path.read_text(encoding='utf-8'))
                attempt['archived_evidence'] = private_chain_evidence(archived)
            receipt['terminal_attempt_evidence'] = deepcopy(evidence)
            outcome = dict(review=saved['result'], stage='SUCCESS', cached=True, receipt=receipt)
            if original_receipt.get('declared_provider_payload_sha256') != declared_payload_sha256:
                raise ValueError('PROVIDER_CACHE_DECLARED_PAYLOAD_MISMATCH')
            validate_provider_success_binding(evidence, outcome, payload,
                payload, cached=True, schema=schema)
            self._reserve_batch_invocation(original_receipt['invocation_id'], cached=True)
            self._record_receipt(receipt)
            return saved['result'],receipt
        receipt=dict(task=task,model=self.settings.model,request_sha256=key,cached=False,
            invocation_id=uuid4().hex,started_at=datetime.now(timezone.utc).isoformat(),
            status='IN_FLIGHT',attempts=[],max_http_attempts=self.max_http_attempts,
            retry_backoff_base_seconds=self.retry_backoff_base_seconds,
            retry_backoff_max_seconds=self.retry_backoff_max_seconds,
            min_request_interval_seconds=self.min_request_interval_seconds,
            pacing_seconds_total=0.0,receipt_origin='PROVIDER_CLIENT',
            provider_invocation_occurred=True,provider_transport_verified=False,
            declared_provider_payload_sha256=declared_payload_sha256,
            actual_provider_http_body_sha256=http_body_sha256)
        self._reserve_batch_invocation(receipt['invocation_id'])
        archive=self.cache/'invocations'/(receipt['invocation_id']+'.json');write(archive,receipt)
        started=time.monotonic()
        response_evidence=None
        failure_evidence=None
        for attempt_index in range(self.max_http_attempts):
            response_evidence=None
            stage='PROVIDER_REQUEST'
            evidence=dict(format=EVIDENCE_FORMAT, invocation_id=receipt['invocation_id'],
                attempt=attempt_index+1, transport_request_sha256=key,
                declared_provider_payload_sha256=declared_payload_sha256,
                actual_provider_http_body_sha256=http_body_sha256,
                request=dict(task=task, system_instructions=system_instructions,
                    schema=deepcopy(schema), input=deepcopy(payload),
                    model_temperature=self.config['model_temperature'],
                    max_output_tokens=self.config['max_output_tokens']),
                terminal_stage=stage, received_at=datetime.now(timezone.utc).isoformat())
            try:
                with self.slots:
                    delay=self._wait_for_request_start()
                    if delay:
                        receipt['pacing_seconds_total']=round(
                            receipt['pacing_seconds_total']+delay,3)
                    if not hasattr(_thread,'session'): _thread.session=requests.Session()
                    if hasattr(type(_thread.session), 'prepare_request') \
                            and hasattr(type(_thread.session), 'send'):
                        prepared_request = _thread.session.prepare_request(request)
                        actual_body = prepared_request.body
                        if isinstance(actual_body, str):
                            actual_body = actual_body.encode('utf-8')
                        if not isinstance(actual_body, bytes) \
                                or hashlib.sha256(actual_body).hexdigest() != http_body_sha256:
                            raise ValueError('PROVIDER_PREPARED_BODY_HASH_MISMATCH')
                        response=_thread.session.send(prepared_request,
                            timeout=(20,self.read_timeout_seconds),
                            allow_redirects=False)
                    else:
                        # Existing offline test doubles intercept ``post`` rather
                        # than the prepared-request API. They cannot certify actual
                        # provider HTTP bytes; leave the wire-byte domain unknown
                        # instead of copying the independently prepared hash into
                        # evidence for a different transport path.
                        http_body_sha256 = None
                        evidence['actual_provider_http_body_sha256'] = None
                        receipt['actual_provider_http_body_sha256'] = None
                        response=_thread.session.post(self.settings.chat_completions_url,
                            json=body,timeout=(20,self.read_timeout_seconds),
                            allow_redirects=False)
                # Persist immediately after receipt, before any JSON or status parse.
                # requests always exposes text; legacy unit-test doubles may not.
                provider_text=response.text
                if not isinstance(provider_text,str):
                    try:
                        provider_text=json.dumps(response.json(),ensure_ascii=False)
                    except (ValueError,TypeError,KeyError):
                        provider_text=''
                evidence['provider_response_text']=provider_text
                if type(response.status_code) is int:
                    evidence['http_status']=response.status_code
                evidence['provider_response_sha256']=hashlib.sha256(
                    evidence['provider_response_text'].encode('utf-8')).hexdigest()
                raw_path = archive.parent / archive.stem / 'attempts' / (
                    '%04d.provider-response.json' % (attempt_index + 1))
                self._write_private_evidence(raw_path, dict(
                    format='provider-raw-response-0.1.0',
                    invocation_id=receipt['invocation_id'], attempt=attempt_index + 1,
                    transport_request_sha256=key,
                    declared_provider_payload_sha256=declared_payload_sha256,
                    actual_provider_http_body_sha256=evidence.get(
                        'actual_provider_http_body_sha256', http_body_sha256),
                    http_status=evidence.get('http_status'),
                    provider_response_sha256=evidence['provider_response_sha256'],
                    provider_response_text=provider_text))
                evidence['raw_response_snapshot'] = dict(
                    relative_path=str(raw_path.relative_to(self.cache)),
                    sha256=evidence['provider_response_sha256'])
                stage='PROVIDER_HTTP_RESPONSE';evidence['terminal_stage']=stage
                response.raise_for_status()
                stage='PROVIDER_ENVELOPE_PARSE';evidence['terminal_stage']=stage
                raw=response.json()
                evidence['provider_envelope']=deepcopy(raw)
                evidence['provider_envelope_sha256']=digest(raw)
                # Keep JSON parsing separate from the provider envelope's shape.
                if not isinstance(raw,dict) or not isinstance(raw.get('choices'),list) \
                        or not raw['choices'] or not isinstance(raw['choices'][0],dict):
                    raise ValueError('PROVIDER_ENVELOPE_STRUCTURE_INVALID')
                choice=raw['choices'][0]
                stage='PROVIDER_CONTENT_PARSE';evidence['terminal_stage']=stage
                if choice.get('finish_reason')=='length':
                    raise ValueError('OUTPUT_TRUNCATED')
                if not isinstance(choice.get('message'),dict) or 'content' not in choice['message']:
                    raise ValueError('PROVIDER_CONTENT_MISSING')
                content=choice['message']['content']
                if not isinstance(content,str):
                    raise ValueError('PROVIDER_CONTENT_NOT_TEXT')
                evidence['response_content_text']=content
                evidence['response_content_sha256']=hashlib.sha256(
                    content.encode('utf-8')).hexdigest()
                stage='PROVIDER_CONTENT_PARSE';evidence['terminal_stage']=stage
                result=json.loads(content)
                if isinstance(result,dict):
                    response_evidence=dict(provider_envelope=deepcopy(raw),response=deepcopy(result))
                    receipt['response_sha256']=digest(raw)
                stage='RESPONSE_SCHEMA_VALIDATION';evidence['terminal_stage']=stage
                evidence['response']=deepcopy(result)
                evidence['response_sha256']=digest(raw)
                Draft202012Validator(schema).validate(result)
                usage, returned_model = _safe_provider_metadata(raw)
                evidence['terminal_stage']='SUCCESS'
                success_attempt=dict(attempt=attempt_index+1,status='SUCCESS',
                    stage='RESPONSE_SCHEMA_VALIDATION',
                    provider_response_sha256=evidence.get('provider_response_sha256'),
                    evidence=self._save_attempt_evidence(archive,evidence))
                receipt['attempts'].append(success_attempt)
                receipt.update(status='SUCCESS',usage=usage,returned_model=returned_model,
                    elapsed_seconds=round(time.monotonic()-started,3),response_sha256=digest(raw),
                    provider_transport_verified=True)
                receipt['attempt_evidence_integrity']=[dict(
                    attempt=row['attempt'], status=row['status'],
                    evidence_content_sha256=row['evidence']['sha256'],
                    provider_response_sha256=row.get('provider_response_sha256'),
                    reason_code=row.get('reason_code'), stage=row.get('stage'))
                    for row in receipt['attempts']]
                receipt['terminal_attempt_evidence']=private_chain_evidence(evidence)
                receipt.pop('original_provider_invocation_verified', None)
                write(path,dict(request=body,result=result,result_sha256=digest(result),receipt=receipt))
                write(archive,receipt)
                self._settle_batch_invocation(receipt)
                self._record_receipt(receipt)
                return result,receipt
            except (requests.RequestException,ValueError,KeyError,TypeError,ValidationError,OSError) as exc:
                # Private files retain provider bodies and exact failed values.
                # Never serialize headers, credentials, endpoint or exception text.
                evidence['reason_code']=provider_reason(stage,exc)
                failure=dict(attempt=attempt_index+1,status='FAILED',error=type(exc).__name__,
                    stage=stage,reason_code=evidence['reason_code'])
                if isinstance(exc, requests.exceptions.HTTPError):
                    failure['http_status'] = exc.response.status_code
                    evidence['http_status'] = exc.response.status_code
                    failure['provider_response_sha256']=evidence.get('provider_response_sha256')
                    failure['provider_response_text_length']=len(
                        evidence.get('provider_response_text',''))
                if isinstance(exc,ValidationError):
                    diagnostic=schema_diagnostic(exc,schema,result)
                elif stage == 'PROVIDER_ENVELOPE_PARSE' \
                        and str(exc) == 'PROVIDER_ENVELOPE_STRUCTURE_INVALID':
                    diagnostic=dict(kind='PROVIDER_ENVELOPE_STRUCTURE',
                        error_type='ValueError', reason_code='PROVIDER_ENVELOPE_STRUCTURE_INVALID')
                elif stage in ('PROVIDER_ENVELOPE_PARSE','PROVIDER_CONTENT_PARSE') \
                        and str(exc) not in ('OUTPUT_TRUNCATED', 'PROVIDER_CONTENT_NOT_TEXT',
                                             'PROVIDER_CONTENT_MISSING'):
                    text=evidence.get('response_content_text') \
                        if stage=='PROVIDER_CONTENT_PARSE' else evidence.get('provider_response_text')
                    if isinstance(text,str):
                        diagnostic=json_diagnostic(exc,text)
                    else:
                        diagnostic=dict(kind='PROVIDER_ENVELOPE_STRUCTURE',
                            error_type=type(exc).__name__)
                else:
                    diagnostic=None
                if diagnostic is not None:
                    evidence['diagnostic']=diagnostic
                    safe=receipt_diagnostic(diagnostic)
                    failure.update({key:deepcopy(safe[key]) for key in (
                        'instance_path','schema_path','validator','schema_sha256',
                        'failing_value_sha256','allowed_value','missing_keys','unexpected_keys',
                        'content_sha256','content_length','line','column','position','reason',
                        ) if key in safe})
                if type(exc) is ValueError and str(exc)=='OUTPUT_TRUNCATED':
                    failure['reason']='OUTPUT_TRUNCATED'
                decision=_retry_decision(exc,evidence)
                if decision['input_overflow_suspected']:
                    # The provider rejected the input length itself. Resending the
                    # same bytes cannot succeed and the caller must shrink the
                    # request instead. Record the fact without displacing the
                    # transport reason code, which must still replay from the
                    # saved HTTP status.
                    failure['reason']='INPUT_OVERFLOW_SUSPECTED'
                    failure['input_overflow_suspected']=True
                failure_evidence=private_chain_evidence(evidence)
                failure['evidence']=self._save_attempt_evidence(archive,evidence)
                receipt['attempts'].append(failure)
                write(archive,receipt)
                if isinstance(exc,requests.exceptions.SSLError) and hasattr(_thread,'session'):
                    # Do not reuse a TLS/keep-alive connection the peer closed.
                    # Certificate verification remains enabled on the new pool.
                    _thread.session.close();del _thread.session
                if decision['retryable'] and attempt_index + 1 < self.max_http_attempts:
                    delay=min(self.retry_backoff_max_seconds,
                        self.retry_backoff_base_seconds*(2**attempt_index))
                    receipt['attempts'][-1]['retry_delay_seconds']=delay
                    write(archive,receipt)
                    time.sleep(delay)
                elif not decision['retryable']:
                    # A non-retryable rejection is terminal for this invocation.
                    # Remaining attempts stay unspent and are settled back to the
                    # batch budget by _settle_batch_invocation.
                    receipt['attempts'][-1]['retry_suppressed']=(
                        'PROVIDER_INPUT_OVERFLOW' if decision['input_overflow_suspected']
                        else 'NON_RETRYABLE_HTTP_STATUS')
                    write(archive,receipt)
                    break
        receipt.update(status='FAILED',elapsed_seconds=round(time.monotonic()-started,3))
        write(archive,receipt)
        self._settle_batch_invocation(receipt)
        self._record_receipt(receipt)
        raise ModelRequestFailed(receipt, response_evidence=response_evidence,
            failure_evidence=failure_evidence)

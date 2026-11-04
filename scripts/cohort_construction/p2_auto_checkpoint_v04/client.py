"""Actual provider calls, separately cached for the frozen v0.4 contract."""
import json
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

_slot_limits = {}
_pacing_locks = {}
_pacing_last_started = {}
_pacing_registry_lock = Lock()
_thread = local()


class ModelRequestFailed(RuntimeError):
    """A failed invocation bound to its own receipt, including under concurrency.

    The receipt contains request identity, timings and sanitized failure
    categories. Parsed response evidence is a separate private exception field;
    it is never serialized into the invocation receipt or its string message.
    RuntimeError inheritance preserves existing callers' exception handling.
    """
    def __init__(self, receipt, *, response_evidence=None):
        self.request_sha256 = receipt['request_sha256']
        self.receipt = deepcopy(receipt)
        self.response_evidence = deepcopy(response_evidence)
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
        self.retry_backoff_base_seconds = self.config.get('retry_backoff_base_seconds', 2.0)
        self.retry_backoff_max_seconds = self.config.get('retry_backoff_max_seconds', 60.0)
        self.min_request_interval_seconds = self.config.get('min_request_interval_seconds', 0.0)
        for value in (self.retry_backoff_base_seconds, self.retry_backoff_max_seconds,
                      self.min_request_interval_seconds):
            if type(value) not in (int, float) or not 0 <= value <= 3600:
                raise ValueError('MODEL_RATE_CONFIG_INVALID')
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

    def ask(self, task, payload, schema, *, system_instructions=SYSTEM_INSTRUCTIONS):
        body = dict(model=self.settings.model, temperature=self.config['model_temperature'],
            max_tokens=self.config['max_output_tokens'], response_format={'type':'json_object'},
            messages=[dict(role='system',content=system_instructions),dict(role='user',
                content=json.dumps(dict(task=task,schema=schema,input=payload),ensure_ascii=False))])
        if self.settings.thinking: body['thinking']={'type':self.settings.thinking}
        key=digest([self.settings.chat_completions_url,body]); path=self.cache/(key+'.json')
        if path.exists():
            saved=json.loads(path.read_text()); Draft202012Validator(schema).validate(saved['result'])
            assert digest(saved['result'])==saved['result_sha256']
            receipt=dict(saved['receipt'],cached=True);self._record_receipt(receipt)
            return saved['result'],receipt
        receipt=dict(task=task,model=self.settings.model,request_sha256=key,cached=False,
            invocation_id=uuid4().hex,started_at=datetime.now(timezone.utc).isoformat(),
            status='IN_FLIGHT',attempts=[],max_http_attempts=self.max_http_attempts,
            retry_backoff_base_seconds=self.retry_backoff_base_seconds,
            retry_backoff_max_seconds=self.retry_backoff_max_seconds,
            min_request_interval_seconds=self.min_request_interval_seconds,
            pacing_seconds_total=0.0)
        archive=self.cache/'invocations'/(receipt['invocation_id']+'.json');write(archive,receipt)
        started=time.monotonic()
        response_evidence=None
        for attempt in range(self.max_http_attempts):
            response_evidence=None
            try:
                with self.slots:
                    delay=self._wait_for_request_start()
                    if delay:
                        receipt['pacing_seconds_total']=round(
                            receipt['pacing_seconds_total']+delay,3)
                    if not hasattr(_thread,'session'): _thread.session=requests.Session()
                    response=_thread.session.post(self.settings.chat_completions_url,
                        headers={'Authorization':'Bearer '+self.settings.api_key},json=body,timeout=(20,180),
                        allow_redirects=False)
                response.raise_for_status();raw=response.json();choice=raw['choices'][0]
                if choice.get('finish_reason')=='length':raise ValueError('OUTPUT_TRUNCATED')
                result=json.loads(choice['message']['content'])
                if isinstance(result,dict):
                    response_evidence=dict(provider_envelope=deepcopy(raw),response=deepcopy(result))
                    receipt['response_sha256']=digest(raw)
                Draft202012Validator(schema).validate(result)
                receipt['attempts'].append(dict(attempt=attempt+1,status='SUCCESS'))
                receipt.update(status='SUCCESS',usage=raw.get('usage'),returned_model=raw.get('model'),
                    elapsed_seconds=round(time.monotonic()-started,3),response_sha256=digest(raw))
                write(path,dict(request=body,result=result,result_sha256=digest(result),receipt=receipt))
                write(archive,receipt);self._record_receipt(receipt)
                return result,receipt
            except (requests.RequestException,ValueError,KeyError,ValidationError) as exc:
                # Record only exception type: never serialize headers or credentials.
                failure=dict(attempt=attempt+1,status='FAILED',error=type(exc).__name__)
                if isinstance(exc, requests.exceptions.HTTPError):
                    # Status is safe operational metadata; never retain body,
                    # headers, endpoint or the exception's formatted message.
                    failure['http_status'] = exc.response.status_code
                if isinstance(exc,ValidationError):
                    # Schema coordinates contain no failed clinical values or
                    # provider messages and are useful for interface auditing.
                    failure['schema_path']=list(exc.absolute_schema_path)
                elif type(exc) is ValueError and str(exc)=='OUTPUT_TRUNCATED':
                    failure['reason']='OUTPUT_TRUNCATED'
                receipt['attempts'].append(failure)
                write(archive,receipt)
                if isinstance(exc,requests.exceptions.SSLError) and hasattr(_thread,'session'):
                    # Do not reuse a TLS/keep-alive connection the peer closed.
                    # Certificate verification remains enabled on the new pool.
                    _thread.session.close();del _thread.session
                if attempt + 1 < self.max_http_attempts:
                    delay=min(self.retry_backoff_max_seconds,
                        self.retry_backoff_base_seconds*(2**attempt))
                    receipt['attempts'][-1]['retry_delay_seconds']=delay
                    write(archive,receipt)
                    time.sleep(delay)
        receipt.update(status='FAILED',elapsed_seconds=round(time.monotonic()-started,3))
        write(archive,receipt);self._record_receipt(receipt)
        raise ModelRequestFailed(receipt, response_evidence=response_evidence)

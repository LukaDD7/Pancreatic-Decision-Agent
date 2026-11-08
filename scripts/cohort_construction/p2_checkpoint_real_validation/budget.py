"""Shared accounting and explicit expected gain for every provider request."""
from copy import deepcopy
import json
import os

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_material_review_app.build import private_mkdir, private_open
from scripts.cohort_construction.p2_checkpoint_ready_pipeline.persistence import write


def _journal_write(path, value):
    """Atomically replace the current snapshot; event files stay immutable."""
    temporary = path.with_name(path.name + '.tmp')
    with private_open(temporary, 'x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class BatchBudgetExhausted(RuntimeError):
    def __init__(self, reason):
        self.receipt = dict(cached=False, status='LOCAL_BATCH_BUDGET_DENIED',
            provider_invocation_occurred=False, reason_code=reason)
        super().__init__(reason)


class BatchModelBudget:
    """One sequential ledger across Episodes; cache hits do not spend requests."""
    def __init__(self, output, *, max_requests=60, max_http_attempts=180, state=None,
                 max_http_attempts_per_request=3):
        if (type(max_requests) is not int or not 1 <= max_requests <= 1000
                or type(max_http_attempts) is not int or not 1 <= max_http_attempts <= 3000
                or type(max_http_attempts_per_request) is not int
                or not 1 <= max_http_attempts_per_request <= 100):
            raise ValueError('REAL_BATCH_BUDGET_INVALID')
        self.max_http_attempts_per_request = max_http_attempts_per_request
        self.output = output
        self.max_requests = max_requests
        self.max_http_attempts = max_http_attempts
        self.records = []
        self.requests = 0
        self.http_attempts = 0
        self.cache_hits = 0
        self.denied = 0
        self.usage = dict(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        if state is not None:
            self.records = deepcopy(state['records'])
            self.requests = state['requests']
            self.http_attempts = state['http_attempts']
            self.cache_hits = state['cache_hits']
            self.denied = state['denied']
            self.usage = deepcopy(state['usage'])
        if self.requests > max_requests or self.http_attempts > max_http_attempts:
            raise ValueError('REAL_BATCH_INHERITED_BUDGET_EXCEEDS_LIMIT')

    def snapshot(self):
        return dict(requests=self.requests, max_requests=self.max_requests,
            http_attempts=self.http_attempts, max_http_attempts=self.max_http_attempts,
            cache_hits=self.cache_hits, denied=self.denied, usage=deepcopy(self.usage),
            records=deepcopy(self.records), cost_amount=None,
            cost_status='NOT_ESTIMATED_RATE_UNAVAILABLE')

    def persist(self):
        if self.output is not None:
            _journal_write(self.output / 'model-call-ledger.json', self.snapshot())

    def adapter(self, stage, ask, context):
        def call(task, payload, schema, *, transport_projection=None):
            # CE-REAL024: reserve and journal the exact projected provider task,
            # not its larger persisted full-request envelope. The projection is
            # deterministic and receipt-bound; it does not change source scope.
            projected = payload if transport_projection is None else transport_projection(payload)
            request_sha256 = canonical_sha256(projected)
            target = projected.get('target', {})
            binding = projected.get('response_binding', {})
            record = dict(context=deepcopy(context), stage=stage, task=task,
                request_sha256=request_sha256, target=deepcopy(target),
                precondition=binding, expected_gain=_gain(stage),
                new_information_expected=_new_information(stage),
                unlocks=_unlocks(stage),
                stop_conditions=['TERMINAL_HOLD_OR_REVISE', 'EXECUTION_OR_VALIDATION_FAILURE',
                    'UNCHANGED_REQUEST_HASH', 'BATCH_BUDGET_EXHAUSTED'],
                provider_request_performed=False)
            if self.requests >= self.max_requests:
                self.denied += 1
                record['outcome'] = 'NOT_ATTEMPTED_BATCH_BUDGET_EXHAUSTED'
                self._save(record)
                raise BatchBudgetExhausted('REAL_BATCH_REQUEST_BUDGET_EXHAUSTED')
            if self.max_http_attempts - self.http_attempts < self.max_http_attempts_per_request:
                self.denied += 1
                record['outcome'] = 'NOT_ATTEMPTED_HTTP_ATTEMPT_BUDGET_EXHAUSTED'
                self._save(record)
                raise BatchBudgetExhausted('REAL_BATCH_HTTP_ATTEMPT_BUDGET_EXHAUSTED')
            # Reserve before provider I/O: exceptions without a sanitized receipt
            # cannot reset the batch allowance.  Append-only events preserve the
            # pre-call plan even if the process stops during provider I/O.
            self.requests += 1
            self.http_attempts += self.max_http_attempts_per_request  # reserve the configured Client maximum
            record['provider_request_performed'] = True
            record['outcome'] = 'PROVIDER_IO_STARTED_OR_UNSETTLED'
            self._save(record)
            try:
                response, receipt = ask(task, projected, schema)
            except Exception as exc:
                receipt = deepcopy(getattr(exc, 'receipt', {}))
                if not isinstance(receipt, dict):
                    receipt = {}
                if receipt.get('cached') is True:
                    self.requests -= 1
                    self.http_attempts -= self.max_http_attempts_per_request
                    self.cache_hits += 1
                self._receipt(record, receipt)
                record['outcome'] = 'EXECUTION_FAILED:' + type(exc).__name__
                record['actual_gain'] = 'FAILURE_CLASSIFIED_NO_DOWNSTREAM_PASS'
                self._save(record)
                raise
            if not isinstance(receipt, dict):
                receipt = {}
            if receipt.get('cached') is True:
                self.requests -= 1
                self.http_attempts -= self.max_http_attempts_per_request
                self.cache_hits += 1
            self._receipt(record, receipt)
            record['outcome'] = 'RESPONSE_RETURNED_PENDING_CHAIN_VALIDATION'
            record['actual_gain'] = 'NEW_RESPONSE_AVAILABLE_FOR_DETERMINISTIC_CHAIN_VALIDATION'
            self._save(record)
            return response, receipt
        return call

    def _save(self, record):
        """Keep both plans and settlements as independent immutable records."""
        self.records.append(deepcopy(record))
        if self.output is not None:
            directory = self.output / 'call-events'
            private_mkdir(directory, exist_ok=True)
            write(directory / ('%06d.json' % len(self.records)), record)
        self.persist()

    def _receipt(self, record, receipt):
        # Receipt schemas contain no endpoint credentials.  Never copy provider
        # exception messages, headers, settings, or response bodies into ledger.
        allowed = ('request_sha256', 'invocation_id', 'cached', 'status', 'model',
            'returned_model', 'usage', 'elapsed_seconds', 'attempts', 'response_sha256',
            'max_http_attempts', 'retry_backoff_base_seconds', 'retry_backoff_max_seconds',
            'min_request_interval_seconds', 'pacing_seconds_total',
            'declared_provider_payload_sha256', 'receipt_origin',
            'provider_invocation_occurred', 'provider_transport_verified')
        record['receipt'] = {key: deepcopy(receipt[key]) for key in allowed if key in receipt}
        # Synthetic adapters without a provider hash are still journaled, but
        # cannot certify the provider payload binding (real receipts require it).
        record['provider_payload_binding_verified'] = (
            record['receipt'].get('declared_provider_payload_sha256') == record['request_sha256'])
        if receipt.get('declared_provider_payload_sha256') is not None \
                and not record['provider_payload_binding_verified']:
            raise ValueError('REAL_BATCH_PROVIDER_PAYLOAD_LEDGER_MISMATCH')
        if not record['receipt'].get('cached'):
            actual = record['receipt'].get('attempts')
            if isinstance(actual, list) and actual:
                self.http_attempts -= self.max_http_attempts_per_request - len(actual)
        usage = record['receipt'].get('usage', {})
        if isinstance(usage, dict):
            for key in self.usage:
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    self.usage[key] += value


def _gain(stage):
    return {
        'graph': 'INDEPENDENT_SEVEN_DIMENSION_AUDIT_OF_THIS_FROZEN_EPISODE_GRAPH',
        'reconstruction': 'SEPARATE_RECONSTRUCTION_PROPOSAL_FOR_THIS_AUDITED_GRAPH',
        'material': 'SOURCE_COVERAGE_VISIBILITY_AND_PARTITION_AUDIT_OF_THIS_CHECKPOINT_VERSION',
        'delivery': 'SEMANTIC_AUDIT_OF_ACTUAL_STAGING_PACKET_FHIR_AND_HTML',
    }[stage]


def _new_information(stage):
    return {
        'graph': 'WHETHER_THIS_GRAPH_CAN_ENTER_MATERIAL_REVIEW_OR_REQUIRES_HOLD_REVISION',
        'reconstruction': 'WHETHER_A_SEPARATE_LLM_CAN_PROPOSE_A_VALID_NEW_GRAPH_VERSION',
        'material': 'WHETHER_THIS_EXACT_COMPILED_HASH_IS_PASS_OR_REQUIRES_A_BOUNDED_CHANGE',
        'delivery': 'WHETHER_THIS_ACTUAL_DELIVERY_INPUT_HASH_IS_PASS_HOLD_OR_REVISE',
    }[stage]


def _unlocks(stage):
    return {
        'graph': 'MATERIAL_PREPARATION_ONLY_IF_AUTO_GRAPH_AUDIT_PASS',
        'reconstruction': 'COMPLETE_FRESH_GRAPH_MATERIAL_AND_DELIVERY_AUDITS_ONLY',
        'material': 'EPISODE_MATERIAL_SET_ONLY_IF_ALL_TARGETS_AUTO_REVIEW_PASS',
        'delivery': 'STAGING_READY_RESOURCE_ONLY_IF_AUTO_DELIVERY_AUDIT_PASS',
    }[stage]

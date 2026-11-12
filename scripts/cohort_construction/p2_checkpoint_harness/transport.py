"""Provider transport for the harness: same audit discipline, message-shaped API.

This is a separate module with its own cache directory. It never reads or writes
the frozen batches or their caches. What it keeps from the frozen client, because
those properties were load-bearing rather than incidental:

* one cache key per exact HTTP body: ``digest([chat_completions_url, body])``,
  kept distinct from the declared payload hash and from any canonical repair hash
* a receipt per invocation with the provider's own ``usage`` block, never an
  estimate presented as a measurement
* a terminal 4xx is not retried; only a provider-confirmed length rejection is
  reported as ``INPUT_OVERFLOW_SUSPECTED`` so the caller shrinks the request
  instead of burning attempts on identical bytes
* no credential, endpoint or provider exception text in any receipt

What differs: the request carries an arbitrary message list, because a tool-using
loop cannot be expressed as one ``[system, user]`` pair.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

import requests

from scripts.cohort_construction.p2_auto_checkpoint_v04.client import (
    _retry_decision, _safe_provider_metadata,
)

FORMAT = "p2-harness-transport-0.1.0"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class TransportFailed(RuntimeError):
    def __init__(self, receipt):
        super().__init__("MODEL_REQUEST_FAILED:" + str(receipt.get("status")))
        self.receipt = receipt


class Transport:
    """Cached, receipted chat-completions caller."""

    def __init__(self, cache, settings, *, config=None, session=None):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache, 0o700)
        self.settings = settings
        self.config = dict(config or {})
        self.session = session
        self.receipts = []
        self._lock = Lock()
        self.max_attempts = int(self.config.get("max_http_attempts_per_request", 3))
        self.read_timeout = float(self.config.get("provider_read_timeout_seconds", 900))
        self.backoff_base = float(self.config.get("retry_backoff_base_seconds", 5))
        self.backoff_max = float(self.config.get("retry_backoff_max_seconds", 60))
        self.max_output_tokens = int(self.config.get("max_output_tokens", 8000))
        self.temperature = self.config.get("model_temperature", 0)

    # -- public --------------------------------------------------------
    def complete(self, messages, *, tools=None, task="agent_step", purpose=None):
        body = dict(model=self.settings.model, temperature=self.temperature,
                    max_tokens=self.max_output_tokens,
                    response_format={"type": "json_object"},
                    messages=list(messages))
        if tools:
            body["tools"] = _wire_tools(tools)
        if self.settings.thinking:
            body["thinking"] = {"type": self.settings.thinking}
        key = _digest([self.settings.chat_completions_url, body])
        cached = self.cache / (key + ".json")
        if cached.is_file():
            saved = json.loads(cached.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("request_sha256") != key:
                raise ValueError("HARNESS_CACHE_KEY_MISMATCH")
            if saved.get("response_sha256") != _digest(saved.get("result")):
                raise ValueError("HARNESS_CACHE_RESULT_HASH_MISMATCH")
            receipt = dict(saved["receipt"], cached=True,
                           provider_invocation_occurred=False)
            self._record(receipt, task=task, purpose=purpose)
            return saved["result"], receipt

        receipt = dict(
            format=FORMAT, task=task, purpose=purpose, model=self.settings.model,
            request_sha256=key, invocation_id=uuid4().hex, status="IN_FLIGHT",
            started_at=datetime.now(timezone.utc).isoformat(), attempts=[],
            declared_messages_sha256=_digest(messages),
            declared_system_sha256=_digest(messages[0]["content"]) if messages else None,
            max_http_attempts=self.max_attempts, cached=False,
            provider_invocation_occurred=True)
        started = time.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.post(
                    self.settings.chat_completions_url, json=body, timeout=(20, self.read_timeout),
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                    allow_redirects=False)
            except requests.RequestException as exc:
                receipt["attempts"].append(dict(
                    attempt=attempt, status="FAILED",
                    error=type(exc).__name__,
                    reason_code="TRANSPORT_ERROR"))
                if attempt < self.max_attempts:
                    self._sleep(attempt)
                    continue
                break
            text = response.text
            evidence = dict(http_status=response.status_code,
                            provider_response_sha256=hashlib.sha256(
                                text.encode("utf-8")).hexdigest(),
                            provider_response_length=len(text))
            if response.status_code >= 400:
                decision = _retry_decision(
                    requests.exceptions.HTTPError(response=response), evidence)
                receipt["attempts"].append(dict(
                    attempt=attempt, status="FAILED", http_status=response.status_code,
                    reason_code="PROVIDER_HTTP_ERROR",
                    input_overflow_suspected=bool(decision["input_overflow_suspected"])))
                if decision["retryable"] and attempt < self.max_attempts:
                    self._sleep(attempt)
                    continue
                receipt.update(status="FAILED",
                               input_overflow_suspected=bool(decision["input_overflow_suspected"]),
                               elapsed_seconds=round(time.monotonic() - started, 3))
                self._finish(receipt, task=task, purpose=purpose)
                raise TransportFailed(receipt)
            try:
                envelope = response.json()
            except ValueError:
                receipt["attempts"].append(dict(
                    attempt=attempt, status="FAILED", reason_code="PROVIDER_ENVELOPE_NOT_JSON",
                    http_status=response.status_code))
                if attempt < self.max_attempts:
                    self._sleep(attempt)
                    continue
                break
            content, refusal = _extract_content(envelope)
            if content is None:
                receipt["attempts"].append(dict(
                    attempt=attempt, status="FAILED", reason_code="PROVIDER_CONTENT_MISSING",
                    http_status=response.status_code, refusal=refusal))
                if attempt < self.max_attempts:
                    self._sleep(attempt)
                    continue
                break
            # The provider's answer is delivered as-is. Whether it is usable
            # JSON is the agent's decision, not the transport's: the agent has
            # one format-repair request and a written stop reason for exactly
            # this case. Parsing here instead meant three identical retries
            # (three billed requests) and then a hard failure that bypassed the
            # repair path entirely — seen on the third live attempt, 2026-11-09.
            try:
                result = json.loads(content)
                parsed = True
            except ValueError:
                result = content
                parsed = False
            usage, returned_model = _safe_provider_metadata(envelope)
            receipt["attempts"].append(dict(attempt=attempt, status="SUCCESS",
                                            http_status=response.status_code))
            receipt.update(status="SUCCESS", usage=usage, returned_model=returned_model,
                           content_parsed=parsed,
                           content_sha256=hashlib.sha256(
                               content.encode("utf-8")).hexdigest(),
                           content_length=len(content),
                           elapsed_seconds=round(time.monotonic() - started, 3),
                           response_sha256=_digest(result))
            if self.session is not None:
                self.session.append("policy/event", dict(
                    kind="provider_usage", usage=usage, task=task,
                    estimated_prompt_tokens=(messages and _estimated(messages)) or 0,
                    request_sha256=key))
            self._write(cached, dict(request_sha256=key, result=result,
                                     response_sha256=_digest(result), receipt=receipt))
            self._finish(receipt, task=task, purpose=purpose)
            return result, receipt
        receipt.update(status="FAILED", elapsed_seconds=round(time.monotonic() - started, 3))
        self._finish(receipt, task=task, purpose=purpose)
        raise TransportFailed(receipt)

    # -- internals -----------------------------------------------------
    def _sleep(self, attempt):
        delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        time.sleep(delay)

    def _write(self, path, value):
        path = Path(path)
        # ``invocations/`` is created on first use, not in the constructor:
        # without this the very first request fails *after* the provider has
        # already answered, so a billed response is lost and the case stops on
        # a local FileNotFoundError. Caught by the first live run, 2026-11-09.
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name("." + path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _finish(self, receipt, *, task, purpose):
        self._write(self.cache / "invocations" / (receipt["invocation_id"] + ".json"),
                    receipt)
        self._record(receipt, task=task, purpose=purpose)

    def _record(self, receipt, *, task, purpose):
        with self._lock:
            self.receipts.append(dict(
                task=task, purpose=purpose, status=receipt.get("status"),
                usage=deepcopy(receipt.get("usage") or {}),
                cached=receipt.get("cached", False),
                request_sha256=receipt.get("request_sha256"),
                invocation_id=receipt.get("invocation_id")))


def _wire_tools(tools):
    """Translate the internal tool schema into the provider's ``tools`` array.

    The registry advertises ``{name, description, input_schema}`` — the shape the
    rest of the harness reads and the shape the agent loop validates against.
    Chat-completions wants ``{type: function, function: {name, description,
    parameters}}``. Sending the internal shape verbatim was refused by the
    provider with ``'name' is a required property - '***.***.function'``
    (HTTP 400, first live run 2026-11-09), so the translation lives here, at the
    one boundary where the two schemas meet.

    A tool already in provider shape is passed through unchanged: the
    translation must not reshape what it has already produced.
    """
    wire = []
    for tool in tools or ():
        if not isinstance(tool, dict):
            raise ValueError("TOOL_SCHEMA_NOT_OBJECT")
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            wire.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("TOOL_SCHEMA_MISSING_NAME")
        wire.append(dict(type="function", function=dict(
            name=name,
            description=tool.get("description") or "",
            parameters=tool.get("input_schema")
            or dict(type="object", properties={}))))
    return wire


def _extract_content(envelope):
    if not isinstance(envelope, dict):
        return None, None
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, None
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return None, None
    refusal = message.get("refusal")
    content = message.get("content")
    if not isinstance(content, str):
        return None, refusal
    if choice.get("finish_reason") == "length":
        return None, "OUTPUT_TRUNCATED"
    return content, refusal


def _estimated(messages):
    from .tokens import estimate_messages
    return estimate_messages(messages)
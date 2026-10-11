"""Small OpenAI-compatible transport; no patient selection or clinical control."""

from dataclasses import asdict, dataclass
import os
import re
import time
from urllib.parse import urlsplit

import openai
from openai import OpenAI

from .protocol import OUTPUT_SCHEMA


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    key_env: str = "BOYU_API_KEY"
    temperature: float = 1.0
    max_tokens: int = 8000
    timeout: float = 300.0
    retries: int = 0
    response_format: str = "prompt_only"

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("Endpoint must be an HTTPS base URL without credentials or query")
        if not self.model.strip() or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", self.key_env):
            raise ValueError("Model and credential environment name are required")
        if (not 0 <= self.temperature <= 2 or self.max_tokens <= 0
                or self.timeout <= 0 or not 0 <= self.retries <= 5):
            raise ValueError("Invalid generation limits")
        if self.response_format not in ("prompt_only", "json_object", "json_schema"):
            raise ValueError("Unknown response format")

    def public_dict(self):
        return asdict(self)  # Contains an environment NAME, never a credential value.

    def request_parameters(self):
        params = {"model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.response_format == "json_object":
            params["response_format"] = {"type": "json_object"}
        elif self.response_format == "json_schema":
            params["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "pancreas_independent_decision", "strict": True, "schema": OUTPUT_SCHEMA}}
        return params


@dataclass
class Completion:
    text: str | None
    finish_reason: str | None
    metadata: dict
    attempts: list


class TransportFailure(Exception):
    def __init__(self, attempts):
        super().__init__("Model transport failed; provider body omitted")
        self.attempts = attempts


class ChatCompletionClient:
    def __init__(self, settings):
        key = os.environ.get(settings.key_env, "").strip()
        if not key:
            raise ValueError("Credential environment variable is empty: " + settings.key_env)
        self.settings = settings
        self.client = OpenAI(api_key=key, base_url=settings.base_url, timeout=settings.timeout, max_retries=0)

    def complete(self, messages, on_attempt=None):
        attempts = []
        for index in range(self.settings.retries + 1):
            started = time.monotonic()
            attempt = {"attempt": index + 1, "status": "STARTED"}
            attempts.append(attempt)
            if on_attempt:
                on_attempt(attempts)
            try:
                response = self.client.chat.completions.create(
                    **self.settings.request_parameters(), messages=messages)
            except openai.APIError as exc:
                code = getattr(exc, "status_code", None)
                attempt.update(status="FAILED", error_type=type(exc).__name__, http_status=code,
                               elapsed_seconds=round(time.monotonic() - started, 3))
                if on_attempt:
                    on_attempt(attempts)
                transient = code in (408, 409, 429) or (code is not None and code >= 500) or isinstance(exc, openai.APIConnectionError)
                if not transient or index == self.settings.retries:
                    raise TransportFailure(attempts) from None
                time.sleep(min(2 ** index, 8))
                continue
            except KeyboardInterrupt:
                attempt.update(status="INTERRUPTED", elapsed_seconds=round(time.monotonic() - started, 3))
                if on_attempt:
                    on_attempt(attempts)
                raise
            attempt.update(status="RECEIVED", elapsed_seconds=round(time.monotonic() - started, 3))
            if on_attempt:
                on_attempt(attempts)
            choice = response.choices[0] if response.choices else None
            metadata = {"requested_model": self.settings.model, "returned_model": response.model,
                        "usage": response.usage.model_dump() if response.usage else None,
                        "request_id": getattr(response, "_request_id", None), "sdk_version": openai.__version__,
                        "refused": bool(choice and getattr(choice.message, "refusal", None))}
            # Do not collect provider reasoning_content, headers or the whole response.
            return Completion(choice.message.content if choice else None,
                              choice.finish_reason if choice else None, metadata, attempts)
        raise AssertionError("Unreachable retry state")

    def close(self):
        self.client.close()

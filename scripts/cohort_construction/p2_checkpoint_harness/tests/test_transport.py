"""Transport against a fresh cache: the first billed answer must not be lost.

Found by the first live run (2026-11-09), in two separate steps:

1. ``invocations/`` was only created when a receipt was archived, but the
   receipt is written *after* the provider answers. On an empty cache the first
   response therefore raised ``FileNotFoundError`` after it had already been
   billed — the tokens were spent and nothing was kept.
2. The registry's tool shape was put on the wire unchanged, and the provider
   refused the body with HTTP 400. The two shapes are different; only this
   boundary can translate one into the other.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.cohort_construction.p2_checkpoint_harness.transport import Transport

MESSAGES = [dict(role="user", content="虚构问题")]


class _Settings:
    model = "fictional-model"
    thinking = None
    api_key = "not-a-real-key"
    chat_completions_url = "https://example.invalid/v1/chat/completions"


class _Response:
    status_code = 200
    text = "{}"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _envelope(content):
    return dict(choices=[dict(message=dict(content=json.dumps(content)),
                              finish_reason="stop")],
                usage=dict(prompt_tokens=11, completion_tokens=3))


def _post_body(transport_call):
    """Run one call with ``requests.post`` replaced; return the HTTP body sent."""
    seen = {}

    def post(url, json=None, **kwargs):
        seen["body"] = json
        return _Response(_envelope(dict(final={})))

    with tempfile.TemporaryDirectory() as tmp:
        with patch("scripts.cohort_construction.p2_checkpoint_harness."
                   "transport.requests.post", post):
            transport_call(Transport(Path(tmp) / "cache", _Settings()))
    return seen["body"]


class TransportCacheTests(unittest.TestCase):
    def test_HTR001_first_answer_is_cached_in_a_brand_new_cache_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"          # deliberately not created
            transport = Transport(cache, _Settings())
            with patch("scripts.cohort_construction.p2_checkpoint_harness."
                       "transport.requests.post",
                       lambda *a, **k: _Response(_envelope(dict(final={})))):
                result, receipt = transport.complete(MESSAGES)
            self.assertEqual(result, dict(final={}))
            self.assertEqual(receipt["status"], "SUCCESS")
            self.assertEqual(receipt["usage"]["prompt_tokens"], 11)
            # Everything the receipt claims exists on disk.
            for path in cache.glob("*.json"):
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["request_sha256"], receipt["request_sha256"])
            self.assertTrue(
                (cache / "invocations" / (receipt["invocation_id"] + ".json")
                 ).is_file())
            self.assertEqual(transport.receipts[0]["status"], "SUCCESS")

    def test_HTR002_the_second_call_is_served_from_cache_without_a_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            calls = []

            def post(*args, **kwargs):
                calls.append(args)
                return _Response(_envelope(dict(final={})))

            with patch("scripts.cohort_construction.p2_checkpoint_harness."
                       "transport.requests.post", post):
                Transport(cache, _Settings()).complete(MESSAGES)
                _, receipt = Transport(cache, _Settings()).complete(MESSAGES)
            self.assertEqual(len(calls), 1)
            self.assertTrue(receipt["cached"])
            self.assertFalse(receipt["provider_invocation_occurred"])

    def test_HTR003_tool_schemas_leave_the_process_in_provider_shape(self):
        # The registry's shape is not the wire shape. Sending it verbatim was
        # refused with HTTP 400 ("'name' is a required property -
        # '***.***.function'") on the first live run.
        registry_shape = [dict(name="lookup", description="虚构检索",
                               input_schema=dict(
                                   type="object",
                                   properties=dict(q=dict(type="string"))))]
        body = _post_body(lambda transport: transport.complete(
            MESSAGES, tools=registry_shape))
        sent = body["tools"]
        self.assertEqual(sent[0]["type"], "function")
        self.assertEqual(sent[0]["function"]["name"], "lookup")
        self.assertEqual(sent[0]["function"]["parameters"],
                         registry_shape[0]["input_schema"])
        self.assertNotIn("input_schema", sent[0])

    def test_HTR004_a_tool_already_in_provider_shape_is_not_reshaped(self):
        wire = [dict(type="function", function=dict(name="f", parameters={}))]
        body = _post_body(lambda transport: transport.complete(MESSAGES, tools=wire))
        self.assertEqual(body["tools"], wire)


    def test_HTR005_unparseable_content_is_returned_verbatim_not_retried(self):
        # Live run 2026-11-09: a plainly non-JSON answer was retried three times
        # (three billed requests) and then raised, so the agent's one format
        # repair never ran. Format is the agent's question; the transport ships
        # what the provider said, exactly once.
        calls = []

        def post(*args, **kwargs):
            calls.append(1)
            return _Response(dict(
                choices=[dict(message=dict(content="这不是 JSON"),
                              finish_reason="stop")],
                usage=dict(prompt_tokens=5, completion_tokens=2)))

        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.cohort_construction.p2_checkpoint_harness."
                       "transport.requests.post", post):
                result, receipt = Transport(Path(tmp) / "cache",
                                            _Settings()).complete(MESSAGES)
        self.assertEqual(result, "这不是 JSON")
        self.assertEqual(len(calls), 1)
        self.assertEqual(receipt["status"], "SUCCESS")
        self.assertFalse(receipt["content_parsed"])
        self.assertEqual(receipt["content_sha256"],
                         hashlib.sha256("这不是 JSON".encode("utf-8")).hexdigest())

    def test_HTR006_truncated_output_is_still_a_content_failure(self):
        # ``finish_reason=length`` means the answer is cut off mid-object. That
        # is not a format the model chose, and resending the same bytes will not
        # help, so it is reported instead of handed back as if it were an answer.
        def post(*args, **kwargs):
            return _Response(dict(choices=[dict(
                message=dict(content='{"partial":'), finish_reason="length")]))

        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.cohort_construction.p2_checkpoint_harness."
                       "transport.requests.post", post):
                with self.assertRaises(Exception):
                    Transport(Path(tmp) / "cache", _Settings()).complete(MESSAGES)


if __name__ == "__main__":
    unittest.main()

"""Fixed-context evaluation tests: fictional data, mock responses, zero network."""

from copy import deepcopy
import json
from pathlib import Path

import httpx
from openai import OpenAI
import pytest

from pancreatic_agent.episode_extract import extract_episode_case
from pancreatic_agent.evaluation import client as transport, runner
from pancreatic_agent.evaluation.client import ChatCompletionClient, Completion, ModelSettings, TransportFailure
from pancreatic_agent.evaluation.protocol import ACTION_CATALOG, ACTION_MODES, OUTPUT_SCHEMA, build_messages, validate_output
from pancreatic_agent.four_stage import sha


@pytest.fixture
def packets():
    root = Path(__file__).resolve().parents[1]
    bundle = json.loads((root / "examples/hewv_fictional_bundle.json").read_text())
    review = json.loads((root / "examples/hewv_fictional_review.json").read_text())["S001"]
    return extract_episode_case(bundle, "S001", review)[0]


def decision(mode="PAUSE", code="PANCREAS_PROTOCOL_CT", source="IMG0001"):
    return {"schema_version": "pancreas-decision-v0.1", "mode": mode,
            "clinical_assessment": "Fictional current assessment", "resectability": "UNDETERMINED",
            "primary_action": {"code": code, "target": "PANCREAS", "purpose": "Resolve a fictional staging question",
                               "management_if_supporting": "Reconsider the pathway", "management_if_not_supporting": "Reassess for surgery",
                               "source_ids": [source]},
            "evidence": [{"source_id": source, "interpretation": "Fictional observed report"}],
            "uncertainties": ["Fictional missing vascular detail"],
            "decision_change_conditions": ["New targeted report resolves this question"], "confidence": 0.5}


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, on_attempt=None):
        self.requests.append(deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def completion(output=None, finish="stop"):
    return Completion(json.dumps(output or decision()), finish,
                      {"returned_model": "fictional-model", "refused": False}, [{"attempt": 1}])


@pytest.fixture
def settings():
    return ModelSettings("https://example.invalid/v1/", "fictional-model")


@pytest.fixture(autouse=True)
def local_output(monkeypatch):
    monkeypatch.setattr(runner, "protected_output", lambda p: p.resolve())


def test_action_schema_and_catalog_are_consistent():
    codes = OUTPUT_SCHEMA["properties"]["primary_action"]["properties"]["code"]["enum"]
    assert len(ACTION_CATALOG["actions"]) == len(ACTION_MODES) == len(codes)
    assert set(codes) == set(ACTION_MODES)


@pytest.mark.parametrize("mode,code", [("PAUSE", "LIVER_MRI"), ("EXIT", "REDIRECT_ONCOLOGY_PATHWAY"),
                                       ("CONTINUE", "CONTINUE_NO_NEW_STAGING")])
def test_three_modes_have_valid_typed_actions(mode, code):
    _, errors = validate_output(json.dumps(decision(mode, code)), ["IMG0001"])
    assert not errors


@pytest.mark.parametrize("text", ['```json\n{}\n```', '{"mode":"PAUSE","mode":"EXIT"}', '{"confidence":NaN}', '[]'])
def test_invalid_json_or_schema_is_never_repaired(text):
    _, errors = validate_output(text, ["IMG0001"])
    assert errors


def test_invalid_action_mode_and_future_source_rejected():
    output = decision("CONTINUE", "LIVER_MRI", "FUTURE001")
    _, errors = validate_output(json.dumps(output), ["IMG0001"])
    assert set(errors) == {"MODE_ACTION_MISMATCH", "NONVISIBLE_SOURCE_REFERENCE"}


def test_nested_mode_is_rejected_without_deleting_the_unexpected_field():
    output = decision()
    output["primary_action"]["mode"] = "PAUSE"
    candidate, errors = validate_output(json.dumps(output), ["IMG0001"])
    assert candidate["primary_action"]["mode"] == "PAUSE"
    assert "SCHEMA:primary_action:additionalProperties" in errors


def test_pause_requires_explicit_condition_for_changing_decision():
    output = decision()
    output["decision_change_conditions"] = []
    assert "PAUSE_REQUIRES_DECISION_CHANGE_CONDITION" in validate_output(json.dumps(output), ["IMG0001"])[1]


def test_four_fresh_contexts_repeats_and_exact_saved_requests(packets, settings, tmp_path):
    fake = FakeClient([completion()] * 8)
    directory, summary = runner.run_evaluation(packets, tmp_path, settings, repeats=2,
                                               allow_draft=True, execute=True, client=fake)
    assert len(fake.requests) == 8 and all(len(m) == 2 for m in fake.requests)
    for i, p in enumerate(packets["packets"]):
        expected = build_messages(p["question"], p["context"])
        assert fake.requests[2 * i] == fake.requests[2 * i + 1] == expected
        request = json.loads((directory / "requests" / f"stage_{i + 1:02d}_repeat_01.json").read_text())
        assert request["messages"] == expected
        assert p["context"] in expected[1]["content"]
        assert "Fictional current assessment" not in expected[1]["content"]
    assert summary["valid_samples"] == summary["api_attempts"] == 8
    assert summary["clinical_accuracy"] is None
    assert all(g["mode_agreement_among_valid"] == 1 for g in summary["by_stage"])


def test_dry_run_has_zero_requests_and_does_not_need_a_key(packets, settings, tmp_path, monkeypatch):
    monkeypatch.delenv("BOYU_API_KEY", raising=False)
    fake = FakeClient([])
    _, summary = runner.run_evaluation(packets, tmp_path, settings, allow_draft=True, client=fake)
    assert not fake.requests and summary["api_attempts"] == 0
    assert summary["validation_pass_rate"] is None


@pytest.mark.parametrize("problem", ["draft_flag", "unresolved", "body", "context", "records_removed"])
def test_bad_input_stops_before_any_remote_call(problem, packets, settings, tmp_path):
    allow = True
    p = packets["packets"][0]
    if problem == "draft_flag":
        allow = False
    elif problem == "unresolved":
        p["status"] = "UNRESOLVED"
    elif problem == "body":
        p["records"][0]["body"] = "tampered"
    elif problem == "context":
        p["context"] += "injected future result"
        p["context_sha256"] = sha(p["context"])
    else:
        p["records"].clear()
    fake = FakeClient([])
    with pytest.raises(ValueError):
        runner.run_evaluation(packets, tmp_path, settings, allow_draft=allow, execute=True, client=fake)
    assert not fake.requests


def test_truncated_json_and_transport_failures_remain_technical_failures(packets, settings, tmp_path):
    fake = FakeClient([completion(finish="length"), TransportFailure([{"attempt": 1, "http_status": 429}]),
                       Completion(None, "stop", {}, [{"attempt": 1}]), completion(decision(source="FUTURE001"))])
    directory, summary = runner.run_evaluation(packets, tmp_path, settings, allow_draft=True, execute=True, client=fake)
    rows = [json.loads(p.read_text()) for p in sorted((directory / "results").glob("*.json"))]
    assert [r["status"] for r in rows] == ["INCOMPLETE_RESPONSE", "TRANSPORT_FAILURE", "EMPTY_RESPONSE", "INVALID_OUTPUT"]
    assert all(r["decision"] is None for r in rows)
    assert summary["valid_samples"] == 0 and all(not g["mode_counts"] for g in summary["by_stage"])
    assert rows[0]["raw_output"]


def mock_transport(monkeypatch, statuses=(200,), finish="stop"):
    requests = []
    codes = iter(statuses)
    def handle(request):
        requests.append(request)
        code = next(codes)
        if code != 200:
            return httpx.Response(code, json={"error": {"message": "sensitive provider body"}})
        return httpx.Response(200, json={"id": "mock", "created": 1, "object": "chat.completion",
            "model": "provider-returned-alias", "choices": [{"index": 0, "finish_reason": finish,
                "message": {"role": "assistant", "content": json.dumps(decision()), "reasoning_content": "private reasoning"}}]})
    sdk = OpenAI(api_key="fictional-key", base_url="https://example.invalid/v1/", max_retries=0,
                 http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setenv("BOYU_API_KEY", "fictional-key")
    monkeypatch.setattr(transport, "OpenAI", lambda **kw: sdk)
    monkeypatch.setattr(transport.time, "sleep", lambda _: None)
    return ChatCompletionClient(ModelSettings("https://example.invalid/v1/", "requested-model", retries=1,
                                              response_format="json_object")), requests


def test_transport_keeps_alias_usage_and_attempts_but_not_reasoning_or_key(monkeypatch):
    client, requests = mock_transport(monkeypatch, (429, 200))
    try:
        response = client.complete(build_messages("fictional question", "fictional report"))
        assert len(requests) == len(response.attempts) == 2
        assert response.metadata["returned_model"] == "provider-returned-alias"
        assert "private reasoning" not in str(response) and "fictional-key" not in str(response)
        assert json.loads(requests[0].content)["response_format"] == {"type": "json_object"}
    finally:
        client.close()


def test_unsupported_json_parameter_does_not_silently_fallback_or_retry(monkeypatch):
    client, requests = mock_transport(monkeypatch, (400,))
    try:
        with pytest.raises(TransportFailure) as caught:
            client.complete(build_messages("fictional", "fictional"))
        assert len(requests) == len(caught.value.attempts) == 1
        assert "sensitive provider body" not in str(caught.value)
    finally:
        client.close()


def test_default_portable_format_does_not_assume_provider_json_capabilities(settings):
    assert settings.response_format == "prompt_only"
    assert "response_format" not in settings.request_parameters()


def test_no_cross_provider_credential_fallback(monkeypatch):
    monkeypatch.delenv("BOYU_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-credential")
    with pytest.raises(ValueError, match="BOYU_API_KEY"):
        ChatCompletionClient(ModelSettings("https://example.invalid/v1/", "fictional"))


def test_interrupted_attempt_is_journaled_without_a_clinical_mode(packets, settings, tmp_path):
    class InterruptedClient:
        def complete(self, messages, on_attempt=None):
            on_attempt([{"attempt": 1, "status": "STARTED"}])
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        runner.run_evaluation(packets, tmp_path, settings, allow_draft=True, execute=True, client=InterruptedClient())
    directory = next((tmp_path / "S001").iterdir())
    row = json.loads((directory / "results/stage_01_repeat_01.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    assert row["status"] == "INTERRUPTED" and row["decision"] is None
    assert manifest["status"] == "INTERRUPTED_OR_FAILED"
    assert summary["api_attempts"] == 1 and summary["valid_samples"] == 0


@pytest.mark.parametrize("url", ["http://example.invalid/v1/", "https://user:password@example.invalid/v1/",
                                  "https://example.invalid/v1/?api_key=secret"])
def test_credentials_cannot_hide_in_endpoint_url(url):
    with pytest.raises(ValueError):
        ModelSettings(url, "fictional-model")

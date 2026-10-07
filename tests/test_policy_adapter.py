"""Transport-only tests; no network or patient data."""

import json

import httpx
from openai import OpenAI
import pytest

from pancreatic_agent import policies
from pancreatic_agent.policies import GenerationError, OpenAICompatiblePolicy, build_messages
from pancreatic_agent.state_machine import LoggedEnvironment, StateMachine, Status
from test_state_machine import baseline, decision


def mocked_policy(monkeypatch, final_text, finish="stop", reasoning="do not persist this"):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,json={"id":"mock-only","created":1,"object":"chat.completion","model":"mock-model-alias","choices":[{"index":0,"finish_reason":finish,"message":{"role":"assistant","content":final_text,"reasoning_content":reasoning}}]})
    client=OpenAI(api_key="mock-not-a-real-key",base_url="https://apicz.boyuerichdata.com/v1/",http_client=httpx.Client(transport=httpx.MockTransport(handle)),max_retries=0)
    monkeypatch.setenv("BOYU_API_KEY","mock-not-a-real-key")
    monkeypatch.setattr(policies,"OpenAI",lambda **kwargs:client)
    return OpenAICompatiblePolicy(),requests


def test_preview_and_actual_request_have_identical_visible_state(monkeypatch):
    policy,requests=mocked_policy(monkeypatch,json.dumps(decision(action="CONTINUE_NO_NEW_STAGING")))
    state=baseline()
    try:
        machine=StateMachine(state,LoggedEnvironment([]))
        assert machine.run(policy)==Status.TERMINAL
        body=json.loads(requests[0].content)
        assert body["messages"]==build_messages(state.visible())
        assert body["model"]=="kimi-k3"
        trace=json.dumps(machine.trace)
        assert "do not persist this" not in trace
        assert "mock-not-a-real-key" not in trace
    finally:policy.close()


def test_truncated_final_output_is_retained_but_never_repaired(monkeypatch):
    policy,_=mocked_policy(monkeypatch,'{"partial":',finish="length")
    try:
        machine=StateMachine(baseline(),LoggedEnvironment([]))
        assert machine.run(policy)==Status.SYSTEM_FAILURE
        assert any(x.get("raw_output")=='{"partial":' for x in machine.trace)
        assert machine.terminal_output is None
    finally:policy.close()


def test_no_final_answer_does_not_use_reasoning_as_a_clinical_output(monkeypatch):
    policy,_=mocked_policy(monkeypatch,None)
    try:
        machine=StateMachine(baseline(),LoggedEnvironment([]))
        assert machine.run(policy)==Status.SYSTEM_FAILURE
        assert "do not persist this" not in json.dumps(machine.trace)
    finally:policy.close()


def test_failed_next_request_cannot_reuse_previous_response_metadata(monkeypatch):
    policy,_=mocked_policy(monkeypatch,json.dumps(decision(action="CONTINUE_NO_NEW_STAGING")))
    try:
        policy.decide(baseline().visible())
        assert policy.last_metadata["returned_model"]=="mock-model-alias"
        def fail(**kwargs):
            raise RuntimeError("synthetic transport failure")
        monkeypatch.setattr(policy.client.chat.completions,"create",fail)
        with pytest.raises(RuntimeError):
            policy.decide(baseline().visible())
        assert policy.last_metadata is None
    finally:policy.close()

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from eic.ai.budget import ChatConfigurationError, app_temperature, budget_conversation, message_units
from eic.ai.providers import ModelProviderError, chat_parameters, stream_chat_completion
from eic.models import AIModel


def model(provider="openai_compatible", profile="auto", **kwargs):
    return AIModel(id=1, tenant_id=1, provider=provider, model_type="chat",
                   model_name=kwargs.pop("model_name", "fixture"), endpoint="http://fixture.invalid/v1",
                   max_input_tokens=4096, max_output_tokens=123, chat_parameter_profile=profile, **kwargs)


@pytest.mark.parametrize("provider,profile,name,key,temp", [
    ("openai_compatible", "auto", "fixture", "max_tokens", 0.4),
    ("openai_compatible", "openai_completion", "deployment", "max_completion_tokens", 0.4),
    ("openai_compatible", "openai_reasoning", "deployment", "max_completion_tokens", None),
    ("openai_compatible", "auto", "o3-mini", "max_completion_tokens", None),
    ("openai_compatible", "auto", "gpt-5", "max_completion_tokens", None),
    ("azure", "openai_completion", "deployment", "max_completion_tokens", 0.4),
    ("zhipu", "auto", "fixture", "max_tokens", 0.4),
    ("ollama", "auto", "fixture", "options", 0.4),
    ("10", "ollama", "fixture", "options", 0.4),
])
def test_wire_parameters(provider, profile, name, key, temp):
    seen = []
    body = ('{"message":{"content":"OK"},"done":true}\n' if provider in {"10", "ollama"}
            else 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')
    async def run():
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, content=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert [c async for c in stream_chat_completion(model(provider, profile, model_name=name),
                    "Q", client=client, temperature=temp)] == ["OK"]
    asyncio.run(run())
    p = seen[0]
    if key == "options":
        assert p[key] == {"num_predict": 123, "num_ctx": 4219, "temperature": temp}
    else:
        assert p[key] == 123
        assert ("temperature" in p) == (temp is not None)
        if temp is not None:
            assert p["temperature"] == temp
        assert "enable_thinking" not in p and "chat_template_kwargs" not in p
    assert p["messages"] == [{"role":"user", "content":"Q"}]


@pytest.mark.parametrize("provider,profile,temperature,reason", [
    ("unknown", "auto", None, "unsupported_chat_provider"),
    ("unknown", "openai_legacy", None, "unsupported_chat_provider"),
    ("unknown", "openai_completion", None, "unsupported_chat_provider"),
    ("unknown", "openai_reasoning", None, "unsupported_chat_provider"),
    ("unknown", "ollama", None, "unsupported_chat_provider"),
    ("ollama", "openai_legacy", None, "provider_profile_mismatch"),
    ("openai", "ollama", None, "provider_profile_mismatch"),
    ("openai", "bad", None, "unsupported_parameter_profile"),
    ("openai", "openai_reasoning", 1.0, "temperature_not_supported"),
    ("openai", "auto", -0.1, "invalid_temperature"),
    ("openai", "auto", 2.1, "invalid_temperature"),
    ("openai", "auto", float("nan"), "invalid_temperature"),
])
def test_unsupported_has_zero_requests(provider, profile, temperature, reason):
    seen = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: seen.append(r) or httpx.Response(200))) as client:
            with pytest.raises(ModelProviderError) as e:
                _ = [c async for c in stream_chat_completion(model(provider, profile), "Q",
                                                             temperature=temperature, client=client)]
            assert e.value.reason == reason and not e.value.retryable
    asyncio.run(run())
    assert not seen


@pytest.mark.parametrize("value", [0, -1, 2_000_001, True, 1.5])
def test_bad_persisted_budget_rejected(value):
    m = model()
    m.max_output_tokens = value
    with pytest.raises(ChatConfigurationError, match="invalid_token_budget"):
        chat_parameters(m)


@pytest.mark.parametrize("value", [-1, 101, True, "50", float("nan")])
def test_bad_app_temperature(value):
    with pytest.raises(ChatConfigurationError):
        app_temperature({"temperature":value})


def test_app_temperature_scale_and_default():
    assert app_temperature({}) is None
    assert [app_temperature({"temperature":n}) for n in (0, 50, 100)] == [0, 1, 2]


def test_budget_boundary_and_chinese_and_whole_turn_trim():
    system = [{"role":"system", "content":"政策"}]
    fixed = system + [{"role":"user", "content":"中文问题"}]
    limit = message_units(fixed)
    history = [{"role":"user", "content":"old"}, {"role":"assistant", "content":"OLD"},
               {"role":"user", "content":"incomplete"}]
    result, metadata = budget_conversation(system, history, "中文问题", limit)
    assert result == fixed and metadata["droppedHistoryMessages"] == 3
    assert metadata["exactTokenizer"] is False
    with pytest.raises(ChatConfigurationError, match="input_budget_exceeded"):
        budget_conversation(system, [], "中文问题", limit - 1)


@pytest.mark.parametrize("body,provider,reason", [
    ('data: {"choices":[{"delta":{"content":"PART"}}]}\n\n', "openai", "provider_outcome_uncertain"),
    ('data: {"choices":[{"delta":{"content":"PART"},"finish_reason":"length"}]}\n\n', "openai", "output_budget_exceeded"),
    ('data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n', "openai", "unsupported_model_finish"),
    ('data: {"error":{"message":"SECRET"}}\n\n', "openai", "provider_error"),
    ('{"message":{"content":"PART"}}\n', "ollama", "provider_outcome_uncertain"),
    ('{"message":{"content":"PART"},"done":true,"done_reason":"length"}\n', "ollama", "output_budget_exceeded"),
])
def test_not_success_without_complete_answer(body, provider, reason):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200,content=body))) as client:
            with pytest.raises(ModelProviderError) as e:
                _ = [c async for c in stream_chat_completion(model(provider), "Q", client=client)]
            assert e.value.reason == reason and not e.value.retryable
            assert "SECRET" not in str(e.value)
    asyncio.run(run())


def test_ambiguous_transport_and_unsupported_parameters_do_not_retry():
    async def run():
        for status, reason in ((400, "provider_parameters_rejected"), (422, "provider_parameters_rejected")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda r: httpx.Response(status, content="SECRET"))) as client:
                with pytest.raises(ModelProviderError) as e:
                    _ = [c async for c in stream_chat_completion(model(), "Q", client=client)]
                assert e.value.reason == reason and not e.value.retryable
        def timeout(request):
            raise httpx.ReadTimeout("SECRET", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(ModelProviderError) as e:
                _ = [c async for c in stream_chat_completion(model(), "Q", client=client)]
            assert e.value.reason == "provider_outcome_uncertain" and not e.value.retryable
    asyncio.run(run())


@pytest.mark.parametrize("key,value", [("answerTokens",0),("maxAskPromptSize",-1),("answerTokens",True),
                                     ("answerTokens",1.5),
                                     ("chatParameterProfile","bad")])
def test_model_input_rejects_invalid_config(key,value):
    from eic.main import AIModelInput
    with pytest.raises(ValidationError):
        AIModelInput(**{key:value})


@pytest.mark.parametrize("provider,profile,key", [
    ("openai","openai_reasoning","max_completion_tokens"),
    ("openai","openai_legacy","max_tokens"),
    ("ollama","ollama","options"),
])
def test_probe_uses_same_parameter_profile_and_configured_output_budget(provider,profile,key):
    from eic.ai.providers import probe_chat_payload
    payload=probe_chat_payload(model(provider,profile))
    assert payload["stream"] is False
    assert payload[key]["num_predict"] == 123 if key=="options" else payload[key] == 123
    assert "max_tokens" not in payload if profile=="openai_reasoning" else True

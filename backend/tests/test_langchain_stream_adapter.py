"""ChatOpenAI EIC 契约适配的 MockTransport 覆盖。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest

os.environ["EIC_SECRET_ENCRYPTION_KEY"] = "test-encryption-secret-with-at-least-thirty-two-characters"

from eic.ai.langchain_stream import stream_chat_completion_langchain
from eic.ai.providers import ModelProviderError, stream_chat_completion
from eic.core.security import encrypt_secret
from eic.models import AIModel


def make_model(endpoint: str = "https://mock-openai.invalid/v1") -> AIModel:
    return AIModel(
        id=9001,
        tenant_id=17,
        provider="openai_compatible",
        model_type="chat",
        endpoint=endpoint,
        model_name="langchain-stream-fixture",
        secret_encrypted=encrypt_secret("fixture-only-key"),
        max_output_tokens=128,
    )


def event(delta: dict | None = None, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "langchain-stream-fixture",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }


def sse(item: dict) -> bytes:
    return ("data: " + json.dumps(item, separators=(",", ":")) + "\n\n").encode()


def response_body(*events: dict, done: bool = True) -> bytes:
    body = b"".join(sse(item) for item in events)
    return body + (b"data: [DONE]\n\n" if done else b"")


async def collect(
    body: bytes,
    *,
    endpoint: str = "https://mock-openai.invalid/v1",
    tools: list[dict] | None = None,
    tool_choice: object | None = None,
    tool_call_sink: list | None = None,
    usage_sink: dict | None = None,
) -> tuple[list[str], int, dict]:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}, request=request
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        chunks = [
            item async for item in stream_chat_completion_langchain(
                make_model(endpoint),
                "你好",
                client=client,
                tools=tools,
                tool_choice=tool_choice,
                tool_call_sink=tool_call_sink,
                usage_sink=usage_sink,
            )
        ]
    request = requests[0] if requests else None
    payload = json.loads(request.content) if request is not None else {}
    if request is not None:
        payload["_request_url"] = str(request.url)
    return chunks, len(requests), payload


def test_stop_stream_keeps_text_and_chat_completions_request_shape() -> None:
    body = response_body(event({"content": "你好"}), event({}, "stop"))
    chunks, count, payload = asyncio.run(collect(body, endpoint="https://mock-openai.invalid/v1/chat/completions"))
    assert "".join(chunks) == "你好"
    assert count == 1
    assert payload["model"] == "langchain-stream-fixture"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["_request_url"] == "https://mock-openai.invalid/v1/chat/completions"


def test_auto_tool_choice_without_sink_accepts_ordinary_stop_answer() -> None:
    tool_schema = [{
        "type": "function",
        "function": {"name": "weather", "parameters": {"type": "object"}},
    }]
    body = response_body(event({"content": "ordinary answer"}), event({}, "stop"))
    chunks, count, _ = asyncio.run(collect(
        body, tools=tool_schema, tool_choice="auto"
    ))
    assert "".join(chunks) == "ordinary answer"
    assert count == 1


def test_stop_finish_is_enough_when_clean_eof_omits_done_sentinel() -> None:
    body = response_body(event({"content": "complete"}), event({}, "stop"), done=False)
    chunks, count, _ = asyncio.run(collect(body))
    assert "".join(chunks) == "complete"
    assert count == 1


def test_visible_chunk_is_yielded_before_terminal_or_eof() -> None:
    async def run() -> None:
        class GatedStream(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self.release_terminal = asyncio.Event()
                self.terminal_requested = asyncio.Event()

            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield sse(event({"content": "incremental"}))
                self.terminal_requested.set()
                await self.release_terminal.wait()
                yield sse(event({}, "stop"))
                yield b"data: [DONE]\n\n"

            async def aclose(self) -> None:
                self.release_terminal.set()

        source = GatedStream()
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            await request.aread()
            return httpx.Response(
                200,
                stream=source,
                headers={"content-type": "text/event-stream"},
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            chunks = stream_chat_completion_langchain(make_model(), "你好", client=client)
            try:
                first = await asyncio.wait_for(anext(chunks), timeout=2)
                assert first == "incremental"
                assert not source.terminal_requested.is_set()
            finally:
                source.release_terminal.set()
            assert [chunk async for chunk in chunks] == []
        assert len(requests) == 1

    asyncio.run(run())


def test_content_after_stop_is_uncertain_and_not_yielded() -> None:
    body = response_body(
        event({"content": "safe prefix"}),
        event({}, "stop"),
        event({"content": "illegal tail"}),
    )
    visible: list[str] = []

    async def run() -> None:
        async with _client_for(body) as client:
            async for text in stream_chat_completion_langchain(
                make_model(), "你好", client=client
            ):
                visible.append(text)

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())

    assert caught.value.reason == "provider_outcome_uncertain"
    assert caught.value.retryable is False
    assert "".join(visible) == "safe prefix"
    assert "illegal tail" not in "".join(visible)


def test_stream_error_after_stop_is_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    source_state: dict[str, bool] = {}

    def chunk(content: str = "", finish_reason: str | None = None) -> SimpleNamespace:
        metadata = {"finish_reason": finish_reason} if finish_reason else {}
        return SimpleNamespace(
            content=content,
            response_metadata=metadata,
            generation_info={},
            tool_call_chunks=[],
            additional_kwargs={},
        )

    class FakeChat:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            source_state["constructed"] = True

        def astream(self, _messages: list[object]) -> AsyncIterator[SimpleNamespace]:
            source_state["astream_called"] = True

            async def generate() -> AsyncIterator[SimpleNamespace]:
                yield chunk("safe prefix")
                source_state["content"] = True
                yield chunk(finish_reason="stop")
                source_state["terminal"] = True
                raise ModelProviderError(
                    "late stream failure",
                    reason="provider_permission_denied",
                    retryable=False,
                )

            return generate()

    monkeypatch.setattr("eic.ai.langchain_stream._EICChatOpenAI", FakeChat)

    async def run() -> None:
        async for _ in stream_chat_completion_langchain(make_model(), "你好"):
            pass

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())

    assert caught.value.reason == "provider_outcome_uncertain"
    assert caught.value.retryable is False
    assert source_state == {
        "constructed": True,
        "astream_called": True,
        "content": True,
        "terminal": True,
    }, (caught.value, source_state)


@pytest.mark.parametrize("done", [True, False])
def test_missing_finish_reason_is_uncertain_with_or_without_done(done: bool) -> None:
    body = response_body(event({"content": "partial"}), done=done)
    sink: dict = {}
    visible: list[str] = []

    async def run() -> None:
        async with _client_for(body) as client:
            async for _ in stream_chat_completion_langchain(
                make_model(), "你好", client=client, usage_sink=sink
            ):
                visible.append(_)

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())
    assert caught.value.reason == "provider_outcome_uncertain"
    assert caught.value.retryable is False
    assert "".join(visible) == "partial"


def _client_for(body: bytes) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        )
    )


def test_length_finish_maps_to_output_budget_exceeded() -> None:
    body = response_body(event({"content": "partial"}), event({}, "length"))
    visible: list[str] = []

    async def run() -> None:
        async with _client_for(body) as client:
            async for text in stream_chat_completion_langchain(
                make_model(), "你好", client=client
            ):
                visible.append(text)

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())
    assert caught.value.reason == "output_budget_exceeded"
    assert "".join(visible) == "partial"


def test_tool_fragments_commit_only_after_tool_calls_terminal() -> None:
    tool_schema = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
    body = response_body(
        event({"tool_calls": [{
            "index": 0,
            "id": "call-weather-1",
            "type": "function",
            "function": {"name": "get_", "arguments": '{"city":'},
        }]}),
        event({"tool_calls": [{
            "index": 0,
            "function": {"name": "weather", "arguments": '"Oslo"}'},
        }]}),
        event({}, "tool_calls"),
    )
    sink: list[dict] = []
    chunks, count, _ = asyncio.run(collect(
        body, tools=tool_schema, tool_choice="auto", tool_call_sink=sink
    ))
    assert chunks == []
    assert count == 1
    assert sink == [{
        "id": "call-weather-1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"Oslo"}'},
    }]


def test_named_tool_choice_rejects_a_different_declared_tool() -> None:
    tools = [
        {"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},
    ]
    body = response_body(
        event({"tool_calls": [{
            "index": 0,
            "id": "call-lookup",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }]}),
        event({}, "tool_calls"),
    )
    sink: list[dict] = []

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(collect(
            body, tools=tools, tool_choice="weather", tool_call_sink=sink
        ))

    assert caught.value.reason == "provider_outcome_uncertain"
    assert sink == []


@pytest.mark.parametrize(
    "tool_delta",
    [
        {"index": 0, "function": {"name": "weather", "arguments": "{}"}},
        {"index": 0, "id": "call-invalid", "function": {"name": "weather", "arguments": "{"}},
    ],
)
def test_incomplete_tool_call_never_reaches_sink(tool_delta: dict) -> None:
    tool_schema = [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}]
    body = response_body(event({"tool_calls": [tool_delta]}), event({}, "tool_calls"))
    sink: list[dict] = []

    async def run() -> None:
        async with _client_for(body) as client:
            async for _ in stream_chat_completion_langchain(
                make_model(), "你好", client=client, tools=tool_schema, tool_call_sink=sink
            ):
                pass

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())
    assert caught.value.reason == "provider_outcome_uncertain"
    assert sink == []


def test_tool_fragments_without_tool_terminal_never_reach_sink() -> None:
    tool_schema = [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}]
    body = response_body(event({"tool_calls": [{
        "index": 0,
        "id": "call-weather-1",
        "function": {"name": "weather", "arguments": "{}"},
    }]}))
    sink: list[dict] = []

    async def run() -> None:
        async with _client_for(body) as client:
            async for _ in stream_chat_completion_langchain(
                make_model(), "你好", client=client, tools=tool_schema, tool_call_sink=sink
            ):
                pass

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())
    assert caught.value.reason == "provider_outcome_uncertain"
    assert sink == []


def test_unknown_finish_reason_is_uncertain_and_does_not_commit_tools() -> None:
    body = response_body(event({"content": "filtered"}), event({}, "content_filter"))
    sink: list[dict] = []

    async def run() -> None:
        async with _client_for(body) as client:
            async for _ in stream_chat_completion_langchain(
                make_model(), "你好", client=client, tool_call_sink=sink
            ):
                pass

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())
    assert caught.value.reason == "provider_outcome_uncertain"
    assert sink == []


def test_usage_absent_and_partial_are_not_filled_from_sdk_derived_metadata() -> None:
    absent_body = response_body(event({"content": "no usage"}), event({}, "stop"))
    absent: dict = {}
    chunks, count, _ = asyncio.run(collect(absent_body, usage_sink=absent))
    assert "".join(chunks) == "no usage"
    assert count == 1
    assert "input_tokens" not in absent
    assert "output_tokens" not in absent
    assert "total_tokens" not in absent

    partial_body = response_body(
        event({"content": "partial usage"}),
        event({}, "stop"),
        {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
         "model": "langchain-stream-fixture", "choices": [], "usage": {"prompt_tokens": 13}},
    )
    partial: dict = {}
    chunks, count, _ = asyncio.run(collect(partial_body, usage_sink=partial))
    assert "".join(chunks) == "partial usage"
    assert count == 1
    assert partial == {"input_tokens": 13, "usage_source": "provider_usage"}

    empty_body = response_body(
        event({"content": "empty usage"}),
        event({}, "stop"),
        {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
         "model": "langchain-stream-fixture", "choices": [], "usage": {}},
    )
    empty: dict = {}
    chunks, count, _ = asyncio.run(collect(empty_body, usage_sink=empty))
    assert "".join(chunks) == "empty usage"
    assert count == 1
    assert empty == {}


def test_current_provider_entrypoint_remains_unrouted_to_adapter() -> None:
    assert stream_chat_completion.__module__ == "eic.ai.providers"
    assert stream_chat_completion_langchain.__module__ == "eic.ai.langchain_stream"

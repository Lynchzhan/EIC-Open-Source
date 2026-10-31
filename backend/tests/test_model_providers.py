import asyncio
import os

import httpx

os.environ["EIC_SECRET_ENCRYPTION_KEY"] = "test-encryption-secret-with-at-least-thirty-two-characters"

from eic.ai.providers import stream_chat_completion
from eic.core.security import encrypt_secret
from eic.models import AIModel


async def collect_chunks(model: AIModel, response_body: str, content_type: str) -> list[str]:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=response_body, headers={"content-type": content_type})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        return [chunk async for chunk in stream_chat_completion(model, "你好", client=client)]


def make_model(provider: str) -> AIModel:
    return AIModel(
        id=1,
        tenant_id=1,
        provider=provider,
        model_type="chat",
        endpoint="http://model.example/v1",
        model_name="demo-model",
        secret_encrypted=encrypt_secret("secret-never-in-response"),
    )


def test_openai_compatible_sse_is_normalized_to_text_chunks() -> None:
    body = 'data: {"choices":[{"delta":{"content":"你"}}]}\n\ndata: {"choices":[{"delta":{"content":"好"}}]}\n\ndata: [DONE]\n\n'
    assert asyncio.run(collect_chunks(make_model("openai_compatible"), body, "text/event-stream")) == ["你", "好"]


def test_ollama_ndjson_is_normalized_to_text_chunks() -> None:
    body = '{"message":{"content":"企智"}}\n{"message":{"content":"中心"},"done":true}\n'
    assert asyncio.run(collect_chunks(make_model("ollama"), body, "application/x-ndjson")) == ["企智", "中心"]


def test_reasoning_content_is_not_treated_as_answer_text() -> None:
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"思考","content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        'data: [DONE]\n\n'
    )
    assert asyncio.run(collect_chunks(make_model("openai_compatible"), body, "text/event-stream")) == ["你", "好"]


def test_dashscope_output_choices_content() -> None:
    body = 'data: {"output":{"choices":[{"message":{"content":"枢"}}]}}\n\ndata: [DONE]\n\n'
    assert asyncio.run(collect_chunks(make_model("openai_compatible"), body, "text/event-stream")) == ["枢"]


def test_reasoning_only_chunks_are_collected_in_sink() -> None:
    body = 'data: {"choices":[{"delta":{"reasoning_content":"先想"}}]}\n\ndata: [DONE]\n\n'
    sink: list[str] = []

    async def collect() -> list[str]:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return [
                chunk
                async for chunk in stream_chat_completion(
                    make_model("openai_compatible"), "你好", client=client, reasoning_sink=sink
                )
            ]

    assert asyncio.run(collect()) == []
    assert sink == ["先想"]

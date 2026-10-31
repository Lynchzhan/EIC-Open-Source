import asyncio
import json
import os

import httpx

os.environ["EIC_SECRET_ENCRYPTION_KEY"] = "test-encryption-secret-with-at-least-thirty-two-characters"

from eic.ai.embeddings import embed_texts
from eic.ai.providers import ModelProviderError
from eic.ai.rerank import rerank_hits
from eic.core.security import encrypt_secret
from eic.models import AIModel
from eic.vector.qdrant_reader import VectorHit


def test_dashscope_rerank_preserves_chunk_identity_and_sorts_scores() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"output": {"results": [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.34}]}},
        )

    async def run() -> list[VectorHit]:
        model = AIModel(
            id=1, tenant_id=1, provider="dashscope", model_type="rerank",
            endpoint="https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
            model_name="gte-rerank-v2", secret_encrypted=encrypt_secret("rerank-secret"),
        )
        hits = [VectorHit("chunk-a", 0.1, {"content": "采购流程"}), VectorHit("chunk-b", 0.2, {"content": "预算审批"})]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await rerank_hits(model, "预算如何审批", hits, client=client)

    result = asyncio.run(run())
    assert [item.point_id for item in result] == ["chunk-b", "chunk-a"]
    assert [item.score for item in result] == [0.91, 0.34]
    assert captured["url"] == "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["input"]["documents"] == ["采购流程", "预算审批"]
    assert captured["authorization"] == "Bearer rerank-secret"


def test_embedding_dimension_is_enforced_before_pgvector_write() -> None:
    async def run() -> None:
        model = AIModel(
            id=1, tenant_id=1, provider="openai_compatible", model_type="embedding",
            endpoint="http://model.example/v1", model_name="text-embedding-v4",
            embedding_dimensions=1024, secret_encrypted=encrypt_secret("embedding-secret"), enabled=True,
        )
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]}))
        async with httpx.AsyncClient(transport=transport) as client:
            try:
                await embed_texts(model, ["hello"], client=client)
            except ModelProviderError as exc:
                assert "Embedding provider request failed" in str(exc)
            else:
                raise AssertionError("embedding dimension mismatch was accepted")

    asyncio.run(run())


def test_openai_compatible_rerank_uses_query_documents_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.88}, {"index": 1, "relevance_score": 0.12}]})

    async def run() -> list[VectorHit]:
        model = AIModel(
            id=2, tenant_id=1, provider="openai_compatible", model_type="rerank",
            endpoint="http://model.example/v1", model_name="rerank-plus",
            secret_encrypted=encrypt_secret("openai-rerank-secret"),
        )
        hits = [VectorHit("chunk-a", 0.1, {"content": "制度"}), VectorHit("chunk-b", 0.2, {"content": "预算"})]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await rerank_hits(model, "制度条款", hits, client=client)

    result = asyncio.run(run())
    assert captured["url"] == "http://model.example/v1/rerank"
    assert captured["body"]["query"] == "制度条款"
    assert captured["body"]["documents"] == ["制度", "预算"]
    assert [item.point_id for item in result] == ["chunk-a", "chunk-b"]

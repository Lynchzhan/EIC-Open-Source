import asyncio
import httpx
import pytest

from eic.ai.providers import ModelProviderError
from eic.ai.rerank import rerank_hits
from eic.models import AIModel
from eic.vector.qdrant_reader import VectorHit


@pytest.mark.parametrize("body", [
    [], {}, {"results": []}, {"results": "wrong"},
    {"results": [{"index": 0, "score": .9}]},
    {"results": [{"index": 0, "score": .9}, {"index": 0, "score": .8}]},
    {"results": [{"index": True, "score": .9}, {"index": 0, "score": .8}]},
    {"results": [{"index": 9, "score": .9}, {"index": 0, "score": .8}]},
    {"results": [{"index": 1, "score": float("nan")}, {"index": 0, "score": .8}]},
    {"results": [{"index": 1, "score": float("inf")}, {"index": 0, "score": .8}]},
    {"results": [{"index": 1, "score": -.1}, {"index": 0, "score": .8}]},
    {"results": [{"index": 1, "score": 1.1}, {"index": 0, "score": .8}]},
    {"results": [{"index": 1, "score": "0.9"}, {"index": 0, "score": .8}]},
    {"results": [{"index": 1, "score": True}, {"index": 0, "score": .8}]},
    {"results": [{"index": 1, "score": .9}, None]},
    {"output": {"results": [{"index": 0, "score": .9}, {"index": 1, "score": .8}]}},
])
def test_malformed_protocol_fails_whole_batch(body):
    async def run():
        # NaN/inf 以原始 JSON 文本送入，避免 httpx 的 JSON 编码器先行拒绝。
        import json
        model = AIModel(provider="openai", model_type="rerank", endpoint="http://fixture.invalid",
                        model_name="fixture", enabled=True)
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(body).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ModelProviderError):
                await rerank_hits(model, "q", [VectorHit("a", 0., {"content": "a"}), VectorHit("b", 0., {"content": "b"})],
                                  limit=2, client=client)
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["dashscope", "dashscope_rerank", "aliyun_rerank", "gte-rerank-v2"])
def test_native_protocol_provider_aliases(provider):
    async def run():
        model = AIModel(provider=provider, model_type="rerank", endpoint="http://fixture.invalid/native",
                        model_name="fixture", enabled=True)
        def handler(request):
            import json
            assert "input" in json.loads(request.content)
            return httpx.Response(200, json={"output": {"results": [{"index": 0, "relevance_score": 1.}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await rerank_hits(model, "q", [VectorHit("a", 0., {"content": "a", "fusionScore": .01})], client=client)
        assert result[0].point_id == "a" and result[0].payload["fusionScore"] == .01
    asyncio.run(run())

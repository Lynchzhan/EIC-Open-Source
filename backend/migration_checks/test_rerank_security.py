import asyncio
import json
import traceback
import uuid

import httpx
import pytest
from langchain_core.documents import Document

from eic.ai.providers import ModelProviderError
from eic.ai.rerank import EICDocumentReranker, rerank_hits
from eic.models import AIModel
from eic.vector.qdrant_reader import VectorHit


def model(endpoint="https://fixture.invalid/v1?token=value", provider="openai"):
    return AIModel(model_type="rerank", provider=provider, endpoint=endpoint,
                   model_name="fixture", enabled=True)


HITS = [VectorHit("a", 0.0, {"content": "a", "fusionScore": 0.1})]


@pytest.mark.parametrize("secret", [None, "", "explicit"])
def test_borrowed_credentials_are_isolated(secret):
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"results": [{"index": 0, "score": 0.9}]})
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), auth=("borrowed", "secret"),
            cookies={"session": "borrowed"}, headers={"X-Api-Key": "borrowed"},
            follow_redirects=True,
        ) as client:
            result = await rerank_hits(model(), "q", HITS, client=client, secret_override=secret)
            assert result[0].point_id == "a"
            assert not client.is_closed
            assert client.auth is not None and client.cookies["session"] == "borrowed"
        request = requests[0]
        assert request.url.path == "/v1/rerank" and request.url.query == b"token=value"
        assert "cookie" not in request.headers and "x-api-key" not in request.headers
        assert request.headers.get("authorization") == ("Bearer explicit" if secret else None)
    asyncio.run(run())


@pytest.mark.parametrize("endpoint", [
    "https://fixture.invalid/dashscope.aliyuncs.com?x=y",
    "https://fixture.invalid/v1?host=dashscope.aliyuncs.com",
    "https://dashscope.aliyuncs.com.evil.invalid/api/v1",
])
def test_dashscope_substrings_do_not_switch_protocol(endpoint):
    async def run():
        def handler(request):
            assert "input" not in json.loads(request.content)
            return httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await rerank_hits(model(endpoint), "q", HITS, client=client)
    asyncio.run(run())


def test_exact_host_query_and_compressor_contract():
    async def run():
        def handler(request):
            assert request.url.path == "/api/v1/services/rerank/text-rerank/text-rerank"
            assert request.url.query == b"x=%2F&x=2"
            assert "input" in json.loads(request.content)
            return httpx.Response(200, json={"output": {"results": [{"index": 0, "score": 0.8}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            doc = Document(id="source", page_content="a", metadata={"tenant": 7})
            adapter = EICDocumentReranker(model=model("https://dashscope.aliyuncs.com/api/v1?x=%2F&x=2"), client=client)
            result = await adapter.acompress_documents([doc], "q")
            assert result[0].id == "source" and result[0].metadata["tenant"] == 7
            assert "rerankScore" not in doc.metadata
    asyncio.run(run())


@pytest.mark.parametrize("endpoint", [
    "", "/relative", "ftp://fixture.invalid", "https://user:password@fixture.invalid",
    "https://fixture.invalid/#fragment", "https://fixture.invalid/#",
    "https://fixture.invalid:bad", "https://[bad", "https://fixture.invalid/ bad",
    "https://fixture.invalid/\npath",
])
def test_bad_endpoint_has_no_request(endpoint):
    async def run():
        def handler(request):
            pytest.fail("invalid endpoint reached transport")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelProviderError):
                await rerank_hits(model(endpoint), "q", HITS, client=client)
    asyncio.run(run())


def test_redirect_is_not_followed_and_error_is_sanitized():
    async def run():
        requests = []
        secret = uuid.uuid4().hex
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://evil.invalid/secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with pytest.raises(ModelProviderError) as exc:
                await rerank_hits(model(), "q", HITS, client=client, secret_override=secret)
            assert exc.value.__cause__ is None
            assert secret not in "".join(traceback.format_exception(exc.value))
            assert len(requests) == 1 and not client.is_closed
    asyncio.run(run())


def test_cancel_propagates_without_closing_borrowed():
    async def run():
        async def handler(request):
            raise asyncio.CancelledError()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(asyncio.CancelledError):
                await rerank_hits(model(), "q", HITS, client=client)
            assert not client.is_closed
    asyncio.run(run())


def test_cleanup_failure_is_not_success(monkeypatch):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})
        )) as borrowed:
            original = httpx.AsyncClient.aclose
            async def close(client):
                if client is borrowed:
                    return await original(client)
                await original(client)
                raise RuntimeError("sensitive cleanup")
            monkeypatch.setattr(httpx.AsyncClient, "aclose", close)
            with pytest.raises(ModelProviderError) as exc:
                await rerank_hits(model(), "q", HITS, client=borrowed)
            assert exc.value.__cause__ is None and not borrowed.is_closed
    asyncio.run(run())


def test_owned_client_environment_and_close(monkeypatch):
    async def run():
        original = httpx.AsyncClient
        clients = []
        def factory(**kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            kwargs["transport"] = httpx.MockTransport(
                lambda r: httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})
            )
            client = original(**kwargs)
            clients.append(client)
            return client
        monkeypatch.setattr(httpx, "AsyncClient", factory)
        await rerank_hits(model(), "q", HITS)
        assert clients[0].is_closed
    asyncio.run(run())


def test_transport_exception_does_not_expose_cause():
    async def run():
        def handler(request):
            raise RuntimeError("transport credential must remain hidden")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelProviderError) as exc:
                await rerank_hits(model(), "q", HITS, client=client)
            assert exc.value.__cause__ is None and exc.value.__suppress_context__
            assert "transport credential" not in "".join(traceback.format_exception(exc.value))
    asyncio.run(run())


def test_cleanup_cancel_is_not_success(monkeypatch):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})
        )) as borrowed:
            original = httpx.AsyncClient.aclose
            async def close(client):
                if client is borrowed:
                    return await original(client)
                await original(client)
                raise asyncio.CancelledError()
            monkeypatch.setattr(httpx.AsyncClient, "aclose", close)
            with pytest.raises(asyncio.CancelledError):
                await rerank_hits(model(), "q", HITS, client=borrowed)
            assert not borrowed.is_closed
    asyncio.run(run())


@pytest.mark.parametrize("borrowed_mode", [False, True])
@pytest.mark.parametrize("cancel_count", [1, 2])
def test_cancel_while_cleanup_waits(monkeypatch, borrowed_mode, cancel_count):
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        counts = {"transport": 0, "facade": 0}
        facades = []

        class TrackingTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.mock = httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json={"results": [{"index": 0, "score": 0.9}]}
                    )
                )

            async def handle_async_request(self, request):
                return await self.mock.handle_async_request(request)

            async def aclose(self):
                if not borrowed_mode:
                    entered.set()
                    await release.wait()
                counts["transport"] += 1
                await self.mock.aclose()
                if not borrowed_mode:
                    finished.set()

        transport = TrackingTransport()
        original_init = httpx.AsyncClient.__init__
        original_close = httpx.AsyncClient.aclose
        borrowed = httpx.AsyncClient(transport=transport) if borrowed_mode else None

        def init(client, **kwargs):
            if not borrowed_mode:
                kwargs["transport"] = transport
            original_init(client, **kwargs)
            facades.append(client)

        async def close(client):
            if borrowed_mode:
                entered.set()
                await release.wait()
            await original_close(client)
            counts["facade"] += 1
            if borrowed_mode:
                finished.set()

        monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
        monkeypatch.setattr(httpx.AsyncClient, "aclose", close)
        task = asyncio.create_task(rerank_hits(model(), "q", HITS, client=borrowed))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set(), "cleanup interrupted before resource release"
            assert counts["facade"] == 1 and facades[0].is_closed
            assert counts["transport"] == (0 if borrowed_mode else 1)
            if borrowed is not None:
                assert not borrowed.is_closed
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if borrowed is not None:
                await original_close(borrowed)
    asyncio.run(run())


def test_closed_borrowed_is_rejected_without_transport_request():
    async def run():
        requests = []
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(
                200, json={"results": [{"index": 0, "score": 1.0}]}
            )
        ))
        await client.aclose()
        with pytest.raises(ModelProviderError) as exc:
            await rerank_hits(model(), "q", HITS, client=client)
        assert not requests and client.is_closed
        assert exc.value.__cause__ is None
    asyncio.run(run())

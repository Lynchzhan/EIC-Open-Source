import asyncio
import json
import os

import httpx
import pytest
from mcp import types
from sdk_mcp_fixture import StandardServer
from langchain_core.documents import Document

os.environ["EIC_SECRET_ENCRYPTION_KEY"] = (
    "test-encryption-secret-with-at-least-thirty-two-characters"
)

from eic.ai.embeddings import EICEmbeddings
from eic.ai.mcp_client import MCP_PROTOCOL_VERSION, HttpMcpClient, McpClientError
from eic.ai.providers import ModelProviderError
from eic.ai.rerank import EICDocumentReranker
from eic.knowledge.documents import to_langchain_document
from eic.knowledge.retrieval import EICRetriever
from eic.models import AIModel
from eic.vector.qdrant_reader import VectorHit

PROVIDER_MOCK_REQUESTS = []
MCP_MOCK_REQUESTS = []


def mock_transport(handler, *, service, requests=None):
    if requests is None:
        requests = PROVIDER_MOCK_REQUESTS if service == "provider" else MCP_MOCK_REQUESTS

    def counted_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.MockTransport(counted_handler)


def embedding_model(**overrides) -> AIModel:
    values = {
        "id": 71,
        "tenant_id": 7,
        "provider": "openai_compatible",
        "model_type": "embedding",
        "endpoint": "https://provider.fixture/v1",
        "model_name": "embedding-fixture",
        "embedding_dimensions": 1024,
        "enabled": True,
    }
    values.update(overrides)
    return AIModel(**values)


def rerank_model(**overrides) -> AIModel:
    values = {
        "id": 72,
        "tenant_id": 7,
        "provider": "openai_compatible",
        "model_type": "rerank",
        "endpoint": "https://provider.fixture/v1",
        "model_name": "rerank-fixture",
        "enabled": True,
    }
    values.update(overrides)
    return AIModel(**values)


def rpc_response(request_id, result=None, *, error=None, headers=None) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": request_id}
    body["error" if error is not None else "result"] = error if error is not None else result
    return httpx.Response(200, json=body, headers=headers)


def fixed_json_handler(body):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return handler


def test_document_metadata_keeps_trusted_scope_and_citations() -> None:
    document = to_langchain_document(
        "page text",
        tenant_id=7,
        knowledge_base_id=17,
        document_id=27,
        chunk_id="chunk-1",
        source_name="policy.pdf",
        page_numbers=[2, 1, 2],
        parent_chunk_id="parent-1",
        metadata={
            "tenant_id": 99,
            "knowledge_base_id": 98,
            "tenantId": 97,
            "knowledgeBaseId": 96,
            "parent_chunk_id": "untrusted-parent",
            "parentChunkId": "untrusted-parent-alias",
            "custom": "kept",
        },
    )

    assert isinstance(document, Document)
    assert document.page_content == "page text"
    assert document.metadata == {
        "tenant_id": 7,
        "knowledge_base_id": 17,
        "document_id": 27,
        "chunk_id": "chunk-1",
        "source_name": "policy.pdf",
        "pageNumbers": [2, 1],
        "parent_chunk_id": "parent-1",
        "custom": "kept",
    }
    with pytest.raises(ValueError, match="page numbers"):
        to_langchain_document(
            "x", tenant_id=7, knowledge_base_id=17, document_id=27,
            chunk_id="chunk-2", page_numbers=[True],
        )


def test_document_metadata_cannot_supply_parent_chunk_id() -> None:
    document = to_langchain_document(
        "page text",
        tenant_id=7,
        knowledge_base_id=17,
        document_id=27,
        chunk_id="chunk-2",
        metadata={
            "parent_chunk_id": "untrusted-parent",
            "parentChunkId": "untrusted-parent-alias",
        },
    )

    assert "parent_chunk_id" not in document.metadata
    assert "parentChunkId" not in document.metadata


def test_retriever_pushes_scope_into_search_and_drops_cross_scope_hits() -> None:
    calls = []

    def search(query, *, tenant_id, knowledge_base_id, limit):
        calls.append((query, tenant_id, knowledge_base_id, limit))
        return [
            VectorHit("inside", 0.9, {
                "content": "allowed", "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id, "document_id": 27,
                "source_name": "guide.md", "pageNumbers": [3],
            }),
            VectorHit("other-tenant", 0.8, {
                "content": "denied", "tenant_id": tenant_id + 1,
                "knowledge_base_id": knowledge_base_id, "document_id": 28,
            }),
            VectorHit("other-base", 0.7, {
                "content": "denied", "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id + 1, "document_id": 29,
            }),
            VectorHit("conflicting-scope", 0.6, {
                "content": "denied", "tenant_id": tenant_id,
                "tenantId": tenant_id + 1,
                "knowledge_base_id": knowledge_base_id, "document_id": 30,
            }),
        ]

    retriever = EICRetriever(search=search, tenant_id=7, knowledge_base_id=17, top_k=4)
    result = retriever.invoke("policy")

    assert calls == [("policy", 7, 17, 4)]
    assert [item.metadata["chunk_id"] for item in result] == ["inside"]
    assert result[0].metadata["tenant_id"] == 7
    assert result[0].metadata["knowledge_base_id"] == 17
    assert result[0].metadata["source_name"] == "guide.md"
    assert result[0].metadata["pageNumbers"] == [3]
    assert result[0].metadata["score"] == 0.9


def test_async_retriever_uses_same_presearch_scope() -> None:
    calls = []

    async def search(query, *, tenant_id, knowledge_base_id, limit):
        calls.append((tenant_id, knowledge_base_id, limit))
        return [VectorHit("async", 0.5, {
            "content": query, "tenant_id": tenant_id,
            "knowledge_base_id": knowledge_base_id, "document_id": 30,
        })]

    retriever = EICRetriever(search=search, tenant_id=8, knowledge_base_id=18)
    result = asyncio.run(retriever.ainvoke("async query"))
    assert calls == [(8, 18, 5)]
    assert result[0].page_content == "async query"


def test_embeddings_batch_order_dimension_and_provider_usage_contract() -> None:
    requests = []
    usage = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        data = [
            {"index": index, "embedding": [float(ord(text[0]))] * 1024}
            for index, text in enumerate(body["input"])
        ]
        return httpx.Response(200, json={
            "data": list(reversed(data)),
            "usage": {"prompt_tokens": len(data) * 3, "completion_tokens": 0},
        })

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="provider")) as client:
            adapter = EICEmbeddings(
                embedding_model(), tenant_id=7, batch_size=2, client=client,
                usage_callback=usage.append,
            )
            return await adapter.aembed_documents(["A-one", "B-two", "C-three", "D-four", "E-five"])

    vectors = asyncio.run(run())
    assert len(requests) == 3
    assert [len(item["input"]) for item in requests] == [2, 2, 1]
    assert [vector[0] for vector in vectors] == [65.0, 66.0, 67.0, 68.0, 69.0]
    assert all(len(vector) == 1024 for vector in vectors)
    assert [item["input_tokens"] for item in usage] == [6, 6, 3]
    assert all(item["output_tokens"] == 0 for item in usage)
    assert all("total_tokens" not in item for item in usage)
    assert all(item["usage_source"] == "provider_usage" for item in usage)


def test_embedding_dimension_mismatch_maps_to_model_provider_error() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="provider")) as client:
            adapter = EICEmbeddings(embedding_model(), client=client)
            with pytest.raises(ModelProviderError, match="Embedding provider request failed"):
                await adapter.aembed_documents(["short vector"])

    asyncio.run(run())
    assert calls == 1


def test_reranker_sorts_documents_and_preserves_identity_metadata() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "results": [
                {"index": 1, "relevance_score": 0.92},
                {"index": 0, "relevance_score": 0.21},
            ],
        })

    documents = [
        Document(page_content="first", metadata={"chunk_id": "a", "citation": "A"}),
        Document(page_content="second", metadata={"chunk_id": "b", "citation": "B"}),
    ]

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="provider")) as client:
            compressor = EICDocumentReranker(
                model=rerank_model(), top_n=2, client=client,
                secret_override="mock-rerank-key",
            )
            return await compressor.acompress_documents(documents, "second")

    result = asyncio.run(run())
    assert len(requests) == 1
    assert [item.page_content for item in result] == ["second", "first"]
    assert [item.metadata["chunk_id"] for item in result] == ["b", "a"]
    assert result[0].metadata["citation"] == "B"
    assert result[0].metadata["rerankScore"] == 0.92
    assert result[0].metadata["scoreSpace"] == "rerank"
    assert requests[0].headers["authorization"] == "Bearer mock-rerank-key"


def test_reranker_maps_http_errors_without_leaking_response_body() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="provider-secret-detail")

    document = Document(page_content="candidate", metadata={"chunk_id": "c"})

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="provider")) as client:
            compressor = EICDocumentReranker(model=rerank_model(), client=client)
            with pytest.raises(ModelProviderError, match="Rerank provider request failed") as error:
                await compressor.acompress_documents([document], "query")
            assert "provider-secret-detail" not in str(error.value)

    asyncio.run(run())
    assert calls == 1


def test_mcp_initialize_tools_call_auth_and_session_flow() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((body, request.headers))
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            return rpc_response(body['id'], {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mock-mcp", "version": "1"},
            }, headers={"Mcp-Session-Id": "session-test-1"})
        if body["method"] == "tools/list":
            return rpc_response(body['id'], {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]})
        return rpc_response(body['id'], {"content": [{"type": "text", "text": "fixture"}]})

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="mcp")) as client:
            mcp = HttpMcpClient({
                "transport": "http",
                "endpoint": "https://mcp.fixture/rpc",
                "headers": {"Authorization": "Bearer mock-mcp-key"},
            }, client=client)
            initialized = await mcp.initialize()
            tools = await mcp.list_tools()
            result = await mcp.call_tool("lookup", {"q": "fixture"})
            return mcp, initialized, tools, result

    mcp, initialized, tools, result = asyncio.run(run())
    assert mcp.initialized is True
    assert mcp.session_id == "session-test-1"
    assert initialized["serverInfo"]["name"] == "mock-mcp"
    assert tools["tools"][0]["name"] == "lookup"
    assert result["content"][0]["text"] == "fixture"
    assert [item[0].get("method") for item in requests] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    assert requests[0][0]["params"]["protocolVersion"] == types.LATEST_PROTOCOL_VERSION
    assert all(item[1]["authorization"] == "Bearer mock-mcp-key" for item in requests)
    assert requests[1][1]["mcp-session-id"] == "session-test-1"
    assert requests[2][1]["mcp-session-id"] == "session-test-1"
    assert requests[2][1]["mcp-protocol-version"] == MCP_PROTOCOL_VERSION
    assert requests[3][1]["mcp-session-id"] == "session-test-1"
    assert len(requests) == 4


def test_mcp_requires_initialization_for_tool_listing_without_request() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="mcp")) as client:
            with pytest.raises(McpClientError) as error:
                await HttpMcpClient({"transport": "http", "endpoint": "https://mcp.fixture/rpc"}, client=client).list_tools()
            assert error.value.code == "not_initialized"

    asyncio.run(run())
    assert calls == 0


@pytest.mark.parametrize("status,expected_code", [(401, "authentication_error"), (403, "authentication_error"), (503, "http_error")])
def test_mcp_http_failures_are_mapped(status, expected_code) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="mcp")) as client:
            mcp = HttpMcpClient({"transport": "http", "endpoint": "https://mcp.fixture/rpc"}, client=client)
            with pytest.raises(McpClientError) as error:
                await mcp.call_tool("lookup", {})
            assert error.value.code == expected_code
            assert error.value.retryable is False

    asyncio.run(run())
    assert calls == 1


@pytest.mark.parametrize(
    "status,expected_code,retryable",
    [
        (400, "http_error", False),
        (401, "authentication_error", False),
        (403, "authentication_error", False),
        (429, "http_error", True),
        (500, "http_error", True),
        (503, "http_error", True),
    ],
)
def test_mcp_initialized_notification_http_status_retryability(
    status, expected_code, retryable,
) -> None:
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "initialize":
            return rpc_response(payload['id'], {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            })
        return httpx.Response(status)

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="mcp")) as client:
            mcp = HttpMcpClient(
                {"transport": "http", "endpoint": "https://mcp.fixture/rpc"},
                client=client,
            )
            with pytest.raises(McpClientError) as error:
                await mcp.initialize()
            assert error.value.code == expected_code
            assert error.value.retryable is False
            assert mcp.initialized is False

    asyncio.run(run())
    assert methods == ["initialize", "notifications/initialized"]


def test_mcp_timeout_and_remote_rpc_errors_have_stable_codes() -> None:
    calls = 0

    def timeout_handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture timeout")

    async def timeout_run():
        async with httpx.AsyncClient(transport=mock_transport(timeout_handler, service="mcp")) as client:
            mcp = HttpMcpClient({"transport": "http", "endpoint": "https://mcp.fixture/rpc"}, client=client)
            with pytest.raises(McpClientError) as error:
                await mcp.call_tool("lookup", {})
            assert error.value.code == "timeout"
            assert error.value.retryable is False

    asyncio.run(timeout_run())
    assert calls == 1

    def rpc_error_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return rpc_response(body["id"], error={"code": -32601, "message": "fixture details"})

    async def error_run():
        async with httpx.AsyncClient(transport=mock_transport(rpc_error_handler, service="mcp")) as client:
            mcp = HttpMcpClient({"transport": "http", "endpoint": "https://mcp.fixture/rpc"}, client=client)
            with pytest.raises(McpClientError) as error:
                await mcp.call_tool("lookup", {})
            assert error.value.code == "remote_error"
            assert error.value.rpc_code == -32601
            assert "fixture details" not in str(error.value)

    asyncio.run(error_run())


def test_mcp_initialize_rejects_unsupported_version_and_wrong_response_id() -> None:
    for response_body in (
        {"jsonrpc": "2.0", "id": 0, "result": {
            "protocolVersion": "2030-01-01", "capabilities": {}, "serverInfo": {"name": "synthetic", "version": "1"},
        }},
        {"jsonrpc": "2.0", "id": 99, "result": {
            "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "serverInfo": {"name": "synthetic", "version": "1"},
        }},
    ):
        async def run(response_body=response_body):
            async with httpx.AsyncClient(
                transport=mock_transport(fixed_json_handler(response_body), service="mcp"),
            ) as client:
                mcp = HttpMcpClient({"transport": "http", "endpoint": "https://mcp.fixture/rpc"}, client=client, timeout=.5)
                with pytest.raises(McpClientError) as error:
                    await mcp.initialize()
                assert error.value.code == "protocol_error"
                assert mcp.initialized is False

        asyncio.run(run())


def test_mcp_session_expiration_is_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            return rpc_response(body['id'], {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "mock", "version": "1"},
            }, headers={"Mcp-Session-Id": "session-expired"})
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="mcp")) as client:
            mcp = HttpMcpClient({"transport": "http", "endpoint": "https://mcp.fixture/rpc"}, client=client)
            await mcp.initialize()
            with pytest.raises(McpClientError) as error:
                await mcp.list_tools()
            assert error.value.code == "session_expired"
            assert error.value.retryable is False

    asyncio.run(run())
    assert [item.get("method") for item in calls] == [
        "initialize", "notifications/initialized", "tools/list",
    ]


@pytest.mark.parametrize(
    "advertised_endpoint",
    ["https://foreign.fixture/collect", "//foreign.fixture/collect"],
)
def test_mcp_sse_rejects_cross_origin_endpoint_without_forwarding(
    advertised_endpoint,
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(
            200,
            content=f"event: endpoint\ndata: {advertised_endpoint}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        async with httpx.AsyncClient(
            transport=mock_transport(handler, service="mcp"),
        ) as client:
            mcp = HttpMcpClient({
                "transport": "sse",
                "endpoint": "https://mcp.fixture/sse",
                "headers": {"Authorization": "Bearer fixture-secret"},
            }, client=client, timeout=.5)
            with pytest.raises(McpClientError) as error:
                await mcp.initialize()
            assert error.value.code == "protocol_error"
            assert mcp.initialized is False

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0].url.host == "mcp.fixture"
    assert requests[0].method == "GET"
    assert requests[0].content == b""
    assert requests[0].headers["authorization"] == "Bearer fixture-secret"


def test_mcp_sse_initialize_list_call_reuses_auth_and_session() -> None:
    server = StandardServer('sse')

    def handler(request: httpx.Request) -> httpx.Response:
        return server(request)

    async def run():
        async with httpx.AsyncClient(
            transport=mock_transport(handler, service="mcp"),
            base_url="https://mcp.fixture",
        ) as client:
            mcp = HttpMcpClient({
                "transport": "sse",
                "endpoint": "https://mcp.fixture/sse",
                "headers": {"Authorization": "Bearer mock-sse-key"},
            }, client=client)
            await mcp.initialize()
            tools = await mcp.list_tools()
            result = await mcp.call_tool("lookup", {})
            return mcp, tools, result

    mcp, tools, result = asyncio.run(run())
    assert mcp.session_id == "synthetic-session"
    assert tools["tools"][0]["name"] == "lookup"
    assert result["content"][0]["text"] == "中文结果"
    assert sum(r.method == 'GET' for r in server.requests) == 1
    assert all(r.headers['authorization'] == 'Bearer mock-sse-key' for r in server.requests)
    assert all(str(r.url) == 'https://mcp.fixture/messages' for r in server.requests if r.method=='POST')
    assert server.methods() == ['initialize','notifications/initialized','tools/list','tools/call']
    assert all(stream.closed for stream in server.streams)


def test_mcp_tools_list_remote_error_is_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            return rpc_response(body['id'], {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            })
        return rpc_response(body["id"], error={"code": -32603, "message": "private detail"})

    async def run():
        async with httpx.AsyncClient(transport=mock_transport(handler, service="mcp")) as client:
            mcp = HttpMcpClient({"transport": "http", "endpoint": "https://mcp.fixture/rpc"}, client=client)
            await mcp.initialize()
            with pytest.raises(McpClientError) as error:
                await mcp.list_tools()
            assert error.value.code == "remote_error"
            assert error.value.rpc_code == -32603
            assert "private detail" not in str(error.value)

    asyncio.run(run())


def test_all_provider_and_mcp_requests_were_intercepted_by_mock_transport() -> None:
    provider_requests = []
    mcp_requests = []

    async def run():
        async with httpx.AsyncClient(
            transport=mock_transport(
                lambda _: httpx.Response(200),
                service="provider",
                requests=provider_requests,
            ),
        ) as provider_client:
            await provider_client.get("https://provider.fixture/counter-check")
        async with httpx.AsyncClient(
            transport=mock_transport(
                lambda _: httpx.Response(200),
                service="mcp",
                requests=mcp_requests,
            ),
        ) as mcp_client:
            await mcp_client.get("https://mcp.fixture/counter-check")

    asyncio.run(run())
    assert len(provider_requests) == 1
    assert len(mcp_requests) == 1
    assert provider_requests[0].url.host == "provider.fixture"
    assert mcp_requests[0].url.host == "mcp.fixture"
    assert all(request.url.host.endswith("fixture") for request in PROVIDER_MOCK_REQUESTS)
    assert all(request.url.host.endswith("fixture") for request in MCP_MOCK_REQUESTS)

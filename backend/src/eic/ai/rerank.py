from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor
from pydantic import ConfigDict, Field

from eic.ai.providers import ModelProviderError
from eic.ai.stream_reader import complete_cleanup
from eic.core.observability import observe_model_call, usage_fields
from eic.core.security import decrypt_secret
from eic.core.runtime_io import borrowed_http_transport
from eic.models import AIModel
from eic.vector.qdrant_reader import VectorHit

DASHSCOPE_RERANK_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"


def is_dashscope_rerank(model: AIModel) -> bool:
    return model.provider.strip().lower() in {"dashscope", "dashscope_rerank", "aliyun_rerank", "gte-rerank-v2"} or _validated_endpoint(model).hostname == "dashscope.aliyuncs.com"


def _validated_endpoint(model: AIModel):
    configured = model.endpoint
    if not configured and model.provider.strip().lower() in {
        "dashscope", "dashscope_rerank", "aliyun_rerank", "gte-rerank-v2",
    }:
        configured = DASHSCOPE_RERANK_ENDPOINT
    try:
        parts = urlsplit(configured)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or "#" in configured or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in configured)
                or "\\" in configured):
            raise ValueError("invalid endpoint")
        _port = parts.port
        httpx.URL(configured)
    except (ValueError, httpx.InvalidURL):
        raise ModelProviderError("Invalid rerank endpoint") from None
    return parts


def _endpoint(model: AIModel) -> str:
    parts = _validated_endpoint(model)
    path = parts.path.rstrip("/")
    if parts.hostname == "dashscope.aliyuncs.com" and path.endswith("/api/v1"):
        path += "/services/rerank/text-rerank/text-rerank"
    return urlunsplit(parts._replace(path=path))


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """仅借用传输层；关闭包装客户端不关闭调用方 transport。"""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def handle_async_request(self, request):
        return await self.client._transport_for_url(request.url).handle_async_request(request)


async def rerank_hits(
    model: AIModel,
    query: str,
    hits: Sequence[VectorHit],
    *,
    limit: int = 5,
    client: httpx.AsyncClient | None = None,
    secret_override: str | None = None,
    usage_sink: dict | None = None,
) -> list[VectorHit]:
    """保留旧 VectorHit 契约，实际排序经标准 Document compressor。"""
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if not hits:
        return []
    documents = [Document(page_content=str(hit.payload.get("content", "")),
                          metadata=dict(hit.payload), id=str(index))
                 for index, hit in enumerate(hits)]
    adapter = EICDocumentReranker(model=model, top_n=limit, client=client,
                                 secret_override=secret_override, usage_sink=usage_sink)
    compressed = await adapter.acompress_documents(documents, query)
    return [VectorHit(hits[int(doc.id)].point_id, doc.metadata["rerankScore"],
                      {**hits[int(doc.id)].payload, "rerankScore": doc.metadata["rerankScore"],
                       "rerankModel": model.model_name, "scoreSpace": "rerank"})
            for doc in compressed]


@observe_model_call("rerank")
async def _rerank_hits(
    model: AIModel,
    query: str,
    hits: Sequence[VectorHit],
    *,
    limit: int = 5,
    client: httpx.AsyncClient | None = None,
    secret_override: str | None = None,
    usage_sink: dict | None = None,
) -> list[VectorHit]:
    """Rerank candidate chunks with DashScope's native text-rerank API.

    The API returns candidate indexes and relevance scores. Original chunk IDs
    and metadata are retained so the result remains traceable to retrieval.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if not hits:
        return []
    if model.model_type.strip().lower() not in {"rerank", "4"} or model.enabled is False:
        raise ModelProviderError("Unsupported rerank provider")

    headers = {"Content-Type": "application/json"}
    documents = [str(hit.payload.get("content", "")) for hit in hits]
    top_n = min(limit, len(hits))
    if is_dashscope_rerank(model):
        url = _endpoint(model)
        payload = {
            "model": model.model_name or "gte-rerank-v2",
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": True, "top_n": top_n},
        }
    else:
        parts = _validated_endpoint(model)
        path = parts.path.rstrip("/")
        if not path.endswith(("/rerank", "/reranks")):
            path += "/rerank"
        url = urlunsplit(parts._replace(path=path))
        payload = {"model": model.model_name, "query": query, "documents": documents, "top_n": top_n}

    active_client = None
    try:
        if client is not None and client.is_closed:
            raise ModelProviderError("Rerank client is closed")
        active_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0), trust_env=False,
            follow_redirects=False,
            transport=_BorrowedTransport(client) if client is not None else borrowed_http_transport(),
        )
        secret = secret_override if secret_override is not None else (
            decrypt_secret(model.secret_encrypted) if model.secret_encrypted else "")
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        usage_sink["_invoked"] = True
        response = await active_client.post(url, json=payload, headers=headers, follow_redirects=False)
        response.raise_for_status()
        body = response.json()
        usage_sink.update(usage_fields(body))
        if not isinstance(body, dict):
            raise TypeError("rerank response is not an object")
        if is_dashscope_rerank(model):
            output = body.get("output")
            results = output.get("results") if isinstance(output, dict) else None
        else:
            results = body.get("results")
        if not isinstance(results, list) or len(results) != top_n:
            raise TypeError("rerank response has no results")

        ranked: list[VectorHit] = []
        seen = set()
        for item in results:
            if not isinstance(item, dict):
                raise TypeError("rerank result is not an object")
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if type(index) is not int or not 0 <= index < len(hits) or index in seen:
                raise ValueError("invalid or duplicate rerank index")
            if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("invalid rerank relevance score")
            seen.add(index)
            relevance = float(score)
            original = hits[index]
            metadata = {**original.payload, "rerankScore": relevance, "rerankModel": model.model_name, "scoreSpace": "rerank"}
            ranked.append(VectorHit(original.point_id, relevance, metadata))
        if not ranked:
            raise ValueError("rerank response contains no valid candidates")
        return sorted(ranked, key=lambda hit: hit.score, reverse=True)[:top_n]
    except Exception:  # noqa: BLE001 - 任意 transport 异常均需脱敏；CancelledError 继续传播。
        raise ModelProviderError("Rerank provider request failed") from None
    finally:
        if active_client is not None:
            try:
                await complete_cleanup(active_client.aclose())
            except Exception:  # noqa: BLE001 - 任意清理异常均需脱敏，失败不可伪成功。
                raise ModelProviderError("Rerank provider cleanup failed") from None


def _run_sync(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    coroutine.close()
    raise RuntimeError("use the async LangChain reranker method inside a running event loop")


class EICDocumentReranker(BaseDocumentCompressor):
    """LangChain compressor adapter retaining source Document identity and metadata."""

    model: AIModel
    top_n: int = Field(default=5, ge=1, strict=True)
    client: Any = Field(default=None, exclude=True, repr=False)
    secret_override: str | None = Field(default=None, exclude=True, repr=False)
    usage_sink: Any = Field(default=None, exclude=True, repr=False)

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks=None,
    ) -> Sequence[Document]:
        return _run_sync(self.acompress_documents(documents, query, callbacks=callbacks))

    async def acompress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks=None,
    ) -> Sequence[Document]:
        if not documents:
            return []
        hits = [
            VectorHit(
                point_id=str(index),
                score=0.0,
                payload={**document.metadata, "content": document.page_content},
            )
            for index, document in enumerate(documents)
        ]
        ranked = await _rerank_hits(
            self.model,
            query,
            hits,
            limit=self.top_n,
            client=self.client,
            secret_override=self.secret_override,
            usage_sink=self.usage_sink,
        )
        result: list[Document] = []
        for hit in ranked:
            original = documents[int(hit.point_id)]
            metadata = {
                **original.metadata,
                "rerankScore": hit.score,
                "rerankModel": self.model.model_name,
                "scoreSpace": "rerank",
            }
            result.append(original.model_copy(update={"metadata": metadata}))
        return result

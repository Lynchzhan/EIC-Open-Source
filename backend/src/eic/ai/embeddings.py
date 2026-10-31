from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence

import httpx
import openai
from cryptography.fernet import InvalidToken
from langchain_core.embeddings import Embeddings

from eic.ai.providers import ModelProviderError
from eic.core.observability import observe_model_call
from eic.core.security import decrypt_secret
from eic.core.runtime_io import borrowed_http_transport
from eic.models import AIModel


def effective_embedding_batch_size(batch_size: int) -> int:
    """请求与重试单元共用供应商批量上限，兼容已存知识库参数。"""
    from eic.core.config import get_settings
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("embedding batch size must be a positive integer")
    return min(batch_size, get_settings().embedding_provider_max_batch_size)


def validate_embedding_texts(texts: Sequence[str]) -> None:
    """发送前校验完整输入；不删除空项，避免向量与原始分块错位。"""
    if isinstance(texts, (str, bytes)):
        raise ModelProviderError("Embedding input must be a sequence of texts",
                                 reason="embedding_input_invalid", retryable=False)
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise ModelProviderError("Embedding input contains an empty or invalid text",
                                     reason="embedding_input_invalid", retryable=False)
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise ModelProviderError("Embedding input is not valid UTF-8",
                                     reason="embedding_input_invalid", retryable=False) from None


def validate_embedding_model(model: AIModel | None, tenant_id: int | None = None) -> None:
    if (
        model is None or model.model_type != "embedding" or not model.enabled
        or (tenant_id is not None and model.tenant_id != tenant_id)
    ):
        raise ModelProviderError("Embedding model is unavailable for this tenant")
    from eic.ai.langchain_embeddings import configuration
    configuration(model)


async def embed_texts(
    model: AIModel, texts: Sequence[str], *,
    tenant_id: int | None = None, client: httpx.AsyncClient | None = None,
    usage_sink: dict | None = None,
    progress_callback=None, timeout: int = 60, batch_size: int = 8, ingestion: bool = False,
) -> list[list[float]]:
    """现有查询与入库入口统一经过 LangChain Embeddings。"""
    validate_embedding_model(model, tenant_id)
    validate_embedding_texts(texts)
    adapter = EICEmbeddings(
        model, tenant_id=tenant_id, client=client,
        batch_size=batch_size, usage_sink=usage_sink, progress_callback=progress_callback,
        timeout=timeout, ingestion=ingestion,
    )
    return await adapter.aembed_documents(list(texts))


async def _guarded_embed_texts(
    model: AIModel, texts: Sequence[str], *,
    tenant_id: int | None = None, client: httpx.AsyncClient | None = None,
    usage_sink: dict | None = None, timeout: int = 60, ingestion: bool = False,
) -> list[list[float]]:
    from eic.core.dependency_controls import DependencyDenied, guard
    validate_embedding_model(model, tenant_id)
    validate_embedding_texts(texts)
    if not texts:
        return []
    try:
        async with guard("embedding", tenant_id or model.tenant_id,
                         provider=model.provider, model_id=model.id, endpoint=model.endpoint,
                         reserve_units=max(1, sum(len(item.encode("utf-8")) for item in texts)),
                         enforce_window_limits=not ingestion, admission_lifetime=timeout + 30):
            return await _embed_texts(
                model,
                texts,
                tenant_id=tenant_id,
                client=client,
                usage_sink=usage_sink,
                timeout=timeout,
            )
    except DependencyDenied as exc:
        raise ModelProviderError(str(exc), reason=exc.code, retryable=False) from exc


@observe_model_call("embedding")
async def _embed_texts(
    model: AIModel, texts: Sequence[str], *,
    tenant_id: int | None = None, client: httpx.AsyncClient | None = None,
    usage_sink: dict | None = None, timeout: int = 60,
) -> list[list[float]]:
    validate_embedding_model(model, tenant_id)
    validate_embedding_texts(texts)
    if not texts:
        return []
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False,
        transport=borrowed_http_transport())
    try:
        secret = decrypt_secret(model.secret_encrypted) if model.secret_encrypted else ""
        from eic.ai.langchain_embeddings import official_embeddings
        raw = await official_embeddings(model, texts, active_client, secret, usage_sink, timeout=timeout)
        vectors = [[float(value) for value in vector] for vector in raw]
        if len(vectors) != len(texts):
            raise ValueError("embedding provider returned an unexpected vector count")
        if any(not vector or any(not math.isfinite(value) for value in vector) for vector in vectors):
            raise ValueError("embedding provider returned invalid vectors")
        if len({len(vector) for vector in vectors}) > 1:
            raise ValueError("embedding provider returned inconsistent dimensions")
        if model.embedding_dimensions and any(len(vector) != model.embedding_dimensions for vector in vectors):
            raise ValueError(
                f"embedding dimension mismatch: expected {model.embedding_dimensions}"
            )
        return vectors
    except ModelProviderError:
        raise
    except (httpx.HTTPError, openai.OpenAIError, ConnectionError, InvalidToken, KeyError, TypeError, ValueError) as exc:
        from eic.ai.langchain_embeddings import safe_embedding_error
        raise safe_embedding_error(exc) from None
    finally:
        if owns_client:
            from eic.ai.langchain_embeddings import _cleanup
            from eic.ai.stream_reader import complete_cleanup
            await complete_cleanup(_cleanup([active_client], []))


def _run_sync(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    coroutine.close()
    raise RuntimeError("use the async LangChain embedding method inside a running event loop")


class EICEmbeddings(Embeddings):
    """LangChain adapter retaining EIC tenant checks, provider guards, and usage capture."""

    def __init__(
        self,
        model: AIModel,
        *,
        tenant_id: int | None = None,
        batch_size: int = 8,
        client: httpx.AsyncClient | None = None,
        usage_callback: Callable[[dict], None] | None = None,
        usage_sink: dict | None = None,
        progress_callback=None,
        timeout: int = 60, ingestion: bool = False,
    ) -> None:
        self.model = model
        self.tenant_id = tenant_id
        # 兼容接口的供应商上限不同，统一采用保守批量，禁止整篇文档单次发送。
        self.batch_size = effective_embedding_batch_size(batch_size)
        self.timeout = timeout
        self.ingestion = ingestion
        self.client = client
        self.usage_callback = usage_callback
        self.usage_sink = usage_sink
        self.progress_callback = progress_callback

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return _run_sync(self.aembed_documents(texts))

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        validate_embedding_model(self.model, self.tenant_id)
        validate_embedding_texts(texts)
        vectors: list[list[float]] = []
        for offset in range(0, len(texts), self.batch_size):
            batch = texts[offset:offset + self.batch_size]
            usage: dict = self.usage_sink if self.usage_sink is not None else {}
            deadline = asyncio.get_running_loop().time() + 900
            while True:
                if self.progress_callback is not None:
                    await self.progress_callback(offset, len(texts), False)
                try:
                    batch_vectors = await _guarded_embed_texts(
                        self.model, batch, tenant_id=self.tenant_id,
                        client=self.client, usage_sink=usage,
                        timeout=self.timeout, ingestion=self.ingestion,
                    )
                    break
                except ModelProviderError as exc:
                    # 只等待尚未获准发送的速率拒绝；供应商错误/超时不重发。
                    if exc.reason != "rate_budget_exceeded" or self.progress_callback is None:
                        raise
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ModelProviderError("Embedding admission wait expired",
                            reason="admission_wait_expired", retryable=False) from None
                    await self.progress_callback(offset, len(texts), True)
                    await asyncio.sleep(5)
            if vectors and batch_vectors and len(vectors[0]) != len(batch_vectors[0]):
                raise ModelProviderError("Embedding provider returned inconsistent batch dimensions",
                                         reason="embedding_response_invalid", retryable=False)
            vectors.extend(batch_vectors)
            if self.usage_callback is not None and usage:
                self.usage_callback(dict(usage))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.aembed_documents([text]))[0]

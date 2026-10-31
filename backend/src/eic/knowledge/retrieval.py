from __future__ import annotations

import asyncio
import inspect
import math
import re
import hashlib
from collections import Counter, OrderedDict
from threading import RLock
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

from eic.knowledge.documents import to_langchain_document
from eic.vector.qdrant_reader import VectorHit


def _matches_scope(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def rrf_fuse(rankings: list[list[VectorHit]], *, k: int = 60) -> list[VectorHit]:
    scores: dict[str, float] = {}
    values: dict[str, VectorHit] = {}
    for ranking in rankings:
        seen = set()
        for rank, hit in enumerate(ranking, start=1):
            if hit.point_id in seen:
                continue
            seen.add(hit.point_id)
            scores[hit.point_id] = scores.get(hit.point_id, 0.0) + 1 / (k + rank)
            values[hit.point_id] = hit
    return [
        VectorHit(hit.point_id, scores[hit.point_id],
                  {**hit.payload, "fusionScore": scores[hit.point_id], "scoreSpace": "fusion"})
        for hit in sorted(values.values(), key=lambda item: scores[item.point_id], reverse=True)
    ]


_BM25_CACHE = OrderedDict()
_BM25_CACHE_CHARS = 0
_BM25_CACHE_DOCUMENTS = 0
_BM25_MAX_CHARS = 4_000_000
_BM25_MAX_DOCUMENTS = 20_000
_BM25_LOCK = RLock()


def lexical_search(query: str, documents: list[VectorHit], *, limit: int = 10) -> list[VectorHit]:
    with _BM25_LOCK:
        return _cached_lexical_search(query, documents, limit=limit)


def _cached_lexical_search(query, documents, *, limit):
    global _BM25_CACHE_CHARS, _BM25_CACHE_DOCUMENTS
    fingerprint = hashlib.sha256()
    weight = 0
    for hit in documents:
        text = str(hit.payload.get("content", ""))
        weight += len(text)
        fingerprint.update(str(hit.point_id).encode() + b"\0" + hashlib.sha256(text.encode()).digest())
    key = fingerprint.digest()
    cached = _BM25_CACHE.pop(key, None)
    if cached is None:
        # 不把本次的完整 metadata/权限上下文保存在缓存中。
        index = Bm25Index([VectorHit(hit.point_id, 0, {"content": str(hit.payload.get("content", ""))})
                           for hit in documents])
        if weight <= _BM25_MAX_CHARS and len(documents) <= _BM25_MAX_DOCUMENTS:
            while _BM25_CACHE and (_BM25_CACHE_CHARS + weight > _BM25_MAX_CHARS
                    or _BM25_CACHE_DOCUMENTS + len(documents) > _BM25_MAX_DOCUMENTS or len(_BM25_CACHE) >= 8):
                _, (old_index, old_weight) = _BM25_CACHE.popitem(last=False)
                _BM25_CACHE_CHARS -= old_weight
                _BM25_CACHE_DOCUMENTS -= len(old_index.documents)
            _BM25_CACHE[key] = (index, weight)
            _BM25_CACHE_CHARS += weight
            _BM25_CACHE_DOCUMENTS += len(documents)
    else:
        index, _ = cached
        _BM25_CACHE[key] = cached
    # 缓存只复用统计与排序，输出使用本次重新授权后的 payload。
    current = {str(hit.point_id): hit for hit in documents}
    return [VectorHit(hit.point_id, hit.score, current[str(hit.point_id)].payload)
            for hit in index.search(query, limit=limit) if str(hit.point_id) in current]


def rerank(query: str, hits: list[VectorHit], scorer: Callable[[str, str], float], *, limit: int = 5) -> list[VectorHit]:
    ranked = [VectorHit(hit.point_id, scorer(query, str(hit.payload.get("content", ""))), hit.payload) for hit in hits]
    return sorted(ranked, key=lambda hit: hit.score, reverse=True)[:limit]


def tokenize(text: str) -> list[str]:
    """Use jieba when installed; retain a deterministic CJK fallback for local demos."""
    try:
        import jieba

        return [term.lower() for term in jieba.lcut(text) if term.strip()]
    except ImportError:
        return re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9_]+", text.lower())


def filter_by_metadata(hits: Iterable[VectorHit], filters: dict[str, object] | None) -> list[VectorHit]:
    if not filters:
        return list(hits)
    return [
        hit for hit in hits
        if all(hit.payload.get(key) == value for key, value in filters.items())
    ]


@dataclass
class Bm25Index:
    documents: list[VectorHit]

    def __post_init__(self) -> None:
        self.tokens = [tokenize(str(hit.payload.get("content", ""))) for hit in self.documents]
        self.term_counts = [Counter(terms) for terms in self.tokens]
        self.document_frequency: dict[str, int] = {}
        for terms in self.tokens:
            for term in set(terms):
                self.document_frequency[term] = self.document_frequency.get(term, 0) + 1
        self.average_length = sum(map(len, self.tokens)) / len(self.tokens) if self.tokens else 0.0

    def search(self, query: str, *, limit: int = 10, k1: float = 1.5, b: float = 0.75) -> list[VectorHit]:
        query_terms = tokenize(query)
        if not query_terms or not self.documents:
            return []
        count = len(self.documents)
        scores: list[VectorHit] = []
        for hit, document_terms, term_counts in zip(self.documents, self.tokens, self.term_counts, strict=True):
            score = 0.0
            for term in query_terms:
                frequency = term_counts.get(term, 0)
                if not frequency:
                    continue
                document_frequency = self.document_frequency.get(term, 0)
                inverse_frequency = math.log(1 + (count - document_frequency + 0.5) / (document_frequency + 0.5))
                length_ratio = len(document_terms) / self.average_length if self.average_length else 1.0
                score += inverse_frequency * frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * length_ratio))
            if score:
                scores.append(VectorHit(hit.point_id, score, hit.payload))
        return sorted(scores, key=lambda hit: hit.score, reverse=True)[:limit]


class EICRetriever(BaseRetriever):
    """LangChain retriever that supplies tenant scope before search and rechecks hits."""

    search: Callable[..., Any] = Field(exclude=True, repr=False)
    tenant_id: int = Field(gt=0, strict=True)
    knowledge_base_id: int = Field(gt=0, strict=True)
    top_k: int = Field(default=5, ge=1, le=100)

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    def _scope(self) -> dict[str, int]:
        return {"tenant_id": self.tenant_id, "knowledge_base_id": self.knowledge_base_id}

    def _search(self, query: str) -> Sequence[VectorHit | Document]:
        return self.search(query, **self._scope(), limit=self.top_k)

    def _documents(self, hits: Sequence[VectorHit | Document]) -> list[Document]:
        documents: list[Document] = []
        for hit in hits:
            if isinstance(hit, Document):
                content, metadata, score = hit.page_content, dict(hit.metadata), None
                chunk_id = metadata.get("chunk_id", metadata.get("chunkId"))
            else:
                content, metadata, score = str(hit.payload.get("content", "")), dict(hit.payload), hit.score
                chunk_id = hit.point_id

            tenant_values = [metadata[key] for key in ("tenant_id", "tenantId") if key in metadata]
            base_values = [
                metadata[key] for key in ("knowledge_base_id", "knowledgeBaseId") if key in metadata
            ]
            if (not tenant_values or not base_values
                    or any(not _matches_scope(value, self.tenant_id) for value in tenant_values)
                    or any(not _matches_scope(value, self.knowledge_base_id) for value in base_values)):
                continue

            source_name = metadata.get("source_name", metadata.get("sourceName", ""))
            page_numbers = metadata.get("pageNumbers", metadata.get("page_numbers", []))
            document_id = metadata.get("document_id", metadata.get("documentId"))
            parent_chunk_id = metadata.get("parent_chunk_id", metadata.get("parentChunkId"))
            safe_metadata = {
                key: value for key, value in metadata.items()
                if key not in {
                    "content", "tenantId", "knowledgeBaseId", "documentId", "chunkId",
                    "sourceName", "page_numbers", "parentChunkId",
                }
            }
            if score is not None:
                safe_metadata["score"] = score
            documents.append(to_langchain_document(
                content,
                tenant_id=self.tenant_id,
                knowledge_base_id=self.knowledge_base_id,
                document_id=document_id,
                chunk_id=str(chunk_id or ""),
                source_name=str(source_name or ""),
                page_numbers=page_numbers,
                parent_chunk_id=parent_chunk_id,
                metadata=safe_metadata,
            ))
        return documents

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        return self._documents(self._search(query))

    async def _aget_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        if inspect.iscoroutinefunction(self.search):
            hits = await self._search(query)
        else:
            hits = await asyncio.to_thread(self._search, query)
            if inspect.isawaitable(hits):
                hits = await hits
        return self._documents(hits)

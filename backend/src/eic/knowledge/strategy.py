from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from eic.knowledge.retrieval import filter_by_metadata, lexical_search, rerank, rrf_fuse, tokenize
from eic.vector.qdrant_reader import VectorHit

STRATEGY_BASIC = "basic"
STRATEGY_ADVANCED = "advanced"
STRATEGY_FULL = "full"
VALID_STRATEGIES = (STRATEGY_BASIC, STRATEGY_ADVANCED, STRATEGY_FULL)

STRATEGY_LABELS = {
    STRATEGY_BASIC: "基础检索：向量检索",
    STRATEGY_ADVANCED: "进阶检索：向量检索 + BM25",
    STRATEGY_FULL: "完整检索：向量检索 + BM25 + 重排 + 父块回填",
}

DEFAULT_RETRIEVAL_CONFIG: dict[str, Any] = {
    "topK": 8,
    "scoreThreshold": 0.0,
    "vectorTopK": 24,
    "bm25TopK": 24,
    "rrfK": 60,
    "finalLimit": 8,
    "rerankTopN": 8,
    "candidateLimit": 48,
    "thresholdVersion": 2,
    "denseThreshold": 0.0,
    "sparseThreshold": 0.0,
    "fusionThreshold": 0.0,
    "rerankThreshold": 0.0,
    "parentFill": True,
    "parentSize": 2400,
    "childSize": 900,
    "childOverlap": 120,
    # 入库参数存放在已有 JSON 配置中，0 表示不设置应用层硬额度。
    "indexConcurrency": 2,
    "maxChunksPerDocument": 0,
    "embeddingQuota": 0,
    "embeddingBatchSize": 8,
    "embeddingBatchTokens": 0,
    "embeddingRetryCount": 2,
    "embeddingRequestTimeout": 60,
}


def normalize_strategy(value: Any, default: str = STRATEGY_FULL) -> str:
    text = str(value or default).strip().lower()
    aliases = {
        "vector": STRATEGY_BASIC,
        "dense": STRATEGY_BASIC,
        "basic": STRATEGY_BASIC,
        "hybrid": STRATEGY_ADVANCED,
        "advanced": STRATEGY_ADVANCED,
        "bm25": STRATEGY_ADVANCED,
        "full": STRATEGY_FULL,
        "complete": STRATEGY_FULL,
        "parent": STRATEGY_FULL,
    }
    return aliases.get(text, default if default in VALID_STRATEGIES else STRATEGY_FULL)


def _as_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def merge_retrieval_config(*parts: dict[str, Any] | None, chunk_size: int | None = None, chunk_overlap: int | None = None) -> dict[str, Any]:
    merged = dict(DEFAULT_RETRIEVAL_CONFIG)
    if chunk_size:
        merged["childSize"] = int(chunk_size)
        merged["parentSize"] = max(int(chunk_size) * 2, merged["parentSize"])
        merged["topK"] = merged.get("topK") or DEFAULT_RETRIEVAL_CONFIG["topK"]
    if chunk_overlap is not None:
        merged["childOverlap"] = int(chunk_overlap)
    for part in parts:
        if not part:
            continue
        mapping = {
            "topK": part.get("topK") or part.get("top_k"),
            "scoreThreshold": part.get("scoreThreshold") if part.get("scoreThreshold") is not None else part.get("score_threshold"),
            "vectorTopK": part.get("vectorTopK") or part.get("vector_top_k"),
            "bm25TopK": part.get("bm25TopK") or part.get("bm25_top_k"),
            "rrfK": part.get("rrfK") or part.get("rrf_k"),
            "finalLimit": part.get("finalLimit") or part.get("final_limit") or part.get("topK"),
            "rerankTopN": part.get("rerankTopN") or part.get("rerank_top_n"),
            "candidateLimit": part.get("candidateLimit"),
            "thresholdVersion": part.get("thresholdVersion"),
            "denseThreshold": part.get("denseThreshold"),
            "sparseThreshold": part.get("sparseThreshold"),
            "fusionThreshold": part.get("fusionThreshold"),
            "rerankThreshold": part.get("rerankThreshold"),
            "parentFill": part.get("parentFill") if part.get("parentFill") is not None else part.get("parent_fill"),
            "parentSize": part.get("parentSize") or part.get("parent_size"),
            "childSize": part.get("childSize") or part.get("child_size") or part.get("chunkSize") or part.get("maxTokensPerParagraph"),
            "childOverlap": part.get("childOverlap") if part.get("childOverlap") is not None else (part.get("child_overlap") if part.get("child_overlap") is not None else (part.get("chunkOverlap") if part.get("chunkOverlap") is not None else part.get("overlappingTokens"))),
            "indexConcurrency": part.get("indexConcurrency") or part.get("ingestionConcurrency"),
            "maxChunksPerDocument": part.get("maxChunksPerDocument") if part.get("maxChunksPerDocument") is not None else part.get("maxChunkCount"),
            "embeddingQuota": part.get("embeddingQuota") if part.get("embeddingQuota") is not None else part.get("vectorizationQuota"),
            "embeddingBatchSize": part.get("embeddingBatchSize") or part.get("batchSize"),
            "embeddingBatchTokens": part.get("embeddingBatchTokens") if part.get("embeddingBatchTokens") is not None else part.get("batchTokens"),
            "embeddingRetryCount": part.get("embeddingRetryCount") if part.get("embeddingRetryCount") is not None else part.get("retryCount"),
            "embeddingRequestTimeout": part.get("embeddingRequestTimeout") if part.get("embeddingRequestTimeout") is not None else part.get("requestTimeout"),
        }
        for key, value in mapping.items():
            if value is None or value == "":
                continue
            if key == "parentFill":
                merged[key] = value not in {False, 0, "0", "false", "False"}
            elif key in {"scoreThreshold", "denseThreshold", "sparseThreshold", "fusionThreshold", "rerankThreshold"}:
                merged[key] = _as_float(value, merged[key])
            else:
                merged[key] = _as_int(value, merged[key], minimum=0)
    child_size = max(1, int(merged["childSize"]))
    overlap = int(merged["childOverlap"])
    if overlap >= child_size:
        overlap = max(0, child_size // 5)
    merged["childSize"] = child_size
    merged["childOverlap"] = overlap
    merged["parentSize"] = max(child_size, int(merged["parentSize"] or child_size * 2))
    merged["topK"] = max(1, min(50, int(merged["topK"] or 8)))
    merged["finalLimit"] = max(1, min(50, int(merged["finalLimit"] or merged["topK"])))
    merged["vectorTopK"] = max(merged["finalLimit"], min(100, int(merged["vectorTopK"] or merged["finalLimit"] * 3)))
    merged["bm25TopK"] = max(merged["finalLimit"], min(100, int(merged["bm25TopK"] or merged["finalLimit"] * 3)))
    merged["rrfK"] = max(1, int(merged["rrfK"] or 60))
    merged["rerankTopN"] = max(merged["finalLimit"], min(100, int(merged["rerankTopN"] or merged["finalLimit"])))
    merged["candidateLimit"] = max(merged["rerankTopN"], min(200, int(merged["candidateLimit"] or 48)))
    merged["indexConcurrency"] = max(1, min(10, int(merged["indexConcurrency"] or 2)))
    merged["maxChunksPerDocument"] = max(0, min(1_000_000, int(merged["maxChunksPerDocument"] or 0)))
    merged["embeddingQuota"] = max(0, min(1_000_000, int(merged["embeddingQuota"] or 0)))
    merged["embeddingBatchSize"] = max(1, min(256, int(merged["embeddingBatchSize"] or 8)))
    merged["embeddingBatchTokens"] = max(0, min(1_000_000, int(merged["embeddingBatchTokens"] or 0)))
    merged["embeddingRetryCount"] = max(0, min(10, int(merged["embeddingRetryCount"] or 0)))
    merged["embeddingRequestTimeout"] = max(5, min(600, int(merged["embeddingRequestTimeout"] or 60)))
    explicit_version = next((p["thresholdVersion"] for p in reversed(parts) if p and "thresholdVersion" in p), None)
    if explicit_version is None and merged["scoreThreshold"] > 0:
        merged["thresholdVersion"] = 1
    merged["thresholdStatus"] = "legacy_requires_review" if (
        merged["thresholdVersion"] != 2 and merged["scoreThreshold"] > 0) else "versioned"
    return merged


@dataclass
class RetrievalSettings:
    strategy: str = STRATEGY_FULL
    config: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RETRIEVAL_CONFIG))

    @property
    def label(self) -> str:
        return STRATEGY_LABELS.get(self.strategy, STRATEGY_LABELS[STRATEGY_FULL])

    @property
    def uses_vector(self) -> bool:
        return True

    @property
    def uses_bm25(self) -> bool:
        return self.strategy in {STRATEGY_ADVANCED, STRATEGY_FULL}

    @property
    def uses_rerank(self) -> bool:
        return self.strategy == STRATEGY_FULL

    @property
    def uses_parent_fill(self) -> bool:
        return self.strategy == STRATEGY_FULL and bool(self.config.get("parentFill", True))

    @property
    def limit(self) -> int:
        if self.strategy == STRATEGY_BASIC:
            return int(self.config.get("topK") or 8)
        return int(self.config.get("finalLimit") or self.config.get("topK") or 8)


def settings_from_knowledge_base(base: Any, extra: dict[str, Any] | None = None) -> RetrievalSettings:
    extra = extra or {}
    strategy = normalize_strategy(extra.get("retrievalStrategy") or getattr(base, "retrieval_strategy", None) or STRATEGY_FULL)
    config = merge_retrieval_config(
        getattr(base, "retrieval_config", None) if base is not None else None,
        extra,
        chunk_size=getattr(base, "chunk_size", None) if base is not None else None,
        chunk_overlap=getattr(base, "chunk_overlap", None) if base is not None else None,
    )
    return RetrievalSettings(strategy=strategy, config=config)


def chunk_settings_from_base(base: Any) -> tuple[int, int, int]:
    settings = settings_from_knowledge_base(base)
    return (
        int(settings.config["parentSize"]),
        int(settings.config["childSize"]),
        int(settings.config["childOverlap"]),
    )


def _lexical_hits(query: str, hits: list[VectorHit], *, limit: int) -> list[VectorHit]:
    query_terms = set(tokenize(query))

    def lexical_score(_: str, content: str) -> float:
        content_terms = set(tokenize(content))
        return len(query_terms & content_terms) / max(len(query_terms), 1)

    return rerank(query, hits, lexical_score, limit=limit)


def _score_map(hits: list[VectorHit]) -> dict[str, float]:
    return {hit.point_id: hit.score for hit in hits}


def apply_score_threshold(hits: list[VectorHit], threshold: float) -> list[VectorHit]:
    if threshold is None:
        return hits
    return [hit for hit in hits if hit.score >= threshold]


def threshold(settings, space):
    if settings.config.get("thresholdVersion") != 2:
        legacy = float(settings.config.get("scoreThreshold") or 0)
        # D07 未批准转换，不能把 cosine 数值用于 RRF/rerank；正阈值阻断待校准。
        return None if legacy <= 0 else (legacy if space == "dense" else math.inf)
    return float(settings.config.get(space + "Threshold") or 0)


def fusion_candidates(settings, dense_hits, sparse_hits):
    dense = apply_score_threshold(dense_hits, threshold(settings, "dense"))
    sparse = apply_score_threshold(sparse_hits, threshold(settings, "sparse"))
    rankings = [ranking for ranking in (dense, sparse) if ranking]
    fused = rrf_fuse(rankings, k=int(settings.config.get("rrfK") or 60)) if rankings else []
    return apply_score_threshold(fused, threshold(settings, "fusion"))[:int(settings.config.get("candidateLimit") or 48)]


def fuse_strategy_hits(
    query: str,
    *,
    settings: RetrievalSettings,
    dense_hits: list[VectorHit],
    sparse_hits: list[VectorHit],
    reranked_hits: list[VectorHit] | None = None,
) -> list[VectorHit]:
    limit = settings.limit
    if settings.strategy == STRATEGY_BASIC:
        ranked = list(dense_hits)[:limit]
        return apply_score_threshold(ranked, threshold(settings, "dense"))
    fused = fusion_candidates(settings, dense_hits, sparse_hits)
    if settings.strategy == STRATEGY_ADVANCED:
        ranked = fused[:limit]
        return ranked
    ranked = list(reranked_hits) if reranked_hits is not None else fused
    if ranked and "rerankScore" in ranked[0].payload:
        ranked = apply_score_threshold(ranked, threshold(settings, "rerank"))
    return ranked[:limit]


def result_channel(
    *,
    settings: RetrievalSettings,
    point_id: str,
    parent_filled: bool,
    dense_scores: dict[str, float],
    sparse_scores: dict[str, float],
    rerank_applied: bool = True,
) -> str:
    if parent_filled:
        return "parent"
    if settings.uses_rerank and rerank_applied:
        return "rerank"
    has_vector = point_id in dense_scores
    has_bm25 = point_id in sparse_scores
    if settings.uses_bm25 and has_vector and has_bm25:
        return "rrf"
    if has_bm25 and not has_vector:
        return "bm25"
    return "vector"


def build_result_payload(
    hits: list[VectorHit],
    *,
    parent_contents: dict[Any, str],
    documents: dict[Any, Any],
    settings: RetrievalSettings,
    dense_scores: dict[str, float] | None = None,
    sparse_scores: dict[str, float] | None = None,
    knowledge_base: Any | None = None,
) -> list[dict[str, Any]]:
    dense_scores = dense_scores or {}
    sparse_scores = sparse_scores or {}
    results: list[dict[str, Any]] = []
    knowledge_base_id = getattr(knowledge_base, "id", None)
    knowledge_base_name = getattr(knowledge_base, "name", None) or ""
    for hit in hits:
        payload = dict(hit.payload or {})
        document_id = payload.get("documentId") or payload.get("document_id")
        parent_id = payload.get("parentChunkId") or payload.get("parent_chunk_id")
        child_content = str(payload.get("content") or "")
        parent_content = parent_contents.get(parent_id) or payload.get("parent_content") or payload.get("parentContent") or ""
        document = documents.get(document_id)
        document_name = ""
        if document is not None:
            document_name = getattr(document, "source_name", None) or payload.get("source_name") or ""
        else:
            document_name = str(payload.get("source_name") or payload.get("sourceName") or "")
        use_parent = settings.uses_parent_fill and parent_content
        page_numbers = payload.get("pageNumbers") or payload.get("page_numbers") or []
        if not isinstance(page_numbers, list):
            page_numbers = [page_numbers]
        results.append({
            "chunkId": hit.point_id,
            "score": hit.score,
            "content": parent_content if use_parent else child_content,
            "childContent": child_content,
            "parentContent": parent_content,
            "parentFilled": bool(use_parent),
            "documentId": document_id,
            "documentName": document_name,
            "metadata": payload,
            "vectorScore": dense_scores.get(str(hit.point_id)),
            "bm25Score": sparse_scores.get(str(hit.point_id)),
            "fusionScore": payload.get("fusionScore"),
            "rerankScore": payload.get("rerankScore"),
            "scoreSpace": "rerank" if "rerankScore" in payload else ("fusion" if "fusionScore" in payload else "dense"),
            "pageNumbers": page_numbers,
            "channel": result_channel(
                settings=settings,
                point_id=str(hit.point_id),
                parent_filled=bool(use_parent),
                dense_scores=dense_scores,
                sparse_scores=sparse_scores,
                rerank_applied="rerankScore" in payload,
            ),
            "knowledgeBaseId": knowledge_base_id,
            "knowledgeBaseName": knowledge_base_name,
        })
    return results


def candidate_hits_from_chunks(chunks: list[Any], filters: dict[str, object] | None = None) -> tuple[list[VectorHit], dict[Any, str]]:
    parent_contents = {
        chunk.id: chunk.content
        for chunk in chunks
        if (chunk.attributes or {}).get("chunk_kind") == "parent"
    }
    candidates = [
        VectorHit(
            str(chunk.id),
            0.0,
            {
                **(chunk.attributes or {}),
                "content": chunk.content,
                "documentId": chunk.document_id,
                "parentChunkId": chunk.parent_chunk_id,
            },
        )
        for chunk in chunks
        if (chunk.attributes or {}).get("chunk_kind", "child") == "child"
    ]
    return filter_by_metadata(candidates, filters), parent_contents


def search_sparse(candidates: list[VectorHit], query: str, *, limit: int) -> list[VectorHit]:
    return lexical_search(query, candidates, limit=limit)


def score_maps(dense_hits: list[VectorHit], sparse_hits: list[VectorHit]) -> tuple[dict[str, float], dict[str, float]]:
    return _score_map(dense_hits), _score_map(sparse_hits)

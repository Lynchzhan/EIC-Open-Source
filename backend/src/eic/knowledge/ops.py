from __future__ import annotations
from eic.core.observability import observe_retrieval

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from eic.ai.embeddings import embed_texts, validate_embedding_model
from eic.ai.providers import ModelProviderError
from eic.ai.rerank import rerank_hits
from eic.core.config import get_settings
from eic.core.database import SessionLocal
from eic.core.runtime_io import vector_connection
from eic.core.runtime_refs import RuntimeReferenceUnavailable, require_runtime_model, require_runtime_resource
from eic.knowledge.evaluation import aggregate_eval_scores, evaluate_case
from eic.knowledge.strategy import (
    STRATEGY_BASIC,
    STRATEGY_LABELS,
    build_result_payload,
    fuse_strategy_hits,
    fusion_candidates,
    merge_retrieval_config,
    normalize_strategy,
    score_maps,
    settings_from_knowledge_base,
)
from eic.models import AIModel, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeIngestionRun
from eic.vector.pgvector_store import PgVectorStore
from eic.vector.qdrant_reader import VectorHit

DOCUMENT_STATUS_LABELS = {
    "pending": "待处理",
    "queued": "处理中",
    "queued_text": "处理中",
    "processing": "处理中",
    "parsed_pending_embedding": "处理中",
    "ready": "已完成",
    "failed": "失败",
    "parsed": "已解析，未配置向量模型",
    "uncertain": "执行中断，需确认后重试",
    "cancelling": "正在停止",
    "cancelled": "已取消",
    "staged": "新版本已写入，等待整体切换",
    "legacy": "历史索引未验证，请重建",
}

DOCUMENT_STATUS_CODE = {
    "pending": 0,
    "queued": 1,
    "queued_text": 1,
    "processing": 1,
    "parsed_pending_embedding": 1,
    "ready": 2,
    "failed": 3,
    "parsed": 1,
    "uncertain": 3,
    "cancelling": 1,
    "cancelled": 3,
    "staged": 1,
    "legacy": 0,
}


async def _scoped_rows_by_id(session, model, identities, tenant_id, *criteria):
    """限制 IN 参数数量，兼容 SQLite/MySQL 的参数上限；每批保留租户筛选。"""
    identities = sorted(set(identities))
    rows = []
    for offset in range(0, len(identities), 500):
        rows.extend(await session.scalars(select(model).where(
            model.id.in_(identities[offset:offset + 500]), model.tenant_id == tenant_id, *criteria)))
    return rows


def guess_file_type(name: str, fallback: str = "Text") -> str:
    suffix = Path(str(name or "")).suffix.lower()
    mapping = {
        ".pdf": "PDF",
        ".doc": "Word",
        ".docx": "Word",
        ".xls": "Excel",
        ".xlsx": "Excel",
        ".txt": "Text",
        ".md": "Markdown",
        ".html": "Html",
        ".htm": "Html",
        ".png": "Image",
        ".jpg": "Image",
        ".jpeg": "Image",
        ".gif": "Image",
        ".bmp": "Image",
        ".webp": "Image",
        ".svg": "Image",
    }
    return mapping.get(suffix, fallback or "Text")


def isoformat_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def display_document_status(document: KnowledgeDocument, chunk_count: int = 0) -> str:
    status = str(document.status or "pending")
    if status in {"processed", "embedded", "done"}:
        status = "ready"
    if status == "ready" and getattr(document, "index_status", None) != "ready":
        return str(getattr(document, "index_status", None) or "pending")
    return status


def serialize_knowledge_document(document: KnowledgeDocument, *, chunk_count: int = 0, include_content: bool = False) -> dict[str, Any]:
    status = display_document_status(document, chunk_count)
    payload = {
        "id": document.id,
        "knowledgeBaseId": document.knowledge_base_id,
        "fileId": document.file_id,
        "sourceName": document.source_name,
        "contentName": document.source_name,
        "fileType": guess_file_type(document.source_name),
        "status": DOCUMENT_STATUS_CODE.get(status, 0),
        "statusText": DOCUMENT_STATUS_LABELS.get(status, status),
        "statusKey": status,
        "chunkCount": int(chunk_count or 0),
        "errorMessage": document.error_message or "",
        "extractionReport": dict(getattr(document, "extraction_report", None) or {}),
        "createdAt": isoformat_dt(getattr(document, "created_at", None)),
        "updatedAt": isoformat_dt(getattr(document, "updated_at", None)),
        "persisted": True,
        "parseStatus": document.parse_status,
        "embeddingStatus": document.embedding_status,
        "indexStatus": document.index_status,
        "indexError": document.index_error or {},
        "activeIndexVersion": document.active_index_version,
        "pendingIndexVersion": document.pending_index_version,
    }
    if include_content:
        payload["content"] = document.content or ""
    else:
        payload["contentPreview"] = (document.content or "")[:200]
    return payload


async def chunk_counts_by_document(session: AsyncSession, document_ids: list[int]) -> dict[int, int]:
    if not document_ids:
        return {}
    rows = await session.execute(
        select(KnowledgeChunk.document_id, func.count())
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(
            KnowledgeChunk.document_id.in_(document_ids),
            KnowledgeChunk.parent_chunk_id.is_not(None),
            KnowledgeChunk.index_version == func.coalesce(KnowledgeDocument.pending_index_version, KnowledgeDocument.active_index_version),
        )
        .group_by(KnowledgeChunk.document_id)
    )
    return {document_id: int(count or 0) for document_id, count in rows}


async def list_knowledge_documents(session: AsyncSession, *, tenant_id: int, knowledge_base_id: int) -> list[dict[str, Any]]:
    # 保持既有完整列表协议；数据库只返回每篇 200 字预览，不加载正文 ORM。
    columns = [column for column in KnowledgeDocument.__table__.c if column.name != "content"]
    statement = select(*columns, func.substr(KnowledgeDocument.content, 1, 200).label("content")).where(
        KnowledgeDocument.tenant_id == tenant_id,
        KnowledgeDocument.knowledge_base_id == knowledge_base_id,
        KnowledgeDocument.is_deleted.is_(False),
    )
    result, cursor = [], None
    while True:
        page = statement
        if cursor is not None:
            modified, identity = cursor
            page = page.where(or_(
                KnowledgeDocument.updated_at < modified,
                (KnowledgeDocument.updated_at == modified) & (KnowledgeDocument.id < identity),
            ))
        rows = (await session.execute(page.order_by(
            KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc(),
        ).limit(200))).all()
        if not rows:
            return result
        documents = [SimpleNamespace(**row._mapping) for row in rows]
        counts = await chunk_counts_by_document(session, [item.id for item in documents])
        result.extend(serialize_knowledge_document(item, chunk_count=counts.get(item.id, 0)) for item in documents)
        cursor = (documents[-1].updated_at, documents[-1].id)


async def serialize_document_detail(session: AsyncSession, document: KnowledgeDocument) -> dict[str, Any]:
    chunks = list(
        await session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document.id, KnowledgeChunk.tenant_id == document.tenant_id,
                   KnowledgeChunk.index_version == (document.pending_index_version or document.active_index_version))
            .order_by(KnowledgeChunk.chunk_index)
        )
    )
    child_count = sum(1 for item in chunks if (item.attributes or {}).get("chunk_kind", "child") == "child")
    payload = serialize_knowledge_document(document, chunk_count=child_count, include_content=True)
    payload["chunks"] = [
        {
            "id": item.id,
            "index": item.chunk_index,
            "kind": (item.attributes or {}).get("chunk_kind", "child"),
            "content": item.content,
            "parentChunkId": item.parent_chunk_id,
            "pageNumbers": (item.attributes or {}).get("pageNumbers") or [],
        }
        for item in chunks
    ]
    return payload


def retrieval_public_fields(base: KnowledgeBase) -> dict[str, Any]:
    strategy = normalize_strategy(getattr(base, "retrieval_strategy", None))
    config = merge_retrieval_config(
        getattr(base, "retrieval_config", None),
        chunk_size=base.chunk_size,
        chunk_overlap=base.chunk_overlap,
    )
    return {
        "retrievalStrategy": strategy,
        "retrievalStrategyLabel": STRATEGY_LABELS.get(strategy, STRATEGY_LABELS["full"]),
        "retrievalConfig": config,
        "aIModelsId": None if base.inherits_embedding_model else base.embedding_model_id,
        "embeddingModelId": None if base.inherits_embedding_model else base.embedding_model_id,
        "aIRerankModelsId": None if base.inherits_rerank_model else base.rerank_model_id,
        "rerankModelId": None if base.inherits_rerank_model else base.rerank_model_id,
        "effectiveEmbeddingModelId": base.embedding_model_id,
        "effectiveRerankModelId": base.rerank_model_id,
        "chunkSize": base.chunk_size,
        "chunkOverlap": base.chunk_overlap,
        "maxTokensPerParagraph": base.chunk_size,
        "maxTokensPerLine": base.chunk_size,
        "overlappingTokens": base.chunk_overlap,
    }


async def _search_dense(
    *,
    base: KnowledgeBase,
    tenant_id: int,
    query: str,
    limit: int,
    filters: dict[str, object] | None,
    session: AsyncSession,
    before_external=None,
    allowed_chunk_ids=None,
    document_versions=None,
) -> tuple[list[VectorHit], str]:
    if not base.embedding_model_id:
        return [], "not_configured"
    embedding_model = await session.get(AIModel, base.embedding_model_id)
    if (
        base.tenant_id != tenant_id or embedding_model is None
        or embedding_model.tenant_id != tenant_id or embedding_model.model_type != "embedding"
        or not embedding_model.enabled
    ):
        return [], "not_configured"
    settings = get_settings()
    try:
        async with SessionLocal() as current:
            _, current_base = await require_runtime_resource(current, tenant_id, base.id, "knowledge_bases",
                                                             KnowledgeBase, require_dedicated=True)
            from eic.knowledge.model_defaults import bind_missing_defaults
            await bind_missing_defaults(current, current_base, require_embedding=True)
            if current_base.embedding_model_id != base.embedding_model_id:
                raise RuntimeReferenceUnavailable("runtime_context_changed")
            embedding_model = await require_runtime_model(current, tenant_id, embedding_model.id, "embedding")
        validate_embedding_model(embedding_model, tenant_id)
        if before_external is not None:
            await before_external()
        vectors = await embed_texts(embedding_model, [query], tenant_id=tenant_id)
        if not vectors:
            return [], "unavailable"
        vector_url = settings.vector_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        from eic.core.dependency_controls import guard
        async with guard("vector", tenant_id, model_id=base.id, endpoint=vector_url):
            async with vector_connection(vector_url) as connection:
                store = PgVectorStore(dimensions=embedding_model.embedding_dimensions or len(vectors[0]))
                scope = {"document_versions": document_versions} if document_versions is not None else {}
                hits = await store.search(
                    connection, tenant_id=tenant_id, knowledge_base_id=base.id,
                    vector=vectors[0], limit=limit, chunk_ids=allowed_chunk_ids,
                    **scope,
                )
        from eic.knowledge.retrieval import filter_by_metadata

        return filter_by_metadata(hits, filters), "ok"
    except RuntimeReferenceUnavailable:
        raise
    except (ModelProviderError, OSError, RuntimeError, ValueError, asyncpg.PostgresError):
        return [], "unavailable"


@observe_retrieval
async def retrieve_knowledge(
    session: AsyncSession,
    *,
    tenant_id: int,
    base: KnowledgeBase,
    query: str,
    extra: dict[str, Any] | None = None,
    filters: dict[str, object] | None = None,
    limit: int | None = None,
    before_external=None,
) -> dict[str, Any]:
    from eic.knowledge.retrieval_graph import run_retrieval_graph

    return await run_retrieval_graph(
        session=session, tenant_id=tenant_id, base=base, query=query,
        extra=extra, filters=filters, limit=limit, before_external=before_external,
    )


async def _authorize_retrieval(
    session, *, tenant_id, base, query, extra, filters, limit, before_external,
):
    try:
        if type(tenant_id) is not int or tenant_id < 1 or base.tenant_id != tenant_id:
            raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
        await require_runtime_resource(session, tenant_id, base.id, "knowledge_bases", KnowledgeBase,
                                       require_dedicated=True)
    except RuntimeReferenceUnavailable:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
    settings = settings_from_knowledge_base(base, extra)
    from eic.knowledge.model_defaults import bind_missing_defaults
    await bind_missing_defaults(session, base, require_embedding=settings.uses_vector,
                                require_rerank=settings.uses_rerank, allow_unconfigured=True)
    if limit:
        settings.config["topK"] = int(limit)
        settings.config["finalLimit"] = int(limit)
    from eic.knowledge.candidates import allowed_documents
    documents = await allowed_documents(session, tenant_id, base.id)
    return {"settings": settings, "documents": documents}


async def _candidate_scope(session, state, tenant_id, base_id, filters):
    from eic.knowledge.sparse_index import metadata_scope, scope_query
    return await metadata_scope(session,
        scope_query(tenant_id, base_id, list(state["documents"])), filters)


async def _recall_dense(state, *, session, tenant_id, base, query, filters, before_external):
    settings = state["settings"]
    documents = state["documents"]

    dense_hits: list[VectorHit] = []
    dense_status = "not_configured"
    if settings.uses_vector:
        from eic.knowledge.indexing import parameter_snapshot
        model = await session.get(AIModel, base.embedding_model_id) if base.embedding_model_id else None
        expected = parameter_snapshot(base, model)
        runs = {r.id: r for r in await _scoped_rows_by_id(session, KnowledgeIngestionRun,
            {d.active_index_version for d in documents.values() if d.active_index_version}, tenant_id)}
        versions = {document.id: document.active_index_version for document in documents.values()
                     if (run := runs.get(document.active_index_version))
                     and run.status == "ready"
                     and run.parameters.get("embedding") == expected["embedding"]
                     and run.parameters.get("embeddingModelId") == expected["embeddingModelId"]}
        statement = await _candidate_scope(session, state, tenant_id, base.id, filters)
        # 有过滤条件时只传轻量 ID；无过滤时 PG 按文档/活动版本筛选后取 TopK。
        dense_ids = [str(identity) for identity in await session.scalars(statement)] if filters else None
        if versions and (dense_ids is None or dense_ids):
            dense_hits, dense_status = await _search_dense(
                base=base,
                tenant_id=tenant_id,
                query=query,
                limit=int(settings.config.get("vectorTopK") or settings.limit),
                filters=filters,
                session=session,
                before_external=before_external,
                allowed_chunk_ids=dense_ids,
                document_versions=versions,
            )
        elif base.embedding_model_id:
            dense_status = "reindex_required"
        from eic.knowledge.candidates import hydrate_hits
        statement = statement.where(KnowledgeChunk.document_id.in_(versions))
        dense_hits = await hydrate_hits(session, statement, dense_hits, filters)

    return {"dense_hits": dense_hits, "dense_status": dense_status}


async def _recall_sparse(state, *, query, session, tenant_id, base, filters):
    settings = state["settings"]

    sparse_hits: list[VectorHit] = []
    status = "skipped"
    if settings.uses_bm25:
        from eic.knowledge.sparse_index import search_index
        from eic.knowledge.candidates import hydrate_hits
        statement = await _candidate_scope(session, state, tenant_id, base.id, filters)
        sparse_hits, status = await search_index(session, statement, query,
            limit=int(settings.config.get("bm25TopK") or settings.limit))
        sparse_hits = await hydrate_hits(session, statement, sparse_hits, filters)
    return {"sparse_hits": sparse_hits, "sparse_status": status}


async def _fuse_retrieval(state):
    return {"fusion_hits": fusion_candidates(
        state["settings"], state["dense_hits"], state["sparse_hits"],
    )}


async def _rerank_retrieval(state, *, session, tenant_id, base, query, before_external):
    settings = state["settings"]
    dense_status = state["dense_status"]

    rerank_status = "skipped"
    reranked_hits = None
    if settings.strategy == STRATEGY_BASIC and dense_status != "ok":
        pass
    else:
        if settings.uses_rerank:
            rerank_model = None
            if base.rerank_model_id:
                candidate = await session.get(AIModel, base.rerank_model_id)
                if candidate is not None and candidate.tenant_id == tenant_id and candidate.model_type == "rerank" and candidate.enabled:
                    rerank_model = candidate
            fused_for_rerank = state["fusion_hits"]
            if rerank_model is not None and fused_for_rerank:
                try:
                    async with SessionLocal() as current:
                        _, current_base = await require_runtime_resource(
                            current, tenant_id, base.id, "knowledge_bases", KnowledgeBase, require_dedicated=True,
                        )
                        from eic.knowledge.model_defaults import bind_missing_defaults
                        await bind_missing_defaults(current, current_base, require_rerank=True)
                        if current_base.rerank_model_id != base.rerank_model_id:
                            raise RuntimeReferenceUnavailable("runtime_context_changed")
                        rerank_model = await require_runtime_model(current, tenant_id, rerank_model.id, "rerank")
                    if before_external is not None:
                        await before_external()
                    reranked_hits = await rerank_hits(
                        rerank_model,
                        query,
                        fused_for_rerank,
                        limit=int(settings.config.get("rerankTopN") or settings.limit),
                    )
                    rerank_status = "ok"
                except ModelProviderError:
                    reranked_hits = fused_for_rerank
                    rerank_status = "skipped"
            else:
                reranked_hits = fused_for_rerank
                rerank_status = "skipped"
    return {"reranked_hits": reranked_hits, "rerank_status": rerank_status}


async def _finalize_retrieval(state, *, base, query, filters, session=None, tenant_id=None):
    settings = state["settings"]
    dense_hits, sparse_hits = state["dense_hits"], state["sparse_hits"]
    dense_status, rerank_status = state["dense_status"], state["rerank_status"]
    hits = [] if settings.strategy == STRATEGY_BASIC and dense_status != "ok" else fuse_strategy_hits(
        query, settings=settings, dense_hits=dense_hits, sparse_hits=sparse_hits,
        reranked_hits=state["reranked_hits"],
    )

    parent_contents = state.get("parent_contents", {})
    if session is not None:
        from eic.knowledge.candidates import allowed_documents, hydrate_hits, parent_contents as load_parents
        # 独立事务避免 MySQL REPEATABLE READ 沿用外呼前的权限及版本快照。
        async with SessionLocal() as verification:
            current = {"documents": await allowed_documents(verification, tenant_id, base.id)}
            statement = await _candidate_scope(verification, current, tenant_id, base.id, filters)
            local = {hit.point_id: hit for hit in await hydrate_hits(verification, statement, hits, filters)}
            hits = [VectorHit(hit.point_id, hit.score, {**local[hit.point_id].payload,
                **{key: value for key, value in hit.payload.items()
                   if key in {"fusionScore", "rerankScore", "scoreSpace"}}}) for hit in hits if hit.point_id in local]
            parent_contents = await load_parents(verification, statement, hits)
    dense_scores, sparse_scores = score_maps(dense_hits, sparse_hits)
    results = build_result_payload(
        hits,
        parent_contents=parent_contents,
        documents=state["documents"],
        settings=settings,
        dense_scores=dense_scores,
        sparse_scores=sparse_scores,
        knowledge_base=base,
    )
    if any(row.get("knowledgeBaseId") != base.id for row in results):
        raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
    return {
        "query": query,
        "filters": filters or {},
        "strategy": settings.strategy,
        "strategyLabel": settings.label,
        "config": settings.config,
        "denseStatus": dense_status,
        "rerankStatus": rerank_status,
        "sparseCount": len(sparse_hits),
        "sparseStatus": state.get("sparse_status", "unknown"),
        "denseCount": len(dense_hits),
        "results": results,
    }


async def validate_knowledge_model_bindings(
    session: AsyncSession, tenant_id: int, payload: dict[str, Any],
    base: KnowledgeBase | None = None,
) -> None:
    from fastapi import HTTPException

    for aliases, attribute, expected_type in (
        (("aIModelsId", "embeddingModelId"), "embedding_model_id", "embedding"),
        (("aIRerankModelsId", "rerankModelId"), "rerank_model_id", "rerank"),
    ):
        supplied = [payload[key] for key in aliases if key in payload]
        if not supplied:
            supplied = [getattr(base, attribute, None)]
        normalized = []
        for value in supplied:
            if isinstance(value, bool):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "模型 ID 无效"})
            if value in (None, "", 0, "0"):
                normalized.append(None)
                continue
            try:
                if not str(value).strip().isdigit() or not 0 < int(value) < 2**63:
                    raise ValueError("invalid model id")
                normalized.append(int(value))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "模型 ID 无效"}) from exc
        if len(set(normalized)) != 1:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "模型别名字段不一致"})
        model_id = normalized[0]
        if any(key in payload for key in aliases):
            payload["inheritsEmbeddingModel" if expected_type == "embedding" else "inheritsRerankModel"] = model_id is None
        if model_id is None:
            from eic.models import TenantModelDefaults
            defaults = await session.get(TenantModelDefaults, tenant_id)
            model_id = getattr(defaults, attribute, None) if defaults else None
            if attribute == "embedding_model_id" and base and base.embedding_model_id:
                # 清除覆盖表示使用默认；已有索引不能因此隐式换向量空间。
                active = await session.scalar(select(KnowledgeDocument.id).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.knowledge_base_id == base.id,
                    KnowledgeDocument.is_deleted.is_(False),
                    KnowledgeDocument.active_index_version.is_not(None)).limit(1))
                if active:
                    model_id = base.embedding_model_id
            if model_id is None:
                continue
            payload[aliases[0]] = model_id
            payload[aliases[1]] = model_id
        model = await session.get(AIModel, model_id)
        if model is None or model.tenant_id != tenant_id or model.model_type != expected_type or not model.enabled:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "模型不属于当前租户、类型不匹配或已停用"})
        if expected_type == "embedding":
            from eic.vector.contracts import ProjectionContractError, validate_projection_dimensions
            try:
                validate_projection_dimensions(model.embedding_dimensions)
            except ProjectionContractError as exc:
                raise HTTPException(status_code=400, detail={"code": 400, "reason": str(exc),
                    "errMsg": "向量模型维度不受当前投影索引支持，请先迁移向量存储合同"}) from None


def apply_knowledge_payload(knowledge_base: KnowledgeBase, payload: dict[str, Any]) -> None:
    if "inheritsEmbeddingModel" in payload:
        knowledge_base.inherits_embedding_model = payload["inheritsEmbeddingModel"] is True
    if "inheritsRerankModel" in payload:
        knowledge_base.inherits_rerank_model = payload["inheritsRerankModel"] is True
    knowledge_base.name = str(payload.get("name") or knowledge_base.name or "")
    embedding_id = payload.get("aIModelsId", payload.get("embeddingModelId"))
    rerank_id = payload.get("aIRerankModelsId", payload.get("rerankModelId"))
    if embedding_id not in (None, "", 0, "0"):
        knowledge_base.embedding_model_id = int(embedding_id)
    elif "aIModelsId" in payload or "embeddingModelId" in payload:
        knowledge_base.embedding_model_id = None
    if rerank_id not in (None, "", 0, "0"):
        knowledge_base.rerank_model_id = int(rerank_id)
    elif "aIRerankModelsId" in payload or "rerankModelId" in payload:
        knowledge_base.rerank_model_id = None
    chunk_size = payload.get("maxTokensPerParagraph") or payload.get("chunkSize") or knowledge_base.chunk_size or 1200
    chunk_overlap = payload.get("overlappingTokens") if payload.get("overlappingTokens") is not None else payload.get("chunkOverlap", knowledge_base.chunk_overlap)
    knowledge_base.chunk_size = int(chunk_size or 1200)
    knowledge_base.chunk_overlap = int(chunk_overlap or 0)
    if payload.get("retrievalStrategy") or payload.get("retrieval_strategy"):
        knowledge_base.retrieval_strategy = normalize_strategy(payload.get("retrievalStrategy") or payload.get("retrieval_strategy"))
    elif not knowledge_base.retrieval_strategy:
        knowledge_base.retrieval_strategy = "full"
    config = merge_retrieval_config(
        knowledge_base.retrieval_config,
        payload.get("retrievalConfig") or payload.get("retrieval_config"),
        {
            "childSize": knowledge_base.chunk_size,
            "childOverlap": knowledge_base.chunk_overlap,
            "maxTokensPerParagraph": knowledge_base.chunk_size,
            "overlappingTokens": knowledge_base.chunk_overlap,
        },
        chunk_size=knowledge_base.chunk_size,
        chunk_overlap=knowledge_base.chunk_overlap,
    )
    knowledge_base.chunk_size = int(config["childSize"])
    knowledge_base.chunk_overlap = int(config["childOverlap"])
    knowledge_base.retrieval_config = config


def parse_id_list(value: Any) -> list[Any]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                loaded = json.loads(text)
                return loaded if isinstance(loaded, list) else [text]
            except json.JSONDecodeError:
                return [part.strip() for part in text.split(",") if part.strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, list):
        return value
    return [value]


async def evaluate_knowledge_set(
    session: AsyncSession,
    *,
    tenant_id: int,
    base: KnowledgeBase,
    cases: list[Any],
    k: int,
) -> dict[str, Any]:
    from eic.knowledge.langchain_entrypoint import retrieve_knowledge as framework_retrieve

    case_results = []
    for item in cases:
        query = str(getattr(item, "query", None) or item.get("query") or "").strip()
        expected_documents = getattr(item, "expected_document_ids", None)
        if expected_documents is None and isinstance(item, dict):
            expected_documents = item.get("expectedDocumentIds") or item.get("expected_document_ids")
        expected_chunks = getattr(item, "expected_chunk_ids", None)
        if expected_chunks is None and isinstance(item, dict):
            expected_chunks = item.get("expectedChunkIds") or item.get("expected_chunk_ids")
        retrieved = await framework_retrieve(session, tenant_id=tenant_id, base=base, query=query, limit=k)
        metrics = evaluate_case(retrieved["results"], expected_documents, expected_chunks, k=k)
        metrics.update({
            "id": getattr(item, "id", None) if not isinstance(item, dict) else item.get("id"),
            "query": query,
            "expectedDocumentIds": parse_id_list(expected_documents),
            "expectedChunkIds": parse_id_list(expected_chunks),
            "results": retrieved["results"],
        })
        case_results.append(metrics)
    summary = aggregate_eval_scores(case_results)
    summary.update({"k": k, "cases": case_results, "strategy": normalize_strategy(base.retrieval_strategy)})
    return summary

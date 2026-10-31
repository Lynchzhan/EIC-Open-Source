"""关系库负责版本切换；PGVector 仅为可重建投影，不跨库假装原子提交。"""
import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import defer

from eic.ai.embeddings import (
    effective_embedding_batch_size, embed_texts, validate_embedding_model, validate_embedding_texts,
)
from eic.ai.providers import ModelProviderError
from eic.ai.stream_reader import complete_cleanup
from eic.core import observability, outbox
from eic.core.config import get_settings
from eic.core.database import SessionLocal
from eic.core.file_access import can_bind_shared_file, file_source_path
from eic.core.ids import new_id
from eic.core.langfuse_context import ingestion_context
from eic.core.runtime_refs import require_runtime_model, require_runtime_resource
from eic.core.runtime_io import vector_connection
from eic.knowledge.documents import extract_document
from eic.knowledge.ingestion_documents import (
    IngestionDocumentLoader,
    ParentChildDocumentTransformer,
    ingestion_chunk_from_document,
)
from eic.knowledge.mineru import MinerUError, extraction_parameters
from eic.knowledge.strategy import chunk_settings_from_base, merge_retrieval_config
from eic.knowledge.text_splitters import splitter_parameter_snapshot
from eic.models import AIModel, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeIngestionRun, Resource
from eic.vector.contracts import ProjectionContractError, check_projection_dimensions
from eic.vector.pgvector_store import ChunkRecord, PgVectorStore


class IndexCancelled(Exception):
    """Raised when a queued or running document receives a graceful stop request."""


def ingestion_settings(base) -> dict[str, int]:
    config = merge_retrieval_config(getattr(base, "retrieval_config", None))
    return {
        key: int(config[key])
        for key in (
            "indexConcurrency", "maxChunksPerDocument", "embeddingQuota",
            "embeddingBatchSize", "embeddingBatchTokens", "embeddingRetryCount",
            "embeddingRequestTimeout",
        )
    }


def parameter_snapshot(base, model):
    return {
        **({"pdfExtraction": extraction_parameters(get_settings())}
           if get_settings().pdf_extractor == "mineru" else {}),
        "chunks": list(chunk_settings_from_base(base)),
        "textSplitter": splitter_parameter_snapshot(),
        "ingestion": ingestion_settings(base),
        "embeddingModelId": base.embedding_model_id,
        "embedding": {
            "provider": model.provider, "endpoint": model.endpoint,
            "model": model.model_name, "dimensions": model.embedding_dimensions,
            "credentialHash": hashlib.sha256((model.secret_encrypted or "").encode()).hexdigest(),
        } if model else None,
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


async def current_inputs(session, document_id, tenant_id, *, include_content=True):
    document = await session.get(KnowledgeDocument, document_id, populate_existing=True,
        options=[] if include_content else [defer(KnowledgeDocument.content, raiseload=True)])
    if document is None or document.tenant_id != tenant_id or document.is_deleted:
        raise ValueError("document_unavailable")
    resource, base = await require_runtime_resource(
        session, tenant_id, document.knowledge_base_id, "knowledge_bases",
        KnowledgeBase, require_dedicated=True,
    )
    from eic.knowledge.model_defaults import bind_missing_defaults
    await bind_missing_defaults(session, base, require_embedding=True, persist=True)
    await session.flush()
    model = await session.get(AIModel, base.embedding_model_id, populate_existing=True) if base.embedding_model_id else None
    if base.embedding_model_id:
        validate_embedding_model(model, tenant_id)
    if document.file_id:
        file = await session.get(Resource, document.file_id, populate_existing=True)
        if not can_bind_shared_file(file, tenant_id):
            raise ValueError("file_unavailable")
    return document, resource, base, model


async def valid_run(session, document_id, tenant_id, version, *, include_content=True):
    document, resource, base, model = await current_inputs(
        session, document_id, tenant_id, include_content=include_content)
    run = await session.get(KnowledgeIngestionRun, version, populate_existing=True)
    expected_parameters = parameter_snapshot(base, model)
    stored_parameters = dict(run.parameters or {}) if run is not None else {}
    if document.status == "cancelling" or (document.index_error or {}).get("code") == "cancel_requested":
        raise IndexCancelled("cancelled_by_user")
    # 兼容迁移前已创建的 run；新 run 仍完整比较入库参数快照。
    parameters_match = (
        stored_parameters == expected_parameters
        if "ingestion" in stored_parameters
        else stored_parameters == {key: value for key, value in expected_parameters.items() if key != "ingestion"}
    )
    if (run is None or run.document_id != document_id or run.tenant_id != tenant_id
            or document.pending_index_version != version
            or document.status == "cancelling"
            or (document.index_error or {}).get("code") == "cancel_requested"
            or run.status in {"failed", "uncertain", "superseded", "cancelled"}
            or run.source_revision != document.source_revision
            or not parameters_match):
        raise ValueError("index_inputs_changed")
    if run.lease_until and run.lease_until.replace(tzinfo=UTC) <= datetime.now(UTC):
        raise ValueError("index_lease_expired")
    if document.file_id:
        file = await session.get(Resource, document.file_id, populate_existing=True)
        if not can_bind_shared_file(file, tenant_id):
            raise ValueError("file_unavailable")
    return document, resource, base, model, run


async def record_failure(document_id, tenant_id, version, code, stage, *, extraction_report=None,
                         preflight_revision=None, preflight_pending=None):
    if version is None:
        if preflight_revision is None:
            return
        async with SessionLocal() as session:
            # 仅结束本次已观察的排队输入；旧任务不能覆盖新修订、在途版本或已就绪文档。
            result = await session.execute(update(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id, KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.is_deleted.is_(False),
                KnowledgeDocument.source_revision == preflight_revision,
                KnowledgeDocument.pending_index_version == preflight_pending,
                KnowledgeDocument.status.in_(["queued", "queued_text"]),
            ).values(status="uncertain" if code == "interrupted" else "failed",
                     index_status="uncertain" if code == "interrupted" else "failed",
                     parse_status="failed", error_message=code,
                     index_error={"stage": stage, "code": code, "automaticRetry": False}))
            if result.rowcount:
                document = await session.get(KnowledgeDocument, document_id)
                await refresh_base_status(session, document.knowledge_base_id, tenant_id)
            await session.commit()
        return
    async with SessionLocal() as session:
        await new_id(session)
        document = await session.get(KnowledgeDocument, document_id)
        run = await session.get(KnowledgeIngestionRun, version) if version else None
        if run and run.tenant_id == tenant_id and run.document_id == document_id:
            if run.status in {"uncertain", "superseded", "cancelled", "ready"}:
                return
            run.status = "uncertain" if code == "interrupted" else "failed"
            run.error_message = code
            run.finished_at = datetime.now(UTC)
            run.lease_until = None
            await outbox.stage(session, "index-cleanup", document_id, tenant_id,
                               event_key=f"index-failure-cleanup:{document_id}:{version}")
        if (version is not None and document and document.tenant_id == tenant_id
                and not document.is_deleted and document.pending_index_version == version):
            document.status = "uncertain" if code == "interrupted" else "failed"
            document.index_status = document.status
            document.error_message = {
                "vector_store_failed:vector_dimension_mismatch":
                    "模型向量维度与当前向量表不兼容，请管理员完成投影迁移后再入库",
                "vector_store_failed:embedding_dimensions_invalid": "模型向量维度配置无效",
                "vector_store_failed:vector_index_dimensions_unsupported":
                    "当前 HNSW 向量投影只支持最多 2000 维，需显式迁移索引合同后再入库",
            }.get(code, code)
            if code == "embedding_failed:embedding_batch_limit":
                from eic.ai.langchain_embeddings import EMBEDDING_BATCH_LIMIT_MESSAGE
                document.error_message = EMBEDDING_BATCH_LIMIT_MESSAGE
            document.index_error = {"stage": stage, "code": code, "automaticRetry": False}
            if extraction_report is not None:
                document.extraction_report = extraction_report
            if stage == "parse":
                document.parse_status = "failed"
            else:
                document.embedding_status = "failed"
            await refresh_base_status(session, document.knowledge_base_id, tenant_id)
        await session.commit()


async def record_cancelled(document_id, tenant_id, version):
    async with SessionLocal() as session:
        await new_id(session)
        document = await session.get(KnowledgeDocument, document_id)
        run = await session.get(KnowledgeIngestionRun, version) if version else None
        if run and run.tenant_id == tenant_id and run.document_id == document_id:
            run.status = "cancelled"
            run.error_message = "cancelled_by_user"
            run.finished_at = datetime.now(UTC)
            run.lease_until = None
            await outbox.stage(session, "index-cleanup", document_id, tenant_id,
                               event_key=f"index-cancel-cleanup:{document_id}:{version}")
        if (document and document.tenant_id == tenant_id and not document.is_deleted
                and document.pending_index_version == version
                and document.status in {"cancelling", "cancelled"}):
            document.status = "cancelled"
            document.index_status = "cancelled"
            document.pending_index_version = None
            document.error_message = "cancelled_by_user"
            document.index_error = {"code": "cancelled_by_user", "automaticRetry": False}
            await refresh_base_status(session, document.knowledge_base_id, tenant_id)
        await session.commit()


async def ensure_document_active(document_id, tenant_id):
    async with SessionLocal() as session:
        document = await session.get(KnowledgeDocument, document_id, populate_existing=True,
            options=[defer(KnowledgeDocument.content, raiseload=True)])
        if (document is None or document.tenant_id != tenant_id or document.is_deleted
                or document.status in {"cancelling", "cancelled", "deleted"}
                or (document.index_error or {}).get("code") == "cancel_requested"):
            raise IndexCancelled("cancelled_by_user")


def embedding_batches(children, *, batch_size: int, batch_tokens: int):
    batch = []
    tokens = 0
    for chunk in children:
        # Avoid adding a tokenizer dependency; this conservative estimate only
        # controls request size and never changes the stored chunk content.
        estimated = max(1, len((chunk.content or "").encode("utf-8")))
        if batch_tokens and estimated > batch_tokens:
            raise ValueError("single_chunk_exceeds_batch_tokens")
        if batch and (len(batch) >= batch_size or (batch_tokens and tokens + estimated > batch_tokens)):
            yield batch
            batch, tokens = [], 0
        batch.append(chunk)
        tokens += estimated
    if batch:
        yield batch


async def child_pages(document_id, tenant_id, version):
    """按版本内唯一的 chunk_index 翻页，不跨外呼持有关系库连接。"""
    after = -1
    while True:
        async with SessionLocal() as session:
            rows = list((await session.execute(select(
                KnowledgeChunk.id, KnowledgeChunk.knowledge_base_id, KnowledgeChunk.chunk_index,
                KnowledgeChunk.content, KnowledgeChunk.attributes.label("attributes"),
            ).where(
                KnowledgeChunk.document_id == document_id, KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.index_version == version, KnowledgeChunk.chunk_index > after,
            ).order_by(KnowledgeChunk.chunk_index).limit(128))).all())
        if not rows:
            return
        after = rows[-1].chunk_index
        yield rows


def fingerprint_children(checksum, children):
    for chunk in children:
        checksum.update(json.dumps(
            [chunk.id, chunk.knowledge_base_id, chunk.chunk_index, chunk.content, chunk.attributes],
            sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8"))
        checksum.update(b"\n")


async def preflight_children(document_id, tenant_id, version, settings):
    """完整扫描后才允许计费；只保留一页文本和累计指纹。"""
    count, checksum = 0, hashlib.sha256()
    async for rows in child_pages(document_id, tenant_id, version):
        await ensure_document_active(document_id, tenant_id)
        validate_embedding_texts([chunk.content for chunk in rows])
        # 与请求分批共用单块字节上限校验，不保留全部批次。
        for _ in embedding_batches(rows, batch_size=128, batch_tokens=settings["embeddingBatchTokens"]):
            pass
        count += len(rows)
        if settings["maxChunksPerDocument"] and count > settings["maxChunksPerDocument"]:
            raise ValueError("chunk_limit_exceeded")
        if settings["embeddingQuota"] and count > settings["embeddingQuota"]:
            raise ValueError("embedding_quota_exceeded")
        fingerprint_children(checksum, rows)
    if not count:
        raise ValueError("no_child_chunks")
    return count, checksum.hexdigest()


async def child_batches(document_id, tenant_id, version, settings):
    batch, tokens = [], 0
    batch_size = min(128, effective_embedding_batch_size(settings["embeddingBatchSize"]))
    token_limit = settings["embeddingBatchTokens"]
    async for rows in child_pages(document_id, tenant_id, version):
        validate_embedding_texts([chunk.content for chunk in rows])
        for chunk in rows:
            estimated = max(1, len(chunk.content.encode("utf-8")))
            if token_limit and estimated > token_limit:
                raise ValueError("single_chunk_exceeds_batch_tokens")
            if batch and (len(batch) >= batch_size or (token_limit and tokens + estimated > token_limit)):
                yield batch
                batch, tokens = [], 0
            batch.append(chunk)
            tokens += estimated
    if batch:
        yield batch


async def embed_children_with_policy(model, batches, *, total, tenant_id, document_id, settings, progress):
    batch_size = min(128, effective_embedding_batch_size(settings["embeddingBatchSize"]))
    completed = 0
    async for batch in batches:
        if completed + len(batch) > total:
            raise ValueError("index_chunks_changed")
        last_error = None
        for attempt in range(settings["embeddingRetryCount"] + 1):
            await ensure_document_active(document_id, tenant_id)
            await progress(completed, total, False)
            try:
                batch_vectors = await embed_texts(
                    model, [chunk.content for chunk in batch],
                    tenant_id=tenant_id, timeout=settings["embeddingRequestTimeout"],
                    batch_size=batch_size, ingestion=True,
                )
                last_error = None
                break
            except ModelProviderError as exc:
                last_error = exc
                # 网关 5xx 不能证明上游未执行；仅对明确限流拒绝重试。
                if (exc.reason != "embedding_http_429"
                        or attempt >= settings["embeddingRetryCount"]):
                    raise
                await asyncio.sleep(min(30, 2 ** attempt))
        if last_error is not None:
            raise last_error
        yield batch, batch_vectors
        completed += len(batch)


async def await_document_capacity(session, document_id, tenant_id):
    """在同一 allocator 写事务中核对 KB 并发，跨进程共享运行租约。"""
    while True:
        await new_id(session)
        document = await session.get(KnowledgeDocument, document_id, populate_existing=True,
            options=[defer(KnowledgeDocument.content, raiseload=True)])
        if not document or document.tenant_id != tenant_id or document.is_deleted:
            return
        if document.status in {"cancelling", "cancelled"}:
            return
        base = await session.get(KnowledgeBase, document.knowledge_base_id, populate_existing=True)
        if base is None:
            return
        active = await session.scalar(select(func.count()).select_from(KnowledgeIngestionRun).join(
            KnowledgeDocument, KnowledgeDocument.pending_index_version == KnowledgeIngestionRun.id,
        ).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == document.knowledge_base_id,
            KnowledgeDocument.id != document_id,
            KnowledgeIngestionRun.status.in_(["parsing", "embedding"]),
            KnowledgeIngestionRun.lease_until > datetime.now(UTC),
        ))
        if active < ingestion_settings(base)["indexConcurrency"]:
            return
        await session.rollback()
        await asyncio.sleep(0.5)


async def refresh_base_status(session, knowledge_base_id, tenant_id):
    resource = await session.get(Resource, knowledge_base_id)
    base = await session.get(KnowledgeBase, knowledge_base_id)
    if not resource or not base or resource.tenant_id != tenant_id or base.tenant_id != tenant_id:
        return
    data = resource.data or {}
    if data.get("indexStatus") == "building":
        return
    # 此事务中刚修改的 document 也必须参与汇总。
    await session.flush()
    documents = list((await session.execute(select(
        KnowledgeDocument.index_status, KnowledgeDocument.active_source_revision, KnowledgeDocument.source_revision,
    ).where(
        KnowledgeDocument.knowledge_base_id == knowledge_base_id,
        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.is_deleted.is_(False)))).all())
    states = {d.index_status if (d.index_status in {"failed", "uncertain"}
                                or d.active_source_revision == d.source_revision) else "pending"
              for d in documents}
    if not documents:
        state = "empty"
    elif states == {"ready"}:
        state = "ready"
    elif states <= {"ready", "lexical_only"}:
        state = "lexical_only"
    elif states & {"failed", "uncertain"}:
        state = "uncertain" if "uncertain" in states else "failed"
    else:
        state = "pending"
    base.status = state
    resource.data = {**data, "indexStatus": state, "documentCount": len(documents)}


async def activate(session, document, run, *, lexical=False):
    document.active_index_version = run.id
    document.active_source_revision = run.source_revision
    document.pending_index_version = None
    document.status = "parsed" if lexical else "ready"
    document.index_status = "lexical_only" if lexical else "ready"
    document.embedding_status = "not_configured" if lexical else "ready"
    document.error_message = ""
    document.index_error = {}
    run.status = document.status
    run.lease_until = None
    run.finished_at = datetime.now(UTC)
    await outbox.stage(session, "index-cleanup", document.id, document.tenant_id,
                       event_key=f"index-cleanup:{document.id}:{run.id}")
    await refresh_base_status(session, document.knowledge_base_id, document.tenant_id)


async def process_knowledge_document(ctx, document_id, tenant_id):
    version = None
    preflight_revision = None
    preflight_pending = None
    observation_token = None
    try:
        async with SessionLocal() as session:
            await await_document_capacity(session, document_id, tenant_id)
            document = await session.get(KnowledgeDocument, document_id, populate_existing=True)
            if document is None or document.tenant_id != tenant_id or document.is_deleted:
                raise ValueError("document_unavailable")
            if document.status == "cancelled":
                return {"status": "cancelled", "document_id": document_id}
            if document.status == "cancelling" or (document.index_error or {}).get("code") == "cancel_requested":
                version = document.pending_index_version
                raise IndexCancelled("cancelled_by_user")
            expected = ctx.get("source_revision")
            if expected is not None and expected != document.source_revision:
                return {"status": "superseded", "document_id": document_id}
            preflight_revision = document.source_revision
            preflight_pending = document.pending_index_version
            document, _, base, model = await current_inputs(session, document_id, tenant_id)
            parameters = parameter_snapshot(base, model)
            fingerprint = digest({"parameters": parameters, "revision": document.source_revision,
                                  "content": document.content, "file": document.file_id, "name": document.source_name})
            active = await session.get(KnowledgeIngestionRun, document.active_index_version)
            if (active and active.parameter_hash == fingerprint and active.status in {"ready", "parsed"}
                    and document.active_source_revision == document.source_revision):
                return {"status": document.status, "document_id": document_id,
                        "index_version": active.id, "unchanged": True}
            pending = await session.get(KnowledgeIngestionRun, document.pending_index_version) if document.pending_index_version else None
            if pending and (pending.source_revision != document.source_revision or pending.parameters != parameters):
                pending.status = "superseded"
                pending.lease_until = None
                document.pending_index_version = None
                pending = None
            if pending and pending.status in {"parsing", "embedding", "parsed", "staged"}:
                if pending.status in {"parsing", "embedding"}:
                    lease = pending.lease_until
                    if lease and lease.replace(tzinfo=UTC) <= datetime.now(UTC):
                        stage = pending.status
                        pending.status = "uncertain"
                        pending.lease_until = None
                        pending.finished_at = datetime.now(UTC)
                        document.status = document.index_status = "uncertain"
                        document.index_error = {"stage": stage, "code": "lease_expired", "automaticRetry": False}
                        await refresh_base_status(session, document.knowledge_base_id, tenant_id)
                        await session.commit()
                        return {"status": "uncertain", "document_id": document_id}
                return {"status": pending.status, "document_id": document_id, "index_version": pending.id}
            if pending and pending.status in {"failed", "uncertain"} and not ctx.get("controlled_retry"):
                return {"status": pending.status, "document_id": document_id, "automaticRetry": False}
            version = await new_id(session)
            run = KnowledgeIngestionRun(id=version, tenant_id=tenant_id, document_id=document_id,
                                        source_revision=document.source_revision, parameter_hash=fingerprint,
                                        parameters=parameters, status="parsing",
                                        lease_until=datetime.now(UTC) + timedelta(seconds=900))
            session.add(run)
            document.pending_index_version = version
            document.status = "processing"
            document.parse_status = "running"
            document.index_status = "building"
            await refresh_base_status(session, document.knowledge_base_id, tenant_id)
            file_record = await session.get(Resource, document.file_id) if document.file_id else None
            if document.file_id and not can_bind_shared_file(file_record, tenant_id):
                raise ValueError("file_unavailable")
            text = document.content
            report = dict(document.extraction_report or {})
            stored = bool(text.strip()) and (not file_record or "manual_edit" in report.get("methods", []))
            source = file_source_path(file_record) if file_record and not stored else None
            source_name = document.source_name
            kb_id = document.knowledge_base_id
            scope = ingestion_context(tenant_id=document.tenant_id,
                document_id=document.id, run_id=run.id)
            await session.commit()
            observation_token = observability.CONTEXT.set(scope)
        with observability.observe_ingestion_stage("document_parse"):
            loaded = await IngestionDocumentLoader(
                source_name=source_name, text=text, report=report, source=source,
                metadata={"tenant_id": tenant_id, "knowledge_base_id": kb_id,
                          "document_id": document_id, "indexVersion": str(version)},
                local_extractor=extract_document,
            ).aload()
            source_document = loaded[0]
            text = source_document.page_content
            report = source_document.metadata["extraction_report"]
            if not text.strip():
                raise ValueError("empty_document")
        with observability.observe_ingestion_stage("document_chunking"):
            parent_size, child_size, overlap = parameters["chunks"]
            chunk_documents = ParentChildDocumentTransformer(
                parent_size=parent_size, child_size=child_size, child_overlap=overlap,
            ).transform_documents(loaded)
            drafts = [ingestion_chunk_from_document(item) for item in chunk_documents]
            if not drafts:
                raise ValueError("empty_chunks")
        from eic.knowledge.chunk_writer import write_chunk_batches
        await write_chunk_batches(sessions=SessionLocal, validate=valid_run, drafts=drafts,
            source_metadata=source_document.metadata, source_name=source_name,
            document_id=document_id, tenant_id=tenant_id, base_id=kb_id, version=version)
        async with SessionLocal() as session:
            await new_id(session)
            document, _, base, model, run = await valid_run(session, document_id, tenant_id, version)
            document.content, document.extraction_report = text, report
            # 解析后的文本参与下次幂等判断，不把解析前空正文的 hash 当最终版本。
            run.parameter_hash = digest({"parameters": parameters, "revision": document.source_revision,
                                         "content": text, "file": document.file_id, "name": document.source_name})
            document.parse_status = "ready"
            document.embedding_status = "pending" if model else "not_configured"
            document.status = "parsed_pending_embedding" if model else "parsed"
            run.chunk_count = len(drafts)
            run.status = "parsed"
            run.lease_until = None
            embedding_event = None
            if model and not ctx.get("defer_activation"):
                embedding_event = await outbox.stage(session, "document-embeddings", document_id, tenant_id,
                                                       event_key=f"embedding:{document_id}:{version}")
            if not model and not ctx.get("defer_activation"):
                await activate(session, document, run, lexical=True)
            await session.commit()
        if model and ctx.get("defer_activation"):
            return await process_document_embeddings({**ctx, "index_version": version}, document_id, tenant_id)
        if embedding_event:
            await outbox.dispatch_one(embedding_event)
        return {"status": "parsed_pending_embedding" if model else "parsed",
                "document_id": document_id, "chunk_count": len(drafts), "index_version": version}
    except IndexCancelled:
        await record_cancelled(document_id, tenant_id, version)
        return {"status": "cancelled", "document_id": document_id}
    except asyncio.CancelledError as exc:
        await record_failure(document_id, tenant_id, version, "interrupted", "parse",
                             extraction_report=getattr(exc, "mineru_report", None),
                             preflight_revision=preflight_revision, preflight_pending=preflight_pending)
        raise
    except Exception as exc:
        import logging
        import traceback
        logging.getLogger(__name__).error(
            'Document ingestion failed exception_type=%s frames=%s', type(exc).__name__,
            [(frame.filename, frame.lineno, frame.name)
             for frame in traceback.extract_tb(exc.__traceback__)])
        from eic.knowledge.local_parser import LocalParseError
        code = ("parse_failed:local:" + exc.code if isinstance(exc, LocalParseError) else
                "parse_failed:mineru:" + exc.code if isinstance(exc, MinerUError) else
                "interrupted" if str(exc) == "index_lease_expired" else "parse_failed:" + type(exc).__name__)
        await record_failure(document_id, tenant_id, version, code, "parse",
                             extraction_report=getattr(exc, "mineru_report", None),
                             preflight_revision=preflight_revision, preflight_pending=preflight_pending)
        return {"status": "failed", "document_id": document_id}
    finally:
        if observation_token is not None:
            observability.CONTEXT.reset(observation_token)


async def process_document_embeddings(ctx, document_id, tenant_id):
    version = ctx.get("index_version")
    observation_token = None
    failure_stage = "embedding"
    projection_attempted, activated = False, False
    try:
        async with SessionLocal() as session:
            await await_document_capacity(session, document_id, tenant_id)
            document = await session.get(KnowledgeDocument, document_id, populate_existing=True,
                options=[defer(KnowledgeDocument.content, raiseload=True)])
            if document is None or document.tenant_id != tenant_id or document.is_deleted:
                raise ValueError("document_unavailable")
            if document.status == "cancelling" or (document.index_error or {}).get("code") == "cancel_requested":
                version = version or document.pending_index_version
                raise IndexCancelled("cancelled_by_user")
            if document.status == "cancelled":
                return {"status": "cancelled", "document_id": document_id}
            version = version or document.pending_index_version
            await current_inputs(session, document_id, tenant_id, include_content=False)
            if not version:
                return {"status": document.status, "document_id": document_id, "unchanged": True}
            document, _, base, model, run = await valid_run(
                session, document_id, tenant_id, version, include_content=False)
            if run.status in {"embedding", "failed", "uncertain", "staged"}:
                return {"status": run.status, "document_id": document_id}
            if run.status != "parsed":
                raise ValueError("chunks_not_parsed")
            validate_embedding_model(model, tenant_id)
            model = await require_runtime_model(session, tenant_id, model.id, "embedding")
            ingestion = ingestion_settings(base)
            run.status = document.embedding_status = "embedding"
            run.lease_until = datetime.now(UTC) + timedelta(seconds=900)
            scope = ingestion_context(tenant_id=document.tenant_id,
                document_id=document.id, run_id=run.id)
            await session.commit()
            observation_token = observability.CONTEXT.set(scope)
        async def progress(completed, total, waiting):
            async with SessionLocal() as session:
                await new_id(session)
                current, _, _, _, active = await valid_run(
                    session, document_id, tenant_id, version, include_content=False)
                active.lease_until = datetime.now(UTC) + timedelta(seconds=900)
                active.embedding_count = completed
                current.index_error = ({"code": "rate_budget_exceeded", "waiting": True,
                    "completedChunks": completed, "totalChunks": total} if waiting else {})
                await session.commit()
        total, expected_fingerprint = await preflight_children(document_id, tenant_id, version, ingestion)
        # 先检查现有投影维度，避免已知不兼容时仍付费调用 embedding。
        failure_stage = 'vector_store'
        await check_projection_dimensions(model.embedding_dimensions)
        failure_stage = 'embedding'
        completed, checksum = 0, hashlib.sha256()
        dimensions = model.embedding_dimensions
        store = None
        batches = child_batches(document_id, tenant_id, version, ingestion)
        embedded = embed_children_with_policy(model, batches, total=total, tenant_id=tenant_id,
            document_id=document_id, settings=ingestion, progress=progress)
        try:
            async for children, vectors in embedded:
                dimensions = dimensions or (len(vectors[0]) if vectors else 0)
                if (len(vectors) != len(children) or not dimensions
                        or completed + len(children) > total
                        or any(len(vector) != dimensions or any(not math.isfinite(v) for v in vector) for vector in vectors)):
                    raise ValueError("embedding_shape_invalid")
                async with SessionLocal() as session:
                    await valid_run(session, document_id, tenant_id, version, include_content=False)
                    await require_runtime_model(session, tenant_id, model.id, "embedding")
                failure_stage = "vector_store"
                vector_url = get_settings().vector_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
                async with vector_connection(vector_url) as connection:
                    if store is None:
                        store = PgVectorStore(dimensions)
                        await store.initialize(connection, dimensions)
                    # 仅本批持有连接和事务；模型外呼不占用 PG 池槽。
                    projection_attempted = True
                    async with connection.transaction():
                        await store.upsert_many(connection, [
                            ChunkRecord(str(chunk.id), tenant_id, chunk.knowledge_base_id, document_id,
                                chunk.content, vector, {**{k: v for k, v in (chunk.attributes or {}).items()
                                    if k != "parent_content"}, "indexVersion": str(version)})
                            for chunk, vector in zip(children, vectors, strict=True)
                        ])
                fingerprint_children(checksum, children)
                completed += len(children)
                failure_stage = "embedding"
        finally:
            async def close_projection():
                try:
                    await embedded.aclose()
                finally:
                    await batches.aclose()
            await complete_cleanup(close_projection())
        if completed != total or checksum.hexdigest() != expected_fingerprint:
            raise ValueError("index_chunks_changed")
        failure_stage = "activation"
        async with SessionLocal() as session:
            await new_id(session)
            document, _, _, model, run = await valid_run(
                session, document_id, tenant_id, version, include_content=False)
            await require_runtime_model(session, tenant_id, model.id, "embedding")
            run.embedding_count = completed
            run.lease_until = None
            if ctx.get("defer_activation"):
                run.status = "staged"
                document.status = "staged"
                document.index_status = "staged"
                document.embedding_status = "ready"
            else:
                await activate(session, document, run)
            await session.commit()
        activated = True
        return {"status": "staged" if ctx.get("defer_activation") else "ready",
                "document_id": document_id, "index_version": version, "embedding_count": completed}
    except IndexCancelled:
        await record_cancelled(document_id, tenant_id, version)
        return {"status": "cancelled", "document_id": document_id}
    except asyncio.CancelledError:
        await record_failure(document_id, tenant_id, version, "interrupted", failure_stage)
        raise
    except Exception as exc:
        if str(exc) == "index_lease_expired":
            code = "interrupted"
        elif isinstance(exc, ProjectionContractError):
            code = "vector_store_failed:" + str(exc)
        elif isinstance(exc, ModelProviderError):
            reason = getattr(exc, "reason", "")
            safe = {"embedding_timeout", "embedding_transport_error", "embedding_response_invalid",
                    "embedding_input_invalid", "embedding_batch_limit",
                    "embedding_sdk_error", "embedding_initialization_failed", "rate_budget_exceeded",
                    "cost_budget_exceeded", "concurrency_budget_exceeded", "budget_not_configured",
                    "circuit_open", "admission_wait_expired"}
            reason = reason if reason in safe or re.fullmatch(r"embedding_http_[1-5][0-9]{2}", reason) else "provider_error"
            code = "embedding_failed:" + reason
        else:
            code = failure_stage + "_failed:" + type(exc).__name__
        await record_failure(document_id, tenant_id, version, code, failure_stage)
        return {"status": "embedding_failed", "document_id": document_id}
    finally:
        try:
            if projection_attempted and not activated:
                async def stage_writer_cleanup():
                    async with SessionLocal() as session:
                        # 较早的取消清理可能已经执行；写入连接收敛后再登记独立清理意图。
                        await outbox.stage(session, "index-cleanup", document_id, tenant_id)
                        await session.commit()
                await complete_cleanup(stage_writer_cleanup())
        finally:
            if observation_token is not None:
                observability.CONTEXT.reset(observation_token)


async def cleanup_document_index(ctx, document_id, tenant_id):
    vector_url = get_settings().vector_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    after, count = 0, 0
    while True:
        async with SessionLocal() as session:
            document = await session.get(KnowledgeDocument, document_id,
                options=[defer(KnowledgeDocument.content, raiseload=True)])
            if document is None or document.tenant_id != tenant_id:
                return {"status": "missing"}
            query = select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == document_id,
                KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.id > after)
            if not document.is_deleted:
                protected = [document.active_index_version] if document.active_index_version is not None else []
                pending = await session.get(KnowledgeIngestionRun, document.pending_index_version) if document.pending_index_version else None
                if pending and pending.status in {"parsing", "embedding", "parsed", "staged"}:
                    protected.append(pending.id)
                if protected:
                    query = query.where(KnowledgeChunk.index_version.not_in(protected))
            ids = list(await session.scalars(query.order_by(KnowledgeChunk.id).limit(400)))
        if not ids:
            return {"status": "cleaned", "projection_count": count}
        async with vector_connection(vector_url) as connection:
            if await connection.fetchval("SELECT to_regclass('public.rag_vectors')"):
                await PgVectorStore().delete_chunks(connection, tenant_id=tenant_id, chunk_ids=[str(identity) for identity in ids])
        after, count = ids[-1], count + len(ids)


async def process_knowledge_base(ctx, knowledge_base_id, tenant_id):
    build_id = ctx.get("event_id")
    try:
        async with SessionLocal() as session:
            allocated = await new_id(session)
            resource, base = await require_runtime_resource(session, tenant_id, knowledge_base_id,
                "knowledge_bases", KnowledgeBase, require_dedicated=True)
            if resource is None:
                raise ValueError("resource_unavailable")
            build_id = build_id or allocated
            data = resource.data or {}
            if data.get("indexStatus") == "cancelled" and data.get("indexEventId") == str(build_id):
                return {"status": "cancelled"}
            if data.get("indexStatus") == "building":
                return {"status": "locked"}
            model = await session.get(AIModel, base.embedding_model_id) if base.embedding_model_id else None
            parameters = parameter_snapshot(base, model)
            documents = list(await session.scalars(select(KnowledgeDocument).options(
                defer(KnowledgeDocument.content, raiseload=True),
            ).where(
                KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.is_deleted.is_(False),
            ).order_by(KnowledgeDocument.id)))
            inventory = {d.id: d.source_revision for d in documents}
            resource.data = {**data, "indexStatus": "building", "indexBuildId": str(build_id),
                             "indexError": {}, "documentCount": len(documents),
                             "indexLeaseUntil": (datetime.now(UTC) + timedelta(seconds=900)).isoformat()}
            base.status = "building"
            await session.commit()
        versions = {}
        concurrency = parameters["ingestion"]["indexConcurrency"]
        semaphore = asyncio.Semaphore(concurrency)

        async def build_one(document_id):
            async with semaphore:
                async with SessionLocal() as session:
                    await new_id(session)
                    resource = await session.get(Resource, knowledge_base_id)
                    if ((resource.data or {}).get("indexBuildId") != str(build_id)
                            or (resource.data or {}).get("indexStatus") != "building"):
                        raise ValueError("index_build_cancelled")
                    resource.data = {**(resource.data or {}),
                                     "indexLeaseUntil": (datetime.now(UTC) + timedelta(seconds=900)).isoformat()}
                    await session.commit()
                result = await process_knowledge_document(
                    {**ctx, "defer_activation": True, "source_revision": inventory[document_id]}, document_id, tenant_id)
                if result.get("status") == "parsed" and parameters["embeddingModelId"]:
                    result = await process_document_embeddings(
                        {**ctx, "defer_activation": True, "index_version": result["index_version"]}, document_id, tenant_id)
                return document_id, result

        results = await asyncio.gather(*(build_one(document_id) for document_id in inventory), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
            document_id, result = result
            if result.get("status") not in {"staged", "parsed", "ready"}:
                raise ValueError("document_build_failed")
            versions[document_id] = result["index_version"]
        async with SessionLocal() as session:
            await new_id(session)
            resource, base = await require_runtime_resource(session, tenant_id, knowledge_base_id,
                "knowledge_bases", KnowledgeBase, require_dedicated=True)
            model = await session.get(AIModel, base.embedding_model_id) if base.embedding_model_id else None
            if (parameter_snapshot(base, model) != parameters
                    or (resource.data or {}).get("indexBuildId") != str(build_id)
                    or (resource.data or {}).get("indexStatus") != "building"):
                raise ValueError("index_inputs_changed")
            current_documents = list(await session.scalars(select(KnowledgeDocument).options(
                defer(KnowledgeDocument.content, raiseload=True),
            ).where(
                KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.is_deleted.is_(False),
            )))
            if {d.id: d.source_revision for d in current_documents} != inventory:
                raise ValueError("document_inventory_changed")
            for document in current_documents:
                version = versions[document.id]
                if document.active_index_version == version:
                    continue
                document, _, _, _, run = await valid_run(
                    session, document.id, tenant_id, version, include_content=False)
                if run.status != ("staged" if model else "parsed"):
                    raise ValueError("version_not_complete")
                await activate(session, document, run, lexical=model is None)
            state = ("ready" if model else "lexical_only") if current_documents else "empty"
            resource.data = {**(resource.data or {}), "indexStatus": state,
                             "indexParameters": parameters, "indexVersions": {str(k): str(v) for k, v in versions.items()},
                             "indexError": {}, "indexLeaseUntil": None}
            base.status = state
            await session.commit()
        return {"status": state,
                "knowledge_base_id": knowledge_base_id, "document_count": len(versions),
                "versions": {str(k): str(v) for k, v in versions.items()}}
    except BaseException as exc:
        async with SessionLocal() as session:
            await new_id(session)
            resource = await session.get(Resource, knowledge_base_id)
            if resource and (resource.data or {}).get("indexBuildId") == str(build_id):
                data = resource.data or {}
                if data.get("indexStatus") not in {"cancelled", "uncertain"}:
                    resource.data = {**data, "indexStatus": "uncertain" if isinstance(exc, asyncio.CancelledError) else "failed",
                                     "indexError": {"code": type(exc).__name__, "automaticRetry": False},
                                     "indexLeaseUntil": None}
                    base = await session.get(KnowledgeBase, knowledge_base_id)
                    if base:
                        base.status = resource.data["indexStatus"]
                await session.commit()
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return {"status": "failed", "knowledge_base_id": knowledge_base_id}


async def recover_index_runs():
    """过期执行只能转为 uncertain，不自动再次调用 provider。"""
    async with SessionLocal() as session:
        expired = list(await session.scalars(select(KnowledgeIngestionRun.id).where(
            KnowledgeIngestionRun.status.in_(["parsing", "embedding"]),
            KnowledgeIngestionRun.lease_until <= datetime.now(UTC),
        ).limit(50)))
        resources = list(await session.scalars(select(Resource).where(
            Resource.resource_type == "knowledge_bases", Resource.is_deleted.is_(False))))
        expired_bases = []
        for resource in resources:
            data = resource.data or {}
            lease = data.get("indexLeaseUntil")
            if data.get("indexStatus") != "building" or not lease:
                continue
            try:
                if datetime.fromisoformat(lease).replace(tzinfo=UTC) <= datetime.now(UTC):
                    expired_bases.append(resource.id)
            except (ValueError, TypeError):
                expired_bases.append(resource.id)
    if not expired and not expired_bases:
        return 0
    async with SessionLocal() as session:
        await new_id(session)
        for version in expired:
            run = await session.get(KnowledgeIngestionRun, version, populate_existing=True)
            if (not run or run.status not in {"parsing", "embedding"} or not run.lease_until
                    or run.lease_until.replace(tzinfo=UTC) > datetime.now(UTC)):
                continue
            stage = run.status
            run.status = "uncertain"
            run.error_message = "index_lease_expired"
            run.lease_until = None
            await outbox.stage(session, "index-cleanup", run.document_id, run.tenant_id,
                               event_key=f"index-expired-cleanup:{run.document_id}:{run.id}")
            document = await session.get(KnowledgeDocument, run.document_id)
            if document and not document.is_deleted and document.pending_index_version == version:
                document.status = document.index_status = "uncertain"
                document.index_error = {"stage": stage, "code": "index_lease_expired", "automaticRetry": False}
                await refresh_base_status(session, document.knowledge_base_id, document.tenant_id)
        for base_id in expired_bases:
            resource = await session.get(Resource, base_id, populate_existing=True)
            data = resource.data or {}
            if data.get("indexStatus") != "building":
                continue
            lease = data.get("indexLeaseUntil")
            try:
                if lease and datetime.fromisoformat(lease).replace(tzinfo=UTC) > datetime.now(UTC):
                    continue
            except (ValueError, TypeError):
                pass
            resource.data = {**data, "indexStatus": "uncertain", "indexLeaseUntil": None,
                             "indexError": {"code": "index_lease_expired", "automaticRetry": False}}
            base = await session.get(KnowledgeBase, base_id)
            if base:
                base.status = "uncertain"
        await session.commit()
    return len(expired) + len(expired_bases)

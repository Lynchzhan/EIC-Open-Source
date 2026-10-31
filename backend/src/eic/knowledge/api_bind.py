from __future__ import annotations

from datetime import UTC, datetime
from copy import deepcopy
from typing import Any
from eic.core.authentication import Principal
from eic.core.authorization import ensure_permission
from eic.core.database import get_session
from eic.core.ids import new_id
from eic.core.reference_ids import coerce_id, require_id

from fastapi import BackgroundTasks, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from eic.knowledge.ops import (
    apply_knowledge_payload,
    evaluate_knowledge_set,
    list_knowledge_documents,
    parse_id_list,
    serialize_document_detail,
    serialize_knowledge_document,
    validate_knowledge_model_bindings,
)
from eic.knowledge.strategy import merge_retrieval_config, normalize_strategy
from eic.knowledge.langchain_entrypoint import retrieve_knowledge
from eic.core.file_access import require_file_access
from eic.core import outbox
from eic.models import KnowledgeBase, KnowledgeDocument, KnowledgeIngestionRun, KnowledgeEvalCase, KnowledgeEvalRun, KnowledgeEvalSet, OutboxEvent, Resource


def bind_knowledge_extra_routes(app, *, current_principal, new_entity_id,
                                queue_or_run_document, validate_shared_file_payload) -> None:

    async def _load_base(session: AsyncSession, principal: Principal, knowledge_id: Any, *, for_update=False) -> tuple[Resource, KnowledgeBase]:
        item_id = require_id(knowledge_id, "知识库 ID 无效")
        resource = await session.get(Resource, item_id, with_for_update=for_update, populate_existing=for_update)
        if resource is None or resource.tenant_id != principal.tenant_id or resource.resource_type != "knowledge_bases" or resource.is_deleted:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        base = await session.get(KnowledgeBase, item_id, with_for_update=for_update, populate_existing=for_update)
        if base is None:
            base = KnowledgeBase(id=item_id, tenant_id=principal.tenant_id, name=resource.name, retrieval_strategy="full", retrieval_config={})
            session.add(base)
            await session.flush()
        elif base.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        return resource, base

    async def _load_document(session: AsyncSession, principal: Principal, document_id: Any) -> KnowledgeDocument:
        item_id = require_id(document_id, "文档 ID 无效")
        document = await session.get(KnowledgeDocument, item_id, with_for_update=True, populate_existing=True)
        if document is None or document.tenant_id != principal.tenant_id or document.is_deleted:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "文档不存在"})
        await _load_base(session, principal, document.knowledge_base_id)
        return document

    @app.get("/api/AIKmss/GetDocuments")
    async def knowledge_get_documents(
        Id: str | int,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        await ensure_permission("knowledge.read", principal, session)
        _, base = await _load_base(session, principal, Id)
        return await list_knowledge_documents(session, tenant_id=principal.tenant_id, knowledge_base_id=base.id)

    @app.get("/api/AIKmss/GetDocument")
    async def knowledge_get_document(
        Id: str | int,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.read", principal, session)
        document = await _load_document(session, principal, Id)
        return await serialize_document_detail(session, document)

    @app.post("/api/AIKmss/UpdateDocument")
    async def knowledge_update_document(
        payload: dict[str, Any], background_tasks: BackgroundTasks,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        document = await _load_document(session, principal, payload.get("id") or payload.get("Id"))
        source_name = str(payload.get("sourceName") or payload.get("contentName") or document.source_name)
        content = str(payload.get("content") or "")
        if not content.strip():
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文档内容不能为空"})
        document.source_name = source_name
        document.content = content
        document.extraction_report = {**(document.extraction_report or {}), "methods": ["manual_edit"]}
        document.source_revision += 1
        document.pending_index_version = None
        document.index_status = "pending"
        document.status = "queued_text"
        document.error_message = ""
        document.updated_at = datetime.now(UTC)
        await outbox.stage_document(session, document.id, principal.tenant_id)
        await session.commit()
        job_id, status = await queue_or_run_document(document.id, principal.tenant_id, background_tasks)
        stored = await session.get(KnowledgeDocument, document.id)
        return {"queued": bool(job_id), "jobId": job_id, "status": status, "document": serialize_knowledge_document(stored or document, include_content=True)}

    @app.post("/api/AIKmss/ReplaceDocumentFile")
    async def knowledge_replace_document_file(
        Id: str | int,
        fileId: str | int, background_tasks: BackgroundTasks,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        file_id = require_id(fileId, "文件 ID 无效")
        await ensure_permission("file.read", principal, session)
        file_record = await require_file_access(session, principal, file_id, shared=True)
        document = await _load_document(session, principal, Id)
        await _load_base(session, principal, document.knowledge_base_id)
        document.file_id = file_id
        document.source_name = file_record.name
        document.content = ""
        document.source_revision += 1
        document.pending_index_version = None
        document.index_status = "pending"
        document.status = "queued"
        document.error_message = ""
        document.updated_at = datetime.now(UTC)
        await outbox.stage_document(session, document.id, principal.tenant_id)
        await session.commit()
        job_id, status = await queue_or_run_document(document.id, principal.tenant_id, background_tasks)
        return {"queued": bool(job_id), "jobId": job_id, "status": status, "documentId": document.id, "fileId": file_id}

    @app.delete("/api/AIKmss/DeleteDocument")
    async def knowledge_delete_document(
        Id: str | int,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> bool:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        document = await _load_document(session, principal, Id)
        from eic.knowledge.lifecycle import retire_document
        await retire_document(session, document)
        await session.commit()
        return True

    @app.post("/api/AIKmss/ImportText")
    async def knowledge_import_text(
        payload: dict[str, Any], background_tasks: BackgroundTasks,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        _, base = await _load_base(session, principal, payload.get("knowledgeBaseId") or payload.get("Id") or payload.get("kmsId"))
        title = str(payload.get("contentName") or payload.get("title") or payload.get("name") or "文本片段")
        content = str(payload.get("content") or "")
        if not content.strip():
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文本内容不能为空"})
        document = KnowledgeDocument(
            id=await new_id(session),
            tenant_id=principal.tenant_id,
            knowledge_base_id=base.id,
            file_id=None,
            source_name=title,
            content=content,
            status="queued_text",
        )
        session.add(document)
        await session.flush()
        await outbox.stage_document(session, document.id, principal.tenant_id)
        await session.commit()
        job_id, status = await queue_or_run_document(document.id, principal.tenant_id, background_tasks)
        return {"queued": bool(job_id), "jobId": job_id, "status": status, "documentId": document.id}

    @app.post("/api/AIKmss/ImportFiles")
    async def knowledge_import_files(
        payload: dict[str, Any], background_tasks: BackgroundTasks, principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        """一次提交多个文件或文本，先落库再由 outbox 后台处理。"""
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        _, base = await _load_base(
            session, principal, payload.get("knowledgeBaseId") or payload.get("id") or payload.get("Id"),
        )
        documents = payload.get("documents") or payload.get("files") or []
        if not isinstance(documents, list) or not documents or len(documents) > 100 or any(not isinstance(item, dict) for item in documents):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "至少选择一个文档"})
        await validate_shared_file_payload({"aIKmssDetailsList": documents}, principal, session)
        queued: list[dict[str, Any]] = []
        event_ids: list[int] = []
        for item in documents:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文档字段无效"})
            file = item.get("file") or {}
            file_id = require_id(item.get("fileId") or file.get("id"), "文件 ID 无效") if (item.get("fileId") or file.get("id")) else None
            content = str(item.get("content") or "")
            if not file_id and not content.strip():
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文档缺少文件或文本内容"})
            file_record = await require_file_access(session, principal, file_id, shared=True) if file_id else None
            title = str(item.get("contentName") or item.get("sourceName") or (file_record.name if file_record else "文本片段"))
            existing = None
            if file_id:
                existing = await session.scalar(select(KnowledgeDocument).where(
                    KnowledgeDocument.tenant_id == principal.tenant_id,
                    KnowledgeDocument.knowledge_base_id == base.id,
                    KnowledgeDocument.file_id == file_id,
                    KnowledgeDocument.is_deleted.is_(False),
                ))
            if existing is not None:
                queued.append({"documentId": existing.id, "fileId": file_id, "status": existing.status, "duplicate": True})
                continue
            document = KnowledgeDocument(
                id=await new_id(session), tenant_id=principal.tenant_id, knowledge_base_id=base.id,
                file_id=file_id, source_name=title, content=content,
                status="queued_text" if content.strip() and not file_id else "queued",
            )
            session.add(document)
            await session.flush()
            event_ids.append(await outbox.stage_document(session, document.id, principal.tenant_id))
            queued.append({"documentId": document.id, "fileId": file_id, "status": document.status, "duplicate": False})
        await session.commit()
        background_tasks.add_task(outbox.dispatch_many, event_ids)
        return {"queued": len(event_ids), "jobIds": [str(value) for value in event_ids], "documents": queued}

    async def _retry_documents(payload: dict[str, Any], principal: Principal, session: AsyncSession, background_tasks: BackgroundTasks) -> dict[str, Any]:
        raw_ids = payload.get("documentIds")
        if raw_ids is None:
            raw_ids = [payload.get("id") or payload.get("Id")]
        if not isinstance(raw_ids, (list, tuple, set)):
            raw_ids = [raw_ids]
        if not raw_ids or len(raw_ids) > 100:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "请选择需要重试的文档"})
        if payload.get("acknowledgeDuplicateCall") is not True:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "重试可能重复调用模型，需要显式确认"})
        document_ids: list[int] = []
        seen_ids: set[int] = set()
        for raw_id in raw_ids:
            document_id = require_id(raw_id, "文档 ID 无效")
            if document_id not in seen_ids:
                seen_ids.add(document_id)
                document_ids.append(document_id)
        if not document_ids:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "请选择需要重试的文档"})
        results: list[dict[str, Any]] = []
        event_ids: list[int] = []
        retryable = {"failed", "uncertain", "parsed", "staged", "legacy", "cancelled"}
        for document_id in document_ids:
            document = await _load_document(session, principal, document_id)
            await _load_base(session, principal, document.knowledge_base_id)
            pending = await session.scalar(select(OutboxEvent).where(
                OutboxEvent.entity_id == document.id, OutboxEvent.tenant_id == principal.tenant_id,
                OutboxEvent.kind.in_(["knowledge-document", "document-embeddings"]),
                OutboxEvent.consume_status.in_(["pending", "running"]),
            ).order_by(OutboxEvent.id.desc()).limit(1))
            if pending is not None and document.status not in {"cancelled", "failed", "uncertain"}:
                if pending.consume_status == "pending":
                    event_ids.append(pending.id)
                results.append({"documentId": document.id, "status": document.status, "accepted": True, "reused": True})
                continue
            run = await session.get(KnowledgeIngestionRun, document.pending_index_version) if document.pending_index_version else None
            if run and run.status in {"parsing", "embedding"}:
                results.append({"documentId": document.id, "status": "running", "accepted": False})
                continue
            if document.status not in retryable:
                results.append({"documentId": document.id, "status": document.status, "accepted": False})
                continue
            document.pending_index_version = None
            if run and run.status not in {"ready", "cancelled", "superseded"}:
                run.status = "superseded"
                run.lease_until = None
            # 新的 source revision 隔离旧 outbox，停止后重试不能复活旧事件。
            document.source_revision += 1
            document.status = "queued_text" if document.content and not document.file_id else "queued"
            document.index_status = "pending"
            document.index_error = {}
            document.error_message = ""
            event_ids.append(await outbox.stage_document(session, document.id, principal.tenant_id))
            results.append({"documentId": document.id, "status": document.status, "accepted": True})
        await session.commit()
        background_tasks.add_task(outbox.dispatch_many, event_ids)
        return {"queued": len(event_ids), "jobIds": [str(value) for value in event_ids], "documents": results}

    @app.post("/api/AIKmss/RetryDocuments")
    async def knowledge_retry_documents(
        payload: dict[str, Any], background_tasks: BackgroundTasks, principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        return await _retry_documents(payload, principal, session, background_tasks)

    @app.post("/api/AIKmss/RetryDocument")
    async def knowledge_retry_document(
        payload: dict[str, Any], background_tasks: BackgroundTasks, principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        result = await _retry_documents(payload, principal, session, background_tasks)
        first = (result.get("documents") or [{}])[0]
        return {**result, "jobId": (result.get("jobIds") or [None])[0], "status": first.get("status"), "automaticRetry": False}

    @app.post("/api/AIKmss/CancelDocument")
    async def knowledge_cancel_document(
        payload: dict[str, Any], principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        document = await _load_document(session, principal, payload.get("id") or payload.get("Id"))
        await _load_base(session, principal, document.knowledge_base_id)
        if document.status in {"ready", "cancelled", "deleted"}:
            return {"documentId": document.id, "status": document.status, "graceful": False}
        run = await session.get(KnowledgeIngestionRun, document.pending_index_version) if document.pending_index_version else None
        if run and run.status in {"parsing", "embedding"}:
            document.status = "cancelling"
            document.index_status = "cancelling"
            document.index_error = {"code": "cancel_requested", "automaticRetry": False}
        else:
            if run and run.status not in {"ready", "failed", "uncertain", "cancelled", "superseded"}:
                run.status = "cancelled"
                run.error_message = "cancelled_by_user"
                run.finished_at = datetime.now(UTC)
            document.pending_index_version = None
            document.status = "cancelled"
            document.index_status = "cancelled"
            document.index_error = {"code": "cancelled_by_user", "automaticRetry": False}
        await session.commit()
        return {"documentId": document.id, "status": document.status, "graceful": document.status == "cancelling"}

    @app.post("/api/AIKmss/CancelIndex")
    async def knowledge_cancel_index(
        Id: str | int, principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> bool:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        resource, base = await _load_base(session, principal, Id)
        resource.data = {**(resource.data or {}), "indexStatus": "cancelled", "indexLeaseUntil": None}
        base.status = "cancelled"
        await session.commit()
        return True

    @app.post("/api/AIKmss/SaveRetrieval")
    async def knowledge_save_retrieval(
        payload: dict[str, Any],
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        await new_id(session)
        resource, base = await _load_base(session, principal, payload.get("id") or payload.get("Id"), for_update=True)
        from eic.knowledge.indexing import parameter_snapshot
        from eic.models import AIModel
        old_model = await session.get(AIModel, base.embedding_model_id) if base.embedding_model_id else None
        previous = parameter_snapshot(base, old_model)
        await validate_knowledge_model_bindings(session, principal.tenant_id, payload, base)
        apply_knowledge_payload(base, payload)
        data = dict(resource.data or {})
        data.update({
            "retrievalStrategy": base.retrieval_strategy,
            "retrievalConfig": base.retrieval_config,
            "maxTokensPerParagraph": base.chunk_size,
            "overlappingTokens": base.chunk_overlap,
            "aIModelsId": base.embedding_model_id,
            "aIRerankModelsId": base.rerank_model_id,
        })
        model = await session.get(AIModel, base.embedding_model_id) if base.embedding_model_id else None
        if parameter_snapshot(base, model) != previous and data.get("indexStatus") != "building":
            data["indexStatus"] = base.status = "pending"
        resource.data = data
        await session.commit()
        return {
            "id": base.id,
            "retrievalStrategy": normalize_strategy(base.retrieval_strategy),
            "retrievalConfig": merge_retrieval_config(base.retrieval_config, chunk_size=base.chunk_size, chunk_overlap=base.chunk_overlap),
        }

    @app.get("/api/AIKmss/GetEvalSets")
    async def knowledge_get_eval_sets(
        knowledgeBaseId: str | int,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        await ensure_permission("knowledge.read", principal, session)
        _, base = await _load_base(session, principal, knowledgeBaseId)
        rows = list(await session.scalars(select(KnowledgeEvalSet).where(
            KnowledgeEvalSet.tenant_id == principal.tenant_id,
            KnowledgeEvalSet.knowledge_base_id == base.id,
        ).order_by(KnowledgeEvalSet.updated_at.desc())))
        return [
            {
                "id": item.id,
                "knowledgeBaseId": item.knowledge_base_id,
                "name": item.name,
                "description": item.description,
                "lastResult": item.last_result or {},
            }
            for item in rows
        ]

    @app.post("/api/AIKmss/SaveEvalSet")
    async def knowledge_save_eval_set(
        payload: dict[str, Any],
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        _, base = await _load_base(session, principal, payload.get("knowledgeBaseId") or payload.get("Id"))
        item_id = coerce_id(payload.get("id"))
        item = await session.get(KnowledgeEvalSet, item_id) if item_id else None
        if item is None:
            item = KnowledgeEvalSet(id=await new_entity_id(session, item_id), tenant_id=principal.tenant_id, knowledge_base_id=base.id, name=str(payload.get("name") or "默认评测集"))
            session.add(item)
        elif item.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "评测集不存在"})
        item.name = str(payload.get("name") or item.name)
        item.description = str(payload.get("description") or "")
        item.knowledge_base_id = base.id
        await session.commit()
        return {"id": item.id, "name": item.name, "description": item.description, "knowledgeBaseId": item.knowledge_base_id}

    @app.delete("/api/AIKmss/DeleteEvalSet")
    async def knowledge_delete_eval_set(
        Id: str | int,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> bool:
        await ensure_permission("knowledge.write", principal, session)
        item = await session.get(KnowledgeEvalSet, require_id(Id, "评测集 ID 无效"),
                                 with_for_update=True, populate_existing=True)
        if item is None or item.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "评测集不存在"})
        history_id = await session.scalar(select(KnowledgeEvalRun.id).where(
            KnowledgeEvalRun.tenant_id == principal.tenant_id,
            KnowledgeEvalRun.eval_set_id == item.id,
        ).limit(1))
        if history_id is not None:
            raise HTTPException(status_code=409, detail={
                "code": 409, "errorCode": "eval_set_has_history",
                "errMsg": "评测集已有历史记录，保留评测集及用例，不能删除",
            })
        await session.execute(delete(KnowledgeEvalCase).where(
            KnowledgeEvalCase.eval_set_id == item.id,
            KnowledgeEvalCase.tenant_id == principal.tenant_id,
        ))
        await session.delete(item)
        await session.commit()
        return True

    @app.get("/api/AIKmss/GetEvalCases")
    async def knowledge_get_eval_cases(
        evalSetId: str | int,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        await ensure_permission("knowledge.read", principal, session)
        eval_set = await session.get(KnowledgeEvalSet, require_id(evalSetId, "评测集 ID 无效"))
        if eval_set is None or eval_set.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "评测集不存在"})
        rows = list(await session.scalars(select(KnowledgeEvalCase).where(
            KnowledgeEvalCase.tenant_id == principal.tenant_id,
            KnowledgeEvalCase.eval_set_id == eval_set.id,
        ).order_by(KnowledgeEvalCase.id.desc())))
        return [
            {
                "id": item.id,
                "evalSetId": item.eval_set_id,
                "query": item.query,
                "expectedDocumentIds": item.expected_document_ids or [],
                "expectedChunkIds": item.expected_chunk_ids or [],
                "expectedAnswer": item.expected_answer or "",
                "complexity": item.complexity,
            }
            for item in rows
        ]

    @app.post("/api/AIKmss/SaveEvalCase")
    async def knowledge_save_eval_case(
        payload: dict[str, Any],
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.write", principal, session)
        # 与运行/删除统一先锁父集，再分配 ID，避免外键锁与 allocator 锁相互等待。
        eval_set = await session.get(
            KnowledgeEvalSet, require_id(payload.get("evalSetId"), "评测集 ID 无效"),
            with_for_update=True, populate_existing=True,
        )
        if eval_set is None or eval_set.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "评测集不存在"})
        query = str(payload.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "评测问题不能为空"})
        item_id = coerce_id(payload.get("id"))
        item = await session.get(KnowledgeEvalCase, item_id) if item_id else None
        if item is None:
            item = KnowledgeEvalCase(id=await new_entity_id(session, item_id), tenant_id=principal.tenant_id, eval_set_id=eval_set.id, query=query)
            session.add(item)
        elif item.tenant_id != principal.tenant_id or item.eval_set_id != eval_set.id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "评测用例不存在"})
        item.query = query
        item.expected_document_ids = parse_id_list(payload.get("expectedDocumentIds"))
        item.expected_chunk_ids = parse_id_list(payload.get("expectedChunkIds"))
        item.expected_answer = str(payload.get("expectedAnswer") or "")
        item.complexity = str(payload.get("complexity") or "medium")
        await session.commit()
        return {
            "id": item.id,
            "evalSetId": item.eval_set_id,
            "query": item.query,
            "expectedDocumentIds": item.expected_document_ids,
            "expectedChunkIds": item.expected_chunk_ids,
            "complexity": item.complexity,
        }

    @app.delete("/api/AIKmss/DeleteEvalCase")
    async def knowledge_delete_eval_case(
        Id: str | int,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> bool:
        await ensure_permission("knowledge.write", principal, session)
        item = await session.get(KnowledgeEvalCase, require_id(Id, "评测用例 ID 无效"))
        if item is None or item.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "评测用例不存在"})
        await session.delete(item)
        await session.commit()
        return True

    @app.post("/api/AIKmss/RunEvalSet")
    async def knowledge_run_eval_set(
        payload: dict[str, Any],
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.read", principal, session)
        eval_set = await session.get(
            KnowledgeEvalSet, require_id(payload.get("evalSetId") or payload.get("id"), "评测集 ID 无效"),
            with_for_update=True, populate_existing=True,
        )
        if eval_set is None or eval_set.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "评测集不存在"})
        base = await session.get(KnowledgeBase, eval_set.knowledge_base_id)
        if base is None or base.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        k = int(payload.get("k") or payload.get("topK") or (base.retrieval_config or {}).get("topK") or 8)
        k = max(1, min(50, k))
        rows = list(await session.scalars(select(KnowledgeEvalCase).where(
            KnowledgeEvalCase.eval_set_id == eval_set.id,
            KnowledgeEvalCase.tenant_id == principal.tenant_id,
        ).order_by(KnowledgeEvalCase.id)))
        cases = [{
            "id": row.id, "query": row.query,
            "expectedDocumentIds": deepcopy(row.expected_document_ids),
            "expectedChunkIds": deepcopy(row.expected_chunk_ids),
            "expectedAnswer": row.expected_answer, "complexity": row.complexity,
        } for row in rows]
        snapshot = {
            "tenantId": str(principal.tenant_id), "actorId": str(principal.user_id),
            "evalSet": {"id": str(eval_set.id), "name": eval_set.name,
                        "description": eval_set.description},
            "knowledgeBase": {
                "id": str(base.id), "name": base.name,
                "strategy": normalize_strategy(base.retrieval_strategy),
                "config": deepcopy(base.retrieval_config or {}),
                "embeddingModelId": str(base.embedding_model_id) if base.embedding_model_id else None,
                "rerankModelId": str(base.rerank_model_id) if base.rerank_model_id else None,
                "chunkSize": base.chunk_size, "chunkOverlap": base.chunk_overlap,
            },
            "cases": deepcopy(cases), "k": k,
            "startedAt": datetime.now(UTC).isoformat(),
        }
        summary = await evaluate_knowledge_set(session, tenant_id=principal.tenant_id, base=base, cases=cases, k=k)
        eval_set.last_result = {key: value for key, value in summary.items() if key != "cases"}
        snapshot["finishedAt"] = datetime.now(UTC).isoformat()
        session.add(KnowledgeEvalRun(
            id=await new_id(session), tenant_id=principal.tenant_id,
            eval_set_id=eval_set.id, knowledge_base_id=base.id,
            k=k, strategy=summary["strategy"],
            result={**deepcopy(summary), "snapshot": snapshot},
        ))
        await session.commit()
        return summary

    @app.post("/api/AIKmss/RecallTest")
    async def knowledge_recall_test(
        payload: dict[str, Any],
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        await ensure_permission("knowledge.read", principal, session)
        _, base = await _load_base(session, principal, payload.get("knowledgeBaseId") or payload.get("Id"))
        query = str(payload.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "召回问题不能为空"})
        limit = int(payload.get("limit") or payload.get("k") or payload.get("topK") or 8)
        extra = dict(payload.get("retrievalConfig") or {})
        if payload.get("retrievalStrategy"):
            extra["retrievalStrategy"] = payload.get("retrievalStrategy")
        return await retrieve_knowledge(
            session,
            tenant_id=principal.tenant_id,
            base=base,
            query=query,
            extra=extra or None,
            limit=max(1, min(50, limit)),
        )

"""knowledge 领域；通过显式装配提供跨域依赖。"""
from eic.api.resource_inputs import KnowledgeWrite, write_payload
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from eic.ai.prompt_catalog import CATALOG
from eic.ai.prompt_catalog import CATALOG_VERSION
from fastapi import BackgroundTasks, Depends
from fastapi import HTTPException
from eic.models import KnowledgeBase
from eic.models import KnowledgeDocument
from eic.models import OutboxEvent
from eic.api.schemas import PageInput as PageInput
from eic.core.authentication import Principal
from fastapi import Request
from eic.models import Resource
from eic.core.database import get_session
import json
from eic import model_lifecycle
from eic.core import outbox
from eic.ai import prompt_templates
from eic.ai.prompt_catalog import public_template
from eic.core.file_access import require_file_access
from sqlalchemy import select
from eic.knowledge.ops import validate_knowledge_model_bindings


@dataclass(frozen=True)
class Dependencies:
    all_resources: Callable[..., Any]
    apply_knowledge_payload: Callable[..., Any]
    attach_operation_logs: Callable[..., Any]
    coerce_id: Callable[..., Any]
    delete_resource: Callable[..., Any]
    enrich_resource_items: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    has_permission: Callable[..., Any]
    lock_tenant_writes: Callable[..., Any]
    new_id: Callable[..., Any]
    require_id: Callable[..., Any]
    resource_page: Callable[..., Any]
    retrieve_knowledge: Callable[..., Any]
    save_resource: Callable[..., Any]
    serialize_resource: Callable[..., Any]
    validate_shared_file_payload: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    async def queue_or_run_document(document_id: int, tenant_id: int, background_tasks=None) -> tuple[str | None, str]:
        event_id = await outbox.latest_document_event(document_id, tenant_id)
        if event_id is None:
            raise RuntimeError("Document intent must be recorded before business commit")
        # 意图已持久化；响应后投递，扫描器负责崩溃恢复。
        if background_tasks is not None:
            background_tasks.add_task(outbox.dispatch_one, event_id)
        job_id = f"eic-outbox:{tenant_id}:{event_id}"
        async with runtime.sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            return job_id, document.status if document else "missing"


    @app.post("/api/AIKmss/AddEdit")
    async def knowledge_base_add_edit(payload: KnowledgeWrite, background_tasks: BackgroundTasks, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, KnowledgeWrite)
        """Keep the legacy resource response while maintaining the dedicated ingestion FK root."""
        await dependencies.ensure_permission("knowledge.write", principal, session)
        await model_lifecycle.lock_tenant(session, principal.tenant_id)
        resource_id = dependencies.require_id(payload["id"], "知识库 ID 无效") if payload.get("id") not in (None, "") else await dependencies.new_id(session)
        payload = {**payload, "id": resource_id}
        await dependencies.validate_shared_file_payload(payload, principal, session)
        current_base = await session.get(KnowledgeBase, dependencies.coerce_id(payload.get("id"))) if dependencies.coerce_id(payload.get("id")) else None
        if current_base is not None and current_base.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        await validate_knowledge_model_bindings(session, principal.tenant_id, payload, current_base)
        # 主 Resource 与文档引用在同一事务提交，文件行锁不在绑定完成前释放。
        await dependencies.save_resource("knowledge_bases", payload, principal, session, commit=False)
        resource = await session.get(Resource, resource_id)
        knowledge_base = await session.get(KnowledgeBase, resource_id)
        if knowledge_base is None:
            knowledge_base = KnowledgeBase(id=resource_id, tenant_id=principal.tenant_id, name=str(payload.get("name") or ""), retrieval_strategy="full", retrieval_config={})
            session.add(knowledge_base)
        elif knowledge_base.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        dependencies.apply_knowledge_payload(knowledge_base, payload)
        if resource is not None:
            data = dict(resource.data or {})
            data.update({
                "retrievalStrategy": knowledge_base.retrieval_strategy,
                "retrievalConfig": knowledge_base.retrieval_config,
                "maxTokensPerParagraph": knowledge_base.chunk_size,
                "overlappingTokens": knowledge_base.chunk_overlap,
                "aIModelsId": knowledge_base.embedding_model_id,
                "aIRerankModelsId": knowledge_base.rerank_model_id,
            })
            resource.data = data
        queued_documents: list[int] = []
        await dependencies.validate_shared_file_payload(payload, principal, session)
        details = payload.get("aIKmssDetailsList") or payload.get("kmsDetailsList") or []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            file_id = dependencies.coerce_id(detail.get("fileId") or (detail.get("file") or {}).get("id"))
            content = str(detail.get("content") or "").strip()
            title = str(detail.get("contentName") or detail.get("fileName") or (detail.get("file") or {}).get("fileName") or "文档")
            existing = None
            if file_id:
                existing = await session.scalar(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == principal.tenant_id,
                        KnowledgeDocument.knowledge_base_id == resource_id,
                        KnowledgeDocument.file_id == file_id,
                        KnowledgeDocument.is_deleted.is_(False),
                    )
                )
            if existing is None and dependencies.coerce_id(detail.get("id")):
                candidate = await session.get(KnowledgeDocument, dependencies.coerce_id(detail.get("id")))
                if candidate is not None and candidate.tenant_id == principal.tenant_id and candidate.knowledge_base_id == resource_id and not candidate.is_deleted:
                    existing = candidate
            if existing is not None:
                continue
            document = KnowledgeDocument(
                id=await dependencies.new_id(session),
                tenant_id=principal.tenant_id,
                knowledge_base_id=resource_id,
                file_id=file_id,
                source_name=title,
                content=content,
                status="queued_text" if content and not file_id else "queued",
            )
            session.add(document)
            await session.flush()
            queued_documents.append(document.id)
            await outbox.stage_document(session, document.id, principal.tenant_id)
        await session.commit()
        for document_id in queued_documents:
            await queue_or_run_document(document_id, principal.tenant_id, background_tasks)
        return True


    @app.delete("/api/AIKmss/Delete")
    async def knowledge_base_delete(Id: str | int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        return await dependencies.delete_resource("knowledge_bases", dependencies.require_id(Id, "知识库 ID 无效"), principal, session)


    @app.get("/api/AIApps/GetALLList")
    @app.get("/api/AIApps/GetMyALLList")
    async def ai_apps_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("ai.resource.read", principal, session)
        return await dependencies.all_resources("ai_apps", principal, session)


    @app.get("/api/AIPrompts/GetALLList")
    async def ai_prompts_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("ai.resource.read", principal, session)
        return await dependencies.all_resources("ai_prompts", principal, session)


    @app.get("/api/AIPrompts/BuiltinCatalog")
    async def prompt_builtin_catalog(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.resource.read", principal, session)
        return {"catalogVersion": CATALOG_VERSION, "templates": [public_template(item) for item in CATALOG],
                "canImport": await dependencies.has_permission("ai.resource.write", principal, session)}


    @app.post("/api/AIPrompts/PreviewBuiltinImport")
    async def prompt_builtin_preview(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.resource.read", principal, session)
        return await prompt_templates.preview(session, principal, runtime.settings.jwt_secret)


    @app.post("/api/AIPrompts/ApplyBuiltinImport")
    async def prompt_builtin_apply(payload: dict[str, Any], request: Request, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.resource.write", principal, session)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        # 锁后重新核对权限，预览与提交期间撤权时拒绝写入。
        await dependencies.ensure_permission("ai.resource.write", principal, session)
        result = await prompt_templates.apply(session, principal, runtime.settings.jwt_secret, payload)
        await dependencies.attach_operation_logs(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id, username=principal.username,
            operate_type="导入内置模板", operate_remark=f"人工选择导入 {len(result['changes'])} 项模板",
            table="ai_prompts", payload=result, request=request,
        )
        await session.commit()
        return result


    @app.get("/api/AIKmss/GetALLList")
    async def knowledge_bases_all_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("knowledge.read", principal, session)
        return await dependencies.all_resources("knowledge_bases", principal, session)


    @app.get("/api/AIApps/NewInitialization")
    async def ai_app_new_initialization(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.resource.read", principal, session)
        return {"id": 0, "name": "", "type": "chat", "isSkill": True, "isMemory": False, "msgType": 2}


    @app.get("/api/AIApps/GetDetails")
    @app.get("/api/AIKmss/GetDetails")
    @app.get("/api/AISkillToolManagement/GetById")
    async def resource_details(request: Request, Id: str | int | None = None, id: str | int | None = None, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        resource_permission = "ai.resource.read"
        expected_type: str | None = "ai_apps"
        if "/AIKmss/" in request.url.path:
            expected_type, resource_permission = "knowledge_bases", "knowledge.read"
        elif "/AISkillToolManagement/" in request.url.path:
            expected_type, resource_permission = "skills", "skill.read"
        if Id is None and id is None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "记录 ID 不能为空"})
        item = await session.get(Resource, dependencies.require_id(Id if Id is not None else id, "记录 ID 无效"))
        if item is None or item.tenant_id != principal.tenant_id or item.is_deleted or (expected_type and item.resource_type != expected_type):
            raise HTTPException(status_code=404, detail={"code": 400, "errMsg": "记录不存在"})
        if item.resource_type == "knowledge_bases":
            resource_permission = "knowledge.read"
        elif item.resource_type == "skills":
            resource_permission = "skill.read"
        await dependencies.ensure_permission(resource_permission, principal, session)
        payload = dependencies.serialize_resource(item)
        if item.resource_type in {"knowledge_bases", "ai_apps", "ai_prompts"}:
            await dependencies.enrich_resource_items(item.resource_type, [payload], session, principal.tenant_id)
        return payload


    @app.post("/api/AIKmss/GetPageData")
    @app.post("/api/AIKmss/GetList")
    @app.post("/api/AIKmss/GetMyPageData")
    async def knowledge_list(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("knowledge_bases", payload, principal, session)


    @app.post("/api/AIKmss/QueueIndex")
    async def knowledge_queue_index(Id: str | int, background_tasks: BackgroundTasks, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("knowledge.write", principal, session)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        item = await session.get(Resource, dependencies.require_id(Id, "知识库 ID 无效"))
        if item is None or item.tenant_id != principal.tenant_id or item.resource_type != "knowledge_bases" or item.is_deleted:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        existing = await session.scalar(select(OutboxEvent).where(
            OutboxEvent.kind == "knowledge-base", OutboxEvent.entity_id == item.id,
            OutboxEvent.tenant_id == principal.tenant_id,
            OutboxEvent.consume_status.in_(["pending", "running", "uncertain"]),
        ).order_by(OutboxEvent.id.desc()).limit(1))
        if existing and existing.consume_status == "uncertain":
            return {"queued": False, "jobId": existing.job_id, "status": "uncertain",
                    "automaticRetry": False}
        event_id = existing.id if existing else await outbox.stage(
            session, "knowledge-base", item.id, principal.tenant_id)
        if not existing:
            item.data = {**(item.data or {}), "indexStatus": "pending",
                         "indexJobId": None, "indexEventId": str(event_id)}
        await session.commit()
        background_tasks.add_task(outbox.dispatch_one, event_id)
        return {"queued": True, "jobId": f"eic-outbox:{principal.tenant_id}:{event_id}", "status": "pending"}


    @app.post("/api/AIKmss/ImportFile")
    async def knowledge_import_file(Id: str | int, fileId: str | int, background_tasks: BackgroundTasks, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("knowledge.write", principal, session)
        knowledge_id = dependencies.require_id(Id, "知识库 ID 无效")
        stored_file_id = dependencies.require_id(fileId, "文件 ID 无效")
        knowledge_base = await session.get(Resource, knowledge_id)
        if knowledge_base is None or knowledge_base.tenant_id != principal.tenant_id or knowledge_base.resource_type != "knowledge_bases" or knowledge_base.is_deleted:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        await dependencies.ensure_permission("file.read", principal, session)
        file_record = await require_file_access(session, principal, stored_file_id, shared=True)
        dedicated_base = await session.get(KnowledgeBase, knowledge_id)
        if dedicated_base is None:
            dedicated_base = KnowledgeBase(id=knowledge_id, tenant_id=principal.tenant_id, name=knowledge_base.name, retrieval_strategy="full", retrieval_config={})
            session.add(dedicated_base)
            await session.flush()
        elif dedicated_base.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        document = await session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == principal.tenant_id,
                KnowledgeDocument.knowledge_base_id == knowledge_id,
                KnowledgeDocument.file_id == stored_file_id,
                KnowledgeDocument.is_deleted.is_(False),
            )
        )
        if document is None:
            document = KnowledgeDocument(
                id=await dependencies.new_id(session), tenant_id=principal.tenant_id, knowledge_base_id=knowledge_id, file_id=stored_file_id,
                source_name=file_record.name, status="queued",
            )
            session.add(document)
            await session.flush()
        file_ids = list((knowledge_base.data or {}).get("fileIds", []))
        if stored_file_id not in file_ids:
            file_ids.append(stored_file_id)
        knowledge_base.data = {**(knowledge_base.data or {}), "fileIds": file_ids, "indexStatus": "queued"}
        await outbox.stage_document(session, document.id, principal.tenant_id)
        await session.commit()
        job_id, status = await queue_or_run_document(document.id, principal.tenant_id, background_tasks)
        return {"queued": bool(job_id), "jobId": job_id, "status": status, "fileId": stored_file_id, "documentId": document.id}


    @app.get("/api/Knowledge/RetrievalTrace")
    async def knowledge_retrieval_trace(
        knowledgeBaseId: str | int,
        query: str,
        limit: int = 5,
        metadata: str | None = None,
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        """Local sparse retrieval trace; vector and Qdrant comparisons stay external adapters."""
        await dependencies.ensure_permission("knowledge.read", principal, session)
        if not 1 <= limit <= 50:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "limit 必须介于 1 到 50"})
        base = await session.get(KnowledgeBase, dependencies.require_id(knowledgeBaseId, "知识库 ID 无效"))
        if base is None or base.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "知识库不存在"})
        try:
            filters = json.loads(metadata) if metadata else None
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "metadata 必须是 JSON 对象"}) from exc
        if filters is not None and not isinstance(filters, dict):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "metadata 必须是 JSON 对象"})
        return await dependencies.retrieve_knowledge(
            session,
            tenant_id=principal.tenant_id,
            base=base,
            query=query,
            filters=filters,
            limit=limit,
        )

    return _RouteExports(
        queue_or_run_document=queue_or_run_document,
        knowledge_base_add_edit=knowledge_base_add_edit,
        knowledge_base_delete=knowledge_base_delete,
        ai_apps_list=ai_apps_list,
        ai_prompts_list=ai_prompts_list,
        prompt_builtin_catalog=prompt_builtin_catalog,
        prompt_builtin_preview=prompt_builtin_preview,
        prompt_builtin_apply=prompt_builtin_apply,
        knowledge_bases_all_list=knowledge_bases_all_list,
        ai_app_new_initialization=ai_app_new_initialization,
        resource_details=resource_details,
        knowledge_list=knowledge_list,
        knowledge_queue_index=knowledge_queue_index,
        knowledge_import_file=knowledge_import_file,
        knowledge_retrieval_trace=knowledge_retrieval_trace,
    )

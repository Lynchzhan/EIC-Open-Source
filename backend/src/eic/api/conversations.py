"""conversations 领域；通过显式装配提供跨域依赖。"""
from eic.api.resource_inputs import ChatMessageWrite, ChatWrite, write_payload
from dataclasses import dataclass
from eic.ai.app_config import chat_model_id
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from eic.models import AIApp
from eic.models import AIModel
from eic.models import AgentMemory
from eic.models import AgentRun
from sqlalchemy.ext.asyncio import AsyncSession
from eic.ai.budget import ChatConfigurationError
from eic.models import ChatMessage
from eic.models import ChatSession
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from eic.models import KnowledgeDocument
from eic.api.schemas import PageInput as PageInput
from eic.core.authentication import Principal
from fastapi import Request
from eic.models import Resource
from fastapi import Response
from eic.api.memory import SESSION_MEMORY_TYPES as SESSION_MEMORY_TYPES
from eic.core.file_access import can_access_file
from eic.ai import attachments as chat_attachments
from eic.ai.context_policy import context_settings
from eic.ai import conversation_context
from eic.core.security import create_access_token
from sqlalchemy import delete
from sqlalchemy import func
from eic.core.database import get_session
from eic.core import memory_lifecycle
from eic.ai.context_policy import memory_settings
from eic import model_lifecycle
from eic.core import observability
import re
from eic.core.file_access import require_file_access
from eic.core.runtime_refs import require_runtime_model
from sqlalchemy import select
from sqlalchemy import update
from eic.core.file_access import valid_file_id


@dataclass(frozen=True)
class Dependencies:
    attachment_error_message: Callable[..., Any]
    delete_resource: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    first_int: Callable[..., Any]
    has_permission: Callable[..., Any]
    lock_tenant_writes: Callable[..., Any]
    new_entity_id: Callable[..., Any]
    new_id: Callable[..., Any]
    normalize_chat_files: Callable[..., Any]
    page_result: Callable[..., Any]
    require_id: Callable[..., Any]
    resource_page: Callable[..., Any]
    runtime_app_config: Callable[..., Any]
    save_resource: Callable[..., Any]
    serialize_file: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    @app.post("/api/AIChats/GetMyPageData")
    async def chats_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        return await dependencies.resource_page("chats", payload, principal, session)


    @app.post("/api/AIChats/Add")
    async def chats_add(payload: ChatWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, ChatWrite)
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        return await dependencies.save_resource("chats", payload, principal, session, create_only=True)


    @app.post("/api/AIChats/AddEdit")
    async def chats_edit(payload: ChatWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, ChatWrite)
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        return await dependencies.save_resource("chats", payload, principal, session, existing_only=True)


    @app.get("/api/AIChatHistorys/CitationSource")
    async def chat_citation_source(requestId: str, documentId: str,
                                   principal: Principal = Depends(current_principal),
                                   session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        await dependencies.ensure_permission("knowledge.read", principal, session)
        _, request_message = await require_owned_chat_request(session, principal, requestId)
        answers = list(await session.scalars(select(ChatMessage).where(
            ChatMessage.tenant_id == principal.tenant_id,
            ChatMessage.session_id == request_message.session_id,
            ChatMessage.request_id == requestId, ChatMessage.role == "assistant")))
        document_id = dependencies.require_id(documentId, "文档 ID 无效")
        cited = any(
            isinstance(source, dict) and str(source.get("documentId")) == str(document_id)
            for answer in answers for source in (answer.token_usage or {}).get("citations", [])
        )
        document = await session.get(KnowledgeDocument, document_id) if cited else None
        if document is None or document.tenant_id != principal.tenant_id or document.is_deleted:
            raise HTTPException(404, detail="引用原文件不存在或不可访问")
        base = await session.get(Resource, document.knowledge_base_id)
        if (base is None or base.tenant_id != principal.tenant_id or base.resource_type != "knowledge_bases"
                or base.is_deleted or (base.data or {}).get("enabled") is False):
            raise HTTPException(404, detail="引用知识库不存在或不可访问")
        if not document.file_id:
            raise HTTPException(404, detail="该引用没有原文件")
        await dependencies.ensure_permission("file.read", principal, session)
        file = await require_file_access(session, principal, document.file_id)
        return {"fileId": str(file.id), "documentId": str(document_id)}


    @app.get("/api/AIModels/ConversationLabelModel")
    async def conversation_label_model_get(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.model.read", principal, session)
        from eic.ai.conversation_labels import settings_row
        row = await settings_row(session, principal.tenant_id)
        return {"modelId": str(row.data['modelId']) if row and row.data.get('modelId') else None,
                "revision": (row.data or {}).get('revision', 0) if row else 0}


    @app.put("/api/AIModels/ConversationLabelModel")
    async def conversation_label_model_save(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.model.write", principal, session)
        await model_lifecycle.lock_tenant(session, principal.tenant_id)
        from eic.ai.conversation_labels import settings_row
        row = await settings_row(session, principal.tenant_id)
        revision = (row.data or {}).get('revision', 0) if row else 0
        if payload.get('revision') != revision:
            raise HTTPException(409, '设置已变化，请刷新后重试')
        model_id = dependencies.first_int(payload.get('modelId'))
        if payload.get('modelId') and not model_id:
            raise HTTPException(400, '模型 ID 无效')
        if model_id:
            await require_runtime_model(session, principal.tenant_id, model_id, 'chat')
        if row is None:
            row = Resource(id=await dependencies.new_id(session), tenant_id=principal.tenant_id,
                owner_id=principal.user_id, resource_type='conversation_label_model', name='会话标题与摘要模型')
            session.add(row)
        row.data = {'modelId': model_id, 'revision': revision + 1}
        await session.commit()
        return True


    @app.get("/api/AIChats/Labels")
    async def conversation_labels_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        from eic.ai.conversation_labels import DEFAULT_TITLES
        from eic.models import ContextJob
        rows = list(await session.scalars(select(Resource).where(Resource.tenant_id == principal.tenant_id,
            Resource.owner_id == principal.user_id, Resource.resource_type == 'chats', Resource.is_deleted.is_(False))
            .order_by(Resource.updated_at.desc()).limit(200)))
        nonempty = set(await session.scalars(select(ChatMessage.session_id).where(
            ChatMessage.tenant_id == principal.tenant_id, ChatMessage.session_id.in_([r.id for r in rows]),
            ChatMessage.role == 'assistant', ChatMessage.status == 'completed').distinct()))
        jobs = {str(j.id): j for j in await session.scalars(select(ContextJob).where(
            ContextJob.tenant_id == principal.tenant_id, ContextJob.owner_id == principal.user_id,
            ContextJob.kind == 'conversation-label', ContextJob.id.in_([
                int(r.data['labelJobId']) for r in rows if (r.data or {}).get('labelJobId')])))}
        first_content = select(ChatMessage.content).where(ChatMessage.tenant_id == principal.tenant_id,
            ChatMessage.session_id == Resource.id, ChatMessage.role == 'user')\
            .order_by(ChatMessage.created_at, ChatMessage.id).limit(1).scalar_subquery()
        previews = dict((await session.execute(select(Resource.id, first_content).where(
            Resource.id.in_([r.id for r in rows]), Resource.tenant_id == principal.tenant_id))).all())
        return [{'id': str(r.id), 'title': r.name, 'summary': (r.data or {}).get('labelSummary', ''),
            'preview': str(previews.get(r.id) or '')[:100], 'version': int((r.data or {}).get('labelVersion', 0)),
            'manual': bool((r.data or {}).get('labelManual')),
            'eligible': r.id in nonempty and r.name in DEFAULT_TITLES and not (r.data or {}).get('labelManual'),
            'status': jobs[str(r.data['labelJobId'])].status if str((r.data or {}).get('labelJobId')) in jobs else 'idle'} for r in rows]


    @app.post("/api/AIChats/Labels")
    async def conversation_labels_generate(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        from eic.ai.conversation_labels import stage
        ids = payload.get('ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
            raise HTTPException(400, '请选择1至200个会话')
        mode = 'history' if payload.get('history') is True else 'manual'
        expected_version = payload.get('expectedVersion')
        if mode == 'manual' and (len(ids) != 1 or type(expected_version) is not int or expected_version < 0):
            raise HTTPException(409, '请重新打开标题管理后确认生成')
        jobs = []
        for chat_id in dict.fromkeys(dependencies.require_id(v, '会话 ID 无效') for v in ids):
            job = await stage(session, principal.tenant_id, principal.user_id, chat_id,
                mode=mode, confirm=payload.get('confirmOverwrite') is True,
                expected_version=expected_version if mode == 'manual' else None)
            if job:
                jobs.append(str(job.id))
        await session.commit()
        return {'jobIds': jobs}


    @app.put("/api/AIChats/Label")
    async def conversation_label_rename(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        from eic.ai.conversation_labels import owned
        await dependencies.new_id(session)
        chat, row = await owned(session, principal.tenant_id, principal.user_id, dependencies.require_id(payload.get('id'), '会话 ID 无效'))
        title = str(payload.get('title') or '').strip()
        if not 1 <= len(title) <= 120:
            raise HTTPException(400, '标题需为1至120字符')
        chat.title = row.name = title
        row.data = {**(row.data or {}), 'labelManual': True,
            'labelVersion': int((row.data or {}).get('labelVersion', 0)) + 1}
        await session.commit()
        return True


    @app.delete("/api/AIChats/Delete")
    async def chats_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        return await dependencies.delete_resource("chats", Id, principal, session)


    from eic.core.chat_ownership import require_owned_chat_session, require_owned_chat_request


    def normalize_chat_request_data(payload: dict[str, Any], session_id: int, fallback_app_id: Any = None) -> dict[str, Any]:
        data = {key: value for key, value in payload.items() if key not in {"id", "appId", "aIAppsId", "cancelled", "reasoningSelection"}}
        data["aIChatsId"] = session_id
        data["content"] = str(payload.get("content") or "")
        app_ids = [
            dependencies.require_id(payload[key], "智能体 ID 无效")
            for key in ("appId", "aIAppsId") if payload.get(key) not in (None, "")
        ]
        if app_ids and (len(set(app_ids)) != 1 or not 0 < app_ids[0] < 2**63):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "智能体别名字段无效或不一致"})
        if app_ids:
            data["appId"] = app_ids[0]
        elif fallback_app_id not in (None, ""):
            data["appId"] = dependencies.require_id(fallback_app_id, "智能体 ID 无效")
        return data


    @app.post("/api/AIChats/ClearMine")
    async def chats_clear_mine(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        confirm = str((payload or {}).get("confirmText") or (payload or {}).get("confirm") or "").strip()
        if confirm != "确认一键删除":
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "请输入「确认一键删除」后再执行"})
        chats = list(await session.scalars(select(Resource).where(
            Resource.tenant_id == principal.tenant_id,
            Resource.resource_type == "chats",
            Resource.owner_id == principal.user_id,
            Resource.is_deleted.is_(False),
        )))
        for item in chats:
            item.is_deleted = True
        owned_sessions = list(await session.scalars(select(ChatSession).where(
            ChatSession.tenant_id == principal.tenant_id,
            ChatSession.owner_id == principal.user_id,
        )))
        session_ids = [item.id for item in owned_sessions]
        attachment_paths = []
        for session_id in set(session_ids + [item.id for item in chats]):
            attachment_paths.extend(await chat_attachments.cleanup_conversation(session_id, principal, session))
        if session_ids:
            for session_id in session_ids:
                await memory_lifecycle.clear_context(session, principal.tenant_id, principal.user_id,
                                                      session_id, detach_memories=True)
            memory_scope = (
                AgentMemory.tenant_id == principal.tenant_id,
                AgentMemory.owner_id == principal.user_id,
                AgentMemory.session_id.in_(session_ids),
            )
            await session.execute(delete(AgentMemory).where(
                *memory_scope, AgentMemory.memory_type.in_(SESSION_MEMORY_TYPES),
            ))
            # 清空聊天不等于删除长期记忆；保留长期事实和运行审计，解除会话外键。
            await session.execute(update(AgentMemory).where(*memory_scope).values(session_id=None))
            await session.execute(update(AgentRun).where(
                AgentRun.tenant_id == principal.tenant_id, AgentRun.session_id.in_(session_ids),
            ).values(session_id=None))
            await session.execute(delete(ChatMessage).where(ChatMessage.tenant_id == principal.tenant_id, ChatMessage.session_id.in_(session_ids)))
            await session.execute(delete(ChatSession).where(ChatSession.tenant_id == principal.tenant_id, ChatSession.id.in_(session_ids)))
        await session.commit()
        await chat_attachments.purge_paths(attachment_paths)
        return {"ok": True, "deleted": len(chats), "sessions": len(session_ids)}


    async def chat_reasoning_context(session_id, principal, session):
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        chat = await require_owned_chat_session(session, principal, session_id)
        resource = await session.get(Resource, session_id)
        data = (resource.data or {}) if resource else {}
        config = await dependencies.runtime_app_config(session, principal.tenant_id, chat.app_id or data.get("appId") or data.get("aIAppsId"))
        model_id = chat_model_id(config)
        if model_id:
            model = await require_runtime_model(session, principal.tenant_id, model_id, "chat")
        else:
            default = await session.scalar(select(Resource).where(Resource.tenant_id == principal.tenant_id,
                Resource.resource_type == "tenant_chat_default", Resource.is_deleted.is_(False)))
            default_id = dependencies.first_int((default.data or {}).get("modelId")) if default else None
            model = await session.scalar(select(AIModel).where(AIModel.tenant_id == principal.tenant_id,
                AIModel.model_type == "chat", AIModel.enabled.is_(True)).order_by(
                    (AIModel.id == default_id).desc(), AIModel.updated_at.desc()))
        if model is None:
            raise HTTPException(409, detail={"errMsg": "尚未配置可用聊天模型"})
        return resource, model


    @app.get("/api/AIChats/Reasoning")
    async def chat_reasoning_get(sessionId: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        from eic.ai.reasoning_policy import capabilities
        resource, model = await chat_reasoning_context(sessionId, principal, session)
        try:
            return capabilities(model, (resource.data or {}).get("reasoningSelection") if resource else None)
        except (ValueError, ChatConfigurationError):
            raise HTTPException(400, detail={"errMsg": "模型思考配置无效，请联系管理员修正"}) from None


    @app.put("/api/AIChats/Reasoning")
    async def chat_reasoning_save(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        from eic.ai.reasoning_policy import capabilities
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        resource, model = await chat_reasoning_context(dependencies.require_id(payload.get("sessionId"), "会话 ID 无效"), principal, session)
        if resource is None:
            raise HTTPException(404, detail={"errMsg": "会话不存在"})
        selection = payload.get("selection")
        try:
            info = capabilities(model, selection)
        except (ValueError, ChatConfigurationError):
            raise HTTPException(400, detail={"errMsg": "思考配置或选择无效，请刷新后重新选择"}) from None
        if selection is not None and info["effective"]["adjusted"]:
            raise HTTPException(409, detail={"errMsg": "模型能力已变化，请刷新后重新选择"})
        resource.data = {**(resource.data or {}), "reasoningSelection": selection}
        await session.commit()
        return info


    @app.post("/api/AIChatHistorys/GetPageData")
    async def chat_history_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        owned_sessions = select(ChatSession.id).where(ChatSession.tenant_id == principal.tenant_id, ChatSession.owner_id == principal.user_id)
        statement = select(ChatMessage).where(ChatMessage.tenant_id == principal.tenant_id, ChatMessage.session_id.in_(owned_sessions))
        extra = payload.model_extra or {}
        chat_id = extra.get("aIChatsId") or extra.get("whereId")
        if chat_id:
            statement = statement.where(ChatMessage.session_id == int(chat_id))
        total = await session.scalar(select(func.count()).select_from(
            statement.with_only_columns(ChatMessage.id).subquery())) or 0
        messages = list(await session.scalars(
            statement.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize)
        ))
        result = []
        may_read_files = await dependencies.has_permission("file.read", principal, session)
        async def resources_by_ids(identities, resource_type):
            resources = {}
            identities = sorted(set(identities))
            for offset in range(0, len(identities), 200):
                resources.update({item.id: item for item in await session.scalars(select(Resource).where(
                    Resource.id.in_(identities[offset:offset + 200]),
                    Resource.tenant_id == principal.tenant_id, Resource.resource_type == resource_type,
                    Resource.is_deleted.is_(False),
                ).execution_options(populate_existing=True))})
            return resources

        message_resources = await resources_by_ids(
            [message.id for message in messages if message.role == "user"], "chat_messages")
        file_ids_by_message = {}
        for message_id, resource in message_resources.items():
            if resource.owner_id == principal.user_id:
                file_ids_by_message[message_id] = [
                    (file_id, valid_file_id(file_id)) for file_id in (resource.data or {}).get("fileIds") or []
                ]
        file_resources = (await resources_by_ids(
            [identity for items in file_ids_by_message.values() for _, identity in items], "files",
        )) if may_read_files else {}
        for message in messages:
            item = {"id": message.id, "requestId": message.request_id, "aIChatsId": message.session_id, "content": message.content, "isSend": message.role == "user", "status": message.status, "tokenUsage": message.token_usage, "citations": (message.token_usage or {}).get("citations") or [], "createdAt": message.created_at.isoformat(), "createTime": message.created_at.isoformat()}
            files = []
            for file_id, identity in file_ids_by_message.get(message.id, []):
                file = file_resources.get(identity)
                if may_read_files and can_access_file(file, principal.tenant_id, principal.user_id):
                    files.append({**dependencies.serialize_file(file), "available": True})
                else:
                    files.append({"id": file_id, "name": "附件已不可访问", "available": False})
            item.update(attachments=files, fileIds=[file["id"] for file in files],
                        fileNames=",".join(file["name"] for file in files),
                        contentFileUrls=",".join(file.get("url", "") for file in files))
            result.append(item)
        return dependencies.page_result(result, payload, total)


    @app.post("/api/AIChatHistorys/Add")
    async def chat_history_add(payload: ChatMessageWrite, request: Request, response: Response, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        payload = write_payload(payload, ChatMessageWrite)
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        payload = await dependencies.normalize_chat_files(payload, principal, session)
        if type(payload.get('isOnlineSearch', False)) is not bool:
            raise HTTPException(400, detail={'errMsg': 'isOnlineSearch 必须为布尔值'})
        request_id = str(payload.get("id") or await dependencies.new_id(session))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "对话请求 ID 无效"})
        previous_rows = list(await session.scalars(select(ChatMessage).where(
            ChatMessage.tenant_id == principal.tenant_id, ChatMessage.request_id == request_id,
            ChatMessage.role == "user",
        ).limit(2)))
        if len(previous_rows) > 1:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "历史对话请求 ID 存在歧义"})
        previous = previous_rows[0] if previous_rows else None
        message_id = previous.id if previous is not None else (int(request_id) if request_id.isdigit() else await dependencies.new_id(session))
        if not 0 < message_id < 2**63:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "对话请求 ID 超出范围"})
        if previous is None and request_id.isdigit():
            message_id = await dependencies.new_entity_id(session, message_id)
        existing = await session.get(Resource, message_id)
        if existing is not None and (
            existing.tenant_id != principal.tenant_id or existing.owner_id != principal.user_id
            or existing.resource_type != "chat_messages" or existing.is_deleted
        ):
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "对话请求 ID 冲突"})
        if existing is not None or previous is not None:
            if existing is None or previous is None or previous.id != message_id:
                raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "对话请求 ID 冲突"})
            await require_owned_chat_request(session, principal, request_id)
            retry_session_id = dependencies.require_id(payload["aIChatsId"], "会话 ID 无效") if payload.get("aIChatsId") else previous.session_id
            old_data = existing.data or {}
            fallback_app_id = old_data.get("appId") or old_data.get("aIAppsId")
            retried = normalize_chat_request_data(payload, retry_session_id, fallback_app_id)
            normalized_old_data = await dependencies.normalize_chat_files(old_data, principal, session)
            original = normalize_chat_request_data(normalized_old_data, previous.session_id, fallback_app_id)
            if retried != original or retried["content"] != previous.content:
                raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "对话请求 ID 已用于不同内容"})
            response.set_cookie(
                key="eic_stream_token", value=create_access_token(principal.user_id, principal.tenant_id, principal.username, auth_stamp=principal.auth_stamp, login_session_id=principal.login_session_id, session_expires_at=principal.session_expires_at),
                httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=300, path="/",
            )
            return {"id": request_id, "aIChatsId": previous.session_id, "content": previous.content, "isSend": True, "streamUrl": f"/api/AIChatHistorys/Stream?requestId={request_id}"}
        if payload.get("fileIds"):
            if not chat_attachments.enabled(runtime.settings):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "附件正文功能尚未启用，本次未发送"})
            try:
                await chat_attachments.load_context(payload["fileIds"], principal, session, runtime.settings)
            except ChatConfigurationError as exc:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": dependencies.attachment_error_message(exc.reason)}) from exc
        requested_session_id = dependencies.require_id(payload["aIChatsId"], "会话 ID 无效") if payload.get("aIChatsId") else 0
        if requested_session_id and not 0 < requested_session_id < 2**63:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "会话 ID 无效"})
        chat_session = await session.get(ChatSession, requested_session_id) if requested_session_id else None
        if chat_session is not None and (chat_session.tenant_id != principal.tenant_id or chat_session.owner_id != principal.user_id):
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "会话不存在"})
        chat_resource = await session.get(Resource, requested_session_id) if requested_session_id else None
        if chat_resource is not None and (
            chat_resource.tenant_id != principal.tenant_id
            or chat_resource.resource_type != "chats"
            or chat_resource.is_deleted
            or chat_resource.owner_id != principal.user_id
        ):
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "会话不存在"})
        if requested_session_id and chat_session is None and chat_resource is None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "会话不存在"})
        normalized_input = normalize_chat_request_data(payload, requested_session_id)
        if chat_session is None:
            app_id = normalized_input.get("appId")
            if not app_id and chat_resource is not None:
                app_id = (chat_resource.data or {}).get("appId") or (chat_resource.data or {}).get("aIAppsId")
            resolved_app_id = None
            if app_id:
                ai_app = await session.get(AIApp, int(app_id))
                if ai_app is not None and ai_app.tenant_id == principal.tenant_id:
                    resolved_app_id = ai_app.id
            session_id = chat_resource.id if chat_resource is not None else await dependencies.new_id(session)
            chat_session = ChatSession(
                id=session_id, tenant_id=principal.tenant_id, owner_id=principal.user_id,
                app_id=resolved_app_id, title=str((chat_resource.name if chat_resource else None) or payload.get("content") or "")[:120],
            )
            session.add(chat_session)
            await session.flush()
        fallback_app_id = chat_session.app_id
        if not fallback_app_id and chat_resource is not None:
            fallback_app_id = (chat_resource.data or {}).get("appId") or (chat_resource.data or {}).get("aIAppsId")
        message_data = normalize_chat_request_data(payload, chat_session.id, fallback_app_id)
        await chat_attachments.bind_to_conversation(message_data.get('fileIds') or [], principal, session, chat_session.id)
        if chat_resource is not None and chat_resource.name in {'', '新对话', '新会话'} and not (chat_resource.data or {}).get('labelManual'):
            temporary_title = re.sub(r'\s+', ' ', str(payload.get('content') or '')).strip()[:20]
            if temporary_title:
                chat_resource.name = chat_session.title = temporary_title
                chat_resource.data = {**(chat_resource.data or {}), 'labelTemporary': True}
        # 冻结服务器保存的会话选择；请求重连复用该快照，不受后续设置影响。
        message_data["reasoningSelection"] = (chat_resource.data or {}).get("reasoningSelection") if chat_resource else None
        session.add(
            Resource(
                id=message_id,
                tenant_id=principal.tenant_id,
                resource_type="chat_messages",
                name=str(payload.get("content") or ""),
                owner_id=principal.user_id,
                data=message_data,
            )
        )
        session.add(ChatMessage(
            id=message_id, tenant_id=principal.tenant_id, session_id=chat_session.id, role="user",
            content=message_data["content"], request_id=request_id, token_usage={}, status="streaming",
        ))
        context = observability.CONTEXT.get()
        if context is not None:
            context.update(request_id=request_id, chat_id=str(chat_session.id),
                           trace_id=observability.turn_trace(principal.tenant_id, request_id))
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "对话请求 ID 冲突，请使用新的 ID"}) from exc
        response.set_cookie(
            key="eic_stream_token", value=create_access_token(principal.user_id, principal.tenant_id, principal.username, auth_stamp=principal.auth_stamp, login_session_id=principal.login_session_id, session_expires_at=principal.session_expires_at),
            httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=300, path="/",
        )
        return {"id": request_id, "aIChatsId": chat_session.id, "content": payload.get("content", ""), "isSend": True, "streamUrl": f"/api/AIChatHistorys/Stream?requestId={request_id}"}


    @app.delete("/api/AIChatHistorys/Delete")
    async def chat_history_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        message = await session.get(ChatMessage, Id)
        if message is not None and message.tenant_id == principal.tenant_id:
            await require_owned_chat_session(session, principal, message.session_id, allow_deleted=True)
            message_resource = await session.get(Resource, message.id)
            if message_resource is not None:
                if (
                    message_resource.tenant_id != principal.tenant_id
                    or message_resource.resource_type != "chat_messages"
                    or message_resource.owner_id != principal.user_id
                ):
                    raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "对话消息不存在"})
                message_resource.is_deleted = True
            await memory_lifecycle.clear_context(session, principal.tenant_id, principal.user_id, message.session_id)
            await session.delete(message)
            await session.commit()
            return True
        return await dependencies.delete_resource("chat_messages", Id, principal, session)


    @app.post("/api/AIChatHistorys/Compact")
    async def chat_history_compact(chatId: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        chat = await require_owned_chat_session(session, principal, chatId)
        config = await dependencies.runtime_app_config(session, principal.tenant_id, chat.app_id)
        try:
            job = await conversation_context.stage_compaction(session, tenant_id=principal.tenant_id,
                owner_id=principal.user_id, session_id=chatId, config=config, force=True)
        except ValueError:
            raise HTTPException(409, detail={"errMsg": "摘要来源无法装入模型输入预算，或摘要配置不可用；请检查模型和摘要设置"}) from None
        await session.commit()
        return {"queued": job is not None, "jobId": str(job.id) if job else None, "chatId": chatId}


    @app.get("/api/AIChatHistorys/ContextStatus")
    async def chat_context_status(chatId: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        chat = await require_owned_chat_session(session, principal, chatId)
        from eic.models import ContextJob, ChatSummary
        jobs = list(await session.scalars(select(ContextJob).where(ContextJob.tenant_id == principal.tenant_id,
            ContextJob.owner_id == principal.user_id, ContextJob.session_id == chatId)
            .order_by(ContextJob.id.desc()).limit(10)))
        summary = await session.scalar(select(ChatSummary).where(ChatSummary.tenant_id == principal.tenant_id,
            ChatSummary.owner_id == principal.user_id, ChatSummary.session_id == chatId)
            .order_by(ChatSummary.version.desc()).limit(1))
        config = await dependencies.runtime_app_config(session, principal.tenant_id, chat.app_id)
        return {"contextMode": context_settings(config)["mode"], "memoryMode": memory_settings(config)["mode"],
            "summary": {"version": summary.version, "throughId": str(summary.through_id), "status": summary.status} if summary else None,
            "jobs": [{"id": str(job.id), "kind": job.kind, "status": job.status, "errorCode": job.error_code} for job in jobs]}

    return _RouteExports(
        chats_page=chats_page,
        chats_add=chats_add,
        chats_edit=chats_edit,
        chat_citation_source=chat_citation_source,
        conversation_label_model_get=conversation_label_model_get,
        conversation_label_model_save=conversation_label_model_save,
        conversation_labels_list=conversation_labels_list,
        conversation_labels_generate=conversation_labels_generate,
        conversation_label_rename=conversation_label_rename,
        chats_delete=chats_delete,
        require_owned_chat_session=require_owned_chat_session,
        require_owned_chat_request=require_owned_chat_request,
        normalize_chat_request_data=normalize_chat_request_data,
        chats_clear_mine=chats_clear_mine,
        chat_reasoning_context=chat_reasoning_context,
        chat_reasoning_get=chat_reasoning_get,
        chat_reasoning_save=chat_reasoning_save,
        chat_history_page=chat_history_page,
        chat_history_add=chat_history_add,
        chat_history_delete=chat_history_delete,
        chat_history_compact=chat_history_compact,
        chat_context_status=chat_context_status,
    )

"""resources 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from eic.api.resource_inputs import RESOURCE_INPUTS, ResourceWrite, resource_payload
from eic.models import AIApp
from eic.models import AIPrompt
from sqlalchemy.ext.asyncio import AsyncSession
from eic.models import ChatMessage
from eic.models import ChatSession
from fastapi import Depends
from fastapi import HTTPException
from eic.models import KnowledgeBase
from eic.models import KnowledgeChunk
from eic.models import KnowledgeDocument
from eic.api.schemas import PageInput as PageInput
from eic.core.authentication import Principal
from fastapi import Request
from eic.models import Resource
from eic.ai.response_contract import ResponseContractError
from eic.models import Tenant
import asyncio
from eic.ai import attachments as chat_attachments
from eic.ai import declarative_skills
from eic.core.security import encrypt_secret
from eic.core.file_access import file_access_mode
from sqlalchemy import func
from eic.core.database import get_session
from eic.core import memory_lifecycle
from eic import model_lifecycle
from eic.ai.context_policy import normalize_context_config
from urllib.parse import parse_qs
from eic.ai import prompt_templates
import re
from eic.core.message_reads import read_fields
from eic.core.message_reads import receipts
from eic.core.file_access import require_file_access
from eic.core.runtime_refs import require_runtime_model
from eic.knowledge.ops import retrieval_public_fields
from sqlalchemy import select
from sqlalchemy import update
from urllib.parse import urlsplit
from eic.core.file_access import valid_file_id
from eic.ai.skill_manager import validate_skill_archive


@dataclass(frozen=True)
class Dependencies:
    app_response_schema: Callable[..., Any]
    attach_operation_logs: Callable[..., Any]
    coerce_id: Callable[..., Any]
    coerce_id_list: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    file_source_path: Callable[..., Any]
    first_int: Callable[..., Any]
    new_entity_id: Callable[..., Any]
    new_id: Callable[..., Any]
    page_result: Callable[..., Any]
    require_id: Callable[..., Any]
    skill_tool_type_value: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    def message_type_aliases(kind: str | None) -> set[str]:
        key = str(kind or "").strip().lower()
        mapping = {
            "announcement": {"announcement", "1"},
            "system": {"system", "2"},
            "private-system": {"private-system", "private_system", "3"},
            "private-user": {"private-user", "private_user", "4"},
            "ai-user": {"ai-user", "ai_user", "5"},
        }
        for canonical, aliases in mapping.items():
            if key == canonical or key in aliases:
                return {canonical, *aliases}
        return {key} if key else set()


    def normalize_message_type(value: Any) -> str:
        if value in (None, ""):
            return "announcement"
        aliases = message_type_aliases(str(value))
        for canonical in ("announcement", "system", "private-system", "private-user", "ai-user"):
            if canonical in aliases:
                return canonical
        return str(value)


    def serialize_resource(item: Resource) -> dict[str, Any]:
        public_data = {
            key: value
            for key, value in (item.data or {}).items()
            if key not in {"secret_encrypted", "headers_encrypted", "modelKey", "ModelKey", "secretKey", "SecretKey", "apiKey"}
        }
        description = str(public_data.get("describe") or public_data.get("description") or "")
        created = item.created_at.isoformat() if item.created_at else ""
        updated = item.updated_at.isoformat() if item.updated_at else created
        type_alias = public_data.get("type") or public_data.get("appType")
        if not type_alias and item.resource_type == "ai_apps":
            type_alias = "chat"
        payload = {
            "id": item.id,
            "name": item.name,
            **public_data,
            "describe": description,
            "description": description,
            "type": type_alias,
            "temperature": public_data.get("temperature", 50),
            "relevance": public_data.get("relevance", 50),
            "secretConfigured": bool((item.data or {}).get("secret_encrypted") or (item.data or {}).get("headers_encrypted")),
            "createdAt": created,
            "createTime": created,
            "updateTime": updated,
            "appId": public_data.get("appId") or public_data.get("aIAppsId"),
            "lastMessage": public_data.get("lastMessage", ""),
        }
        if item.resource_type == 'chats':
            payload['name'] = item.name
        if item.resource_type == "messages":
            content = str(public_data.get("messageContent") or public_data.get("content") or item.name or "")
            author = str(public_data.get("createUser") or public_data.get("sendUserName") or "")
            msg_type = normalize_message_type(public_data.get("type") or public_data.get("sysMsgType"))
            payload.update({
                "title": public_data.get("title") or item.name,
                "messageContent": content,
                "content": content,
                "createUser": author,
                "sendUserName": author,
                "createdTime": created,
                "createTime": created,
                "sysMsgType": public_data.get("sysMsgType") or msg_type,
                "type": msg_type,
                **read_fields(public_data),
            })
        elif item.resource_type == "dictionaries":
            flag = public_data.get("isSystem")
            if flag in (True, 1, "1", "\u662f"):
                is_system = "\u662f"
            elif flag in (False, 0, "0", "\u5426"):
                is_system = "\u5426"
            else:
                is_system = str(flag or "\u5426")
            payload.update({
                "key": public_data.get("key") or item.name,
                "value": public_data.get("value") or "",
                "isSystem": is_system,
                "sort": public_data.get("sort", 0),
                "remarks": public_data.get("remarks") or "",
                "type": public_data.get("type") or "default",
                "createTime": created,
            })
        if item.resource_type == "skills":
            # 包内名称保留在声明中；列表和详情使用用户维护的展示名。
            payload.update(name=item.name, executionMode="metadata", executionEnabled=False)
        return payload


    def serialize_file(item: Resource) -> dict[str, Any]:
        # 不向其他用户暴露本机 path；文件 URL 始终经过认证和对象授权。
        return {
            "id": item.id, "name": item.name, "size": (item.data or {}).get("size", 0),
            "contentType": (item.data or {}).get("contentType", "application/octet-stream"),
            "url": f"/api/File/GetFile?fileid={item.id}", "accessMode": file_access_mode(item),
        }


    async def validate_shared_file_payload(payload: dict[str, Any], principal: Principal, session: AsyncSession) -> None:
        ids = []
        if payload.get("fileIds") is not None:
            if not isinstance(payload["fileIds"], list):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "fileIds 必须是数组"})
            ids.extend(payload["fileIds"])
        if "aIKmssDetailsList" in payload and "kmsDetailsList" in payload and payload["aIKmssDetailsList"] != payload["kmsDetailsList"]:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "知识库文档别名字段不一致"})
        details = payload.get("aIKmssDetailsList", payload.get("kmsDetailsList", []))
        if not isinstance(details, list):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "知识库文档列表无效"})
        for detail in details:
            if not isinstance(detail, dict):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "知识库文档字段无效"})
            file = detail.get("file") or {}
            if not isinstance(file, dict):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文件引用字段无效"})
            aliases = [valid_file_id(value) for value in (detail.get("fileId"), file.get("id")) if value not in (None, "")]
            if len(set(aliases)) > 1:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文件别名字段不一致"})
            ids.extend(aliases)
        if ids:
            await dependencies.ensure_permission("file.read", principal, session)
        for value in sorted(set(valid_file_id(value) for value in ids)):
            await require_file_access(session, principal, value, shared=True)


    async def normalize_chat_files(payload: dict[str, Any], principal: Principal, session: AsyncSession) -> dict[str, Any]:
        values = payload.get("fileIds", [])
        if not isinstance(values, list):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "fileIds 必须是数组"})
        ids = [valid_file_id(value) for value in values]
        if any(key in payload for key in ("fileId", "attachments", "files", "file")):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "附件请使用 fileIds 字段"})
        urls = payload.get("contentFileUrls") or ""
        if not isinstance(urls, str):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "附件 URL 字段无效"})
        url_ids = []
        for raw in filter(None, urls.split(",")):
            try:
                url = urlsplit(raw.strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "附件 URL 字段无效"}) from exc
            query = parse_qs(url.query, keep_blank_values=True)
            matches = query.get("fileid", [])
            if url.scheme or url.netloc or url.fragment or url.path not in {"/api/File/GetFile", "/api/File/GetFilePath"} or len(matches) != 1 or set(query) != {"fileid"}:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "附件仅接受受控文件 ID，不接受任意 URL"})
            url_ids.append(valid_file_id(matches[0]))
        if ids and url_ids and ids != url_ids:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "附件 ID 与 URL 不一致"})
        ids = ids or url_ids
        if not ids:
            if payload.get("fileNames"):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "附件缺少有效文件 ID"})
            if any(key in payload for key in ("fileIds", "fileNames", "contentFileUrls")):
                return {**payload, "fileIds": [], "fileNames": "", "contentFileUrls": ""}
            return payload
        await dependencies.ensure_permission("file.read", principal, session)
        files = [await require_file_access(session, principal, value) for value in dict.fromkeys(ids)]
        return {
            **payload, "fileIds": [file.id for file in files], "fileNames": ",".join(file.name for file in files),
            "contentFileUrls": ",".join(f"/api/File/GetFile?fileid={file.id}" for file in files),
        }


    async def require_no_shared_file_references(session: AsyncSession, item: Resource) -> None:
        linked = await session.scalar(select(KnowledgeDocument.id).where(
            KnowledgeDocument.tenant_id == item.tenant_id, KnowledgeDocument.file_id == item.id,
            KnowledgeDocument.is_deleted.is_(False),
        ).limit(1))
        if linked is not None:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "文件仍被活动知识库文档引用，请先解除引用"})
        historical = await session.scalar(select(KnowledgeDocument.id).where(
            KnowledgeDocument.tenant_id == item.tenant_id, KnowledgeDocument.file_id == item.id,
            KnowledgeDocument.is_deleted.is_(True),
        ).limit(1))
        if historical is not None:
            raise HTTPException(status_code=409, detail={"code": 409,
                "errMsg": "文件仍被已删除文档的历史记录保留，当前不允许删除或修改共享；需先制定历史保留与清理策略",
                "reason": "historical_document_reference_retained"})
        resources = await session.scalars(select(Resource).where(
            Resource.tenant_id == item.tenant_id, Resource.resource_type.in_(["knowledge_bases", "skills"]),
            Resource.is_deleted.is_(False),
        ))
        for resource in resources:
            data = resource.data or {}
            values = list(data.get("fileIds") or []) if isinstance(data.get("fileIds"), list) else []
            values.append(data.get("fileId"))
            if isinstance(data.get("skillFile"), dict):
                values.append(data["skillFile"].get("id"))
            for key in ("aIKmssDetailsList", "kmsDetailsList"):
                for detail in data.get(key, []) if isinstance(data.get(key), list) else []:
                    if isinstance(detail, dict):
                        values.append(detail.get("fileId"))
                        if isinstance(detail.get("file"), dict):
                            values.append(detail["file"].get("id"))
            if any(dependencies.coerce_id(value) == item.id for value in values):
                raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "文件仍被共享资源引用，请先解除引用"})


    async def resource_page(resource_type: str, page: PageInput, principal: Principal, session: AsyncSession, type_filter: str | None = None, skip_permission: bool = False) -> dict[str, Any]:
        if resource_type in {"http_logs", "os_logs"}:
            permission = "system.permission.read"
        elif resource_type == "skills":
            permission = "skill.read"
        elif resource_type == "knowledge_bases":
            permission = "knowledge.read"
        elif resource_type == "chats":
            permission = "ai.chat.read"
        else:
            permission = "ai.resource.read"
        if not skip_permission:
            await dependencies.ensure_permission(permission, principal, session)
        criteria = [Resource.resource_type == resource_type, Resource.tenant_id == principal.tenant_id, Resource.is_deleted.is_(False)]
        if resource_type == "chats":
            criteria.append(Resource.owner_id == principal.user_id)
        if page.searchKey:
            criteria.append(Resource.name.contains(page.searchKey))
        rows = list(await session.scalars(select(Resource).where(*criteria).order_by(Resource.updated_at.desc())))
        items = [serialize_resource(item) for item in rows]
        if type_filter:
            aliases = message_type_aliases(type_filter)
            items = [item for item in items if str(item.get("type") or item.get("sysMsgType") or "") in aliases]
        if resource_type == "skills":
            wanted = dependencies.skill_tool_type_value(page.parameter)
            if wanted is None:
                extra = getattr(page, "model_extra", None) or {}
                wanted = dependencies.skill_tool_type_value(extra.get("skillToolType"))
            if wanted is not None:
                items = [item for item in items if dependencies.skill_tool_type_value(item.get("skillToolType")) == wanted]
        total = len(items)
        start = max((page.pageNum - 1) * page.pageSize, 0)
        items = items[start:start + page.pageSize]
        if resource_type == "messages":
            states = await receipts(session, principal.tenant_id, principal.user_id, [int(item["id"]) for item in items])
            original_data = {row.id: row.data or {} for row in rows}
            for item in items:
                item.update(read_fields(original_data[int(item["id"])], states.get(int(item["id"]))))
        await enrich_resource_items(resource_type, items, session, principal.tenant_id)
        return dependencies.page_result(items, page, total)


    async def enrich_resource_items(resource_type: str, items: list[dict[str, Any]], session: AsyncSession, tenant_id: int) -> None:
        if not items:
            return
        ids = [dependencies.first_int(item.get("id")) for item in items]
        ids = [item_id for item_id in ids if item_id is not None]
        if not ids:
            return
        if resource_type == "knowledge_bases":
            count_rows = await session.execute(
                select(KnowledgeDocument.knowledge_base_id, func.count())
                .where(KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.knowledge_base_id.in_(ids),
                       KnowledgeDocument.is_deleted.is_(False))
                .group_by(KnowledgeDocument.knowledge_base_id)
            )
            counts = {knowledge_base_id: count for knowledge_base_id, count in count_rows}
            bases = {
                item.id: item
                for item in await session.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(ids)))
            }
            status_map = {"ready": 2, "queued": 1, "processing": 1, "building": 1,
                          "staged": 1, "parsed": 1, "lexical_only": 1, "pending": 0,
                          "failed": 3, "uncertain": 3, "cancelled": 3, "empty": 0}
            for item in items:
                knowledge_id = dependencies.first_int(item.get("id"))
                document_count = int(counts.get(knowledge_id, 0) or 0)
                item["documentCount"] = document_count
                base = bases.get(knowledge_id)
                raw_status = item.get("indexStatus") or (getattr(base, "status", None) if base is not None else None) or item.get("status")
                if isinstance(raw_status, int):
                    item["status"] = raw_status
                else:
                    key = str(raw_status or "empty").lower()
                    item["status"] = status_map.get(key, 0)
                    item["statusKey"] = key
                if base is not None:
                    item.update(retrieval_public_fields(base))
                description = str(item.get("description") or item.get("describe") or "").strip()
                item["description"] = description
                item["describe"] = description
                item["chunkCount"] = item.get("chunkCount") or 0
            chunk_rows = await session.execute(
                select(KnowledgeChunk.knowledge_base_id, func.count())
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.knowledge_base_id.in_(ids),
                       KnowledgeDocument.is_deleted.is_(False),
                       KnowledgeChunk.index_version == KnowledgeDocument.active_index_version,
                       KnowledgeDocument.active_source_revision == KnowledgeDocument.source_revision,
                       KnowledgeChunk.chunk_index >= 0)
                .group_by(KnowledgeChunk.knowledge_base_id)
            )
            chunk_counts = {knowledge_base_id: count for knowledge_base_id, count in chunk_rows}
            for item in items:
                knowledge_id = dependencies.first_int(item.get("id"))
                item["chunkCount"] = int(chunk_counts.get(knowledge_id, 0) or 0)
            return
        if resource_type == "ai_prompts":
            prompts = {item.id: item for item in await session.scalars(select(AIPrompt).where(AIPrompt.id.in_(ids)))}
            for item in items:
                prompt = prompts.get(dependencies.first_int(item.get("id")))
                if prompt is not None and prompt.content:
                    content = prompt.content
                else:
                    content = str(item.get("content") or item.get("prompt") or "")
                item["prompt"] = content
                item["content"] = content
                description = str(item.get("description") or item.get("describe") or "").strip()
                if not description and content:
                    description = content.split("。")[0].strip()[:80]
                item["description"] = description
                item["describe"] = description
            return
        if resource_type == "ai_apps":
            apps = {item.id: item for item in await session.scalars(select(AIApp).where(AIApp.id.in_(ids)))}
            for item in items:
                app = apps.get(dependencies.first_int(item.get("id")))
                if app is not None:
                    if app.chat_model_id:
                        item["chatModelID"] = app.chat_model_id
                        item["chatModelId"] = app.chat_model_id
                        if item.get("status") in (None, "", "needs_model_config"):
                            item["status"] = "ready"
                prompt_ids = dependencies.coerce_id_list(item.get("aiPromptIDs") if "aiPromptIDs" in item else None) or dependencies.coerce_id_list(
                    item.get("aiPromptID") or item.get("promptId") or item.get("aIPromptId") or (app.prompt_id if app is not None else None)
                )
                kms_ids = dependencies.coerce_id_list(item.get("kmsIds") if "kmsIds" in item else None) or dependencies.coerce_id_list(
                    item.get("kmsId") or item.get("knowledgeBaseId") or item.get("knowledge_base_id") or item.get("aIKmssId") or (app.knowledge_base_id if app is not None else None)
                )
                item["aiPromptIDs"] = prompt_ids
                item["aiPromptID"] = prompt_ids[0] if prompt_ids else None
                item["promptId"] = prompt_ids[0] if prompt_ids else None
                item["kmsIds"] = kms_ids
                item["kmsId"] = kms_ids[0] if kms_ids else None
                item["knowledgeBaseId"] = kms_ids[0] if kms_ids else None


    def resource_write_permission(resource_type: str) -> str:
        if resource_type == "skills":
            return "skill.write"
        if resource_type == "knowledge_bases":
            return "knowledge.write"
        if resource_type == "chats":
            return "ai.chat.write"
        return "ai.resource.write"


    async def lock_tenant_writes(session: AsyncSession, tenant_id: int) -> None:
        """串行化短创建事务，不在此锁内执行模型或队列调用。"""
        if session.new or session.dirty or session.deleted:
            raise RuntimeError("tenant creation lock requires a clean transaction")
        # 丢弃仅授权读取的旧快照，兼容 MySQL 的 REPEATABLE READ。
        await session.rollback()
        if session.get_bind().dialect.name == "sqlite":
            result = await session.execute(
                update(Tenant).where(Tenant.id == tenant_id, Tenant.active.is_(True)).values(code=Tenant.code)
            )
            available = result.rowcount == 1
        else:
            available = await session.scalar(
                select(Tenant.id).where(Tenant.id == tenant_id, Tenant.active.is_(True)).with_for_update()
            ) is not None
        if not available:
            raise HTTPException(status_code=401, detail={"code": 401, "errMsg": "租户不可用"})


    async def save_resource(resource_type: str, payload: dict[str, Any], principal: Principal, session: AsyncSession, request: Request | None = None, *, commit: bool = True, create_only: bool = False, existing_only: bool = False) -> bool:
        permission = resource_write_permission(resource_type)
        await dependencies.ensure_permission(permission, principal, session)
        payload = resource_payload(resource_type, payload)
        if resource_type in {"chats", "messages", "ai_prompts"}:
            await lock_tenant_writes(session, principal.tenant_id)
            if resource_type == 'chats':
                await dependencies.new_id(session)
        if resource_type in {'ai_apps', 'knowledge_bases'}:
            await model_lifecycle.lock_tenant(session, principal.tenant_id)
        if resource_type == "ai_prompts":
            await dependencies.ensure_permission(permission, principal, session)
            payload = dict(payload)
            if "prompt" not in payload and "content" in payload:
                payload["prompt"] = payload["content"]
            if "description" not in payload:
                payload["description"] = payload.get("describe", "")
            prompt_templates.validate_fields(payload)
            if "content" in payload and payload["content"] != payload["prompt"]:
                raise HTTPException(422, detail={"code": 422, "errMsg": "提示词正文字段不一致"})
            if "describe" in payload and payload["describe"] != payload["description"]:
                raise HTTPException(422, detail={"code": 422, "errMsg": "提示词描述字段不一致"})
        item_id = dependencies.coerce_id(payload.get("id"))
        item = await session.get(Resource, item_id) if item_id else None
        if existing_only and item is None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "记录不存在"})
        if item is not None and resource_type in {"chats", "chat_messages"} and item.owner_id != principal.user_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "session not found"})
        if item is not None and (item.tenant_id != principal.tenant_id or item.resource_type != resource_type or item.is_deleted):
            raise HTTPException(status_code=404, detail={"code": 400, "errMsg": "记录不存在"})
        name = str(payload.get("name") or payload.get("title") or "")
        if resource_type == 'chats' and item is not None and 'name' not in payload and 'title' not in payload:
            name = item.name
        clean = {
            key: value
            for key, value in payload.items()
            if key not in {"id", "modelKey", "ModelKey", "secretKey", "SecretKey", "apiKey"}
        }
        if resource_type == "ai_prompts":
            # 普通编辑保留服务器来源标识，不接受客户端伪造官方模板身份。
            for key in list(clean):
                if key == "source" or key.startswith("builtin"):
                    clean.pop(key)
            if item is not None:
                clean.update({key: value for key, value in (item.data or {}).items()
                              if key == "source" or key.startswith("builtin")})
                previous = item.data or {}
                if (name != item.name or clean["prompt"] != str(previous.get("content") or previous.get("prompt") or "")
                        or clean["description"] != str(previous.get("description") or previous.get("describe") or "")):
                    clean["builtinEdited"] = True
            clean["content"] = clean["prompt"]
            clean["describe"] = clean["description"]
        if resource_type == "chats":
            # 展示摘要的版本和来源标记只由服务端管理，普通会话编辑不得丢失。
            for key in list(clean):
                if key.startswith('label'):
                    clean.pop(key)
            if item is not None:
                clean.update({key: value for key, value in (item.data or {}).items() if key.startswith('label')})
                if name != item.name:
                    clean['labelManual'] = True
                    clean['labelVersion'] = int(clean.get('labelVersion', 0)) + 1
                if clean.get('labelSummary'):
                    clean['lastMessage'] = clean['labelSummary']
            clean.pop("reasoningSelection", None)
            if item is not None and "reasoningSelection" in (item.data or {}):
                clean["reasoningSelection"] = item.data["reasoningSelection"]
        if resource_type == "ai_apps":
            from eic.ai.runtime_policy import normalize_policy
            try:
                clean = normalize_context_config({**((item.data or {}) if item else {}), **clean})
                clean["runtimePolicy"] = normalize_policy(clean.get("runtimePolicy", (item.data or {}).get("runtimePolicy") if item else None))
            except ValueError as exc:
                raise HTTPException(400, detail={"errMsg": str(exc)}) from None
            if clean["runtimePolicy"] and clean["runtimePolicy"]["action"] == "degrade":
                for tier in clean["runtimePolicy"]["tiers"]:
                    await require_runtime_model(session, principal.tenant_id, int(tier["modelId"]), "chat")
            for key, model_type in (("summaryModelId", "chat"), ("extractionModelId", "chat"), ("embeddingModelId", "embedding")):
                model_id = (clean["contextSettings"] if key == "summaryModelId" else clean["memorySettings"]).get(key)
                if model_id:
                    await require_runtime_model(session, principal.tenant_id, int(model_id), model_type)
            try:
                schema = dependencies.app_response_schema(clean)
            except ResponseContractError as exc:
                raise HTTPException(422, detail={"code": 422, "errMsg": "结构化响应配置无效或与工具配置冲突",
                                                 "errorCode": str(exc)}) from None
            if "responseSchema" in clean:
                clean["responseSchema"] = schema
        if create_only and item is not None:
            if item.name != name or (item.data or {}) != clean:
                raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "创建 ID 已用于不同内容"})
            return True
        if resource_type == "messages":
            for field in ("isRead", "readState", "readAt", "legacyReadUnattributed"):
                clean.pop(field, None)
            if item is not None and "isRead" in (item.data or {}):
                clean["isRead"] = item.data["isRead"]
            title = str(payload.get("title") or payload.get("name") or "")
            content = str(payload.get("messageContent") or payload.get("content") or "")
            msg_type = normalize_message_type(payload.get("type") or payload.get("sysMsgType"))
            author = str(payload.get("createUser") or payload.get("sendUserName") or principal.username)
            name = title or name
            clean.update({
                "title": title or name,
                "content": content,
                "messageContent": content,
                "type": msg_type,
                "sysMsgType": payload.get("sysMsgType") or msg_type,
                "createUser": author,
                "sendUserName": author,
            })
        if resource_type == "dictionaries":
            dict_type = str(clean.get("type") or "default")
            dict_key = str(clean.get("key") or name)
            if not name:
                name = f"{dict_type}:{dict_key}"
            flag = clean.get("isSystem")
            if flag in (True, 1, "1", "\u662f"):
                clean["isSystem"] = "\u662f"
            elif flag in (False, 0, "0", "\u5426", None, ""):
                clean["isSystem"] = "\u5426"
            clean["type"] = dict_type
            clean["key"] = dict_key
        if resource_type == "ai_apps":
            app_secret = payload.get("secretKey") or payload.get("SecretKey")
            if app_secret:
                clean["secret_encrypted"] = encrypt_secret(str(app_secret))
            elif item is not None and (item.data or {}).get("secret_encrypted"):
                clean["secret_encrypted"] = (item.data or {})["secret_encrypted"]
        if resource_type == "skills":
            if item is not None and (item.data or {}).get("builtinKey"):
                raise HTTPException(409, detail={"code": 409, "errMsg": "内置技能使用独立启停接口管理"})
            for field in ("builtinKey", "builtinVersion", "contentHash", "executionMode",
                          "skillDocument", "skillManifest"):
                clean.pop(field, None)
            declared_type = payload.get("skillToolType")
            if declared_type is None and item is not None:
                declared_type = (item.data or {}).get("skillToolType")
            if declared_type is not None:
                clean["skillToolType"] = declared_type
            has_file_input = "skillFile" in payload or "fileId" in payload
            file_info = payload.get("skillFile") if has_file_input else (item.data or {}).get("skillFile") if item else None
            file_id = payload.get("fileId") if has_file_input else (item.data or {}).get("fileId") if item else None
            if file_info is not None and not isinstance(file_info, dict):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "技能文件引用无效"})
            aliases = [valid_file_id(value) for value in (file_id, (file_info or {}).get("id")) if value not in (None, "")]
            if len(set(aliases)) > 1:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "技能文件别名字段不一致"})
            if aliases:
                await dependencies.ensure_permission("file.read", principal, session)
                if payload.get("shareSkillFile") is True:
                    await dependencies.ensure_permission("file.write", principal, session)
                    bound_file = await require_file_access(session, principal, aliases[0], write=True)
                    if bound_file.owner_id != principal.user_id:
                        raise HTTPException(status_code=403, detail={"code": 403, "errMsg": "仅文件所有者可以确认共享"})
                else:
                    bound_file = await require_file_access(session, principal, aliases[0], shared=True)
                if dependencies.first_int(declared_type) == 2 or bound_file.name.lower().endswith(".zip"):
                    if not bound_file.name.lower().endswith(".zip"):
                        raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文件技能需要 ZIP 格式声明包"})
                    try:
                        await asyncio.to_thread(validate_skill_archive, dependencies.file_source_path(bound_file))
                        def read_package():
                            path = dependencies.file_source_path(bound_file)
                            with path.open("rb") as stream:
                                return declarative_skills.parse_skill_package(stream.read(131073))
                        package = await asyncio.to_thread(read_package)
                        clean.update(package.resource_data())
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail={"code": 400, "errMsg": str(exc)}) from exc
                if payload.get("shareSkillFile") is True:
                    bound_file.data = {**(bound_file.data or {}), "accessMode": "tenant"}
                clean.pop("shareSkillFile", None)
                clean["fileId"] = bound_file.id
                clean["skillFile"] = serialize_file(bound_file)
            else:
                if dependencies.first_int(declared_type) == 2:
                    raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文件技能需要有效 fileId 和 ZIP 声明包"})
                clean["fileId"], clean["skillFile"] = None, None
        if item is None:
            item = Resource(id=await dependencies.new_entity_id(session, item_id), tenant_id=principal.tenant_id, resource_type=resource_type, name=name, owner_id=principal.user_id, data=clean)
            session.add(item)
        else:
            item.name = name
            item.data = clean
        if resource_type == "chats":
            await session.flush()
            chat_session = await session.get(ChatSession, item.id)
            app_id_raw = clean.get("appId") or clean.get("aIAppsId")
            resolved_app_id = None
            if app_id_raw:
                ai_app = await session.get(AIApp, int(app_id_raw))
                if ai_app is not None and ai_app.tenant_id == principal.tenant_id:
                    resolved_app_id = ai_app.id
            if chat_session is None:
                session.add(ChatSession(
                    id=item.id, tenant_id=principal.tenant_id, owner_id=principal.user_id,
                    app_id=resolved_app_id, title=name[:120],
                ))
            else:
                if chat_session.tenant_id != principal.tenant_id or chat_session.owner_id != principal.user_id:
                    raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "会话不存在"})
                if name:
                    chat_session.title = name[:120]
                if resolved_app_id:
                    chat_session.app_id = resolved_app_id
        if resource_type == "skills":
            method_name = str(payload.get("classMethod") or "").strip()
            if method_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", method_name):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "方法名需使用英文、数字或下划线"})
        if resource_type == "ai_apps":
            await session.flush()
            chat_model_id = dependencies.first_int(clean.get("chatModelID"), clean.get("chatModelId"), clean.get("aIModelId"), clean.get("AIModelId"))
            prompt_ids = dependencies.coerce_id_list(clean.get("aiPromptIDs") if "aiPromptIDs" in clean else (clean.get("aiPromptID") or clean.get("promptId") or clean.get("aIPromptId")))
            kms_ids = dependencies.coerce_id_list(clean.get("kmsIds") if "kmsIds" in clean else (clean.get("kmsId") or clean.get("knowledgeBaseId") or clean.get("knowledge_base_id") or clean.get("aIKmssId")))
            clean["aiPromptIDs"] = prompt_ids
            clean["aiPromptID"] = prompt_ids[0] if prompt_ids else None
            clean["promptId"] = prompt_ids[0] if prompt_ids else None
            clean["kmsIds"] = kms_ids
            clean["kmsId"] = kms_ids[0] if kms_ids else None
            clean["knowledgeBaseId"] = kms_ids[0] if kms_ids else None
            item.data = clean
            prompt_id = prompt_ids[0] if prompt_ids else None
            knowledge_base_id = kms_ids[0] if kms_ids else None
            ai_app = await session.get(AIApp, item.id)
            if ai_app is None:
                session.add(AIApp(
                    id=item.id, tenant_id=principal.tenant_id, name=name or item.name,
                    description=str(clean.get("describe") or clean.get("description") or ""),
                    chat_model_id=chat_model_id, prompt_id=prompt_id, knowledge_base_id=knowledge_base_id,
                    config=clean, enabled=True,
                ))
            else:
                if ai_app.tenant_id != principal.tenant_id:
                    raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "智能体不存在"})
                ai_app.name = name or ai_app.name
                ai_app.description = str(clean.get("describe") or clean.get("description") or ai_app.description)
                ai_app.chat_model_id = chat_model_id
                ai_app.prompt_id = prompt_id
                ai_app.knowledge_base_id = knowledge_base_id
                ai_app.config = clean
                ai_app.enabled = True
        if resource_type == "ai_prompts":
            dedicated = await session.get(AIPrompt, item.id)
            if dedicated is not None and dedicated.tenant_id != principal.tenant_id:
                raise HTTPException(404, detail={"code": 404, "errMsg": "提示词不存在"})
            if dedicated is None:
                session.add(AIPrompt(id=item.id, tenant_id=principal.tenant_id,
                                    name=name, content=clean["prompt"], enabled=True))
            else:
                dedicated.name, dedicated.content = name, clean["prompt"]
        await dependencies.attach_operation_logs(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id, username=principal.username,
            operate_type="保存", operate_remark=f"保存 {resource_type} {name}", table=resource_type,
            table_id=item.id, payload=payload, request=request,
        )
        if commit:
            await session.commit()
        else:
            await session.flush()
        return True


    async def delete_resource(resource_type: str, item_id: int, principal: Principal, session: AsyncSession, request: Request | None = None) -> bool:
        if resource_type == 'knowledge_bases':
            await lock_tenant_writes(session, principal.tenant_id)
            await dependencies.new_id(session)
        if resource_type in {'chats', 'chat_messages'}:
            await lock_tenant_writes(session, principal.tenant_id)
        permission = resource_write_permission(resource_type)
        await dependencies.ensure_permission(permission, principal, session)
        if resource_type == "ai_prompts":
            await lock_tenant_writes(session, principal.tenant_id)
            await dependencies.ensure_permission(permission, principal, session)
        item = await session.get(Resource, item_id)
        if item is not None and resource_type in {"chats", "chat_messages"} and item.owner_id != principal.user_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "session not found"})
        if item is None or item.tenant_id != principal.tenant_id or item.resource_type != resource_type or item.is_deleted:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "记录不存在或已删除"})
        item.is_deleted = True
        attachment_paths = []
        if resource_type == 'knowledge_bases':
            from eic.knowledge.lifecycle import retire_base_documents
            await retire_base_documents(session, item)
        if resource_type == "chats":
            attachment_paths = await chat_attachments.cleanup_conversation(item.id, principal, session)
            await memory_lifecycle.clear_context(session, principal.tenant_id, principal.user_id, item.id)
        elif resource_type == "chat_messages":
            source = await session.get(ChatMessage, item.id)
            if source and source.tenant_id == principal.tenant_id:
                await memory_lifecycle.clear_context(session, principal.tenant_id, principal.user_id, source.session_id)
        if resource_type == "ai_apps":
            ai_app = await session.get(AIApp, item.id)
            if ai_app is not None and ai_app.tenant_id == principal.tenant_id:
                ai_app.enabled = False
        await dependencies.attach_operation_logs(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id, username=principal.username,
            operate_type="删除", operate_remark=f"删除 {resource_type} {item.name}", table=resource_type,
            table_id=item.id, payload={"id": item_id}, request=request,
        )
        await session.commit()
        await chat_attachments.purge_paths(attachment_paths)
        return True


    def register_resource_endpoints(prefix: str, resource_type: str) -> None:
        input_type = RESOURCE_INPUTS.get(resource_type, ResourceWrite)
        async def page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
            return await resource_page(resource_type, payload, principal, session)

        async def add_edit(payload: input_type, request: Request, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
            return await save_resource(resource_type, payload, principal, session, request)

        async def delete(Id: str | int, request: Request, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
            return await delete_resource(resource_type, dependencies.require_id(Id, "记录 ID 无效"), principal, session, request)

        app.post(f"/api/{prefix}/GetPageData", name=f"{prefix}_page")(page)
        app.post(f"/api/{prefix}/AddEdit", name=f"{prefix}_add_edit")(add_edit)
        app.delete(f"/api/{prefix}/Delete", name=f"{prefix}_delete")(delete)


    async def all_resources(resource_type: str, principal: Principal, session: AsyncSession) -> list[dict[str, Any]]:
        result = await session.scalars(select(Resource).where(Resource.tenant_id == principal.tenant_id, Resource.resource_type == resource_type, Resource.is_deleted.is_(False)).order_by(Resource.updated_at.desc()))
        items = [serialize_resource(item) for item in result]
        await enrich_resource_items(resource_type, items, session, principal.tenant_id)
        return items


    def empty_compatibility_page(payload: PageInput) -> dict[str, Any]:
        return dependencies.page_result([], payload, 0)

    return _RouteExports(
        message_type_aliases=message_type_aliases,
        normalize_message_type=normalize_message_type,
        serialize_resource=serialize_resource,
        serialize_file=serialize_file,
        validate_shared_file_payload=validate_shared_file_payload,
        normalize_chat_files=normalize_chat_files,
        require_no_shared_file_references=require_no_shared_file_references,
        resource_page=resource_page,
        enrich_resource_items=enrich_resource_items,
        resource_write_permission=resource_write_permission,
        lock_tenant_writes=lock_tenant_writes,
        save_resource=save_resource,
        delete_resource=delete_resource,
        register_resource_endpoints=register_resource_endpoints,
        all_resources=all_resources,
        empty_compatibility_page=empty_compatibility_page,
    )

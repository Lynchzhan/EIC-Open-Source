"""chat_stream 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from eic.models import AgentRun
from sqlalchemy.ext.asyncio import AsyncSession
from eic.models import ChatEvent
from fastapi import Cookie
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Header
from eic.core.authentication import Principal
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import StreamingResponse
from eic.core import agent_approval
from eic.core import agent_runtime
from eic.ai import attachments as chat_attachments
from eic.core.security import create_access_token
from eic.core.security import decode_access_token
from eic.core import generation
from eic.core.database import get_session
import json
import jwt
import re
from eic.core.authentication import resolve_identity
from sqlalchemy import select


@dataclass(frozen=True)
class Dependencies:
    ensure_permission: Callable[..., Any]
    has_permission: Callable[..., Any]
    require_id: Callable[..., Any]
    require_owned_chat_request: Callable[..., Any]
    require_owned_chat_session: Callable[..., Any]
    sse_events: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    @app.post("/api/AIChatHistorys/Cancel")
    async def chat_history_cancel(requestId: str, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        message, stored = await dependencies.require_owned_chat_request(session, principal, requestId)
        if stored.status == "approval_executing":
            raise agent_approval.conflict("tool_outcome_uncertain", "工具正在执行，结果需复核；请勿重复提交")
        changed = await generation.cancel_request(session, stored)
        if changed and message is not None:
            message.data = {**(message.data or {}), "cancelled": True}
        if changed:
            await agent_runtime.runtime_store.finish_for_request(
                session, tenant_id=principal.tenant_id, request_id=requestId, status="cancelled")
        await session.commit()
        return True


    @app.get("/api/AIChatHistorys/GetEvents")
    async def chat_events(requestId: str, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        await dependencies.require_owned_chat_request(session, principal, requestId, allow_deleted=True)
        events = await session.scalars(
            select(ChatEvent)
            .where(ChatEvent.tenant_id == principal.tenant_id, ChatEvent.request_id == requestId)
            .order_by(ChatEvent.sequence)
        )
        return [{"event": item.event_type, "sequence": item.sequence, "payload": json.loads(item.payload)} for item in events]


    @app.get("/api/AIChatHistorys/Budget")
    async def chat_budget(sessionId: str, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        chat = await dependencies.require_owned_chat_session(session, principal, dependencies.require_id(sessionId, "会话 ID 无效"))
        from eic.models import ConversationBudget
        from eic.ai.runtime_policy import Account
        row = await session.get(ConversationBudget, chat.id)
        if row is None:
            return {"configured": False}
        if row.tenant_id != principal.tenant_id or row.owner_id != principal.user_id:
            raise HTTPException(404, detail={"errMsg": "会话预算不存在"})
        state = row.state
        kinds = {}
        for entry in state["entries"].values():
            if entry["status"] == "not_invoked":
                continue
            item = kinds.setdefault(entry["kind"], {"calls": 0, "tokens": 0, "unknown": 0})
            item["calls"] += 1
            item["tokens"] += entry["tokens"]
            item["unknown"] += entry["status"] in {"pending", "unknown"}
        return {"configured": True, "totals": Account.totals(state), "limits": state["policy"],
                "tier": state["tier"] + 1, "reason": state["reason"], "byKind": kinds,
                "historical": state["history"], "reasonCode": state.get("reasonCode"),
                "blocked": state.get("blocked")}


    def attachment_error_message(reason: str) -> str:
        return {
            "attachment_context_not_supported": "附件正文功能尚未启用，本次未调用模型",
            "attachment_budget_exceeded": "附件总数、大小或正文长度超过配置预算",
            "attachment_format_unsupported": "支持 PDF、DOCX、TXT、MD、图片 OCR、Excel、CSV、PPTX 和 JSON；请转换不支持的格式",
            "attachment_parse_failed": "附件无法提取有效文字，请检查格式、加密状态及本地解析/OCR依赖；图片仅支持文字识别",
        }.get(reason, "附件正文不可用，本次未调用模型")


    @app.get("/api/AIChatHistorys/Capabilities")
    async def chat_capabilities(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        active = chat_attachments.enabled(runtime.settings)
        can_tools = await dependencies.has_permission("tool.execute", principal, session)
        return {"attachments": active, "attachmentFormats": sorted(chat_attachments.FORMATS) if active else [],
                "onlineSearch": True,
                'attachmentMaxFiles': runtime.settings.chat_attachment_max_files,
                'attachmentMaxBytes': runtime.settings.chat_attachment_max_bytes,
                'attachmentMaxChars': runtime.settings.chat_attachment_max_chars,
                'searchBackendConfigured': runtime.settings.chat_search_backend != 'none' and bool(runtime.settings.chat_search_endpoint),
                "skillExecution": can_tools and await dependencies.has_permission("skill.read", principal, session),
                "mcpExecution": can_tools and await dependencies.has_permission("mcp.call", principal, session),
                "skillDisplayMode": "declarative", "toolApproval": True, "traceSummary": True,
                "traceDetail": principal.is_super_admin}


    @app.post("/api/AIChatHistorys/Resume")
    async def chat_resume(requestId: str, request: Request, response: Response,
                          principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        _, stored = await dependencies.require_owned_chat_request(session, principal, requestId)
        run = await session.scalar(select(AgentRun).where(
            AgentRun.tenant_id == principal.tenant_id, AgentRun.request_id == requestId))
        response.set_cookie(key="eic_stream_token",
            value=create_access_token(principal.user_id, principal.tenant_id, principal.username, auth_stamp=principal.auth_stamp, login_session_id=principal.login_session_id, session_expires_at=principal.session_expires_at),
            httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=300, path="/")
        floor = int((run.run_metadata or {}).get("streamAfterSequence", 0)) if run and run.status == "running" else 0
        return {"id": requestId, "status": stored.status,
                "afterEventId": f"{requestId}-{floor}" if floor else None,
                "streamUrl": f"/api/AIChatHistorys/Stream?requestId={requestId}"}


    @app.get("/api/AIChatHistorys/Stream")
    async def chat_stream(requestId: str = Query(...), authorization: str | None = Header(default=None), eic_stream_token: str | None = Cookie(default=None), session: AsyncSession = Depends(get_session), last_event_id: str | None = Header(default=None, alias="Last-Event-ID"), afterEventId: str | None = Query(default=None)) -> StreamingResponse:
        raw_token = None
        if authorization and authorization.lower().startswith("bearer "):
            raw_token = authorization.split(" ", 1)[1].strip()
        if not raw_token:
            raw_token = eic_stream_token
        if not raw_token:
            raise HTTPException(status_code=401, detail={"code": 401, "errMsg": "\u6d41\u5f0f\u4f1a\u8bdd\u5df2\u5931\u6548"})
        try:
            token = decode_access_token(raw_token)
            principal = await resolve_identity(token, session)
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=401, detail={"code": 401, "errMsg": "\u6d41\u5f0f\u4f1a\u8bdd\u5df2\u5931\u6548"}) from exc
        await dependencies.ensure_permission("ai.chat.write", principal, session)
        await dependencies.require_owned_chat_request(session, principal, requestId)
        after_sequence = 0
        for cursor in (afterEventId, last_event_id):
            if not cursor:
                continue
            match = re.fullmatch(re.escape(requestId) + r"-(\d{1,10})", cursor)
            if match is None:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "流式事件游标无效"})
            # 自动重连更新 Header，但 EventSource 的初始 URL 不会变化。
            after_sequence = max(after_sequence, int(match.group(1)))
        return StreamingResponse(dependencies.sse_events(requestId, principal, after_sequence), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return _RouteExports(
        chat_history_cancel=chat_history_cancel,
        chat_events=chat_events,
        chat_budget=chat_budget,
        attachment_error_message=attachment_error_message,
        chat_capabilities=chat_capabilities,
        chat_resume=chat_resume,
        chat_stream=chat_stream,
    )

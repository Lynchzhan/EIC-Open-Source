"""chat_events 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.core.runtime_bindings import RuntimeBindings
from eic.models import ChatEvent
from eic.models import ChatMessage
from eic.core.authentication import Principal
from eic.models import Resource
from eic.core import agent_runtime
from eic.ai import search as chat_search
from sqlalchemy import func
from eic.core import generation
import json
from sqlalchemy import select
from eic.ai.budget import text_units


@dataclass(frozen=True)
class Dependencies:
    attachment_error_message: Callable[..., Any]
    new_id: Callable[..., Any]
    require_owned_chat_request: Callable[..., Any]


def bind_routes(*, dependencies: Dependencies, runtime: RuntimeBindings):
    def sse_payload(text: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if extra:
            for key, value in extra.items():
                if key != "text":
                    payload[key] = value
        return payload


    def compact_citations(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        for row in rows or []:
            content = str(row.get("childContent") or row.get("content") or "")
            citations.append({
                "documentId": row.get("documentId"),
                "documentName": row.get("documentName") or "",
                "chunkId": row.get("chunkId"),
                "pageNumbers": row.get("pageNumbers") or (row.get("metadata") or {}).get("pageNumbers") or [],
                "score": row.get("score"),
                "scoreSpace": row.get("scoreSpace"),
                "retrievalScore": row.get("retrievalScore"),
                "retrievalScoreSpace": row.get("retrievalScoreSpace"),
                "content": content[:240],
                "knowledgeBaseId": row.get("knowledgeBaseId"),
                "knowledgeBaseName": row.get("knowledgeBaseName") or "",
                "channel": row.get("channel"),
            })
        return citations


    def sse_message(request_id: str, sequence: int, event: str, text: str, extra: dict[str, Any] | None = None) -> str:
        return f"id: {request_id}-{sequence}\nevent: {event}\ndata: {json.dumps(sse_payload(text, extra), ensure_ascii=False)}\n\n"


    async def persist_chat_event(request_id: str, principal: Principal, sequence: int, event: str, text: str, extra: dict[str, Any] | None = None) -> None:
        async with runtime.sessions() as session:
            await generation.fence(session)
            session.add(
                ChatEvent(
                    id=await dependencies.new_id(session),
                    tenant_id=principal.tenant_id,
                    chat_id=int(request_id) if request_id.isdigit() else 0,
                    request_id=request_id,
                    event_type=event,
                    payload=json.dumps(sse_payload(text, extra), ensure_ascii=False),
                    sequence=sequence,
                )
            )
            await session.commit()


    async def audited_sse_message(request_id: str, principal: Principal, sequence: int, event: str, text: str, extra: dict[str, Any] | None = None) -> str:
        await persist_chat_event(request_id, principal, sequence, event, text, extra)
        return sse_message(request_id, sequence, event, text, extra)


    async def mark_chat_cancelled(request_id: str, principal: Principal) -> None:
        async with runtime.sessions() as session:
            await generation.fence(session)
            message, stored = await dependencies.require_owned_chat_request(session, principal, request_id)
            changed = await generation.cancel_request(session, stored) if stored is not None else False
            if message is not None and changed:
                data = dict(message.data or {})
                data["cancelled"] = True
                message.data = data
            await agent_runtime.runtime_store.finish_for_request(
                session,
                tenant_id=principal.tenant_id,
                request_id=request_id,
                status="cancelled",
                error_code="cancelled_by_user",
            )
            await session.commit()


    async def finalize_chat_response(
        request_id: str, principal: Principal, *, status: str, answer: str,
        token_usage: dict[str, Any], reason: str | None = None,
    ) -> list[str]:
        """终态、部分回答、错误和唯一 done 在同一 fenced 事务中提交。"""
        if status not in {"completed", "failed"}:
            raise ValueError("Invalid generation terminal status")
        partial = status == "failed" and bool(answer.strip())
        usage = {**token_usage, "partial": partial, "reason": reason, "automaticRetry": False}
        terminal = {"status": status, "partial": partial, "reason": reason, "retryable": False}
        chunks: list[str] = []
        async with runtime.sessions() as session:
            await generation.fence(session)
            _, user = await dependencies.require_owned_chat_request(session, principal, request_id)
            if user.status not in {"streaming", "generating"}:
                raise generation.LeaseLost("Generation already terminal")
            user.status, user.token_usage = status, usage
            await agent_runtime.runtime_store.finish_for_request(
                session,
                tenant_id=principal.tenant_id,
                request_id=request_id,
                status=status,
                error_code=reason,
                result={
                    "answerDigest": agent_runtime.digest_payload(answer),
                    "partial": partial,
                    "providerOutcome": token_usage.get("providerOutcome", "known"),
                    "degraded": token_usage.get("degraded", False),
                    "fallbackType": token_usage.get("fallbackType"),
                },
            )
            if answer.strip():
                session.add(ChatMessage(
                    id=await dependencies.new_id(session), tenant_id=principal.tenant_id,
                    session_id=user.session_id, role="assistant", content=answer,
                    request_id=request_id, token_usage=usage, status=status,
                ))
            sequence = (await session.scalar(select(func.max(ChatEvent.sequence)).where(
                ChatEvent.tenant_id == principal.tenant_id, ChatEvent.request_id == request_id,
            )) or 0) + 1
            events = [("done", "", terminal)]
            if status == "failed":
                from eic.ai.runtime_policy import BUDGET_HINTS
                hints = {
                    **BUDGET_HINTS,
                    **chat_search.ERRORS,
                    "history_limit_exceeded": "历史超过扫描上限；请在记忆面板执行分批摘要，完成后用新消息继续。",
                    "summary_budget_exceeded": "摘要与本轮问题超过输入预算，请缩短问题或配置更大的模型输入预算。",
                    "conversation_budget_exceeded": "会话预算校验未通过，请展开预算详情查看具体原因。",
                    "knowledge_configuration_error": "知识库模型配置无效，请检查绑定的向量模型、重排模型及索引版本；系统未自动重试。",
                    "input_budget_exceeded": "系统提示、检索证据或本轮问题超出输入预算，请缩短内容或调整模型预算。",
                    "attachment_context_not_supported": dependencies.attachment_error_message("attachment_context_not_supported"),
                    "attachment_budget_exceeded": dependencies.attachment_error_message("attachment_budget_exceeded"),
                    "attachment_format_unsupported": dependencies.attachment_error_message("attachment_format_unsupported"),
                    "attachment_parse_failed": dependencies.attachment_error_message("attachment_parse_failed"),
                    "temperature_not_supported": "当前模型参数能力不支持温度，请移除温度配置或确认参数协议。",
                    "output_budget_exceeded": "回答达到输出预算，已保留部分内容，请调整预算后用新请求重试。",
                    "provider_parameters_rejected": "上游拒绝了已配置参数，请检查模型参数协议；系统未删除参数自动重发。",
                    "provider_permission_denied": "模型服务拒绝当前调用权限，请检查服务端凭据与项目权限。",
                    "provider_outcome_uncertain": "模型调用结果不确定，系统未自动重发，请检查后用新请求重试。",
                    "approval_required": "当前工具属于写操作，需要审批后才能继续；本次请求未执行该工具。",
                    "tool_not_allowed": "模型请求的工具不在当前技能白名单中，系统已阻止执行。",
                    "invalid_tool_arguments": "模型生成的工具参数未通过校验，系统已阻止执行。",
                    "tool_timeout": "工具执行超时，系统未自动重试，以避免产生未知副作用。",
                    "tool_outcome_uncertain": "工具结果尚未确认，请复核持久记录；系统未自动重放。",
                    "tool_failed": "工具执行失败，系统未自动重试；请检查工具配置和日志。",
                    "duplicate_tool_call_id": "检测到重复工具调用标识且参数不一致，系统已阻止重复执行。",
                    "max_tool_calls_exceeded": "本轮工具调用次数已达到上限，系统已停止继续调用工具。",
                    "tool_budget_exceeded": "本轮 Agent 执行预算已用尽，系统已停止继续调用工具。",
                    "agent_time_limit": "本轮执行已达到时长上限，已保留现有内容；请调整超时配置后手动重试。",
                    "invalid_model_decision": "模型返回的工具调用格式无效，系统未执行不完整的工具请求。",
                    "circuit_open": "当前依赖处于熔断或探测阶段，请稍后重试或配置备用模型。",
                    "concurrency_budget_exceeded": "并发预算已用尽，请稍后重试。",
                    "rate_budget_exceeded": "请求速率预算已用尽，请稍后重试。",
                    "cost_budget_exceeded": "保守成本预留预算已用尽，请调整策略后重试。",
                    "budget_not_configured": "准入预算配置不完整，请联系管理员。",
                    "provider_rate_limited": "模型服务正在限流，请稍后重试。",
                }
                events.insert(0, ("generationerror", hints.get(reason, "生成未完成，已保留现有内容，请手动重试。"), terminal))
            for event, text, payload in events:
                session.add(ChatEvent(
                    id=await dependencies.new_id(session), tenant_id=principal.tenant_id,
                    chat_id=int(request_id) if request_id.isdigit() else 0, request_id=request_id,
                    event_type=event, sequence=sequence,
                    payload=json.dumps(sse_payload(text, payload), ensure_ascii=False),
                ))
                chunks.append(sse_message(request_id, sequence, event, text, payload))
                sequence += 1
            await session.commit()
        return chunks


    def estimate_tokens(text: str) -> int:
        """与预算一致的保守估算，不宣称精确 tokenizer 或账单用量。"""
        return text_units(text)


    async def is_chat_cancelled(request_id: str, tenant_id: int, *, session=None) -> bool:
        if session is None:
            async with runtime.sessions() as current:
                return await is_chat_cancelled(request_id, tenant_id, session=current)
        candidates = list(await session.scalars(select(ChatMessage).where(
            ChatMessage.tenant_id == tenant_id, ChatMessage.request_id == request_id,
            ChatMessage.role == "user",
        ).limit(2)))
        if len(candidates) != 1:
            return True
        stored = candidates[0]
        if stored.status == "cancelled":
            return True
        message = await session.get(Resource, stored.id)
        return bool(
            message and message.tenant_id == tenant_id and message.resource_type == "chat_messages"
            and (message.is_deleted or (message.data or {}).get("cancelled"))
        )


    async def next_chat_event_sequence(request_id: str, tenant_id: int) -> int:
        async with runtime.sessions() as session:
            last = await session.scalar(select(func.max(ChatEvent.sequence)).where(
                ChatEvent.tenant_id == tenant_id, ChatEvent.request_id == request_id,
            ))
        return (last or 0) + 1

    return _RouteExports(
        sse_payload=sse_payload,
        compact_citations=compact_citations,
        sse_message=sse_message,
        persist_chat_event=persist_chat_event,
        audited_sse_message=audited_sse_message,
        mark_chat_cancelled=mark_chat_cancelled,
        finalize_chat_response=finalize_chat_response,
        estimate_tokens=estimate_tokens,
        is_chat_cancelled=is_chat_cancelled,
        next_chat_event_sequence=next_chat_event_sequence,
    )

"""原 producer 专用的子工具未知结果收敛，不授予继续执行资格。"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, update

from eic.core.generation import database_now
from eic.core.ids import new_id
from eic.models import (
    AgentRun, AgentRunLease, AgentToolCall, ChatMessage, ChatSession,
    GenerationLease, ToolAudit,
)


async def converge_child_unknown(
    sessions: Any, *, generation_owner: tuple[int, str] | None,
    worker_owner: tuple[str, str] | None, tenant_id: int, user_id: int,
    request_id: str, run_id: str, call_id: str, tool_name: str,
    input_digest: str, tool_type: str,
) -> bool:
    """仅消费可信入口捕获的原 owner；True 表示本事务首次收敛并提交。

    False 表示身份不匹配、所有权已更换或调用已终结；数据库错误向上传播。
    调用方负责 complete_cleanup 等待及保留原取消，不能用 GraphState 填充身份。
    """
    if (not isinstance(generation_owner, tuple) or len(generation_owner) != 2
            or type(generation_owner[0]) is not int or generation_owner[0] <= 0
            or not isinstance(generation_owner[1], str) or not 0 < len(generation_owner[1]) <= 64):
        return False
    if worker_owner is not None and (
        not isinstance(worker_owner, tuple) or len(worker_owner) != 2
        or worker_owner[0] != run_id or not isinstance(worker_owner[1], str)
        or not 0 < len(worker_owner[1]) <= 64
    ):
        return False
    if (type(tenant_id) is not int or tenant_id <= 0 or type(user_id) is not int or user_id <= 0
            or not isinstance(request_id, str) or not 0 < len(request_id) <= 80
            or not isinstance(run_id, str) or not 0 < len(run_id) <= 64
            or not isinstance(call_id, str) or not 0 < len(call_id) <= 120
            or not isinstance(tool_name, str) or not 0 < len(tool_name) <= 200
            or not isinstance(input_digest, str) or re.fullmatch(r"[a-f0-9]{64}", input_digest) is None
            or tool_type not in {"local", "mcp", "skill"}):
        return False
    message_id, generation_token = generation_owner
    chat_id = int(request_id) if request_id.isascii() and request_id.isdigit() else None
    if chat_id is not None and not 0 < chat_id < 2**63:
        chat_id = None
    async with sessions() as session:
        async with session.begin():
            # 共享 allocator 先取得写锁；所有失败分支未提交 allocator 变动。
            audit_id = await new_id(session)
            lease = await session.scalar(select(GenerationLease).where(
                GenerationLease.message_id == message_id,
                GenerationLease.tenant_id == tenant_id,
                GenerationLease.request_id == request_id,
                GenerationLease.owner_token == generation_token,
                GenerationLease.state.in_(("active", "cancelled", "cancelled_closed")),
            ).with_for_update())
            if lease is None:
                await session.rollback()
                return False
            worker = await session.scalar(select(AgentRunLease).where(
                AgentRunLease.run_id == run_id).with_for_update())
            if worker_owner is not None:
                if worker is None or worker.worker_token != worker_owner[1]:
                    await session.rollback()
                    return False
            elif worker is not None and (worker.worker_token or worker.state == "active"):
                # 无 worker 的旧入口不能为任何已领取过的 run 冒充原 owner。
                await session.rollback()
                return False
            run = await session.scalar(select(AgentRun).where(
                AgentRun.run_id == run_id, AgentRun.tenant_id == tenant_id,
                AgentRun.request_id == request_id,
            ).with_for_update())
            if run is None or run.session_id is None:
                await session.rollback()
                return False
            binding = await session.scalar(select(ChatMessage.id).join(
                ChatSession, ChatSession.id == ChatMessage.session_id,
            ).where(
                ChatMessage.id == message_id, ChatMessage.tenant_id == tenant_id,
                ChatMessage.request_id == request_id, ChatMessage.role == "user",
                ChatMessage.session_id == run.session_id,
                ChatSession.tenant_id == tenant_id, ChatSession.owner_id == user_id,
            ))
            if binding is None:
                await session.rollback()
                return False
            call = await session.scalar(select(AgentToolCall).where(
                AgentToolCall.run_id == run_id, AgentToolCall.call_id == call_id,
                AgentToolCall.tool_name == tool_name, AgentToolCall.input_digest == input_digest,
            ).with_for_update())
            if call is None:
                await session.rollback()
                return False
            changed = await session.execute(update(AgentToolCall).where(
                AgentToolCall.id == call.id, AgentToolCall.run_id == run_id,
                AgentToolCall.call_id == call_id, AgentToolCall.tool_name == tool_name,
                AgentToolCall.input_digest == input_digest,
                AgentToolCall.status.in_(("running", "unknown")),
                AgentToolCall.effect_status.in_(("none", "unknown")),
                AgentToolCall.finished_at.is_(None),
            ).values(status="failed", effect_status="unknown",
                     error_code="tool_outcome_uncertain", output_digest=None,
                     finished_at=await database_now(session))
              .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                await session.rollback()
                return False
            session.add(ToolAudit(
                id=audit_id, tenant_id=tenant_id, user_id=user_id,
                chat_id=chat_id,
                tool_name=tool_name, tool_type=tool_type, outcome="unknown",
                input_digest=input_digest, output_digest=None, duration_ms=0,
                error_message="tool_outcome_uncertain",
            ))
        return True

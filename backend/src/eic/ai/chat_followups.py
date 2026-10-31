"""回答持久终态后的附加任务；单项故障不改变回答或阻断其他入队。"""
from dataclasses import dataclass
import logging
from typing import Any

from eic.ai import conversation_context
from eic.ai.context_policy import context_settings, memory_settings
from eic.core.authentication import Principal
from eic.core.runtime_bindings import RuntimeBindings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompletedTurn:
    principal: Principal
    session_id: int
    message_id: int
    request_id: str
    config: dict[str, Any]
    message_data: dict[str, Any]
    model_used: bool
    degraded: bool


async def stage_label(runtime: RuntimeBindings, turn: CompletedTurn):
    if not turn.model_used or turn.degraded:
        return
    try:
        from eic.ai.conversation_labels import stage
        async with runtime.sessions() as session:
            await stage(session, turn.principal.tenant_id, turn.principal.user_id,
                        turn.session_id, mode="auto")
            await session.commit()
    except Exception:
        log.warning("会话标题入队失败 errorCode=conversation_label_enqueue_failed")


async def stage_context_jobs(runtime: RuntimeBindings, turn: CompletedTurn, *,
                             estimated_input: int, input_limit: int, memory_save):
    settings = context_settings(turn.config)
    if (turn.model_used and settings["automatic"] and input_limit > 0
            and estimated_input * 100 >= input_limit * settings["triggerPercent"]):
        try:
            async with runtime.sessions() as session:
                await conversation_context.stage_compaction(session, tenant_id=turn.principal.tenant_id,
                    owner_id=turn.principal.user_id, session_id=turn.session_id, config=turn.config)
                await session.commit()
        except Exception:
            log.warning("摘要入队失败 errorCode=context_enqueue_failed")
    memory = memory_settings(turn.config)
    if turn.model_used and not turn.degraded and memory["mode"] == "review":
        try:
            from eic.ai.memory_candidates import stage_extraction
            async with runtime.sessions() as session:
                await stage_extraction(session, tenant_id=turn.principal.tenant_id,
                    owner_id=turn.principal.user_id, session_id=turn.session_id,
                    request_id=turn.request_id, config=turn.config)
                await session.commit()
        except Exception:
            log.warning("记忆候选入队失败 errorCode=memory_extraction_enqueue_failed")
    write = turn.message_data.get("memoryWrite")
    if turn.model_used and memory["mode"] != "off" and isinstance(write, dict):
        try:
            async with runtime.sessions() as session:
                await memory_save({**write, "asyncWrite": True, "sourceMessageId": turn.message_id,
                    "sessionId": turn.session_id, "agentId": turn.config.get("agentId"),
                    "idempotencyKey": f"chat:{turn.message_id}"}, turn.principal, session)
        except Exception:
            log.warning("异步记忆写入排队失败 errorCode=memory_write_unavailable")

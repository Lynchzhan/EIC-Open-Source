"""chat_producer 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.core.runtime_bindings import RuntimeBindings
from eic.ai.agent_graph import AGENT_GRAPH_VERSION
from eic.models import AgentRun
from collections.abc import AsyncIterator
from eic.ai.budget import ChatConfigurationError
from eic.models import ChatEvent
from eic.models import ChatMessage
from eic.models import ChatSession
from eic.ai.checkpoint_security import CheckpointReadScope
from eic.ai.shared_executor import ExecutorCheckpointer
from eic.models import GenerationLease
from fastapi import HTTPException
from cryptography.fernet import InvalidToken
from eic.core.authentication import Principal
from eic.core.runtime_refs import RuntimeReferenceUnavailable
from datetime import UTC
from eic.models import User
from eic.core import agent_approval
from eic.core import agent_runtime
from eic.core import agent_worker
import anyio
import asyncio
from eic.ai.stream_reader import complete_cleanup
from sqlalchemy import func
from eic.core import generation
import json
import logging
from eic.core import observability
from sqlalchemy import select
import time
from datetime import timedelta
from eic.core.langfuse_context import turn_context
import uuid


@dataclass(frozen=True)
class Dependencies:
    launch_producer: Callable[..., Any]
    _sse_events_inner: Callable[..., Any]
    finalize_chat_response: Callable[..., Any]
    mark_chat_cancelled: Callable[..., Any]
    new_id: Callable[..., Any]
    require_owned_chat_request: Callable[..., Any]
    require_runtime_chat_permission: Callable[..., Any]
    sse_message: Callable[..., Any]


def bind_routes(*, dependencies: Dependencies, runtime: RuntimeBindings):
    async def replay_chat_events(request_id: str, principal: Principal, after_sequence: int = 0) -> AsyncIterator[str]:
        """订阅持久事件；订阅者断开不取消生产者，也不重新调用 provider。"""
        cursor = after_sequence
        terminal_polls = 0
        cancel_wait_started = None
        next_recovery = 0.0
        poll_delay = 0.1
        next_heartbeat = time.monotonic() + 10
        while True:
            if time.monotonic() >= next_recovery:
                await generation.recover_expired(tenant_id=principal.tenant_id, request_id=request_id)
                next_recovery = time.monotonic() + 3
            async with runtime.sessions() as session:
                try:
                    _, stored = await dependencies.require_owned_chat_request(session, principal, request_id)
                    await dependencies.require_runtime_chat_permission(session, principal)
                except (HTTPException, RuntimeReferenceUnavailable):
                    # 订阅授权失败不是产品终态，也不能占用持久事件游标。
                    yield 'event: subscriptionerror\ndata: ' + json.dumps({
                        "status": "uncertain", "reason": "subscription_authorization_unavailable",
                        "retryable": False,
                    }) + '\n\n'
                    return
                events = list(await session.scalars(
                    select(ChatEvent).where(
                        ChatEvent.tenant_id == principal.tenant_id,
                        ChatEvent.request_id == request_id, ChatEvent.sequence > cursor,
                    ).order_by(ChatEvent.sequence)
                ))
                terminal = stored.status in {"completed", "failed", "cancelled", "waiting_approval", "approval_executing"}
                # SQLite 的两个 SELECT 可跨越取消事务提交；done 可能比已载入的
                # message.status 更新。按取消事件重新核验关闭租约，避免先发 done。
                cancel_event = any(event.event_type == "done"
                    and json.loads(event.payload).get("status") == "cancelled" for event in events)
                if stored.status == "cancelled" or cancel_event:
                    lease = await session.get(GenerationLease, stored.id)
                    waiting_cancel_close = lease is not None and lease.state == "cancelled"
                else:
                    waiting_cancel_close = False
                cached_answer = None
                cached_sequence = 0
                if terminal:
                    has_answer = await session.scalar(select(ChatEvent.id).where(
                        ChatEvent.tenant_id == principal.tenant_id,
                        ChatEvent.request_id == request_id, ChatEvent.event_type == "aimsg",
                    ).limit(1))
                    if has_answer is None:
                        # 老版本没有持久化 token 增量；读取最终答复，不重跑模型或改历史。
                        cached_answer = await session.scalar(select(ChatMessage.content).where(
                            ChatMessage.tenant_id == principal.tenant_id,
                            ChatMessage.request_id == request_id, ChatMessage.role == "assistant",
                        ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
                        last = await session.scalar(select(func.max(ChatEvent.sequence)).where(
                            ChatEvent.tenant_id == principal.tenant_id, ChatEvent.request_id == request_id,
                        ))
                        cached_sequence = (last or 0) + 1
            if waiting_cancel_close:
                cancel_wait_started = cancel_wait_started or asyncio.get_running_loop().time()
                if asyncio.get_running_loop().time() - cancel_wait_started < 4:
                    # 取消关闭最多等待4秒，保留既有注释帧协议；常规长请求使用命名心跳。
                    yield ": keepalive\n\n"
                    await asyncio.sleep(.1)
                    continue
            for event in events:
                cursor = event.sequence
                if event.event_type == "done" and cached_answer is not None:
                    continue
                payload = json.loads(event.payload)
                yield dependencies.sse_message(request_id, cursor, event.event_type, payload.get("text", ""), payload)
                if event.event_type == "done":
                    return
            if cached_answer is not None:
                if cached_sequence > cursor:
                    yield dependencies.sse_message(request_id, cached_sequence, "aimsg", cached_answer)
                yield dependencies.sse_message(request_id, max(cursor, cached_sequence) + 1, "done", "",
                                  {**(stored.token_usage or {}), "status": stored.status})
                return
            # status 与最后事件提交之间有短窗口；进程中断/提前取消时只补本连接的 done。
            terminal_polls = terminal_polls + 1 if terminal else 0
            if terminal_polls >= 5:
                yield dependencies.sse_message(request_id, cursor + 1, "done", "",
                                  {**(stored.token_usage or {}), "status": stored.status})
                return
            if time.monotonic() >= next_heartbeat:
                yield 'event: heartbeat\ndata: {}\n\n'
                next_heartbeat = time.monotonic() + 10
            poll_delay = 0.1 if events or terminal else min(0.5, poll_delay * 1.5)
            await asyncio.sleep(poll_delay)


    @observability.observe_sse
    async def sse_events(request_id: str, principal: Principal, after_sequence: int = 0) -> AsyncIterator[str]:
        async with runtime.sessions() as session:
            _, stored = await dependencies.require_owned_chat_request(session, principal, request_id)
            run = await session.scalar(select(AgentRun).where(
                AgentRun.tenant_id == principal.tenant_id, AgentRun.request_id == request_id))
            if run is not None and run.status not in {"waiting_approval", "approval_approved"}:
                after_sequence = max(after_sequence, int((run.run_metadata or {}).get("streamAfterSequence", 0)))
            message_id = stored.id
            observability.CONTEXT.get()["chat_id"] = str(stored.session_id)
            if run is not None:
                observability.CONTEXT.get().update(run_id=run.run_id, trace_id=run.trace_id)
            token = await generation.claim(session, stored)
            await session.commit()
        if token is None:
            async for chunk in replay_chat_events(request_id, principal, after_sequence):
                yield chunk
            return
        producer = asyncio.create_task(dependencies.launch_producer(request_id, principal, message_id, token),
                                      name=f"eic-generation-{message_id}")
        generation.track_producer(producer)
        async for chunk in replay_chat_events(request_id, principal, after_sequence):
            yield chunk


    async def recover_agent_request(message_id: int, *, sessions=None) -> bool:
        """接管过期请求的安全图快照；未知 provider 结果和旧版本保持不重放。"""
        async with (sessions or runtime.sessions)() as session:
            await dependencies.new_id(session)
            now = await generation.database_now(session)
            lease = await session.get(GenerationLease, message_id)
            stored = await session.get(ChatMessage, message_id)
            if (lease is None or stored is None or lease.state != "active"
                    or stored.status != "generating"
                    or lease.expires_at.replace(tzinfo=UTC) > now):
                return False
            run = await session.scalar(select(AgentRun).where(
                AgentRun.tenant_id == stored.tenant_id, AgentRun.request_id == stored.request_id,
            ).limit(1))
            if run is None or run.graph_version != AGENT_GRAPH_VERSION or run.status != "running":
                return False
            decision = await agent_runtime.runtime_store.decide_recovery(
                session, run_id=run.run_id, expected_graph_version=AGENT_GRAPH_VERSION,
            )
            if decision.action != "safe_replay":
                await agent_runtime.runtime_store.finish_run(
                    session, run_id=run.run_id, status="manual_review",
                    error_code=decision.reason, result={"automaticRetry": False},
                )
                await session.commit()
                return False
            chat = await session.get(ChatSession, stored.session_id)
            user = await session.get(User, chat.owner_id) if chat else None
            if (user is None or not user.is_active or user.tenant_id != stored.tenant_id
                    or user.approval_status != "approved"):
                return False
            principal = Principal(user.id, user.tenant_id, user.username, user.is_super_admin)
            try:
                await dependencies.require_owned_chat_request(session, principal, stored.request_id)
                await dependencies.require_runtime_chat_permission(session, principal)
            except (HTTPException, RuntimeReferenceUnavailable):
                return False
            worker_token = await agent_runtime.runtime_store.claim_worker(session, run_id=run.run_id)
            if worker_token is None:
                # Agent 租约可能晚于 generation 租约到期，等待下一轮而非提前宣告失败。
                return True
            try:
                if decision.state and decision.state.get("continuationEnvelope"):
                    agent_approval.unseal(decision.state["continuationEnvelope"])
                else:
                    async def authorize_recovery_read(read_session, persisted_run, scope):
                        if (persisted_run.run_id != run.run_id or persisted_run.session_id != stored.session_id
                                or persisted_run.request_id != stored.request_id
                                or persisted_run.graph_version != AGENT_GRAPH_VERSION
                                or persisted_run.tenant_id != principal.tenant_id or scope.owner_id != principal.user_id):
                            return False
                        await dependencies.require_owned_chat_request(read_session, principal, stored.request_id)
                        await dependencies.require_runtime_chat_permission(read_session, principal)
                        return True
                    # 恢复必须读取 producer 实际使用的共享执行器快照；旧 CHAT_AGENT_GRAPH
                    # 入口不承载聊天 producer 的持久状态，不能作为恢复判据。
                    saver = ExecutorCheckpointer(sessions or runtime.sessions)
                    scope = CheckpointReadScope(run_id=run.run_id, tenant_id=principal.tenant_id,
                        owner_id=principal.user_id, session_id=stored.session_id,
                        request_id=stored.request_id, graph_version=AGENT_GRAPH_VERSION)
                    with saver.authorized_read_scope(scope, authorizer=authorize_recovery_read):
                        checkpoint = await saver.aget_tuple({"configurable": {"thread_id": run.run_id}})
                    if checkpoint is None:
                        raise ValueError("graph_checkpoint_missing")
            except (InvalidToken, ValueError, TypeError, KeyError, PermissionError, HTTPException, RuntimeReferenceUnavailable):
                await agent_runtime.runtime_store.finish_run(
                    session, run_id=run.run_id, status="manual_review",
                    error_code="graph_checkpoint_invalid", result={"automaticRetry": False},
                )
                await session.commit()
                return False
            from eic.ai.runtime_policy import close_abandoned_turn
            await close_abandoned_turn(session, stored, ended_at=lease.expires_at)
            token = uuid.uuid4().hex
            lease.owner_token = token
            lease.expires_at = now + timedelta(seconds=generation.LEASE_SECONDS)
            await session.commit()
            identity = (stored.request_id, principal, stored.id, token, run.run_id, worker_token)
        producer = asyncio.create_task(dependencies.launch_producer(*identity), name=f"eic-agent-resume-{message_id}")
        generation.track_producer(producer)
        return True


    async def _run_chat_producer(
        request_id: str, principal: Principal, message_id: int, token: str,
        recovered_run_id: str | None = None, recovered_worker_token: str | None = None,
    ) -> None:
        """ASGI 连接只订阅；租约归独立 producer，断线不改变持久请求状态。"""
        from eic.ai.runtime_policy import ACTIVE
        budget_context_token = ACTIVE.set(None)
        context_token = generation.OWNER.set((message_id, token))
        observation_token = observability.CONTEXT.set(turn_context(
            tenant_id=principal.tenant_id, request_id=request_id,
            owner_key=observability.actor_key(principal.tenant_id, principal.user_id)))
        worker_context = None
        try:
            async with runtime.sessions() as session:
                await generation.fence(session)
                stored = await session.get(ChatMessage, message_id)
                if (stored is None or stored.tenant_id != principal.tenant_id
                        or stored.request_id != request_id):
                    raise generation.LeaseLost("Producer request identity changed")
                run = await agent_runtime.runtime_store.create_run(
                    session, tenant_id=principal.tenant_id, session_id=stored.session_id,
                    request_id=request_id, graph_version=AGENT_GRAPH_VERSION,
                    trace_id=observability.turn_trace(principal.tenant_id, request_id),
                    metadata={"entrypoint": "chat"},
                )
                if recovered_run_id is not None and run.run_id != recovered_run_id:
                    raise generation.LeaseLost("Recovered run identity changed")
                worker_token = recovered_worker_token or await agent_worker.claim(session, run.run_id)
                observability.CONTEXT.set(turn_context(
                    tenant_id=principal.tenant_id, request_id=stored.request_id,
                    trace_id=run.trace_id, run_id=run.run_id, chat_id=str(stored.session_id),
                    owner_key=observability.actor_key(principal.tenant_id, principal.user_id)))
                await session.commit()
            worker_context = agent_worker.OWNER.set((run.run_id, worker_token))
        except BaseException:
            ACTIVE.reset(budget_context_token)
            observability.CONTEXT.reset(observation_token)
            generation.OWNER.reset(context_token)
            raise
        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(generation.heartbeat(asyncio.current_task(), stop_heartbeat),
                                             name="eic-generation-heartbeat")
        inner = _leased_sse_events(request_id, principal)
        try:
            async for _ in inner:
                pass
        except generation.LeaseLost:
            pass
        finally:
            stop_heartbeat.set()
            async def close():
                # 不取消正在访问 DB 的心跳，等待事务正常释放后再确认 provider 关闭。
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                await inner.aclose()
                try:
                    if ACTIVE.get() is not None:
                        await ACTIVE.get().finish_turn()
                finally:
                    try:
                        await generation.finish()
                    finally:
                        async with runtime.sessions() as session:
                            await agent_worker.release(session, run.run_id, worker_token)
                            await session.commit()
            try:
                await complete_cleanup(close())
            finally:
                if worker_context is not None:
                    agent_worker.OWNER.reset(worker_context)
                ACTIVE.reset(budget_context_token)
                generation.OWNER.reset(context_token)
                observability.CONTEXT.reset(observation_token)


    async def _leased_sse_events(request_id: str, principal: Principal) -> AsyncIterator[str]:
        from eic.ai.runtime_policy import BudgetStopped
        inner = dependencies._sse_events_inner(request_id, principal)
        try:
            async for chunk in inner:
                yield chunk
        except asyncio.CancelledError:
            from eic.ai.runtime_policy import ACTIVE
            budget_expired = ACTIVE.get() is not None and ACTIVE.get().expired
            with anyio.CancelScope(shield=True):
                try:
                    async with runtime.sessions() as session:
                        parts = list(await session.scalars(select(ChatEvent.payload).where(
                            ChatEvent.tenant_id == principal.tenant_id, ChatEvent.request_id == request_id,
                            ChatEvent.event_type == "aimsg",
                        ).order_by(ChatEvent.sequence)))
                    await dependencies.finalize_chat_response(request_id, principal, status="failed",
                        answer="".join(json.loads(part).get("text", "") for part in parts),
                        token_usage={"providerOutcome": "uncertain"},
                        reason="conversation_time_limit" if budget_expired else "provider_outcome_uncertain")
                except generation.LeaseLost:
                    pass
            if not budget_expired:
                raise
        except GeneratorExit:
            with anyio.CancelScope(shield=True):
                try:
                    await dependencies.mark_chat_cancelled(request_id, principal)
                except generation.LeaseLost:
                    pass
            raise
        except generation.LeaseLost:
            raise
        except RuntimeReferenceUnavailable:
            for chunk in await dependencies.finalize_chat_response(
                request_id, principal, status="failed", answer="", token_usage={},
                reason="runtime_reference_unavailable",
            ):
                yield chunk
        except ChatConfigurationError as exc:
            for chunk in await dependencies.finalize_chat_response(
                request_id, principal, status="failed", answer="", token_usage={}, reason=exc.reason,
            ):
                yield chunk
        except Exception as exc:
            # 只记录类型和代码位置，避免异常文本泄露正文、参数或凭据。
            import traceback
            logging.getLogger(__name__).error(
                "Chat generation failed request_id=%s exception_type=%s frames=%s",
                request_id, type(exc).__name__,
                [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(exc.__traceback__)],
            )
            async with runtime.sessions() as session:
                parts = list(await session.scalars(select(ChatEvent.payload).where(
                    ChatEvent.tenant_id == principal.tenant_id, ChatEvent.request_id == request_id,
                    ChatEvent.event_type == "aimsg",
                ).order_by(ChatEvent.sequence)))
            for chunk in await dependencies.finalize_chat_response(
                request_id, principal, status="failed",
                answer="".join(json.loads(part).get("text", "") for part in parts),
                token_usage={}, reason=exc.reason if isinstance(exc, BudgetStopped) else "generation_error",
            ):
                yield chunk
        finally:
            await complete_cleanup(inner.aclose())

    return _RouteExports(
        replay_chat_events=replay_chat_events,
        sse_events=sse_events,
        recover_agent_request=recover_agent_request,
        _run_chat_producer=_run_chat_producer,
        _leased_sse_events=_leased_sse_events,
    )

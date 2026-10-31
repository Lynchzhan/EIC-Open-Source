"""持久租约隔离旧 producer；仅模型请求前的安全节点允许恢复。"""
import asyncio
import json
import logging
import uuid
from contextvars import ContextVar
from datetime import UTC, timedelta

from sqlalchemy import func, select, update

from eic.core.database import SessionLocal
from eic.core.ids import new_id
from eic.models import ChatEvent, ChatMessage, GenerationLease

LEASE_SECONDS = 15
HEARTBEAT_SECONDS = 3
OWNER: ContextVar[tuple[int, str] | None] = ContextVar("eic_generation_owner", default=None)
log = logging.getLogger(__name__)
PRODUCERS: set[asyncio.Task] = set()
_recover_request = None


def configure_recovery(handler):
    """由装配入口注册安全图恢复服务；核心租约不导入 Web 应用。"""
    global _recover_request
    _recover_request = handler


def track_producer(task):
    PRODUCERS.add(task)
    def finished(done):
        PRODUCERS.discard(done)
        if not done.cancelled() and done.exception() is not None:
            # 不记录异常正文，异常可能带有 provider 的敏感内容。
            log.error("Generation producer failed; lease recovery will close the request")
    task.add_done_callback(finished)


async def stop_producers():
    tasks = list(PRODUCERS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class LeaseLost(RuntimeError):
    pass


async def database_now(session):
    value = await session.scalar(select(func.current_timestamp()))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def claim(session, message):
    token = uuid.uuid4().hex
    # 与所有写事件使用相同锁顺序，避免 lease/allocator 反向加锁。
    await new_id(session)
    result = await session.execute(update(ChatMessage).where(
        ChatMessage.id == message.id, ChatMessage.tenant_id == message.tenant_id,
        ChatMessage.role == "user", ChatMessage.status == "streaming",
    ).values(status="generating"))
    if result.rowcount != 1:
        return None
    existing = await session.get(GenerationLease, message.id)
    if existing is None:
        session.add(GenerationLease(
            message_id=message.id, tenant_id=message.tenant_id, request_id=message.request_id,
            owner_token=token, expires_at=await database_now(session) + timedelta(seconds=LEASE_SECONDS),
            state="active",
        ))
    elif existing.state == "finished":
        existing.owner_token = token
        existing.expires_at = await database_now(session) + timedelta(seconds=LEASE_SECONDS)
        existing.state = "active"
    else:
        raise LeaseLost("Previous generation lease not closed")
    return token


async def fence(session):
    """Renew with an atomic token/expiry check inside the business transaction."""
    owner = OWNER.get()
    if owner is None:
        return
    await new_id(session)
    now = await database_now(session)
    result = await session.execute(update(GenerationLease).where(
        GenerationLease.message_id == owner[0],
        GenerationLease.owner_token == owner[1], GenerationLease.state == "active",
        GenerationLease.expires_at > now,
    ).values(expires_at=now + timedelta(seconds=LEASE_SECONDS))
      .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise LeaseLost("Generation lease expired or revoked")
    from eic.core import agent_worker
    try:
        await agent_worker.fence(session)
    except RuntimeError as exc:
        raise LeaseLost("Agent worker lease expired or revoked") from exc


async def heartbeat(producer_task, stop=None):
    loop = asyncio.get_running_loop()
    next_renewal = loop.time() + HEARTBEAT_SECONDS
    stop = stop or asyncio.Event()
    try:
        while not stop.is_set():
            # 撤销快速只读轮询；续租仍按原周期，不把每次轮询变成写锁。
            try:
                await asyncio.wait_for(stop.wait(), min(.1, HEARTBEAT_SECONDS))
                return
            except TimeoutError:
                pass
            async with SessionLocal() as session:
                owner = OWNER.get()
                lease = await session.get(GenerationLease, owner[0]) if owner else None
                revoked = lease is None or lease.owner_token != owner[1] or lease.state != "active"
            if revoked:
                if not stop.is_set():
                    producer_task.cancel()
                return
            if not stop.is_set() and loop.time() >= next_renewal:
                # 先关闭只读事务，再按 allocator -> lease 顺序开启写事务。
                # SQLite 不支持两个读事务同时升级为 writer。
                async with SessionLocal() as session:
                    await fence(session)
                    await session.commit()
                next_renewal = loop.time() + HEARTBEAT_SECONDS
    except asyncio.CancelledError:
        raise
    except Exception:
        # 续租失败即停止本 producer；不让它继续写入或重新调用模型。
        if not stop.is_set():
            producer_task.cancel()
        log.exception("Generation heartbeat failed; producer cancelled")


async def finish():
    owner = OWNER.get()
    if owner is None:
        return
    async with SessionLocal() as session:
        await new_id(session)
        await session.execute(update(GenerationLease).where(
            GenerationLease.message_id == owner[0],
            GenerationLease.owner_token == owner[1], GenerationLease.state == "active",
            GenerationLease.message_id.in_(select(ChatMessage.id).where(
                ChatMessage.status.in_(["completed", "failed", "cancelled", "waiting_approval"]),
            )),
        ).values(state="finished"))
        # 该确认只在 provider/reader 清理后写入，供跨进程订阅者等待取消关闭。
        await session.execute(update(GenerationLease).where(
            GenerationLease.message_id == owner[0],
            GenerationLease.owner_token == owner[1], GenerationLease.state == "cancelled",
        ).values(state="cancelled_closed"))
        await session.commit()


async def cancel_request(session, message):
    """撤销 producer 写入资格，取消终态和 done 使用同一事务提交。"""
    event_id = await new_id(session)
    result = await session.execute(update(ChatMessage).where(
        ChatMessage.id == message.id, ChatMessage.tenant_id == message.tenant_id,
        ChatMessage.status.in_(["streaming", "generating", "waiting_approval"]),
    ).values(status="cancelled"))
    if result.rowcount != 1:
        return False
    await session.execute(update(GenerationLease).where(
        GenerationLease.message_id == message.id, GenerationLease.state == "active",
    ).values(state="cancelled"))
    sequence = (await session.scalar(select(func.max(ChatEvent.sequence)).where(
        ChatEvent.tenant_id == message.tenant_id, ChatEvent.request_id == message.request_id,
    )) or 0) + 1
    session.add(ChatEvent(
        id=event_id, tenant_id=message.tenant_id, request_id=message.request_id,
        chat_id=int(message.request_id) if message.request_id.isdigit() else 0,
        sequence=sequence, event_type="done",
        payload=json.dumps({"text": "", "status": "cancelled", "retryable": False}),
    ))
    return True


async def recover_expired(*, tenant_id=None, request_id=None):
    """One atomic terminal event + failed state per expired request, no retry."""
    from eic.core.agent_approval import recover_uncertain
    recovered = await recover_uncertain(SessionLocal, tenant_id=tenant_id, request_id=request_id)
    async with SessionLocal() as session:
        # 仅对过期候选开写事务；空闲巡检不占用全局分配锁。
        now = await database_now(session)
        query = select(GenerationLease.message_id).where(
            GenerationLease.state == "active", GenerationLease.expires_at <= now,
        )
        if tenant_id is not None:
            query = query.where(GenerationLease.tenant_id == tenant_id)
        if request_id is not None:
            query = query.where(GenerationLease.request_id == request_id)
        candidates = list(await session.scalars(query.limit(100)))
    for message_id in candidates:
        # 安全图节点可被生产 Worker 接管；模型已开始的请求仍走原终态收口。
        if _recover_request is not None and await _recover_request(message_id, sessions=SessionLocal):
            continue
        async with SessionLocal() as session:
            event_id = await new_id(session)
            now = await database_now(session)
            changed = await session.execute(update(GenerationLease).where(
                GenerationLease.message_id == message_id, GenerationLease.state == "active",
                GenerationLease.expires_at <= now,
            ).values(state="expired"))
            if changed.rowcount != 1:
                await session.rollback()
                continue
            lease = await session.get(GenerationLease, message_id)
            message = await session.get(ChatMessage, message_id)
            if message is not None:
                from eic.ai.runtime_policy import close_abandoned_turn
                await close_abandoned_turn(session, message, ended_at=lease.expires_at)
                if message.status == "generating":
                    message.status = "failed"
                    message.token_usage = {
                        **(message.token_usage or {}), "recoveryReason": "producer_lease_expired",
                        "providerOutcome": "uncertain", "automaticRetry": False,
                    }
                has_done = await session.scalar(select(ChatEvent.id).where(
                    ChatEvent.tenant_id == lease.tenant_id, ChatEvent.request_id == lease.request_id,
                    ChatEvent.event_type == "done",
                ).limit(1))
                if message.status in {"completed", "failed", "cancelled"}:
                    # 聊天终态与 Agent Run 同事务收口，避免崩溃后审计永久 running。
                    from eic.core.agent_runtime import runtime_store
                    await runtime_store.finish_for_request(
                        session, tenant_id=lease.tenant_id, request_id=lease.request_id,
                        status=message.status, error_code="producer_lease_expired",
                        result={"providerOutcome": "uncertain", "automaticRetry": False},
                    )
                if has_done or message.status not in {"completed", "failed", "cancelled"}:
                    await session.commit()
                    continue
                sequence = (await session.scalar(select(func.max(ChatEvent.sequence)).where(
                    ChatEvent.tenant_id == lease.tenant_id, ChatEvent.request_id == lease.request_id,
                )) or 0) + 1
                session.add(ChatEvent(
                    id=event_id, tenant_id=lease.tenant_id, request_id=lease.request_id,
                    chat_id=int(lease.request_id) if lease.request_id.isdigit() else 0,
                    sequence=sequence, event_type="done",
                    payload=json.dumps({"text": "", "status": message.status,
                                        "reason": "producer_lease_expired", "retryable": False}),
                ))
                recovered += 1
            await session.commit()
    return recovered


async def recovery_loop():
    while True:
        try:
            await recover_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Generation recovery scan failed")
        await asyncio.sleep(HEARTBEAT_SECONDS)

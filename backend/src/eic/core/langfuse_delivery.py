"""持久投递：可恢复未发送项；超时/中断保留 unconfirmed，等待人工核对。"""
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from eic.models import LangfuseDelivery, ObservabilityEvent


def destination(exporter):
    if exporter._client is None:
        return None
    material = "\0".join(("eic-durable-export-v1", exporter._endpoint, exporter._namespace, exporter._environment))
    # 公钥散列用于区分同站点不同项目，不保存任何认证值。
    from eic.core.config import get_settings
    public = get_settings().langfuse_public_key.get_secret_value()
    return hashlib.sha256((material + "\0" + public).encode()).hexdigest()


async def stage(session, row, exporter):
    target = destination(exporter)
    if target is None:
        return
    identity = (row["event_key"], target)
    if await session.get(LangfuseDelivery, identity) is not None:
        return
    try:
        events = exporter._build(row, environment=exporter._environment, namespace=exporter._namespace)
        ids = sorted(event["id"] for event in events)
        export_key = hashlib.sha256(json.dumps(ids or ["not_applicable", identity[0]]).encode()).hexdigest()
        status = "pending" if ids else "not_applicable"
    except Exception:
        export_key = hashlib.sha256(("invalid:" + identity[0]).encode()).hexdigest()
        status = "rejected"
    if await session.scalar(select(LangfuseDelivery.event_key).where(
            LangfuseDelivery.destination == target, LangfuseDelivery.export_key == export_key)) is not None:
        return
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        # sqlite3 legacy transaction control 不会为 SELECT 开事务，首次 SAVEPOINT
        # 会自行成为顶层事务；显式 BEGIN 保证调用者 rollback 能撤销投递记录。
        driver = connection.sync_connection.connection.driver_connection
        if not driver.in_transaction:
            await connection.exec_driver_sql("BEGIN")
    try:
        # 跨进程相同 OTLP span 只保留一个投递身份；冲突不回滚已落盘的原事件。
        async with session.begin_nested():
            session.add(LangfuseDelivery(event_key=identity[0], destination=target, export_key=export_key,
                status=status, attempts=0, next_attempt_at=datetime.now(UTC), updated_at=datetime.now(UTC)))
            await session.flush()
    except IntegrityError:
        if await session.scalar(select(LangfuseDelivery.event_key).where(
                LangfuseDelivery.destination == target, LangfuseDelivery.export_key == export_key)) is None:
            raise


async def dispatch_one(sessions, exporter):
    return bool(await dispatch_batch(sessions, exporter, limit=1, _single_event=True))


async def dispatch_batch(sessions, exporter, *, limit=None, _single_event=False):
    target = destination(exporter)
    if target is None or not exporter._accepting:
        return 0
    capacity = max(1, min(limit or exporter._batch_size, 128))
    now, token = datetime.now(UTC), uuid.uuid4().hex
    claimed, payloads = {}, []
    async with sessions() as session:
        await session.execute(update(LangfuseDelivery).where(
            LangfuseDelivery.destination == target, LangfuseDelivery.status == "sending",
            LangfuseDelivery.lease_until <= now).values(
                status="unconfirmed", owner_token=None, lease_until=None, updated_at=now))
        candidates = list((await session.execute(select(
            LangfuseDelivery.event_key, LangfuseDelivery.attempts).where(
                LangfuseDelivery.destination == target, LangfuseDelivery.status == "pending",
                LangfuseDelivery.next_attempt_at <= now).order_by(
                LangfuseDelivery.next_attempt_at, LangfuseDelivery.event_key).limit(capacity))).all())
        for identity, attempts in candidates:
            changed = await session.execute(update(LangfuseDelivery).where(
                LangfuseDelivery.event_key == identity, LangfuseDelivery.destination == target,
                LangfuseDelivery.status == "pending", LangfuseDelivery.attempts == attempts
            ).values(status="sending", owner_token=token, lease_until=now + timedelta(seconds=30),
                     attempts=attempts + 1, updated_at=now))
            if changed.rowcount == 1:
                claimed[identity] = attempts + 1
        if claimed:
            events = await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.event_key.in_(claimed)))
            payloads = [{column.name: getattr(event, column.name)
                         for column in ObservabilityEvent.__table__.columns if column.name != "collected_at"}
                        for event in events]
        await session.commit()
    if not claimed:
        return 0
    send_batch = getattr(exporter, "deliver_persisted_batch", None)
    if callable(send_batch) and not _single_event:
        outcomes = await send_batch(payloads)
    else:
        # 保留只实现单事件协议的调用者；实际批量出口始终走上面的单次 POST。
        outcomes = {row["event_key"]: await exporter.deliver_persisted(row) for row in payloads}
    async with sessions() as session:
        for identity, attempts in claimed.items():
            outcome = outcomes.get(identity, "missing")
            deferred = outcome == "deferred"
            if deferred:
                outcome = "pending"
            elif outcome == "retryable":
                outcome = "pending" if attempts < 5 else "rejected"
            if outcome not in {"pending", "rejected", "acknowledged", "unconfirmed", "not_applicable", "missing"}:
                outcome = "unconfirmed"
            timestamp = datetime.now(UTC)
            await session.execute(update(LangfuseDelivery).where(
                LangfuseDelivery.event_key == identity, LangfuseDelivery.destination == target,
                LangfuseDelivery.owner_token == token, LangfuseDelivery.status == "sending",
                LangfuseDelivery.lease_until > timestamp).values(
                    status=outcome, owner_token=None, lease_until=None, updated_at=timestamp,
                    attempts=attempts - 1 if deferred else attempts,
                    next_attempt_at=timestamp + timedelta(seconds=0 if deferred else min(300, 2 ** attempts))))
        await session.commit()
    return len(claimed)


async def loop(sessions, exporter, diagnostics):
    next_diagnostics = 0
    while True:
        try:
            started = asyncio.get_running_loop().time()
            worked = await dispatch_batch(sessions, exporter)
            if worked:
                diagnostics["lastBatchSize"] = worked
                diagnostics["lastDispatchMs"] = round((asyncio.get_running_loop().time() - started) * 1000, 2)
            if asyncio.get_running_loop().time() >= next_diagnostics:
                target = destination(exporter)
                async with sessions() as session:
                    counts = await session.execute(select(LangfuseDelivery.status, func.count()).where(
                        LangfuseDelivery.destination == target).group_by(LangfuseDelivery.status))
                    oldest = await session.scalar(select(func.min(LangfuseDelivery.updated_at)).where(
                        LangfuseDelivery.destination == target,
                        LangfuseDelivery.status.in_(["pending", "sending"])))
                    unconfirmed = await session.scalar(select(func.min(LangfuseDelivery.updated_at)).where(
                        LangfuseDelivery.destination == target, LangfuseDelivery.status == "unconfirmed"))
                    timestamp = datetime.now(UTC)
                    def age(value):
                        if value is None:
                            return None
                        return max(0, (timestamp - value.replace(tzinfo=UTC)).total_seconds())
                    diagnostics.update(destination=target, counts=dict(counts.all()),
                        oldestPendingAgeSeconds=age(oldest), oldestUnconfirmedAgeSeconds=age(unconfirmed),
                        updatedAt=timestamp.isoformat())
                next_diagnostics = asyncio.get_running_loop().time() + 2
            await asyncio.sleep(0.05 if worked else 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            diagnostics["workerErrors"] = diagnostics.get("workerErrors", 0) + 1
            await asyncio.sleep(2)

"""数据库共享控制面；有界探测、过期 fencing 和保守准入预留。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
import uuid

from sqlalchemy import func, select

from eic.core.config import get_settings
from eic.core.database import SessionLocal
from eic.core.ids import new_id
from eic.core.outbox import now, utc
from eic.models import AdmissionLease, AdmissionWindow, DependencyCircuit

log = logging.getLogger(__name__)


class DependencyDenied(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Permit:
    key: str
    epoch: int
    token: str | None
    state: str


def dependency_key(domain, tenant_id, provider="", model_id="", endpoint=""):
    return hashlib.sha256(json.dumps(
        [domain, tenant_id, provider, str(model_id), endpoint.rstrip("/")],
        separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


async def acquire(sessions, key, settings, *, probe_lifetime=0) -> Permit:
    async with sessions() as session:
        await new_id(session)
        timestamp = await now(session)
        row = await session.get(DependencyCircuit, key)
        if row is None:
            row = DependencyCircuit(key=key, state="CLOSED", failures=0,
                                    window_started=timestamp, epoch=0)
            session.add(row)
        if row.state == "CLOSED":
            permit = Permit(key, row.epoch, None, "CLOSED")
        elif row.state == "OPEN" and row.open_until and utc(row.open_until) <= timestamp:
            token = uuid.uuid4().hex
            row.state, row.probe_token = "HALF_OPEN", token
            row.probe_until = timestamp + timedelta(seconds=max(
                settings.circuit_probe_seconds,
                getattr(settings, "provider_total_timeout_seconds", 0) + 30,
                probe_lifetime,
            ))
            permit = Permit(key, row.epoch, token, "HALF_OPEN")
        elif row.state == "HALF_OPEN" and row.probe_until and utc(row.probe_until) <= timestamp:
            # 旧探测者过期，先打开一个冷却周期；迟到结果没有提交资格。
            row.state, row.probe_token, row.probe_until = "OPEN", None, None
            row.open_until = timestamp + timedelta(seconds=settings.circuit_open_seconds)
            row.epoch += 1
            await session.commit()
            raise DependencyDenied("circuit_open", "依赖熔断中，请稍后重试或使用已配置兜底")
        else:
            await session.rollback()
            raise DependencyDenied("circuit_open", "依赖熔断中，已有探测者或尚未达到恢复时间")
        await session.commit()
        return permit


async def finish(sessions, permit, success: bool | None, settings):
    async with sessions() as session:
        await new_id(session)
        timestamp = await now(session)
        row = await session.get(DependencyCircuit, permit.key)
        if row is None or row.epoch != permit.epoch:
            return False
        if permit.token:
            if (row.state != "HALF_OPEN" or row.probe_token != permit.token
                    or row.probe_until is None or utc(row.probe_until) <= timestamp):
                return False
            row.probe_token, row.probe_until = None, None
            row.epoch += 1
            row.state = "CLOSED" if success is True else "OPEN"
            row.open_until = None if success is True else timestamp + timedelta(seconds=settings.circuit_open_seconds)
            row.failures = 0 if success is True else row.failures
            row.window_started = timestamp
        elif row.state == "CLOSED":
            if success is True:
                row.failures, row.window_started = 0, timestamp
            elif success is False:
                if timestamp - utc(row.window_started) >= timedelta(seconds=settings.circuit_window_seconds):
                    row.failures, row.window_started = 0, timestamp
                row.failures += 1
                if row.failures >= settings.circuit_failure_threshold:
                    row.state = "OPEN"
                    row.open_until = timestamp + timedelta(seconds=settings.circuit_open_seconds)
                    row.epoch += 1
        else:
            return False
        await session.commit()
        return True


async def admit(sessions, tenant_id, reserve_units, settings, *, lifetime=210, enforce_window_limits=True):
    limits = [
        settings.admission_global_concurrency, settings.admission_tenant_concurrency,
        settings.admission_global_rate, settings.admission_tenant_rate,
        settings.admission_global_units, settings.admission_tenant_units,
    ]
    if (any(type(value) is not int or not 0 < value < 2**63 for value in limits)
            or type(reserve_units) is not int or not 0 < reserve_units < 2**63
            or type(tenant_id) is not int or not 0 < tenant_id < 2**63):
        raise DependencyDenied("budget_not_configured", "准入预算配置不完整或预留单位无效")
    async with sessions() as session:
        await new_id(session)
        timestamp = await now(session)
        active = select(func.count()).select_from(AdmissionLease).where(
            AdmissionLease.finished.is_(False), AdmissionLease.expires_at > timestamp,
        )
        global_active = await session.scalar(active)
        tenant_active = await session.scalar(active.where(AdmissionLease.tenant_id == tenant_id))
        if global_active >= limits[0] or tenant_active >= limits[1]:
            raise DependencyDenied("concurrency_budget_exceeded", "并发预算已用尽，请稍后重试")
        windows = [("global", limits[2], limits[4]), (f"tenant:{tenant_id}", limits[3], limits[5])]
        for key, rate, units in (windows if enforce_window_limits else []):
            row = await session.get(AdmissionWindow, key)
            if row is None:
                row = AdmissionWindow(key=key, started_at=timestamp, requests=0, reserved_units=0)
                session.add(row)
            elif timestamp - utc(row.started_at) >= timedelta(seconds=60):
                row.started_at, row.requests, row.reserved_units = timestamp, 0, 0
            if row.requests >= rate:
                raise DependencyDenied("rate_budget_exceeded", "请求速率预算已用尽，请稍后重试")
            if row.reserved_units + reserve_units > units:
                raise DependencyDenied("cost_budget_exceeded", "保守成本预留预算已用尽，请调整策略后重试")
            row.requests += 1
            row.reserved_units += reserve_units
        token = uuid.uuid4().hex
        session.add(AdmissionLease(token=token, tenant_id=tenant_id, finished=False,
                                   expires_at=timestamp + timedelta(seconds=lifetime)))
        await session.commit()
        return token


async def release(sessions, token):
    async with sessions() as session:
        await new_id(session)
        lease = await session.get(AdmissionLease, token)
        if lease is not None:
            lease.finished = True
            # 成本/速率预留不退款，未知结果也不能绕过总预算。
        await session.commit()


@asynccontextmanager
async def guard(domain, tenant_id, *, provider="", model_id="", endpoint="",
                sessions=None, reserve_units=None, audit=None, failure_classifier=None,
                enforce_window_limits=True, admission_lifetime=0):
    settings = get_settings()
    sessions = sessions or SessionLocal
    permit, admission = None, None
    try:
        if settings.dependency_controls_enabled:
            permit = await acquire(sessions, dependency_key(
                domain, tenant_id, provider, model_id, endpoint,
            ), settings, **({'probe_lifetime': admission_lifetime} if admission_lifetime else {}))
        if reserve_units is not None and settings.admission_enabled:
            admission = await admit(
                sessions, tenant_id, reserve_units, settings,
                lifetime=max(settings.provider_total_timeout_seconds + 30, settings.circuit_probe_seconds, admission_lifetime),
                enforce_window_limits=enforce_window_limits,
            )
    except BaseException as exc:
        if audit is not None:
            audit.append({"domain": domain, "state": "denied",
                          "errorCode": getattr(exc, "code", "dependency_control_store_unavailable")})
        if permit:
            await finish(sessions, permit, None, settings)
        raise
    observation = {"domain": domain, "state": permit.state if permit else "disabled",
                   "key": permit.key if permit else None, "reservedUnits": reserve_units if admission else None}
    if audit is not None:
        audit.append(observation)
    success = None
    try:
        yield observation
        success = True
    except BaseException as exc:
        success = None if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) or (
            failure_classifier is not None and not failure_classifier(exc)
        ) else False
        raise
    finally:
        async def cleanup():
            try:
                outcome = None if observation.get("cached") else success
                if permit:
                    observation["receiptApplied"] = await finish(
                        sessions, permit, outcome, settings,
                    )
                observation["outcome"] = "healthy" if outcome is True else "neutral" if outcome is None else "failed"
                if admission:
                    await release(sessions, admission)
            except Exception:
                observation["receiptApplied"] = False
                observation["errorCode"] = "dependency_control_store_unavailable"
                log.warning("依赖控制收据提交失败 errorCode=dependency_control_store_unavailable")
        from eic.core.async_cleanup import complete_cleanup
        await complete_cleanup(cleanup())

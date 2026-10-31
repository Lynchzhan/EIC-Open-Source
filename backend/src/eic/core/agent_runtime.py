"""Agent Run、Trace、Checkpoint 和工具调用的持久化边界。"""
from __future__ import annotations
from eic.core import observability

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eic.core.ids import new_id
from eic.models import AgentCheckpoint, AgentEvent, AgentRun, AgentRunLease, AgentToolCall

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "manual_review"})
RECOVERY_POLICIES = frozenset({"safe_replay", "manual_review", "terminal"})
AGENT_STATE_SCHEMA_VERSION = 1
WORKER_LEASE_SECONDS = 30
SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
)


class AgentLeaseLost(RuntimeError):
    """旧 Worker 已失去写入资格。"""


@dataclass(frozen=True)
class RecoveryDecision:
    action: Literal["safe_replay", "manual_review", "terminal"]
    reason: str
    run_id: str
    checkpoint_id: int | None = None
    checkpoint_seq: int | None = None
    state: dict[str, Any] | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_agent_run_id() -> str:
    return uuid.uuid4().hex


def new_trace_id() -> str:
    return uuid.uuid4().hex


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_payload(value: Any, key: str = "") -> Any:
    """只保留可序列化摘要，避免 trace 写入密钥和认证材料。"""
    if key and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact_payload(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(redact_payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest_payload(value: Any, *, redact: bool = True) -> str:
    serialized = canonical_json(value) if redact else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AgentRuntimeStore:
    """在调用方事务中写入 Agent 运行记录；不自行 commit。"""

    async def create_run(
        self,
        session: AsyncSession,
        *,
        tenant_id: int,
        session_id: int | None,
        request_id: str | None,
        graph_version: str,
        agent_run_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        if request_id is not None:
            # 与事件 ID 分配共用事务写锁；跨进程重复领取不能产生第二条 Run。
            await new_id(session)
            existing = await session.scalar(
                select(AgentRun).where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.request_id == request_id,
                ).with_for_update()
            )
            if existing is not None:
                if existing.session_id != session_id or existing.graph_version != graph_version:
                    raise ValueError("request_identity_conflict")
                return existing
        run = AgentRun(
            run_id=agent_run_id or new_agent_run_id(),
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=request_id,
            trace_id=trace_id or new_trace_id(),
            graph_version=graph_version,
            status="running",
            current_node="start",
            event_seq=0,
            checkpoint_seq=0,
            step_count=0,
            error_code="",
            run_metadata=redact_payload(metadata or {}),
            result={},
            started_at=utcnow(),
            heartbeat_at=utcnow(),
            finished_at=None,
        )
        session.add(run)
        now = utcnow()
        session.add(AgentRunLease(
            run_id=run.run_id,
            worker_token="",
            expires_at=now,
            state="released",
            heartbeat_at=now,
            claimed_at=None,
            attempt=0,
        ))
        await session.flush()
        return run

    async def append_event(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        node_name: str,
        event_type: str,
        status: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        run = await self._require_run(session, run_id)
        run.event_seq += 1
        run.step_count += 1
        run.current_node = node_name
        run.heartbeat_at = utcnow()
        safe_payload = redact_payload(payload or {})
        event = AgentEvent(
            id=await new_id(session),
            run_id=run_id,
            sequence=run.event_seq,
            node_name=node_name,
            event_type=event_type,
            status=status,
            payload=safe_payload,
            payload_hash=digest_payload(safe_payload),
            created_at=utcnow(),
        )
        session.add(event)
        await session.flush()
        category = ("approval" if "approval" in event_type else
                    "budget" if "budget" in event_type or "budget" in run.error_code else
                    "error" if status in {"failed", "error"} else None)
        if category is not None:
            observability.stage(session, observability.observation(
                category, node_name, status, tenant_id=run.tenant_id,
                event_key=f"agent-event:{run_id}:{event.sequence}", run_id=run_id,
                request_id=run.request_id, trace_id=run.trace_id, source="agent_events",
                error_code=run.error_code or None, source_sequence=event.sequence,
                occurred_at=event.created_at, summary={"eventType": event_type}))
        return event

    async def append_graph_events(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        events: list[dict[str, Any]],
    ) -> list[AgentEvent]:
        stored: list[AgentEvent] = []
        for item in events:
            stored.append(await self.append_event(
                session,
                run_id=run_id,
                node_name=str(item.get("node") or "unknown"),
                event_type=str(item.get("eventType") or "node_completed"),
                status=str(item.get("status") or "completed"),
                payload=item,
            ))
        return stored

    async def latest_checkpoint(
        self,
        session: AsyncSession,
        *,
        run_id: str,
    ) -> AgentCheckpoint | None:
        return await session.scalar(
            select(AgentCheckpoint)
            .where(AgentCheckpoint.run_id == run_id)
            .order_by(AgentCheckpoint.checkpoint_seq.desc(), AgentCheckpoint.id.desc())
            .limit(1)
        )

    async def append_checkpoint(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        node_name: str,
        state: dict[str, Any],
        state_schema_version: int = 1,
        resume_policy: str = "safe_replay",
    ) -> AgentCheckpoint:
        if resume_policy not in RECOVERY_POLICIES:
            raise ValueError("invalid checkpoint resume policy")
        run = await self._require_run(session, run_id)
        run.checkpoint_seq += 1
        run.current_node = node_name
        run.heartbeat_at = utcnow()
        safe_state = redact_payload(state)
        checkpoint = AgentCheckpoint(
            id=await new_id(session),
            run_id=run_id,
            checkpoint_seq=run.checkpoint_seq,
            node_name=node_name,
            state_schema_version=state_schema_version,
            state_json=safe_state,
            state_hash=digest_payload(safe_state),
            resume_policy=resume_policy,
            created_at=utcnow(),
        )
        session.add(checkpoint)
        await session.flush()
        return checkpoint

    async def decide_recovery(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        expected_graph_version: str | None = None,
        expected_state_schema_version: int = AGENT_STATE_SCHEMA_VERSION,
    ) -> RecoveryDecision:
        """只允许从完整、同版本且可安全重放的节点继续。"""
        run = await session.get(AgentRun, run_id)
        if run is None:
            return RecoveryDecision("manual_review", "run_not_found", run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return RecoveryDecision("terminal", "run_already_terminal", run_id)
        if expected_graph_version and run.graph_version != expected_graph_version:
            return RecoveryDecision("manual_review", "graph_version_mismatch", run_id)
        checkpoint = await self.latest_checkpoint(session, run_id=run_id)
        if checkpoint is None:
            return RecoveryDecision("manual_review", "checkpoint_missing", run_id)
        if checkpoint.state_schema_version != expected_state_schema_version:
            return RecoveryDecision(
                "manual_review",
                "checkpoint_schema_unsupported",
                run_id,
                checkpoint.id,
                checkpoint.checkpoint_seq,
            )
        raw_state = checkpoint.state_json
        if not isinstance(raw_state, dict):
            return RecoveryDecision(
                "manual_review",
                "checkpoint_state_invalid",
                run_id,
                checkpoint.id,
                checkpoint.checkpoint_seq,
            )
        state = raw_state
        if digest_payload(state) != checkpoint.state_hash:
            return RecoveryDecision(
                "manual_review",
                "checkpoint_integrity_failed",
                run_id,
                checkpoint.id,
                checkpoint.checkpoint_seq,
            )
        if checkpoint.resume_policy not in RECOVERY_POLICIES:
            return RecoveryDecision(
                "manual_review",
                "checkpoint_policy_unsupported",
                run_id,
                checkpoint.id,
                checkpoint.checkpoint_seq,
                state,
            )
        if checkpoint.resume_policy == "terminal":
            return RecoveryDecision(
                "terminal",
                "checkpoint_terminal",
                run_id,
                checkpoint.id,
                checkpoint.checkpoint_seq,
                state,
            )
        if checkpoint.resume_policy == "manual_review":
            return RecoveryDecision(
                "manual_review",
                "checkpoint_requires_review",
                run_id,
                checkpoint.id,
                checkpoint.checkpoint_seq,
                state,
            )
        if state.get("providerOutcome") == "uncertain" or state.get("providerStarted") is True:
            return RecoveryDecision(
                "manual_review",
                "provider_outcome_uncertain",
                run_id,
                checkpoint.id,
                checkpoint.checkpoint_seq,
                state,
            )
        return RecoveryDecision(
            "safe_replay",
            "latest_safe_checkpoint",
            run_id,
            checkpoint.id,
            checkpoint.checkpoint_seq,
            state,
        )

    async def claim_worker(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        lease_seconds: int = WORKER_LEASE_SECONDS,
    ) -> str | None:
        """以数据库条件更新完成单 Worker 领取；活跃租约会拒绝重复 Worker。"""
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ValueError("agent run does not exist")
        if run.status in TERMINAL_RUN_STATUSES:
            return None
        lease = await session.get(AgentRunLease, run_id)
        if lease is None:
            now = utcnow()
            lease = AgentRunLease(
                run_id=run_id,
                worker_token="",
                expires_at=now,
                state="released",
                heartbeat_at=now,
                claimed_at=None,
                attempt=0,
            )
            session.add(lease)
            await session.flush()
        token = uuid.uuid4().hex
        now = utcnow()
        changed = await session.execute(
            update(AgentRunLease)
            .where(
                AgentRunLease.run_id == run_id,
                or_(
                    AgentRunLease.state != "active",
                    AgentRunLease.expires_at <= now,
                ),
            )
            .values(
                worker_token=token,
                expires_at=now + timedelta(seconds=lease_seconds),
                state="active",
                heartbeat_at=now,
                claimed_at=now,
                attempt=AgentRunLease.attempt + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            return None
        return token

    async def renew_worker(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        worker_token: str,
        lease_seconds: int = WORKER_LEASE_SECONDS,
    ) -> None:
        now = utcnow()
        changed = await session.execute(
            update(AgentRunLease)
            .where(
                AgentRunLease.run_id == run_id,
                AgentRunLease.worker_token == worker_token,
                AgentRunLease.state == "active",
                AgentRunLease.expires_at > now,
            )
            .values(
                expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise AgentLeaseLost("agent worker lease expired or revoked")

    async def release_worker(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        worker_token: str,
        state: str = "released",
    ) -> bool:
        if state not in {"released", "finished"}:
            raise ValueError("invalid agent worker lease state")
        changed = await session.execute(
            update(AgentRunLease)
            .where(
                AgentRunLease.run_id == run_id,
                AgentRunLease.worker_token == worker_token,
                AgentRunLease.state == "active",
            )
            .values(state=state, expires_at=utcnow(), heartbeat_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        return changed.rowcount == 1

    async def append_tool_call(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: Any,
        status: str,
        timeout_ms: int = 0,
        retry_count: int = 0,
        effect_status: str = "none",
        result: Any = None,
        error_code: str = "",
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> AgentToolCall:
        await self._require_run(session, run_id)
        record = AgentToolCall(
            id=await new_id(session),
            run_id=run_id,
            call_id=call_id,
            tool_name=tool_name,
            input_digest=digest_payload(arguments, redact=False),
            output_digest=digest_payload(result) if result is not None else None,
            status=status,
            timeout_ms=timeout_ms,
            retry_count=retry_count,
            effect_status=effect_status,
            error_code=error_code,
            started_at=started_at or utcnow(),
            finished_at=finished_at,
            created_at=utcnow(),
        )
        session.add(record)
        await session.flush()
        return record

    async def get_tool_call(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        call_id: str,
    ) -> AgentToolCall | None:
        """按运行和 call_id 读取已记录调用，供恢复和幂等重放使用。"""
        return await session.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == run_id, AgentToolCall.call_id == call_id)
            .limit(1)
        )

    async def append_tool_call_once(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: Any,
        status: str,
        timeout_ms: int = 0,
        retry_count: int = 0,
        effect_status: str = "none",
        result: Any = None,
        error_code: str = "",
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> AgentToolCall:
        """同一调用先记录 intent，再单向完成；终态和输入身份不可覆写。"""
        existing = await self.get_tool_call(session, run_id=run_id, call_id=call_id)
        if existing is not None:
            if (existing.tool_name, existing.input_digest) != (
                tool_name, digest_payload(arguments, redact=False),
            ):
                raise ValueError("duplicate_tool_call_id")
            if existing.status == "running" and status != "running":
                existing.status = status
                existing.effect_status = effect_status
                existing.output_digest = digest_payload(result) if result is not None else None
                existing.error_code = error_code
                existing.finished_at = finished_at or utcnow()
                await session.flush()
            return existing
        return await self.append_tool_call(
            session,
            run_id=run_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            status=status,
            timeout_ms=timeout_ms,
            retry_count=retry_count,
            effect_status=effect_status,
            result=result,
            error_code=error_code,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def finish_run(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        status: str,
        error_code: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> AgentRun | None:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("invalid agent run terminal status")
        run = await session.get(AgentRun, run_id)
        if run is None:
            return None
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        now = utcnow()
        run.status = status
        run.current_node = "terminal"
        run.error_code = error_code or ""
        run.result = redact_payload(result or {})
        run.heartbeat_at = now
        run.finished_at = now
        await session.execute(
            update(AgentRunLease)
            .where(AgentRunLease.run_id == run_id)
            .values(state="finished", expires_at=now, heartbeat_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.flush()
        return run

    async def finish_for_request(
        self,
        session: AsyncSession,
        *,
        tenant_id: int,
        request_id: str,
        status: str,
        error_code: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> AgentRun | None:
        run = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.request_id == request_id,
            )
            .order_by(AgentRun.started_at.desc(), AgentRun.run_id.desc())
            .limit(1)
        )
        if run is None:
            return None
        return await self.finish_run(
            session,
            run_id=run.run_id,
            status=status,
            error_code=error_code,
            result=result,
        )

    async def _require_run(self, session: AsyncSession, run_id: str) -> AgentRun:
        from eic.core import agent_worker, generation
        await new_id(session)
        await generation.fence(session)
        await agent_worker.fence(session)
        owner = agent_worker.OWNER.get()
        if owner is not None and owner[0] != run_id:
            raise AgentLeaseLost("agent_worker_run_mismatch")
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ValueError("agent run does not exist")
        return run


runtime_store = AgentRuntimeStore()

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eic.core.security import decrypt_secret, encrypt_secret
from eic.models import ToolAudit, ToolExecutionIntent


class ToolIntentError(RuntimeError):
    """Base error for durable tool intent decisions."""


class ToolIntentConflict(ToolIntentError):
    """An idempotency key is already bound to different normalized input."""


class ToolIntentInProgress(ToolIntentError):
    """The prior attempt is executing or has an uncertain outcome."""


@dataclass(frozen=True)
class ToolIntentConvergenceScope:
    """由已持久claim的服务路径保存；不从客户端配置推导归属。"""
    intent_id: str
    tenant_id: int
    user_id: int
    tool_name: str
    input_digest: str
    audit_id: int
    audit_tool_name: str
    audit_input_digest: str
    audit_tool_type: str = "mcp"


async def converge_tool_intent_unknown(sessions, *, scope: ToolIntentConvergenceScope,
                                      error_code: str = 'dispatch_outcome_unknown') -> bool:
    """新session重新核对归属，CAS仅收敛executing；异常交由调用方报告失败。"""
    if scope.audit_tool_type not in {"mcp", "local"}:
        raise ToolIntentConflict('unsupported convergence audit type')
    if scope.tool_name != f"{scope.audit_tool_type}:{scope.audit_tool_name}":
        raise ToolIntentConflict('convergence tool and audit are not bound')
    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(select(ToolExecutionIntent).where(
                ToolExecutionIntent.id == scope.intent_id,
                ToolExecutionIntent.tenant_id == scope.tenant_id,
                ToolExecutionIntent.user_id == scope.user_id,
                ToolExecutionIntent.tool_name == scope.tool_name,
                ToolExecutionIntent.input_digest == scope.input_digest))
            audit = await session.scalar(select(ToolAudit).where(
                ToolAudit.id == scope.audit_id, ToolAudit.tenant_id == scope.tenant_id,
                ToolAudit.user_id == scope.user_id, ToolAudit.tool_type == scope.audit_tool_type,
                ToolAudit.tool_name == scope.audit_tool_name,
                ToolAudit.input_digest == scope.audit_input_digest))
            if row is None or audit is None:
                raise ToolIntentConflict('convergence scope does not own intent and audit')
            if row.state != 'executing':
                return False
            changed = await session.execute(update(ToolExecutionIntent).where(
                ToolExecutionIntent.id == scope.intent_id,
                ToolExecutionIntent.tenant_id == scope.tenant_id,
                ToolExecutionIntent.user_id == scope.user_id,
                ToolExecutionIntent.tool_name == scope.tool_name,
                ToolExecutionIntent.input_digest == scope.input_digest,
                ToolExecutionIntent.state == 'executing').values(
                    state='unknown', error_code=error_code[:80]))
            if changed.rowcount != 1:
                return False
            audit.outcome = 'unknown'
            audit.error_message = error_code[:80]
            audit.output_digest = None
        return True


Authorize = Callable[[], Awaitable[None] | None]
Handler = Callable[[], Awaitable[Any] | Any]


@dataclass(frozen=True)
class ToolIntentDecision:
    intent_id: str
    replayed: bool
    result: Any = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _authorize(authorize: Authorize) -> None:
    result = authorize()
    if result is not None:
        await result


def _scope_query(tenant_id: int, user_id: int, tool_name: str, key_digest: str):
    return select(ToolExecutionIntent).where(
        ToolExecutionIntent.tenant_id == tenant_id,
        ToolExecutionIntent.user_id == user_id,
        ToolExecutionIntent.idempotency_key_digest == key_digest,
    )


def _decision(row: ToolExecutionIntent, input_digest: str, tool_name: str) -> ToolIntentDecision:
    if row.input_digest != input_digest or row.tool_name != tool_name:
        raise ToolIntentConflict("idempotency key is bound to different input")
    if row.state != "succeeded" or row.result_encrypted is None:
        raise ToolIntentInProgress("prior tool outcome is not safely replayable")
    return ToolIntentDecision(row.id, True, json.loads(decrypt_secret(row.result_encrypted)))


async def begin_tool_intent(
    session: AsyncSession,
    *,
    tenant_id: int,
    user_id: int,
    tool_name: str,
    normalized_input: Any,
    idempotency_key: str | None,
    authorize: Authorize,
    on_prepared: Callable[[str, str], None] | None = None,
) -> ToolIntentDecision:
    """Authorize and durably claim a dispatch before any external effect."""
    await _authorize(authorize)
    if not tool_name or len(tool_name) > 200:
        raise ValueError("invalid tool name")
    key = idempotency_key or uuid.uuid4().hex
    if len(key) > 512:
        raise ValueError("idempotency key is too long")
    key_digest = _digest(key)
    input_digest = _digest(_canonical_json(normalized_input))
    row = ToolExecutionIntent(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        user_id=user_id,
        tool_name=tool_name,
        idempotency_key_digest=key_digest,
        input_digest=input_digest,
        state="executing",
    )
    session.add(row)
    # 提交前把服务生成的身份交回可信调用方，覆盖提交响应丢失或取消窗口。
    if on_prepared is not None:
        on_prepared(row.id, input_digest)
    try:
        await session.commit()
        return ToolIntentDecision(row.id, False)
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(_scope_query(tenant_id, user_id, tool_name, key_digest))
        if existing is None:
            raise
        await _authorize(authorize)
        return _decision(existing, input_digest, tool_name)
    except Exception:
        await session.rollback()
        raise


async def finish_tool_intent(
    session: AsyncSession,
    *,
    intent_id: str,
    tenant_id: int,
    user_id: int,
    tool_name: str,
    result: Any = None,
    error_code: str | None = None,
) -> None:
    """Commit a known result, or a non-replayable terminal uncertainty."""
    row = await session.scalar(select(ToolExecutionIntent).where(
        ToolExecutionIntent.id == intent_id,
        ToolExecutionIntent.tenant_id == tenant_id,
        ToolExecutionIntent.user_id == user_id,
        ToolExecutionIntent.tool_name == tool_name,
    ))
    if row is None or row.state != "executing":
        raise ToolIntentInProgress("intent is missing or no longer executable")
    if error_code is None:
        serialized = _canonical_json(result)
        row.result_encrypted = encrypt_secret(serialized)
        row.result_digest = _digest(serialized)
        row.state = "succeeded"
        row.error_code = None
    else:
        row.state = "unknown"
        row.error_code = error_code[:80]
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def replay_tool_intent(
    session: AsyncSession,
    *,
    tenant_id: int,
    user_id: int,
    tool_name: str,
    normalized_input: Any,
    idempotency_key: str,
    authorize: Authorize,
) -> ToolIntentDecision:
    """Read a prior result only after rechecking current authorization."""
    await _authorize(authorize)
    row = await session.scalar(_scope_query(tenant_id, user_id, tool_name, _digest(idempotency_key)))
    if row is None:
        raise LookupError("tool intent not found")
    return _decision(row, _digest(_canonical_json(normalized_input)), tool_name)


async def execute_with_tool_intent(
    session: AsyncSession,
    *,
    tenant_id: int,
    user_id: int,
    tool_name: str,
    normalized_input: Any,
    idempotency_key: str | None,
    authorize: Authorize,
    handler: Handler,
) -> ToolIntentDecision:
    """Claim, reauthorize at dispatch, execute once, and persist encrypted output."""
    decision = await begin_tool_intent(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        tool_name=tool_name,
        normalized_input=normalized_input,
        idempotency_key=idempotency_key,
        authorize=authorize,
    )
    if decision.replayed:
        return decision
    try:
        await _authorize(authorize)
        result = handler()
        if hasattr(result, "__await__"):
            result = await result
    except Exception:
        try:
            await finish_tool_intent(
                session,
                intent_id=decision.intent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                tool_name=tool_name,
                error_code="dispatch_outcome_unknown",
            )
        except Exception:
            pass
        raise
    await finish_tool_intent(
        session,
        intent_id=decision.intent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        tool_name=tool_name,
        result=result,
    )
    return ToolIntentDecision(decision.intent_id, False, result)

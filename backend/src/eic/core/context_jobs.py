"""上下文后台任务的持久模型调用边界；业务结果由调用方另行 CAS 提交。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import jsonschema
from sqlalchemy import select, func

from eic.ai.agent_graph import AGENT_GRAPH_VERSION, AgentBudget
from eic.ai.providers import ModelProviderError, stream_chat_completion
from eic.ai.reliable_calls import reliable_stream
from eic.ai.runtime_policy import ACTIVE, Account, BudgetStopped, DEFAULTS, normalize_policy, settle_call_state
from eic.ai.shared_executor import ExecutorCheckpointer, ExecutionServices, build_execution_graph, execute_shared
from eic.core import observability
from eic.core.ids import new_id
from eic.core.langfuse_context import turn_context
from eic.core.security import _fernet
from eic.models import AgentRun, ContextJob, ConversationBudget, OutboxEvent


KINDS = {'chat-compaction', 'memory-extraction', 'conversation-label'}
LEASE_SECONDS = 30


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _fingerprint(model):
    return _hash({key: getattr(model, key, None) for key in (
        'id', 'tenant_id', 'provider', 'model_name', 'endpoint', 'secret_encrypted',
        'max_input_tokens', 'max_output_tokens', 'chat_parameter_profile', 'reasoning_policy')})


class ContextJobLost(RuntimeError):
    pass


class _BackgroundAccount(Account):
    """共用原账本；先预留时长，完成后换成实际时长，不创建前台 turn。

    崩溃保留时长预留，宁可保守拒绝，也不按零计费。前台 totals 无需修改。
    """
    def __init__(self, sessions, *args):
        super().__init__(*args)
        self.sessions = sessions

    async def reserve(self, model, kind, tokens):
        async with self.sessions() as session:
            row, state = await self.locked(session)
            total, policy = self.totals(state), state['policy']
            if state['history']['unknown']:
                await self.stop(session, row, state, 'conversation_usage_incomplete')
            if total['calls'] >= policy['maxCalls']:
                await self.stop(session, row, state, 'conversation_call_limit')
            if total['tokens'] + tokens > policy['maxTokens']:
                await self.stop(session, row, state, 'conversation_token_limit')
            if total['seconds'] >= policy['maxSeconds']:
                await self.stop(session, row, state, 'conversation_time_limit')
            seconds = min(policy['callTimeoutSeconds'], policy['maxSeconds'] - total['seconds'])
            key = uuid.uuid4().hex
            state['entries'][key] = {'kind': kind, 'modelId': str(model.id),
                'tier': state['tier'], 'tokens': tokens, 'seconds': seconds,
                'status': 'pending', 'backgroundSecondsReserved': seconds}
            # Account.totals 只累加 history/turns，不累加 entry.seconds。
            state['history']['seconds'] += seconds
            state.update(reason=None, reasonCode=None, blocked=None)
            row.state = state
            await session.commit()
            return key, seconds

    async def settle(self, key, sink, elapsed, failed):
        async with self.sessions() as session:
            row, state = await self.locked(session)
            entry = state['entries'][key]
            reserved = entry.pop('backgroundSecondsReserved', 0)
            invoked = bool(sink.get('_invoked'))
            state['history']['seconds'] += (max(0, elapsed) if invoked else 0) - reserved
            settle_call_state(state, key, sink, elapsed, failed,
                              reason='后台调用达到档位阈值或失败，下次前台调用重新选择模型')
            row.state = state
            await session.commit()


def _strict_json(text):
    def constant(value):
        raise ValueError('context_job_nonfinite_json')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('context_job_duplicate_json_key')
            result[key] = value
        return result

    return json.loads(text, parse_constant=constant, object_pairs_hook=pairs)


class _Runtime:
    def __init__(self, sessions, job_id, tenant_id, ctx, validate_source, binding):
        self.sessions, self.job_id, self.tenant_id = sessions, job_id, tenant_id
        self.ctx, self.validate_source, self.binding = dict(ctx), validate_source, binding
        self.token = uuid.uuid4().hex
        self.run_id = None
        self.identity = self.model_hash = None

    async def checked(self, session, *, owned=True):
        # allocator -> event -> job：认领、续租、结果写入采用同一锁顺序。
        await new_id(session)
        event = await session.scalar(select(OutboxEvent).where(
            OutboxEvent.id == self.ctx['event_id']).with_for_update())
        job = await session.scalar(select(ContextJob).where(
            ContextJob.id == self.job_id).with_for_update())
        now = _utc(await session.scalar(select(func.current_timestamp())))
        if (job is None or event is None or job.tenant_id != self.tenant_id
                or job.kind not in KINDS or job.session_id is None
                or event.kind != job.kind or event.entity_id != job.id
                or event.tenant_id != job.tenant_id or event.consume_status != 'running'
                or event.consume_token != self.ctx['consume_token']
                or event.consume_until is None or _utc(event.consume_until) <= now):
            raise ContextJobLost('context_job_consumer_fence_lost')
        identity = _hash([job.id, job.tenant_id, job.owner_id, job.session_id,
                          job.kind, job.job_key, job.source_hash, job.payload, self.binding])
        if self.identity is not None and identity != self.identity:
            raise ContextJobLost('context_job_source_binding_changed')
        if owned and (job.lease_token != self.token or job.run_id != self.run_id
                or job.lease_until is None or _utc(job.lease_until) <= now
                or job.status not in {'running', 'provider_started', 'provider_done'}):
            raise ContextJobLost('context_job_lease_lost')
        model = await self.validate_source(session, job)
        if model is None or model.tenant_id != job.tenant_id:
            raise ContextJobLost('context_job_model_scope_invalid')
        fingerprint = _fingerprint(model)
        if self.model_hash is not None and fingerprint != self.model_hash:
            raise ContextJobLost('context_job_model_changed')
        self.identity, self.model_hash = identity, fingerprint
        if owned:
            job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return job, model, now

    async def guard(self):
        async with self.sessions() as session:
            await self.checked(session)
            await session.commit()


class _ContextCheckpointer(ExecutorCheckpointer):
    def __init__(self, runtime):
        super().__init__(runtime.sessions)
        self.runtime = runtime

    async def _guard(self, session, run_id):
        job, _, _ = await self.runtime.checked(session)
        run = await session.get(AgentRun, run_id, populate_existing=True)
        if (run_id != self.runtime.run_id or run is None or run.status != 'running'
                or run.tenant_id != job.tenant_id or run.session_id != job.session_id
                or run.graph_version != AGENT_GRAPH_VERSION
                or run.run_metadata != {'contextJobId': job.id,
                    'binding': self.runtime.identity, 'model': self.runtime.model_hash}):
            raise ContextJobLost('context_job_graph_binding_invalid')
        return run

    async def _authorize_read(self, session, run_id):
        return await self._guard(session, run_id)


def _decode_result(job, binding):
    value = json.loads(_fernet().decrypt(job.result_encrypted.encode('ascii')))
    if value['binding'] != binding:
        raise ContextJobLost('context_job_result_binding_invalid')
    return value['result']


async def guard_context_job_commit(session, job_id, tenant_id, ctx):
    """业务事务内调用，返回锁定 job；调用方在同一事务校验来源并写 completed。

    ctx 必须是传给 run_json_job 的原服务端字典，包含其签发的进程内租约收据。
    不提交事务，不续租，不授予模型调用权；禁止跨事务缓存返回的 ORM 对象。
    """
    receipt = ctx.get('_context_job_receipt')
    if not isinstance(receipt, tuple) or len(receipt) != 6:
        raise ContextJobLost('context_job_commit_receipt_missing')
    await new_id(session)
    event = await session.scalar(select(OutboxEvent).where(
        OutboxEvent.id == ctx['event_id']).with_for_update())
    job = await session.scalar(select(ContextJob).where(
        ContextJob.id == job_id).with_for_update())
    now = _utc(await session.scalar(select(func.current_timestamp())))
    if (job is None or event is None or job.tenant_id != tenant_id
            or job.status != 'provider_done' or job.kind not in KINDS
            or event.kind != job.kind or event.entity_id != job.id
            or event.tenant_id != tenant_id or event.consume_status != 'running'
            or event.consume_token != ctx['consume_token'] or event.consume_until is None
            or _utc(event.consume_until) <= now or job.lease_until is None
            or _utc(job.lease_until) <= now
            or receipt != (job.id, tenant_id, job.run_id, job.lease_token,
                           job.source_hash, _hash(job.payload))):
        raise ContextJobLost('context_job_commit_fence_lost')
    return job


def _receipt(ctx, job):
    ctx['_context_job_receipt'] = (job.id, job.tenant_id, job.run_id,
                                  job.lease_token, job.source_hash, _hash(job.payload))


async def _recover_run(session, runtime, job, status):
    run = await session.get(AgentRun, job.run_id, populate_existing=True)
    if (run is None or run.tenant_id != job.tenant_id or run.session_id != job.session_id
            or run.request_id != f'context:{job.id}'
            or run.trace_id not in {observability.turn_trace(job.tenant_id, f'context:{job.id}'),
                                   uuid.uuid5(uuid.NAMESPACE_URL, job.run_id).hex}
            or run.graph_version != AGENT_GRAPH_VERSION
            or run.run_metadata != {'contextJobId': job.id,
                'binding': runtime.identity, 'model': runtime.model_hash}
            or run.status not in {'running', status}):
        raise ContextJobLost('context_job_recovery_binding_invalid')
    if run.status == 'running':
        # 这是恢复收敛时刻，不能当作原执行终点；导出契约单独投影。
        run.status = status
        run.error_code = f'context_job_recovered_{status}'
        run.finished_at = datetime.now(UTC)
    _bind_observation_trace(job, run)
    return run


def _bind_observation_trace(job, run):
    usage = dict(job.usage or {})
    usage['_observation'] = {**usage.get('_observation', {}), 'trace_id': run.trace_id}
    job.usage = usage


async def run_json_job(sessions, job_id, tenant_id, ctx, *, system: str, content: str,
                       schema: dict, validate_source) -> dict:
    """返回已校验 JSON；仅提交 provider_done，业务 completed 由 handler 提交。

    validate_source 必须在当前事务检查用户、会话、配置、来源及权限并返回模型。
    ctx 必须来自可信 Outbox 消费者，不能由客户端请求构造。
    """
    jsonschema.validators.validator_for(schema).check_schema(schema)
    runtime = _Runtime(sessions, job_id, tenant_id, ctx, validate_source,
                       _hash([system, content, schema]))
    async with sessions() as session:
        job, model, now = await runtime.checked(session, owned=False)
        result_binding = [runtime.identity, runtime.model_hash]
        if job.status in {'provider_done', 'completed'}:
            result = _decode_result(job, result_binding)
            jsonschema.validate(result, schema)
            if not isinstance(result, dict):
                raise ValueError('context_job_result_not_object')
            if job.status == 'provider_done':
                if job.lease_token and job.lease_until and _utc(job.lease_until) > now:
                    raise ContextJobLost('context_job_already_claimed')
                await _recover_run(session, runtime, job, 'completed')
                job.lease_token = runtime.token
                job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
                _receipt(ctx, job)
            else:
                await _recover_run(session, runtime, job, 'completed')
            await session.commit()
            return result
        if job.status in {'cancelled', 'uncertain', 'failed'}:
            raise ContextJobLost('context_job_terminal_no_replay')
        if job.lease_until is not None and _utc(job.lease_until) > now and job.lease_token:
            raise ContextJobLost('context_job_already_claimed')
        if job.status == 'provider_started':
            await _recover_run(session, runtime, job, 'uncertain')
            job.status, job.error_code = 'uncertain', 'context_job_provider_outcome_unknown'
            await session.commit()
            raise ContextJobLost('context_job_provider_outcome_unknown')
        if job.status not in {'pending', 'running'}:
            raise ContextJobLost('context_job_status_invalid')
        # running 只允许从未派发的任务重新认领；外呼前原子切到 provider_started。
        runtime.run_id = job.run_id or ('context:' + uuid.uuid4().hex)
        job.run_id, job.lease_token = runtime.run_id, runtime.token
        job.lease_until, job.status = now + timedelta(seconds=LEASE_SECONDS), 'running'
        run = await session.get(AgentRun, runtime.run_id)
        metadata = {'contextJobId': job.id, 'binding': runtime.identity, 'model': runtime.model_hash}
        if run is None:
            run_started_at = datetime.now(UTC)
            run = AgentRun(run_id=runtime.run_id, tenant_id=tenant_id,
                session_id=job.session_id, request_id=f'context:{job.id}',
                trace_id=observability.turn_trace(tenant_id, f'context:{job_id}'),
                graph_version=AGENT_GRAPH_VERSION, status='running', run_metadata=metadata,
                started_at=run_started_at)
            session.add(run)
        elif run.run_metadata != metadata or run.status != 'running':
            raise ContextJobLost('context_job_run_conflict')
        if (run.tenant_id != job.tenant_id or run.session_id != job.session_id
                or run.request_id != f'context:{job.id}' or run.graph_version != AGENT_GRAPH_VERSION
                or run.trace_id not in {observability.turn_trace(job.tenant_id, f'context:{job.id}'),
                                       uuid.uuid5(uuid.NAMESPACE_URL, run.run_id).hex}):
            raise ContextJobLost('context_job_run_conflict')
        _bind_observation_trace(job, run)
        existing = await session.get(ConversationBudget, job.session_id)
        if existing is not None and (existing.tenant_id != tenant_id or existing.owner_id != job.owner_id):
            raise ContextJobLost('context_job_budget_scope_invalid')
        policy = normalize_policy(existing.state['policy'] if existing is not None else
                                  (job.payload or {}).get('runtimePolicy') or DEFAULTS)
        account = _BackgroundAccount(sessions, tenant_id, job.owner_id, job.session_id, policy)
        await session.commit()

    usage, attempts = {}, {}
    known_result = False

    async def decide(state):
        nonlocal known_result
        async with sessions() as session:
            current, bound_model, _ = await runtime.checked(session)
            if current.status != 'running':
                raise ContextJobLost('context_job_dispatch_already_started')
            from eic.ai.budget import message_units
            from eic.ai.providers import chat_parameters
            _, parameters = chat_parameters(bound_model)
            if message_units([{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]) > parameters['inputLimit']:
                known_result = True
                raise ValueError('context_job_input_budget_exceeded')
            current.status = 'provider_started'
            await session.commit()
        parts = []
        source = reliable_stream(lambda: stream_chat_completion(bound_model, content,
            messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': content}],
            usage_sink=usage), bound_model, sessions=sessions, metadata=attempts)
        try:
            async for part in source:
                await runtime.guard()
                parts.append(part)
        finally:
            await source.aclose()
        known_result = True
        try:
            result = _strict_json(''.join(parts))
            jsonschema.validate(result, schema)
            if not isinstance(result, dict):
                raise ValueError('context_job_result_not_object')
        except (ValueError, jsonschema.ValidationError):
            await terminal('failed', 'context_job_invalid_json')
            raise ValueError('context_job_invalid_json') from None
        async with sessions() as session:
            current, _, _ = await runtime.checked(session)
            current.result_encrypted = _fernet().encrypt(json.dumps(
                {'binding': result_binding, 'result': result}, allow_nan=False,
                ensure_ascii=True).encode()).decode('ascii')
            current.usage = {**(current.usage or {}), **{key: usage[key] for key in (
                'input_tokens', 'output_tokens', 'total_tokens', 'cached_tokens') if key in usage}}
            current.status, current.error_code = 'provider_done', ''
            await session.commit()
        return {'content': 'stored'}

    async def terminal(status, code):
        async with sessions() as session:
            current, _, now = await runtime.checked(session)
            if current.status == 'provider_done':
                return
            current.status, current.error_code = status, code
            run = await session.get(AgentRun, runtime.run_id)
            if run is not None:
                run.status, run.error_code, run.finished_at = status, code, datetime.now(UTC)
            from eic.core.memory_lifecycle import record
            record(session, current, 'conversation_label' if current.kind == 'conversation-label' else
                   'context_compaction' if current.kind == 'chat-compaction' else 'memory_extraction', status)
            await session.commit()

    async def renew():
        while True:
            await asyncio.sleep(LEASE_SECONDS / 3)
            await runtime.guard()

    async def no_tools(*args):
        raise ValueError('context_job_tools_disabled')

    active_token = ACTIVE.set(account)
    observation_token = observability.CONTEXT.set(turn_context(tenant_id=tenant_id,
        request_id=f'context:{job_id}', trace_id=run.trace_id, run_id=runtime.run_id, chat_id=str(account.session_id),
        owner_key=observability.actor_key(tenant_id, account.owner_id)))
    execution = heartbeat = None
    try:
        services = ExecutionServices(model=decide, execute_tool=no_tools, validate_tool=no_tools,
            guard=runtime.guard, budget=AgentBudget(max_steps=1, max_tool_calls=1,
                                                   max_total_ms=policy['callTimeoutSeconds'] * 1000))
        execution = asyncio.create_task(execute_shared('context-job', run_id=runtime.run_id,
            services=services, graph=build_execution_graph(_ContextCheckpointer(runtime))))
        heartbeat = asyncio.create_task(renew())
        done, _ = await asyncio.wait((execution, heartbeat), return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            await heartbeat
            raise ContextJobLost('context_job_heartbeat_stopped')
        await execution
        async with sessions() as session:
            current, _, now = await runtime.checked(session)
            if current.status != 'provider_done':
                raise ContextJobLost('context_job_result_missing')
            result = _decode_result(current, result_binding)
            run = await session.get(AgentRun, runtime.run_id)
            run.status, run.finished_at = 'completed', datetime.now(UTC)
            _receipt(ctx, current)
            await session.commit()
            return result
    except BaseException as exc:
        if execution is not None:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        try:
            safe_label_failure = (job.kind == 'conversation-label' and isinstance(exc, ModelProviderError)
                and exc.reason in {'invalid_provider_configuration', 'invalid_provider_endpoint',
                    'invalid_provider_credentials', 'circuit_open', 'provider_parameters_rejected',
                    'provider_permission_denied', 'provider_rate_limited', 'provider_error'})
            await terminal('failed' if known_result or safe_label_failure else 'uncertain',
                           'conversation_label_model_unavailable' if safe_label_failure else
                           'context_job_local_failure' if known_result else 'context_job_outcome_unknown')
        except Exception:
            # 已失租约不能越权落盘；provider_started 的持久栅栏阻止下一消费者重放。
            pass
        raise
    finally:
        for task in (execution, heartbeat):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (execution, heartbeat) if task is not None),
                             return_exceptions=True)
        observability.CONTEXT.reset(observation_token)
        ACTIVE.reset(active_token)

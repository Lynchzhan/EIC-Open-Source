"""只读 task 的持久图、数据库租约和恢复身份栅栏。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from sqlalchemy import select, update, func

from eic.ai.agent_graph import AGENT_GRAPH_VERSION
from eic.core import tasks, task_schedule
from eic.core.config import get_settings
from eic.core.ids import new_id
from eic.core.security import _fernet
from eic.models import ScheduledTask, TaskRun, TaskGraphCheckpoint, TaskGraphLease, TaskGraphWrite, OutboxEvent

LEASE_SECONDS = 30


class TaskLeaseLost(RuntimeError):
    pass


async def database_now(session):
    value = await session.scalar(select(func.current_timestamp()))
    return task_schedule.utc(value)


def binding(task, run):
    payload = tasks.normalize_input(run.input_snapshot or {})
    # 自由文本不是执行输入，也不应进入 checkpoint；仅摘要绑定原始快照。
    identity = [task.tenant_id, task.id, run.id, task.owner_id, run.schedule_revision,
        task.schedule_revision, task_schedule.utc(run.scheduled_for).isoformat() if run.scheduled_for else None,
        run.input_snapshot, AGENT_GRAPH_VERSION, 1]
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    execution = ({k: payload[k] for k in ('handlerType', 'prompt', 'maxSteps', 'appId', 'agentId') if k in payload}
                 if payload['handlerType'] == 'agent' else
                 {'handlerType': payload['handlerType'], 'knowledge_base_id': payload['knowledge_base_id']})
    return digest, execution



@dataclass(frozen=True)
class InvocationBinding:
    thread_id: str
    run_id: int
    digest: str
    worker_token: str
    fencing_version: int
    checkpoint_id: str
    sdk_task_id: str
    sdk_checkpoint_ns: str
    active_call_json: str


@dataclass(frozen=True)
class CompletionReceipt:
    invocation: InvocationBinding
    result_json: str


def _completion_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


def _completion_branches(checkpoint):
    values = checkpoint['channel_values']
    if any(k in values for k in ('__start__', '__pregel_tasks')):
        return None
    return {k for k in values if k.startswith('branch:to:')}


def _completion_snapshot_matches(receipt, *, thread_id, run_id, digest,
                                 worker_token, fencing_version,
                                 parent_id, base, base_metadata,
                                 checkpoint, metadata):
    # 只验证固定单工具图的成功快照；不授予取消/整图清理证明。
    if receipt is None:
        return False
    try:
        invocation = receipt.invocation
        if (invocation.thread_id, invocation.run_id, invocation.digest,
                invocation.worker_token, invocation.fencing_version) != (
                thread_id, run_id, digest, worker_token, fencing_version):
            return False
        if not invocation.sdk_task_id or invocation.sdk_checkpoint_ns != (
                'execute_tool:' + invocation.sdk_task_id):
            return False
        if parent_id != invocation.checkpoint_id or base['id'] != parent_id:
            return False
        if checkpoint['id'] == parent_id or base['v'] != 4 or checkpoint['v'] != 4:
            return False
        if metadata.get('source') != 'loop' or (
                type(base_metadata.get('step')) is not int
                or type(metadata.get('step')) is not int
                or metadata['step'] != base_metadata['step'] + 1):
            return False
        execute_branch = 'branch:to:execute_tool'
        record_branch = 'branch:to:record_tool_result'
        if _completion_branches(base) != {execute_branch}:
            return False
        if _completion_branches(checkpoint) != {record_branch}:
            return False
        if record_branch not in (checkpoint.get('updated_channels') or ()):
            return False
        old, new = base['channel_values'], checkpoint['channel_values']
        if (old.get('current_node'), new.get('current_node')) != (
                'validate_tool', 'execute_tool'):
            return False
        for values in (old, new):
            if values.get('provider_outcome') != 'known' or values.get('effect_status') != 'none':
                return False
        call = json.loads(invocation.active_call_json)
        if (old.get('active_call') != call or new.get('active_call') != call
                or call.get('name') != 'knowledge-check' or call.get('call_id') != 'handler'):
            return False
        result = new['active_result']
        if (_completion_json(result) != receipt.result_json
                or result.get('status') != 'completed'
                or result.get('effectStatus') != 'none'
                or result.get('callId') != call['call_id']
                or result.get('toolName') != call['name']):
            return False
        if old.get('active_result') != {} or old.get('tool_call_count') != 0:
            return False
        for key in ('graph_version', 'agent_run_id', 'user_message',
                    'tool_call_count', 'completed_calls', 'tool_results',
                    'tool_errors', 'tool_calls', 'pending_calls'):
            if key not in old or new.get(key) != old[key]:
                return False
        if old['agent_run_id'] != thread_id:
            return False
        old_versions = base['channel_versions']
        new_versions = checkpoint['channel_versions']
        consumed = checkpoint['versions_seen'].get('execute_tool', {})
        source_version = old_versions[execute_branch]
        target_version = new_versions[record_branch]
        target_seen = checkpoint['versions_seen'].get('record_tool_result', {}).get(record_branch, 0)
        if not all(type(v) is int for v in (source_version, target_version, target_seen)):
            return False
        if consumed.get(execute_branch) != source_version or target_version <= target_seen:
            return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class SessionClosure:
    invocation: InvocationBinding
    witness: object
    task: object


def _unsafe_outcome(value):
    if isinstance(value, dict):
        if (value.get('effect_status') == 'unknown' or value.get('effectStatus') == 'unknown'
                or value.get('provider_outcome') == 'uncertain'):
            return True
        return any(_unsafe_outcome(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_unsafe_outcome(v) for v in value)
    return False


class TaskRuntime:
    def __init__(self, sessions, task_id, tenant_id, run_id, consumer=None):
        self.sessions, self.task_id, self.tenant_id, self.run_id = sessions, task_id, tenant_id, run_id
        self.token, self.version, self.digest = uuid.uuid4().hex, 0, ''
        self.consumer = consumer
        self._invocation = self._completion = self._session_closure = None
        self._session_witness = self._supervision = None
        self._base_identity = None

    async def current(self, session):
        task = await session.get(ScheduledTask, self.task_id, populate_existing=True)
        run = await session.get(TaskRun, self.run_id, populate_existing=True)
        if (task is None or run is None or task.tenant_id != self.tenant_id
                or run.tenant_id != self.tenant_id or run.task_id != task.id):
            raise ValueError('task_identity_missing')
        return task, run

    async def authorize(self, session):
        task, run = await self.current(session)
        if (not get_settings().task_execution_enabled or not task.enabled or task.deleted_at is not None
                or task.schedule_revision != run.schedule_revision or run.status != 'running'
                or not await tasks.owner_authorized(session, task)):
            raise TaskLeaseLost('task_permission_schedule_or_run_revoked')
        digest, payload = binding(task, run)
        if digest != self.digest:
            raise TaskLeaseLost('task_binding_changed')
        return task, run

    async def fence(self, session):
        # 与旧入口保持 allocator -> lease 的写锁顺序；授权读取在写锁后刷新。
        await new_id(session)
        now = await database_now(session)
        await self.fence_consumer(session, now)
        changed = await session.execute(update(TaskGraphLease).where(
            TaskGraphLease.run_id == self.run_id, TaskGraphLease.worker_token == self.token,
            TaskGraphLease.fencing_version == self.version, TaskGraphLease.binding_digest == self.digest,
            TaskGraphLease.state.in_(('active', 'inflight')), TaskGraphLease.expires_at > now,
        ).values(expires_at=now + timedelta(seconds=LEASE_SECONDS)))
        if changed.rowcount != 1:
            raise TaskLeaseLost('task_worker_fence_lost')
        return await self.authorize(session)

    async def fence_consumer(self, session, now):
        if self.consumer:
            event_id, token = self.consumer
            event = await session.get(OutboxEvent, event_id, populate_existing=True)
            if (event is None or event.kind != 'scheduled-task' or event.entity_id != self.run_id
                    or event.tenant_id != self.tenant_id or event.consume_status != 'running'
                    or event.consume_token != token or event.consume_until is None
                    or task_schedule.utc(event.consume_until) <= now):
                raise TaskLeaseLost('task_consumer_fence_lost')

    async def guard(self):
        async with self.sessions() as session:
            await self.fence(session)
            await session.commit()

    async def begin_agent_dispatch(self):
        """覆盖整次模型/工具循环；图状态永远无权解除在途标记。"""
        async with self.sessions() as session:
            task, _ = await self.fence(session)
            await tasks.check_input(session, self.tenant_id, task.owner_id, self.payload)
            lease = await session.get(TaskGraphLease, self.run_id, populate_existing=True)
            lease.state = 'inflight'
            await session.commit()

    async def finish_agent(self, execution=None):
        """只接受当前调用栈正常返回的结果；无恢复/重试资格签发。"""
        from eic.ai.checkpoint_security import sanitize_tool_result
        async with self.sessions() as session:
            task, run = await self.fence(session)
            lease = await session.get(TaskGraphLease, self.run_id, populate_existing=True)
            known = (execution is not None and not _unsafe_outcome(execution)
                     and execution.get('provider_outcome') == 'known')
            calls = execution.get('total_tool_call_count') if isinstance(execution, dict) else None
            calls = calls if type(calls) is int and 0 <= calls <= 24 else None
            top_calls = execution.get('tool_call_count') if isinstance(execution, dict) else None
            top_calls = top_calls if type(top_calls) is int and 0 <= top_calls <= 24 else None
            outcome = ('success' if execution.get('stop_reason') == 'answer' else 'failed') if known else 'uncertain'
            result = {'schemaVersion': 1, 'handlerType': 'agent', 'status': outcome,
                      'answer': execution.get('answer', '') if known else '',
                      'stopReason': execution.get('stop_reason', '') if known else 'task_agent_outcome_unknown',
                      'toolCallCount': calls,
                      'topLevelToolCallCount': top_calls,
                      'graphVersion': AGENT_GRAPH_VERSION, 'safeToRetry': False}
            run.status, run.result = outcome, sanitize_tool_result(result)
            run.error_message = '' if outcome == 'success' else result['stopReason']
            run.finished_at = await database_now(session)
            task.status, task.last_run_at = outcome, run.finished_at
            lease.state = 'finished' if known else 'quarantined'
            await session.commit()
            return {'status': outcome, 'safeToRetry': False, 'task_id': self.task_id, 'run_id': self.run_id}

    async def claim(self):
        async with self.sessions() as session:
            await new_id(session)
            await self.fence_consumer(session, await database_now(session))
            task, run = await self.current(session)
            if run.status in tasks.TERMINAL:
                return run.status
            if (not get_settings().task_execution_enabled or not task.enabled or task.deleted_at is not None
                    or task.schedule_revision != run.schedule_revision):
                await tasks.cancel_runs(session, task, run_id=run.id, reason='task_disabled_or_schedule_replaced')
                await session.commit()
                return 'cancelled'
            if not await tasks.owner_authorized(session, task):
                run.status, run.error_message, run.finished_at = 'failed', 'permission_revoked', await database_now(session)
                await session.commit()
                return 'failed'
            self.digest, self.payload = binding(task, run)
            now = await database_now(session)
            if run.scheduled_for and task_schedule.utc(run.scheduled_for) > now:
                return 'locked'
            if (task.task_type == 'once' and run.scheduled_for and run.attempt == 0
                    and now - task_schedule.utc(run.scheduled_for) >= timedelta(minutes=1)
                    and task.schedule_policy in {'hold', 'skip'}):
                await tasks.cancel_runs(session, task, run_id=run.id, reason='once_misfire')
                task.status = 'schedule_held' if task.schedule_policy == 'hold' else 'missed'
                await session.commit()
                return 'cancelled'
            lease = await session.get(TaskGraphLease, run.id)
            if lease is None:
                if run.status == 'running':
                    run.status, run.error_message = 'uncertain', 'legacy_running_without_recovery_proof'
                    await session.commit()
                    return 'uncertain'
                lease = TaskGraphLease(run_id=run.id, binding_digest=self.digest, worker_token=self.token,
                    fencing_version=1, expires_at=now + timedelta(seconds=LEASE_SECONDS), state='active')
                session.add(lease)
            else:
                if lease.binding_digest != self.digest:
                    run.status, run.error_message = 'uncertain', 'task_checkpoint_binding_mismatch'
                    lease.state = 'quarantined'
                    await session.commit()
                    return 'uncertain'
                if lease.state in {'active', 'inflight'} and task_schedule.utc(lease.expires_at) > now:
                    return 'already_claimed'
                decision = await self.recovery_gate_locked(session, lease, now)
                if decision == 'wait':
                    return 'already_claimed'
                if decision != 'known':
                    # 只有 direct 的首次过期终止认领加版本；scanner/重入均不加。
                    if lease.state != 'quarantined' and task_schedule.utc(lease.expires_at) <= now:
                        lease.worker_token, lease.fencing_version = self.token, lease.fencing_version + 1
                    self.version = lease.fencing_version
                    await self.quarantine_locked(session, lease, now)
                    await session.commit()
                    return 'uncertain'
                lease.worker_token, lease.fencing_version = self.token, lease.fencing_version + 1
                lease.expires_at, lease.state = now + timedelta(seconds=LEASE_SECONDS), 'active'
            self.version = lease.fencing_version
            run.status, run.attempt, run.started_at = 'running', run.attempt + 1, now
            await session.commit()
            return 'claimed'

    async def begin_inflight(self, info, request):
        """先提交在途栅栏，再允许进入 handler 的 Session。"""
        call = vars(request)
        thread = f'task:{self.tenant_id}:{self.run_id}'
        if (info is None or info.thread_id != thread
                or not isinstance(info.task_id, str) or not info.task_id
                or info.checkpoint_ns != 'execute_tool:' + info.task_id
                or not isinstance(info.checkpoint_id, str) or not info.checkpoint_id
                or request.call_id != 'handler' or request.name != 'knowledge-check'
                or request.arguments != self.payload or request.requires_approval
                or self._invocation is not None):
            raise ValueError('task_checkpoint_invocation_invalid')
        invocation = InvocationBinding(thread, self.run_id, self.digest, self.token, self.version,
            info.checkpoint_id, info.task_id, info.checkpoint_ns, _completion_json(call))
        async with self.sessions() as session:
            await self.fence(session)
            lease = await session.get(TaskGraphLease, self.run_id, populate_existing=True)
            saver = TaskGraphCheckpointer(self)
            row = await session.get(TaskGraphCheckpoint, (self.run_id, '', info.checkpoint_id))
            latest = await session.scalar(select(TaskGraphCheckpoint.checkpoint_id).where(
                TaskGraphCheckpoint.run_id == self.run_id,
                TaskGraphCheckpoint.namespace == '').order_by(TaskGraphCheckpoint.checkpoint_id.desc()).limit(1))
            if (lease.state != 'active' or row is None or latest != info.checkpoint_id
                    or not await self.recovery_checkpoint_known(session, lease)):
                raise ValueError('task_checkpoint_invocation_base_invalid')
            data = saver.decode(row)
            checkpoint, metadata = data['checkpoint'], data['metadata']
            values = checkpoint['channel_values']
            if (checkpoint.get('v') != 4 or type(metadata.get('step')) is not int
                    or _completion_branches(checkpoint) != {'branch:to:execute_tool'}
                    or values.get('current_node') != 'validate_tool'
                    or values.get('active_call') != call or values.get('active_result') != {}
                    or values.get('provider_outcome') != 'known' or values.get('effect_status') != 'none'
                    or values.get('agent_run_id') != thread or values.get('graph_version') != AGENT_GRAPH_VERSION
                    or values.get('tool_call_count') != 0 or _unsafe_outcome(values)):
                raise ValueError('task_checkpoint_invocation_state_invalid')
            base_identity = (row.checkpoint_id, row.worker_token, row.fencing_version)
            lease.state = 'inflight'
            await session.commit()
        # 不确定的 commit 不走到这里，不执行 handler。
        self._invocation = invocation
        # 合法 retry 可恢复前一 ownership 的基础快照；其密文与全历史已在锁内校验。
        self._base_identity = base_identity

    def bind_session(self, witness, supervision):
        if (self._invocation is None or self._session_witness is not None
                or asyncio.current_task() is not supervision.tool_task or supervision.rejected):
            raise ValueError('task_checkpoint_session_binding_invalid')
        self._session_witness, self._supervision = witness, supervision

    def session_closed(self, witness, supervision):
        task = asyncio.current_task()
        if (witness is not self._session_witness or supervision is not self._supervision
                or task is not supervision.tool_task or supervision.rejected
                or task.cancel_requests or task.uncancel_requests or supervision.issued is not None):
            raise ValueError('task_checkpoint_session_closure_invalid')
        self._session_closure = SessionClosure(self._invocation, witness, task)

    def issue_completion(self, result):
        closure = self._session_closure
        if (self._invocation is None or self._completion is not None or closure is None
                or closure.invocation is not self._invocation
                or closure.witness is not self._session_witness
                or closure.task is not asyncio.current_task() or self._supervision.rejected
                or closure.task.cancel_requests or closure.task.uncancel_requests
                or result.get('callId') != 'handler' or result.get('toolName') != 'knowledge-check'
                or result.get('status') != 'completed' or result.get('effectStatus') != 'none'
                or _unsafe_outcome(result)):
            raise ValueError('task_checkpoint_completion_invalid')
        self._completion = CompletionReceipt(self._invocation, _completion_json(result))

    def _prehandler_denial_reason(self, supervision, error):
        """只保留服务拒绝诊断；不签发关闭、完成或重试凭据。"""
        from eic.ai.shared_executor import (
            ReadOnlyCancellationSupervisor, _AuditedTask,
            _require_worker_callback_boundary,
        )

        if (type(error) is not str or error not in {
                'shared_checkpoint_worker_graph_config_not_supported',
                'shared_checkpoint_ambient_callbacks_not_supported',
                'shared_checkpoint_callback_chain_not_supported',
            } or type(supervision) is not ReadOnlyCancellationSupervisor):
            return None
        if (any(item is not None for item in (
                self._invocation, self._completion, self._session_closure,
                self._session_witness, self._supervision,
                supervision.graph_task, supervision.tool_task, supervision.handler_task,
                supervision.request, supervision.session_receipt, supervision.exit_receipt,
                supervision.issued))
                or supervision.rejected is not True
                or supervision.consumer is not asyncio.current_task()
                or asyncio.current_task().cancelling()):
            return None
        owner = supervision.owner
        if (type(owner) is not _AuditedTask or owner.supervision is not supervision
                or owner not in supervision.tasks or not owner.done() or owner.cancelled()
                or owner.cancel_requests or owner.uncancel_requests):
            return None
        terminal = owner.exception()
        if type(terminal) is not ValueError or terminal.args != (error,):
            return None
        # 最内层真实抛出位置必须是固定服务边界，不能靠同名业务异常认领。
        trace = terminal.__traceback__
        if trace is None:
            return None
        while trace.tb_next is not None:
            trace = trace.tb_next
        if trace.tb_frame.f_code is not _require_worker_callback_boundary.__code__:
            return None
        return error

    async def finish(self, outcome, result, error='', *, supervision=None, interruption=None):
        async with self.sessions() as session:
            task, run = await self.fence(session)
            lease = await session.get(TaskGraphLease, run.id, populate_existing=True)
            # 真实 r3 closure，不把成功 receipt 或 effect=none 当取消清理证明。
            closed = (supervision is not None and supervision is self._supervision
                and supervision.closed_receipt is not None
                and supervision.closed_receipt == frozenset(supervision.tasks)
                and supervision.owner is not None and supervision.owner.done()
                and not asyncio.current_task().cancelling())
            safe_error = (closed and interruption is not None
                and supervision.retry_permitted(interruption)
                and (interruption is supervision.accepted_error
                    and supervision.consumed_receipt is supervision.exit_receipt
                    and supervision.exit_receipt is not None
                    and supervision.exit_receipt.session is self._session_witness
                    or interruption is supervision.normal_error
                    and self._session_closure is not None
                    and self._session_closure.witness is self._session_witness))
            normal_closed = (closed and supervision.chain_clean()
                and not supervision.owner.cancel_requests and not supervision.owner.uncancel_requests
                and supervision.issued is None)
            if safe_error and not await self._checkpoint_history_known(session, lease):
                safe_error = False
            if (outcome == 'uncertain' or _unsafe_outcome(result)
                    or outcome == 'retrying' and not safe_error
                    or lease.state == 'inflight' and not (outcome in {'retrying', 'failed'} and safe_error)
                    or self._invocation is not None and not (safe_error or normal_closed)):
                denial = self._prehandler_denial_reason(supervision, error) if outcome == 'uncertain' else None
                outcome, result, error = 'uncertain', {}, denial or 'task_checkpoint_outcome_unknown'
                # 未证明清理：仍留 inflight；确认任务全退出后才 quarantine。
                lease.state = 'quarantined' if closed else 'inflight'
            else:
                lease.state = 'released' if outcome == 'retrying' else 'finished'
            run.status, run.result, run.error_message = outcome, result, error
            run.finished_at = await database_now(session) if outcome != 'retrying' else None
            task.status, task.last_run_at = outcome, await database_now(session)
            await session.commit()
            return outcome

    async def quarantine_locked(self, session, lease, timestamp):
        """调用方已持 allocator；不提交、不改 ownership/version。"""
        task, run = await self.current(session)
        if lease is not None:
            if lease.state in {'active', 'inflight'} and task_schedule.utc(lease.expires_at) > timestamp:
                return False
            lease.state = 'quarantined'
        if run.status not in tasks.TERMINAL:
            run.status, run.error_message, run.finished_at = (
                'uncertain', 'task_checkpoint_outcome_unknown', timestamp)
            task.status = 'uncertain'
        return True

    async def recovery_gate_locked(self, session, lease, timestamp):
        if lease is None or lease.run_id != self.run_id:
            return 'quarantine'
        if lease.state == 'quarantined':
            return 'quarantine'
        if lease.state == 'inflight':
            return 'wait' if task_schedule.utc(lease.expires_at) > timestamp else 'quarantine'
        if lease.state not in {'active', 'released'}:
            return 'quarantine'
        if await self.recovery_checkpoint_known(session, lease):
            return 'known'
        # 不明内容不能抢写仍有效的另一 owner；known active 仍可按原规则 stage。
        return 'wait' if task_schedule.utc(lease.expires_at) > timestamp else 'quarantine'

    async def recovery_checkpoint_known(self, session, lease):
        """只读全历史检查；绝不改本 runtime 的 token/version/digest。"""
        if lease is None or lease.state not in {'active', 'released'}:
            return False
        return await self._checkpoint_history_known(session, lease)

    async def _checkpoint_history_known(self, session, lease):
        try:
            task, run = await self.current(session)
            digest, _ = binding(task, run)
            if lease is None or lease.binding_digest != digest:
                return False
            rows = list(await session.scalars(select(TaskGraphCheckpoint).where(
                TaskGraphCheckpoint.run_id == self.run_id)))
            writes = list(await session.scalars(select(TaskGraphWrite).where(
                TaskGraphWrite.run_id == self.run_id)))
            bases = {row.checkpoint_id: row for row in rows}
            saver = TaskGraphCheckpointer(self)
            for row in rows:
                if row.namespace != '' or row.schedule_revision != run.schedule_revision:
                    return False
                data = saver.decode(row, digest=digest)
                if _unsafe_outcome(data):
                    return False
            for write in writes:
                if write.checkpoint_id not in bases:
                    return False
                envelope = saver.serde.loads_typed((write.payload_type,
                    _fernet().decrypt(write.payload_encrypted.encode('ascii'))))
                if envelope['binding'] != [digest, write.checkpoint_id, write.task_id,
                        write.write_index, write.channel, write.worker_token, write.fencing_version]:
                    return False
                if _unsafe_outcome({write.channel: envelope['value']}):
                    return False
            return True
        except Exception:
            return False


class TaskGraphCheckpointer(BaseCheckpointSaver):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def config(self, checkpoint_id=None):
        return {'configurable': {'thread_id': f'task:{self.runtime.tenant_id}:{self.runtime.run_id}',
            'checkpoint_ns': '', **({'checkpoint_id': checkpoint_id} if checkpoint_id else {})}}

    def check_config(self, config):
        c = config['configurable']
        if c['thread_id'] != self.config()['configurable']['thread_id'] or c.get('checkpoint_ns', ''):
            raise ValueError('task_checkpoint_thread_mismatch')

    def encode(self, key, value):
        if getattr(self.runtime, 'payload', {}).get('handlerType') == 'agent':
            from eic.ai.checkpoint_security import sanitize_checkpoint_payload
            value = sanitize_checkpoint_payload(value)
        kind, raw = self.serde.dumps_typed({'binding': [self.runtime.digest, *key], 'value': value})
        return kind, _fernet().encrypt(raw).decode('ascii')

    def decode(self, row, *, digest=None):
        digest = self.runtime.digest if digest is None else digest
        if (row.tenant_id != self.runtime.tenant_id or row.task_id != self.runtime.task_id
                or row.graph_version != AGENT_GRAPH_VERSION or row.schema_version != 1):
            raise ValueError('task_checkpoint_identity_or_version_mismatch')
        value = self.serde.loads_typed((row.payload_type, _fernet().decrypt(row.payload_encrypted.encode('ascii'))))
        if value['binding'] != [digest, row.checkpoint_id, row.worker_token, row.fencing_version]:
            raise ValueError('task_checkpoint_binding_mismatch')
        return value['value']

    async def aget_tuple(self, config):
        self.check_config(config)
        async with self.runtime.sessions() as session:
            await self.runtime.fence(session)
            query = select(TaskGraphCheckpoint).where(TaskGraphCheckpoint.run_id == self.runtime.run_id)
            if config['configurable'].get('checkpoint_id'):
                query = query.where(TaskGraphCheckpoint.checkpoint_id == config['configurable']['checkpoint_id'])
            row = await session.scalar(query.order_by(TaskGraphCheckpoint.checkpoint_id.desc()).limit(1))
            if row is None:
                await session.commit()
                return None
            data = self.decode(row)
            values = data['checkpoint'].get('channel_values', {})
            pending = list(await session.scalars(select(TaskGraphWrite).where(
                TaskGraphWrite.run_id == self.runtime.run_id, TaskGraphWrite.checkpoint_id == row.checkpoint_id
            ).order_by(TaskGraphWrite.task_id, TaskGraphWrite.write_index)))
            writes = []
            for w in pending:
                envelope = self.serde.loads_typed((w.payload_type, _fernet().decrypt(w.payload_encrypted.encode('ascii'))))
                if envelope['binding'] != [self.runtime.digest, row.checkpoint_id, w.task_id, w.write_index,
                        w.channel, w.worker_token, w.fencing_version]:
                    raise ValueError('task_checkpoint_write_binding_mismatch')
                writes.append((w.task_id, w.channel, envelope['value']))
            if (_unsafe_outcome(values)
                    or any(_unsafe_outcome({ch: v}) for _, ch, v in writes)):
                raise ValueError('task_checkpoint_outcome_unknown')
            await session.commit()
            return CheckpointTuple(config=self.config(row.checkpoint_id), checkpoint=data['checkpoint'],
                metadata=data['metadata'], parent_config=self.config(row.parent_id) if row.parent_id else None,
                pending_writes=writes)

    async def aput(self, config, checkpoint, metadata, new_versions):
        self.check_config(config)
        runtime = self.runtime
        key = [checkpoint['id'], runtime.token, runtime.version]
        payload = {'checkpoint': checkpoint, 'metadata': metadata, 'writes': []}
        kind, encrypted = self.encode(key, payload)
        # 核验将要写入的真实密文字节，而非跨 await 仍可变的调用方 dict。
        payload = self.serde.loads_typed((kind, _fernet().decrypt(encrypted.encode('ascii'))))['value']
        checkpoint, metadata = payload['checkpoint'], payload['metadata']
        can_complete = False
        async with runtime.sessions() as session:
            _, run = await runtime.fence(session)
            lease = await session.get(TaskGraphLease, runtime.run_id, populate_existing=True)
            parent_id = config['configurable'].get('checkpoint_id')
            row = await session.get(TaskGraphCheckpoint, (runtime.run_id, '', key[0]))
            if row is not None:
                if (row.parent_id != parent_id or row.schedule_revision != run.schedule_revision
                        or row.worker_token != runtime.token or row.fencing_version != runtime.version
                        or self.decode(row) != payload):
                    raise ValueError('task_checkpoint_existing_row_conflict')
                # 幂等 row 不据传入内容再次解封。
            else:
                # 在 add/flush 之前读 latest，避免把自身误认为更晚的提交。
                latest = await session.scalar(select(TaskGraphCheckpoint.checkpoint_id).where(
                    TaskGraphCheckpoint.run_id == runtime.run_id,
                    TaskGraphCheckpoint.namespace == '').order_by(TaskGraphCheckpoint.checkpoint_id.desc()).limit(1))
                if lease.state == 'inflight' and runtime._completion is not None and parent_id:
                    parent = await session.get(TaskGraphCheckpoint, (runtime.run_id, '', parent_id))
                    if (parent is not None and latest == parent_id
                            and (parent.checkpoint_id, parent.worker_token, parent.fencing_version)
                                == runtime._base_identity
                            and parent.schedule_revision == run.schedule_revision
                            and not _unsafe_outcome(payload)
                            and await runtime._checkpoint_history_known(session, lease)):
                        data = self.decode(parent)
                        can_complete = _completion_snapshot_matches(runtime._completion,
                            thread_id=self.config()['configurable']['thread_id'], run_id=runtime.run_id,
                            digest=runtime.digest, worker_token=runtime.token, fencing_version=runtime.version,
                            parent_id=parent_id, base=data['checkpoint'], base_metadata=data['metadata'],
                            checkpoint=checkpoint, metadata=metadata)
                session.add(TaskGraphCheckpoint(run_id=runtime.run_id, namespace='', checkpoint_id=key[0],
                    parent_id=parent_id, tenant_id=runtime.tenant_id,
                    task_id=runtime.task_id, schedule_revision=run.schedule_revision, worker_token=runtime.token,
                    fencing_version=runtime.version, graph_version=AGENT_GRAPH_VERSION, schema_version=1,
                    payload_type=kind, payload_encrypted=encrypted))
                if can_complete:
                    lease.state = 'active'
            # typed fixture / unknown 正常保存，但没有解封权限。
            await session.commit()
        if can_complete:
            runtime._completion = None
        return self.config(key[0])

    async def aput_writes(self, config, writes, task_id, task_path=''):
        self.check_config(config)
        async with self.runtime.sessions() as session:
            await self.runtime.fence(session)
            checkpoint_id = config['configurable']['checkpoint_id']
            for i, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, i)
                row = await session.get(TaskGraphWrite, (self.runtime.run_id, checkpoint_id, task_id, index))
                if row is not None and index >= 0:
                    continue
                key = [checkpoint_id, task_id, index, channel, self.runtime.token, self.runtime.version]
                kind, encrypted = self.encode(key, value)
                if row is None:
                    session.add(TaskGraphWrite(run_id=self.runtime.run_id, checkpoint_id=checkpoint_id,
                        task_id=task_id, write_index=index, channel=channel, worker_token=self.runtime.token,
                        fencing_version=self.runtime.version, payload_type=kind, payload_encrypted=encrypted))
                else:
                    row.channel, row.worker_token, row.fencing_version = channel, self.runtime.token, self.runtime.version
                    row.payload_type, row.payload_encrypted = kind, encrypted
            await session.commit()

    async def alist(self, config, *, filter=None, before=None, limit=None):
        item = await self.aget_tuple(config)
        if item is not None and limit != 0:
            yield item

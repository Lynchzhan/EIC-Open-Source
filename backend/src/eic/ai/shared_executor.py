"""三个业务入口共用的执行图；权限与事务只通过运行时服务边界执行。"""
from __future__ import annotations

import asyncio
import copy
from collections.abc import Coroutine
import inspect
import json
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langchain_core.runnables.config import ensure_config, get_async_callback_manager_for_config

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command, interrupt

from eic.ai.agent_graph import (
    AGENT_GRAPH_VERSION,
    AgentBudget,
    ModelDecision,
    ToolCallRequest,
    _trace_event,
    normalize_model_decision,
)
from eic.ai.checkpointer import DatabaseCheckpointer
from eic.ai.checkpoint_security import sanitize_checkpoint, sanitize_checkpoint_payload
from eic.ai.providers import ModelProviderError
from eic.ai.stream_reader import complete_cleanup
from eic.ai.tool_center import ToolExecutionError, _digest


class _DeadlineExceeded(TimeoutError):
    pass


_SUPERVISION = ContextVar('eic_cancellation_scope', default=None)


class _SupervisionFactory:
    """同一 loop 共用调度器；注册和注销之间不让出事件循环。"""

    def __init__(self):
        self.scopes = set()

    def __call__(self, loop, coroutine, context=None):
        active = context.get(_SUPERVISION) if context is not None else _SUPERVISION.get()
        inherited = _SUPERVISION.get()
        caller = getattr(asyncio.current_task(), 'supervision', None)
        scope = caller or inherited or active
        if scope is not None:
            if scope not in self.scopes or active is not scope or (
                    inherited is not None and inherited is not scope):
                scope.reject()
                coroutine.close()
                raise ValueError('shared_checkpoint_task_scope_mismatch')
            return _AuditedTask(coroutine, loop=loop, context=context, supervision=scope)
        return asyncio.Task(coroutine, loop=loop, context=context)


class _DeliveryCoroutine(Coroutine):
    """由原 Task 驱动；先记录真实投递对象，再进入业务协程。"""

    def __init__(self, coroutine, task):
        self.coroutine, self.task = coroutine, task

    def __await__(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        return self.send(None)

    def send(self, value):
        return self.coroutine.send(value)

    def throw(self, *args):
        # 不归一化或重建异常；一至三个位置参数均原样转交。
        # 只有 asyncio 的单异常实例调用形状可签发投递凭据。
        scope = self.task.supervision
        if len(args) == 1 and isinstance(args[0], BaseException):
            error = args[0]
            self.task.deliveries.append(error)
            if isinstance(error, asyncio.CancelledError):
                scope.delivered(self.task, error)
        else:
            scope.reject()
        return self.coroutine.throw(*args)

    def close(self):
        self.task.supervision.reject()
        return self.coroutine.close()


class _AuditedTask(asyncio.Task):
    """审计请求历史与真实 throw，不能用最终 cancelling 数替代历史。"""

    def __init__(self, coroutine, *, name=None, loop=None, context=None, supervision=None):
        self.supervision = supervision or _SUPERVISION.get()
        self.cancel_requests, self.uncancel_requests, self.deliveries = [], [], []
        super().__init__(_DeliveryCoroutine(coroutine, self),
                         loop=loop or asyncio.get_running_loop(), name=name, context=context)
        self.supervision.tasks.add(self)

    def cancel(self, msg=None):
        scope = self.supervision
        authorization = scope.issuing if self is scope.tool_task else None
        self.cancel_requests.append((asyncio.current_task(), msg, authorization))
        # 唯一原生入口 cancel 的归属在 timeout 退出后联合验证。
        if self is not scope.owner and authorization is not scope.issued:
            scope.reject()
        elif self is not scope.owner and authorization is None:
            scope.reject()
        return super().cancel(msg)

    def uncancel(self):
        scope = self.supervision
        self.uncancel_requests.append((asyncio.current_task(), scope.native_exit))
        if self is not scope.owner or scope.native_exit is not scope.entry_timeout:
            scope.reject()
        return super().uncancel()


class _EntryTimeout:
    """保留 asyncio.timeout；只围观其原生退出调用，不替换转换逻辑。"""

    def __init__(self, scope, seconds):
        self.scope = scope
        self.native = asyncio.timeout(seconds)
        scope.entry_timeout = self

    def expired(self):
        return self.native.expired()

    async def __aenter__(self):
        await self.native.__aenter__()
        return self

    async def __aexit__(self, kind, error, traceback):
        self.scope.native_exit = self
        try:
            return await self.native.__aexit__(kind, error, traceback)
        finally:
            self.scope.native_exit = None


class _HandlerBudget:
    """原始数值、原始 loop.time 截止时间；取消权由一个监督者协调。"""

    def __init__(self, scope, seconds):
        self.scope = scope
        self.when = asyncio.get_running_loop().time() + seconds
        self.handle = None

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        if self.when <= loop.time():
            self.handle = loop.call_soon(self.scope.cancel_handler, 'handler', self)
        else:
            self.handle = loop.call_at(self.when, self.scope.cancel_handler, 'handler', self)
        return self

    async def __aexit__(self, kind, error, traceback):
        self.handle.cancel()
        return False


class _SupervisedToolExit(GraphBubbleUp):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__('read_only_owned_cancellation')


@dataclass(frozen=True)
class _HandlerExitReceipt:
    task: Any
    request: Any
    cancellation: Any
    session: Any


class _SessionWitness:
    """直接观察 Session 的退出返回值；被抑制或替换的异常不能出具凭据。"""

    def __init__(self, supervisor, manager):
        self.supervisor, self.manager = supervisor, manager

    async def __aenter__(self):
        try:
            return await self.manager.__aenter__()
        except BaseException:
            self.supervisor.reject()
            raise

    async def __aexit__(self, kind, error, traceback):
        try:
            suppressed = await self.manager.__aexit__(kind, error, traceback)
        except BaseException:
            self.supervisor.reject()
            raise
        if error is not None and suppressed:
            self.supervisor.reject()
            raise ValueError('shared_checkpoint_session_suppressed_exception')
        if error is not None and error is self.supervisor.handler_cancellation:
            self.supervisor.session_receipt = (self, error)
        return suppressed


class _ObservedCheckpointer(BaseCheckpointSaver):
    """只记录调用边界，不改变序列化、配置、checkpoint 或恢复策略。"""

    def __init__(self, delegate, supervisor):
        super().__init__(serde=delegate.serde)
        self.delegate, self.supervisor = delegate, supervisor

    @property
    def config_specs(self):
        try:
            return self.delegate.config_specs
        except BaseException as exc:
            self.supervisor.checkpoint_errors.append(('config_specs', exc))
            self.supervisor.reject()
            raise

    def get_next_version(self, current, channel):
        try:
            return self.delegate.get_next_version(current, channel)
        except BaseException as exc:
            self.supervisor.checkpoint_errors.append(('get_next_version', exc))
            self.supervisor.reject()
            raise

    def _contains_proof(self, value, seen):
        if id(value) in seen:
            return False
        seen.add(id(value))
        if (value is self.supervisor
                or self.supervisor.issued is not None and value is self.supervisor.issued
                or isinstance(value, (_SupervisedToolExit, _HandlerExitReceipt, _SessionWitness, asyncio.Task))):
            return True
        if isinstance(value, BaseException):
            return any(self._contains_proof(v, seen)
                       for v in (value.args, value.__cause__, value.__context__))
        if isinstance(value, dict):
            return any(self._contains_proof(v, seen) for item in value.items() for v in item)
        if isinstance(value, (list, tuple)):
            return any(self._contains_proof(v, seen) for v in value)
        return False

    async def _observe(self, method, *args, **kwargs):
        marker = object()
        self.supervisor.checkpoint_pending.add(marker)
        try:
            if self._contains_proof((args, kwargs), set()):
                raise ValueError('shared_checkpoint_ephemeral_proof_not_serializable')
            return await getattr(self.delegate, method)(*args, **kwargs)
        except BaseException as exc:
            self.supervisor.checkpoint_errors.append((method, exc))
            self.supervisor.reject()
            raise
        finally:
            self.supervisor.checkpoint_pending.discard(marker)

    async def aget_tuple(self, config):
        return await self._observe('aget_tuple', config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._observe('aput', config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=''):
        return await self._observe('aput_writes', config, writes, task_id, task_path)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        marker = object()
        self.supervisor.checkpoint_pending.add(marker)
        try:
            async for item in self.delegate.alist(config, filter=filter, before=before, limit=limit):
                yield item
        except BaseException as exc:
            self.supervisor.checkpoint_errors.append(('alist', exc))
            self.supervisor.reject()
            raise
        finally:
            self.supervisor.checkpoint_pending.discard(marker)


class ReadOnlyCancellationSupervisor:
    """每次 execute_shared 独占，凭据不进入 State、checkpoint 或返回 JSON。"""

    def __init__(self):
        self.owner = self.graph_task = self.tool_task = None
        self.request = self.request_binding = None
        self.handler_task = self.handler_cancellation = None
        self.session_receipt = self.exit_receipt = self.node_exit = None
        self.graph_exit_receipt = self.accepted_error = self.normal_error = None
        self.node_receipt = self.consumed_receipt = None
        self.handler_timeout = self.deadline_handoff = None
        self.issued = None
        self.checkpoint_pending = set()
        self.checkpoint_errors = []
        self.rejected = False
        self.tasks = set()
        self.issuing = self.native_exit = self.entry_timeout = None
        self.delivery = None
        self.cleanup_blocked = None
        self.factory = None
        self.consumer = None
        self.closed_receipt = None

    def reject(self):
        self.rejected = True
        self.accepted_error = None

    def handler_budget(self, seconds):
        return _HandlerBudget(self, seconds)

    def delivered(self, task, error):
        if task is self.owner:
            return
        if (task is not self.tool_task or self.handler_task is not task
                or self.issued is None or self.delivery is not None
                or error.args != (self.issued,)):
            self.reject()
            return
        self.delivery = (task, self.request, error)

    def chain_clean(self, expected=None):
        factory = asyncio.get_running_loop().get_task_factory()
        closed = (self.closed_receipt is not None
                  and self.closed_receipt == frozenset(self.tasks)
                  and self.owner.done())
        if self.rejected or (factory is not self.factory and not closed):
            return False
        for task in self.tasks:
            if task is self.owner:
                continue
            if not task.done():
                return False
            if task is not self.tool_task and (task.cancel_requests or task.uncancel_requests):
                return False
            if task.cancelled():
                return False
            error = task.exception()
            if error is not None and error is not expected:
                return False
        return not self.checkpoint_pending and not self.checkpoint_errors

    def native_entry_owned(self, error, cancellation):
        task = self.owner
        return (isinstance(task, _AuditedTask)
                and self.entry_timeout.expired()
                and error.__cause__ is cancellation
                and len(task.cancel_requests) == 1
                and task.cancel_requests[0] == (None, None, None)
                and len(task.uncancel_requests) == 1
                and task.uncancel_requests[0][0] is task
                and task.uncancel_requests[0][1] is self.entry_timeout
                and [e for e in task.deliveries if isinstance(e, asyncio.CancelledError)] == [cancellation]
                and task.cancelling() == 0)

    def session(self, manager):
        return _SessionWitness(self, manager)

    def bind(self):
        if self.owner is not None or asyncio.current_task().cancelling():
            raise ValueError('shared_checkpoint_supervisor_reused_or_cancelled')
        self.owner = asyncio.current_task()
        if not isinstance(self.owner, _AuditedTask):
            raise ValueError('shared_checkpoint_owner_not_audited')

    async def call_tool(self, services, request, remaining_ms):
        if self.tool_task is not None or self.rejected:
            raise ValueError('shared_checkpoint_supervisor_call_reused')
        self.request = request
        self.request_binding = (request.call_id, request.name, _digest(request.arguments))
        self.tool_task = _AuditedTask(services.execute_tool(request, remaining_ms),
                                      name='eic-owned-read-only-tool')
        return await asyncio.shield(self.tool_task)

    def handler_entered(self, request, timeout):
        if asyncio.current_task() is not self.tool_task or request is not self.request:
            self.reject()
            raise ValueError('shared_checkpoint_handler_task_mismatch')
        self.handler_task = asyncio.current_task()
        self.handler_timeout = timeout

    def handler_left(self):
        self.handler_task = None

    def observe_handler_cancel(self, request, exc):
        if (self.delivery is not None
                and self.delivery[0] is asyncio.current_task()
                and self.delivery[1] is request and self.delivery[2] is exc):
            self.handler_cancellation = exc
        else:
            self.reject()

    def cancel_handler(self, origin='entry', budget=None):
        # timer 与入口 continuation 调用同一状态机，合法 inner 先到不再二次取消。
        if self.issued is not None:
            if origin == 'handler' and budget is not self.handler_timeout:
                self.reject()
                return False
            return self._owned_tool_cancel()
        if (self.rejected or self.handler_task is not self.tool_task
                or self.tool_task is None or self.tool_task.done()
                or self.tool_task.cancel_requests or self.tool_task.uncancel_requests
                or self.handler_timeout is None
                or origin == 'handler' and budget is not self.handler_timeout
                or origin == 'entry' and asyncio.current_task() is not self.owner):
            self.reject()
            return False
        self.issued = object()
        self.deadline_handoff = (self.handler_timeout, self.issued, origin)
        self.issuing = self.issued
        try:
            if not self.tool_task.cancel(self.issued):
                self.reject()
                return False
        finally:
            self.issuing = None
        return True

    def _owned_tool_cancel(self):
        return (self.tool_task is not None and self.issued is not None
                and len(self.tool_task.cancel_requests) == 1
                and self.tool_task.cancel_requests[0][1] is self.issued
                and self.tool_task.cancel_requests[0][2] is self.issued
                and not self.tool_task.uncancel_requests
                and self.tool_task.cancelling() == 1
                and self.deadline_handoff is not None
                and self.deadline_handoff[0] is self.handler_timeout
                and self.deadline_handoff[1] is self.issued)

    def raise_tool_exit(self, request, exc):
        if (self.rejected or request is not self.request
                or asyncio.current_task() is not self.tool_task
                or exc is not self.handler_cancellation
                or len(exc.args) != 1 or exc.args[0] is not self.issued
                or self.session_receipt is None
                or self.session_receipt[1] is not exc or not self._owned_tool_cancel()):
            self.reject()
            raise exc
        self.exit_receipt = _HandlerExitReceipt(self.tool_task, request, exc, self.session_receipt[0])
        self.node_exit = _SupervisedToolExit(self.exit_receipt)
        raise self.node_exit from exc

    def reject_suppressed_cancel(self):
        if (self.issued is not None or self.tool_task.cancel_requests
                or self.tool_task.uncancel_requests):
            self.reject()
            raise ValueError('shared_checkpoint_handler_suppressed_cancellation')

    def stopped_node(self, state, exc):
        if (self.rejected or exc is not self.node_exit
                or exc.receipt is not self.exit_receipt
                or not self.tool_task.done() or self.tool_task.cancelled()
                or self.tool_task.exception() is not exc or not self._owned_tool_cancel()
                or state.get('effect_status') != 'none'
                or state.get('provider_outcome') != 'known'
                or self._binding(state) != self.request_binding):
            self.reject()
            raise ValueError('shared_checkpoint_owned_tool_proof_rejected') from exc
        # GraphBubbleUp 在固定 SDK 不被重试或写入 ERROR 通道；保留执行前游标。
        self.node_receipt = (exc, self.request_binding)
        raise exc

    @staticmethod
    def _binding(state):
        call = state.get('active_call') or {}
        return (call.get('call_id'), call.get('name'), _digest(call.get('arguments')))

    def consume(self, native_timeout, entry_cancel, state):
        if (not self.native_entry_owned(native_timeout, entry_cancel)
                or not self.chain_clean(self.node_exit)
                or self.rejected or self.consumed_receipt is not None
                or asyncio.current_task() is not self.owner
                or not isinstance(native_timeout, TimeoutError)
                or native_timeout.__cause__ is not entry_cancel
                or not isinstance(entry_cancel, asyncio.CancelledError)
                or self.owner.cancelling() != 0
                or self.graph_task is None or not self.graph_task.done()
                or self.graph_task.cancelled() or self.graph_task.exception() is not self.node_exit
                or self.graph_task.cancel_requests or self.graph_task.uncancel_requests
                or self.graph_exit_receipt is None
                or self.graph_exit_receipt[0] is not self.graph_task
                or self.graph_exit_receipt[1] is not self.node_exit
                or self.node_receipt is None or self.node_receipt[0] is not self.node_exit
                or self.checkpoint_pending or self.checkpoint_errors
                or self.exit_receipt is None or not self._owned_tool_cancel()
                or self.exit_receipt.task is not self.tool_task
                or self.exit_receipt.request is not self.request
                or self.exit_receipt.cancellation is not self.handler_cancellation
                or self.session_receipt is None
                or self.exit_receipt.session is not self.session_receipt[0]
                or self.tool_task.exception() is not self.node_exit
                or state.get('provider_outcome') != 'known'
                or state.get('effect_status') != 'unknown'
                or self._binding(state) != self.request_binding):
            self.reject()
            return None
        self.consumed_receipt = self.exit_receipt
        self.accepted_error = TimeoutError('read_only_handler_budget_exceeded')
        return self.accepted_error

    def retry_permitted(self, exc):
        if (asyncio.current_task() is not self.consumer
                or self.owner is None or not self.owner.done()
                or self.owner.cancelling()
                or not self.chain_clean(self.node_exit if exc is self.accepted_error else exc)):
            return False
        if exc is self.accepted_error:
            return self.consumed_receipt is not None
        # 普通错误只接受零取消。任何 cancel/uncancel 历史永久拒绝。
        return (exc is self.normal_error
                and not self.owner.cancel_requests and not self.owner.uncancel_requests
                and self.tool_task is not None and self.tool_task.done()
                and not self.tool_task.cancel_requests and not self.tool_task.uncancel_requests
                and self.tool_task.exception() is exc and self.issued is None)

    async def abort(self):
        self.reject()
        self.cleanup_blocked = 'shared_checkpoint_cleanup_not_confirmed'
        # 这是隔离性等待，不承诺有限返回；不把存活任务变成可重试终态。
        while True:
            tasks = [t for t in self.tasks
                     if t is not self.owner and t is not asyncio.current_task() and not t.done()]
            if not tasks:
                self.cleanup_blocked = None
                return
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _supervised_graph(user_message, *, run_id, services, graph, resume):
    supervision = services.cancellation_supervision
    try:
        result = await _execute_shared(user_message, run_id=run_id, services=services,
                                       graph=graph, resume=resume)
        if (supervision.rejected or supervision.issued is not None
                or supervision.checkpoint_errors or supervision.checkpoint_pending):
            supervision.reject()
            raise ValueError('shared_checkpoint_graph_suppressed_failure')
        return result
    except _SupervisedToolExit as exc:
        if exc is not supervision.node_exit or supervision.node_receipt is None:
            supervision.reject()
            raise ValueError('shared_checkpoint_graph_exception_replaced') from exc
        supervision.graph_exit_receipt = (asyncio.current_task(), exc)
        raise


def _require_worker_callback_boundary(graph, config, supervision):
    """专用 worker 配置边界；不模拟 LangGraph 合并，不删除 callback。"""
    if supervision is None:
        return
    graph_config = getattr(graph, 'config', None)
    # 只接受默认未配置图；包括 callbacks=[] 在内的任何配置均拒绝。
    if (graph_config is not None
            and (type(graph_config) is not dict or graph_config)):
        supervision.reject()
        raise ValueError('shared_checkpoint_worker_graph_config_not_supported')
    if (type(config) is not dict or set(config) != {'configurable', 'recursion_limit'}
            or type(config['configurable']) is not dict
            or set(config['configurable']) != {'thread_id'}
            or config['recursion_limit'] != 256):
        supervision.reject()
        raise ValueError('shared_checkpoint_worker_call_config_not_supported')
    # 无显式覆盖地读取 ambient；空 manager 对象也不在此专用边界内。
    ambient = ensure_config()
    callbacks = ambient.get('callbacks')
    if callbacks is not None and not (type(callbacks) is list and not callbacks):
        supervision.reject()
        raise ValueError('shared_checkpoint_ambient_callbacks_not_supported')
    # 调用配置没有 callbacks 键，不能覆盖 ambient 后谎称它为空。
    manager = get_async_callback_manager_for_config(ensure_config(config))
    if manager.handlers or manager.inheritable_handlers:
        supervision.reject()
        raise ValueError('shared_checkpoint_callback_chain_not_supported')


async def _run_supervised(user_message, *, run_id, services, graph, resume):
    scope = services.cancellation_supervision
    loop = asyncio.get_running_loop()
    # 冻结运行链仅为 3.11；自定义工厂不能被静默绕过。
    existing = loop.get_task_factory()
    if (sys.version_info[:2] != (3, 11)
            or existing is not None and type(existing) is not _SupervisionFactory
            or _SUPERVISION.get() is not None
            or scope.consumer is not None):
        scope.reject()
        raise ValueError('shared_checkpoint_task_factory_not_supported')
    scope.consumer = asyncio.current_task()

    factory = existing if existing is not None else _SupervisionFactory()
    factory.scopes.add(scope)
    scope.factory = factory
    loop.set_task_factory(factory)
    token = _SUPERVISION.set(scope)
    owner = None
    try:
        owner = _AuditedTask(_execute_supervised(user_message, run_id=run_id,
            services=services, graph=graph, resume=resume),
            name='eic-budget-owner', supervision=scope)
        try:
            return await asyncio.shield(owner)
        except asyncio.CancelledError:
            scope.reject()
            owner.cancel()
            await complete_cleanup(asyncio.gather(owner, return_exceptions=True))
            raise
    finally:
        cleanup_completed = False
        try:
            if owner is not None and not owner.done():
                scope.reject()
                owner.cancel()
                await complete_cleanup(asyncio.gather(owner, return_exceptions=True))
            if any(t is not owner and not t.done() for t in scope.tasks):
                await complete_cleanup(scope.abort())
            cleanup_completed = True
        except BaseException:
            scope.reject()
            raise
        finally:
            try:
                _SUPERVISION.reset(token)
            except BaseException:
                cleanup_completed = False
                scope.reject()
                raise
            finally:
                if loop.get_task_factory() is not factory:
                    scope.reject()
                    raise ValueError('shared_checkpoint_task_factory_replaced')
                if any(not t.done() for t in scope.tasks):
                    scope.reject()
                    scope.cleanup_blocked = 'shared_checkpoint_cleanup_not_confirmed'
                    # 活跃任务仍需原工厂监督；不伪造退出凭据或恢复完成。
                    raise ValueError('shared_checkpoint_cleanup_not_confirmed')
                try:
                    factory.scopes.remove(scope)
                    if not factory.scopes:
                        loop.set_task_factory(None)
                except BaseException:
                    scope.reject()
                    raise
                if cleanup_completed:
                    scope.closed_receipt = frozenset(scope.tasks)


async def _execute_supervised(user_message, *, run_id, services, graph, resume):
    supervision = services.cancellation_supervision
    supervision.bind()
    graph = copy.copy(graph or build_execution_graph(InMemorySaver()))
    if graph.checkpointer:
        graph.checkpointer = _ObservedCheckpointer(graph.checkpointer, supervision)
    timeout = _EntryTimeout(supervision, max(0, services.remaining()))
    entry_cancel = None
    try:
        async with timeout:
            _require_worker_callback_boundary(graph,
                {'configurable': {'thread_id': run_id}, 'recursion_limit': 256}, supervision)
            await services.check()
            supervision.graph_task = _AuditedTask(
                _supervised_graph(user_message, run_id=run_id, services=services, graph=graph, resume=resume),
                name='eic-owned-read-only-graph')
            try:
                try:
                    result = await asyncio.shield(supervision.graph_task)
                except _SupervisedToolExit as terminal:
                    if terminal is not supervision.node_exit:
                        supervision.reject()
                        raise
                    # 内预算先到：图已退出，仍由原入口 deadline 原生认领。
                    await asyncio.Future()
                else:
                    if (not supervision.chain_clean()
                            or supervision.owner.cancel_requests
                            or supervision.owner.uncancel_requests):
                        supervision.reject()
                        raise ValueError('shared_checkpoint_success_chain_unconfirmed')
                    return result
            except asyncio.CancelledError as exc:
                entry_cancel = exc
                # expired 只决定是否请求清理；不能授予归属或重试资格。
                if timeout.expired():
                    supervision.cancel_handler('entry')
                raise
    except TimeoutError as exc:
        if entry_cancel is not None and exc.__cause__ is entry_cancel:
            try:
                if supervision.rejected or supervision.issued is None:
                    raise ValueError('shared_checkpoint_no_owned_handler')
                # 沿用现有收尾窗口；总入口 timeout 和 handler 预算没有增加。
                async with asyncio.timeout(1):
                    try:
                        await asyncio.shield(supervision.graph_task)
                    except _SupervisedToolExit as terminal:
                        if terminal is not supervision.node_exit:
                            raise ValueError('shared_checkpoint_graph_exception_replaced') from terminal
                    else:
                        raise ValueError('shared_checkpoint_graph_suppressed_cancellation')
                proof = supervision.consume(exc, entry_cancel, services.result)
                if (proof is not None and services.known_read_only_interruption is not None
                        and services.known_read_only_interruption(supervision.request, proof)):
                    services.result = {**services.result, 'effect_status': 'none',
                                       'stop_reason': 'total_budget_exceeded'}
                    return services.result
                raise ValueError('shared_checkpoint_cancellation_proof_rejected')
            except BaseException:
                await complete_cleanup(supervision.abort())
                raise
        if not timeout.expired() and not isinstance(exc, _DeadlineExceeded):
            supervision.normal_error = exc
            raise
        await complete_cleanup(supervision.abort())
        services.result = {**services.result, 'stop_reason': 'total_budget_exceeded'}
        return services.result
    except BaseException as exc:
        if (not isinstance(exc, asyncio.CancelledError) and entry_cancel is None
                and supervision.graph_task is not None and supervision.graph_task.done()
                and not supervision.graph_task.cancelled()
                and supervision.graph_task.exception() is exc):
            supervision.normal_error = exc
        else:
            await complete_cleanup(supervision.abort())
        raise
    finally:
        if any(t is not supervision.owner and not t.done() for t in supervision.tasks):
            await complete_cleanup(supervision.abort())
        if supervision.rejected:
            services.result = {**services.result, 'effect_status': 'unknown',
                               'stop_reason': 'total_budget_exceeded'}
            # 仅在已确认所有受监督 Task 退出后写保守恢复标记。
            async def mark_uncertain():
                async with asyncio.timeout(1):
                    config = {'configurable': {'thread_id': run_id}, 'recursion_limit': 256}
                    _require_worker_callback_boundary(graph, config, supervision)
                    await graph.aupdate_state(config, {
                        'stop_reason': 'total_budget_exceeded',
                        'provider_outcome': services.result.get('provider_outcome', 'not_started'),
                        'effect_status': 'unknown'})
            await complete_cleanup(mark_uncertain())


class ExecutionState(TypedDict, total=False):
    user_message: str
    agent_run_id: str
    graph_version: str
    answer: str
    stop_reason: str
    current_node: str
    agent_step_count: int
    tool_call_count: int
    tool_calls: list[dict]
    tool_results: list[dict]
    tool_errors: list[dict]
    pending_calls: list[dict]
    active_call: dict
    active_result: dict
    completed_calls: dict[str, dict]
    trace_events: list[dict]
    approval_required: dict | None
    needs_approval: bool
    provider_outcome: str
    effect_status: str
    execution_context: dict


@dataclass
class ExecutionServices:
    # 这些对象不进入 Graph State、checkpoint 或模型上下文。
    model: Any
    execute_tool: Any
    validate_tool: Any
    guard: Any = None
    record_result: Any = None
    pause: Any = None
    validate_resume: Any = None
    event_sink: Any = None
    capture: Any = None
    restore: Any = None
    budget: AgentBudget = field(default_factory=AgentBudget)
    started: float = field(default_factory=time.perf_counter)
    model_available: bool = True
    result: dict = field(default_factory=dict)
    # 仅可信服务边界提供本次handler退出证明；不进入checkpoint。
    known_read_only_interruption: Any = None
    cancellation_supervision: Any = None

    async def emit(self, value):
        if self.event_sink is not None:
            await self.event_sink(value)

    async def check(self):
        if self.remaining() <= 0:
            raise _DeadlineExceeded()
        if self.guard is not None:
            await self.guard()
        if self.remaining() <= 0:
            raise _DeadlineExceeded()

    def remaining(self):
        return self.budget.max_total_ms / 1000 - (time.perf_counter() - self.started)


SERVICES: ContextVar[ExecutionServices] = ContextVar('eic_execution_services')


def _request(state):
    return ToolCallRequest(**state['active_call'])


def _event(state, node, **payload):
    return {'current_node': node, 'trace_events': [
        *state.get('trace_events', []), _trace_event(node, time.perf_counter(), payload),
    ]}


async def check_execution(state):
    services = SERVICES.get()
    await services.check()
    reason = ''
    if not services.model_available:
        reason = 'model_unavailable'
    elif state['agent_step_count'] >= services.budget.max_steps:
        reason = 'max_steps_exceeded'
    elif services.remaining() <= 0:
        reason = 'total_budget_exceeded'
    return {'stop_reason': reason, 'current_node': 'check_execution'}


async def model_decision(state):
    services = SERVICES.get()
    await services.check()
    services.result = {**state, 'provider_outcome': 'uncertain'}
    try:
        async with asyncio.timeout(max(0, services.remaining())):
            snapshot = copy.deepcopy(state)
            if inspect.iscoroutinefunction(services.model):
                value = await services.model(snapshot)
            else:
                value = await asyncio.to_thread(services.model, snapshot)
                if inspect.isawaitable(value):
                    value = await value
        decision = normalize_model_decision(value)
    except ModelProviderError:
        # A1 的 reason/retryable 原样交给服务层，绝不将未知结果重发。
        raise
    except TimeoutError:
        return {'stop_reason': 'total_budget_exceeded', 'provider_outcome': 'uncertain'}
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        decision = ModelDecision('invalid')
    update = _event(state, 'model_decision', kind=decision.kind)
    update.update(agent_step_count=state['agent_step_count'] + 1, provider_outcome='known')
    if decision.kind == 'answer':
        update.update(answer=decision.content, stop_reason='answer')
    elif decision.kind != 'tool_calls' or not decision.tool_calls:
        update['stop_reason'] = 'invalid_model_decision'
    elif state['tool_call_count'] >= services.budget.max_tool_calls:
        update['stop_reason'] = 'max_tool_calls_exceeded'
    else:
        update['pending_calls'] = [vars(call) for call in decision.tool_calls]
    if services.capture is not None:
        update['execution_context'] = services.capture()
    services.result = {**state, **update}
    return update


async def validate_tool(state):
    services = SERVICES.get()
    await services.check()
    call, *remaining = state['pending_calls']
    request = ToolCallRequest(**call)
    update = _event(state, 'validate_tool', callId=request.call_id, toolName=request.name)
    update.update(active_call=call, pending_calls=remaining, active_result={}, needs_approval=False)
    if state['tool_call_count'] >= services.budget.max_tool_calls:
        update['stop_reason'] = 'max_tool_calls_exceeded'
        return update
    if services.remaining() <= 0:
        update['stop_reason'] = 'total_budget_exceeded'
        return update
    validation = await services.validate_tool(request)
    cached = state['completed_calls'].get(request.call_id)
    digest = _digest(request.arguments)
    if cached and (cached['toolName'], cached['inputDigest']) != (request.name, digest):
        update['stop_reason'] = 'duplicate_tool_call_id'
    elif validation.get('errorCode'):
        update['active_result'] = {'callId': request.call_id, 'toolName': request.name,
            'status': 'rejected', 'effectStatus': 'none', 'inputDigest': digest, **validation}
    elif cached:
        update['active_result'] = {**cached, 'replayed': True}
    else:
        update['needs_approval'] = bool(validation.get('requiresApproval') or request.requires_approval)
    return update


async def _resume_result(state, services, value):
    request = _request(state)
    # Graph resume 本身不能授予写权限；消费证明由 EIC 服务重新读取校验。
    if services.validate_resume is None:
        raise ValueError('approval_resume_service_required')
    result = await services.validate_resume(request, value)
    if (result.get('callId') != request.call_id or result.get('toolName') != request.name
            or result.get('inputDigest') != _digest(request.arguments)
            or result.get('status') != 'completed' or result.get('effectStatus') != 'applied'):
        raise ValueError('approval_resume_result_mismatch')
    return result


async def approval(state):
    services = SERVICES.get()
    await services.check()
    request = _request(state)
    value = interrupt({'callId': request.call_id, 'toolName': request.name,
                       'inputDigest': _digest(request.arguments)})
    result = await _resume_result(state, services, value)
    return {'active_result': result, 'approval_required': None, 'current_node': 'approval'}


async def execute_tool(state):
    services = SERVICES.get()
    await services.check()
    services.result = {**state, 'effect_status': 'unknown'}
    request = _request(state)
    try:
        if services.remaining() <= 0:
            raise ToolExecutionError('tool_budget_exceeded', '执行预算已耗尽')
        # 超时与副作用不确定性由 EIC/ToolCenter 决定，不能在这里重试。
        remaining_ms = max(1, int(services.remaining() * 1000))
        if services.cancellation_supervision is None:
            result = await services.execute_tool(request, remaining_ms)
        else:
            result = await services.cancellation_supervision.call_tool(services, request, remaining_ms)
    except _SupervisedToolExit as exc:
        return services.cancellation_supervision.stopped_node(state, exc)
    except (OSError, TimeoutError, asyncio.CancelledError) as exc:
        # 服务必须证明目标handler异常且Session清理成功；不凭工具名称豁免。
        if (state.get('effect_status') == 'none'
                and state.get('provider_outcome') == 'known'
                and services.known_read_only_interruption is not None
                and services.known_read_only_interruption(request, exc)):
            services.result = {**services.result, 'effect_status': 'none'}
        raise
    except ToolExecutionError as exc:
        result = {'callId': request.call_id, 'toolName': request.name, 'status': 'failed',
            'errorCode': exc.code, 'message': str(exc), 'retryable': exc.retryable,
            'inputDigest': exc.input_digest or _digest(request.arguments),
            'elapsedMs': exc.elapsed_ms, 'effectStatus': exc.effect_status}
    update = {**_event(state, 'execute_tool', callId=request.call_id, status=result['status']),
              'active_result': result, 'effect_status': result.get('effectStatus', 'none')}
    services.result = {**state, **update}
    return update


async def record_tool_result(state):
    services = SERVICES.get()
    await services.check()
    request, result = _request(state), state['active_result']
    if services.record_result is not None:
        await services.record_result(request, result)
    result = {**result, 'inputDigest': _digest(request.arguments)}
    reason = ''
    if result.get('effectStatus') == 'unknown':
        reason = str(result.get('errorCode') or 'tool_outcome_uncertain')
    elif result.get('errorCode') in {'duplicate_tool_call_id', 'tool_budget_exceeded'}:
        reason = result['errorCode']
    return {**_event(state, 'record_tool_result', callId=request.call_id),
        **({'execution_context': services.capture()} if services.capture is not None else {}),
        'tool_call_count': state['tool_call_count'] + 1,
        'tool_results': [*state['tool_results'], result],
        'tool_errors': [*state['tool_errors'], *([result] if result['status'] != 'completed' else [])],
        'tool_calls': [*state['tool_calls'], *([] if result['status'] == 'rejected' else [
            {'callId': request.call_id, 'toolName': request.name, 'inputDigest': result['inputDigest']}])],
        'completed_calls': {**state['completed_calls'], request.call_id: result},
        'effect_status': result.get('effectStatus', 'none'), 'stop_reason': reason}


def _compile_execution_template():
    graph = StateGraph(ExecutionState)
    def bind(name):
        async def dispatch(state):
            return await globals()[name](state)
        return dispatch
    for name in ('check_execution', 'model_decision', 'validate_tool', 'approval',
                 'execute_tool', 'record_tool_result'):
        node = bind(name)
        graph.add_node(name, node)
    graph.add_edge(START, 'check_execution')
    graph.add_conditional_edges('check_execution', lambda s: END if s['stop_reason'] else 'model_decision')
    graph.add_conditional_edges('model_decision', lambda s: END if s['stop_reason'] else 'validate_tool')
    graph.add_conditional_edges('validate_tool', lambda s: END if s['stop_reason'] else
        'record_tool_result' if s['active_result'] else 'approval' if s['needs_approval'] else 'execute_tool')
    graph.add_edge('approval', 'record_tool_result')
    graph.add_edge('execute_tool', 'record_tool_result')
    graph.add_conditional_edges('record_tool_result', lambda s: END if s['stop_reason'] else
                               'validate_tool' if s['pending_calls'] else 'check_execution')
    return graph.compile(checkpointer=False)


_EXECUTION_TEMPLATE = _compile_execution_template()


def build_execution_graph(checkpointer=None):
    # 编译结构只含无状态节点；身份、凭据、checkpoint 均由本次调用绑定。
    graph = copy.copy(_EXECUTION_TEMPLATE)
    graph.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    return graph


class ExecutorCheckpointer(DatabaseCheckpointer):
    """在同一事务保存图快照和恢复策略，不能覆盖未知副作用为 safe_replay。"""

    async def aput(self, config, checkpoint, metadata, new_versions):
        from eic.core import agent_runtime
        from eic.models import AgentGraphCheckpoint
        c = config['configurable']
        key = [str(c['thread_id']), c.get('checkpoint_ns', ''), checkpoint['id']]
        checkpoint = sanitize_checkpoint(checkpoint)
        kind, encrypted = self._encode(key, {'checkpoint': checkpoint, 'metadata': sanitize_checkpoint_payload(metadata)})
        values = checkpoint.get('channel_values', {})
        uncertain = values.get('provider_outcome') == 'uncertain' or values.get('effect_status') == 'unknown'
        async with self._sessions()() as session:
            run = await self._guard(session, key[0])
            if await session.get(AgentGraphCheckpoint, tuple(key)) is None:
                session.add(AgentGraphCheckpoint(run_id=key[0], namespace=key[1], checkpoint_id=key[2],
                    parent_id=c.get('checkpoint_id'), graph_version=run.graph_version,
                    schema_version=1, payload_type=kind, payload_encrypted=encrypted))
                await agent_runtime.runtime_store.append_checkpoint(session, run_id=run.run_id,
                    node_name='tool_result' if values.get('current_node') == 'record_tool_result'
                        else str(values.get('current_node') or 'start'),
                    state={'graphVersion': run.graph_version, 'graphCheckpointId': key[2],
                           'providerStarted': values.get('provider_outcome') == 'uncertain',
                           'providerOutcome': 'uncertain' if uncertain else 'known',
                           'effectStatus': values.get('effect_status', 'none')},
                    resume_policy='manual_review' if uncertain else 'safe_replay')
            await session.commit()
        return self._config(*key)


async def _execute_shared(user_message, *, run_id, services, graph=None, resume=None):
    await services.check()
    graph = graph or build_execution_graph(InMemorySaver())
    config = {'configurable': {'thread_id': run_id}, 'recursion_limit': 256}
    token = SERVICES.set(services)
    try:
        await services.check()
        _require_worker_callback_boundary(graph, config, services.cancellation_supervision)
        existing = await graph.checkpointer.aget_tuple(config) if graph.checkpointer else None
        if existing is not None:
            values = existing.checkpoint.get('channel_values', {})
            if (values.get('graph_version') != AGENT_GRAPH_VERSION
                    or values.get('agent_run_id') != run_id or values.get('user_message') != user_message):
                raise ValueError('shared_checkpoint_binding_mismatch')
            if services.restore is not None and values.get('execution_context'):
                services.restore(values['execution_context'], values)
            _require_worker_callback_boundary(graph, config, services.cancellation_supervision)
            snapshot = await graph.aget_state(config)
            services.result = dict(snapshot.values)
            if any(task.interrupts for task in snapshot.tasks) and resume is None:
                services.result = {**snapshot.values, 'stop_reason': 'approval_required'}
                if services.pause is not None:
                    await services.pause(_request(snapshot.values), services.result)
                return services.result
            if resume is not None and not any(task.interrupts for task in snapshot.tasks):
                raise ValueError('approval_interrupt_missing')
            if resume is not None:
                # LangGraph 会持久化 resume 输入；先拒绝伪造票据，避免污染 pending writes。
                await _resume_result(snapshot.values, services, resume)
            initial = Command(resume=resume) if resume is not None else None
        else:
            if resume is not None:
                raise ValueError('approval_checkpoint_missing')
            initial = ExecutionState(user_message=user_message, agent_run_id=run_id,
                graph_version=AGENT_GRAPH_VERSION, answer='', stop_reason='', agent_step_count=0,
                tool_call_count=0, tool_calls=[], tool_results=[], tool_errors=[], pending_calls=[],
                completed_calls={}, trace_events=[], approval_required=None,
                provider_outcome='not_started', effect_status='none')
        _require_worker_callback_boundary(graph, config, services.cancellation_supervision)
        result = await graph.ainvoke(initial, config, durability='sync')
        await services.check()
        if result.get('__interrupt__'):
            request = _request(result)
            result.update(stop_reason='approval_required', approval_required={
                'callId': request.call_id, 'toolName': request.name})
            if services.pause is not None:
                await services.pause(request, result)
            result['tool_results'] = [*result['tool_results'], {
                'callId': request.call_id, 'toolName': request.name, 'status': 'approval_required',
                'errorCode': 'approval_required'}]
        services.result = result
        return result
    finally:
        SERVICES.reset(token)


async def execute_shared(user_message, *, run_id, services, graph=None, resume=None):
    """setup、checkpoint 和 invoke 共用入口预算，不创建不可控后台工作。"""
    services.result = ExecutionState(user_message=user_message, agent_run_id=run_id,
        graph_version=AGENT_GRAPH_VERSION, answer='', stop_reason='', agent_step_count=0,
        tool_call_count=0, tool_calls=[], tool_results=[], tool_errors=[], pending_calls=[],
        completed_calls={}, trace_events=[], approval_required=None,
        provider_outcome='not_started', effect_status='none')
    if services.cancellation_supervision is not None:
        return await _run_supervised(user_message, run_id=run_id, services=services,
                                      graph=graph, resume=resume)
    timeout = asyncio.timeout(max(0, services.remaining()))
    try:
        async with timeout:
            await services.check()
            graph = graph or build_execution_graph(InMemorySaver())
            return await _execute_shared(user_message, run_id=run_id, services=services,
                                         graph=graph, resume=resume)
    except TimeoutError as exc:
        if not timeout.expired() and not isinstance(exc, _DeadlineExceeded):
            raise
        services.result = {**services.result, 'stop_reason': 'total_budget_exceeded'}
        if graph is not None and (services.result.get('provider_outcome') == 'uncertain'
                                  or services.result.get('effect_status') == 'unknown'):
            # 超时后的唯一工作是有界写入保守恢复标记，不再派发模型或工具。
            async def mark_uncertain():
                async with asyncio.timeout(1):
                    await graph.aupdate_state({'configurable': {'thread_id': run_id}}, {
                        'stop_reason': 'total_budget_exceeded',
                        'provider_outcome': services.result.get('provider_outcome', 'not_started'),
                        'effect_status': services.result.get('effect_status', 'none')})
            await complete_cleanup(mark_uncertain())
        return services.result


async def stream_shared(user_message, *, run_id, services, graph=None, resume=None):
    """有界事件桥；关闭订阅时取消执行并等待 provider 清理，不留下后台调用。"""
    queue = asyncio.Queue(maxsize=16)
    previous_sink = services.event_sink
    async def emit(value):
        if previous_sink is not None:
            await previous_sink(value)
        await queue.put(value)
    services.event_sink = emit
    task = asyncio.create_task(execute_shared(user_message, run_id=run_id,
        services=services, graph=graph, resume=resume))
    pending = None
    try:
        while True:
            pending = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait([pending, task], return_when=asyncio.FIRST_COMPLETED)
            if pending in done:
                yield pending.result()
                pending = None
                continue
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            pending = None
            while not queue.empty():
                yield queue.get_nowait()
            await task
            break
    finally:
        async def close():
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await complete_cleanup(close())
        finally:
            services.event_sink = previous_sink


from __future__ import annotations

import asyncio
import copy
import inspect
import operator
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from eic.ai.checkpointer import DatabaseCheckpointer
from eic.ai.tool_center import ToolCenter, ToolExecutionError, _digest
from eic.ai.reliable_calls import validate_tool_arguments
from eic.ai.tool_arguments import (
    MAX_TOOL_CALLS, MAX_TOOL_ROUND_BYTES, ToolArgumentsError,
    parse_tool_arguments, tool_argument_text_size,
)

AGENT_GRAPH_VERSION = "m3-shared-executor-v1"


class AgentState(TypedDict):
    user_message: str
    rewritten_query: str
    model_available: bool
    allowed_tools: list[str]
    retrieved_documents: list[dict[str, object]]
    tool_calls: list[dict[str, object]]
    tool_results: list[dict[str, object]]
    route: Literal["model", "no_model", "tool"]
    reasoning: str
    agent_run_id: str
    trace_id: str
    graph_version: str
    current_node: str
    trace_events: Annotated[list[dict[str, object]], operator.add]


@dataclass(frozen=True)
class AgentBudget:
    """P12 的确定性执行预算，避免模型把工具链路无限延长。"""

    max_steps: int = 8
    max_tool_calls: int = 6
    max_total_ms: int = 30_000

    def __post_init__(self) -> None:
        if self.max_steps <= 0 or self.max_tool_calls <= 0 or self.max_total_ms <= 0:
            raise ValueError("Agent 执行预算必须为正数")


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]
    requires_approval: bool = False


@dataclass(frozen=True)
class ModelDecision:
    """模型适配层的最小协议；后续可直接映射 OpenAI-compatible tool_calls。"""

    kind: Literal["answer", "tool_calls", "invalid"]
    content: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()


ModelDecisionFn = Callable[[dict[str, Any]], ModelDecision | dict[str, Any] | Awaitable[ModelDecision | dict[str, Any]]]


def _trace_event(node: str, started: float, payload: dict[str, object]) -> dict[str, object]:
    return {
        "node": node,
        "eventType": "node_completed",
        "status": "completed",
        "elapsedMs": round((time.perf_counter() - started) * 1000),
        "payload": payload,
    }


def prepare_context(state: AgentState) -> dict[str, object]:
    """Deduplicate server-approved tool names before any model is contacted."""
    started = time.perf_counter()
    approved_tools = list(dict.fromkeys(state["allowed_tools"]))
    return {
        "allowed_tools": approved_tools,
        "reasoning": f"已加载 {len(approved_tools)} 个经租户白名单批准的技能。",
        "current_node": "prepare_context",
        "trace_events": [_trace_event("prepare_context", started, {
            "allowedToolCount": len(approved_tools),
        })],
    }


def rewrite_query(state: AgentState) -> dict[str, str]:
    """Apply a deterministic first-pass rewrite before retrieval.

    The initial implementation intentionally avoids an extra model call: it
    normalizes whitespace and keeps the original query available for audit.
    A provider-backed rewrite can be added behind this node later.
    """
    started = time.perf_counter()
    rewritten = " ".join(state["user_message"].split())
    return {
        "rewritten_query": rewritten,
        "current_node": "rewrite_query",
        "trace_events": [_trace_event("rewrite_query", started, {
            "queryDigest": _digest(rewritten),
            "queryLength": len(rewritten),
        })],
    }


def prepare_retrieval(state: AgentState) -> dict[str, object]:
    """Cap and preserve server-produced retrieval evidence for the answer node."""
    started = time.perf_counter()
    documents = state["retrieved_documents"][:8]
    return {
        "retrieved_documents": documents,
        "reasoning": f"{state['reasoning']}已准备 {len(documents)} 条知识库证据。",
        "current_node": "prepare_retrieval",
        "trace_events": [_trace_event("prepare_retrieval", started, {
            "documentCount": len(documents),
        })],
    }


def choose_route(state: AgentState) -> dict[str, str]:
    started = time.perf_counter()
    route = "model" if state["model_available"] else "no_model"
    return {
        "route": route,
        "current_node": "choose_route",
        "trace_events": [_trace_event("choose_route", started, {
            "route": route,
            "modelAvailable": state["model_available"],
            "documentCount": len(state["retrieved_documents"]),
        })],
    }


def route_after_decision(state: AgentState) -> Literal["model", "no_model", "tool"]:
    return state["route"]


def build_agent_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("prepare_context", prepare_context)
    graph.add_node("rewrite_query", rewrite_query)
    graph.add_node("prepare_retrieval", prepare_retrieval)
    graph.add_node("choose_route", choose_route)
    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "rewrite_query")
    graph.add_edge("rewrite_query", "prepare_retrieval")
    graph.add_edge("prepare_retrieval", "choose_route")
    graph.add_conditional_edges("choose_route", route_after_decision, {"model": END, "no_model": END, "tool": END})
    return graph.compile(checkpointer=checkpointer)


CHAT_AGENT_GRAPH = build_agent_graph(DatabaseCheckpointer())
STATELESS_AGENT_GRAPH = build_agent_graph()


def plan_chat_execution(
    user_message: str,
    *,
    model_available: bool,
    allowed_tools: list[str],
    retrieved_documents: list[dict[str, object]] | None = None,
    agent_run_id: str | None = None,
    trace_id: str | None = None,
    graph_version: str = AGENT_GRAPH_VERSION,
) -> AgentState:
    return STATELESS_AGENT_GRAPH.invoke(
        {
            "user_message": user_message,
            "rewritten_query": user_message,
            "model_available": model_available,
            "allowed_tools": allowed_tools,
            "retrieved_documents": retrieved_documents or [],
            "tool_calls": [],
            "tool_results": [],
            "route": "no_model",
            "reasoning": "",
            "agent_run_id": agent_run_id or uuid.uuid4().hex,
            "trace_id": trace_id or uuid.uuid4().hex,
            "graph_version": graph_version,
            "current_node": "start",
            "trace_events": [],
        }
    )


async def plan_chat_execution_durable(
    user_message: str, *, model_available: bool, allowed_tools: list[str],
    retrieved_documents: list[dict[str, object]] | None = None,
    agent_run_id: str, trace_id: str, graph=None,
) -> AgentState:
    """真实聊天使用持久图；存在安全快照时不重复注入原始输入。"""
    graph = graph or CHAT_AGENT_GRAPH
    config = {"configurable": {"thread_id": agent_run_id}}
    existing = await graph.checkpointer.aget_tuple(config)
    if existing is not None:
        values = existing.checkpoint.get("channel_values", {})
        if values.get("graph_version") not in (None, AGENT_GRAPH_VERSION):
            raise ValueError("graph_checkpoint_version_mismatch")
        if values.get("agent_run_id") not in (None, agent_run_id):
            raise ValueError("graph_checkpoint_run_mismatch")
        if values.get("user_message") not in (None, user_message):
            raise ValueError("graph_checkpoint_input_mismatch")
        return await graph.ainvoke(None, config, durability="sync")
    return await graph.ainvoke({
        "user_message": user_message, "rewritten_query": user_message,
        "model_available": model_available, "allowed_tools": allowed_tools,
        "retrieved_documents": retrieved_documents or [], "tool_calls": [], "tool_results": [],
        "route": "no_model", "reasoning": "", "agent_run_id": agent_run_id,
        "trace_id": trace_id, "graph_version": AGENT_GRAPH_VERSION,
        "current_node": "start", "trace_events": [],
    }, config, durability="sync")


async def execute_tool_calling_agent(
    user_message: str, *, model_decider: ModelDecisionFn, tool_center: ToolCenter,
    model_available: bool = True, allowed_tools=(), retrieved_documents=None,
    agent_run_id=None, trace_id=None, graph_version: str = AGENT_GRAPH_VERSION,
    budget: AgentBudget | None = None,
) -> dict[str, Any]:
    """兼容原调用协议，模型工具循环统一交给共享图。"""
    started = time.perf_counter()

    plan = dict(plan_chat_execution(user_message, model_available=model_available,
        allowed_tools=list(allowed_tools), retrieved_documents=retrieved_documents,
        agent_run_id=agent_run_id, trace_id=trace_id, graph_version=graph_version))
    allowed = set(plan["allowed_tools"])
    async def decide(state):
        snapshot = copy.deepcopy({**plan, **state})
        if inspect.iscoroutinefunction(model_decider):
            return await model_decider(snapshot)
        value = await asyncio.to_thread(model_decider, snapshot)
        return await value if inspect.isawaitable(value) else value

    async def validate(request):
        definition = tool_center.get(request.name)
        if definition is None or request.name not in allowed:
            return {"errorCode": "tool_not_allowed", "message": "工具未获当前 Agent 白名单授权"}
        try:
            validate_tool_arguments(tool_center, request.arguments, definition.input_schema)
        except ToolExecutionError as exc:
            return {"errorCode": exc.code, "message": str(exc)}
        return {"requiresApproval": definition.requires_approval}

    async def execute(request, remaining_ms):
        definition = tool_center.get(request.name)
        execution = await tool_center.execute_audited_async(request.name, request.arguments, allowed,
            idempotency_key=f"{plan['agent_run_id']}:{request.call_id}",
            timeout_ms=min(definition.timeout_ms, remaining_ms))
        return {"callId": request.call_id, "toolName": request.name, "status": "completed",
                "result": execution.value, "inputDigest": execution.input_digest,
                "outputDigest": execution.output_digest, "elapsedMs": execution.elapsed_ms,
                "replayed": execution.replayed, "effectStatus": "applied"}

    async def dispatch_standard(call):
        return await execute(ToolCallRequest(call_id=call.call_id, name=call.definition.name,
            arguments=call.arguments, requires_approval=call.definition.requires_approval), call.remaining_ms)

    standard_tools = tool_center.standard_tools(allowed, dispatch=dispatch_standard)

    async def standard_execute(request, remaining_ms):
        return await standard_tools.ainvoke(request.name, request.arguments,
            call_id=request.call_id, remaining_ms=remaining_ms)

    services = _shared_executor.ExecutionServices(model=decide, execute_tool=standard_execute, validate_tool=validate,
                                 budget=budget or AgentBudget(), started=started, model_available=model_available)
    result = await _shared_executor.execute_shared(user_message, run_id=plan["agent_run_id"], services=services)
    return {**plan, **result,
        "tool_budget": {"maxSteps": services.budget.max_steps,
                        "maxToolCalls": services.budget.max_tool_calls,
                        "maxTotalMs": services.budget.max_total_ms},
        "trace_events": [*plan["trace_events"], *result["trace_events"]]}


def normalize_model_decision(value: object) -> ModelDecision:
    """把模型返回的标准或兼容 tool call 结构归一为内部协议。"""
    if isinstance(value, ModelDecision):
        return value
    if isinstance(value, str):
        return ModelDecision("answer", content=value)
    if not isinstance(value, dict):
        return ModelDecision("invalid")
    raw_calls = value.get("tool_calls") or value.get("toolCalls") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    calls: list[ToolCallRequest] = []
    if isinstance(raw_calls, list):
        if len(raw_calls) > MAX_TOOL_CALLS:
            raise ToolExecutionError("invalid_tool_arguments", "模型工具调用数量超过本地限制", retryable=False)
        argument_bytes = 0
        for raw in raw_calls:
            if not isinstance(raw, dict):
                return ModelDecision("invalid")
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            arguments = raw.get("arguments", function.get("arguments", {}))
            if isinstance(arguments, str):
                try:
                    argument_bytes += tool_argument_text_size(arguments)
                    if argument_bytes > MAX_TOOL_ROUND_BYTES:
                        raise ToolArgumentsError("模型工具参数累计超过本地限制")
                    arguments = parse_tool_arguments(arguments)
                except ToolArgumentsError as exc:
                    raise ToolExecutionError(exc.code, str(exc), retryable=False) from None
            if not isinstance(arguments, dict):
                return ModelDecision("invalid")
            name = str(raw.get("name") or function.get("name") or "").strip()
            if not name:
                return ModelDecision("invalid")
            calls.append(ToolCallRequest(
                call_id=str(raw.get("call_id") or raw.get("callId") or raw.get("id") or f"tool-{uuid.uuid4().hex}"),
                name=name,
                arguments=arguments,
                requires_approval=bool(raw.get("requires_approval") or raw.get("requiresApproval")),
            ))
    if calls:
        return ModelDecision("tool_calls", tool_calls=tuple(calls))
    content = value.get("content") or value.get("answer") or ""
    return ModelDecision("answer", content=str(content)) if str(content).strip() else ModelDecision("invalid")


_normalize_model_decision = normalize_model_decision

# 延迟到协议定义完成再导入，兼容 shared_executor 的反向协议引用。
from eic.ai import shared_executor as _shared_executor


def _finish_tool_agent(state: dict[str, Any], reason: str) -> dict[str, Any]:
    state["stop_reason"] = reason
    state["current_node"] = "terminal"
    state["route"] = "model" if reason == "answer" else "tool"
    return state

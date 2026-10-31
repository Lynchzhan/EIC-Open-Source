import asyncio
import time

import httpx
import pytest

from eic.ai.agent_graph import AgentBudget, execute_tool_calling_agent, normalize_model_decision
from eic.ai.providers import stream_chat_completion
from eic.ai.tool_center import ToolCenter, ToolDefinition, ToolExecutionError
from eic.models import AIModel


def calculator_center() -> ToolCenter:
    center = ToolCenter()
    center.register(ToolDefinition(
        name="calculator",
        description="执行四则运算",
        handler=lambda args: {"value": args["a"] + args["b"]},
        input_schema={
            "type": "object",
            "required": ["a", "b"],
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "additionalProperties": False,
        },
    ))
    return center


def test_tool_center_rejects_invalid_schema_and_emits_openai_schema() -> None:
    center = calculator_center()
    assert center.openai_tools({"calculator"})[0]["function"]["name"] == "calculator"
    with pytest.raises(ToolExecutionError) as error:
        center.execute_audited("calculator", {"a": 1, "b": 2, "extra": 3}, {"calculator"})
    assert error.value.code == "invalid_tool_arguments"


def test_tool_center_idempotency_replays_completed_result() -> None:
    calls = []
    center = ToolCenter()
    center.register(ToolDefinition(
        name="write_fixture",
        description="写入测试计数",
        handler=lambda args: calls.append(args["value"]) or {"ok": True},
        input_schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
            "additionalProperties": False,
        },
    ))
    first = center.execute_audited("write_fixture", {"value": 1}, {"write_fixture"}, idempotency_key="run:call-1")
    second = center.execute_audited("write_fixture", {"value": 1}, {"write_fixture"}, idempotency_key="run:call-1")
    assert calls == [1]
    assert first.replayed is False
    assert second.replayed is True
    assert second.output_digest == first.output_digest


def test_tool_center_timeout_is_structured_and_not_retried() -> None:
    center = ToolCenter()

    def slow(_: dict) -> dict:
        time.sleep(0.05)
        return {"ok": True}

    center.register(ToolDefinition("slow", "慢工具", slow, timeout_ms=10))

    async def run() -> None:
        with pytest.raises(ToolExecutionError) as error:
            await center.execute_audited_async("slow", {}, {"slow"}, idempotency_key="run:slow")
        assert error.value.code == "tool_timeout"
        assert error.value.retryable is False

    asyncio.run(run())


def test_agent_tool_calling_loop_executes_then_returns_to_model() -> None:
    center = calculator_center()
    decisions = iter([
        {"tool_calls": [{"id": "call-1", "name": "calculator", "arguments": {"a": 2, "b": 3}}]},
        {"content": "计算结果为 5"},
    ])

    async def run() -> dict:
        return await execute_tool_calling_agent(
            "计算 2 加 3",
            model_decider=lambda state: next(decisions),
            tool_center=center,
            allowed_tools=["calculator"],
        )

    state = asyncio.run(run())
    assert state["answer"] == "计算结果为 5"
    assert state["stop_reason"] == "answer"
    assert state["tool_call_count"] == 1
    assert state["tool_results"][0]["result"] == {"value": 5}
    assert [event["node"] for event in state["trace_events"]][-5:-1] == [
        "model_decision", "validate_tool", "execute_tool", "record_tool_result",
    ]


def test_agent_rejects_unauthorized_tool_and_stops_at_budget() -> None:
    center = calculator_center()
    decisions = iter([
        {"tool_calls": [{"id": "call-1", "name": "calculator", "arguments": {"a": 1, "b": 2}}]},
        {"tool_calls": [{"id": "call-2", "name": "calculator", "arguments": {"a": 1, "b": 2}}]},
    ])

    async def run() -> dict:
        return await execute_tool_calling_agent(
            "重复计算",
            model_decider=lambda state: next(decisions),
            tool_center=center,
            allowed_tools=[],
            budget=AgentBudget(max_steps=2, max_tool_calls=4, max_total_ms=1000),
        )

    state = asyncio.run(run())
    assert state["tool_results"][0]["errorCode"] == "tool_not_allowed"
    assert state["stop_reason"] == "max_steps_exceeded"


def test_agent_write_tool_requires_approval_before_execution() -> None:
    calls = []
    center = ToolCenter()
    center.register(ToolDefinition(
        name="write_fixture",
        description="写入测试数据",
        handler=lambda args: calls.append(args) or {"ok": True},
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        requires_approval=True,
    ))

    async def run() -> dict:
        return await execute_tool_calling_agent(
            "写入数据",
            model_decider=lambda state: {"tool_calls": [{"id": "write-1", "name": "write_fixture", "arguments": {}}]},
            tool_center=center,
            allowed_tools=["write_fixture"],
        )

    state = asyncio.run(run())
    assert state["stop_reason"] == "approval_required"
    assert state["tool_results"][0]["status"] == "approval_required"
    assert calls == []


def test_normalize_standard_openai_tool_call() -> None:
    decision = normalize_model_decision({
        "tool_calls": [{
            "id": "call-standard",
            "type": "function",
            "function": {
                "name": "calculator",
                "arguments": "{\"a\": 2, \"b\": 3}",
            },
        }],
    })
    assert decision.kind == "tool_calls"
    assert decision.tool_calls[0].call_id == "call-standard"
    assert decision.tool_calls[0].name == "calculator"
    assert decision.tool_calls[0].arguments == {"a": 2, "b": 3}


def test_openai_stream_collects_tool_call_argument_deltas() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
        '"type":"function","function":{"name":"calculator","arguments":"{\\"a\\":"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"2,\\"b\\":3}"}}]},'
        '"finish_reason":"tool_calls"}]}\n\n'
    )
    model = AIModel(
        id=1,
        tenant_id=1,
        provider="openai_compatible",
        model_type="chat",
        endpoint="http://fixture.invalid/v1",
        model_name="fixture",
        max_input_tokens=4096,
        max_output_tokens=123,
    )
    calls: list[dict[str, object]] = []

    async def run() -> list[str]:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return [
                chunk
                async for chunk in stream_chat_completion(
                    model,
                    "计算 2 加 3",
                    client=client,
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "description": "执行四则运算",
                            "parameters": {"type": "object"},
                        },
                    }],
                    tool_call_sink=calls,
                )
            ]

    assert asyncio.run(run()) == []
    assert calls == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "calculator", "arguments": '{"a":2,"b":3}'},
    }]

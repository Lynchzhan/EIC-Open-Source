"""P12 的原失败场景；仅使用合成数据库和协议桩，不调用真实模型。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select, text

from eic.ai.agent_graph import AgentBudget, execute_tool_calling_agent, normalize_model_decision
from eic.ai.budget import message_units
from eic.ai.providers import ModelProviderError, stream_chat_completion
from eic.ai.tool_center import ToolCenter, ToolDefinition, ToolExecutionError
from eic.models import AIModel
from test_id_generation_safety import fixture_db as base_fixture_db
from test_p12_tool_calling import calculator_center


@asynccontextmanager
async def fixture_db(monkeypatch, tmp_path, **kwargs):
    from eic.core import generation
    async with base_fixture_db(monkeypatch, tmp_path, **kwargs) as sessions:
        # 与 schema upgrade 后的 SQLite 一致，在启动 producer 前设置 WAL。
        async with sessions() as session:
            assert await session.scalar(text('PRAGMA journal_mode=WAL')) == 'wal'
            await session.commit()
        try:
            yield sessions
        finally:
            # done 事件可早于 producer 清理结束；关库前等待该批后台任务。
            async with asyncio.timeout(10):
                await asyncio.gather(*list(generation.PRODUCERS))


@pytest.mark.parametrize("name,args", [
    ("calculator", {"a": 9, "b": 2}),
    ("other", {"a": 1, "b": 2}),
])
def test_idempotency_key_binds_tool_and_arguments(name, args):
    center = calculator_center()
    center.register(ToolDefinition("other", "另一个工具", lambda _: {"value": 0}))
    center.execute_audited("calculator", {"a": 1, "b": 2}, {"calculator"}, idempotency_key="run:1")
    with pytest.raises(ToolExecutionError) as error:
        center.execute_audited(name, args, {"calculator", "other"}, idempotency_key="run:1")
    assert error.value.code == "duplicate_tool_call_id"


def test_failed_concurrent_call_executes_once():
    calls = []

    def fail(args):
        calls.append(args)
        time.sleep(0.04)
        raise RuntimeError("PRIVATE_ERROR")

    center = ToolCenter()
    center.register(ToolDefinition("fail", "失败工具", fail))

    def invoke(_):
        with pytest.raises(ToolExecutionError) as error:
            center.execute_audited("fail", {}, {"fail"}, idempotency_key="run:failure")
        assert "PRIVATE_ERROR" not in str(error.value)
        return error.value.code

    with ThreadPoolExecutor(max_workers=3) as executor:
        assert list(executor.map(invoke, range(3))) == ["tool_failed"] * 3
    assert len(calls) == 1


def test_timeout_late_success_is_not_replayed_or_reexecuted():
    calls = []
    center = ToolCenter()

    def slow(_):
        calls.append("started")
        time.sleep(0.06)
        calls.append("finished")
        return {"ok": True}

    center.register(ToolDefinition("slow", "慢工具", slow, timeout_ms=20))

    async def run():
        with pytest.raises(ToolExecutionError) as error:
            await center.execute_audited_async("slow", {}, {"slow"}, idempotency_key="run:slow")
        assert error.value.code == "tool_timeout"
        assert error.value.effect_status == "unknown"
        await asyncio.sleep(0.1)
        with pytest.raises(ToolExecutionError) as repeated:
            await center.execute_audited_async("slow", {}, {"slow"}, idempotency_key="run:slow")
        assert repeated.value.code == "tool_timeout"

    asyncio.run(run())
    assert calls == ["started", "finished"]


@pytest.mark.parametrize("interruption", [TimeoutError, asyncio.CancelledError])
def test_interruption_at_result_delivery_keeps_idempotency_unknown(monkeypatch, interruption):
    center = ToolCenter()
    calls = []
    center.register(ToolDefinition("delivery", "结果交付边界", lambda _: calls.append(1) or {"ok": True}))
    real_to_thread = asyncio.to_thread

    async def interrupted_delivery(function, *args, **kwargs):
        # 确定性复现：线程写入成功缓存之后，等待方在收到结果之前被中断。
        function(*args, **kwargs)
        raise interruption()

    async def run():
        monkeypatch.setattr(asyncio, "to_thread", interrupted_delivery)
        with pytest.raises(interruption if interruption is asyncio.CancelledError else ToolExecutionError):
            await center.execute_audited_async(
                "delivery", {}, {"delivery"}, idempotency_key="run:delivery",
            )
        monkeypatch.setattr(asyncio, "to_thread", real_to_thread)
        with pytest.raises(ToolExecutionError) as repeated:
            await center.execute_audited_async(
                "delivery", {}, {"delivery"}, idempotency_key="run:delivery",
            )
        assert repeated.value.code == "tool_timeout"
        assert repeated.value.effect_status == "unknown"

    asyncio.run(run())
    assert calls == [1]


def test_replay_cannot_mutate_cached_result():
    center = calculator_center()
    first = center.execute_audited("calculator", {"a": 1, "b": 2}, {"calculator"}, idempotency_key="run:1")
    first.value["value"] = 999
    replay = center.execute_audited("calculator", {"a": 1, "b": 2}, {"calculator"}, idempotency_key="run:1")
    assert replay.value == {"value": 3}


def test_argument_order_does_not_change_call_identity():
    center = calculator_center()
    first = center.execute_audited("calculator", {"a": 1, "b": 2}, {"calculator"}, idempotency_key="run:1")
    replay = center.execute_audited("calculator", {"b": 2, "a": 1}, {"calculator"}, idempotency_key="run:1")
    assert replay.replayed and first.input_digest == replay.input_digest


def test_async_model_decision_is_bounded_by_total_deadline():
    async def decide(_):
        await asyncio.sleep(0.2)
        return {"content": "too late"}

    started = time.perf_counter()
    state = asyncio.run(execute_tool_calling_agent(
        "Q", model_decider=decide, tool_center=calculator_center(),
        budget=AgentBudget(max_total_ms=20),
    ))
    assert state["stop_reason"] == "total_budget_exceeded"
    assert time.perf_counter() - started < 0.15


def test_sync_decider_deadline_stops_agent_but_does_not_kill_worker_thread():
    import threading
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def decide(_):
        calls.append(1)
        started.set()
        release.wait(timeout=2)
        finished.set()
        return {"content": "too late"}

    async def run():
        try:
            state = await execute_tool_calling_agent(
                "Q", model_decider=decide, tool_center=calculator_center(),
                budget=AgentBudget(max_total_ms=40),
            )
            assert state["stop_reason"] == "total_budget_exceeded"
            assert state["tool_results"] == [] and state["answer"] == ""
            assert started.is_set() and not finished.is_set()
        finally:
            # 显式释放同步线程；API deadline 不等同进程退出或线程终止。
            release.set()

    asyncio.run(run())
    assert calls == [1] and finished.is_set()


def test_rejected_calls_count_toward_tool_budget():
    decision = {"tool_calls": [{"id": f"call-{i}", "name": "calculator",
                               "arguments": {"a": 1, "b": 2}} for i in range(7)]}
    state = asyncio.run(execute_tool_calling_agent(
        "Q", model_decider=lambda _: decision, tool_center=calculator_center(),
        allowed_tools=[], budget=AgentBudget(max_steps=1, max_tool_calls=6),
    ))
    assert state["stop_reason"] == "max_tool_calls_exceeded"
    assert state["tool_call_count"] == 6
    assert len(state["tool_results"]) == 6


def test_agent_conflicting_call_id_is_rejected():
    decisions = iter([
        {"tool_calls": [{"id": "call-1", "name": "calculator", "arguments": {"a": 1, "b": 2}}]},
        {"tool_calls": [{"id": "call-1", "name": "calculator", "arguments": {"a": 9, "b": 2}}]},
        {"content": "OK"},
    ])
    state = asyncio.run(execute_tool_calling_agent(
        "Q", model_decider=lambda _: next(decisions), tool_center=calculator_center(),
        allowed_tools=["calculator"],
    ))
    assert state["stop_reason"] == "duplicate_tool_call_id"


def test_missing_ids_are_unique_across_rounds():
    payload = {"tool_calls": [{"name": "calculator", "arguments": {"a": 1, "b": 2}}]}
    assert normalize_model_decision(payload).tool_calls[0].call_id != \
        normalize_model_decision(payload).tool_calls[0].call_id


def test_message_budget_counts_null_content_tool_metadata_and_schema():
    small = [{"role": "assistant", "content": None}]
    base = message_units(small)
    assert message_units([{**small[0], 'tool_calls':[{'id':'budget-call','type':'function',
        'function':{'name':'fixture','arguments':json.dumps({'value':'x'*200})}}]}]) > base + 200
    assert message_units(small, [{"function": {"description": "x" * 200}}]) > base + 200


def test_provider_rejects_oversized_tool_schema_before_network():
    seen = []
    model = AIModel(id=1, tenant_id=1, provider="openai", model_name="fixture",
                    endpoint="http://fixture.invalid", max_input_tokens=100)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(200),
        )) as client:
            with pytest.raises(ModelProviderError) as error:
                _ = [x async for x in stream_chat_completion(
                    model, "Q", client=client, tools=[{"description": "x" * 200}],
                )]
            assert error.value.reason == "input_budget_exceeded"
    asyncio.run(run())
    assert not seen


def test_openai_stream_name_fragments_and_parallel_arguments():
    body = (
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"a","function":{"name":"cal","arguments":"{\\"a\\":1,"}},'
        '{"index":1,"id":"b","function":{"name":"calculator","arguments":"{\\"a\\":2,"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":0,"function":{"name":"culator","arguments":"\\"b\\":2}"}},'
        '{"index":1,"function":{"arguments":"\\"b\\":3}"}}]},"finish_reason":"tool_calls"}]}\n\n'
    )
    model = AIModel(id=1, tenant_id=1, provider="openai", model_name="fixture",
                    endpoint="http://fixture.invalid")
    calls = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=body),
        )) as client:
            return [x async for x in stream_chat_completion(
                model, "Q", client=client, tools=calculator_center().openai_tools({"calculator"}),
                tool_call_sink=calls,
            )]
    assert asyncio.run(run()) == []
    assert [c["function"]["name"] for c in calls] == ["calculator"] * 2
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [{"a": 1, "b": 2}, {"a": 2, "b": 3}]


@pytest.mark.parametrize("choice", ["auto", "none"])
def test_ollama_tool_messages_use_native_arguments_and_tool_name(choice):
    seen, calls = [], []
    model = AIModel(id=1, tenant_id=1, provider="ollama", model_name="fixture",
                    endpoint="http://fixture.invalid")
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "old", "type": "function", "function": {
                "name": "calculator", "arguments": '{"a":1,"b":2}',
            },
        }]},
        {"role": "tool", "name": "calculator", "tool_call_id": "old", "content": '{"value":3}'},
    ]
    body = (json.dumps({"message": {"tool_calls": [{
        "function": {"name": "calculator", "arguments": {"a": 2, "b": 3}},
    }]}, "done": True}) + "\n")

    async def run():
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, content=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return [x async for x in stream_chat_completion(
                model, "Q", messages=messages, client=client, tool_choice=choice,
                tools=calculator_center().openai_tools({"calculator"}), tool_call_sink=calls,
            )]
    if choice == 'none':
        with pytest.raises(ModelProviderError) as error:
            asyncio.run(run())
        assert error.value.reason == 'provider_outcome_uncertain' and calls == []
    else:
        assert asyncio.run(run()) == []
    wire = seen[0]
    assert wire["messages"][0]["tool_calls"][0]["function"]["arguments"] == {"a": 1, "b": 2}
    assert wire["messages"][1]["tool_name"] == "calculator"
    assert "tool_call_id" not in wire["messages"][1]
    assert "tool_choice" not in wire
    assert bool(wire.get('tools')) == (choice == "auto")
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"a":1,"b":2}'
    if choice != 'none':
        assert calls[0]["function"]["arguments"] == {"a": 2, "b": 3}


async def seed_chat(sessions, *, two_models=False, app_config=None):
    from eic.models import ChatSession, Resource
    async with sessions() as session:
        for identifier in ([201, 202] if two_models else [201]):
            session.add(AIModel(id=identifier, tenant_id=1, provider="openai",
                                model_name=str(identifier), endpoint="http://fixture.invalid",
                                model_type="chat", enabled=True))
        session.add(Resource(id=501, tenant_id=1, owner_id=11, resource_type="skills",
                             name="calculator", data={"enabled": True, "allowedTools": ["calculator"]}))
        if app_config is not None:
            session.add(Resource(id=401, tenant_id=1, owner_id=11, resource_type="ai_apps",
                                 name="Agent", data=app_config))
            (await session.get(ChatSession, 101)).app_id = 401
        await session.commit()


def tool_call(index, name="calculator", args=None):
    return {"id": f"call-{index}", "type": "function", "function": {
        "name": name, "arguments": json.dumps({"a": 1, "b": 2} if args is None else args),
    }}


def test_sse_six_tool_calls_still_allow_final_answer(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentToolCall, ChatMessage, AgentCheckpoint
    calls = []

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions)

            async def provider(model, question, **kwargs):
                calls.append(kwargs)
                if len(calls) <= 6:
                    kwargs["tool_call_sink"].append(tool_call(len(calls)))
                else:
                    assert kwargs["tools"] == [] and kwargs["tool_choice"] == "none"
                    yield "最终答案"

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            output = [x async for x in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "completed"
                records = list(await session.scalars(select(AgentToolCall)))
                assert len(records) == 6 and all(row.status == "completed" for row in records)
                assert all(row.finished_at is not None for row in records)
                checkpoints = list(await session.scalars(select(AgentCheckpoint).where(
                    AgentCheckpoint.node_name == "tool_pending",
                )))
                assert len(checkpoints) == 6 and all(row.resume_policy == "manual_review" for row in checkpoints)
            assert "最终答案" in "".join(output)
    asyncio.run(run())
    assert len(calls) == 7


@pytest.mark.parametrize("scenario", ["invalid", "runtime_error", "approval", "excess_calls"])
def test_sse_non_provider_errors_do_not_fall_back(monkeypatch, tmp_path, scenario):
    from eic import main
    from eic.models import ChatMessage
    calls, executions = [], []
    center = calculator_center()
    center.register(ToolDefinition("write", "写工具", lambda args: executions.append(args) or {},
                                   requires_approval=True))
    monkeypatch.setattr(main, "tool_center", center)

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, two_models=True)
            if scenario == "approval":
                from eic.models import Resource
                async with sessions() as session:
                    (await session.get(Resource, 501)).data = {"allowedTools": ["write"]}
                    await session.commit()

            async def provider(model, question, **kwargs):
                calls.append(model.id)
                if scenario == "runtime_error":
                    raise RuntimeError("PRIVATE_ERROR")
                if scenario == "approval":
                    kwargs["tool_call_sink"].append(tool_call(1, "write", {}))
                elif scenario == "excess_calls":
                    kwargs["tool_call_sink"].extend([tool_call(i) for i in range(7)])
                yield ""

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            output = [x async for x in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            async with sessions() as session:
                message = await session.get(ChatMessage, 301)
                assert message.status == ("waiting_approval" if scenario == "approval" else "failed")
                if scenario == "approval":
                    from eic.models import AgentRun
                    from eic.core import agent_approval
                    current = await session.scalar(select(AgentRun))
                    assert current.status == "waiting_approval"
                    pending = await agent_approval.pending(session, current)
                    assert pending is not None and pending.state_json["toolName"] == "write"
                    assert any('"status": "waiting_approval"' in chunk for chunk in output)
            assert "PRIVATE_ERROR" not in "".join(output)
    asyncio.run(run())
    assert len(calls) == 1 and executions == []


@pytest.mark.parametrize("revoke", ["skill", "permission", "app"])
def test_sse_revalidates_before_tool_execution(monkeypatch, tmp_path, revoke):
    from eic import main
    from eic.core import generation
    from eic.models import AgentToolCall, Resource, User, ChatMessage
    executions = []
    center = calculator_center()
    center._tools["calculator"] = ToolDefinition("calculator", "计算器",
                                                 lambda _: executions.append(1) or {"value": 3})
    monkeypatch.setattr(main, "tool_center", center)

    async def run():
        config = {"isSkill": True, "skills": [{"aiSkillToolManagementId": 501, "isSelect": True}]}
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, app_config=config if revoke == "app" else None)

            async def provider(model, question, **kwargs):
                async with sessions() as session:
                    if revoke == "skill":
                        (await session.get(Resource, 501)).data = {"enabled": False, "allowedTools": ["calculator"]}
                    elif revoke == "permission":
                        (await session.get(User, 11)).is_active = False
                    else:
                        (await session.get(Resource, 401)).data = {**config, "isSkill": False}
                    await session.commit()
                kwargs["tool_call_sink"].append(tool_call(1))
                yield ""

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            _ = [x async for x in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            # 撤权订阅者先退出；终态断言须等待独立 producer 清理，不依赖轮询竞态。
            async with asyncio.timeout(10):
                await asyncio.gather(*list(generation.PRODUCERS))
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "failed"
                assert list(await session.scalars(select(AgentToolCall))) == []
    asyncio.run(run())
    assert executions == []


@pytest.mark.parametrize("enabled,selected,expected", [
    (True, True, {"calculator"}), (True, False, set()), (False, True, set()),
])
def test_agent_binding_uses_flags_and_selected_resource_ids(monkeypatch, tmp_path, enabled, selected, expected):
    from eic import main
    from eic.models import Resource

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions)
            async with sessions() as session:
                session.add(Resource(id=502, tenant_id=99, resource_type="skills",
                                     name="other tenant", data={"allowedTools": ["calculator"]}))
                await session.commit()
                config = {"isSkill": enabled, "skills": [{
                    "aiSkillToolManagementId": 501, "isSelect": selected,
                    "aiSkillToolManagementName": "untrusted-name",
                }, {"aiSkillToolManagementId": 502, "isSelect": True}]}
                assert await main.load_chat_tool_names(
                    session, main.Principal(11, 1, "fixture", True), 401, config,
                ) == expected | {'ReadContextArtifact'}
    asyncio.run(run())


def test_pending_intent_prevents_automatic_replay_and_preserves_raw_input_identity(monkeypatch, tmp_path):
    from eic import main
    from eic.ai.agent_graph import ToolCallRequest
    from eic.core import agent_runtime
    from eic.models import AgentToolCall

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                row = await agent_runtime.runtime_store.create_run(
                    session, tenant_id=1, session_id=101, request_id="fixture", graph_version="p12-test",
                )
                run_id = row.run_id
                await session.commit()
            request = ToolCallRequest("call-1", "calculator", {"token": "value-a"})
            definition = calculator_center().get("calculator")
            await main.begin_chat_tool_call(main.Principal(11, 1, "fixture", True), run_id, request, definition)
            with pytest.raises(ToolExecutionError) as error:
                await main.begin_chat_tool_call(main.Principal(11, 1, "fixture", True), run_id, request, definition)
            assert error.value.code == "tool_outcome_uncertain"
            async with sessions() as session:
                record = await session.scalar(select(AgentToolCall))
                assert record.status == "running" and record.effect_status == "unknown"
                assert record.input_digest != agent_runtime.digest_payload({"token": "value-b"}, redact=False)
                decision = await agent_runtime.runtime_store.decide_recovery(
                    session, run_id=run_id, expected_graph_version="p12-test",
                )
                assert decision.action == "manual_review"
    asyncio.run(run())


@pytest.mark.parametrize("argument", [{"code": "INVALID_PRIVATE_VALUE"}, {"code": "AB-12", "extra": 1},
                                    {"code": "AB-12", "value": float("nan")}])
def test_schema_composition_pattern_unknown_fields_and_nonfinite_values(argument):
    calls = []
    center = ToolCenter()
    center.register(ToolDefinition("validated", "完整 Schema 校验", lambda args: calls.append(args) or {},
                                   input_schema={
                                       "type": "object", "required": ["code"],
                                       "properties": {"code": {"type": "string", "pattern": "^[A-Z]{2}-[0-9]{2}$"},
                                                      "value": {"type": "number"}},
                                       "additionalProperties": False,
                                       "allOf": [{"not": {"required": ["forbidden"]}}],
                                   }))
    with pytest.raises(ToolExecutionError) as error:
        center.execute_audited("validated", argument, {"validated"})
    assert error.value.code == "invalid_tool_arguments"
    assert "INVALID_PRIVATE_VALUE" not in str(error.value) and calls == []


def test_schema_remote_refs_are_rejected_during_registration():
    center = ToolCenter()
    with pytest.raises(ValueError):
        center.register(ToolDefinition("bad", "拒绝远程 Schema", lambda _: {},
                                       input_schema={"$ref": "https://fixture.invalid/schema"}))


@pytest.mark.parametrize("scenario", ["replay", "conflict", "provider_error"])
def test_sse_repeat_identity_and_post_tool_provider_error(monkeypatch, tmp_path, scenario):
    from eic import main
    from eic.models import AgentToolCall, ChatMessage
    executions, calls = [], []
    center = ToolCenter()
    center.register(ToolDefinition("calculator", "计算器",
                                   lambda args: executions.append(args) or {"value": args["a"] + args["b"]}))
    monkeypatch.setattr(main, "tool_center", center)

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, two_models=True)

            async def provider(model, question, **kwargs):
                calls.append(model.id)
                if len(calls) == 1:
                    kwargs["tool_call_sink"].append(tool_call(1))
                elif len(calls) == 2:
                    if scenario == "provider_error":
                        raise ModelProviderError("PRIVATE_ERROR", retryable=True)
                    kwargs["tool_call_sink"].append(tool_call(
                        1, args={"a": 9, "b": 2} if scenario == "conflict" else None,
                    ))
                else:
                    yield "最终答案"
                yield ""

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            _ = [x async for x in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            async with sessions() as session:
                message = await session.get(ChatMessage, 301)
                assert message.status == ("completed" if scenario == "replay" else "failed")
                rows = list(await session.scalars(select(AgentToolCall)))
                assert len(rows) == 1 and rows[0].status == "completed"
                if scenario == "conflict":
                    assert message.token_usage["reason"] == "duplicate_tool_call_id"
    asyncio.run(run())
    assert len(executions) == 1 and len(set(calls)) == 1
    assert len(calls) == (3 if scenario == "replay" else 2)


def test_sse_provider_total_timeout_has_no_fallback(monkeypatch, tmp_path):
    from eic import main
    from eic.models import ChatMessage
    calls = []
    # 留出限速容器中权限与 checkpoint 提交时间，确保测试实际进入 provider。
    monkeypatch.setattr(main, "AgentBudget", lambda **kwargs: AgentBudget(**{**kwargs, 'max_total_ms': 5000}))

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, two_models=True)

            async def provider(model, question, **kwargs):
                calls.append(model.id)
                await asyncio.Event().wait()
                yield "too late"

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            _ = [x async for x in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            assert len(calls) == 1
            async with sessions() as session:
                message = await session.get(ChatMessage, 301)
                assert message.status == "failed" and message.token_usage["reason"] == "agent_time_limit"
                assert message.token_usage["providerOutcome"] == "uncertain"
    asyncio.run(run())
    assert len(calls) == 1


def test_sse_timeout_records_unknown_effect_and_stops(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentCheckpoint, AgentToolCall, ChatMessage
    calls, executions = [], []
    center = ToolCenter()

    def slow(_):
        executions.append("started")
        time.sleep(3)
        executions.append("finished")
        return {"ok": True}

    center.register(ToolDefinition("calculator", "慢工具", slow, timeout_ms=2000))
    monkeypatch.setattr(main, "tool_center", center)

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, two_models=True)

            async def provider(model, question, **kwargs):
                calls.append(model.id)
                kwargs["tool_call_sink"].append(tool_call(1))
                yield ""

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            _ = [x async for x in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            await asyncio.sleep(1.5)
            async with sessions() as session:
                record = await session.scalar(select(AgentToolCall))
                assert record.status == "failed" and record.effect_status == "unknown"
                assert record.error_code == "tool_timeout" and record.finished_at is not None
                assert (await session.get(ChatMessage, 301)).status == "failed"
                checkpoint = await session.scalar(select(AgentCheckpoint).where(
                    AgentCheckpoint.node_name == "tool_result",
                ))
                assert checkpoint.resume_policy == "manual_review"
    asyncio.run(run())
    assert calls and len(calls) == 1 and executions == ["started", "finished"]


def test_stale_generation_owner_cannot_commit_tool_intent(monkeypatch, tmp_path):
    from eic import main
    from eic.ai.agent_graph import ToolCallRequest
    from eic.core import agent_runtime, generation
    from eic.models import AgentToolCall
    from test_id_generation_safety import claim

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await claim(sessions)
            async with sessions() as session:
                row = await agent_runtime.runtime_store.create_run(
                    session, tenant_id=1, session_id=101, request_id="fixture", graph_version="p12-test",
                )
                run_id = row.run_id
                await session.commit()
            owner = generation.OWNER.set((301, "old-owner"))
            try:
                with pytest.raises(generation.LeaseLost):
                    await main.begin_chat_tool_call(
                        main.Principal(11, 1, "fixture", True), run_id,
                        ToolCallRequest("call-1", "calculator", {"a": 1, "b": 2}),
                        calculator_center().get("calculator"),
                    )
            finally:
                generation.OWNER.reset(owner)
            async with sessions() as session:
                assert list(await session.scalars(select(AgentToolCall))) == []
    asyncio.run(run())

"""固定合成回归：运行应用实现，评测器在结果产生后读取 gold。"""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time

from .boundaries import HarnessError, canonical, digest
from .metrics import agent_metrics, average, citation_metrics, ranking_metrics
from .observations import agent_observations, node_metrics, verify_runtime_trace

STRATA = ("simple", "medium", "complex", "long_context")


def validate_cases(document):
    cases = document.get("cases")
    if document.get("schemaVersion") != 1 or not isinstance(cases, list) or not cases:
        raise HarnessError("固定集为空或结构不完整")
    ids = []
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "stratum", "kind", "input", "gold"}:
            raise HarnessError("固定用例结构无效")
        if case["stratum"] not in STRATA or case["kind"] not in {"rag", "tool", "memory", "recovery", "retry", "context"}:
            raise HarnessError("固定用例类型无效")
        if not isinstance(case["id"], str) or not isinstance(case["input"], dict) or not isinstance(case["gold"], dict):
            raise HarnessError("固定用例字段无效")
        ids.append(case["id"])
    if len(ids) != len(set(ids)):
        raise HarnessError("固定用例 ID 重复")
    return cases


async def _execute(kind, inputs, *, sessions, index, corpus):
    """只向被测路径传 input，不传 gold。返回无私有数据的合成测量。"""
    if kind == "rag":
        from eic.knowledge.retrieval import lexical_search, rrf_fuse
        from eic.vector.qdrant_reader import VectorHit
        documents = [VectorHit(item["id"], 0, {"content": item["content"]}) for item in corpus["documents"]]
        hits = lexical_search(inputs["query"], documents, limit=inputs["k"])
        if inputs.get("hybrid"):
            # 第二路仅为受控 rank fixture，不冒充真实向量/嵌入测量。
            hits = rrf_fuse([hits, list(reversed(hits))])[:inputs["k"]]
        return {"ranked": [hit.point_id for hit in hits],
                "citations": [{"claimId": hit.point_id, "sourceId": hit.point_id} for hit in hits]}
    if kind == "tool":
        from eic.ai.agent_graph import AgentBudget, execute_tool_calling_agent
        from eic.ai.tool_center import ToolCenter, ToolDefinition
        calls = []
        center = ToolCenter()
        center.register(ToolDefinition("calculator", "固定加法工具",
            lambda args: calls.append(1) or {"value": args["a"] + args["b"]},
            input_schema={"type": "object", "required": ["a", "b"], "additionalProperties": False,
                          "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}))
        counter = 0
        async def decide(state):
            nonlocal counter
            counter += 1
            if counter == 1 or inputs["mode"] == "budget":
                requests = [{"id": f"c{counter}", "name": "calculator",
                             "arguments": {"a": inputs["a"], "b": inputs["b"]}}]
                if inputs["mode"] in {"multi", "repeat"}:
                    requests.append({"id": "c2" if inputs["mode"] == "multi" else "c1",
                                     "name": "calculator", "arguments": {
                                         "a": inputs["a"], "b": inputs["b"] + (inputs["mode"] == "multi")}})
                return {"tool_calls": requests}
            return {"content": ",".join(str(item["result"]["value"]) for item in state["tool_results"])}
        state = await execute_tool_calling_agent(
            "固定加法请求", model_decider=decide, tool_center=center, allowed_tools=["calculator"],
            budget=AgentBudget(max_steps=inputs.get("maxSteps", 8), max_tool_calls=10),
        )
        return {"state": state, "handlerCalls": len(calls), "nodeTimings": node_metrics(state["trace_events"])}
    if kind == "context":
        from eic.ai.context_manifest import prepare_context
        history = []
        for n in range(inputs["turns"]):
            history.extend([{"role": "user", "content": f"u{n}-" + "问" * inputs["size"]},
                            {"role": "assistant", "content": f"a{n}-" + "答" * inputs["size"]}])
        system = [{"role": "system", "content": "固定系统约束"}]
        messages, info = prepare_context(system, history, "当前问题", inputs["limit"])
        kept = messages[1:-1]
        return {"questionRetained": messages[-1] == {"role": "user", "content": "当前问题"},
                "systemRetained": messages[0] == system[0],
                "completeTurns": len(kept) % 2 == 0 and all(
                    kept[n]["role"] == "user" and kept[n+1]["role"] == "assistant"
                    for n in range(0, len(kept), 2)),
                "withinBudget": info["inputEstimate"] <= inputs["limit"],
                "inputEstimate": info["inputEstimate"], "manifestHash": info["contextManifest"]["hash"]}
    if kind == "memory":
        from sqlalchemy import delete
        from eic.ai.memory_context import recall_memory
        from eic.models import AgentMemory
        async with sessions() as session:
            await session.execute(delete(AgentMemory))
            values = dict(tenant_id=1, owner_id=11, agent_id=None, memory_type="long_term")
            session.add(AgentMemory(id=501, content=inputs["query"] + " current", memory_key="fact",
                                    source_priority=2, **values))
            if inputs["mode"] in {"conflict", "isolation"}:
                session.add(AgentMemory(id=502, content=inputs["query"] + " obsolete", memory_key="fact",
                                        source_priority=1, **values))
            if inputs["mode"] == "isolation":
                session.add(AgentMemory(id=503, content=inputs["query"] + " other user",
                                        **{**values, "owner_id": 12}))
                session.add(AgentMemory(id=504, content=inputs["query"] + " expired",
                                        expires_at=datetime.now(timezone.utc) - timedelta(days=1), **values))
                session.add(AgentMemory(id=505, content=inputs["query"] + " deleted", status="deleted", **values))
                session.add(AgentMemory(id=506, content=inputs["query"] + " other session",
                                        **{**values, "memory_type": "session", "session_id": 102}))
            await session.commit()
            rows, metadata = await recall_memory(session, tenant_id=1, owner_id=11, session_id=101,
                                                  agent_id=None, query=inputs["query"])
            return {"selected": ["current" if item["id"] == 501 else "unexpected" for item in rows],
                    "mode": metadata["mode"]}
    if kind == "recovery":
        from sqlalchemy import select
        from eic.core.agent_runtime import AgentRuntimeStore
        from eic.models import AgentCheckpoint, AgentEvent, AgentToolCall
        async with sessions() as session:
            store = AgentRuntimeStore()
            run = await store.create_run(session, tenant_id=1, session_id=None,
                                         request_id=f"case-{index}", graph_version="fixture-v1")
            await store.append_event(session, run_id=run.run_id, node_name="context",
                                     event_type="node_completed", payload={"status": "ready"})
            checkpoint = await store.append_checkpoint(session, run_id=run.run_id, node_name="context",
                state={"providerStarted": inputs["mode"] == "uncertain"},
                resume_policy="terminal" if inputs["mode"] == "terminal" else "safe_replay")
            if inputs["mode"] == "corrupt":
                checkpoint.state_hash = "0" * 64
            await session.commit()
            decision = await store.decide_recovery(session, run_id=run.run_id,
                expected_graph_version="different" if inputs["mode"] == "version" else "fixture-v1")
            events = list(await session.scalars(select(AgentEvent).where(AgentEvent.run_id == run.run_id)))
            checkpoints = list(await session.scalars(select(AgentCheckpoint).where(AgentCheckpoint.run_id == run.run_id)))
            trace = verify_runtime_trace(run, events, checkpoints, [])
            result = {"action": decision.action, "reason": decision.reason, "traceValid": trace["valid"]}
        if inputs["mode"] == "safe":
            from .crash_probe import crash_probe
            result["crashProbe"] = await asyncio.to_thread(crash_probe)
        return result
    if kind == "retry":
        from eic.ai.providers import ModelProviderError
        from eic.ai.reliable_calls import reliable_stream
        from eic.core.config import get_settings
        from eic.models import AIModel
        settings = get_settings()
        before = {key: getattr(settings, key) for key in [
            "dependency_controls_enabled", "admission_enabled", "provider_rate_limit_retries",
        ]}
        settings.dependency_controls_enabled, settings.admission_enabled = True, False
        settings.provider_rate_limit_retries = 1
        calls, reason, chunks, metadata = [], "", [], {}
        async def provider():
            calls.append(1)
            if inputs["mode"] == "partial":
                yield "partial"
            if inputs["mode"] != "healthy" and len(calls) == 1:
                error = {"rate_limit": "provider_rate_limited", "uncertain": "provider_outcome_uncertain",
                         "partial": "provider_rate_limited", "invalid": "provider_parameters_rejected"}[inputs["mode"]]
                raise ModelProviderError("合成故障", reason=error, retryable=error == "provider_rate_limited")
            yield "fixture"
        model = AIModel(id=1000+index, tenant_id=1, provider="openai", endpoint="http://fixture.invalid",
                        model_name="fixture", model_type="chat", max_input_tokens=1024, max_output_tokens=512)
        try:
            chunks = [item async for item in reliable_stream(provider, model, sessions=sessions, metadata=metadata)]
        except ModelProviderError as error:
            reason = error.reason
        finally:
            for key, value in before.items():
                setattr(settings, key, value)
        return {"calls": len(calls), "answer": "".join(chunks), "reason": reason,
                "retryCount": metadata.get("retryCount", 0)}
    raise HarnessError("未知固定用例类型")


def score_case(case, actual, elapsed_ms):
    gold = case["gold"]
    observation = {"latencyMs": elapsed_ms}
    ranking, citations, observations = None, None, []
    if case["kind"] == "rag":
        ranking = ranking_metrics(actual["ranked"], gold["relevance"], case["input"]["k"])
        citations = citation_metrics(actual["citations"],
            {key: [key] for key in gold["relevance"]}, actual["ranked"])
        passed = ranking["recallAtK"] == 1 and citations["citationRecall"] == 1
        observation.update(taskSuccessRate=passed, answerCorrectness=None, answerCompleteness=None)
    elif case["kind"] == "tool":
        observations = agent_observations(actual["state"], expected_answer=gold["answer"],
            expected_tools=set(gold["tools"]), elapsed_ms=elapsed_ms, expected_stop=gold.get("stop", "answer"))
        passed = observations[0]["taskSuccessRate"] and actual["handlerCalls"] == gold["handlerCalls"]
        observations[0]["taskSuccessRate"] = passed
        if case["input"]["mode"] == "repeat":
            observations[0]["duplicateSideEffectRate"] = actual["handlerCalls"] > gold["handlerCalls"]
    elif case["kind"] == "memory":
        passed = actual["selected"] == gold["selected"]
        observation["taskSuccessRate"] = passed
    elif case["kind"] == "context":
        passed = all(actual[key] == value for key, value in gold.items()) and actual["withinBudget"]
        observation.update(taskSuccessRate=passed, inputEstimate=actual["inputEstimate"])
    elif case["kind"] == "recovery":
        passed = actual["action"] == gold["action"]
        observation.update(taskSuccessRate=passed, crashRecoveryRate=(
            actual["crashProbe"]["restored"] if "crashProbe" in actual else None),
                           uncertainOutcomeRate=case["input"]["mode"] == "uncertain")
        if "crashProbe" in actual:
            passed = passed and actual["crashProbe"]["restored"]
            observation["taskSuccessRate"] = passed
    else:
        passed = all(actual[key] == value for key, value in gold.items())
        observation.update(taskSuccessRate=passed, retryCount=actual["retryCount"],
                           uncertainOutcomeRate=actual["reason"] == "provider_outcome_uncertain",
                           fallbackSuccessRate=passed if case["input"]["mode"] == "rate_limit" else None)
    if not observations:
        observations = [observation]
    result = {"id": case["id"], "stratum": case["stratum"], "kind": case["kind"],
              "inputSha256": digest(canonical(case["input"]).encode()),
              "passed": bool(passed), "ranking": ranking, "citations": citations,
              "observations": observations, "errorCode": "", "elapsedMs": elapsed_ms,
              "measurements": safe_measurements(case["kind"], actual)}
    if actual.get("nodeTimings") is not None:
        result["nodeTimings"] = actual["nodeTimings"]
    if actual.get("crashProbe") is not None:
        result["crashProbe"] = actual["crashProbe"]
    return result


def safe_measurements(kind, actual):
    if kind == "tool":
        state = actual["state"]
        return {"answerSha256": digest(state["answer"].encode()), "stopReason": state["stop_reason"],
                "steps": state["agent_step_count"], "toolCalls": state["tool_call_count"],
                "budget": state["tool_budget"], "handlerCalls": actual["handlerCalls"],
                "toolResults": [{key: row[key] for key in [
                    "callId", "toolName", "status", "errorCode", "effectStatus", "retryCount", "replayed",
                ] if key in row} for row in state["tool_results"]],
                "nodeTimings": actual.get("nodeTimings", {})}
    if kind == "retry":
        return {**{key: actual[key] for key in ["calls", "reason", "retryCount"]},
                "answerSha256": digest(actual["answer"].encode())}
    return actual


def validate_case_measurement(case, row):
    if row.get("errorCode"):
        if row.get("passed") is not False:
            raise HarnessError("失败测量被标为成功")
        return
    measurement = row.get("measurements")
    if not isinstance(measurement, dict):
        raise HarnessError("用例缺失可重算的原始测量")
    elapsed = row.get("elapsedMs")
    from .metrics import number
    number(elapsed, "elapsedMs")
    if case["kind"] == "tool":
        expected_answer = case["gold"]["answer"]
        matches = measurement["answerSha256"] == digest(expected_answer.encode())
        actual = {"state": {"answer": expected_answer if matches else "__answer_digest_mismatch__",
                           "stop_reason": measurement["stopReason"], "tool_results": measurement["toolResults"],
                           "tool_budget": measurement["budget"], "agent_step_count": measurement["steps"],
                           "tool_call_count": measurement["toolCalls"], "trace_events": []},
                  "handlerCalls": measurement["handlerCalls"], "nodeTimings": measurement["nodeTimings"]}
    elif case["kind"] == "retry":
        expected_answer = case["gold"].get("answer", "")
        actual = {**measurement, "answer": expected_answer if measurement["answerSha256"] == digest(
            expected_answer.encode()) else "__answer_digest_mismatch__"}
    else:
        actual = measurement
    expected = score_case(case, actual, elapsed)
    for key in ["passed", "ranking", "citations", "observations", "nodeTimings", "crashProbe"]:
        if row.get(key) != expected.get(key):
            raise HarnessError("逐项评分与原始测量不一致：" + case["id"])


def summarize(rows):
    report = agent_metrics([item for row in rows for item in row["observations"]])
    for name in ["hitAtK", "recallAtK", "mrr", "ndcgAtK"]:
        values = [row["ranking"][name] for row in rows if row["ranking"] is not None]
        report["metrics"][name] = average(values)
        report["sampleCounts"][name] = sum(value is not None for value in values)
    for name in ["citationPrecision", "citationRecall"]:
        values = [row["citations"][name] for row in rows if row["citations"] is not None]
        report["metrics"][name] = average(values)
        report["sampleCounts"][name] = sum(value is not None for value in values)
    return {**report, "caseCount": len(rows), "passedCount": sum(row["passed"] for row in rows),
            "failedCaseIds": [row["id"] for row in rows if not row["passed"]]}


async def run_suite(document, corpus):
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from eic.core.ids import initialize_allocator
    from eic.models import Base, ChatSession, Tenant, User
    cases = validate_cases(document)
    if corpus.get("schemaVersion") != 1 or not corpus.get("documents"):
        raise HarnessError("固定 corpus 为空")
    rows = []
    with tempfile.TemporaryDirectory(prefix="eic-harness-") as directory:
        engine = create_async_engine("sqlite+aiosqlite:///" + (Path(directory) / "fixture.db").as_posix())
        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as session:
                session.add(Tenant(id=1, code=1, name="synthetic"))
                await session.flush()
                for user in [11, 12]:
                    session.add(User(id=user, tenant_id=1, username=f"fixture-{user}", password_hash="unused"))
                await session.flush()
                for sid in [101, 102]:
                    session.add(ChatSession(id=sid, tenant_id=1, owner_id=11, title="synthetic"))
                await initialize_allocator(session)
                await session.commit()
            for index, case in enumerate(cases):
                started = time.perf_counter()
                try:
                    actual = await asyncio.wait_for(_execute(case["kind"], case["input"],
                        sessions=sessions, index=index, corpus=corpus), timeout=20)
                    rows.append(score_case(case, actual, (time.perf_counter() - started) * 1000))
                except Exception as error:
                    # 不把异常正文、连接串或堆栈写进公开结果；失败 ID 必须保留。
                    rows.append({"id": case["id"], "stratum": case["stratum"], "kind": case["kind"],
                                 "inputSha256": digest(canonical(case["input"]).encode()), "passed": False,
                                 "ranking": None, "citations": None,
                                 "observations": [{"taskSuccessRate": False, "latencyMs": (time.perf_counter()-started)*1000}],
                                 "errorCode": "case_error_" + type(error).__name__})
        finally:
            await engine.dispose()
    return {"schemaVersion": 1, "scope": document["scope"], "cases": rows,
            "summary": summarize(rows),
            "strata": {name: summarize([row for row in rows if row["stratum"] == name]) for name in STRATA},
            "caseSetSha256": digest(canonical(document).encode()), "corpusSha256": digest(canonical(corpus).encode()),
            "fixtureCounts": dict(Counter(case["kind"] for case in cases))}

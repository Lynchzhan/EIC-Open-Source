"""会话统计、权限、真实 usage、失败/取消和采集事务负控。"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS

from eic.core import observability as obs
from eic.core.conversation_observation import turns, summary, approval_wait
from eic.models import ChatMessage
from test_fv2_observability_query import setup, api, insert, NOW


def message(key="r1", id=900, status="completed"):
    return NS(id=id, request_id=key, status=status, created_at=NOW)


def event(key="span1", **fields):
    return NS(span_id=key, category="model_call", request_id="r1", stage="model",
              trace_id="trace1", usage_source="provider_usage", input_tokens=4,
              output_tokens=2, total_tokens=6, cached_tokens=0, attempt=1,
              status="success", event_key=key, occurred_at=NOW, duration_ms=100,
              **fields)


def test_distinct_rounds_spans_zero_and_missing_not_estimated():
    calls = [event(), event()]
    calls.append(NS(**{**vars(event("retry")), "status":"error", "attempt":2,
                      "total_tokens":None, "input_tokens":None, "output_tokens":None}))
    calls.append(NS(**{**vars(event("embed")), "stage":"embedding", "total_tokens":0}))
    items = turns([message(), message(id=901), message("r2", 902, "cancelled")], calls, [], [], [])
    assert len(items) == 2 and items[0]["modelCallCount"] == 3
    assert items[0]["usage"]["totalTokens"] == {"value":6,"providedCalls":2,"population":3,"complete":False}
    assert items[0]["usageByKind"]["embedding"]["totalTokens"]["value"] == 0
    assert items[0]["retryCount"] == 1 and items[0]["processingMs"] is None
    total = summary(items)
    assert total["roundCount"] == 2 and total["cancelledRounds"] == 1
    assert not total["usage"]["totalTokens"]["complete"] and total["processingMs"] is None


def test_approval_wait_decision_consumed_and_overlap():
    rows = [NS(run_id="a", event_type="approval_pending", created_at=NOW + timedelta(seconds=1)),
            NS(run_id="a", event_type="approval_decision", created_at=NOW + timedelta(seconds=4)),
            NS(run_id="a", event_type="approval_consumed", created_at=NOW + timedelta(seconds=5))]
    assert approval_wait(rows, NOW + timedelta(seconds=10)) == (3000, True)
    run = NS(run_id="a", request_id="r1", trace_id="trace1", finished_at=NOW + timedelta(seconds=10))
    root = NS(**{**vars(event("root")), "stage":"chat_turn", "category":"request",
                 "status":"completed", "occurred_at":run.finished_at, "duration_ms":10000})
    turn = turns([message()], [root, event()], [run], rows, [])[0]
    assert turn["durationMs"] == 10000 and turn["processingMs"] == 7000
    assert turn["approvalWaitMs"] == 3000 and turn["timingComplete"]
    historical = turns([message()], [event()], [run], rows, [])[0]
    assert historical["processingMs"] == 7000 and not historical["timingComplete"]


def test_session_api_current_owner_rbac_tenant_and_whole_summary(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            async with sessions() as db:
                db.add_all([ChatMessage(id=910, tenant_id=1, session_id=101, role="user",
                    request_id="r1", content="PRIVATE TEXT", status="completed", created_at=NOW),
                    ChatMessage(id=911, tenant_id=1, session_id=101, role="assistant",
                    request_id="r1", content="PRIVATE ANSWER", status="completed", created_at=NOW),
                    ChatMessage(id=912, tenant_id=1, session_id=101, role="user",
                    request_id="r3", content="PRIVATE TEXT", status="failed", created_at=NOW)])
                await db.commit()
            await insert(sessions, "call", "model_call", span_id="span1", request_id="r1",
                         chat_id="101", trace_id="trace1", total_tokens=6, usage_source="provider_usage")
            data = (await api(client, "Session", chatId=101, pageSize=1)).json()
            # 公用 fixture 中已有一轮 streaming 请求，整场统计也应保留。
            assert data["summary"]["roundCount"] == 3 and data["hasMore"]
            assert data["summary"]["usage"]["totalTokens"]["value"] == 6
            assert "PRIVATE" not in str(data) and "content" not in str(data)
            subset = (await api(client, "Session", chatId=101, requestIds="r3")).json()
            assert len(subset["data"]) == 1 and subset["summary"]["roundCount"] == 3
            assert (await api(client, "Session", chatId=102)).status_code == 404
            assert (await api(client, "Session", chatId=201, user=13, scope="tenant")).status_code == 404
            assert (await api(client, "Session", chatId=102, user=13, scope="tenant")).status_code == 200
            assert (await api(client, "Sessions", scope="tenant")).status_code == 403
            own = (await api(client, "Sessions", pageSize=10)).json()
            assert [item["chatId"] for item in own["data"]] == ["101"]
            matching = (await api(client, "Sessions", category="model_call")).json()
            assert [item["chatId"] for item in matching["data"]] == ["101"]
            excluded = (await api(client, "Sessions", modelId="missing")).json()
            assert excluded["data"] == []
            assert (await api(client, "Session", chatId=101,
                requestIds=",".join(str(i) for i in range(101)))).status_code == 422
    asyncio.run(run())


def test_turn_collection_commit_and_rollback(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, _):
            captured = []
            monkeypatch.setattr(obs.collector, "running", True)
            monkeypatch.setattr(obs.collector, "submit", lambda row: captured.append(row))
            token = obs.CONTEXT.set({"tenant_id":1,"owner_key":obs.actor_key(1,11),"request_id":"r4"})
            try:
                async with sessions() as db:
                    row = ChatMessage(id=920, tenant_id=1, session_id=101, role="user", request_id="r4", content="secret", status="streaming")
                    db.add(row)
                    await db.commit()
                    row.status = "completed"
                    await db.commit()
                roots = [item for item in captured if item and item["stage"] == "chat_turn"]
                assert [item["status"] for item in roots] == ["started","completed"]
                assert roots[1]["duration_ms"] >= 0 and "secret" not in str(roots)
                captured.clear()
                async with sessions() as db:
                    db.add(ChatMessage(id=921, tenant_id=1, session_id=101, role="user", request_id="rollback", content="x", status="streaming"))
                    await db.flush()
                    await db.rollback()
                assert not captured
            finally:
                obs.CONTEXT.reset(token)
    asyncio.run(run())


def test_provider_invocation_parent_usage_and_cancel(monkeypatch):
    captured = []
    monkeypatch.setattr(obs.collector, "submit", lambda row: captured.append(row))
    model = NS(tenant_id=1, id=99)
    @obs.observe_model_call("embedding")
    async def call(model, usage_sink=None):
        usage_sink.update(_invoked=True, total_tokens=0, usage_source="provider_usage")
        return 1
    @obs.observe_model_call("rerank")
    async def cancel(model, usage_sink=None):
        usage_sink["_invoked"] = True
        raise asyncio.CancelledError()
    async def run():
        token = obs.CONTEXT.set({"tenant_id":1,"span_id":"parent","trace_id":"trace1"})
        try:
            assert await call(model, usage_sink=None) == 1
            try:
                await cancel(model)
            except asyncio.CancelledError:
                pass
        finally:
            obs.CONTEXT.reset(token)
    asyncio.run(run())
    assert captured[0]["parent_span_id"] == "parent" and captured[0]["total_tokens"] == 0
    assert captured[1]["status"] == "cancelled" and captured[1]["usage_source"] == "not_provided"


def test_provider_response_to_persisted_session_statistics(monkeypatch, tmp_path):
    import httpx
    from eic.ai.embeddings import embed_texts
    from eic.ai.rerank import rerank_hits
    from eic.models import AIModel
    from eic.vector.qdrant_reader import VectorHit

    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            await obs.collector.start(sessions)
            token = obs.CONTEXT.set({"tenant_id":1, "owner_key":obs.actor_key(1, 11),
                "request_id":"r1", "chat_id":"101", "trace_id":"provider-test", "span_id":"root"})
            try:
                async with sessions() as db:
                    row = ChatMessage(id=930, tenant_id=1, session_id=101, role="user",
                        request_id="r1", content="SECRET PROMPT", status="streaming", created_at=NOW)
                    db.add(row)
                    await db.commit()
                    model = AIModel(id=99, tenant_id=1, enabled=True, provider="openai_compatible",
                        model_type="embedding", endpoint="http://provider.invalid/v1", model_name="fixture",
                        embedding_dimensions=2)
                    response = {"data":[{"index":0, "embedding":[1, 2]}],
                        "usage":{"prompt_tokens":7, "total_tokens":7}}
                    async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json=response))) as provider:
                        assert await embed_texts(model, ["SECRET PROMPT"], client=provider) == [[1, 2]]
                    model.model_type = "rerank"
                    response = {"results":[{"index":0, "relevance_score":.9}],
                        "usage":{"input_tokens":3, "total_tokens":3}}
                    async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json=response))) as provider:
                        await rerank_hits(model, "SECRET PROMPT",
                            [VectorHit("a", .1, {"content":"SECRET DOCUMENT"})], limit=1, client=provider)
                    row.status = "completed"
                    await db.commit()
                await obs.collector.flush()
                result = (await api(client, "Session", chatId=101, requestIds="r1")).json()
                turn = result["data"][0]
                assert turn["modelCallCount"] == 2 and turn["usage"]["totalTokens"]["value"] == 10
                assert turn["usageByKind"]["embedding"]["totalTokens"]["value"] == 7
                assert turn["usageByKind"]["rerank"]["totalTokens"]["value"] == 3
                assert turn["timingComplete"] and turn["usage"]["totalTokens"]["complete"]
                trace = (await api(client, "Trace", traceId="provider-test")).json()
                calls = [item for item in trace["spans"] if item["category"] == "model_call"]
                assert len(calls) == 2 and all(item["parentSpanId"] == "root" for item in calls)
                assert "SECRET" not in str(result) + str(trace)
            finally:
                obs.CONTEXT.reset(token)
                await obs.collector.stop()
    asyncio.run(run())


def test_complete_usage_requires_a_collected_turn_boundary():
    old = turns([message()], [event()], [], [], [])[0]
    assert old["usage"]["totalTokens"]["value"] == 6
    assert not old["usage"]["totalTokens"]["complete"]
    assert not old["usageByKind"]["model"]["totalTokens"]["complete"]

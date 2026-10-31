"""首批真实验收缺口的离线回归，不发送网络请求。"""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from eic.ai.context_manifest import knowledge_evidence_content, prepare_context
from eic.core.langfuse_contract import build_ingestion_events
from eic.core.langfuse_otlp import build_completed_spans
from eic.core import observability as obs


def event(**overrides):
    return dict(tenant_id=1, trace_id="fixture", event_key="fixture-event",
        span_id="a" * 32, parent_span_id="b" * 32, occurred_at=datetime.now(UTC),
        category="model_call", source="provider_response", stage="rerank",
        status="success", duration_ms=12, summary={"modelName": "org/reranker-v1"},
        **overrides)


@pytest.mark.parametrize("stage,source,name", [
    ("model", "provider_stream", "eic.model_call"),
    ("embedding", "provider_response", "eic.embedding"),
    ("rerank", "provider_response", "eic.rerank"),
])
def test_model_name_and_distinct_generation_names(stage, source, name):
    row = event()
    row.update(stage=stage, source=source)
    legacy = build_ingestion_events(row, environment="test", namespace="fixture")[1]
    assert legacy["body"]["model"] == "org/reranker-v1"
    assert legacy["body"]["name"] == name
    span = build_completed_spans(row, environment="test", namespace="fixture")[0]["body"]
    attrs = {item["key"]: item["value"] for item in span["attributes"]}
    assert attrs["gen_ai.request.model"] == {"stringValue": "org/reranker-v1"}
    assert attrs["langfuse.observation.type"] == {"stringValue": "generation"}


@pytest.mark.parametrize("value", [None, "", "secret\nbody", {"secret": "value"}, "x" * 121])
def test_invalid_model_name_is_not_exported(value):
    row = event()
    row["summary"] = {"modelName": value, "apiKey": "must-not-export"}
    body = build_ingestion_events(row, environment="test", namespace="fixture")[1]["body"]
    assert "model" not in body
    assert "must-not-export" not in str(body)


@pytest.mark.parametrize("outcome,status", [(None, "success"), (ValueError, "error"),
                                          (asyncio.CancelledError, "cancelled")])
def test_stage_context_restored_and_parent_preserved(monkeypatch, outcome, status):
    rows = []
    monkeypatch.setattr(obs.collector, "submit", rows.append)
    context = {"tenant_id": 1, "trace_id": "fixture", "span_id": "b" * 32}
    token = obs.CONTEXT.set(context)
    try:
        def run():
            with obs.observe_retrieval_stage("dense_recall", tenant_id=1):
                assert obs.CONTEXT.get()["span_id"] != context["span_id"]
                if outcome:
                    raise outcome()
        if outcome:
            with pytest.raises(outcome):
                run()
        else:
            run()
        assert obs.CONTEXT.get() == context
        assert rows[0]["status"] == status
        assert rows[0]["parent_span_id"] == context["span_id"]
        body = build_ingestion_events(rows[0], environment="test", namespace="fixture")[1]["body"]
        assert body["name"] == "eic.dense_recall"
    finally:
        obs.CONTEXT.reset(token)


def test_evidence_name_and_pages_are_budgeted_with_content():
    item = {"documentName": "acceptance.pdf", "documentId": 3, "chunkId": "c1",
            "pageNumbers": [2], "content": "09:37 CEDAR-742"}
    content = knowledge_evidence_content(item)
    assert '"documentName": "acceptance.pdf"' in content
    assert '"pageNumbers": [2]' in content
    assert item["content"] in content
    messages, budget = prepare_context([], [], "when", 4096,
        optional=[{"source": "knowledge", "id": "c1", "priority": 70, "content": content}])
    assert any(message["content"] == content for message in messages)
    assert budget["contextManifest"]["entries"][0]["retained"]


def test_unrecognized_retrieval_stage_is_not_exported():
    row = event()
    row.update(category="rag_retrieval", source="knowledge_ops", stage="private-query-text")
    assert build_completed_spans(row, environment="test", namespace="fixture") == []


def test_actual_retrieval_graph_emits_six_measured_nodes(monkeypatch):
    from eic.knowledge import ops
    from eic.knowledge.retrieval_graph import run_retrieval_graph
    rows = []
    monkeypatch.setattr(obs.collector, "submit", rows.append)

    async def authorize(*args, **kwargs):
        return {}

    async def dense(*args, **kwargs):
        return {"dense_hits": [], "dense_status": "not_configured"}

    async def sparse(*args, **kwargs):
        return {"sparse_hits": []}

    async def fuse(*args, **kwargs):
        return {"fusion_hits": []}

    async def rerank(*args, **kwargs):
        return {"reranked_hits": [], "rerank_status": "skipped"}

    async def finalize(*args, **kwargs):
        return {"results": [], "rerankStatus": "skipped"}

    for name, function in [("_authorize_retrieval", authorize), ("_recall_dense", dense),
            ("_recall_sparse", sparse), ("_fuse_retrieval", fuse),
            ("_rerank_retrieval", rerank), ("_finalize_retrieval", finalize)]:
        monkeypatch.setattr(ops, name, function)

    async def run():
        token = obs.CONTEXT.set({"tenant_id": 1, "trace_id": "fixture", "span_id": "b" * 32})
        try:
            return await run_retrieval_graph(session=None, tenant_id=1,
                base=SimpleNamespace(id=2, tenant_id=1), query="private-query",
                extra=None, filters=None, limit=None, before_external=None)
        finally:
            obs.CONTEXT.reset(token)

    assert asyncio.run(run()) == {"results": [], "rerankStatus": "skipped"}
    assert [row["stage"] for row in rows] == [
        "authorize", "dense_recall", "sparse_recall", "fusion", "rerank", "citations"]
    assert len({row["span_id"] for row in rows}) == 6
    assert all(row["parent_span_id"] == "b" * 32 for row in rows)
    assert "private-query" not in str(rows)
    assert all(build_completed_spans(row, environment="test", namespace="fixture") for row in rows)
    rerank = next(row for row in rows if row["stage"] == "rerank")
    assert rerank["summary"]["rerankStatus"] == "skipped"
    exported = build_ingestion_events(rerank, environment="test", namespace="fixture")[1]["body"]
    assert exported["metadata"]["rerankStatus"] == "skipped"


@pytest.mark.parametrize("kind", ["model", "embedding", "rerank"])
def test_model_decorators_capture_configured_name(monkeypatch, kind):
    from eic.ai import runtime_policy
    rows = []
    monkeypatch.setattr(obs.collector, "submit", rows.append)

    async def begin(*args, **kwargs):
        return None

    async def finish(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime_policy, "begin_call", begin)
    monkeypatch.setattr(runtime_policy, "finish_call", finish)
    model = SimpleNamespace(id=3, tenant_id=1, model_name="fixture-model")

    async def response(model, *, usage_sink):
        usage_sink["_invoked"] = True
        return []

    async def stream(model, *, usage_sink):
        usage_sink["_invoked"] = True
        yield "fixture"

    async def run():
        if kind == "model":
            return [part async for part in obs.observe_model_stream(stream)(model)]
        return await obs.observe_model_call(kind)(response)(model)

    asyncio.run(run())
    assert len(rows) == 1
    assert rows[0]["summary"]["modelName"] == "fixture-model"
    assert rows[0]["stage"] == kind

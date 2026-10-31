"""聚合污染与未知身份负控，不连接外部数据库或模型。"""
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from eic.core import observability as obs, observability_query as query
from eic.models import ObservabilityEvent


def row(key, category, **fields):
    now = datetime.now(UTC)
    return ObservabilityEvent(**obs.observation(category, "model", "success", tenant_id=1,
                             event_key=key, occurred_at=now, **fields), collected_at=now)


def test_unknown_call_identity_is_not_invented_from_event_row():
    metrics = query.summarize([row("unknown-request", "request"), row("unknown-model", "model_call"),
                               row("unknown-run", "agent_run")])
    assert metrics["requests"]["count"] == 0
    assert metrics["modelCalls"]["count"] == 0
    assert metrics["agentRuns"]["count"] == 0
    assert metrics["coverage"]["unidentifiedCallRows"] == 3


def test_estimated_or_unknown_usage_source_never_becomes_real_total():
    metrics = query.summarize([row("estimated", "model_call", span_id="physical",
                                  total_tokens=123, usage_source="estimated")])
    assert metrics["usage"]["totalTokens"]["value"] is None
    assert metrics["usage"]["totalTokens"]["providedCalls"] == 0


@pytest.mark.parametrize("params", [{"start": ""}, {"end": ""}])
def test_explicit_empty_time_is_not_default(params):
    with pytest.raises(HTTPException) as error:
        query.window(**params)
    assert error.value.detail["code"] == "observation_timezone_required"


def test_tool_audit_identity_and_real_zero_remain_measurable():
    metrics = query.summarize([row("tool-audit:1:123", "tool_call", source="tool_audits", duration_ms=0),
                               row("rag", "rag_retrieval", span_id="rag", summary={"topK": 3, "resultCount": 0})])
    assert metrics["tools"]["count"] == 1
    assert metrics["tools"]["latency"]["p95Ms"] == 0
    assert metrics["rag"]["byTopK"] == {"3": 1}
    assert metrics["rag"]["sourceDistribution"] is None

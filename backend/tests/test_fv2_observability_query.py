"""真实合成 SQLite/ASGI 请求：权限、窗口、分母、缺失值和查询负控。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select, update

from eic import main
from eic.core import observability as obs, observability_query as query
from eic.core.database import get_session
from auth_fixture import signed_headers
from eic.models import (
    AgentRun, ChatSession, ObservabilityEvent, Permission, Role, RolePermission, Tenant, User, UserRole,
)
from test_id_generation_safety import fixture_db

NOW = datetime.now(UTC).replace(microsecond=0)
BEGIN = NOW - timedelta(hours=2)
END = NOW + timedelta(hours=1)
WINDOW = {"start": BEGIN.isoformat(), "end": END.isoformat()}


@asynccontextmanager
async def setup(monkeypatch, tmp_path):
    async with fixture_db(monkeypatch, tmp_path, is_super_admin=False) as sessions:
        async with sessions() as db:
            db.add(Tenant(id=2, code=2, name="foreign"))
            db.add_all([
                User(id=12, tenant_id=1, username="other", password_hash="unused"),
                User(id=13, tenant_id=1, username="admin", password_hash="unused", is_super_admin=True),
                User(id=21, tenant_id=2, username="foreign", password_hash="unused", is_super_admin=True),
                Permission(id="ai.chat.read", code="ai.chat.read", name="fixture", category="ai"),
                Role(id=501, tenant_id=1, name="fixture"),
            ])
            await db.flush()
            db.add_all([
                RolePermission(role_id=501, permission_id="ai.chat.read"),
                UserRole(user_id=11, role_id=501), UserRole(user_id=12, role_id=501),
                ChatSession(id=102, tenant_id=1, owner_id=12),
                ChatSession(id=201, tenant_id=2, owner_id=21),
                AgentRun(run_id="own-run", tenant_id=1, request_id="r1", session_id=101,
                         trace_id="trace-own", graph_version="fixture"),
                AgentRun(run_id="other-run", tenant_id=1, request_id="r2", session_id=102,
                         trace_id="trace-other", graph_version="fixture"),
            ])
            await db.commit()

        async def session_override():
            async with sessions() as db:
                yield db

        main.app.dependency_overrides[get_session] = session_override
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url="http://fixture.invalid") as client:
                yield sessions, client
        finally:
            main.app.dependency_overrides.pop(get_session, None)


def headers(user=11, tenant=1):
    return signed_headers(user, tenant, "fixture", "unused")


async def insert(sessions, key, category="request", status="success", owner=11, tenant=1, **fields):
    if category == "request":
        fields.setdefault("request_id", key)
    row = obs.observation(category, fields.pop("stage", "http"), status, tenant_id=tenant,
                          owner_id=owner, event_key=key, occurred_at=fields.pop("occurred_at", NOW),
                          **fields)
    async with sessions() as db:
        db.add(ObservabilityEvent(**row, collected_at=NOW + timedelta(seconds=1)))
        await db.commit()


async def api(client, path="Summary", user=11, tenant=1, **params):
    return await client.get("/api/Observability/" + path, params={**WINDOW, **params},
                            headers=headers(user, tenant))


def test_real_rbac_owner_tenant_and_unknown_background_fail_closed(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            await insert(sessions, "own", request_id="r1", chat_id="101", run_id="own-run")
            await insert(sessions, "other", owner=12, request_id="r2", chat_id="102", trace_id="trace-other")
            await insert(sessions, "foreign", owner=21, tenant=2, trace_id="trace-foreign")
            await insert(sessions, "unowned", owner=None)
            await insert(sessions, "bad-chat", chat_id="102")
            await insert(sessions, "bad-run", run_id="other-run")
            response = await api(client, "Events")
            assert response.status_code == 200
            assert [row["eventKey"] for row in response.json()["data"]] == ["own"]
            assert (await api(client)).json()["metrics"]["requests"]["count"] == 1
            assert (await api(client, scope="tenant")).status_code == 403
            assert (await api(client, user=13, tenantId=2, scope="tenant")).status_code == 403
            assert (await api(client, user=13, scope="tenant")).json()["metrics"]["requests"]["count"] == 5
            for trace in ("trace-other", "trace-foreign", "unknown"):
                denied = await api(client, "Trace", traceId=trace)
                assert denied.status_code == 404
                assert denied.json()["detail"]["errMsg"] == "Trace 不存在或无权访问"
            assert (await client.get("/api/Observability/Summary")).status_code == 401
    asyncio.run(run())


@pytest.mark.parametrize("change", ["permission", "active", "tenant", "admin"])
def test_revocation_and_stale_token_rechecked_each_request(monkeypatch, tmp_path, change):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            user = 13 if change == "admin" else 11
            params = {"scope": "tenant"} if change == "admin" else {}
            assert (await api(client, user=user, **params)).status_code == 200
            async with sessions() as db:
                if change == "permission":
                    await db.execute(delete(RolePermission))
                elif change == "tenant":
                    await db.execute(update(Tenant).where(Tenant.id == 1).values(active=False))
                else:
                    await db.execute(update(User).where(User.id == user).values(
                        **({"is_super_admin": False} if change == "admin" else {"is_active": False})))
                await db.commit()
            assert (await api(client, user=user, **params)).status_code in {401, 403}
    asyncio.run(run())


def test_current_chat_ownership_not_historical_owner_hash(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            await insert(sessions, "owned", chat_id="101", run_id="own-run")
            assert len((await api(client, "Events")).json()["data"]) == 1
            async with sessions() as db:
                await db.execute(update(ChatSession).where(ChatSession.id == 101).values(owner_id=12))
                await db.commit()
            assert not (await api(client, "Events")).json()["data"]
    asyncio.run(run())


@pytest.mark.parametrize("params", [
    {"start": "2026-10-01"}, {"end": "2026-10-06T00:00:00"},
    {"start": "not-a-date"}, {"start": END.isoformat(), "end": BEGIN.isoformat()},
    {"start": (NOW-timedelta(days=31)).isoformat(), "end": NOW.isoformat()},
    {"pageSize": 201}, {"pageSize": 0}, {"page": 0}, {"page": 202, "pageSize": 50},
    {"category": "not-a-category"}, {"scope": "global"},
])
def test_invalid_window_pagination_and_filters_rejected(monkeypatch, tmp_path, params):
    async def run():
        async with setup(monkeypatch, tmp_path) as (_, client):
            assert (await api(client, "Events", **params)).status_code == 422
    asyncio.run(run())


def test_timezone_half_open_boundary_and_stable_pagination(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            for key, at in (("before", BEGIN-timedelta(microseconds=1)), ("start", BEGIN),
                            ("mid-a", NOW), ("mid-b", NOW), ("end", END)):
                await insert(sessions, key, occurred_at=at)
            params = {"start": BEGIN.astimezone(__import__("datetime").timezone(timedelta(hours=8))).isoformat(),
                      "end": END.isoformat(), "pageSize": 2}
            first = (await api(client, "Events", **params)).json()
            second = (await api(client, "Events", page=2, **params)).json()
            assert [x["eventKey"] for x in first["data"]] == ["mid-b", "mid-a"]
            assert [x["eventKey"] for x in second["data"]] == ["start"]
            assert first["hasMore"] and not second["hasMore"]
            assert first["window"]["start"] == BEGIN.isoformat()
            assert (await api(client, "Events")).json()["pageSize"] == 50
    asyncio.run(run())


def test_empty_metrics_do_not_claim_perfect_rate_free_cost_or_latency(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (_, client):
            metrics = (await api(client)).json()["metrics"]
            assert metrics["requests"]["count"] == 0 and metrics["requests"]["successRate"] is None
            assert metrics["requests"]["latency"]["p95Ms"] is None
            assert metrics["usage"]["totalTokens"]["value"] is None
            assert metrics["cache"]["hitRate"] is None
            assert metrics["budget"]["amount"] is None
            assert metrics["rag"]["citationRate"] is None
    asyncio.run(run())


def test_deduplicated_denominators_physical_retry_unknown_usage_and_zero(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            await insert(sessions, "request-running", status="running", request_id="r",
                         occurred_at=NOW-timedelta(seconds=1))
            await insert(sessions, "request-success", request_id="r", duration_ms=0)
            await insert(sessions, "request-error", status="error", request_id="err", duration_ms=100)
            for key, status, span, fields in (
                ("model-0", "success", "span0", {"input_tokens": 0, "total_tokens": 0,
                                               "usage_source": "provider_usage", "cache_outcome": "miss"}),
                ("model-copy", "success", "span0", {"input_tokens": 0, "total_tokens": 0,
                                                  "usage_source": "provider_usage", "cache_outcome": "miss"}),
                ("model-retry", "error", "span1", {}),
                ("model-ok", "success", "span2", {"total_tokens": 9, "cached_tokens": 2,
                                                "usage_source": "provider_usage", "cache_outcome": "hit"}),
            ):
                await insert(sessions, key, "model_call", status, span_id=span, **fields)
            result = (await api(client)).json()["metrics"]
            assert result["requests"]["count"] == 2 and result["requests"]["successRate"] == .5
            assert result["requests"]["latency"]["p50Ms"] == 50
            assert result["modelCalls"]["count"] == 3
            assert result["usage"]["totalTokens"] == {
                "value": 9, "providedCalls": 2, "population": 3, "complete": False, "source": "provider_reported"}
            assert result["usage"]["inputTokens"]["value"] == 0
            assert result["usage"]["savedTokens"]["value"] is None
            assert result["cache"]["measuredCalls"] == 2 and result["cache"]["hitRate"] == .5
    asyncio.run(run())


def test_dimension_filters_trace_first_error_retry_and_admin_redaction(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            await insert(sessions, "early", "model_call", "error", span_id="a", trace_id="trace",
                         agent_id="agent", model_id="model", workspace_id="workspace",
                         stage="model", attempt=1, error_code="provider_error",
                         occurred_at=NOW-timedelta(seconds=2))
            await insert(sessions, "later", "tool_call", "timeout", trace_id="trace", tool_name="calculator",
                         stage="tool", occurred_at=NOW-timedelta(seconds=1))
            await insert(sessions, "retry", "model_call", "success", trace_id="trace", span_id="b", attempt=2)
            # 模拟历史/导入数据被污染；查询详情仍二次白名单清理。
            async with sessions() as db:
                item = await db.get(ObservabilityEvent, "early")
                item.summary = {"method": "GET", "route": "SECRET TOKEN", "authorization": "SECRET",
                                "httpStatus": 500}
                await db.commit()
            result = (await api(client, "Trace", traceId="trace", pageSize=1)).json()
            assert result["firstFailure"]["stage"] == "model"
            assert result["modelCallCount"] == 2 and result["retryCount"] == 1
            assert result["hasMore"] and len(result["data"]) == 1
            assert (await api(client, "Trace", traceId="trace", detail=True)).status_code == 403
            detail = (await api(client, "Trace", traceId="trace", detail=True, user=13, scope="tenant")).json()
            assert detail["data"][0]["summary"] == {"method": "GET", "httpStatus": 500}
            assert "SECRET" not in str(detail) and "owner_key" not in str(detail)
            for params in ({"agentId": "agent"}, {"modelId": "model"}, {"workspaceId": "workspace"},
                           {"status": "error"}, {"errorStage": "model"}):
                assert [x["eventKey"] for x in (await api(client, "Events", **params)).json()["data"]] == ["early"]
            assert [x["eventKey"] for x in (await api(client, "Events", toolName="calculator")).json()["data"]] == ["later"]
    asyncio.run(run())


def test_large_query_negative_control_and_collection_health_no_global_counters(monkeypatch, tmp_path):
    async def run():
        async with setup(monkeypatch, tmp_path) as (sessions, client):
            for key in ("a", "b", "c"):
                await insert(sessions, key)
            monkeypatch.setattr(query, "MAX_AGGREGATE_ROWS", 2)
            response = await api(client)
            assert response.status_code == 422 and response.json()["detail"]["code"] == "observation_query_too_large"
            assert "metrics" not in response.json()
            assert (await api(client, "CollectionHealth")).status_code == 403
            obs.collector.counters["written"] = 12345
            health = (await api(client, "CollectionHealth", user=13)).json()
            assert health["counters"] is None and health["queueDepth"] is None
            assert not health["durableLossAccounting"] and health["crashLoss"] == "not_measurable"
            assert "12345" not in str(health)
    asyncio.run(run())

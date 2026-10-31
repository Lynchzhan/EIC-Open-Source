import asyncio
import os
from pathlib import Path

import pytest

TEST_DB = Path(__file__).with_name("test_eic.db")
TEST_DB.unlink(missing_ok=True)
os.environ["EIC_DATABASE_URL"] = "sqlite+aiosqlite:///" + TEST_DB.as_posix()
os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"] = "TestOnly-Admin-Password-2026-Long"
os.environ["EIC_JWT_SECRET"] = "test-jwt-secret-with-at-least-thirty-two-characters"
os.environ["EIC_SECRET_ENCRYPTION_KEY"] = "test-encryption-secret-with-at-least-thirty-two-characters"

from fastapi.testclient import TestClient

from eic.core.database import SessionLocal
from eic.main import app, bootstrap_session
from eic.models import KnowledgeChunk, KnowledgeDocument, Resource


def test_empty_environment_core_compatibility_flow(monkeypatch) -> None:
    async def fake_model_stream(*_, **__) -> object:
        yield "企智"
        yield "中心"

    monkeypatch.setattr("eic.main.stream_chat_completion", fake_model_stream)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        login = client.post(
            "/api/Authorize/GetToken",
            json={"name": "admin", "passWord": os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"], "tenantId": 1000},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()}"}
        tenant_id = client.get("/api/User/GetUser", headers=headers).json()["tenantId"]

        assert client.post(
            "/api/AIModels/AddEdit",
            headers=headers,
            json={"endPoint": "http://127.0.0.1:11434/v1", "modelName": "qwen3:4b", "apiKey": "test-secret-value"},
        ).json() is True
        model = client.post("/api/AIModels/GetPageData", headers=headers, json={}).json()["data"][0]
        assert model["secretConfigured"] is True
        assert model["secretHint"].endswith("alue")
        assert "apiKey" not in model
        assert "secret_encrypted" not in model

        assert client.post("/api/AIApps/AddEdit", headers=headers, json={"name": "测试智能体", "secretKey": "app-secret-value"}).json() is True
        assert client.post("/api/AIApps/GetPageData", headers=headers, json={}).json()["total"] == 1
        app_config = client.post("/api/AIApps/GetPageData", headers=headers, json={}).json()["data"][0]
        assert "secretKey" not in app_config and "SecretKey" not in app_config and "secret_encrypted" not in app_config

        assert client.post("/api/AIKmss/AddEdit", headers=headers, json={"name": "测试知识库"}).json() is True
        knowledge_base = client.post("/api/AIKmss/GetList", headers=headers, json={}).json()["data"][0]
        assert client.post("/api/AIKmss/GetPageData", headers=headers, json={}).json()["total"] == 1

        async def fake_enqueue(event_id: int, tenant_id: int, **kwargs) -> str:
            from eic.core.database import SessionLocal
            from eic.models import OutboxEvent
            async with SessionLocal() as session:
                event = await session.get(OutboxEvent, event_id)
                assert event.entity_id == knowledge_base["id"] and event.kind == "knowledge-base"
            assert tenant_id == tenant_id_from_user
            return f"eic-outbox:{tenant_id}:{event_id}"

        tenant_id_from_user = tenant_id
        monkeypatch.setattr("eic.core.outbox._inline_kinds", lambda *_: set())
        monkeypatch.setattr("eic.core.queue.enqueue_outbox", fake_enqueue)
        queued = client.post(f"/api/AIKmss/QueueIndex?Id={knowledge_base['id']}", headers=headers)
        result = queued.json()
        assert result["queued"] is True and result["status"] == "pending"
        assert result["jobId"].startswith(f"eic-outbox:{tenant_id}:")

        message = client.post("/api/AIChatHistorys/Add", headers=headers, json={"id": 987654321, "content": "你好"})
        stream = client.get(message.json()["streamUrl"])
        assert stream.status_code == 200
        assert '"text": "企智"' in stream.text
        assert '"text": "中心"' in stream.text
        assert "event: aimsg" in stream.text
        assert "event: done" in stream.text
        audit_events = client.get("/api/AIChatHistorys/GetEvents?requestId=987654321", headers=headers).json()
    assert [event["event"] for event in audit_events] == ["processmsg", "aIReasoningContentMsg", "processmsg", "reasoningConfig", "aimsg", "aimsg", "done"]


def test_bootstrap_session_is_available_without_database_advisory_lock() -> None:
    async def run() -> None:
        async with bootstrap_session() as session:
            assert session is not None

    asyncio.run(run())


@pytest.mark.parametrize("document_state", [
    "valid", "orphan", "private", "deleted", "wrong_tenant", "wrong_base", "missing_file",
])
def test_chat_stream_injects_bound_knowledge_evidence(monkeypatch, document_state) -> None:
    captured: dict[str, object] = {}
    offset = ["valid", "orphan", "private", "deleted", "wrong_tenant", "wrong_base", "missing_file"].index(document_state) * 10
    parent_id, document_id, child_id, resource_id = [9100000000000000 + offset + n for n in (1, 2, 3, 4)]

    async def fake_model_stream(_, question, *, messages=None, **__):
        captured["question"] = question
        captured["messages"] = messages or []
        yield "基于证据的回答"

    async def add_evidence(knowledge_base_id: int, tenant_id: int) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from eic.core.config import get_settings
        # 仅这三个历史脏数据反例使用独立、不启用 FK 的合成连接。
        # 运行时连接仍强制 FK；召回断言保持原样，不能让错误 fixture 绕过 API。
        legacy = create_async_engine(get_settings().database_url)
        factory = (async_sessionmaker(legacy, expire_on_commit=False)
                   if document_state in {"orphan", "wrong_tenant", "wrong_base"} else SessionLocal)
        async with factory() as session:
            file_id = resource_id if document_state in {"private", "deleted", "missing_file"} else None
            if file_id and document_state != "missing_file":
                session.add(Resource(
                    id=file_id, tenant_id=tenant_id, resource_type="files", name="fixture.txt",
                    data={"accessMode": "private" if document_state == "private" else "tenant"},
                    is_deleted=document_state == "deleted",
                ))
            if document_state != "orphan":
                session.add(KnowledgeDocument(
                    id=document_id,
                    tenant_id=tenant_id + 1 if document_state == "wrong_tenant" else tenant_id,
                    knowledge_base_id=knowledge_base_id + 1 if document_state == "wrong_base" else knowledge_base_id,
                    file_id=file_id, source_name="fixture.txt", content="企业分析父块证据", status="ready",
                ))
            await session.flush()
            session.add_all(
                [
                    KnowledgeChunk(
                        id=parent_id, tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id, document_id=document_id,
                        parent_chunk_id=None, chunk_index=-1, content="企业分析父块证据",
                        content_hash="a" * 64, attributes={"chunk_kind": "parent"},
                    ),
                    KnowledgeChunk(
                        id=child_id, tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id, document_id=document_id,
                        parent_chunk_id=parent_id, chunk_index=0, content="企业分析子块证据",
                        content_hash="b" * 64, attributes={"chunk_kind": "child"},
                    ),
                ]
            )
            await session.commit()
        await legacy.dispose()

    monkeypatch.setattr("eic.main.stream_chat_completion", fake_model_stream)
    with TestClient(app) as client:
        login = client.post(
            "/api/Authorize/GetToken",
            json={"name": "admin", "passWord": os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"], "tenantId": 1000},
        )
        headers = {"Authorization": f"Bearer {login.json()}"}
        tenant_id = client.get("/api/User/GetUser", headers=headers).json()["tenantId"]
        assert client.post(
            "/api/AIModels/AddEdit",
            headers=headers,
            json={"endPoint": "http://model.example/v1", "modelName": "mock-chat", "apiKey": "test"},
        ).json() is True
        model = next(
            item for item in client.post("/api/AIModels/GetPageData", headers=headers, json={}).json()["data"]
            if item["modelName"] == "mock-chat"
        )
        assert client.post("/api/AIKmss/AddEdit", headers=headers, json={"name": "证据知识库-" + document_state}).json() is True
        knowledge_base = next(
            item for item in client.post("/api/AIKmss/GetList", headers=headers, json={}).json()["data"]
            if item["name"] == "证据知识库-" + document_state
        )
        asyncio.run(add_evidence(knowledge_base["id"], tenant_id))
        assert client.post(
            "/api/AIApps/AddEdit",
            headers=headers,
            json={"name": "证据问答应用-" + document_state, "chatModelId": model["id"], "knowledgeBaseId": knowledge_base["id"]},
        ).json() is True
        app_config = next(
            item for item in client.post("/api/AIApps/GetPageData", headers=headers, json={}).json()["data"]
            if item["name"] == "证据问答应用-" + document_state
        )
        message = client.post(
            "/api/AIChatHistorys/Add",
            headers=headers,
            json={"content": "请分析企业分析证据", "aIChatsId": 0, "appId": app_config["id"]},
        )
        stream = client.get(message.json()["streamUrl"])

    assert stream.status_code == 200
    has_evidence = any("企业分析父块证据" in str(item.get("content", "")) for item in captured["messages"])
    assert ("event: retrieval" in stream.text) == (document_state == "valid")
    assert has_evidence == (document_state == "valid")
    assert "event: done" in stream.text

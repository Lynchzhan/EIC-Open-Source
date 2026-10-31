import asyncio
import json
import os

import httpx
from fastapi.testclient import TestClient

os.environ["EIC_DATABASE_URL"] = "sqlite+aiosqlite:///" + __file__.replace("test_m3_m5_features.py", "test_eic.db").replace("\\", "/")
os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"] = "TestOnly-Admin-Password-2026-Long"
os.environ["EIC_JWT_SECRET"] = "test-jwt-secret-with-at-least-thirty-two-characters"
os.environ["EIC_SECRET_ENCRYPTION_KEY"] = "test-encryption-secret-with-at-least-thirty-two-characters"

from eic.ai.mcp_client import HttpMcpClient
from eic.ai.skill_manager import SkillManager
from eic.knowledge.ingestion import build_ingestion_chunks
from eic.knowledge.retrieval import Bm25Index, filter_by_metadata
from eic.main import app
from eic.vector.qdrant_reader import VectorHit
from sdk_mcp_fixture import StandardServer


def test_parent_child_chunking_and_filtered_bm25() -> None:
    drafts = build_ingestion_chunks(
        "企智中心知识库用于企业分析。企智中心提供检索与智能问答。",
        source_name="guide.md",
        parent_size=24,
        child_size=12,
        child_overlap=2,
    )
    assert drafts
    assert all(draft.content_hash for draft in drafts)
    hits = [
        VectorHit("a", 0, {"content": "企智中心企业分析", "category": "guide"}),
        VectorHit("b", 0, {"content": "无关内容", "category": "other"}),
    ]
    filtered = filter_by_metadata(hits, {"category": "guide"})
    assert [hit.point_id for hit in Bm25Index(filtered).search("企智中心")] == ["a"]


def test_skill_manager_parses_manifest_and_reports_invalid_manifests(tmp_path) -> None:
    valid = tmp_path / "valid"
    valid.mkdir()
    (valid / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo\nversion: '1.0.0'\nallowed_tools: [calculator]\n---\n# Demo\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text("# missing metadata", encoding="utf-8")
    skills, failures = SkillManager().discover(tmp_path)
    assert skills[0].name == "demo"
    assert skills[0].allowed_tools == ("calculator",)
    assert len(failures) == 1


def test_http_mcp_client_uses_jsonrpc_tools_call() -> None:
    server = StandardServer()

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            return await HttpMcpClient({"transport": "http", "endpoint": "https://mcp.example/rpc"}, client=client).call_tool("lookup", {"q": "企智"})

    assert asyncio.run(run())["content"][0]["text"] == "中文结果"
    captured=next(p for p in server.payloads if p.get('method')=='tools/call')
    assert captured["params"]["name"] == "lookup"


def test_http_mcp_client_uses_sse_endpoint_handshake() -> None:
    server=StandardServer('sse')

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="https://mcp.example") as client:
            return await HttpMcpClient({"transport": "sse", "endpoint": "https://mcp.example/sse"}, client=client).call_tool("lookup", {})

    assert asyncio.run(run())["content"][0]["text"] == "中文结果"
    assert sum(r.method=='GET' for r in server.requests)==1
    assert server.calls()==1
    assert all(str(r.url)=='https://mcp.example/messages' for r in server.requests if r.method=='POST')
    assert all(s.closed for s in server.streams)


def test_session_memory_skill_tool_audit_and_task_flow(monkeypatch, tmp_path) -> None:
    skill = tmp_path / "regression-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: regression-skill\ndescription: Regression fixture\n"
        "version: '1.0.0'\nallowed_tools: [calculator]\n---\n# Fixture\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("eic.main.SKILL_ROOT", tmp_path)
    with TestClient(app) as client:
        login = client.post(
            "/api/Authorize/GetToken",
            json={"name": "admin", "passWord": os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"], "tenantId": 1000},
        )
        headers = {"Authorization": f"Bearer {login.json()}"}
        tenant_id = client.get("/api/User/GetUser", headers=headers).json()["tenantId"]
        refreshed = client.post("/api/AISkillToolManagement/RefreshLocalSkills", headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()["loaded"] == 1
        assert any(
            item["name"] == "regression-skill"
            for item in client.get("/api/AISkillToolManagement/GetAllSkills", headers=headers).json()
        )
        executed = client.post(
            "/api/AISkillToolManagement/ExecuteTool",
            headers=headers,
            json={"name": "calculator", "arguments": {"a": 2, "b": 3}},
        )
        assert executed.json()["result"] == {"value": 5.0}
        assert client.get("/api/AISkillToolManagement/GetToolAudits", headers=headers).json()[0]["outcome"] == "success"
        assert client.post('/api/AIApps/AddEdit',headers=headers,json={'name':'memory-agent','memoryEnabled':True}).json() is True
        agent=next(a for a in client.get('/api/AIApps/GetALLList',headers=headers).json() if a['name']=='memory-agent')
        memory = client.post("/api/AIAgentMemory/Save", headers=headers, json={"content": "用户关注 PGVector", "agentId": agent['id']})
        assert memory.status_code == 200
        assert client.get("/api/AIAgentMemory/Search?query=PGVector", headers=headers).json()[0]["agentId"] == agent['id']
        message = client.post("/api/AIChatHistorys/Add", headers=headers, json={"content": "历史记录测试", "aIChatsId": 0}).json()
        history = client.post("/api/AIChatHistorys/GetPageData", headers=headers, json={"aIChatsId": message["aIChatsId"]}).json()
        assert history["data"][0]["content"] == "历史记录测试"
        assert history["data"][0]["status"] == "streaming"
        created = client.post("/api/AIKmss/AddEdit", headers=headers, json={"name": "task-fixture-kb"})
        assert created.status_code == 200 and created.json() is True
        bases = client.post("/api/AIKmss/GetPageData", headers=headers, json={"pageSize": 100}).json()["data"]
        base_id = next(item["id"] for item in bases if item["name"] == "task-fixture-kb")
        assert client.post(
            "/api/AITasks/AddEdit",
            headers=headers,
            json={"name": "知识库夜间检查", "cron": "0 2 * * *", "taskType": "cron", "payload": {"handlerType": "knowledge-check", "knowledge_base_id": base_id}},
        ).json() is True
        task = client.post("/api/AITasks/GetTaskList", headers=headers, json={}).json()[0]

        async def fake_enqueue(event_id: int, actual_tenant_id: int, **kwargs: object) -> str:
            from eic.core.database import SessionLocal
            from eic.models import OutboxEvent, TaskRun
            async with SessionLocal() as session:
                event = await session.get(OutboxEvent, event_id)
                run = await session.get(TaskRun, event.entity_id)
                assert (run.task_id, actual_tenant_id) == (task["id"], tenant_id)
                assert event.kind == "scheduled-task" and kwargs["available_at"] is not None
                run_id = run.id
            return f"task-run-{run_id}"

        monkeypatch.setattr("eic.core.queue.enqueue_outbox", fake_enqueue)
        from unittest.mock import AsyncMock
        monkeypatch.setattr("eic.core.queue.task_capabilities", AsyncMock(return_value={"execution": True}))
        from eic.core.config import get_settings
        monkeypatch.setattr(get_settings(), "task_execution_enabled", True)
        trigger = client.post("/api/AITasks/Trigger?name=知识库夜间检查", headers=headers).json()
        assert trigger["queued"] is True
        assert trigger["jobId"] == f"task-run-{trigger['runId']}"
        status = client.get(f"/api/AITasks/{task['id']}/Status", headers=headers).json()
        assert status["runs"][0]["status"] == "queued"
        assert client.post(f"/api/AITasks/{task['id']}/Cancel", headers=headers).json() is True
        assert client.get(f"/api/AITasks/{task['id']}/Status", headers=headers).json()["status"] == "cancelled"
        for skill in client.get("/api/AISkillToolManagement/GetAllSkills", headers=headers).json():
            client.delete("/api/AISkillToolManagement/Delete", headers=headers, params={"Id": skill["id"]})


def test_mcp_configuration_is_tenant_bound_and_secret_is_not_serialized(monkeypatch) -> None:
    with TestClient(app) as client:
        login = client.post(
            "/api/Authorize/GetToken",
            json={"name": "admin", "passWord": os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"], "tenantId": 1000},
        )
        headers = {"Authorization": f"Bearer {login.json()}"}
        assert client.post(
            "/api/AISkillToolManagement/AddMcp", headers=headers,
            json={"name": "mock-mcp", "transport": "http", "endpoint": "https://mcp.example/rpc", "headers": {"Authorization": "Bearer hidden"}},
        ).json() is True
        item = client.get("/api/AISkillToolManagement/GetAllMcps", headers=headers).json()[0]
        assert item["name"] == "mock-mcp"
        assert "headers_encrypted" not in item and "hidden" not in json.dumps(item)
        assert item["secretConfigured"] is True
        assert client.delete("/api/AISkillToolManagement/DeleteMcp", headers=headers, params={"Id": item["id"]}).json() is True

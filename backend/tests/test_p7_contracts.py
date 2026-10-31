"""响应类型和拒绝副作用契约；不将 HTTP 200 等同业务成功。"""
import asyncio

from sqlalchemy import func, select

from p7_fixtures import p7_world


def test_login_scalar_and_invalid_password_no_provider(p7_world):
    w = p7_world
    ok = w["client"].post("/api/Authorize/GetToken", json={
        "name": "A", "passWord": "Fixture-Only-P7-Password", "tenantId": 7100,
    })
    assert ok.status_code == 200 and isinstance(ok.json(), str) and ok.json().count(".") == 2
    bad = w["client"].post("/api/Authorize/GetToken", json={
        "name": "A", "passWord": "incorrect", "tenantId": 7100,
    })
    assert bad.status_code == 400 and bad.json()["detail"]["errMsg"]
    assert w["calls"] == []


def test_owner_tenant_role_and_rejected_write_side_effects(p7_world):
    from eic.models import Resource
    w = p7_world
    for user in ("A", "C"):
        result = w["client"].post("/api/AIChats/Add", headers=w["headers"][user],
                                  json={"name": "chat-" + user})
        assert result.status_code == 200 and result.json() is True
    for user, expected in (("A", ["chat-A"]), ("B", []), ("C", ["chat-C"])):
        response = w["client"].post("/api/AIChats/GetMyPageData", headers=w["headers"][user],
                                    json={"pageSize": 100})
        assert response.status_code == 200, response.text
        page = response.json()
        assert isinstance(page["total"], int) and isinstance(page["data"], list)
        assert [item["name"] for item in page["data"]] == expected
    async def count():
        async with w["sessions"]() as session:
            return await session.scalar(select(func.count()).select_from(Resource))
    before = asyncio.run(count())
    bad = w["client"].post("/api/AIApps/AddEdit", headers=w["headers"]["B"], json={"name": "denied"})
    assert bad.status_code == 403 and bad.json()["detail"]["permission"] == "ai.resource.write"
    assert asyncio.run(count()) == before and w["calls"] == []


def test_rejected_chat_never_creates_messages_or_calls_provider(p7_world):
    from eic.models import ChatMessage
    w = p7_world
    result = w["client"].post("/api/AIChatHistorys/Add", headers=w["headers"]["B"],
                              json={"id": "invalid/request", "content": "never-execute"})
    assert result.status_code == 400 and result.json()["detail"]["errMsg"] == "对话请求 ID 无效"
    async def count():
        async with w["sessions"]() as session:
            return await session.scalar(select(func.count()).select_from(ChatMessage))
    assert asyncio.run(count()) == 0 and w["calls"] == []

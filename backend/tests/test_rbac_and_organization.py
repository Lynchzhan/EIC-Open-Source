import asyncio
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

TEST_DB = Path(__file__).with_name("test_eic.db")
os.environ.setdefault("EIC_DATABASE_URL", "sqlite+aiosqlite:///" + TEST_DB.as_posix())
os.environ.setdefault("EIC_BOOTSTRAP_ADMIN_PASSWORD", "TestOnly-Admin-Password-2026-Long")
os.environ.setdefault("EIC_JWT_SECRET", "test-jwt-secret-with-at-least-thirty-two-characters")
os.environ.setdefault("EIC_SECRET_ENCRYPTION_KEY", "test-encryption-secret-with-at-least-thirty-two-characters")

from eic.core.database import SessionLocal
from eic.main import app
from eic.models import AIModel
from eic.seed import _backfill_ai_app_display_fields


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/Authorize/GetToken",
        json={"name": username, "passWord": password, "tenantId": 1000},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()}"}


def test_database_rbac_and_organization_crud() -> None:
    with TestClient(app) as client:
        admin_headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        assert client.get("/api/Permission/GetUserPermissions", headers=admin_headers).json() == ["*"]

        assert client.post(
            "/api/Role/AddEdit", headers=admin_headers, json={"name": "只读模型", "description": "测试角色"}
        ).json() is True
        roles = client.post("/api/Role/GetPageData", headers=admin_headers, json={}).json()["data"]
        role = next(item for item in roles if item["name"] == "只读模型")
        role_id = role["id"]
        assert client.post(
            "/api/User/AddEdit",
            headers=admin_headers,
            json={"username": "readonly", "password": "Readonly-Password-2026", "roleIds": [role_id]},
        ).json() is True
        readonly_headers = login(client, "readonly", "Readonly-Password-2026")
        assert client.get("/api/Permission/GetUserPermissions", headers=readonly_headers).json() == []
        assert client.post("/api/AIModels/GetPageData", headers=readonly_headers, json={}).status_code == 403

        assert client.post(
            "/api/Permission/EditAllAreaPermissions",
            headers=admin_headers,
            json={"roleId": role_id, "permissionIds": ["ai.model.read"]},
        ).json() is True
        assert client.get("/api/Permission/GetUserPermissions", headers=readonly_headers).json() == ["ai.model.read"]
        assert client.post("/api/AIModels/GetPageData", headers=readonly_headers, json={}).status_code == 200
        assert client.post("/api/AIModels/AddEdit", headers=readonly_headers, json={"endPoint": "x", "modelName": "y"}).status_code == 403

        assert client.post("/api/Department/AddEdit", headers=admin_headers, json={"name": "研发部"}).json() is True
        department = client.get("/api/Department/GetALLList", headers=admin_headers).json()[0]
        assert client.post("/api/Department/AddEdit", headers=admin_headers, json={"name": "平台组", "parentId": department["id"]}).json() is True
        tree = client.get("/api/Department/GetDepartmentTree", headers=admin_headers).json()
        assert tree[0]["children"][0]["name"] == "平台组"
        assert client.post("/api/Position/AddEdit", headers=admin_headers, json={"name": "后端工程师"}).json() is True
        assert client.post("/api/Position/GetPageData", headers=admin_headers, json={}).json()["total"] == 1


def test_stream_cookie_cannot_cross_tenant_or_user() -> None:
    with TestClient(app) as client:
        admin_headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        assert client.post("/api/Role/AddEdit", headers=admin_headers, json={"name": "聊天用户"}).json() is True
        chat_role = next(item for item in client.post("/api/Role/GetPageData", headers=admin_headers, json={}).json()["data"] if item["name"] == "聊天用户")
        assert client.post("/api/Permission/EditAllAreaPermissions", headers=admin_headers, json={"roleId": chat_role["id"], "permissionIds": ["ai.chat.write", "ai.chat.read"]}).json() is True
        assert client.post(
            "/api/User/AddEdit", headers=admin_headers,
            json={"username": "stream-owner", "password": "Stream-Password-2026", "roleIds": [chat_role["id"]]},
        ).json() is True
        owner_headers = login(client, "stream-owner", "Stream-Password-2026")
        created = client.post("/api/AIChatHistorys/Add", headers=owner_headers, json={"content": "private"})
        assert created.status_code == 200
        request_id = created.json()["id"]
        owner_cookie = client.cookies.get("eic_stream_token")
        client.cookies.clear()
        assert client.get(f"/api/AIChatHistorys/Stream?requestId={request_id}", cookies={"eic_stream_token": owner_cookie}).status_code == 200
        assert client.get("/api/AIChatHistorys/Stream?requestId=999999999", cookies={"eic_stream_token": owner_cookie}).status_code == 404

def test_legacy_frontend_field_aliases() -> None:
    with TestClient(app) as client:
        admin_headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        empty_body = client.post("/api/AITasks/GetTaskList", headers=admin_headers)
        assert empty_body.status_code == 200
        assert isinstance(empty_body.json(), list)

        tree = client.get("/api/Department/GetDepartmentTree", headers=admin_headers).json()
        assert tree and tree[0]["name"]
        assert tree[0]["status"] == 1
        assert "children" in tree[0]

        tenants = client.post("/api/Tenant/GetPageData", headers=admin_headers, json={}).json()["data"]
        assert tenants[0]["status"] == 1
        assert tenants[0]["createTime"]
        assert client.post(
            "/api/Tenant/AddEdit",
            headers=admin_headers,
            json={"id": tenants[0]["id"], "active": True},
        ).json() is True

        permissions = client.post("/api/Permission/GetPageData", headers=admin_headers, json={"pageNum": 1, "pageSize": 10}).json()["data"]
        assert permissions[0]["fullName"]
        assert permissions[0]["areaName"]
        assert permissions[0]["isManual"] is False

        roles = client.post("/api/Role/GetPageData", headers=admin_headers, json={}).json()["data"]
        assert "remarks" in roles[0]

        area_tree = client.get(f"/api/Permission/GetAllAreaPermissions?roleId={roles[0]['id']}", headers=admin_headers).json()
        assert area_tree["dtos"]
        assert area_tree["dtos"][0]["permissionType"] == "接口权限"
        assert area_tree["dtos"][0]["dtos"][0]["areaName"]
        assert area_tree["dtos"][0]["dtos"][0]["modules"][0]["actions"]
        assert area_tree["dtos"][0]["dtos"][0]["modules"][0]["actions"][0]["actionName"]

def test_chat_history_reuses_chat_id_and_unconfigured_stream() -> None:
    with TestClient(app) as client:
        admin_headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        assert client.post(
            "/api/AIApps/AddEdit",
            headers=admin_headers,
            json={"name": "展示字段测试", "description": "卡片描述", "describe": "卡片描述"},
        ).json() is True
        apps = client.post("/api/AIApps/GetPageData", headers=admin_headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        display_app = next(item for item in apps if item["name"] == "展示字段测试")
        assert display_app["describe"] == "卡片描述"
        assert display_app["type"] == "chat"

        assert client.post(
            "/api/AIChats/Add",
            headers=admin_headers,
            json={"name": "会话绑定测试", "appId": display_app["id"], "aIAppsId": display_app["id"]},
        ).json() is True
        chats = client.post("/api/AIChats/GetMyPageData", headers=admin_headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        chat = next(item for item in chats if item["name"] == "会话绑定测试")

        created = client.post(
            "/api/AIChatHistorys/Add",
            headers=admin_headers,
            json={"content": "你好", "aIChatsId": chat["id"], "appId": display_app["id"]},
        )
        assert created.status_code == 200
        body = created.json()
        assert int(body["aIChatsId"]) == int(chat["id"])

        history = client.post(
            "/api/AIChatHistorys/GetPageData",
            headers=admin_headers,
            json={"pageNum": 1, "pageSize": 50, "aIChatsId": chat["id"]},
        ).json()["data"]
        assert any(int(item["aIChatsId"]) == int(chat["id"]) and item["content"] == "你好" for item in history)

        async def disable_chat_models() -> list[int]:
            async with SessionLocal() as session:
                models = list(await session.scalars(select(AIModel).where(AIModel.enabled.is_(True))))
                ids = [model.id for model in models]
                for model in models:
                    model.enabled = False
                await session.commit()
                return ids

        async def restore_chat_models(model_ids: list[int]) -> None:
            async with SessionLocal() as session:
                for model_id in model_ids:
                    model = await session.get(AIModel, model_id)
                    if model is not None:
                        model.enabled = True
                await session.commit()

        disabled_ids = asyncio.run(disable_chat_models())
        try:
            stream = client.get(body["streamUrl"])
            assert stream.status_code == 200
            assert "event: done" in stream.text
            assert "尚未配置可用对话模型" in stream.text
            assert "请在模型管理中创建模型配置" in stream.text
            history_after = client.post(
                "/api/AIChatHistorys/GetPageData",
                headers=admin_headers,
                json={"pageNum": 1, "pageSize": 50, "aIChatsId": chat["id"]},
            ).json()["data"]
            assert any(item.get("isSend") is False and "请在模型管理中创建模型配置" in str(item.get("content") or "") for item in history_after)
        finally:
            asyncio.run(restore_chat_models(disabled_ids))
            client.delete(f"/api/AIChats/Delete?Id={chat['id']}", headers=admin_headers)
            client.delete(f"/api/AIApps/Delete?Id={display_app['id']}", headers=admin_headers)


def test_seed_ai_app_display_fields_are_backfilled() -> None:
    with TestClient(app) as client:
        login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])

        async def run() -> None:
            async with SessionLocal() as session:
                from eic.models import AIApp, Resource, Tenant

                tenant = await session.scalar(select(Tenant).where(Tenant.code == 1000))
                assert tenant is not None
                stamp = int(time.time() * 1000)
                item = Resource(
                    id=stamp * 1000 + 99,
                    tenant_id=tenant.id,
                    resource_type="ai_apps",
                    name="backfill-app",
                    owner_id=None,
                    data={},
                )
                session.add(item)
                await session.flush()
                await _backfill_ai_app_display_fields(session, tenant)
                await session.commit()
                refreshed = await session.get(Resource, item.id)
                assert refreshed.data["describe"] == "backfill-app"
                assert refreshed.data["type"] == "chat"
                refreshed.is_deleted = True
                await session.commit()

                described = Resource(
                    id=stamp * 1000 + 100,
                    tenant_id=tenant.id,
                    resource_type="ai_apps",
                    name="企业内部知识助手",
                    owner_id=None,
                    data={},
                )
                session.add(described)
                session.add(AIApp(
                    id=described.id,
                    tenant_id=tenant.id,
                    name="企业内部知识助手",
                    description="通用企业管理问答示例",
                ))
                await session.flush()
                await _backfill_ai_app_display_fields(session, tenant)
                await session.commit()
                refreshed_described = await session.get(Resource, described.id)
                assert refreshed_described.data["describe"] == "通用企业管理问答示例"
                refreshed_described.is_deleted = True
                await session.commit()

                named = Resource(
                    id=stamp * 1000 + 101,
                    tenant_id=tenant.id,
                    resource_type="ai_apps",
                    name="企业内部知识助手-旧回填",
                    owner_id=None,
                    data={"describe": "企业内部知识助手-旧回填", "type": "chat"},
                )
                session.add(named)
                session.add(AIApp(
                    id=named.id,
                    tenant_id=tenant.id,
                    name="企业内部知识助手-旧回填",
                    description="通用企业管理问答示例",
                ))
                await session.flush()
                await _backfill_ai_app_display_fields(session, tenant)
                await session.commit()
                refreshed_named = await session.get(Resource, named.id)
                assert refreshed_named.data["describe"] == "通用企业管理问答示例"
                refreshed_named.is_deleted = True
                await session.commit()

        asyncio.run(run())


def test_legacy_ai_model_enum_payload_and_test_connection(monkeypatch) -> None:
    async def fake_probe(model, secret, *, prompt=None, timeout_seconds=None):
        assert model.model_type == "chat"
        assert model.provider == "openai_compatible"
        assert str(model.endpoint).startswith("https://")
        assert secret == "sk-test-key"
        return {"ok": True, "message": "连接成功"}

    import eic.main as main_module

    original = main_module.probe_model_connection
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/AIModels/TestConnection")
    cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__ or ()))
    monkeypatch.setattr(cells["probe_model_connection"], "cell_contents", fake_probe)
    main_module.probe_model_connection = fake_probe
    try:
        with TestClient(app) as client:
            headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
            payload = {
                "aiType": 1,
                "aiModelType": 1,
                "endPoint": "llm.example.aliyuncs.com",
                "modelName": "qwen3.7-flash-enum",
                "modelKey": "sk-test-key",
                "modelDescription": "qwen3.7-flash-enum",
            }
            tested = client.post("/api/AIModels/TestConnection", headers=headers, json=payload)
            assert tested.status_code == 200
            result = tested.json()
            assert result["code"] == 200 and result["isSuccess"] is True
            assert result["data"]["ok"] is True
            assert result["data"]["message"] == "连接成功"
            assert result["data"]["diagnostics"]["configSource"] == "form"
            assert result["data"]["diagnostics"]["modelName"] == "qwen3.7-flash-enum"
            assert result["data"]["elapsedMs"] >= 0
            assert client.post("/api/AIModels/AddEdit", headers=headers, json=payload).json() is True
            row = next(
                item
                for item in client.post("/api/AIModels/GetPageData", headers=headers, json={}).json()["data"]
                if item["modelName"] == "qwen3.7-flash-enum"
            )
            assert row["aiType"] == 1
            assert row["aiModelType"] == 1
            listed = client.get("/api/AIModels/GetALLList?Type=1", headers=headers).json()
            assert any(item["modelName"] == "qwen3.7-flash-enum" for item in listed)
    finally:
        main_module.probe_model_connection = original



def test_secret_hint_fits_mysql_column() -> None:
    from eic.core.security import secret_hint

    assert secret_hint("") == ""
    assert secret_hint("abcd").endswith("abcd")
    long_secret = ("k" * 180) + "Y9N0"
    hint = secret_hint(long_secret)
    assert hint == "****Y9N0"
    assert len(hint) <= 24
    assert long_secret not in hint


def test_long_api_key_model_save_keeps_short_secret_hint() -> None:
    with TestClient(app) as client:
        headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        long_key = "sk-" + ("A" * 160) + "Y9N0"
        payload = {
            "aiType": 1,
            "aiModelType": 1,
            "endPoint": "https://llm.example.aliyuncs.com/compatible-mode/v1",
            "modelName": "long-key-save-model",
            "modelKey": long_key,
            "modelDescription": "long-key-save-model",
            "maxAskPromptSize": 262144,
            "answerTokens": 4096,
            "embeddingValueSize": 2048,
        }
        response = client.post("/api/AIModels/AddEdit", headers=headers, json=payload)
        assert response.status_code == 200
        assert response.json() is True
        row = next(
            item
            for item in client.post("/api/AIModels/GetPageData", headers=headers, json={}).json()["data"]
            if item["modelName"] == "long-key-save-model"
        )
        assert len(row["secretHint"] or "") <= 24
        assert row["secretHint"].endswith("Y9N0")
        assert long_key not in str(row)
        assert "apiKey" not in row
        assert "modelKey" not in row
        assert "secret_encrypted" not in row


def test_ai_model_save_database_error_returns_chinese_message() -> None:
    from eic.core.database import SessionLocal, get_session
    from sqlalchemy.exc import SQLAlchemyError

    async def failing_session():
        async with SessionLocal() as session:
            async def boom(*_args, **_kwargs):
                raise SQLAlchemyError("simulated db failure")

            session.commit = boom
            yield session

    with TestClient(app) as client:
        headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        app.dependency_overrides[get_session] = failing_session
        try:
            response = client.post(
                "/api/AIModels/AddEdit",
                headers=headers,
                json={
                    "aiType": 1,
                    "aiModelType": 1,
                    "endPoint": "https://llm.example.aliyuncs.com/compatible-mode/v1",
                    "modelName": "save-error-model",
                    "modelKey": "sk-test-save-error",
                },
            )
            assert response.status_code == 500
            assert response.json()["detail"]["errMsg"] == "模型保存失败"
        finally:
            app.dependency_overrides.pop(get_session, None)



def test_get_user_role_key_returns_role_options_without_userid() -> None:
    with TestClient(app) as client:
        headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        assert client.post("/api/Role/AddEdit", headers=headers, json={"name": "用户表单角色"}).json() is True
        response = client.get("/api/User/GetUserRoleKey", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        match = next(item for item in data if item["value"] == "用户表单角色")
        assert match["key"]
        created = client.post(
            "/api/User/EditUser",
            headers=headers,
            json={
                "name": "legacy-form-user",
                "nickName": "表单用户",
                "PassWord": "Legacy-Form-Password-2026",
                "roles": [{"id": match["key"], "name": match["value"]}],
                "status": True,
            },
        )
        assert created.status_code == 200
        assert created.json() is True
